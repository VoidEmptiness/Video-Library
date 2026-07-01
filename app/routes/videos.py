from __future__ import annotations

import io
import logging
import os
import zipfile
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from ..config import APP_TITLE, MAX_FILES_PER_UPLOAD, PAGE_SIZE, templates
from ..database import get_db
from ..models import Tag, Video
from ..services.storage import THUMB_DIR, ensure_dirs, unique_storage_name, video_path
from ..services.thumbnails import generate_all_thumbnails
from ..services.utils import (
    _all_tags,
    _delete_video_files,
    _format_duration,
    _probe_video_metadata,
    _redirect,
    _safe_return_to,
    _stream_file,
    _tag_ids_from_csv,
    _video_count,
    _video_query,
)
from ..services.hls import _cleanup_hls
from .auth import AdminUser, AdminUserHTML, UserHTML, is_guest

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/", response_class=HTMLResponse)
def index(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    user: UserHTML,
    tags: str | None = None,
    q: str | None = None,
    untagged: bool = False,
    page: int = 1,
):
    tag_ids = _tag_ids_from_csv(tags)
    total = _video_count(db, tag_ids=tag_ids, q=q, untagged=untagged)
    total_pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    page = max(1, min(page, total_pages))
    offset = (page - 1) * PAGE_SIZE
    videos = list(
        db.execute(
            _video_query(db, tag_ids=tag_ids, q=q, untagged=untagged)
            .offset(offset)
            .limit(PAGE_SIZE)
        )
        .unique()
        .scalars()
    )
    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "title": APP_TITLE,
            "user": user,
            "videos": videos,
            "all_tags": _all_tags(db),
            "selected_tag_ids": tag_ids,
            "tags": tags or "",
            "q": q or "",
            "untagged_filter": untagged,
            "page": page,
            "total_pages": total_pages,
            "total_videos": total,
        },
    )


@router.post("/videos/upload")
async def upload_video(
    background_tasks: BackgroundTasks,
    db: Annotated[Session, Depends(get_db)],
    _: AdminUserHTML,
    files: list[UploadFile] = File(...),
):
    if not files:
        raise HTTPException(status_code=400, detail="No files")
    if len(files) > MAX_FILES_PER_UPLOAD:
        raise HTTPException(
            status_code=400,
            detail=f"Слишком много файлов. Максимум: {MAX_FILES_PER_UPLOAD}",
        )
    ensure_dirs()
    created_ids: list[int] = []
    thumb_pairs: list[tuple[int, Path]] = []

    for file in files:
        if not file.filename:
            raise HTTPException(status_code=400, detail="No filename")

        storage_name = unique_storage_name(file.filename)
        dest = video_path(storage_name)

        size = 0
        try:
            with dest.open("wb") as f:
                while True:
                    chunk = await file.read(1024 * 1024)
                    if not chunk:
                        break
                    size += len(chunk)
                    f.write(chunk)
        except OSError as e:
            if dest.exists():
                dest.unlink()
            logger.exception("Disk error while saving %s", file.filename)
            if hasattr(e, "errno") and e.errno == 28:
                raise HTTPException(status_code=507, detail="Недостаточно места на диске")
            raise HTTPException(status_code=500, detail="Ошибка записи файла на диск")

        video = Video(
            filename=storage_name,
            original_name=file.filename,
            content_type=file.content_type or "application/octet-stream",
            size_bytes=size,
        )
        db.add(video)
        db.flush()

        thumb_pairs.append((video.id, dest))
        created_ids.append(video.id)

    logger.info("Uploaded %d file(s): %s", len(created_ids), [f.filename for f in files if f.filename])

    if thumb_pairs:
        background_tasks.add_task(generate_all_thumbnails, thumb_pairs)

    db.commit()
    ids_str = ",".join(str(x) for x in created_ids)
    return RedirectResponse(url=f"/upload/tags?ids={ids_str}", status_code=303)


@router.get("/videos/{video_id}", response_class=HTMLResponse)
def video_page(
    request: Request,
    video_id: int,
    db: Annotated[Session, Depends(get_db)],
    user: UserHTML,
):
    video = db.get(Video, video_id)
    if not video:
        raise HTTPException(status_code=404, detail="Video not found")
    media_path = video_path(video.filename)
    codec_name, duration_seconds, video_width, video_height = (
        _probe_video_metadata(media_path) if media_path.exists() else (None, None, None, None)
    )
    resolution_label = f"{video_width}\u00d7{video_height}" if video_width and video_height else None
    codec_display = {"hevc": "H.265", "h264": "H.264", "h265": "H.265"}.get(codec_name, codec_name) if codec_name else None
    from ..services.settings import get_setting

    default_volume = get_setting("default_volume", 1.0)
    if is_guest(user):
        default_volume = 0.01
    return templates.TemplateResponse(
        "video.html",
        {
            "request": request,
            "title": f"{APP_TITLE} \u2014 {video.original_name}",
            "user": user,
            "video": video,
            "all_tags": _all_tags(db),
            "codec_name": codec_display,
            "duration_label": _format_duration(duration_seconds),
            "resolution_label": resolution_label,
            "video_width": video_width,
            "video_height": video_height,
            "default_volume": default_volume,
        },
    )


@router.get("/media/{video_id}")
def media_stream(
    video_id: int,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    _: UserHTML,
):
    video = db.get(Video, video_id)
    if not video:
        raise HTTPException(status_code=404, detail="Video not found")
    return _stream_file(video_path(video.filename), video.content_type or "application/octet-stream", request)


