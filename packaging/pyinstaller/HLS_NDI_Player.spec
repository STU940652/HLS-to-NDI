# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for GTK + GStreamer (official wheels) + PyGObject."""
from __future__ import annotations

import importlib
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_all

spec_dir = SPECPATH
repo_root = os.path.abspath(os.path.join(spec_dir, os.pardir, os.pardir))
rthook = os.path.join(spec_dir, "rthook_gstreamer.py")
hooks_dir = os.path.join(spec_dir, "hooks")

block_cipher = None

datas: list = []
binaries: list = []
hiddenimports: list = []


def _setup_build_gstreamer_env() -> None:
    try:
        import gstreamer_libs

        gstreamer_libs.setup_python_environment()
    except Exception:
        pass


_setup_build_gstreamer_env()

# Official GStreamer wheel packages (binaries + typelibs + plugins).
_gst_packages = [
    "gstreamer_libs",
    "gstreamer_plugins",
    "gstreamer_plugins_gpl",
    "gstreamer_plugins_gpl_restricted",
    "gstreamer_plugins_restricted",
    "gstreamer_plugins_libs",
    "gstreamer_gtk",
    "gstreamer_python",
    "gstreamer_cli",
]
if sys.platform == "win32":
    _gst_packages.append("gstreamer_msvc_runtime")


def _apply_darwin_build_dyld_paths() -> None:
    """Let PyInstaller GI hooks resolve wheel dylibs during the macOS build."""
    if sys.platform != "darwin":
        return
    lib_dirs: list[str] = []
    for pkg in _gst_packages:
        try:
            root = Path(importlib.import_module(pkg).__file__).resolve().parent
            for sub in ("lib", "lib64"):
                candidate = root / sub
                if candidate.is_dir():
                    lib_dirs.append(str(candidate))
        except Exception:
            pass
    if not lib_dirs:
        return
    for key in ("DYLD_LIBRARY_PATH", "DYLD_FALLBACK_LIBRARY_PATH"):
        existing = os.environ.get(key, "")
        merged = os.pathsep.join(lib_dirs) + (os.pathsep + existing if existing else "")
        os.environ[key] = merged


_apply_darwin_build_dyld_paths()

for _pkg in _gst_packages:
    try:
        d, b, h = collect_all(_pkg)
        datas += d
        binaries += b
        hiddenimports += h
    except Exception:
        pass


def _exclude_problematic_gstreamer_plugins(entries: list) -> list:
    """Drop plugins that break frozen macOS (embedded Python, OpenSSL mismatch)."""
    skip_markers = ("gstpython", "gstcurl")
    filtered: list = []
    for entry in entries:
        source = os.path.basename(str(entry[0])).lower()
        if any(marker in source for marker in skip_markers):
            continue
        filtered.append(entry)
    return filtered


binaries = _exclude_problematic_gstreamer_plugins(binaries)


def _typelib_workdir() -> Path:
    workpath = globals().get("WORKPATH")
    if workpath:
        return Path(workpath) / "gi_typelib_work"
    return Path(spec_dir) / "_gi_typelib_work"


def _gstreamer_plugin_dirs_for_build() -> list[str]:
    dirs: list[str] = []
    seen: set[str] = set()
    for pkg in _gst_packages + ["gstreamer_python"]:
        try:
            root = Path(importlib.import_module(pkg).__file__).resolve().parent
            plugin_dir = root / "lib" / "gstreamer-1.0"
            if plugin_dir.is_dir() and str(plugin_dir) not in seen:
                dirs.append(str(plugin_dir))
                seen.add(str(plugin_dir))
        except Exception:
            pass
    return dirs


