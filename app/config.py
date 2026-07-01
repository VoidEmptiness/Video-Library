from __future__ import annotations

import os
import time
from pathlib import Path

from fastapi.templating import Jinja2Templates


APP_TITLE = os.getenv("APP_TITLE", "Video Library")
MAX_FILES_PER_UPLOAD = int(os.getenv("MAX_FILES_PER_UPLOAD", "50"))
THUMBNAIL_CONCURRENCY = int(os.getenv("THUMBNAIL_CONCURRENCY", "4"))
PAGE_SIZE = int(os.getenv("PAGE_SIZE", "32"))

DATA_DIR = Path(os.getenv("DATA_DIR", str(Path(__file__).parent.parent / "_data")))

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


def _format_mtime(timestamp: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(timestamp))


templates.env.filters["format_mtime"] = _format_mtime
templates.env.globals["static_version"] = str(int(time.time()))


def short_name(value: str | None, limit: int = 36) -> str:
    value = value or ""
    if len(value) <= limit:
        return value
    return value[:limit].rstrip() + "..."

templates.env.filters["short_name"] = short_name
