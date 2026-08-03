import argparse
import importlib.util
import json
import sys
from pathlib import Path


SCRIPT = Path(__file__).parents[1] / "scripts" / "media-reduce-in-place.py"
SPEC = importlib.util.spec_from_file_location("media_reduce_in_place", SCRIPT)
reduce = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = reduce
SPEC.loader.exec_module(reduce)


def test_candidate_is_hidden_and_adjacent():
    source = Path("/media/Shows/Test/episode.mp4")

    assert reduce.candidate_path(source) == Path(
        "/media/Shows/Test/.episode.reduce-candidate.mp4"
    )


def test_completed_sources_only_resumes_matching_policy(tmp_path):
    journal = tmp_path / "journal.jsonl"
    policy = {"crf": 23}
    journal.write_text(
        "\n".join(
            [
                json.dumps(
                    {"source": "/a.mp4", "status": "replaced", "policy": policy}
                ),
                json.dumps(
                    {"source": "/b.mp4", "status": "retained", "policy": policy}
                ),
                json.dumps(
                    {"source": "/c.mp4", "status": "failed", "policy": policy}
                ),
                json.dumps(
                    {"source": "/d.mp4", "status": "replaced", "policy": {"crf": 24}}
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    assert reduce.completed_sources(journal, policy) == {"/a.mp4", "/b.mp4"}


def test_policy_captures_output_affecting_arguments():
    args = argparse.Namespace(
        max_height=720,
        crf=23,
        preset="slow",
        audio_bitrate="128k",
        minimum_savings_percent=15.0,
        minimum_savings_mib=50.0,
        skip_loudness_normalization=True,
        target_lufs=-20.0,
    )

    assert reduce.policy_from_args(args) == {
        "max_height": 720,
        "crf": 23,
        "preset": "slow",
        "audio_bitrate": "128k",
        "minimum_savings_percent": 15.0,
        "minimum_savings_mib": 50.0,
        "normalize_audio": False,
        "target_lufs": -20.0,
    }
