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


_FROZEN_SKIP_ENV_KEYS = frozenset(
    {
        # gstreamer_gtk sets this to the bundle Frameworks dir; breaks child python3
        # processes (e.g. gst-plugin-scanner) and stdlib resolution (encodings).
        "PYTHONPATH",
        "GST_PYTHONPATH_1_0",
    }
)

_FROZEN_PACKAGE_NAMES = _frozen_gstreamer_packages() + ("gstreamer_python",)
_PLUGIN_SKIP_PACKAGES = frozenset({"gstreamer_python"})

# NDI Advanced SDK runtime (ndisink loads libndi.dylib from here on macOS).
_DARWIN_NDI_RUNTIME_V6 = "/Library/NDI SDK for Apple/lib/macOS/"


def _apply_darwin_ndi_runtime() -> None:
    if not os.environ.get("NDI_RUNTIME_DIR_V6"):
        os.environ["NDI_RUNTIME_DIR_V6"] = _DARWIN_NDI_RUNTIME_V6
    ndi_lib_dir = _DARWIN_NDI_RUNTIME_V6
    if os.path.isdir(ndi_lib_dir):
        existing = os.environ.get("DYLD_FALLBACK_LIBRARY_PATH", "")
        if ndi_lib_dir not in existing.split(os.pathsep):
            os.environ["DYLD_FALLBACK_LIBRARY_PATH"] = ndi_lib_dir + (
                os.pathsep + existing if existing else ""
            )


def _filesystem_package_roots(root: str) -> list[str]:
    """Discover wheel package dirs under the frozen bundle without importing them."""
    roots: list[str] = []
    for name in _FROZEN_PACKAGE_NAMES:
        candidate = os.path.join(root, name)
        if os.path.isdir(candidate):
            roots.append(candidate)
    return roots


# Bump when bundled GStreamer plugin set changes (forces gst-plugin-scanner rescan).
_DARWIN_GST_REGISTRY_VERSION = 4


def _darwin_user_registry_path() -> str:
    cache_root = os.path.join(
        os.path.expanduser("~/Library/Caches"),
        "HLS NDI Player",
        "gstreamer-1.0",
    )
    os.makedirs(cache_root, exist_ok=True)
    for legacy in (
        "registry.bin",
        "registry-v2.bin",
        "registry-v3.bin",
    ):
        try:
            os.remove(os.path.join(cache_root, legacy))
        except FileNotFoundError:
            pass
    return os.path.join(cache_root, f"registry-v{_DARWIN_GST_REGISTRY_VERSION}.bin")


def _apply_darwin_frozen_gstreamer_environment(root: str) -> None:
    """macOS bundle layout needs explicit GStreamer paths and no wheel PATH/PYTHONPATH."""
    _apply_darwin_ndi_runtime()

    for key in _FROZEN_SKIP_ENV_KEYS:
        os.environ.pop(key, None)

    os.environ["GST_REGISTRY_FORK"] = "no"

    lib_dirs: list[str] = []
    plugin_dirs: list[str] = []
    typelib_dirs: list[str] = []
    xdg_data_dirs: list[str] = []

    for package_root in _filesystem_package_roots(root):
        package_name = os.path.basename(package_root.rstrip(os.sep))
        lib_dir = os.path.join(package_root, "lib")
        if os.path.isdir(lib_dir):
            lib_dirs.append(lib_dir)

        if package_name not in _PLUGIN_SKIP_PACKAGES:
            plugin_dir = os.path.join(lib_dir, "gstreamer-1.0")
            if os.path.isdir(plugin_dir):
                plugin_dirs.append(plugin_dir)

        typelib_dir = os.path.join(lib_dir, "girepository-1.0")
        if os.path.isdir(typelib_dir):
            typelib_dirs.append(typelib_dir)

        if package_name not in _PLUGIN_SKIP_PACKAGES:
            share_dir = os.path.join(package_root, "share")
            if os.path.isdir(share_dir):
                xdg_data_dirs.append(share_dir)

    if lib_dirs:
        libs = os.pathsep.join(dict.fromkeys(lib_dirs))
        for key in ("DYLD_FALLBACK_LIBRARY_PATH", "DYLD_LIBRARY_PATH"):
            existing = os.environ.get(key, "")
            os.environ[key] = libs + (os.pathsep + existing if existing else "")

    if plugin_dirs:
        plugins = os.pathsep.join(dict.fromkeys(plugin_dirs))
        os.environ["GST_PLUGIN_PATH_1_0"] = plugins
        os.environ["GST_PLUGIN_SYSTEM_PATH_1_0"] = plugins

    libs_root = os.path.join(root, "gstreamer_libs")
    scanner = os.path.join(
        libs_root, "libexec", "gstreamer-1.0", "gst-plugin-scanner"
    )
    if os.path.isfile(scanner):
        os.environ["GST_PLUGIN_SCANNER_1_0"] = scanner
        os.environ["GST_PLUGIN_SCANNER"] = scanner

    registry = _darwin_user_registry_path()
    os.environ["GST_REGISTRY_1_0"] = registry
    os.environ["GST_REGISTRY"] = registry

    if typelib_dirs:
        typelibs = os.pathsep.join(dict.fromkeys(typelib_dirs))
        existing = os.environ.get("GI_TYPELIB_PATH", "")
        os.environ["GI_TYPELIB_PATH"] = typelibs + (os.pathsep + existing if existing else "")

    if xdg_data_dirs:
        data_dirs = os.pathsep.join(dict.fromkeys(xdg_data_dirs))
        existing = os.environ.get("XDG_DATA_DIRS", "")
        os.environ["XDG_DATA_DIRS"] = data_dirs + (os.pathsep + existing if existing else "")


def _apply_frozen_gstreamer_environment(root: str) -> None:
    if sys.platform == "darwin":
        _apply_darwin_frozen_gstreamer_environment(root)
        return

    env = os.environ.copy()
    dll_directories: list[str] = []

    for name in _frozen_gstreamer_packages():
        try:
            module = importlib.import_module(name)
        except ImportError:
            continue
        for key, value in getattr(module, "environment", {}).items():
            if key in _FROZEN_SKIP_ENV_KEYS:
                continue
            if sys.platform == "win32" and key == "LD_LIBRARY_PATH":
                continue
            if sys.platform == "win32" and key == "PATH" and isinstance(value, str):
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
    for key in _FROZEN_SKIP_ENV_KEYS:
        os.environ.pop(key, None)

    if sys.platform == "win32":
        for path in dll_directories:
            _register_windows_dll_dir(path)


if getattr(sys, "frozen", False):
    root = getattr(sys, "_MEIPASS", None)
    if root:
        _apply_frozen_gstreamer_environment(root)
else:
    if sys.platform == "darwin":
        _apply_darwin_ndi_runtime()
    import gstreamer_libs

    gstreamer_libs.setup_python_environment()
