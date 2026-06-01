from __future__ import annotations

import json
from pathlib import Path


_SETTINGS_DIR = Path(__file__).resolve().parent.parent / "data"
_SETTINGS_FILE = _SETTINGS_DIR / "settings.json"

_DEFAULT_SETTINGS = {
    "transcode_to_720p": True,
    "default_volume": 1.0,
}


def _ensure_dir() -> None:
    _SETTINGS_DIR.mkdir(parents=True, exist_ok=True)


def load_settings() -> dict:
    _ensure_dir()
    if not _SETTINGS_FILE.exists():
        return dict(_DEFAULT_SETTINGS)
    try:
        data = json.loads(_SETTINGS_FILE.read_text("utf-8"))
        if not isinstance(data, dict):
            return dict(_DEFAULT_SETTINGS)
        return data
    except (json.JSONDecodeError, OSError):
        return dict(_DEFAULT_SETTINGS)


def save_settings(settings: dict) -> None:
    _ensure_dir()
    merged = dict(_DEFAULT_SETTINGS)
    if isinstance(settings, dict):
        merged.update(settings)
    _SETTINGS_FILE.write_text(json.dumps(merged, indent=2, ensure_ascii=False), "utf-8")


def get_setting(key: str, default=None):
    return load_settings().get(key, default)
