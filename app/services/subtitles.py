from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import threading
from functools import lru_cache
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from .storage import subtitle_dir, video_path
from .transcoding import FFMPEG_EXE, FFPROBE_EXE, ffprobe_available

logger = logging.getLogger(__name__)

_TIME_RE = re.compile(
    r"(\d{1,2}:\d{2}:\d{2})[,.](\d{1,3})\s*-->\s*(\d{1,2}:\d{2}:\d{2})[,.](\d{1,3})"
)


def _srt_to_vtt(text: str) -> str:
    lines_out = ["WEBVTT"]
    for line in text.replace("\r", "").split("\n"):
        stripped = line.strip()
        if not stripped:
            lines_out.append("")
            continue
        m = _TIME_RE.search(line)
        if m:
            line = f"{m.group(1)}.{m.group(2)} --> {m.group(3)}.{m.group(4)}"
        lines_out.append(line)
    return "\n".join(lines_out).rstrip() + "\n"


def _decode_subtitle(raw: bytes) -> str:
    for enc in ("utf-8-sig", "utf-16", "cp1251", "latin-1"):
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", errors="replace")


def _read_subtitle_text(path: Path) -> str:
    text = _decode_subtitle(path.read_bytes())
    if path.suffix.lower() == ".srt":
        text = _srt_to_vtt(text)
    elif not text.lstrip().startswith("WEBVTT"):
        text = "WEBVTT\n" + text
    return text


LANGUAGE_NAMES = {
    "ru": "Русский",
    "en": "English",
    "de": "Deutsch",
    "fr": "Français",
    "es": "Español",
    "it": "Italiano",
    "pt": "Português",
    "pl": "Polski",
    "uk": "Українська",
    "be": "Беларуская",
    "kk": "Қазақша",
    "ja": "日本語",
    "ko": "한국어",
    "zh": "中文",
    "ar": "العربية",
}


@lru_cache(maxsize=512)
def _probe_embedded_streams_cached(
    path_str: str, size: int, mtime_ns: int
) -> tuple[tuple[int, str | None, str | None, str | None], ...]:
    if not ffprobe_available():
        return ()
    try:
        completed = subprocess.run(
            [
                str(FFPROBE_EXE),
                "-v",
                "error",
                "-select_streams",
                "s",
                "-show_entries",
                "stream=codec_name:stream_tags=language,title",
                "-of",
                "json",
                path_str,
            ],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except Exception:
        logger.warning("Failed to probe embedded subtitles for %s", path_str)
        return ()
    if completed.returncode != 0:
        return ()
    try:
        payload = json.loads(completed.stdout or "{}")
    except json.JSONDecodeError:
        return ()
    streams = payload.get("streams") or []
    result: list[tuple[int, str | None, str | None]] = []
    for idx, s in enumerate(streams):
        if not isinstance(s, dict):
            continue
        tags = s.get("tags") or {}
        result.append(
            (
                idx,
                s.get("codec_name"),
                str(tags.get("language") or None),
                str(tags.get("title") or None),
            )
        )
    return tuple(result)


def _embedded_streams(source: Path) -> list[dict]:
    if not source.exists():
        return []
    try:
        size = source.stat().st_size
        mtime_ns = source.stat().st_mtime_ns
    except OSError:
        return []
    return [
        {"index": idx, "codec_name": codec, "language": lang, "title": title}
        for idx, codec, lang, title in _probe_embedded_streams_cached(
            str(source), size, mtime_ns
        )
    ]


def _embedded_label(stream: dict) -> str:
    if stream.get("title"):
        return stream["title"][:128]
    lang = (stream.get("language") or "").lower()
    if lang in LANGUAGE_NAMES:
        return LANGUAGE_NAMES[lang]
    if lang and lang != "und":
        return lang.upper()
    return f"Дорожка {stream['index'] + 1}"


def _embedded_vtt_path(video_id: int, stream_index: int) -> Path:
    return subtitle_dir(video_id) / f"embedded_{stream_index}.vtt"


_extraction_locks: dict[tuple[int, int], threading.Lock] = {}
_extraction_locks_guard = threading.Lock()


def _extraction_lock(video_id: int, stream_index: int) -> threading.Lock:
    with _extraction_locks_guard:
        key = (video_id, stream_index)
        lock = _extraction_locks.get(key)
        if not lock:
            lock = threading.Lock()
            _extraction_locks[key] = lock
        return lock


def extract_embedded_subtitle(
    source: Path, video_id: int, stream_index: int
) -> Path | None:
    dest = _embedded_vtt_path(video_id, stream_index)
    if dest.exists() and dest.stat().st_size > 0:
        return dest
    with _extraction_lock(video_id, stream_index):
        if dest.exists() and dest.stat().st_size > 0:
            return dest
        tmp = dest.with_suffix(".tmp.vtt")
        cmd = [
            str(FFMPEG_EXE),
            "-y",
            "-v",
            "error",
            "-i",
            str(source),
            "-map",
            f"0:s:{stream_index}",
            "-c:s",
            "webvtt",
            str(tmp),
        ]
        logger.info(
            "Extracting embedded subtitle stream %d from %s",
            stream_index,
            source.name,
        )
        try:
            completed = subprocess.run(
                cmd,
                capture_output=True,
                timeout=600,
                check=False,
            )
        except Exception:
            logger.exception("Failed to extract embedded subtitles from %s", source)
            tmp.unlink(missing_ok=True)
            return None
        if (
            completed.returncode == 0
            and tmp.exists()
            and tmp.stat().st_size > 0
        ):
            dest.parent.mkdir(parents=True, exist_ok=True)
            os.replace(tmp, dest)
            return dest
        tmp.unlink(missing_ok=True)
        logger.warning(
            "Embedded subtitle stream %d of %s is not extractable to WebVTT",
            stream_index,
            source.name,
        )
        return None


def extract_all_embedded_subtitles(source: Path, video_id: int) -> None:
    if not source.exists():
        return
    for stream in _embedded_streams(source):
        extract_embedded_subtitle(source, video_id, stream["index"])


def _subtitle_list(db: Session, video) -> list[dict]:
    from ..models import Subtitle

    uploaded = sorted(
        list(db.scalars(select(Subtitle).where(Subtitle.video_id == video.id))),
        key=lambda s: (not s.is_default, s.created_at),
    )
    payload = [
        {
            "kind": "uploaded",
            "key": f"u{s.id}",
            "id": s.id,
            "label": s.label or s.original_name,
            "original_name": s.original_name,
            "is_default": s.is_default,
            "url": f"/subtitles/{s.id}",
        }
        for s in uploaded
    ]
    source = video_path(video.filename)
    if source.exists():
        for stream in _embedded_streams(source):
            payload.append(
                {
                    "kind": "embedded",
                    "key": f"e{stream['index']}",
                    "id": None,
                    "label": _embedded_label(stream),
                    "original_name": None,
                    "is_default": False,
                    "url": f"/subtitles/embedded/{video.id}/{stream['index']}",
                    "stream_index": stream["index"],
                }
            )
    return payload