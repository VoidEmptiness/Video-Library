from __future__ import annotations

import json
import logging
import os
import subprocess
from functools import lru_cache
from pathlib import Path

from fastapi import HTTPException, Request, Response
from fastapi.responses import FileResponse, RedirectResponse, StreamingResponse
from pathlib import PurePath
from sqlalchemy import func, select
from sqlalchemy.orm import Session, joinedload

from ..models import Tag, Video, VideoTag
from .storage import THUMB_DIR, video_path
from .transcoding import FFPROBE_EXE, ffprobe_available

logger = logging.getLogger(__name__)


def _format_duration(seconds: float | None) -> str:
    if seconds is None or seconds < 0:
        return "\u2014"
    total = int(seconds)
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    if hours > 0:
        return f"{hours:02d}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def _parse_range_header(range_header: str, file_size: int) -> tuple[int, int] | None:
    if not range_header.startswith("bytes="):
        return None
    value = range_header.replace("bytes=", "", 1).strip()
    if "," in value or "-" not in value:
        return None
    start_raw, end_raw = value.split("-", 1)
    try:
        if start_raw == "":
            length = int(end_raw)
            if length <= 0:
                return None
            start = max(file_size - length, 0)
            end = file_size - 1
        else:
            start = int(start_raw)
            end = int(end_raw) if end_raw else file_size - 1
    except ValueError:
        return None
    if start < 0 or end < start:
        return None
    if start >= file_size:
        return None
    end = min(end, file_size - 1)
    return start, end


TEXT_EXTENSIONS = {
    ".txt", ".json", ".md", ".csv", ".xml", ".yaml", ".yml", ".toml", ".ini",
    ".cfg", ".log", ".html", ".css", ".js", ".py", ".sh", ".env", ".conf",
    ".sql", ".rb", ".php", ".pl", ".lua", ".bat", ".ps1",
}


def _is_text_file(name: str) -> bool:
    ext = PurePath(name).suffix.lower()
    return ext in TEXT_EXTENSIONS


def _delete_video_files(path: Path, thumb: Path | None) -> None:
    try:
        if path.exists():
            path.unlink()
    except Exception as e:
        logger.warning("Failed to delete file %s: %s", path, e)
    try:
        if thumb and thumb.exists():
            thumb.unlink()
    except Exception as e:
        logger.warning("Failed to delete thumbnail %s: %s", thumb, e)


@lru_cache(maxsize=256)
def _probe_video_metadata(path: Path) -> tuple[str | None, float | None, int | None, int | None]:
    if not ffprobe_available():
        logger.warning("ffprobe not available, skipping metadata probe for %s", path)
        return None, None, None, None
    try:
        completed = subprocess.run(
            [
                str(FFPROBE_EXE),
                "-v",
                "error",
                "-show_entries",
                "stream=codec_name,width,height:format=duration",
                "-select_streams",
                "v:0",
                "-of",
                "json",
                str(path),
            ],
            capture_output=True,
            text=True,
            timeout=8,
            check=False,
        )
    except Exception as e:
        logger.warning("Failed to probe metadata for %s: %s", path, e)
        return None, None, None, None
    if completed.returncode != 0:
        return None, None, None, None
    try:
        payload = json.loads(completed.stdout or "{}")
    except json.JSONDecodeError:
        return None, None, None, None
    codec = None
    width = None
    height = None
    streams = payload.get("streams") or []
    if streams and isinstance(streams, list):
        codec = streams[0].get("codec_name") or None
        width = streams[0].get("width") or None
        height = streams[0].get("height") or None
    duration = None
    fmt = payload.get("format") or {}
    raw_duration = fmt.get("duration")
    if raw_duration is not None:
        try:
            duration = float(raw_duration)
        except (TypeError, ValueError):
            duration = None
    return codec, duration, width, height


def _redirect(url: str) -> RedirectResponse:
    return RedirectResponse(url=url, status_code=303)


def _safe_return_to(return_to: str | None) -> str:
    if return_to and return_to.startswith("/") and not return_to.startswith("//"):
        return return_to
    return "/"


def _all_tags(db: Session) -> list[Tag]:
    return list(db.scalars(select(Tag).order_by(Tag.name)))


def _tag_ids_from_csv(csv: str | None) -> set[int]:
    if not csv:
        return set()
    out: set[int] = set()
    for part in csv.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            out.add(int(part))
        except ValueError:
            continue
    return out


def _video_query(
    db: Session,
    tag_ids: set[int] | None = None,
    q: str | None = None,
    untagged: bool = False,
):
    stmt = select(Video).order_by(Video.created_at.desc())
    if untagged:
        stmt = stmt.where(~Video.tags.any()).options(joinedload(Video.tags))
    elif tag_ids:
        subq = (
            select(VideoTag.video_id)
            .where(VideoTag.tag_id.in_(tag_ids))
            .group_by(VideoTag.video_id)
            .subquery()
        )
        stmt = stmt.where(Video.id.in_(subq)).options(joinedload(Video.tags))
    else:
        stmt = stmt.options(joinedload(Video.tags))
    if q:
        q = q.strip()
        if q:
            stmt = stmt.where(Video.original_name.ilike(f"%{q}%"))
    return stmt


def _video_count(
    db: Session,
    tag_ids: set[int] | None = None,
    q: str | None = None,
    untagged: bool = False,
) -> int:
    stmt = select(func.count(Video.id))
    if untagged:
        stmt = stmt.where(~Video.tags.any())
    elif tag_ids:
        subq = (
            select(VideoTag.video_id)
            .where(VideoTag.tag_id.in_(tag_ids))
            .group_by(VideoTag.video_id)
            .subquery()
        )
        stmt = stmt.where(Video.id.in_(subq))
    if q:
        q = q.strip()
        if q:
            stmt = stmt.where(Video.original_name.ilike(f"%{q}%"))
    result = db.execute(stmt).scalar()
    return result or 0


def _stream_file(
    path: Path, media_type: str, request: Request
) -> FileResponse | StreamingResponse:
    if not path.exists():
        raise HTTPException(status_code=404, detail="File missing on disk")
    file_size = path.stat().st_size
    range_header = request.headers.get("range")
    if not range_header:
        return FileResponse(path=str(path), media_type=media_type)
    parsed = _parse_range_header(range_header, file_size)
    if not parsed:
        return Response(
            status_code=416, headers={"Content-Range": f"bytes */{file_size}"}
        )
    start, end = parsed
    chunk_size = (end - start) + 1

    def iter_file():
        with path.open("rb") as fh:
            fh.seek(start)
            remaining = chunk_size
            while remaining > 0:
                data = fh.read(min(1024 * 1024, remaining))
                if not data:
                    break
                remaining -= len(data)
                yield data

    headers = {
        "Accept-Ranges": "bytes",
        "Content-Range": f"bytes {start}-{end}/{file_size}",
        "Content-Length": str(chunk_size),
    }
    return StreamingResponse(
        iter_file(), status_code=206, media_type=media_type, headers=headers
    )
