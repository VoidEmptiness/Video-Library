from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

try:
    import fcntl
    HAS_FCNTL = True
except ImportError:
    HAS_FCNTL = False


_SETTINGS_DIR = Path(os.getenv("SETTINGS_DIR", str(Path(__file__).resolve().parent.parent / "data")))
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
    data = json.dumps(merged, indent=2, ensure_ascii=False)
    tmp = tempfile.NamedTemporaryFile(dir=str(_SETTINGS_DIR), suffix=".tmp", delete=False, mode="w", encoding="utf-8")
    try:
        if HAS_FCNTL:
            fcntl.flock(tmp.fileno(), fcntl.LOCK_EX)
        tmp.write(data)
        tmp.flush()
        os.fsync(tmp.fileno())
        if HAS_FCNTL:
            fcntl.flock(tmp.fileno(), fcntl.LOCK_UN)
        tmp.close()
        os.replace(tmp.name, str(_SETTINGS_FILE))
    except Exception:
        try:
            os.unlink(tmp.name)
        except Exception:
            pass
        tmp.close()
        raise


def get_setting(key: str, default=None):
    return load_settings().get(key, default)