@router.get("/download/{video_id}")
def video_download(
    video_id: int,
    db: Annotated[Session, Depends(get_db)],
    _: AdminUser,
):
    video = db.get(Video, video_id)
    if not video:
        raise HTTPException(status_code=404, detail="Video not found")
    path = video_path(video.filename)
    if not path.exists():
        raise HTTPException(status_code=404, detail="File missing on disk")
    return FileResponse(
        path=str(path),
        media_type="application/octet-stream",
        filename=video.original_name,
    )


@router.get("/thumb/{video_id}")
def thumb_get(
    video_id: int,
    db: Annotated[Session, Depends(get_db)],
    _: UserHTML,
):
    video = db.get(Video, video_id)
    if not video or not video.thumbnail_path:
        raise HTTPException(status_code=404, detail="Thumb not found")
    p = Path(video.thumbnail_path)
    if not p.exists():
        raise HTTPException(status_code=404, detail="Thumb missing on disk")
    return FileResponse(path=str(p), media_type="image/jpeg")


@router.post("/videos/{video_id}/delete")
def delete_video(
    video_id: int,
    db: Annotated[Session, Depends(get_db)],
    _: AdminUserHTML,
):
    video = db.get(Video, video_id)
    if not video:
        raise HTTPException(status_code=404, detail="Video not found")
    _cleanup_hls(video_id)
    _delete_video_files(
        video_path(video.filename),
        Path(video.thumbnail_path) if video.thumbnail_path else None,
    )
    db.delete(video)
    db.commit()
    logger.info("Deleted video %s (%s)", video_id, video.original_name)
    return _redirect("/")


@router.post("/videos/tags-bulk")
def tags_bulk(
    db: Annotated[Session, Depends(get_db)],
    _: AdminUserHTML,
    video_ids: Annotated[list[int] | None, Form()] = None,
    tag_ids: Annotated[list[int] | None, Form()] = None,
    return_to: Annotated[str | None, Form()] = None,
):
    if not video_ids:
        return _redirect(_safe_return_to(return_to))

    tag_objs: list[Tag] = []
    if tag_ids:
        tag_objs = list(db.scalars(select(Tag).where(Tag.id.in_(tag_ids)).order_by(Tag.name)))

    if not tag_objs:
        return _redirect(_safe_return_to(return_to))

    videos = list(
        db.execute(
            select(Video).where(Video.id.in_(video_ids)).options(joinedload(Video.tags))
        )
        .unique()
        .scalars()
    )
    for video in videos:
        existing_ids = {t.id for t in video.tags}
        for tag in tag_objs:
            if tag.id not in existing_ids:
                video.tags.append(tag)
        db.add(video)
    db.commit()

    return _redirect(_safe_return_to(return_to))


@router.post("/videos/delete-selected")
def delete_selected_videos(
    db: Annotated[Session, Depends(get_db)],
    _: AdminUserHTML,
    video_ids: Annotated[list[int] | None, Form()] = None,
    return_to: Annotated[str | None, Form()] = None,
):
    if not video_ids:
        return _redirect(_safe_return_to(return_to))

    videos = list(db.scalars(select(Video).where(Video.id.in_(video_ids))))

    for video in videos:
        _cleanup_hls(video.id)
        _delete_video_files(
            video_path(video.filename),
            Path(video.thumbnail_path) if video.thumbnail_path else None,
        )
        db.delete(video)
    db.commit()

    return _redirect(_safe_return_to(return_to))


@router.post("/videos/download-selected")
def download_selected_videos(
    db: Annotated[Session, Depends(get_db)],
    _: AdminUser,
    video_ids: Annotated[list[int] | None, Form()] = None,
):
    if not video_ids:
        raise HTTPException(status_code=400, detail="No videos selected")

    videos = list(db.scalars(select(Video).where(Video.id.in_(video_ids))))
    if not videos:
        raise HTTPException(status_code=404, detail="Videos not found")

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for video in videos:
            path = video_path(video.filename)
            if path.exists():
                zf.write(str(path), video.original_name)

    return Response(
        content=buf.getvalue(),
        media_type="application/zip",
        headers={"Content-Disposition": 'attachment; filename="videos.zip"'},
    )


@router.post("/videos/{video_id}/tags")
def set_video_tags(
    video_id: int,
    db: Annotated[Session, Depends(get_db)],
    _: AdminUserHTML,
    tag_ids: Annotated[list[int] | None, Form()] = None,
    return_to: Annotated[str | None, Form()] = None,
):
    video = db.get(Video, video_id)
    if not video:
        raise HTTPException(status_code=404, detail="Video not found")
    tags = []
    if tag_ids:
        tags = list(db.scalars(select(Tag).where(Tag.id.in_(tag_ids)).order_by(Tag.name)))
    video.tags = tags
    db.add(video)
    db.commit()
    return _redirect(_safe_return_to(return_to) or f"/videos/{video_id}")


@router.get("/upload/tags", response_class=HTMLResponse)
def upload_tags_page(
    request: Request,
    ids: str,
    db: Annotated[Session, Depends(get_db)],
    _: AdminUserHTML,
):
    id_list: list[int] = []
    for part in (ids or "").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            id_list.append(int(part))
        except ValueError:
            continue

    if not id_list:
        return _redirect("/")

    videos = list(
        db.execute(
            select(Video)
            .where(Video.id.in_(id_list))
            .options(joinedload(Video.tags))
        )
        .unique()
        .scalars()
    )
    all_tags = _all_tags(db)
    return_to = "/upload/tags?ids=" + ",".join(str(x) for x in id_list)
    return templates.TemplateResponse(
        "upload_tags.html",
        {
            "request": request,
            "title": f"{APP_TITLE} \u2014 Указать теги",
            "videos": videos,
            "all_tags": all_tags,
            "return_to": return_to,
        },
    )
