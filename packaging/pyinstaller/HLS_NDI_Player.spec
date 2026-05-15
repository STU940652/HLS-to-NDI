# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for GTK + GStreamer (official wheels) + PyGObject."""
from __future__ import annotations

import os
import sys

from PyInstaller.utils.hooks import collect_all

spec_dir = SPECPATH
repo_root = os.path.abspath(os.path.join(spec_dir, os.pardir, os.pardir))
rthook = os.path.join(spec_dir, "rthook_gstreamer.py")

block_cipher = None

datas: list = []
binaries: list = []
hiddenimports: list = []

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

hiddenimports = list(
    dict.fromkeys(
        hiddenimports
        + [
            "gi.repository.GObject",
            "gi.repository.GLib",
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
