import importlib.util
from pathlib import Path

import pytest


SCRIPT = Path(__file__).parents[1] / "scripts" / "archive-download-unique.py"
SPEC = importlib.util.spec_from_file_location("archive_download_unique", SCRIPT)
archive = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(archive)


def test_item_identifier_accepts_download_url():
    assert (
        archive.item_identifier("https://archive.org/download/example-item")
        == "example-item"
    )


def test_select_unique_prefers_original_for_same_episode():
    files = [
        {
            "name": "Show - S01E01 - Pilot.mp4",
            "source": "derivative",
            "original": "Show - S01E01 - Pilot.mkv",
            "height": "480",
            "width": "854",
            "size": "100",
        },
        {
            "name": "Show - S01E01 - Pilot.mkv",
            "source": "original",
            "height": "1080",
            "width": "1920",
            "size": "200",
        },
        {
            "name": "Show - S01E02 - Next.mkv",
            "source": "original",
            "height": "1080",
            "width": "1920",
            "size": "200",
        },
    ]

    selected = archive.select_unique(files)

    assert [file["name"] for file in selected] == [
        "Show - S01E01 - Pilot.mkv",
        "Show - S01E02 - Next.mkv",
    ]


def test_select_unique_can_prefer_mp4_for_compatibility():
    files = [
        {
            "name": "Show - S01E01 - Pilot.mp4",
            "source": "derivative",
            "original": "Show - S01E01 - Pilot.mkv",
            "height": "480",
            "width": "854",
            "size": "100",
        },
        {
            "name": "Show - S01E01 - Pilot.mkv",
            "source": "original",
            "height": "1080",
            "width": "1920",
            "size": "200",
        },
    ]

    selected = archive.select_unique(files, preferred_extension="mp4")

    assert [file["name"] for file in selected] == ["Show - S01E01 - Pilot.mp4"]


def test_select_unique_groups_movie_versions_and_can_prefer_smallest():
    files = [
        {"name": "Aladdin (1993 VHS).mp4", "size": "200", "source": "original"},
        {
            "name": "Aladdin (1993 VHS) (Version 2).mp4",
            "size": "100",
            "source": "original",
        },
        {"name": "Bambi (1989 VHS).mp4", "size": "150", "source": "original"},
    ]

    selected = archive.select_unique(
        files, "mp4", group_movies=True, prefer_smallest=True
    )

    assert [file["name"] for file in selected] == [
        "Aladdin (1993 VHS) (Version 2).mp4",
        "Bambi (1989 VHS).mp4",
    ]


def test_movie_key_ignores_release_and_version_markers():
    assert archive.movie_key("Dumbo (1991 VHS) (Version 2).mp4") == archive.movie_key(
        "Dumbo (2002 VHS).mp4"
    )


def test_episode_key_keeps_different_shows_separate():
    assert archive.episode_key("First Show S01E01.mkv") != archive.episode_key(
        "Second Show S01E01.mp4"
    )


def test_safe_destination_rejects_parent_traversal(tmp_path):
    with pytest.raises(ValueError, match="unsafe archive filename"):
        archive.safe_destination(tmp_path, "../outside.mp4")


class FakeResponse:
    def __init__(self, body, status):
        self.body = body
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def getcode(self):
        return self.status

    def read(self, _size):
        body, self.body = self.body, b""
        return body


def test_download_once_resumes_partial_file(tmp_path, monkeypatch):
    destination = tmp_path / "episode.mp4"
    partial = tmp_path / "episode.mp4.part"
    partial.write_bytes(b"first")
    requests = []
    positions = []

    def fake_urlopen(request, timeout):
        requests.append((request, timeout))
        return FakeResponse(b"second", 206)

    monkeypatch.setattr(archive, "urlopen", fake_urlopen)

    result = archive.download_once(
        "https://example.test/episode.mp4", destination, 11, positions.append
    )

    assert destination.read_bytes() == b"firstsecond"
    assert requests[0][0].get_header("Range") == "bytes=5-"
    assert requests[0][1] == 90
    assert positions == [5, 11]
    assert "resumed" in result


def test_download_once_restarts_when_server_ignores_range(tmp_path, monkeypatch):
    destination = tmp_path / "episode.mp4"
    (tmp_path / "episode.mp4.part").write_bytes(b"stale")
    monkeypatch.setattr(
        archive,
        "urlopen",
        lambda _request, timeout: FakeResponse(b"complete", 200),
    )

    archive.download_once("https://example.test/episode.mp4", destination, 8)

    assert destination.read_bytes() == b"complete"


def test_progress_tracker_replaces_positions_instead_of_double_counting():
    tracker = archive.ProgressTracker()

    tracker.update("one.mp4", 10)
    tracker.update("two.mp4", 5)
    tracker.update("one.mp4", 12)

    assert tracker.total() == 17
