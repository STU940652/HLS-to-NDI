"""PyInstaller runtime hook: replicate gstreamer_bundle.pth environment setup.

The wheel stack expects `gstreamer_libs.setup_python_environment()` before PyGObject
imports. Normal installs get that from site-packages `.pth`; frozen apps do not.
"""
from __future__ import annotations

import os
import sys

import gstreamer_libs

gstreamer_libs.setup_python_environment()


def _prepend_env_path(key: str, path: str) -> None:
    if not os.path.isdir(path):
        return
    current = os.environ.get(key)
    os.environ[key] = path + (os.pathsep + current if current else "")


def _register_windows_dll_dir(path: str) -> None:
    if sys.platform != "win32" or not os.path.isdir(path):
        return
    os.add_dll_directory(path)
    current = os.environ.get("PYGI_DLL_DIRS")
    os.environ["PYGI_DLL_DIRS"] = path + (os.pathsep + current if current else "")


if getattr(sys, "frozen", False):
    # Gtk typelibs live only under gstreamer_gtk. gstreamer_libs.gstreamer_env()
    # imports that package opportunistically; if the import fails in the frozen
    # bundle, GI_TYPELIB_PATH never includes Gtk and gi.require_version() errors.
    root = getattr(sys, "_MEIPASS", None)
    if root:
        for sub in (
            os.path.join("gstreamer_gtk", "lib", "girepository-1.0"),
            os.path.join("gstreamer_python", "lib", "girepository-1.0"),
            os.path.join("gstreamer_libs", "lib", "girepository-1.0"),
            "gi_typelibs",
        ):
            _prepend_env_path("GI_TYPELIB_PATH", os.path.join(root, sub))

        for sub in (
            os.path.join("gstreamer_gtk", "bin"),
            os.path.join("gstreamer_libs", "bin"),
            os.path.join("gstreamer_python", "bin"),
        ):
            _register_windows_dll_dir(os.path.join(root, sub))

        _, dll_dirs = gstreamer_libs.gstreamer_env()
        for entry in dll_dirs.split(os.pathsep):
            _register_windows_dll_dir(entry)
