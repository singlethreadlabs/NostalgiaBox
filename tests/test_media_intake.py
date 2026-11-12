from types import SimpleNamespace

import pytest

from nostalgiabox.config import config_from_dict
from nostalgiabox.server.database import Database
from nostalgiabox.server.media import MediaIndexer, MediaProbe, probe_media
from nostalgiabox.server.schedule import Scheduler


def station(tmp_path, monkeypatch):
    root = tmp_path / 'media'
    show = root / 'Arthur'
    show.mkdir(parents=True)
    (show / 'S01E01.mp4').write_bytes(b'complete')
    config = config_from_dict({'channels': [{'number': 2, 'path': str(show)}]})
    database = Database(tmp_path / 'server.db')
    database.initialize()
    monkeypatch.setattr('nostalgiabox.server.media.probe_media', lambda path: MediaProbe(
        600, 'mov,mp4,m4a,3gp,3g2,mj2', 'h264', 'aac'
    ))
    indexer = MediaIndexer(database, config, root)
    indexer.refresh()
    return show, database, indexer, Scheduler(database, config)


def test_intake_waits_for_two_stable_observations(tmp_path, monkeypatch):
    show, database, indexer, scheduler = station(tmp_path, monkeypatch)
    added = show / 'S01E02.mp4'
    added.write_bytes(b'partial')
    assert indexer.refresh(scan=indexer.prepare(stable_seconds=120, now=1000)) == 1
    added.write_bytes(b'complete download')
    assert indexer.refresh(scan=indexer.prepare(stable_seconds=120, now=1120)) == 1
    assert indexer.refresh(scan=indexer.prepare(stable_seconds=120, now=1239)) == 1
    assert indexer.refresh(scan=indexer.prepare(stable_seconds=120, now=1240)) == 2


def test_unchanged_scan_preserves_future_and_current_programs(tmp_path, monkeypatch):
    show, database, indexer, scheduler = station(tmp_path, monkeypatch)
    scheduler.fill()
    with database.connect() as connection:
        before = [tuple(row) for row in connection.execute('SELECT * FROM programs')]
    indexer.refresh(scan=indexer.prepare(stable_seconds=120))
    with database.connect() as connection:
        assert [tuple(row) for row in connection.execute('SELECT * FROM programs')] == before


def test_invalid_addition_does_not_block_valid_addition(tmp_path, monkeypatch):
    show, database, indexer, scheduler = station(tmp_path, monkeypatch)
    (show / 'bad.mp4').write_bytes(b'bad')
    (show / 'good.mp4').write_bytes(b'good')
    valid_probe = MediaProbe(600, 'mp4', 'h264', 'aac')
    def probe(path):
        if path.name == 'bad.mp4':
            raise ValueError('invalid video')
        return valid_probe
    monkeypatch.setattr('nostalgiabox.server.media.probe_media', probe)
    indexer.prepare(stable_seconds=120, now=1000)
    assert indexer.refresh(scan=indexer.prepare(stable_seconds=120, now=1120)) == 2
    with database.connect() as connection:
        assert connection.execute("SELECT 1 FROM media_items WHERE path LIKE '%bad.mp4'").fetchone() is None


def test_snapshot_changed_before_commit_is_rejected(tmp_path, monkeypatch):
    show, database, indexer, scheduler = station(tmp_path, monkeypatch)
    added = show / 'S01E02.mp4'
    added.write_bytes(b'complete')
    snapshot = indexer.prepare()
    added.write_bytes(b'changed again')
    with pytest.raises(ValueError, match='changed'):
        indexer.refresh(scan=snapshot)
    with database.connect() as connection:
        assert connection.execute('SELECT COUNT(*) FROM media_items').fetchone()[0] == 1


def test_hidden_and_escaping_files_are_rejected(tmp_path, monkeypatch):
    show, database, indexer, scheduler = station(tmp_path, monkeypatch)
    (show / '.partial.mp4').write_bytes(b'partial')
    assert indexer.refresh() == 1
    outside = tmp_path / 'outside.mp4'
    outside.write_bytes(b'outside')
    (show / 'escape.mp4').symlink_to(outside)
    with pytest.raises(ValueError, match='escapes'):
        indexer.refresh()


@pytest.mark.parametrize('streams,duration', [([], '600'), ([{'codec_type': 'video', 'codec_name': 'h264'}], 'nan'), ([{'codec_type': 'video', 'codec_name': 'h264'}], None)])
def test_probe_requires_real_video_and_finite_duration(monkeypatch, tmp_path, streams, duration):
    import json
    monkeypatch.setattr('nostalgiabox.server.media.shutil.which', lambda name: '/bin/ffprobe')
    monkeypatch.setattr('nostalgiabox.server.media.subprocess.run', lambda *args, **kwargs: SimpleNamespace(
        returncode=0, stdout=json.dumps({'streams': streams, 'format': {'duration': duration}})
    ))
    with pytest.raises(ValueError):
        probe_media(tmp_path / 'bad.mp4')


def test_empty_show_pool_rejects_refresh_without_changing_catalog(tmp_path, monkeypatch):
    show, database, indexer, scheduler = station(tmp_path, monkeypatch)
    scheduler.fill()
    with database.connect() as connection:
        before = [tuple(row) for row in connection.execute('SELECT * FROM programs')]
    (show / 'S01E01.mp4').unlink()
    with pytest.raises(ValueError, match='no playable'):
        indexer.refresh()
    with database.connect() as connection:
        assert [tuple(row) for row in connection.execute('SELECT * FROM programs')] == before


def test_probe_cannot_accept_unvalidated_media_without_ffprobe(monkeypatch, tmp_path):
    monkeypatch.setattr("nostalgiabox.server.media.shutil.which", lambda name: None)
    with pytest.raises(RuntimeError, match="ffprobe"):
        probe_media(tmp_path / "unvalidated.mp4")


def test_snapshot_revalidates_path_containment_before_commit(tmp_path, monkeypatch):
    import os
    show, database, indexer, scheduler = station(tmp_path, monkeypatch)
    added = show / "S01E02.mp4"
    added.write_bytes(b"complete")
    snapshot = indexer.prepare()
    stat = added.stat()
    outside = tmp_path / "outside.mp4"
    outside.write_bytes(b"complete")
    os.utime(outside, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    added.unlink()
    added.symlink_to(outside)
    with pytest.raises(ValueError, match="escapes"):
        indexer.refresh(scan=snapshot)
