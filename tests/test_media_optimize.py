import importlib.util
import json
import sys
from pathlib import Path


SCRIPT = Path(__file__).parents[1] / "scripts" / "media-optimize.py"
SPEC = importlib.util.spec_from_file_location("media_optimize", SCRIPT)
optimizer = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = optimizer
SPEC.loader.exec_module(optimizer)


def media_info(**overrides):
    values = {
        "path": Path("episode.mp4"),
        "size": 200 * 1024 * 1024,
        "duration": 1_200,
        "width": 640,
        "height": 480,
        "video_codec": "h264",
        "audio_codec": "aac",
        "audio_bitrate": 96_000,
        "format_names": ("mov", "mp4"),
    }
    values.update(overrides)
    return optimizer.MediaInfo(**values)


def test_direct_480p_file_is_kept_when_savings_are_not_meaningful():
    recommendation = optimizer.recommend(media_info(), max_height=480)

    assert recommendation.action == "keep"
    assert recommendation.estimated_savings_bytes == 0
    assert recommendation.reasons == ()


def test_1080p_file_is_recommended_for_downscaling():
    recommendation = optimizer.recommend(
        media_info(size=500 * 1024 * 1024, width=1920, height=1080),
        max_height=480,
    )

    assert recommendation.action == "optimize"
    assert "resolution exceeds 480p" in recommendation.reasons
    assert recommendation.estimated_savings_bytes > 300 * 1024 * 1024


def test_hevc_file_is_flagged_for_browser_compatibility_without_fake_savings():
    recommendation = optimizer.recommend(
        media_info(size=100 * 1024 * 1024, video_codec="hevc"),
        max_height=480,
    )

    assert recommendation.action == "optimize"
    assert recommendation.estimated_savings_bytes == 0
    assert recommendation.direct_play is False


def test_high_audio_bitrate_alone_does_not_trigger_wasteful_video_encode():
    recommendation = optimizer.recommend(
        media_info(audio_bitrate=192_000),
        max_height=480,
    )

    assert recommendation.action == "keep"
    assert recommendation.estimated_savings_bytes == 0


def test_find_media_is_recursive_and_ignores_partial_files(tmp_path):
    nested = tmp_path / "show"
    nested.mkdir()
    episode = nested / "episode.mp4"
    episode.touch()
    (nested / "episode.mp4.part").touch()
    (nested / "notes.txt").touch()

    assert optimizer.find_media([tmp_path]) == [episode.resolve()]


def test_find_media_deduplicates_symlinks(tmp_path):
    source = tmp_path / "source.mp4"
    source.touch()
    alias = tmp_path / "alias.mp4"
    alias.symlink_to(source)

    assert optimizer.find_media([tmp_path]) == [source.resolve()]


def test_measure_loudness_parses_ffmpeg_json(monkeypatch):
    payload = {
        "input_i": "-21.4",
        "input_tp": "-2.3",
        "input_lra": "5.8",
        "input_thresh": "-31.2",
        "target_offset": "0.1",
    }

    def fake_run(*args, **kwargs):
        return type(
            "Result",
            (),
            {"returncode": 0, "stderr": "log output\n" + json.dumps(payload)},
        )()

    monkeypatch.setattr(optimizer.subprocess, "run", fake_run)

    measurement = optimizer.measure_loudness(Path("episode.mp4"))

    assert measurement.integrated_lufs == -21.4
    assert measurement.true_peak_dbtp == -2.3
    assert measurement.loudness_range_lu == 5.8
    assert measurement.threshold == -31.2
    assert measurement.offset == 0.1


def test_loudness_outside_target_is_recommended_for_normalization():
    recommendation = optimizer.recommend(media_info(), max_height=480)
    measured = optimizer.add_loudness(
        recommendation,
        optimizer.LoudnessMeasurement(-20.0, -2.0, 6.0),
    )

    assert measured.normalize_audio is True
    assert measured.integrated_lufs == -20.0


def test_loudness_inside_target_is_not_recommended_for_normalization():
    recommendation = optimizer.recommend(media_info(), max_height=480)
    measured = optimizer.add_loudness(
        recommendation,
        optimizer.LoudnessMeasurement(-16.4, -2.0, 6.0),
    )

    assert measured.normalize_audio is False
