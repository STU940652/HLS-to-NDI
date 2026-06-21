"""PyInstaller runtime hook: replicate gstreamer_bundle.pth environment setup.

The wheel stack expects `gstreamer_libs.setup_python_environment()` before PyGObject
imports. Normal installs get that from site-packages `.pth`; frozen apps do not.

Note: PyInstaller's built-in pyi_rth_gi hook runs after custom runtime hooks and
assigns GI_TYPELIB_PATH to sys._MEIPASS/gi_typelibs only. Gtk typelibs must be
copied into that folder at build time (see HLS_NDI_Player.spec).
"""
from __future__ import annotations

import os
import sys

import gstreamer_libs

gstreamer_libs.setup_python_environment()


def _register_windows_dll_dir(path: str) -> None:
    if sys.platform != "win32" or not os.path.isdir(path):
        return
    os.add_dll_directory(path)
    current = os.environ.get("PYGI_DLL_DIRS")
    os.environ["PYGI_DLL_DIRS"] = path + (os.pathsep + current if current else "")


if getattr(sys, "frozen", False):
    root = getattr(sys, "_MEIPASS", None)
    if root:
        for sub in (
            os.path.join("gstreamer_gtk", "bin"),
            os.path.join("gstreamer_libs", "bin"),
            os.path.join("gstreamer_python", "bin"),
        ):
            _register_windows_dll_dir(os.path.join(root, sub))

        _, dll_dirs = gstreamer_libs.gstreamer_env()
        for entry in dll_dirs.split(os.pathsep):
            _register_windows_dll_dir(entry)
