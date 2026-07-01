from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from ..config import APP_TITLE, templates
from ..services.settings import get_setting, load_settings, save_settings
from .auth import AdminUserHTML

router = APIRouter()


@router.get("/settings", response_class=HTMLResponse)
def settings_page(
    request: Request,
    _: AdminUserHTML,
):
    settings = load_settings()
    return templates.TemplateResponse(
        "settings.html",
        {
            "request": request,
            "title": f"{APP_TITLE} \u2014 Настройки",
            "default_volume": settings.get("default_volume", 1.0),
        },
    )


@router.post("/settings")
def settings_save(
    _: AdminUserHTML,
    default_volume: Annotated[float | None, Form()] = None,
):
    settings = {}
    if default_volume is not None:
        settings["default_volume"] = max(0.0, min(1.0, default_volume))
    save_settings(settings)
    return RedirectResponse(url="/settings", status_code=303)
