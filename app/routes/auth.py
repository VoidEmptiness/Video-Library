from __future__ import annotations

import os
from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from ..config import APP_TITLE, templates
from ..services.auth import (
    GUEST_USER,
    SESSION_COOKIE,
    create_session_token,
    has_users,
    is_guest,
    parse_session_token,
    session_expiry_dt,
    verify_credentials,
)

router = APIRouter()


def _wants_auth() -> bool:
    return os.getenv("AUTH_ENABLED", "true").lower() in {"1", "true", "yes", "on"}


def _redirect(url: str) -> RedirectResponse:
    return RedirectResponse(url=url, status_code=303)


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


@router.get("/healthz")
def healthz():
    return {"ok": True}


@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    if not _wants_auth():
        return _redirect("/")
    if not has_users():
        return _redirect("/setup")
    return templates.TemplateResponse("login.html", {"request": request, "title": APP_TITLE})


@router.post("/login")
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


@router.post("/logout")
def logout():
    resp = _redirect("/login")
    resp.delete_cookie(SESSION_COOKIE)
    return resp


@router.get("/setup", response_class=HTMLResponse)
def setup_page(request: Request):
    if not _wants_auth():
        return _redirect("/")
    if has_users():
        return _redirect("/login")
    return templates.TemplateResponse("setup.html", {"request": request, "title": APP_TITLE})


@router.post("/setup")
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
    from ..services.auth import create_first_admin

    try:
        create_first_admin(username, password)
    except RuntimeError:
        return _redirect("/login")
    return _redirect("/login")


@router.post("/guest-login")
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
