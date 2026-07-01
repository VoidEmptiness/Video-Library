from __future__ import annotations

import asyncio
from functools import lru_cache
import io
import logging
import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
import zipfile
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated
from urllib.parse import quote

from fastapi import BackgroundTasks, Body, Depends, FastAPI, File, Form, HTTPException, Query, Request, Response, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, StreamingResponse
from pathlib import PurePath
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.orm import Session, joinedload

from .database import SessionLocal, engine, get_db
from .models import Base, Folder, Tag, User, Video, VideoTag
from .services.auth import GUEST_USER, SESSION_COOKIE, create_session_token, has_users, is_guest, parse_session_token, session_expiry_dt, verify_credentials
from .services.storage import THUMB_DIR, ensure_dirs, unique_storage_name, video_path

DATA_DIR = Path(os.getenv("DATA_DIR", str(Path(__file__).parent.parent / "_data")))
from .services.thumbnails import generate_thumbnail
from .services.settings import get_setting, load_settings, save_settings
from .services.transcoding import FFMPEG_EXE, FFPROBE_EXE, ffprobe_available, video_height

logger = logging.getLogger(__name__)

APP_TITLE = os.getenv("APP_TITLE", "Video Library")
MAX_FILES_PER_UPLOAD = int(os.getenv("MAX_FILES_PER_UPLOAD", "50"))
THUMBNAIL_CONCURRENCY = int(os.getenv("THUMBNAIL_CONCURRENCY", "4"))
PAGE_SIZE = int(os.getenv("PAGE_SIZE", "32"))
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


def _format_mtime(timestamp: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(timestamp))


templates.env.filters["format_mtime"] = _format_mtime
templates.env.globals["static_version"] = str(int(time.time()))


@asynccontextmanager
async def _lifespan(app: FastAPI):
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        force=True,
    )
    logger.info("Starting %s", APP_TITLE)
    ensure_dirs()
    init_db()
    logger.info("DB initialized")
    yield
    logger.info("Shutting down %s", APP_TITLE)


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
    from .services.auth import ensure_secret_key, migrate_env_users
    ensure_secret_key()
    migrate_env_users()


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


TEXT_EXTENSIONS = {".txt", ".json", ".md", ".csv", ".xml", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".log", ".html", ".css", ".js", ".py", ".sh", ".env", ".conf", ".sql", ".rb", ".php", ".pl", ".lua", ".bat", ".ps1"}


def _is_text_file(name: str) -> bool:
    ext = PurePath(name).suffix.lower()
    return ext in TEXT_EXTENSIONS


def _safe_data_path(subpath: str = "") -> Path:
    root = DATA_DIR.resolve()
    if subpath:
        clean = PurePath(subpath).as_posix().strip("/")
        full = (root / clean).resolve()
    else:
        full = root
    try:
        full.relative_to(root)
    except ValueError:
        raise HTTPException(status_code=403, detail="Path traversal denied")
    return full


def _dir_size(path: Path) -> int:
    total = 0
    for root, _dirs, files in os.walk(path):
        for fn in files:
            try:
                total += (Path(root) / fn).stat().st_size
            except OSError:
                pass
    return total


def _data_files(subpath: str = "") -> list[dict]:
    target = _safe_data_path(subpath)
    if not target.exists():
        return []
    items = []
    for p in sorted(target.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower())):
        st = p.stat()
        if p.is_dir():
            size = _dir_size(p)
        else:
            size = st.st_size
        size_bytes = size
        if size < 1024:
            size_str = f"{size} B"
        elif size < 1024 * 1024:
            size_str = f"{size / 1024:.1f} KB"
        else:
            size_str = f"{size / 1024 / 1024:.1f} MB"
        items.append({
            "name": p.name,
            "size": size_str,
            "size_bytes": size_bytes,
            "mtime": st.st_mtime,
            "is_text": _is_text_file(p.name) if p.is_file() else False,
            "is_dir": p.is_dir(),
            "path": p,
        })
    return items


