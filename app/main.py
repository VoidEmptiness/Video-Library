from __future__ import annotations

import asyncio
import io
import logging
import os
import json
import subprocess
import time
import zipfile
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated

from fastapi import BackgroundTasks, Depends, FastAPI, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, StreamingResponse
from pathlib import PurePath
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.orm import Session, joinedload

from .database import SessionLocal, engine, get_db
from .models import Base, Folder, Tag, Video, VideoTag
from .services.auth import GUEST_USER, SESSION_COOKIE, create_session_token, is_guest, parse_session_token, session_expiry_dt, verify_credentials
from .services.storage import THUMB_DIR, VIDEO_DIR, ensure_dirs, unique_storage_name, video_path
from .services.thumbnails import generate_thumbnail
from .services.settings import get_setting, load_settings, save_settings
from .services.transcoding import (
    FFPROBE_EXE,
    TRANSCODE_DOWNSCALE_FPS,
    TRANSCODE_DOWNSCALE_HEIGHT,
    TRANSCODE_DOWNSCALE_MAX_HEIGHT,
    ffprobe_available,
    transcode_to_h264,
    video_fps,
    video_height,
    video_needs_downscale,
)

logger = logging.getLogger(__name__)

APP_TITLE = os.getenv("APP_TITLE", "Video Library")
MAX_FILES_PER_UPLOAD = int(os.getenv("MAX_FILES_PER_UPLOAD", "50"))
THUMBNAIL_CONCURRENCY = int(os.getenv("THUMBNAIL_CONCURRENCY", "4"))
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
templates.env.globals["static_version"] = str(int(time.time()))


@asynccontextmanager
async def _lifespan(app: FastAPI):
    ensure_dirs()
    init_db()
    yield


app = FastAPI(title=APP_TITLE, lifespan=_lifespan)
app.mount("/static", StaticFiles(directory=str(Path(__file__).parent / "static")), name="static")


def short_name(value: str | None, limit: int = 36) -> str:
    value = value or ""
    if len(value) <= limit:
        return value
    return value[:limit].rstrip() + "..."


templates.env.filters["short_name"] = short_name


def init_db() -> None:
    Base.metadata.create_all(bind=engine)
    _migrate_db()


def _migrate_db() -> None:
    from sqlalchemy import inspect, text
    try:
        inspector = inspect(engine)
        columns = [col["name"] for col in inspector.get_columns("folders")]
        if "parent_id" not in columns:
            with engine.connect() as conn:
                conn.execute(text("ALTER TABLE folders ADD COLUMN parent_id INTEGER REFERENCES folders(id) ON DELETE SET NULL"))
                conn.commit()
        indexes = [ix["name"] for ix in inspector.get_indexes("folders")]
        for ix_name in indexes:
            if ix_name and "name" in ix_name.lower():
                with engine.connect() as conn:
                    conn.execute(text('DROP INDEX IF EXISTS "%s"' % ix_name.replace('"', '""')))
                    conn.commit()
                break
    except Exception:
        logger.exception("Migration failed (folders)")

    try:
        inspector = inspect(engine)
        columns = [col["name"] for col in inspector.get_columns("tags")]
        if "description" not in columns:
            with engine.connect() as conn:
                conn.execute(text("ALTER TABLE tags ADD COLUMN description VARCHAR(256)"))
                conn.commit()
    except Exception:
        logger.exception("Migration failed (tags)")


def _wants_auth() -> bool:
    return os.getenv("AUTH_ENABLED", "true").lower() in {"1", "true", "yes", "on"}


def require_user(request: Request) -> str | None:
    if not _wants_auth():
        return "anonymous"
    token = request.cookies.get(SESSION_COOKIE, "")
    user = parse_session_token(token)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return user


def require_user_html(request: Request) -> str | None:
    if not _wants_auth():
        return "anonymous"
    token = request.cookies.get(SESSION_COOKIE, "")
    user = parse_session_token(token)
    if not user:
        raise HTTPException(status_code=303, headers={"Location": "/login"})
    return user


