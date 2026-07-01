from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import APP_TITLE, templates
from ..database import get_db
from ..models import Tag
from ..services.utils import _redirect
from .auth import AdminUserHTML

router = APIRouter()


@router.get("/tags", response_class=HTMLResponse)
def tags_page(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    _: AdminUserHTML,
):
    tags = list(db.scalars(select(Tag).order_by(Tag.name)))
    return templates.TemplateResponse(
        "tags.html",
        {"request": request, "title": f"{APP_TITLE} \u2014 Теги", "tags": tags},
    )


@router.post("/tags/create")
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


@router.post("/tags/{tag_id}/edit")
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


@router.post("/tags/{tag_id}/delete")
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


@router.post("/tags/delete-selected")
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