@app.get("/admin", response_class=HTMLResponse)
def admin_page(
    request: Request,
    _: AdminUserHTML,
    path: str = "",
):
    subpath = path.strip("/")
    files = _data_files(subpath)
    return templates.TemplateResponse(
        "admin.html",
        {
            "request": request,
            "title": f"{APP_TITLE} — Admin / {subpath or '_data'}",
            "files": files,
            "subpath": subpath,
        },
    )


def _safe_file_path(filename: str, subpath: str = "") -> Path:
    root = DATA_DIR.resolve()
    clean_name = PurePath(filename).name
    clean_sub = PurePath(subpath).as_posix().strip("/") if subpath else ""
    full = (root / clean_sub / clean_name).resolve()
    try:
        full.relative_to(root)
    except ValueError:
        raise HTTPException(status_code=403, detail="Path traversal denied")
    return full


@app.get("/admin/download/{filename}")
def admin_download_file(
    filename: str,
    _: AdminUser,
    path: str = "",
):
    fpath = _safe_file_path(filename, path)
    if not fpath.exists() or not fpath.is_file():
        raise HTTPException(status_code=404, detail="File not found")
    return FileResponse(
        path=str(fpath),
        media_type="application/octet-stream",
        filename=filename,
    )


@app.get("/admin/download-all")
def admin_download_all(
    request: Request,
    _: AdminUser,
    path: str = "",
):
    subpath = path.strip("/")
    target = _safe_data_path(subpath)
    if not target.exists():
        return RedirectResponse(url="/admin?error=Папка+не+найдена" + (f"&path={quote(subpath)}" if subpath else ""), status_code=303)

    buf = io.BytesIO()
    name_part = subpath.replace("/", "_") if subpath else "_data"
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for root, _dirs, files in os.walk(target):
            root_path = Path(root)
            for fn in files:
                fp = root_path / fn
                arcname = str(fp.relative_to(target))
                zf.write(str(fp), arcname)
    return Response(
        content=buf.getvalue(),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{name_part}.zip"'},
    )


@app.post("/admin/download-selected")
def admin_download_selected(
    _: AdminUser,
    selected: Annotated[list[str] | None, Form()] = None,
    path: Annotated[str, Form()] = "",
):
    if not selected:
        raise HTTPException(status_code=400, detail="No files selected")

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name in selected:
            fpath = _safe_file_path(name, path)
            if not fpath.exists():
                continue
            if fpath.is_file():
                zf.write(str(fpath), name)
            elif fpath.is_dir():
                for root, _dirs, files in os.walk(fpath):
                    root_path = Path(root)
                    for fn in files:
                        fp = root_path / fn
                        arcname = str(Path(name) / fp.relative_to(fpath))
                        zf.write(str(fp), arcname)
    return Response(
        content=buf.getvalue(),
        media_type="application/zip",
        headers={"Content-Disposition": 'attachment; filename="_data_selected.zip"'},
    )


@app.post("/admin/upload")
async def admin_upload(
    _: AdminUserHTML,
    files: list[UploadFile] = File(...),
    path: Annotated[str, Form()] = "",
):
    subpath = path.strip("/")
    target = _safe_data_path(subpath)
    target.mkdir(parents=True, exist_ok=True)
    for file in files:
        if not file.filename:
            continue
        safe = PurePath(file.filename).name
        dest = target / safe
        with dest.open("wb") as f:
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break
                f.write(chunk)
    qs = f"?path={quote(subpath)}" if subpath else ""
    return RedirectResponse(url=f"/admin{qs}", status_code=303)


@app.get("/admin/download-dir/{dirname}")
def admin_download_dir(
    dirname: str,
    _: AdminUser,
    path: str = "",
):
    fpath = _safe_file_path(dirname, path)
    if not fpath.exists() or not fpath.is_dir():
        raise HTTPException(status_code=404, detail="Directory not found")

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for root, _dirs, files in os.walk(fpath):
            for fn in files:
                fp = Path(root) / fn
                arcname = str(fp.relative_to(fpath))
                zf.write(str(fp), arcname)
    return Response(
        content=buf.getvalue(),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{dirname}.zip"'},
    )


