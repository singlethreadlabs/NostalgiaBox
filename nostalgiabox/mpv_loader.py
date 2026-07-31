"""Load python-mpv, including Homebrew's libmpv location on macOS."""

from __future__ import annotations

import ctypes.util
import importlib
import sys
from pathlib import Path
from types import ModuleType


def load_mpv() -> ModuleType:
    """Import python-mpv without globally overriding macOS library lookup."""
    if sys.platform != "darwin":
        return importlib.import_module("mpv")

    dylib = next(
        (
            candidate
            for candidate in (
                Path("/opt/homebrew/lib/libmpv.dylib"),
                Path("/usr/local/lib/libmpv.dylib"),
            )
            if candidate.is_file()
        ),
        None,
    )
    if dylib is None:
        return importlib.import_module("mpv")

    original_find_library = ctypes.util.find_library

    def find_library(name: str) -> str | None:
        if name == "mpv":
            return str(dylib)
        return original_find_library(name)

    ctypes.util.find_library = find_library
    try:
        return importlib.import_module("mpv")
    finally:
        ctypes.util.find_library = original_find_library