User = Annotated[str | None, Depends(require_user)]
UserHTML = Annotated[str | None, Depends(require_user_html)]


def require_admin(request: Request) -> str | None:
    user = require_user(request)
    if is_guest(user):
        raise HTTPException(status_code=403, detail="Admins only")
    return user


def require_admin_html(request: Request) -> str | None:
    user = require_user_html(request)
    if is_guest(user):
        raise HTTPException(status_code=403, detail="Admins only")
    return user


AdminUser = Annotated[str | None, Depends(require_admin)]
AdminUserHTML = Annotated[str | None, Depends(require_admin_html)]


def _redirect(url: str) -> RedirectResponse:
    return RedirectResponse(url=url, status_code=303)


def _safe_return_to(return_to: str | None) -> str:
    if return_to and return_to.startswith("/") and not return_to.startswith("//"):
        return return_to
    return "/"


def _delete_video_files(path: Path, thumb: Path | None) -> None:
    try:
        if path.exists():
            path.unlink()
    except Exception:
        pass
    try:
        if thumb and thumb.exists():
            thumb.unlink()
    except Exception:
        pass


def _delete_video_files_with_720p(video: Video) -> None:
    path = video_path(video.filename)
    thumb = Path(video.thumbnail_path) if video.thumbnail_path else None
    _delete_video_files(path, thumb)
    if video.filename_720p:
        try:
            p = video_path(video.filename_720p)
            if p.exists():
                p.unlink()
        except Exception:
            pass


def _probe_video_metadata(path: Path) -> tuple[str | None, float | None, int | None, int | None]:
    if not ffprobe_available():
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
    except Exception:
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


def _format_duration(seconds: float | None) -> str:
    if seconds is None or seconds < 0:
        return "—"
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


@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    if not _wants_auth():
        return _redirect("/")
    return templates.TemplateResponse("login.html", {"request": request, "title": APP_TITLE})


@app.post("/login")
def login_submit(
    request: Request,
    username: Annotated[str, Form()],
    password: Annotated[str, Form()],
):
    if not _wants_auth():
        return _redirect("/")
    if not verify_credentials(username, password):
        return templates.TemplateResponse(
            "login.html",
            {"request": request, "title": APP_TITLE, "error": "Неверный логин или пароль"},
            status_code=400,
        )
    token = create_session_token(username)
    resp = _redirect("/")
    resp.set_cookie(
        key=SESSION_COOKIE,
        value=token,
        httponly=True,
        samesite="lax",
        expires=session_expiry_dt(),
    )
    return resp


@app.post("/logout")
def logout():
    resp = _redirect("/login")
    resp.delete_cookie(SESSION_COOKIE)
    return resp


@app.post("/guest-login")
def guest_login():
    if not _wants_auth():
        return _redirect("/")
    token = create_session_token(GUEST_USER)
    resp = _redirect("/")
    resp.set_cookie(
        key=SESSION_COOKIE,
        value=token,
        httponly=True,
        samesite="lax",
        expires=session_expiry_dt(),
    )
    return resp


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


def _video_query(db: Session, tag_ids: set[int] | None = None, q: str | None = None, untagged: bool = False):
    stmt = select(Video).order_by(Video.created_at.desc())
    if untagged:
        stmt = stmt.where(~Video.tags.any()).options(joinedload(Video.tags))
    elif tag_ids:
        subq = select(VideoTag.video_id).where(VideoTag.tag_id.in_(tag_ids)).group_by(VideoTag.video_id).subquery()
        stmt = stmt.where(Video.id.in_(subq)).options(joinedload(Video.tags))
    else:
        stmt = stmt.options(joinedload(Video.tags))
    if q:
        q = q.strip()
        if q:
            stmt = stmt.where(Video.original_name.ilike(f"%{q}%"))
    return stmt


