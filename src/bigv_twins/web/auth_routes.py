"""Login / register / logout routes."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession

from . import auth, db, invites
from .db import User, find_user_by_username

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


@router.get("/login", response_class=HTMLResponse)
async def login_page(
    request: Request,
    user: Annotated[User | None, Depends(auth.current_user)],
):
    if user:
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(
        request=request,
        name="login.html",
        context={"error": None, "username": ""},
    )


@router.post("/login")
async def login_submit(
    request: Request,
    session: Annotated[AsyncSession, Depends(db.get_session)],
    username: Annotated[str, Form()],
    password: Annotated[str, Form()],
):
    ip = auth.client_ip(request)
    if not auth.login_limiter.hit(f"login:{ip}", limit=5, window_s=60):
        return templates.TemplateResponse(
            request=request,
            name="login.html",
            context={"error": "尝试过于频繁，1 分钟后再试。", "username": username},
            status_code=429,
        )

    user = await find_user_by_username(session, username.strip())
    if user is None or not auth.verify_password(password, user.password_hash):
        return templates.TemplateResponse(
            request=request,
            name="login.html",
            context={"error": "用户名或密码错误。", "username": username},
            status_code=401,
        )

    user.last_login_at = datetime.now(timezone.utc)
    auth.login_session(request, user)
    return RedirectResponse("/", status_code=303)


@router.get("/register", response_class=HTMLResponse)
async def register_page(
    request: Request,
    user: Annotated[User | None, Depends(auth.current_user)],
):
    if user:
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(
        request=request,
        name="register.html",
        context={"error": None, "username": "", "invite": ""},
    )


@router.post("/register")
async def register_submit(
    request: Request,
    session: Annotated[AsyncSession, Depends(db.get_session)],
    username: Annotated[str, Form()],
    password: Annotated[str, Form()],
    invite: Annotated[str, Form()],
):
    ip = auth.client_ip(request)
    if not auth.register_limiter.hit(f"register:{ip}", limit=10, window_s=3600):
        return templates.TemplateResponse(
            request=request,
            name="register.html",
            context={"error": "尝试过于频繁，请稍后再试。", "username": username, "invite": ""},
            status_code=429,
        )

    username = username.strip()
    invite_code = invite.strip()

    if not (3 <= len(username) <= 32) or not all(c.isalnum() or c in "-_." for c in username):
        return templates.TemplateResponse(
            request=request, name="register.html",
            context={"error": "用户名 3-32 字符，仅字母/数字/-_.", "username": username, "invite": invite_code},
            status_code=400,
        )
    if len(password) < 8:
        return templates.TemplateResponse(
            request=request, name="register.html",
            context={"error": "密码至少 8 位。", "username": username, "invite": invite_code},
            status_code=400,
        )

    inv = await invites.validate(session, invite_code)
    if inv is None:
        return templates.TemplateResponse(
            request=request, name="register.html",
            context={"error": "邀请码无效或已失效。", "username": username, "invite": ""},
            status_code=400,
        )

    if await find_user_by_username(session, username):
        return templates.TemplateResponse(
            request=request, name="register.html",
            context={"error": "用户名已存在。", "username": username, "invite": invite_code},
            status_code=400,
        )

    user = User(
        username=username,
        password_hash=auth.hash_password(password),
        role="user",
        invite_id=inv.id,
    )
    session.add(user)
    await session.flush()
    auth.login_session(request, user)
    return RedirectResponse("/", status_code=303)


@router.post("/logout")
async def logout(request: Request):
    auth.logout_session(request)
    return RedirectResponse("/login", status_code=303)
