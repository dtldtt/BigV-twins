"""SQLAlchemy 2.0 async models + engine for the web chat app.

Single sqlite file at `settings.chats_db_path` (project_root/chats.db).
Tables: users, invites, blogger_overrides, conversations, messages.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import AsyncIterator

from sqlalchemy import (
    Boolean, DateTime, ForeignKey, Integer, String, Text, select,
)
from sqlalchemy.ext.asyncio import (
    AsyncAttrs, AsyncSession, async_sessionmaker, create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from bigv_twins.config import settings


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Base(AsyncAttrs, DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False)  # 'admin' | 'user'
    invite_id: Mapped[int | None] = mapped_column(ForeignKey("invites.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now, nullable=False)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    conversations: Mapped[list["Conversation"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )


class Invite(Base):
    __tablename__ = "invites"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now, nullable=False)
    created_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    deactivated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class BloggerOverride(Base):
    __tablename__ = "blogger_overrides"

    slug: Mapped[str] = mapped_column(String(64), primary_key=True)
    hidden: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    hidden_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    hidden_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)


class Conversation(Base):
    __tablename__ = "conversations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    blogger_slug: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(120), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=_now, onupdate=_now, nullable=False, index=True
    )

    user: Mapped[User] = relationship(back_populates="conversations")
    messages: Mapped[list["Message"]] = relationship(
        back_populates="conversation",
        cascade="all, delete-orphan",
        order_by="Message.created_at",
    )


class Message(Base):
    __tablename__ = "messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    conversation_id: Mapped[int] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    role: Mapped[str] = mapped_column(String(16), nullable=False)  # 'user' | 'assistant'
    content: Mapped[str] = mapped_column(Text, nullable=False)
    token_usage_in: Mapped[int | None] = mapped_column(Integer, nullable=True)
    token_usage_out: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now, nullable=False)

    conversation: Mapped[Conversation] = relationship(back_populates="messages")


# ============================================================================
# Multi-blogger conversations ("问所有人" 多人横向对比模式)
#
# Completely independent from single-blogger Conversation/Message above.
# Deletion cascades within multi_* tables; does NOT touch individual chats.
# ============================================================================


class MultiConversation(Base):
    __tablename__ = "multi_conversations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    title: Mapped[str] = mapped_column(String(120), nullable=False)
    # JSON-encoded list of blogger slugs participating in this multi-conv.
    # e.g. '["mr-dang","eyu","buffett","advisor"]'
    participant_slugs: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=_now, onupdate=_now, nullable=False, index=True
    )

    messages: Mapped[list["MultiMessage"]] = relationship(
        back_populates="conversation",
        cascade="all, delete-orphan",
        order_by="MultiMessage.created_at",
    )


class MultiMessage(Base):
    """A single 'turn' marker — either the user's question or the rollup summary.

    Per-blogger answers live in `MultiSubResponse` and FK back to the role='user'
    message that triggered them.
    """
    __tablename__ = "multi_messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    conversation_id: Mapped[int] = mapped_column(
        ForeignKey("multi_conversations.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    role: Mapped[str] = mapped_column(String(16), nullable=False)  # 'user' | 'summary'
    content: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now, nullable=False)

    conversation: Mapped[MultiConversation] = relationship(back_populates="messages")
    sub_responses: Mapped[list["MultiSubResponse"]] = relationship(
        back_populates="user_message",
        cascade="all, delete-orphan",
        order_by="MultiSubResponse.created_at",
    )


class MultiSubResponse(Base):
    """One blogger's answer to one user-message in a multi-conversation."""
    __tablename__ = "multi_sub_responses"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_message_id: Mapped[int] = mapped_column(
        ForeignKey("multi_messages.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    blogger_slug: Mapped[str] = mapped_column(String(64), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    # 'running' (mid-stream, written as 'done' once SSE completes), 'done', 'error'
    status: Mapped[str] = mapped_column(String(16), default="done", nullable=False)
    error_msg: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now, nullable=False)

    user_message: Mapped[MultiMessage] = relationship(back_populates="sub_responses")


_engine = create_async_engine(
    f"sqlite+aiosqlite:///{settings.chats_db_path}",
    echo=False,
    future=True,
)

_SessionFactory = async_sessionmaker(_engine, expire_on_commit=False, class_=AsyncSession)


async def init_db() -> None:
    """Create tables if missing. Idempotent; called at app startup."""
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.exec_driver_sql("PRAGMA foreign_keys = ON")


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency. Yields a session, commits on success, rolls back on error."""
    async with _SessionFactory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def find_user_by_id(session: AsyncSession, user_id: int) -> User | None:
    return await session.get(User, user_id)


async def find_user_by_username(session: AsyncSession, username: str) -> User | None:
    result = await session.execute(select(User).where(User.username == username))
    return result.scalar_one_or_none()