@app.get("/", response_class=HTMLResponse)
def index(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    user: UserHTML,
    tags: str | None = None,
    q: str | None = None,
    untagged: bool = False,
):
    tag_ids = _tag_ids_from_csv(tags)
    videos = list(db.execute(_video_query(db, tag_ids=tag_ids, q=q, untagged=untagged)).unique().scalars())
    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "title": APP_TITLE,
            "user": user,
            "videos": videos,
            "all_tags": _all_tags(db),
            "selected_tag_ids": tag_ids,
            "q": q or "",
            "untagged_filter": untagged,
        },
    )


def _do_transcode(video_id: int, original_path: Path) -> None:
    if not get_setting("transcode_to_720p", True):
        return
    if not video_needs_downscale(original_path):
        return

    display_name = original_path.stem + "_720p.mp4"
    out_name = unique_storage_name(display_name, ext_override=".mp4")
    out_dest = video_path(out_name)

    target_fps = None
    original_fps = video_fps(original_path)
    if original_fps is not None:
        if original_fps >= TRANSCODE_DOWNSCALE_FPS:
            target_fps = TRANSCODE_DOWNSCALE_FPS

    if not transcode_to_h264(
        original_path,
        out_dest,
        video_id,
        downscale_height=TRANSCODE_DOWNSCALE_HEIGHT,
        target_fps=target_fps,
    ):
        return

    db = SessionLocal()
    try:
        video = db.get(Video, video_id)
        if video:
            video.filename_720p = out_name
            db.add(video)
            db.commit()
    except Exception:
        logger.exception("Failed to save transcode result for video %s", video_id)
    finally:
        db.close()


async def _generate_all_thumbnails(pairs: list[tuple[int, Path]]) -> None:
    sem = asyncio.Semaphore(THUMBNAIL_CONCURRENCY)

    async def _one(video_id: int, src: Path) -> Path | None:
        async with sem:
            thumb = THUMB_DIR / f"{video_id}.jpg"
            ok = await asyncio.to_thread(generate_thumbnail, src, thumb)
            return thumb if ok else None

    results = await asyncio.gather(*[_one(vid, p) for vid, p in pairs])

    db = SessionLocal()
    try:
        for video_id, thumb_path in zip([vid for vid, _ in pairs], results):
            if thumb_path is None:
                continue
            video = db.get(Video, video_id)
            if video:
                video.thumbnail_path = str(thumb_path)
                db.add(video)
        db.commit()
    except Exception:
        logger.exception("Failed to save thumbnails")
    finally:
        db.close()


@app.post("/videos/upload")
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
    transcode_args: list[tuple[int, Path]] = []

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
            if hasattr(e, 'errno') and e.errno == 28:
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
        transcode_args.append((video.id, dest))

        created_ids.append(video.id)

    if thumb_pairs:
        background_tasks.add_task(_generate_all_thumbnails, thumb_pairs)
    for video_id, dest in transcode_args:
        background_tasks.add_task(_do_transcode, video_id, dest)
    db.commit()
    ids_str = ",".join(str(x) for x in created_ids)
    return RedirectResponse(url=f"/upload/tags?ids={ids_str}", status_code=303)


@app.get("/videos/{video_id}", response_class=HTMLResponse)
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
    codec_name, duration_seconds, video_width, video_height = _probe_video_metadata(media_path) if media_path.exists() else (None, None, None, None)
    resolution_label = f"{video_width}×{video_height}" if video_width and video_height else None
    codec_display = {"hevc": "H.265", "h264": "H.264", "h265": "H.265"}.get(codec_name, codec_name) if codec_name else None
    has_720p = bool(video.filename_720p) and video_path(video.filename_720p).exists()
    default_volume = get_setting("default_volume", 1.0)
    if is_guest(user):
        default_volume = 0.01
    return templates.TemplateResponse(
        "video.html",
        {
            "request": request,
            "title": f"{APP_TITLE} — {video.original_name}",
            "user": user,
            "video": video,
            "all_tags": _all_tags(db),
            "codec_name": codec_display,
            "duration_label": _format_duration(duration_seconds),
            "resolution_label": resolution_label,
            "video_width": video_width,
            "video_height": video_height,
            "has_720p": has_720p,
            "default_volume": default_volume,
        },
    )


