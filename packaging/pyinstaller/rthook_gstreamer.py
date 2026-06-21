"""PyInstaller runtime hook: replicate gstreamer_bundle.pth environment setup.

The wheel stack expects `gstreamer_libs.setup_python_environment()` before PyGObject
imports. Normal installs get that from site-packages `.pth`; frozen apps do not.

PyInstaller's pyi_rth_gi hook runs after this hook and sets GI_TYPELIB_PATH to
gi_typelibs only. Wheel typelibs are mirrored there at build time (see spec).

Frozen bundles flatten the wheel layout: gi is not nested under gstreamer_python, so
importing gstreamer_python (which walks for Lib/site-packages/gi) fails on macOS.
Use a frozen-specific environment merge that skips that import.
"""
from __future__ import annotations

import importlib
import os
import sys


def _prepend_to_env(env: dict[str, str], key: str, value: str | list) -> None:
    if isinstance(value, list):
        for item in value:
            _prepend_to_env(env, key, item)
        return
    old = env.get(key)
    env[key] = value + (os.pathsep + old if old else "")


def _frozen_gstreamer_packages() -> tuple[str, ...]:
    packages = [
        "gstreamer_libs",
        "gstreamer_gtk",
        "gstreamer_plugins",
        "gstreamer_plugins_restricted",
        "gstreamer_plugins_gpl",
        "gstreamer_plugins_gpl_restricted",
        "gstreamer_plugins_libs",
        "gstreamer_plugins_frei0r",
        "gstreamer_cli",
    ]
    if sys.platform == "win32":
        packages.append("gstreamer_ext_runtime")
    return tuple(packages)


def _register_windows_dll_dir(path: str) -> None:
    if sys.platform != "win32" or not os.path.isdir(path):
        return
    os.add_dll_directory(path)
    current = os.environ.get("PYGI_DLL_DIRS")
    os.environ["PYGI_DLL_DIRS"] = path + (os.pathsep + current if current else "")


def _apply_frozen_gstreamer_environment(root: str) -> None:
    env = os.environ.copy()
    dll_directories: list[str] = []

    for name in _frozen_gstreamer_packages():
        try:
            module = importlib.import_module(name)
        except ImportError:
            continue
        for key, value in getattr(module, "environment", {}).items():
            if sys.platform in ("win32", "darwin") and key == "LD_LIBRARY_PATH":
                continue
            if sys.platform in ("win32", "darwin") and key == "PATH" and isinstance(value, str):
                for entry in value.split(os.pathsep):
                    if entry and entry != ".":
                        dll_directories.append(entry)
            _prepend_to_env(env, key, value)

    # gstreamer_python paths without importing its package (broken when gi is flattened).
    gp_root = os.path.join(root, "gstreamer_python")
    for sub, keys in (
        ("bin", ("PATH",)),
        ("lib/girepository-1.0", ("GI_TYPELIB_PATH",)),
        ("lib/gstreamer-1.0", ("GST_PLUGIN_PATH_1_0", "GST_PLUGIN_SYSTEM_PATH_1_0")),
    ):
        path = os.path.join(gp_root, sub)
        if os.path.isdir(path):
            if sub == "bin" and sys.platform == "win32":
                dll_directories.append(path)
            for key in keys:
                _prepend_to_env(env, key, path)

    os.environ.update(env)

    if sys.platform == "win32":
        for path in dll_directories:
            _register_windows_dll_dir(path)


if getattr(sys, "frozen", False):
    root = getattr(sys, "_MEIPASS", None)
    if root:
        _apply_frozen_gstreamer_environment(root)
else:
    import gstreamer_libs

    gstreamer_libs.setup_python_environment()