def _rmtree(path: Path):
    if path.is_dir():
        for child in path.iterdir():
            _rmtree(child)
        path.rmdir()
    else:
        path.unlink()


def _delete_video_by_filename(db: Session, name: str) -> None:
    video = db.query(Video).filter(Video.filename == name).first()
    if video is None:
        return
    vpath = video_path(video.filename)
    if vpath.exists():
        vpath.unlink()
    thumb = THUMB_DIR / f"{video.id}.jpg"
    if thumb.exists():
        thumb.unlink()
    db.delete(video)
    db.commit()


@app.post("/admin/delete/{filename}")
def admin_delete_file(
    filename: str,
    _: AdminUserHTML,
    path: Annotated[str, Form()] = "",
    db: Annotated[Session, Depends(get_db)] = None,
):
    fpath = _safe_file_path(filename, path)
    if fpath.exists():
        _rmtree(fpath)
        name = PurePath(filename).name
        _delete_video_by_filename(db, name)
    qs = f"?path={quote(path)}" if path else ""
    return RedirectResponse(url=f"/admin{qs}", status_code=303)


@app.post("/admin/delete-selected")
def admin_delete_selected(
    _: AdminUserHTML,
    selected: Annotated[list[str] | None, Form()] = None,
    path: Annotated[str, Form()] = "",
    db: Annotated[Session, Depends(get_db)] = None,
):
    if selected:
        for name in selected:
            fpath = _safe_file_path(name, path)
            if fpath.exists():
                _rmtree(fpath)
                clean = PurePath(name).name
                _delete_video_by_filename(db, clean)
    qs = f"?path={quote(path)}" if path else ""
    return RedirectResponse(url=f"/admin{qs}", status_code=303)


@app.post("/admin/rename")
def admin_rename_file(
    _: AdminUserHTML,
    old_name: str = Form(...),
    new_name: str = Form(...),
    path: Annotated[str, Form()] = "",
):
    old_safe = PurePath(old_name).name
    new_safe = PurePath(new_name).name
    if not old_safe or not new_safe:
        raise HTTPException(status_code=400, detail="Invalid filename")
    subpath = path.strip("/")
    base = _safe_data_path(subpath)
    src = base / old_safe
    dst = base / new_safe
    if not src.exists():
        raise HTTPException(status_code=404, detail="File not found")
    if dst.exists():
        qs = f"?path={quote(subpath)}&error=" + quote("«" + new_safe + "» уже существует")
        return RedirectResponse(url=f"/admin{qs}", status_code=303)
    src.rename(dst)
    qs = f"?path={quote(subpath)}" if subpath else ""
    return RedirectResponse(url=f"/admin{qs}", status_code=303)


@app.post("/admin/create")
def admin_create_file(
    _: AdminUserHTML,
    filename: str = Form(...),
    path: Annotated[str, Form()] = "",
):
    safe = PurePath(filename).name
    if not safe:
        raise HTTPException(status_code=400, detail="Invalid filename")
    subpath = path.strip("/")
    target = _safe_data_path(subpath)
    target.mkdir(parents=True, exist_ok=True)
    fpath = target / safe
    if fpath.exists():
        kind = "Файл" if PurePath(safe).suffix else "Папка"
        qs = f"?path={quote(subpath)}&error=" + quote(kind + " «" + safe + "» уже существует")
        return RedirectResponse(url=f"/admin{qs}", status_code=303)
    if PurePath(safe).suffix:
        fpath.write_text("", encoding="utf-8")
    else:
        fpath.mkdir(parents=True, exist_ok=False)
    qs = f"?path={quote(subpath)}" if subpath else ""
    return RedirectResponse(url=f"/admin{qs}", status_code=303)


