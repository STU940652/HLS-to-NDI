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

for _pkg in _gst_packages:
    try:
        d, b, h = collect_all(_pkg)
        datas += d
        binaries += b
        hiddenimports += h
    except Exception:
        pass

# PyInstaller's pyi_rth_gi hook sets GI_TYPELIB_PATH to gi_typelibs only (after our
# runtime hook). Mirror wheel typelibs there so Gtk, Gst, GLib, etc. are all found.
_gi_typelib_datas: dict[str, tuple[str, str]] = {}


def _typelib_workdir() -> Path:
    workpath = globals().get("WORKPATH")
    if workpath:
        return Path(workpath) / "gi_typelib_work"
    return Path(spec_dir) / "_gi_typelib_work"


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
    compiler = shutil.which("g-ir-compiler")
    if compiler is None:
        raise SystemExit("g-ir-compiler not found on PATH (expected from gstreamer_cli)")

    for base in missing:
        gir_name = f"{base}.gir"
        typelib_name = f"{base}.typelib"
        gir_path = _find_gir_in_wheels(gir_name)
        if gir_path is None:
            print(f"Warning: could not find {gir_name} in gstreamer wheels")
            continue

        try:
            fixed_gir = workroot / gir_name
            _fix_gir_shared_library_paths(gir_path, fixed_gir)
            out_typelib = workroot / typelib_name
            subprocess.run(
                [compiler, str(fixed_gir), "-o", str(out_typelib)],
                check=True,
            )
            _gi_typelib_datas[typelib_name] = (str(out_typelib), "gi_typelibs")
        except Exception as exc:
            print(f"Warning: g-ir-compiler for {gir_name}: {exc}")

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
            "gi.repository.GdkPixbuf",
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
    hookspath=[],
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
