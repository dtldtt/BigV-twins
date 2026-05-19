"""Invite-code logic.

At most one row in `invites` has `deactivated_at IS NULL`. Generating a new
code automatically deactivates the previous active one. Users registered
with deactivated codes keep their accounts.
"""

from __future__ import annotations

import secrets
from datetime import datetime, timezone

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from .db import Invite


def _now() -> datetime:
    return datetime.now(timezone.utc)


def make_code() -> str:
    """43-char URL-safe random token (≈256 bits of entropy)."""
    return secrets.token_urlsafe(32)


async def get_active(session: AsyncSession) -> Invite | None:
    result = await session.execute(
        select(Invite).where(Invite.deactivated_at.is_(None))
    )
    return result.scalar_one_or_none()


async def rotate(session: AsyncSession, *, created_by_user_id: int | None) -> Invite:
    """Deactivate any current active code, create + return a fresh one."""
    now = _now()
    await session.execute(
        update(Invite)
        .where(Invite.deactivated_at.is_(None))
        .values(deactivated_at=now)
    )
    new = Invite(
        code=make_code(),
        created_at=now,
        created_by_user_id=created_by_user_id,
    )
    session.add(new)
    await session.flush()
    return new


async def validate(session: AsyncSession, code: str) -> Invite | None:
    if not code:
        return None
    result = await session.execute(
        select(Invite).where(Invite.code == code, Invite.deactivated_at.is_(None))
    )
    return result.scalar_one_or_none()


async def list_all(session: AsyncSession) -> list[Invite]:
    result = await session.execute(select(Invite).order_by(Invite.created_at.desc()))
    return list(result.scalars())
