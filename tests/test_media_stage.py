import importlib.util
import json
import sys
from pathlib import Path


SCRIPT = Path(__file__).parents[1] / "scripts" / "media-stage.py"
SPEC = importlib.util.spec_from_file_location("media_stage", SCRIPT)
stage = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = stage
SPEC.loader.exec_module(stage)


def test_output_name_marks_samples():
    source = Path("episode.mkv")

    assert stage.output_name(source, sample_seconds=None) == "episode.mp4"
    assert stage.output_name(source, sample_seconds=60) == "episode.sample-60s.mp4"


def test_relative_output_preserves_subdirectories(tmp_path):
    root = tmp_path / "show"
    source = root / "Season 1" / "episode.mkv"
    source.parent.mkdir(parents=True)
    source.touch()

    assert stage.relative_output(source, [root], sample_seconds=None) == Path(
        "Season 1/episode.mp4"
    )


def test_loudnorm_filter_uses_first_pass_measurements():
    measurement = stage.optimizer.LoudnessMeasurement(
        integrated_lufs=-22.0,
        true_peak_dbtp=-5.0,
        loudness_range_lu=4.0,
        threshold=-32.0,
        offset=0.1,
    )

    value = stage.loudnorm_filter(measurement)

    assert "measured_I=-22.0" in value
    assert "measured_TP=-5.0" in value
    assert "measured_thresh=-32.0" in value
    assert "TP=-2.5" in value
    assert "linear=false" in value


def test_high_bitrate_h264_is_encoded_when_savings_are_meaningful():
    info = stage.optimizer.MediaInfo(
        path=Path("large.mp4"),
        size=600 * 1024 * 1024,
        duration=1_200,
        width=640,
        height=480,
        video_codec="h264",
        audio_codec="aac",
        audio_bitrate=128_000,
        format_names=("mp4",),
    )

    assert stage.should_encode_video(info, max_height=480) is True


def test_efficient_h264_is_stream_copied():
    info = stage.optimizer.MediaInfo(
        path=Path("efficient.mp4"),
        size=150 * 1024 * 1024,
        duration=1_200,
        width=640,
        height=480,
        video_codec="h264",
        audio_codec="aac",
        audio_bitrate=96_000,
        format_names=("mp4",),
    )

    assert stage.should_encode_video(info, max_height=480) is False


def test_probe_stream_layout_tracks_audio_and_subtitles(monkeypatch):
    payload = {
        "streams": [
            {"codec_type": "video", "codec_name": "h264"},
            {"codec_type": "audio", "codec_name": "aac"},
            {"codec_type": "audio", "codec_name": "ac3"},
            {"codec_type": "subtitle", "codec_name": "subrip"},
            {"codec_type": "data", "codec_name": "bin_data"},
        ]
    }
    monkeypatch.setattr(
        stage,
        "run",
        lambda *args, **kwargs: type(
            "Result", (), {"stdout": json.dumps(payload)}
        )(),
    )

    layout = stage.probe_stream_layout(Path("episode.mkv"))

    assert layout.audio_codecs == ("aac", "ac3")
    assert layout.subtitle_codecs == ("subrip",)


def test_repair_filter_uses_measured_aac_output():
    measurement = stage.optimizer.LoudnessMeasurement(
        integrated_lufs=-16.1,
        true_peak_dbtp=1.6,
        loudness_range_lu=9.0,
        threshold=-27.0,
        offset=-0.1,
    )

    value = stage.loudnorm_filter(measurement)

    assert "measured_I=-16.1" in value
    assert "measured_TP=1.6" in value
