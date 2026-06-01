from __future__ import annotations

import os
import json
import subprocess
from pathlib import Path
from typing import Annotated

from fastapi import BackgroundTasks, Depends, FastAPI, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.orm import Session, joinedload

from .database import SessionLocal, engine, get_db
from .models import Base, Folder, Tag, Video
from .services.auth import SESSION_COOKIE, create_session_token, parse_session_token, session_expiry_dt, verify_credentials
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
    video_needs_transcode,
)


APP_TITLE = os.getenv("APP_TITLE", "Video Library")
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

app = FastAPI(title=APP_TITLE)
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
                    conn.execute(text(f"DROP INDEX IF EXISTS {ix_name}"))
                    conn.commit()
                break
    except Exception:
        pass

    try:
        inspector = inspect(engine)
        columns = [col["name"] for col in inspector.get_columns("tags")]
        if "description" not in columns:
            with engine.connect() as conn:
                conn.execute(text("ALTER TABLE tags ADD COLUMN description VARCHAR(256)"))
                conn.commit()
    except Exception:
        pass


@app.on_event("startup")
def _startup() -> None:
    ensure_dirs()
    init_db()


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
    stmt = select(Video).options(joinedload(Video.tags)).order_by(Video.created_at.desc())
    if untagged:
        stmt = stmt.where(~Video.tags.any())
    elif tag_ids:
        stmt = stmt.join(Video.tags).where(Tag.id.in_(tag_ids)).group_by(Video.id)
    if q:
        q = q.strip()
        if q:
            stmt = stmt.where(Video.original_name.ilike(f"%{q}%"))
    return stmt


@app.get("/", response_class=HTMLResponse)
def index(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    _: UserHTML,
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
            "videos": videos,
            "all_tags": _all_tags(db),
            "selected_tag_ids": tag_ids,
            "q": q or "",
            "untagged_filter": untagged,
        },
    )


def _do_transcode(video_id: int, original_path: Path) -> None:
    from .database import SessionLocal as _SessionLocal
    from .models import Video as _Video

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

    db = _SessionLocal()
    try:
        video = db.get(_Video, video_id)
        if video:
            video.filename_720p = out_name
            db.add(video)
            db.commit()
    except Exception:
        pass
    finally:
        db.close()


@app.post("/videos/upload")
async def upload_video(
    background_tasks: BackgroundTasks,
    db: Annotated[Session, Depends(get_db)],
    _: UserHTML,
    files: list[UploadFile] = File(...),
):
    if not files:
        raise HTTPException(status_code=400, detail="No files")
    ensure_dirs()
    created_ids: list[int] = []

    for file in files:
        if not file.filename:
            raise HTTPException(status_code=400, detail="No filename")

        storage_name = unique_storage_name(file.filename)
        dest = video_path(storage_name)

        size = 0
        with dest.open("wb") as f:
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                f.write(chunk)

        video = Video(
            filename=storage_name,
            original_name=file.filename,
            content_type=file.content_type or "application/octet-stream",
            size_bytes=size,
        )
        db.add(video)
        db.flush()

        thumb = THUMB_DIR / f"{video.id}.jpg"
        if generate_thumbnail(dest, thumb):
            video.thumbnail_path = str(thumb)

        background_tasks.add_task(_do_transcode, video.id, dest)

        created_ids.append(video.id)

    db.commit()
    ids_str = ",".join(str(x) for x in created_ids)
    return RedirectResponse(url=f"/upload/tags?ids={ids_str}", status_code=303)


