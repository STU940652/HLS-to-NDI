"""Persistent application settings (dev and PyInstaller frozen builds)."""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import gi

gi.require_version("GLib", "2.0")
from gi.repository import GLib  # noqa: E402

logger = logging.getLogger(__name__)

APP_CONFIG_DIR_NAME = "com.example.gtk_ndi_player"
SETTINGS_FILENAME = "settings.json"

DEFAULT_NDI_NAME = "GTK_NDI_Player"


@dataclass
class AppSettings:
    ndi_name: str = DEFAULT_NDI_NAME
    s3_directory_uri: str = ""


def settings_path() -> Path:
    """User-writable config path (XDG on Linux, AppData on Windows)."""
    return Path(GLib.get_user_config_dir()) / APP_CONFIG_DIR_NAME / SETTINGS_FILENAME


def load_settings() -> AppSettings:
    path = settings_path()
    if not path.is_file():
        return AppSettings()
    try:
        raw: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Could not read settings from %s: %s", path, exc)
        return AppSettings()

    ndi_name = raw.get("ndi_name", DEFAULT_NDI_NAME)
    s3_directory_uri = raw.get("s3_directory_uri", "")
    if not isinstance(ndi_name, str):
        ndi_name = DEFAULT_NDI_NAME
    if not isinstance(s3_directory_uri, str):
        s3_directory_uri = ""
    return AppSettings(
        ndi_name=ndi_name.strip() or DEFAULT_NDI_NAME,
        s3_directory_uri=s3_directory_uri.strip(),
    )


def save_settings(settings: AppSettings) -> None:
    path = settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = asdict(settings)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)
