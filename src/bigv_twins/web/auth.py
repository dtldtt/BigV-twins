"""Password hashing, session helpers, FastAPI auth dependencies."""

from __future__ import annotations

import time
from collections import defaultdict, deque
from typing import Annotated

import bcrypt
from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from . import db
from .db import User, find_user_by_id


# -------- password hashing --------

def hash_password(plain: str) -> str:
    return bcrypt.hashpw(plain.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(plain: str, hashed: str) -> bool:
    if not hashed:
        return False
    try:
        return bcrypt.checkpw(plain.encode("utf-8"), hashed.encode("utf-8"))
    except ValueError:
        return False


# -------- session via starlette SessionMiddleware --------

def login_session(request: Request, user: User) -> None:
    request.session["user_id"] = user.id
    request.session["role"] = user.role
    request.session["username"] = user.username


def logout_session(request: Request) -> None:
    request.session.clear()


# -------- FastAPI dependencies --------

async def current_user(
    request: Request,
    session: Annotated[AsyncSession, Depends(db.get_session)],
) -> User | None:
    uid = request.session.get("user_id")
    if uid is None:
        return None
    return await find_user_by_id(session, uid)


async def require_user(
    request: Request,
    session: Annotated[AsyncSession, Depends(db.get_session)],
) -> User:
    user = await current_user(request, session)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_303_SEE_OTHER,
            detail="login required",
            headers={"Location": "/login"},
        )
    return user


async def require_admin(
    user: Annotated[User, Depends(require_user)],
) -> User:
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="admin only")
    return user


# -------- crude IP rate limit (single-process) --------

class _Bucket:
    """A simple sliding-window counter per key. Not perfect across workers."""

    def __init__(self) -> None:
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    def hit(self, key: str, limit: int, window_s: int) -> bool:
        now = time.monotonic()
        q = self._hits[key]
        cutoff = now - window_s
        while q and q[0] < cutoff:
            q.popleft()
        if len(q) >= limit:
            return False
        q.append(now)
        return True


login_limiter = _Bucket()
register_limiter = _Bucket()


def client_ip(request: Request) -> str:
    return (
        request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
        or (request.client.host if request.client else "?")
    )
