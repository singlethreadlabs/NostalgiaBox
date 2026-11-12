"""Background media work must preserve startup and ordinary tuning."""

import subprocess

from nostalgiabox.server.app import ServerState
from nostalgiabox.server.media import MediaProbe
from nostalgiabox.server.settings import ServerSettings


def _server(tmp_path, monkeypatch):
    root = tmp_path / "media"
    show = root / "Arthur"
    show.mkdir(parents=True)
    (show / "S01E01.mp4").write_bytes(b"complete")
    config = tmp_path / "config.yaml"
    config.write_text(
        f"channels:\n  - number: 2\n    name: Arthur\n    path: {show}\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "nostalgiabox.server.media.probe_media",
        lambda path: MediaProbe(600, "mp4", "h264", "aac"),
    )
    settings = ServerSettings(config, tmp_path / "server.db", tmp_path / "cache", root)
    return ServerState(settings), show, settings


def test_cached_catalog_allows_restart_with_unfinished_new_media(tmp_path, monkeypatch):
    server, show, settings = _server(tmp_path, monkeypatch)
    server.playback.close()
    (show / "new-download.mp4").write_bytes(b"partial download")
    attempted = []

    def probe(path):
        attempted.append(path.name)
        if path.name == "new-download.mp4":
            raise ValueError("unfinished media")
        return MediaProbe(600, "mp4", "h264", "aac")

    monkeypatch.setattr("nostalgiabox.server.media.probe_media", probe)
    restarted = ServerState(settings)
    try:
        assert restarted.indexed_count == 1
        assert "new-download.mp4" not in attempted
        assert restarted.scheduler.now(2).path.name == "S01E01.mp4"
    finally:
        restarted.playback.close()


def test_probe_timeout_is_retried_for_unchanged_completed_file(tmp_path, monkeypatch):
    server, show, _ = _server(tmp_path, monkeypatch)
    added = show / "S01E02.mp4"
    added.write_bytes(b"completed media")
    attempts = []

    def probe(path):
        attempts.append(path.name)
        if len(attempts) == 1:
            raise subprocess.TimeoutExpired("ffprobe", 20)
        return MediaProbe(600, "mp4", "h264", "aac")

    monkeypatch.setattr("nostalgiabox.server.media.probe_media", probe)
    try:
        indexer = server.indexer
        indexer.prepare(stable_seconds=120, now=1000)
        indexer.refresh(scan=indexer.prepare(stable_seconds=120, now=1120))
        assert indexer.refresh(scan=indexer.prepare(stable_seconds=120, now=1180)) == 2
        assert attempts == [added.name, added.name]
    finally:
        server.playback.close()


def test_background_worker_retries_failures_and_stops(tmp_path, monkeypatch):
    server, _, _ = _server(tmp_path, monkeypatch)
    scans = []
    original_refresh = server.refresh

    class StopAfterTwoPasses:
        def __init__(self):
            self.waits = 0

        def is_set(self):
            return self.waits >= 2

        def wait(self, seconds):
            assert seconds == 60
            self.waits += 1
            return self.is_set()

    def scan(*, automatic):
        assert automatic
        scans.append(automatic)
        if len(scans) == 1:
            raise OSError("temporary media mount failure")
        return original_refresh(automatic=automatic)

    server.stopping = StopAfterTwoPasses()
    monkeypatch.setattr(server, "refresh", scan)
    try:
        server.maintain()
        assert len(scans) == 2
        assert server.media_scan_status == "ready"
        server.maintain()
        assert len(scans) == 2
    finally:
        server.playback.close()


def test_restart_bootstraps_a_new_configured_channel(tmp_path, monkeypatch):
    server, _, settings = _server(tmp_path, monkeypatch)
    server.playback.close()
    second = settings.media_root / "Rugrats"
    second.mkdir()
    episode = second / "S01E01.mp4"
    episode.write_bytes(b"completed media")
    with settings.config_path.open("a", encoding="utf-8") as config:
        config.write(f"  - number: 3\n    name: Rugrats\n    path: {second}\n")

    restarted = ServerState(settings)
    try:
        assert restarted.indexed_count == 2
        assert restarted.scheduler.now(3).path.name == episode.name
    finally:
        restarted.playback.close()
