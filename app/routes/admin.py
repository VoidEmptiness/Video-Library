from __future__ import annotations

import io
import logging
import os
import zipfile
from pathlib import Path
from typing import Annotated
from urllib.parse import quote

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from pathlib import PurePath
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import APP_TITLE, DATA_DIR, templates
from ..database import get_db
from ..models import Video
from ..services.storage import THUMB_DIR, video_path
from ..services.utils import _is_text_file, _redirect, _probe_video_metadata
from ..services.thumbnails import generate_all_thumbnails
from .auth import AdminUser, AdminUserHTML

logger = logging.getLogger(__name__)

router = APIRouter()


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


@router.get("/admin", response_class=HTMLResponse)
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
            "title": f"{APP_TITLE} \u2014 Admin / {subpath or '_data'}",
            "files": files,
            "subpath": subpath,
        },
    )


@router.get("/admin/download/{filename}")
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


@router.get("/admin/download-all")
def admin_download_all(
    request: Request,
    _: AdminUser,
    path: str = "",
):
    subpath = path.strip("/")
    target = _safe_data_path(subpath)
    if not target.exists():
        return RedirectResponse(
            url="/admin?error=Папка+не+найдена" + (f"&path={quote(subpath)}" if subpath else ""),
            status_code=303,
        )

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


@router.post("/admin/download-selected")
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


@router.post("/admin/upload")
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


@router.get("/admin/download-dir/{dirname}")
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


@router.post("/admin/delete/{filename}")
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


@router.post("/admin/delete-selected")
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


@router.post("/admin/rename")
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


@router.post("/admin/create")
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


@router.get("/admin/read/{filename}")
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


@router.post("/admin/save/{filename}")
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


@router.post("/admin/reset-thumbnails")
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
        background_tasks.add_task(generate_all_thumbnails, thumb_pairs)
    return RedirectResponse(url="/admin", status_code=303)