@app.get("/videos/{video_id}", response_class=HTMLResponse)
def video_page(
    request: Request,
    video_id: int,
    db: Annotated[Session, Depends(get_db)],
    _: UserHTML,
):
    video = db.get(Video, video_id)
    if not video:
        raise HTTPException(status_code=404, detail="Video not found")
    db.refresh(video)
    media_path = video_path(video.filename)
    codec_name, duration_seconds, video_width, video_height = _probe_video_metadata(media_path) if media_path.exists() else (None, None, None, None)
    resolution_label = f"{video_width}×{video_height}" if video_width and video_height else None
    codec_display = {"hevc": "H.265", "h264": "H.264", "h265": "H.265"}.get(codec_name, codec_name) if codec_name else None
    has_720p = bool(video.filename_720p) and video_path(video.filename_720p).exists()
    default_volume = get_setting("default_volume", 1.0)
    return templates.TemplateResponse(
        "video.html",
        {
            "request": request,
            "title": f"{APP_TITLE} — {video.original_name}",
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
    path = video_path(video.filename_720p)
    if not path.exists():
        raise HTTPException(status_code=404, detail="File missing on disk")
    file_size = path.stat().st_size
    range_header = request.headers.get("range")
    media_type = "video/mp4"

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
    path = video_path(video.filename)
    if not path.exists():
        raise HTTPException(status_code=404, detail="File missing on disk")
    file_size = path.stat().st_size
    range_header = request.headers.get("range")
    media_type = video.content_type or "application/octet-stream"

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
    _: UserHTML,
):
    video = db.get(Video, video_id)
    if not video:
        raise HTTPException(status_code=404, detail="Video not found")
    _delete_video_files_with_720p(video)
    db.delete(video)
    db.commit()
    return _redirect("/")


@app.post("/videos/delete-selected")
def delete_selected_videos(
    db: Annotated[Session, Depends(get_db)],
    _: UserHTML,
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


@app.post("/videos/{video_id}/tags")
def set_video_tags(
    video_id: int,
    db: Annotated[Session, Depends(get_db)],
    _: UserHTML,
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
    if return_to:
        return RedirectResponse(url=return_to, status_code=303)
    return _redirect(f"/videos/{video_id}")


@app.get("/upload/tags", response_class=HTMLResponse)
def upload_tags_page(
    request: Request,
    ids: str,
    db: Annotated[Session, Depends(get_db)],
    _: UserHTML,
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
    _: UserHTML,
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
    _: UserHTML,
    description: Annotated[str | None, Form()] = None,
):
    name = name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="Empty name")
    exists = db.scalar(select(func.count()).select_from(Tag).where(Tag.name == name))
    if exists:
        return _redirect("/tags")
    description_clean = description.strip() if description else None
    db.add(Tag(name=name, description=description_clean or None))
    db.commit()
    return _redirect("/tags")


@app.post("/tags/{tag_id}/edit")
def tag_edit(
    tag_id: int,
    db: Annotated[Session, Depends(get_db)],
    _: UserHTML,
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
    _: UserHTML,
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
    _: UserHTML,
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
    _: UserHTML,
):
    folders = list(
        db.execute(
            select(Folder)
            .options(joinedload(Folder.tags), joinedload(Folder.parent))
            .order_by(Folder.parent_id.nullslast(), Folder.name)
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
    _: UserHTML,
    name: Annotated[str, Form()],
    parent_id: Annotated[int | None, Form()] = None,
    match_all: Annotated[str | None, Form()] = None,
    tag_ids: Annotated[list[int] | None, Form()] = None,
):
    name = name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="Empty name")
    if parent_id and not db.get(Folder, parent_id):
        parent_id = None
    from sqlalchemy.exc import IntegrityError
    
    base_name = name
    attempt = 1
    while True:
        try:
            folder = Folder(name=name, parent_id=parent_id, match_all=bool(match_all))
            if tag_ids:
                folder.tags = list(db.scalars(select(Tag).where(Tag.id.in_(tag_ids)).order_by(Tag.name)))
            db.add(folder)
            db.commit()
            break
        except IntegrityError:
            db.rollback()
            attempt += 1
            name = f"{base_name} ({attempt})"
    
    if parent_id:
        return _redirect(f"/folders/{parent_id}")
    return _redirect("/folders")


@app.post("/folders/{folder_id}/delete")
def folder_delete(
    folder_id: int,
    db: Annotated[Session, Depends(get_db)],
    _: UserHTML,
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
    _: UserHTML,
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
    _: UserHTML,
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
    if parent_id and parent_id != folder.id and db.get(Folder, parent_id):
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


def _folder_videos(db: Session, folder: Folder):
    tag_ids = [t.id for t in folder.tags]
    if not tag_ids:
        return []

    if folder.match_all:
        stmt = (
            select(Video)
            .join(Video.tags)
            .where(Tag.id.in_(tag_ids))
            .group_by(Video.id)
            .having(func.count(func.distinct(Tag.id)) == len(tag_ids))
            .options(joinedload(Video.tags))
            .order_by(Video.created_at.desc())
        )
    else:
        stmt = (
            select(Video)
            .join(Video.tags)
            .where(Tag.id.in_(tag_ids))
            .group_by(Video.id)
            .options(joinedload(Video.tags))
            .order_by(Video.created_at.desc())
        )
    return list(db.execute(stmt).unique().scalars())


@app.get("/folders/{folder_id}", response_class=HTMLResponse)
def folder_view(
    request: Request,
    folder_id: int,
    db: Annotated[Session, Depends(get_db)],
    _: UserHTML,
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
            "folder": folder,
            "videos": videos,
            "all_tags": _all_tags(db),
        },
    )


@app.get("/settings", response_class=HTMLResponse)
def settings_page(
    request: Request,
    _: UserHTML,
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
    _: UserHTML,
    transcode_to_720p: Annotated[str | None, Form()] = None,
    default_volume: Annotated[float | None, Form()] = None,
):
    settings = {"transcode_to_720p": transcode_to_720p == "1"}
    if default_volume is not None:
        settings["default_volume"] = max(0.0, min(1.0, default_volume))
    save_settings(settings)
    return RedirectResponse(url="/settings", status_code=303)