def _stream_file(path: Path, media_type: str, request: Request) -> FileResponse | StreamingResponse:
    if not path.exists():
        raise HTTPException(status_code=404, detail="File missing on disk")
    file_size = path.stat().st_size
    range_header = request.headers.get("range")

    if not range_header:
        return FileResponse(path=str(path), media_type=media_type)

    parsed = _parse_range_header(range_header, file_size)
    if not parsed:
        return Response(status_code=416, headers={"Content-Range": f"bytes */{file_size}"})

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
    return StreamingResponse(iter_file(), status_code=206, media_type=media_type, headers=headers)


@app.get("/media/720p/{video_id}")
def media_stream_720p(
    video_id: int,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    _: User,
):
    video = db.get(Video, video_id)
    if not video:
        raise HTTPException(status_code=404, detail="Video not found")
    if not video.filename_720p:
        raise HTTPException(status_code=404, detail="720p version not available")
    return _stream_file(video_path(video.filename_720p), "video/mp4", request)


@app.get("/media/{video_id}")
def media_stream(
    video_id: int,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    _: User,
):
    video = db.get(Video, video_id)
    if not video:
        raise HTTPException(status_code=404, detail="Video not found")
    return _stream_file(video_path(video.filename), video.content_type or "application/octet-stream", request)


@app.get("/download/{video_id}")
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


@app.get("/download/720p/{video_id}")
def video_download_720p(
    video_id: int,
    db: Annotated[Session, Depends(get_db)],
    _: AdminUser,
):
    video = db.get(Video, video_id)
    if not video:
        raise HTTPException(status_code=404, detail="Video not found")
    if not video.filename_720p:
        raise HTTPException(status_code=404, detail="720p version not available")
    path = video_path(video.filename_720p)
    if not path.exists():
        raise HTTPException(status_code=404, detail="File missing on disk")
    stem = PurePath(video.original_name).stem
    download_name = f"{stem}_720p.mp4"
    return FileResponse(
        path=str(path),
        media_type="application/octet-stream",
        filename=download_name,
    )


@app.get("/thumb/{video_id}")
def thumb_get(
    video_id: int,
    db: Annotated[Session, Depends(get_db)],
    _: User,
):
    video = db.get(Video, video_id)
    if not video or not video.thumbnail_path:
        raise HTTPException(status_code=404, detail="Thumb not found")
    p = Path(video.thumbnail_path)
    if not p.exists():
        raise HTTPException(status_code=404, detail="Thumb missing on disk")
    return FileResponse(path=str(p), media_type="image/jpeg")


@app.post("/videos/{video_id}/delete")
def delete_video(
    video_id: int,
    db: Annotated[Session, Depends(get_db)],
    _: AdminUserHTML,
):
    video = db.get(Video, video_id)
    if not video:
        raise HTTPException(status_code=404, detail="Video not found")
    _delete_video_files_with_720p(video)
    db.delete(video)
    db.commit()
    return _redirect("/")


@app.post("/videos/tags-bulk")
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


@app.post("/videos/delete-selected")
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
        _delete_video_files_with_720p(video)
        db.delete(video)
    db.commit()

    return _redirect(_safe_return_to(return_to))


@app.post("/videos/download-selected")
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


@app.post("/videos/{video_id}/tags")
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


@app.get("/upload/tags", response_class=HTMLResponse)
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
            "title": f"{APP_TITLE} — Указать теги",
            "videos": videos,
            "all_tags": all_tags,
            "return_to": return_to,
        },
    )


@app.get("/tags", response_class=HTMLResponse)
def tags_page(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    _: AdminUserHTML,
):
    tags = list(db.scalars(select(Tag).order_by(Tag.name)))
    return templates.TemplateResponse(
        "tags.html",
        {"request": request, "title": f"{APP_TITLE} — Теги", "tags": tags},
    )


