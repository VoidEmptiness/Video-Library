from __future__ import annotations

import io
import json
import os
import shutil
import tempfile
import zipfile
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from ..database import get_db
from ..models import Tag, Video
from ..services.storage import ensure_dirs, video_path
from ..services.utils import _redirect
from .auth import AdminUser, AdminUserHTML

router = APIRouter()


@router.get("/library/export")
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
                "tags": [
                    {"name": t.name, "description": t.description} for t in video.tags
                ],
            })
        zf.writestr("library.json", json.dumps(meta, ensure_ascii=False, indent=2))
    return Response(
        content=buf.getvalue(),
        media_type="application/zip",
        headers={"Content-Disposition": 'attachment; filename="library.zip"'},
    )


@router.post("/library/import")
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
                raise HTTPException(
                    status_code=400, detail="ZIP must contain library.json"
                )

            try:
                items = json.loads(zf.read("library.json"))
            except json.JSONDecodeError:
                raise HTTPException(
                    status_code=400, detail="Invalid library.json in archive"
                )

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

                existing = db.scalar(
                    select(Video).where(Video.filename == filename)
                )
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
                    content_type=item.get(
                        "content_type", "application/octet-stream"
                    ),
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
                        tag = Tag(
                            name=name, description=tag_item.get("description")
                        )
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
