from pathlib import Path

import nostalgiabox.mpv_loader as loader


def test_macos_loader_targets_homebrew_dylib(monkeypatch):
    sentinel = object()
    observed = {}

    monkeypatch.setattr(loader.sys, "platform", "darwin")
    monkeypatch.setattr(Path, "is_file", lambda path: str(path).startswith("/opt/homebrew"))

    def import_module(name):
        observed["name"] = name
        observed["library"] = loader.ctypes.util.find_library("mpv")
        return sentinel

    monkeypatch.setattr(loader.importlib, "import_module", import_module)

    assert loader.load_mpv() is sentinel
    assert observed == {
        "name": "mpv",
        "library": "/opt/homebrew/lib/libmpv.dylib",
    }