@app.get("/admin/read/{filename}")
def admin_read_file(
    filename: str,
    _: AdminUser,
    path: str = "",
):
    fpath = _safe_file_path(filename, path)
    if not fpath.exists() or not fpath.is_file():
        raise HTTPException(status_code=404, detail="File not found")
    if not _is_text_file(fpath.name):
        raise HTTPException(status_code=400, detail="Not a text file")
    content = fpath.read_text("utf-8", errors="replace")
    return {"name": fpath.name, "content": content}


@app.post("/admin/save/{filename}")
def admin_save_file(
    filename: str,
    _: AdminUserHTML,
    content: str = Form(...),
    path: Annotated[str, Form()] = "",
):
    fpath = _safe_file_path(filename, path)
    if not fpath.exists() or not fpath.is_file():
        raise HTTPException(status_code=404, detail="File not found")
    if not _is_text_file(fpath.name):
        raise HTTPException(status_code=400, detail="Not a text file")
    fpath.write_text(content, encoding="utf-8")
    qs = f"?path={quote(path)}" if path else ""
    return RedirectResponse(url=f"/admin{qs}", status_code=303)


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
    if not has_users():
        return _redirect("/setup")
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


@app.get("/setup", response_class=HTMLResponse)
def setup_page(request: Request):
    if not _wants_auth():
        return _redirect("/")
    if has_users():
        return _redirect("/login")
    return templates.TemplateResponse("setup.html", {"request": request, "title": APP_TITLE})


@app.post("/setup")
def setup_submit(
    request: Request,
    username: Annotated[str, Form()],
    password: Annotated[str, Form()],
    password_confirm: Annotated[str, Form()],
):
    if not _wants_auth():
        return _redirect("/")
    if has_users():
        return _redirect("/login")
    if not username or not password:
        return templates.TemplateResponse(
            "setup.html",
            {"request": request, "title": APP_TITLE, "error": "Заполните все поля"},
            status_code=400,
        )
    if password != password_confirm:
        return templates.TemplateResponse(
            "setup.html",
            {"request": request, "title": APP_TITLE, "error": "Пароли не совпадают"},
            status_code=400,
        )
    if len(password) < 4:
        return templates.TemplateResponse(
            "setup.html",
            {"request": request, "title": APP_TITLE, "error": "Пароль должен быть не менее 4 символов"},
            status_code=400,
        )
    from .services.auth import create_first_admin
    try:
        create_first_admin(username, password)
    except RuntimeError:
        return _redirect("/login")
    return _redirect("/login")


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


def _video_count(db: Session, tag_ids: set[int] | None = None, q: str | None = None, untagged: bool = False) -> int:
    stmt = select(func.count(Video.id))
    if untagged:
        stmt = stmt.where(~Video.tags.any())
    elif tag_ids:
        subq = select(VideoTag.video_id).where(VideoTag.tag_id.in_(tag_ids)).group_by(VideoTag.video_id).subquery()
        stmt = stmt.where(Video.id.in_(subq))
    if q:
        q = q.strip()
        if q:
            stmt = stmt.where(Video.original_name.ilike(f"%{q}%"))
    result = db.execute(stmt).scalar()
    return result or 0


@app.get("/", response_class=HTMLResponse)
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
        created_ids.append(video.id)

    logger.info("Uploaded %d file(s): %s", len(created_ids), [f.filename for f in files if f.filename])

    if thumb_pairs:
        background_tasks.add_task(_generate_all_thumbnails, thumb_pairs)

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


ALL_RENDITION_HEIGHTS = [240, 360, 480, 720, 1080]
HLS_TEMP_DIR = Path(os.getenv("HLS_TEMP_DIR", "/tmp/hls"))

def _valid_heights(video_height: int | None) -> list[int]:
    if not video_height:
        return list(ALL_RENDITION_HEIGHTS)
    return [h for h in ALL_RENDITION_HEIGHTS if h <= video_height]

HLS_BANDWIDTHS = {
    240: 400_000,
    360: 700_000,
    480: 1_400_000,
    720: 3_000_000,
    1080: 6_000_000,
}


def _hls_dir(video_id: int, height: int, start_sec: int = 0) -> Path:
    return HLS_TEMP_DIR / str(video_id) / f"{height}_{start_sec}"


