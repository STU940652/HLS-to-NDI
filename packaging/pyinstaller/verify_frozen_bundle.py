"""Post-build checks for frozen macOS/Windows bundles (run in CI)."""
from __future__ import annotations

import os
import runpy
import sys
from pathlib import Path

_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

from darwin_lib_dedup import find_duplicate_real_dylibs


def _frameworks_dir(app_path: Path) -> Path:
    if sys.platform == "darwin":
        return app_path / "Contents" / "Frameworks"
    internal = app_path / "_internal"
    if internal.is_dir():
        return internal
    return app_path


def _executable_path(app_path: Path) -> Path:
    if sys.platform == "darwin":
        return app_path / "Contents" / "MacOS" / "HLS_NDI_Player"
    return app_path / "HLS_NDI_Player.exe"


def _apply_frozen_rthook(frameworks: Path) -> None:
    sys.frozen = True
    sys._MEIPASS = str(frameworks)
    rthook = Path(__file__).resolve().parent / "rthook_gstreamer.py"
    runpy.run_path(str(rthook), run_name="__rthook__")


def _verify_python_modules(executable: Path) -> None:
    required_snippets = (
        b"app.s3_listing",
        b"app.settings",
    )
    try:
        blob = executable.read_bytes()
    except OSError as exc:
        raise SystemExit(f"Could not read executable {executable}: {exc}") from exc
    missing = [s.decode() for s in required_snippets if s not in blob]
    if missing:
        raise SystemExit(
            "Frozen bundle missing Python modules: "
            + ", ".join(missing)
            + ". Ensure they are committed and rebuild."
        )


def _verify_darwin_tls_environment() -> None:
    cert = os.environ.get("SSL_CERT_FILE", "")
    if not cert or not os.path.isfile(cert):
        raise SystemExit(
            "Frozen bundle missing TLS CA configuration (SSL_CERT_FILE). "
            "Ensure gstreamer_libs/etc/ssl/certs/ca-certificates.crt is bundled."
        )
    gio_modules = os.environ.get("GIO_EXTRA_MODULES", "")
    module_dirs = [p for p in gio_modules.split(os.pathsep) if p and os.path.isdir(p)]
    if not module_dirs:
        raise SystemExit(
            "Frozen bundle missing GIO TLS modules (GIO_EXTRA_MODULES). "
            "Ensure gstreamer_plugins_libs/lib/gio/modules is bundled."
        )


def _verify_hls_elements() -> None:
    import gi

    gi.require_version("Gst", "1.0")
    from gi.repository import Gst

    Gst.init(None)
    registry = Gst.Registry.get()
    for path_key in ("GST_PLUGIN_PATH_1_0", "GST_PLUGIN_PATH"):
        for plugin_dir in os.environ.get(path_key, "").split(os.pathsep):
            if plugin_dir and os.path.isdir(plugin_dir):
                registry.scan_path(plugin_dir)

    required = ("hlsdemux2", "souphttpsrc")
    missing = [name for name in required if Gst.ElementFactory.find(name) is None]
    if missing:
        raise SystemExit(
            "Frozen bundle GStreamer registry missing required HLS elements: "
            + ", ".join(missing)
        )


def main() -> int:
    if len(sys.argv) != 2:
        print(f"Usage: {sys.argv[0]} <path-to-.app-or-_internal-dir>", file=sys.stderr)
        return 2

    app_path = Path(sys.argv[1]).resolve()
    if not app_path.exists():
        raise SystemExit(f"Bundle path not found: {app_path}")

    frameworks = _frameworks_dir(app_path)
    executable = _executable_path(app_path)
    if not frameworks.is_dir():
        raise SystemExit(f"Missing bundle frameworks dir: {frameworks}")
    if not executable.is_file():
        raise SystemExit(f"Missing bundle executable: {executable}")

    if sys.platform == "darwin":
        duplicates = find_duplicate_real_dylibs(app_path)
        if duplicates:
            raise SystemExit(
                "Duplicate real dylibs in bundle (causes macOS ObjC class conflicts):\n"
                + "\n".join(duplicates)
                + "\nRun packaging/pyinstaller/darwin_lib_dedup.py on the .app first."
            )

    _verify_python_modules(executable)
    _apply_frozen_rthook(frameworks)
    if sys.platform == "darwin":
        _verify_darwin_tls_environment()
    _verify_hls_elements()
    print("Frozen bundle verification passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