@app.post("/tags/create")
def tag_create(
    name: Annotated[str, Form()],
    db: Annotated[Session, Depends(get_db)],
    _: AdminUserHTML,
    description: Annotated[str | None, Form()] = None,
):
    name = name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="Empty name")
    exists = db.scalar(select(func.count()).select_from(Tag).where(Tag.name == name))
    if exists > 0:
        return _redirect("/tags")
    description_clean = description.strip() if description else None
    db.add(Tag(name=name, description=description_clean or None))
    db.commit()
    return _redirect("/tags")


@app.post("/tags/{tag_id}/edit")
def tag_edit(
    tag_id: int,
    db: Annotated[Session, Depends(get_db)],
    _: AdminUserHTML,
    name: Annotated[str, Form()],
    description: Annotated[str | None, Form()] = None,
):
    tag = db.get(Tag, tag_id)
    if not tag:
        raise HTTPException(status_code=404, detail="Tag not found")
    name = name.strip()
    if name:
        tag.name = name
    tag.description = description.strip() if description else None
    db.add(tag)
    db.commit()
    return _redirect("/tags")


@app.post("/tags/{tag_id}/delete")
def tag_delete(
    tag_id: int,
    db: Annotated[Session, Depends(get_db)],
    _: AdminUserHTML,
):
    tag = db.get(Tag, tag_id)
    if not tag:
        raise HTTPException(status_code=404, detail="Tag not found")
    db.delete(tag)
    db.commit()
    return _redirect("/tags")


@app.post("/tags/delete-selected")
def tag_delete_selected(
    db: Annotated[Session, Depends(get_db)],
    _: AdminUserHTML,
    tag_ids: Annotated[list[int] | None, Form()] = None,
):
    if not tag_ids:
        return _redirect("/tags")
    tags = list(db.scalars(select(Tag).where(Tag.id.in_(tag_ids))))
    for tag in tags:
        db.delete(tag)
    db.commit()
    return _redirect("/tags")


@app.get("/folders", response_class=HTMLResponse)
def folders_page(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    _: AdminUserHTML,
):
    folders = list(
        db.execute(
            select(Folder)
            .options(joinedload(Folder.tags), joinedload(Folder.parent))
            .order_by(Folder.parent_id.is_(None), Folder.parent_id, Folder.name)
        )
        .unique()
        .scalars()
    )
    return templates.TemplateResponse(
        "folders.html",
        {
            "request": request,
            "title": f"{APP_TITLE} — Папки",
            "folders": folders,
            "all_tags": _all_tags(db),
            "all_folders": folders,
        },
    )


@app.post("/folders/create")
def folder_create(
    db: Annotated[Session, Depends(get_db)],
    _: AdminUserHTML,
    name: Annotated[str, Form()],
    parent_id: Annotated[int | None, Form()] = None,
    match_all: Annotated[str | None, Form()] = None,
    tag_ids: Annotated[list[int] | None, Form()] = None,
):
    name = name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="Empty name")
    if parent_id is not None and parent_id > 0 and not db.get(Folder, parent_id):
        parent_id = None
    elif parent_id is not None and parent_id <= 0:
        parent_id = None
    folder = Folder(name=name, parent_id=parent_id, match_all=bool(match_all))
    if tag_ids:
        folder.tags = list(db.scalars(select(Tag).where(Tag.id.in_(tag_ids)).order_by(Tag.name)))
    db.add(folder)
    db.commit()
    
    if parent_id:
        return _redirect(f"/folders/{parent_id}")
    return _redirect("/folders")


@app.post("/folders/{folder_id}/delete")
def folder_delete(
    folder_id: int,
    db: Annotated[Session, Depends(get_db)],
    _: AdminUserHTML,
):
    folder = db.get(Folder, folder_id)
    if not folder:
        raise HTTPException(status_code=404, detail="Folder not found")
    db.delete(folder)
    db.commit()
    return _redirect("/folders")