def _hls_playlist_path(video_id: int, height: int, start_sec: int = 0) -> Path:
    return _hls_dir(video_id, height, start_sec) / "playlist.m3u8"


_generation_tasks: dict[tuple[int, int, int], dict] = {}
_generation_lock = threading.Lock()


def _kill_ffmpeg_by_video(video_id: int) -> None:
    pattern = f"{HLS_TEMP_DIR}/{video_id}/"
    try:
        subprocess.run(
            ["pkill", "-f", pattern],
            capture_output=True, timeout=5
        )
    except Exception:
        try:
            for p in Path("/proc").glob("*/cmdline"):
                try:
                    data = p.read_bytes().replace(b"\0", b" ")
                    if b"ffmpeg" in data and pattern.encode() in data:
                        os.kill(int(p.parent.name), 9)
                except (ValueError, OSError):
                    pass
        except Exception:
            pass


def _run_ffmpeg(source_path: Path, video_id: int, height: int, start_sec: int = 0) -> None:
    out_dir = _hls_dir(video_id, height, start_sec)
    out_dir.mkdir(parents=True, exist_ok=True)
    seg_pattern = str(out_dir / "seg_%03d.ts")
    playlist_path = str(out_dir / "playlist.m3u8")

    cmd = [
        str(FFMPEG_EXE), "-y",
    ]
    if start_sec > 0:
        cmd += ["-ss", str(start_sec)]
    cmd += [
        "-i", str(source_path),
        "-map", "0:v:0",
        "-map", "0:a?",
        "-c:v", "libx264",
        "-preset", "ultrafast",
        "-crf", "28",
        "-threads", "0",
        "-g", "30",
        "-keyint_min", "30",
        "-sc_threshold", "0",
        "-vf", f"scale=-2:{height}",
        "-sws_flags", "fast_bilinear",
        "-profile:v", "main",
        "-pix_fmt", "yuv420p",
        "-c:a", "aac",
        "-b:a", "64k",
        "-sn",
        "-f", "hls",
        "-hls_time", "3",
        "-hls_list_size", "0",
        "-hls_playlist_type", "event",
        "-hls_segment_filename", seg_pattern,
        "-hls_flags", "independent_segments+temp_file",
        playlist_path,
    ]

    logger.info("Starting HLS: %s (%dp, start=%ds)", source_path.name, height, start_sec)
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        with _generation_lock:
            key = (video_id, height, start_sec)
            if key not in _generation_tasks or _generation_tasks[key].get("done"):
                proc.kill()
                proc.wait(timeout=5)
                return
            _generation_tasks[key]["proc"] = proc
        proc.wait(timeout=7200)
        ok = proc.returncode == 0 and _hls_playlist_path(video_id, height, start_sec).exists()
        if ok:
            logger.info("HLS done: video %d (%dp, start=%d), %d segments", video_id, height, start_sec,
                        len(list(out_dir.glob("seg_*.ts"))))
    except Exception:
        logger.exception("HLS failed for video %d (%dp, start=%d)", video_id, height, start_sec)
        ok = False

    with _generation_lock:
        key = (video_id, height, start_sec)
        if key in _generation_tasks:
            _generation_tasks[key]["done"] = True
            _generation_tasks[key]["success"] = ok


def _kill_processes(video_id: int, same_start: int | None = None) -> list[tuple[int, int, int]]:
    killed: list[tuple[int, int, int]] = []
    with _generation_lock:
        for key in list(_generation_tasks.keys()):
            if key[0] != video_id:
                continue
            if same_start is not None and key[2] != same_start:
                continue
            task = _generation_tasks[key]
            if task.get("done"):
                continue
            proc = task.get("proc")
            if proc:
                threading.Thread(
                    target=lambda p: (p.kill(), p.wait(timeout=3)),
                    args=(proc,), daemon=True
                ).start()
            task["done"] = True
            killed.append(key)
        for key in killed:
            del _generation_tasks[key]
    for _, h, s in killed:
        d = _hls_dir(video_id, h, s)
        if d.exists():
            threading.Thread(
                target=shutil.rmtree, args=(str(d),), kwargs={"ignore_errors": True}, daemon=True
            ).start()
    _kill_ffmpeg_by_video(video_id)
    return killed


