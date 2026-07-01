from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session, joinedload

from ..config import APP_TITLE, PAGE_SIZE, templates
from ..database import get_db
from ..models import Folder, Tag, Video, VideoTag
from ..services.utils import _all_tags, _redirect
from .auth import AdminUserHTML, UserHTML

router = APIRouter()


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
        matching_ids_q = matching_ids_q.having(
            func.count(func.distinct(VideoTag.tag_id)) == len(tag_ids)
        )

    stmt = (
        select(Video)
        .where(Video.id.in_(matching_ids_q.subquery()))
        .options(joinedload(Video.tags))
        .order_by(Video.created_at.desc())
    )
    return stmt


def _folder_videos(
    db: Session, folder: Folder, page: int = 1, page_size: int = 30
) -> tuple[list[Video], int]:
    stmt = _folder_videos_query(db, folder)
    if stmt is None:
        return [], 0
    count_q = select(func.count()).select_from(stmt.subquery())
    total = db.execute(count_q).scalar() or 0
    total_pages = max(1, (total + page_size - 1) // page_size)
    page = max(1, min(page, total_pages))
    offset = (page - 1) * page_size
    videos = list(
        db.execute(stmt.offset(offset).limit(page_size)).unique().scalars()
    )
    return videos, total


@router.get("/folders", response_class=HTMLResponse)
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
            "title": f"{APP_TITLE} \u2014 Папки",
            "folders": folders,
            "all_tags": _all_tags(db),
            "all_folders": folders,
        },
    )


@router.post("/folders/create")
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
        folder.tags = list(
            db.scalars(select(Tag).where(Tag.id.in_(tag_ids)).order_by(Tag.name))
        )
    db.add(folder)
    db.commit()

    if parent_id:
        return _redirect(f"/folders/{parent_id}")
    return _redirect("/folders")


@router.post("/folders/{folder_id}/delete")
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


@router.post("/folders/delete-selected")
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


@router.post("/folders/{folder_id}/edit")
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
    if (
        parent_id is not None
        and parent_id > 0
        and parent_id != folder.id
        and db.get(Folder, parent_id)
        and not _would_create_cycle(db, folder.id, parent_id)
    ):
        folder.parent_id = parent_id
    else:
        folder.parent_id = None
    if tag_ids:
        folder.tags = list(
            db.scalars(select(Tag).where(Tag.id.in_(tag_ids)).order_by(Tag.name))
        )
    else:
        folder.tags = []
    db.add(folder)
    db.commit()
    return _redirect("/folders")


@router.get("/folders/{folder_id}", response_class=HTMLResponse)
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
            "title": f"{APP_TITLE} \u2014 {folder.name}",
            "user": user,
            "folder": folder,
            "videos": videos,
            "all_tags": _all_tags(db),
            "page": page,
            "total_pages": total_pages,
            "total_videos": total,
        },
    )