@app.post("/folders/delete-selected")
def folder_delete_selected(
    db: Annotated[Session, Depends(get_db)],
    _: AdminUserHTML,
    folder_ids: Annotated[list[int] | None, Form()] = None,
):
    if not folder_ids:
        return _redirect("/folders")
    folders = list(db.scalars(select(Folder).where(Folder.id.in_(folder_ids))))
    for folder in folders:
        db.delete(folder)
    db.commit()
    return _redirect("/folders")


@app.post("/folders/{folder_id}/edit")
def folder_edit(
    folder_id: int,
    db: Annotated[Session, Depends(get_db)],
    _: AdminUserHTML,
    name: Annotated[str, Form()],
    parent_id: Annotated[int | None, Form()] = None,
    match_all: Annotated[str | None, Form()] = None,
    tag_ids: Annotated[list[int] | None, Form()] = None,
):
    folder = db.get(Folder, folder_id)
    if not folder:
        raise HTTPException(status_code=404, detail="Folder not found")
    folder.name = name.strip() or folder.name
    folder.match_all = bool(match_all)
    if parent_id is not None and parent_id > 0 and parent_id != folder.id and db.get(Folder, parent_id) and not _would_create_cycle(db, folder.id, parent_id):
        folder.parent_id = parent_id
    else:
        folder.parent_id = None
    if tag_ids:
        folder.tags = list(db.scalars(select(Tag).where(Tag.id.in_(tag_ids)).order_by(Tag.name)))
    else:
        folder.tags = []
    db.add(folder)
    db.commit()
    return _redirect("/folders")


def _would_create_cycle(db: Session, folder_id: int, parent_id: int | None) -> bool:
    if not parent_id:
        return False
    current = parent_id
    seen = {folder_id}
    while current is not None:
        if current in seen:
            return True
        seen.add(current)
        parent = db.get(Folder, current)
        current = parent.parent_id if parent else None
    return False


def _folder_videos(db: Session, folder: Folder):
    tag_ids = [t.id for t in folder.tags]
    if not tag_ids:
        return []

    matching_ids_q = (
        select(VideoTag.video_id)
        .where(VideoTag.tag_id.in_(tag_ids))
        .group_by(VideoTag.video_id)
    )
    if folder.match_all:
        matching_ids_q = matching_ids_q.having(func.count(func.distinct(VideoTag.tag_id)) == len(tag_ids))

    stmt = (
        select(Video)
        .where(Video.id.in_(matching_ids_q.subquery()))
        .options(joinedload(Video.tags))
        .order_by(Video.created_at.desc())
    )
    return list(db.execute(stmt).unique().scalars())


@app.get("/folders/{folder_id}", response_class=HTMLResponse)
def folder_view(
    request: Request,
    folder_id: int,
    db: Annotated[Session, Depends(get_db)],
    user: UserHTML,
):
    folder = db.get(Folder, folder_id)
    if not folder:
        raise HTTPException(status_code=404, detail="Folder not found")
    folder = (
        db.execute(
            select(Folder)
            .where(Folder.id == folder_id)
            .options(joinedload(Folder.tags), joinedload(Folder.children))
        )
        .unique()
        .scalar_one()
    )
    videos = _folder_videos(db, folder)
    return templates.TemplateResponse(
        "folder_view.html",
        {
            "request": request,
            "title": f"{APP_TITLE} — {folder.name}",
            "user": user,
            "folder": folder,
            "videos": videos,
            "all_tags": _all_tags(db),
        },
    )


@app.get("/library/export")
def library_export(
    db: Annotated[Session, Depends(get_db)],
    _: AdminUser,
):
    videos = list(
        db.execute(
            select(Video).options(joinedload(Video.tags)).order_by(Video.id)
        )
        .unique()
        .scalars()
    )
    meta = []
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as zf:
        for video in videos:
            src = video_path(video.filename)
            if src.exists():
                zf.write(str(src), video.filename)
            meta.append({
                "filename": video.filename,
                "filename_720p": video.filename_720p,
                "original_name": video.original_name,
                "content_type": video.content_type,
                "size_bytes": video.size_bytes,
                "created_at": video.created_at.isoformat() if video.created_at else None,
                "tags": [{"name": t.name, "description": t.description} for t in video.tags],
            })
        zf.writestr("library.json", json.dumps(meta, ensure_ascii=False, indent=2))
    return Response(
        content=buf.getvalue(),
        media_type="application/zip",
        headers={"Content-Disposition": 'attachment; filename="library.zip"'},
    )