def _ensure_hls_async(source_path: Path, video_id: int, height: int, start_sec: int = 0) -> bool:
    playlist = _hls_playlist_path(video_id, height, start_sec)
    if playlist.exists():
        if _generation_active(video_id, height, start_sec):
            return True
        out_dir = _hls_dir(video_id, height, start_sec)
        if any(out_dir.glob("seg_*.ts")):
            return True
        shutil.rmtree(str(out_dir), ignore_errors=True)

    with _generation_lock:
        for key in list(_generation_tasks.keys()):
            if key[0] != video_id:
                continue
            task = _generation_tasks[key]
            if task.get("done"):
                continue
            proc = task.get("proc")
            if proc:
                threading.Thread(
                    target=lambda p: (p.kill(), p.wait(timeout=3)),
                    args=(proc,), daemon=True
                ).start()
            task["done"] = True
            d = _hls_dir(video_id, key[1], key[2])
            if d.exists():
                threading.Thread(
                    target=shutil.rmtree, args=(str(d),), kwargs={"ignore_errors": True}, daemon=True
                ).start()
        _kill_ffmpeg_by_video(video_id)

        key = (video_id, height, start_sec)
        if key in _generation_tasks:
            task = _generation_tasks[key]
            if not task["done"]:
                return True
            del _generation_tasks[key]
        thread = threading.Thread(
            target=_run_ffmpeg, args=(source_path, video_id, height, start_sec), daemon=True
        )
        _generation_tasks[key] = {"done": False, "success": False, "thread": thread}
        thread.start()
        return True


def _wait_for_playlist(video_id: int, height: int, start_sec: int = 0, timeout: int = 120) -> bool:
    playlist = _hls_playlist_path(video_id, height, start_sec)
    for _ in range(timeout):
        if playlist.exists() and playlist.stat().st_size > 10:
            return True
        time.sleep(1)
    return False


def _generation_active(video_id: int, height: int, start_sec: int = 0) -> bool:
    with _generation_lock:
        key = (video_id, height, start_sec)
        task = _generation_tasks.get(key)
        if task and not task["done"]:
            return True
        return False


def _cleanup_hls(video_id: int) -> None:
    with _generation_lock:
        for key in list(_generation_tasks.keys()):
            if key[0] == video_id:
                task = _generation_tasks[key]
                if not task.get("done"):
                    proc = task.get("proc")
                    if proc:
                        threading.Thread(
                            target=lambda p: (p.kill(), p.wait(timeout=3)),
                            args=(proc,), daemon=True
                        ).start()
                    task["done"] = True
                del _generation_tasks[key]
    d = HLS_TEMP_DIR / str(video_id)
    if d.exists():
        import shutil
        shutil.rmtree(str(d), ignore_errors=True)


@app.get("/videos/{video_id}/renditions")
def list_renditions(
    video_id: int,
    db: Annotated[Session, Depends(get_db)],
    _: User,
):
    video = db.get(Video, video_id)
    if not video:
        raise HTTPException(status_code=404, detail="Video not found")

    result = [
        {
            "height": 0,
            "label": "Source",
            "available": True,
            "content_type": video.content_type or "video/mp4",
        }
    ]
    for h in ALL_RENDITION_HEIGHTS:
        result.append({
            "height": h,
            "label": f"{h}p",
            "available": _hls_playlist_path(video_id, h).exists(),
            "content_type": "application/vnd.apple.mpegurl",
        })
    return result