def _build_bundled_gstreamer_registry() -> Path | None:
    """Pre-scan plugins at build time so runtime does not fork gst-plugin-scanner."""
    if sys.platform != "darwin":
        return None

    registry_path = _typelib_workdir() / "gstreamer_registry.bin"
    registry_path.parent.mkdir(parents=True, exist_ok=True)
    saved_env = {
        key: os.environ.get(key)
        for key in (
            "GST_REGISTRY_1_0",
            "GST_REGISTRY",
            "GST_REGISTRY_FORK",
            "GST_REGISTRY_UPDATE",
            "GST_PLUGIN_PATH_1_0",
            "GST_PLUGIN_SYSTEM_PATH_1_0",
            "GST_PLUGIN_SCANNER_1_0",
            "GST_PLUGIN_SCANNER",
        )
    }
    try:
        if registry_path.is_file():
            registry_path.unlink()
        os.environ["GST_REGISTRY_1_0"] = str(registry_path)
        os.environ["GST_REGISTRY"] = str(registry_path)
        os.environ["GST_REGISTRY_FORK"] = "no"
        os.environ["GST_REGISTRY_UPDATE"] = "no"

        plugin_dirs = _gstreamer_plugin_dirs_for_build()
        if plugin_dirs:
            plugins = os.pathsep.join(plugin_dirs)
            os.environ["GST_PLUGIN_PATH_1_0"] = plugins
            os.environ["GST_PLUGIN_SYSTEM_PATH_1_0"] = plugins

        try:
            libs_root = Path(importlib.import_module("gstreamer_libs").__file__).resolve().parent
            scanner = libs_root / "libexec" / "gstreamer-1.0" / "gst-plugin-scanner"
            if scanner.is_file():
                os.environ["GST_PLUGIN_SCANNER_1_0"] = str(scanner)
                os.environ["GST_PLUGIN_SCANNER"] = str(scanner)
        except Exception:
            pass

        import gi

        gi.require_version("Gst", "1.0")
        from gi.repository import Gst

        Gst.init(None)
        # Verify elements the app needs are present in the prebuilt registry.
        for factory in ("videotestsrc", "ndisink", "uridecodebin3"):
            if Gst.ElementFactory.find(factory) is None:
                print(f"Warning: GStreamer registry missing element factory: {factory}")
    except Exception as exc:
        print(f"Warning: GStreamer registry prebuild failed: {exc}")
        return None
    finally:
        for key, value in saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    return registry_path if registry_path.is_file() else None


_bundled_gstreamer_registry = _build_bundled_gstreamer_registry()
if _bundled_gstreamer_registry is not None:
    datas.append((str(_bundled_gstreamer_registry), "."))

# PyInstaller's pyi_rth_gi hook sets GI_TYPELIB_PATH to gi_typelibs only (after our
# runtime hook). Mirror wheel typelibs there so Gtk, Gst, GLib, etc. are all found.
_gi_typelib_datas: dict[str, tuple[str, str]] = {}


def _mirror_wheel_typelibs(package_name: str) -> None:
    try:
        module = importlib.import_module(package_name)
        package_root = Path(module.__file__).resolve().parent
        for sub in ("lib/girepository-1.0", "share/girepository-1.0"):
            typelib_dir = package_root / sub
            if not typelib_dir.is_dir():
                continue
            for typelib in sorted(typelib_dir.glob("*.typelib")):
                _gi_typelib_datas[typelib.name] = (str(typelib), "gi_typelibs")
    except Exception:
        pass


def _fix_gir_shared_library_paths(gir_file: Path, out_gir: Path) -> None:
    with open(gir_file, encoding="utf-8") as src:
        lines = src.readlines()
    with open(out_gir, "w", encoding="utf-8") as dst:
        for line in lines:
            if "shared-library" in line:
                split = re.split("(=)", line)
                files = re.split('(["|,])', split[2])
                for index, item in enumerate(files):
                    if "lib" in item:
                        files[index] = "@loader_path/" + os.path.basename(item)
                line = "".join(split[0:2]) + "".join(files)
            dst.write(line)


