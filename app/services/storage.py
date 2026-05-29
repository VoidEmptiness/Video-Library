from __future__ import annotations

import os
import re
import secrets
from pathlib import Path


VIDEO_DIR = Path(os.getenv("VIDEO_DIR", "/data/videos"))
THUMB_DIR = Path(os.getenv("THUMB_DIR", "/data/thumbs"))


def ensure_dirs() -> None:
    VIDEO_DIR.mkdir(parents=True, exist_ok=True)
    THUMB_DIR.mkdir(parents=True, exist_ok=True)


_SAFE_RE = re.compile(r"[^a-zA-Z0-9._-]+")


def safe_filename(name: str) -> str:
    name = name.strip().replace(" ", "_")
    name = _SAFE_RE.sub("_", name)
    name = name.strip("._-")
    return name or "video"


def unique_storage_name(original_name: str, ext_override: str | None = None) -> str:
    base = safe_filename(Path(original_name).stem)
    ext = ext_override or Path(original_name).suffix.lower()
    if ext and not ext.startswith("."):
        ext = f".{ext}"
    token = secrets.token_hex(8)
    return f"{base}-{token}{ext}"


def video_path(filename: str) -> Path:
    return VIDEO_DIR / filename


def thumb_path(filename_no_ext: str) -> Path:
    return THUMB_DIR / f"{filename_no_ext}.jpg"