@app.get("/hls/{video_id}/master.m3u8")
def hls_master_playlist(
    video_id: int,
    db: Annotated[Session, Depends(get_db)],
    _: User,
    start: int = Query(0),
    height: int = Query(None),
):
    video = db.get(Video, video_id)
    if not video:
        raise HTTPException(status_code=404, detail="Video not found")

    source_path = video_path(video.filename)
    if not source_path.exists():
        raise HTTPException(status_code=404, detail="Source missing")

    _, _, _, video_height = _probe_video_metadata(source_path)
    valid_heights = _valid_heights(video_height)
    heights = [height] if height else [h for h in valid_heights if _hls_playlist_path(video_id, h, start).exists()]
    if not heights:
        raise HTTPException(status_code=404, detail="No renditions available")

    lines = ["#EXTM3U"]
    for h in heights:
        if h not in valid_heights:
            continue
        bw = HLS_BANDWIDTHS.get(h, 500_000)
        lines.append(f"#EXT-X-STREAM-INF:BANDWIDTH={bw},RESOLUTION={h*16//9}x{h}")
        lines.append(f"{h}/{start}/playlist.m3u8")

    lines.append("")
    return Response("\n".join(lines), media_type="application/vnd.apple.mpegurl")


@app.get("/hls/{video_id}/{height}/{start}/playlist.m3u8")
def hls_rendition_playlist(
    video_id: int,
    height: int,
    start: int,
    db: Annotated[Session, Depends(get_db)],
    _: User,
):
    video = db.get(Video, video_id)
    if not video:
        raise HTTPException(status_code=404, detail="Video not found")

    source_path = video_path(video.filename)
    if not source_path.exists():
        raise HTTPException(status_code=404, detail="Source missing")

    _, _, _, video_height = _probe_video_metadata(source_path)
    if height not in _valid_heights(video_height):
        raise HTTPException(status_code=400, detail="Invalid height")

    playlist_path = _hls_playlist_path(video_id, height, start)
    if not playlist_path.exists():
        if not _ensure_hls_async(source_path, video_id, height, start):
            raise HTTPException(status_code=503, detail="Failed to start HLS generation")
        if not _wait_for_playlist(video_id, height, start, timeout=120):
            raise HTTPException(status_code=503, detail="HLS generation timeout")

    _, duration_seconds, _, _ = _probe_video_metadata(source_path) if source_path.exists() else (None, None, None, None)
    headers = {
        "X-Original-Duration": str(duration_seconds or 0),
        "X-Start-Sec": str(start),
    }
    return FileResponse(str(playlist_path), media_type="application/vnd.apple.mpegurl", headers=headers)


@app.get("/hls/{video_id}/{height}/{start}/{filename}")
async def hls_segment(
    video_id: int,
    height: int,
    start: int,
    filename: str,
    _: User,
):
    seg_path = _hls_dir(video_id, height, start) / filename
    if seg_path.exists():
        return FileResponse(str(seg_path), media_type="video/MP2T")

    if _generation_active(video_id, height, start):
        for _ in range(120):
            if seg_path.exists():
                return FileResponse(str(seg_path), media_type="video/MP2T")
            await asyncio.sleep(0.5)

    raise HTTPException(status_code=404, detail="Segment not found")


def _kill_hls(video_id: int) -> None:
    with _generation_lock:
        for key in list(_generation_tasks.keys()):
            if key[0] == video_id:
                task = _generation_tasks[key]
                if not task.get("done"):
                    proc = task.get("proc")
                    if proc:
                        threading.Thread(
                            target=lambda p: (p.kill(), p.wait(timeout=3)),
                            args=(proc,), daemon=True
                        ).start()
                    task["done"] = True
                del _generation_tasks[key]
    _kill_ffmpeg_by_video(video_id)


@app.post("/videos/{video_id}/cleanup-hls")
def cleanup_hls(
    video_id: int,
    _: User,
):
    _kill_hls(video_id)
    return {"ok": True}


async def _wait_for_playlist_async(video_id: int, height: int, start_sec: int = 0, timeout: int = 120) -> bool:
    playlist = _hls_playlist_path(video_id, height, start_sec)
    for _ in range(timeout):
        if playlist.exists() and playlist.stat().st_size > 10:
            return True
        await asyncio.sleep(0.5)
    return False