def _find_g_ir_compiler() -> str | None:
    """g-ir-compiler is a build tool (gobject-introspection), not in gstreamer wheels."""
    found = shutil.which("g-ir-compiler")
    if found:
        return found
    for candidate in (
        "/opt/homebrew/bin/g-ir-compiler",
        "/usr/local/bin/g-ir-compiler",
    ):
        if os.path.isfile(candidate):
            return candidate
    for pkg in _gst_packages:
        try:
            root = Path(importlib.import_module(pkg).__file__).resolve().parent
            for match in root.rglob("g-ir-compiler"):
                if match.is_file():
                    return str(match)
        except Exception:
            pass
    return None


def _find_gir_in_wheels(gir_name: str) -> Path | None:
    for pkg in _gst_packages:
        try:
            root = Path(importlib.import_module(pkg).__file__).resolve().parent
            direct = root / "share" / "gir-1.0" / gir_name
            if direct.is_file():
                return direct
            matches = list(root.rglob(gir_name))
            if matches:
                return matches[0]
        except Exception:
            pass
    return None


def _find_gir_on_system(gir_name: str) -> Path | None:
    """Unix-only GIR files ship with Homebrew glib, not in gstreamer wheels."""
    brew = shutil.which("brew")
    if brew:
        try:
            prefix = subprocess.run(
                [brew, "--prefix", "glib"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            if prefix:
                candidate = Path(prefix) / "share" / "gir-1.0" / gir_name
                if candidate.is_file():
                    return candidate
        except Exception:
            pass

    for prefix in (
        os.environ.get("HOMEBREW_PREFIX"),
        "/opt/homebrew",
        "/usr/local",
    ):
        if not prefix:
            continue
        candidate = Path(prefix) / "share" / "gir-1.0" / gir_name
        if candidate.is_file():
            return candidate

    for cellar in (Path("/opt/homebrew/Cellar"), Path("/usr/local/Cellar")):
        if not cellar.is_dir():
            continue
        for gir_dir in cellar.glob("glib/*/share/gir-1.0"):
            candidate = gir_dir / gir_name
            if candidate.is_file():
                return candidate
    return None


def _find_gir(gir_name: str) -> Path | None:
    return _find_gir_in_wheels(gir_name) or _find_gir_on_system(gir_name)


def _find_system_typelib(typelib_name: str) -> Path | None:
    brew = shutil.which("brew")
    if brew:
        try:
            prefix = subprocess.run(
                [brew, "--prefix", "glib"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            if prefix:
                for sub in ("lib/girepository-1.0", "share/girepository-1.0"):
                    candidate = Path(prefix) / sub / typelib_name
                    if candidate.is_file():
                        return candidate
        except Exception:
            pass

    for prefix in (
        os.environ.get("HOMEBREW_PREFIX"),
        "/opt/homebrew",
        "/usr/local",
    ):
        if not prefix:
            continue
        for sub in ("lib/girepository-1.0", "share/girepository-1.0"):
            candidate = Path(prefix) / sub / typelib_name
            if candidate.is_file():
                return candidate
    return None


def _collect_darwin_unix_typelibs() -> None:
    """GLibUnix/GioUnix typelibs are macOS-only and often ship as .gir without .typelib."""
    if sys.platform != "darwin":
        return

    unix_bases = ("GLibUnix-2.0", "GioUnix-2.0")

    for pkg in _gst_packages:
        try:
            root = Path(importlib.import_module(pkg).__file__).resolve().parent
            for base in unix_bases:
                for typelib in root.rglob(f"{base}.typelib"):
                    _gi_typelib_datas[typelib.name] = (str(typelib), "gi_typelibs")
        except Exception:
            pass

    missing = [base for base in unix_bases if f"{base}.typelib" not in _gi_typelib_datas]
    if not missing:
        return

    workroot = _typelib_workdir()
    workroot.mkdir(parents=True, exist_ok=True)
    compiler = _find_g_ir_compiler()
    if compiler is None:
        raise SystemExit(
            "g-ir-compiler not found on PATH. "
            "Install gobject-introspection (e.g. brew install gobject-introspection) "
            "to compile GLibUnix/GioUnix typelibs from Homebrew glib .gir files."
        )

    for base in missing:
        gir_name = f"{base}.gir"
        typelib_name = f"{base}.typelib"
        gir_path = _find_gir(gir_name)
        if gir_path is None:
            system_typelib = _find_system_typelib(typelib_name)
            if system_typelib is not None:
                _gi_typelib_datas[typelib_name] = (str(system_typelib), "gi_typelibs")
                continue
            print(
                f"Warning: could not find {gir_name} in gstreamer wheels "
                "or Homebrew glib (brew install gobject-introspection)"
            )
            continue

        try:
            fixed_gir = workroot / gir_name
            _fix_gir_shared_library_paths(gir_path, fixed_gir)
            out_typelib = workroot / typelib_name
            includedir = gir_path.parent
            subprocess.run(
                [
                    compiler,
                    f"--includedir={includedir}",
                    str(fixed_gir),
                    "-o",
                    str(out_typelib),
                ],
                check=True,
            )
            _gi_typelib_datas[typelib_name] = (str(out_typelib), "gi_typelibs")
        except Exception as exc:
            print(f"Warning: g-ir-compiler for {gir_name}: {exc}")
            system_typelib = _find_system_typelib(typelib_name)
            if system_typelib is not None:
                _gi_typelib_datas[typelib_name] = (str(system_typelib), "gi_typelibs")

    still_missing = [base for base in unix_bases if f"{base}.typelib" not in _gi_typelib_datas]
    if still_missing:
        raise SystemExit(
            "Required macOS typelibs not collected for gi_typelibs: "
            + ", ".join(f"{name}.typelib" for name in still_missing)
        )


for _pkg in _gst_packages:
    _mirror_wheel_typelibs(_pkg)

_collect_darwin_unix_typelibs()

datas += list(_gi_typelib_datas.values())

hiddenimports = list(
    dict.fromkeys(
        hiddenimports
        + [
            # Imported dynamically inside gstreamer_libs.gstreamer_env().
            "gstreamer_gtk",
            "gstreamer_libs",
            "gstreamer_python",
            "gstreamer_plugins",
            "gstreamer_plugins_gpl",
            "gstreamer_plugins_gpl_restricted",
            "gstreamer_plugins_restricted",
            "gstreamer_plugins_libs",
            "gstreamer_cli",
            "gstreamer_ext_runtime",
            "gi.repository.GObject",
            "gi.repository.GLib",
            "gi.repository.GLibUnix",
            "gi.repository.GioUnix",
            "gi.repository.Gst",
            "gi.repository.Gtk",
            "gi.repository.Gdk",
            "gi.repository.Gio",
            "gi.repository.cairo",
            "cairo",
            "cairo._cairo",
        ]
    )
)

a = Analysis(
    [os.path.join(repo_root, "hls_ndi_player.py")],
    pathex=[repo_root],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[hooks_dir],
    hooksconfig={
        "gi": {
            "module-versions": {
                "Gtk": "4.0",
                "Gdk": "4.0",
            },
        },
    },
    runtime_hooks=[rthook],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="HLS_NDI_Player",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="HLS_NDI_Player",
)

if sys.platform == "darwin":
    app = BUNDLE(
        coll,
        name="HLS NDI Player.app",
        icon=None,
        bundle_identifier="org.freedesktop.gstreamer.HLSNDIPlayer",
        info_plist={
            "CFBundleName": "HLS NDI Player",
            "CFBundleDisplayName": "HLS NDI Player",
            "CFBundleIdentifier": "org.freedesktop.gstreamer.HLSNDIPlayer",
            "NSHighResolutionCapable": True,
        },
    )
