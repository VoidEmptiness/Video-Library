from __future__ import annotations

import logging
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import Subtitle, Video
from ..services.storage import ensure_dirs, subtitle_path, unique_storage_name, video_path
from ..services.subtitles import (
    _embedded_streams,
    _read_subtitle_text,
    _subtitle_list,
    extract_embedded_subtitle,
)
from .auth import AdminUserHTML, User

logger = logging.getLogger(__name__)

router = APIRouter()

ACCEPTED_SUBTITLE_EXTS = {".srt", ".vtt"}


def _redirect(url: str) -> RedirectResponse:
    return RedirectResponse(url=url, status_code=303)


def _get_video_or_404(db: Session, video_id: int) -> Video:
    video = db.get(Video, video_id)
    if not video:
        raise HTTPException(status_code=404, detail="Video not found")
    return video


def _get_subtitle_or_404(db: Session, subtitle_id: int) -> Subtitle:
    subtitle = db.get(Subtitle, subtitle_id)
    if not subtitle:
        raise HTTPException(status_code=404, detail="Subtitles not found")
    return subtitle


def _subtitle_label(label: str | None, fallback: str) -> str:
    return (label or "").strip()[:128] or fallback


@router.post("/videos/{video_id}/subtitles")
async def upload_subtitles(
    video_id: int,
    db: Annotated[Session, Depends(get_db)],
    _: AdminUserHTML,
    files: Annotated[list[UploadFile], File()],
    labels: Annotated[list[str] | None, Form()] = None,
):
    video = _get_video_or_404(db, video_id)
    if not files:
        raise HTTPException(status_code=400, detail="No files")

    existing = list(video.subtitles)
    ensure_dirs()
    uploaded: list[Subtitle] = []

    for i, file in enumerate(files):
        if not file.filename:
            raise HTTPException(status_code=400, detail="No filename")
        ext = Path(file.filename).suffix.lower()
        if ext not in ACCEPTED_SUBTITLE_EXTS:
            raise HTTPException(
                status_code=400,
                detail=f"Неподдерживаемый формат {ext or '(без расширения)'}. Допустимы .srt и .vtt",
            )

        label = labels[i] if labels and i < len(labels) else None
        storage_name = unique_storage_name(file.filename)
        dest = subtitle_path(video_id, storage_name)
        dest.parent.mkdir(parents=True, exist_ok=True)

        try:
            with dest.open("wb") as f:
                while chunk := await file.read(1024 * 1024):
                    f.write(chunk)
        except OSError:
            if dest.exists():
                dest.unlink()
            logger.exception("Disk error while saving subtitles for %s", file.filename)
            raise HTTPException(status_code=500, detail="Ошибка записи файла на диск")

        subtitle = Subtitle(
            video_id=video_id,
            filename=storage_name,
            original_name=file.filename,
            label=_subtitle_label(label, Path(file.filename).stem),
            is_default=not existing and not uploaded,
        )
        db.add(subtitle)
        uploaded.append(subtitle)

    logger.info(
        "Uploaded %d subtitle(s) for video %d: %s",
        len(uploaded),
        video_id,
        [f.filename for f in files if f.filename],
    )
    db.commit()
    return _redirect(f"/videos/{video_id}")


@router.get("/videos/{video_id}/subtitles")
def list_subtitles(
    video_id: int,
    db: Annotated[Session, Depends(get_db)],
    _: User,
):
    video = _get_video_or_404(db, video_id)
    return _subtitle_list(db, video)


@router.get("/subtitles/embedded/{video_id}/{stream_index}")
def embedded_subtitle_content(
    video_id: int,
    stream_index: int,
    db: Annotated[Session, Depends(get_db)],
    _: User,
):
    video = _get_video_or_404(db, video_id)
    source = video_path(video.filename)
    if not source.exists():
        raise HTTPException(status_code=404, detail="Source missing")
    if not any(s["index"] == stream_index for s in _embedded_streams(source)):
        raise HTTPException(status_code=404, detail="Subtitle track not found")
    dest = extract_embedded_subtitle(source, video_id, stream_index)
    if not dest:
        raise HTTPException(
            status_code=404,
            detail="Subtitle track is not extractable to WebVTT",
        )
    return Response(
        _read_subtitle_text(dest),
        media_type="text/vtt; charset=utf-8",
    )


@router.get("/subtitles/{subtitle_id}")
def subtitle_content(
    subtitle_id: int,
    db: Annotated[Session, Depends(get_db)],
    _: User,
):
    subtitle = _get_subtitle_or_404(db, subtitle_id)
    path = subtitle_path(subtitle.video_id, subtitle.filename)
    if not path.exists():
        raise HTTPException(status_code=404, detail="File missing on disk")
    return Response(
        _read_subtitle_text(path),
        media_type="text/vtt; charset=utf-8",
    )


@router.post("/videos/{video_id}/subtitles/{subtitle_id}/delete")
def delete_subtitle(
    video_id: int,
    subtitle_id: int,
    db: Annotated[Session, Depends(get_db)],
    _: AdminUserHTML,
):
    subtitle = _get_subtitle_or_404(db, subtitle_id)
    if subtitle.video_id != video_id:
        raise HTTPException(status_code=404, detail="Subtitles not found")
    path = subtitle_path(video_id, subtitle.filename)
    try:
        if path.exists():
            path.unlink()
    except OSError:
        logger.warning("Failed to delete subtitle file %s", path)
    db.delete(subtitle)
    db.commit()
    return _redirect(f"/videos/{video_id}")


@router.post("/videos/{video_id}/subtitles/{subtitle_id}/default")
def set_default_subtitle(
    video_id: int,
    subtitle_id: int,
    db: Annotated[Session, Depends(get_db)],
    _: AdminUserHTML,
):
    subtitle = _get_subtitle_or_404(db, subtitle_id)
    if subtitle.video_id != video_id:
        raise HTTPException(status_code=404, detail="Subtitles not found")
    for s in db.scalars(select(Subtitle).where(Subtitle.video_id == video_id)):
        s.is_default = s.id == subtitle_id
    db.commit()
    return _redirect(f"/videos/{video_id}")