@app.post("/hls/{video_id}/seek")
async def hls_seek(
    video_id: int,
    body: Annotated[dict, Body()],
    db: Annotated[Session, Depends(get_db)],
    _: User,
):
    video = db.get(Video, video_id)
    if not video:
        raise HTTPException(status_code=404, detail="Video not found")

    source_path = video_path(video.filename)
    if not source_path.exists():
        raise HTTPException(status_code=404, detail="Source missing")

    height = int(body["height"])
    old_start = int(body.get("start_sec", 0))
    new_position = float(body["new_position"])

    _, _, _, video_height = _probe_video_metadata(source_path)
    if height not in _valid_heights(video_height):
        raise HTTPException(status_code=400, detail="Invalid height")

    new_start = max(0, int(new_position))

    _kill_processes(video_id)

    if not _ensure_hls_async(source_path, video_id, height, new_start):
        raise HTTPException(status_code=503, detail="Failed to start HLS generation")
    if not await _wait_for_playlist_async(video_id, height, new_start, timeout=120):
        raise HTTPException(status_code=503, detail="HLS generation timeout")

    _, duration_seconds, _, _ = _probe_video_metadata(source_path) if source_path.exists() else (None, None, None, None)

    return {
        "start_sec": new_start,
        "original_duration": duration_seconds or 0,
    }


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
    _cleanup_hls(video_id)
    _delete_video_files(video_path(video.filename), Path(video.thumbnail_path) if video.thumbnail_path else None)
    db.delete(video)
    db.commit()
    logger.info("Deleted video %s (%s)", video_id, video.original_name)
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
        _cleanup_hls(video.id)
        _delete_video_files(video_path(video.filename), Path(video.thumbnail_path) if video.thumbnail_path else None)
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


def _folder_videos_query(db: Session, folder: Folder):
    tag_ids = [t.id for t in folder.tags]
    if not tag_ids:
        return None

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
    return stmt


def _folder_videos(db: Session, folder: Folder, page: int = 1, page_size: int = 30) -> tuple[list[Video], int]:
    stmt = _folder_videos_query(db, folder)
    if stmt is None:
        return [], 0
    count_q = select(func.count()).select_from(stmt.subquery())
    total = db.execute(count_q).scalar() or 0
    total_pages = max(1, (total + page_size - 1) // page_size)
    page = max(1, min(page, total_pages))
    offset = (page - 1) * page_size
    videos = list(db.execute(stmt.offset(offset).limit(page_size)).unique().scalars())
    return videos, total


@app.get("/folders/{folder_id}", response_class=HTMLResponse)
def folder_view(
    request: Request,
    folder_id: int,
    db: Annotated[Session, Depends(get_db)],
    user: UserHTML,
    page: int = 1,
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
    videos, total = _folder_videos(db, folder, page=page, page_size=PAGE_SIZE)
    total_pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    return templates.TemplateResponse(
        "folder_view.html",
        {
            "request": request,
            "title": f"{APP_TITLE} — {folder.name}",
            "user": user,
            "folder": folder,
            "videos": videos,
            "all_tags": _all_tags(db),
            "page": page,
            "total_pages": total_pages,
            "total_videos": total,
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
    tmp = tempfile.NamedTemporaryFile(suffix=".zip", delete=False)
    try:
        shutil.copyfileobj(file.file, tmp)
        tmp.close()

        with zipfile.ZipFile(tmp.name, "r") as zf:
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

    finally:
        try:
            os.unlink(tmp.name)
        except Exception:
            pass
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
            "default_volume": settings.get("default_volume", 1.0),
        },
    )


@app.post("/settings")
def settings_save(
    _: AdminUserHTML,
    default_volume: Annotated[float | None, Form()] = None,
):
    settings = {}
    if default_volume is not None:
        settings["default_volume"] = max(0.0, min(1.0, default_volume))
    save_settings(settings)
    logger.info("Settings saved: %s", settings)
    return RedirectResponse(url="/settings", status_code=303)


@app.post("/admin/reset-thumbnails")
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
    return RedirectResponse(url="/admin", status_code=303)