@app.post("/library/import")
def library_import(
    db: Annotated[Session, Depends(get_db)],
    _: AdminUserHTML,
    file: UploadFile = File(...),
):
    raw = file.file.read()
    try:
        zf = zipfile.ZipFile(io.BytesIO(raw), "r")
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid ZIP file")

    if "library.json" not in zf.namelist():
        raise HTTPException(status_code=400, detail="ZIP must contain library.json")

    try:
        items = json.loads(zf.read("library.json"))
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid library.json in archive")

    if not isinstance(items, list):
        items = [items]

    ensure_dirs()
    imported = 0
    skipped = 0

    for item in items:
        raw_filename = item.get("filename") or ""
        filename = Path(raw_filename).name
        if not filename:
            skipped += 1
            continue

        existing = db.scalar(select(Video).where(Video.filename == filename))
        if existing:
            skipped += 1
            continue

        dest = video_path(filename)
        if raw_filename in zf.namelist():
            with dest.open("wb") as f:
                f.write(zf.read(raw_filename))
        else:
            skipped += 1
            continue

        video = Video(
            filename=filename,
            filename_720p=item.get("filename_720p"),
            original_name=item.get("original_name", filename),
            content_type=item.get("content_type", "application/octet-stream"),
            size_bytes=item.get("size_bytes", dest.stat().st_size),
        )
        db.add(video)
        db.flush()

        for tag_item in item.get("tags", []):
            name = tag_item.get("name")
            if not name:
                continue
            tag = db.scalar(select(Tag).where(Tag.name == name))
            if not tag:
                tag = Tag(name=name, description=tag_item.get("description"))
                db.add(tag)
                db.flush()
            video.tags.append(tag)

        imported += 1

    zf.close()
    db.commit()
    return _redirect(f"/?imported={imported}&skipped={skipped}")


@app.get("/settings", response_class=HTMLResponse)
def settings_page(
    request: Request,
    _: AdminUserHTML,
):
    settings = load_settings()
    return templates.TemplateResponse(
        "settings.html",
        {
            "request": request,
            "title": f"{APP_TITLE} — Настройки",
            "transcode_to_720p": settings.get("transcode_to_720p", True),
            "default_volume": settings.get("default_volume", 1.0),
        },
    )


@app.post("/settings")
def settings_save(
    _: AdminUserHTML,
    transcode_to_720p: Annotated[str | None, Form()] = None,
    default_volume: Annotated[float | None, Form()] = None,
):
    settings = {"transcode_to_720p": transcode_to_720p == "1"}
    if default_volume is not None:
        settings["default_volume"] = max(0.0, min(1.0, default_volume))
    save_settings(settings)
    return RedirectResponse(url="/settings", status_code=303)


@app.post("/settings/reset-thumbnails")
def reset_thumbnails(
    background_tasks: BackgroundTasks,
    db: Annotated[Session, Depends(get_db)],
    _: AdminUserHTML,
):
    videos = list(db.scalars(select(Video)))
    thumb_pairs: list[tuple[int, Path]] = []
    for video in videos:
        old_thumb = Path(video.thumbnail_path) if video.thumbnail_path else THUMB_DIR / f"{video.id}.jpg"
        if old_thumb.exists():
            try:
                old_thumb.unlink()
            except Exception:
                pass
        video.thumbnail_path = None
        src = video_path(video.filename)
        if src.exists():
            thumb_pairs.append((video.id, src))
    db.commit()
    if thumb_pairs:
        background_tasks.add_task(_generate_all_thumbnails, thumb_pairs)
    return RedirectResponse(url="/settings", status_code=303)

