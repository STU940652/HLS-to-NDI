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

for _pkg in ("gi", "PyGObject"):
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


def _collect_typelib_via_gi(namespace: str, version: str) -> None:
    """Resolve a typelib path through gi (needs gstreamer env) and mirror to gi_typelibs."""
    try:
        import gi

        try:
            gi.require_version("GIRepository", "3.0")
            from gi.repository import GIRepository

            repo = GIRepository.Repository()
            repo.require(namespace, version, GIRepository.RepositoryLoadFlags.LAZY)
        except ValueError:
            gi.require_version("GIRepository", "2.0")
            from gi.repository import GIRepository

            repo = GIRepository.Repository.get_default()
            repo.require(
                namespace,
                version,
                GIRepository.RepositoryLoadFlags.IREPOSITORY_LOAD_FLAG_LAZY,
            )

        typelib = repo.get_typelib_path(namespace)
        if typelib and os.path.isfile(typelib):
            _gi_typelib_datas[Path(typelib).name] = (typelib, "gi_typelibs")
    except Exception as exc:
        print(f"Warning: could not collect typelib {namespace}: {exc}")


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

    for namespace, version in (("GLibUnix", "2.0"), ("GioUnix", "2.0")):
        typelib_name = f"{namespace}-{version}.typelib"
        if typelib_name in _gi_typelib_datas:
            continue
        _collect_typelib_via_gi(namespace, version)

    missing = [base for base in unix_bases if f"{base}.typelib" not in _gi_typelib_datas]
    if not missing:
        return

    from PyInstaller.config import CONF
    from PyInstaller.utils.hooks.gi import gir_library_path_fix

    workroot = Path(BUILDPATH) / "gi_typelib_work"
    workroot.mkdir(parents=True, exist_ok=True)
    CONF["workpath"] = str(workroot)

    compiler = shutil.which("g-ir-compiler")
    for base in missing:
        gir_name = f"{base}.gir"
        typelib_name = f"{base}.typelib"
        if typelib_name in _gi_typelib_datas:
            continue

        gir_path = None
        package_root = None
        for pkg in _gst_packages:
            try:
                root = Path(importlib.import_module(pkg).__file__).resolve().parent
                matches = list(root.rglob(gir_name))
                if matches:
                    gir_path = matches[0]
                    package_root = gir_path.parent.parent.parent
                    break
            except Exception:
                pass

        if gir_path is None or package_root is None:
            print(f"Warning: could not find {gir_name} in gstreamer wheels")
            continue

        entry = None
        fake_typelib = package_root / "lib" / "girepository-1.0" / typelib_name
        try:
            entry = gir_library_path_fix(str(fake_typelib))
        except Exception as exc:
            print(f"Warning: gir_library_path_fix for {gir_name}: {exc}")

        if entry is None and compiler is not None:
            try:
                fixed_gir = workroot / gir_name
                _fix_gir_shared_library_paths(gir_path, fixed_gir)
                out_typelib = workroot / typelib_name
                subprocess.run(
                    [compiler, str(fixed_gir), "-o", str(out_typelib)],
                    check=True,
                )
                entry = (str(out_typelib), "gi_typelibs")
            except Exception as exc:
                print(f"Warning: g-ir-compiler for {gir_name}: {exc}")

        if entry:
            _gi_typelib_datas[Path(entry[0]).name] = entry

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
    hooksconfig={},
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
