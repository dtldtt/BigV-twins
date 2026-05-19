"""Blogger about pages: GET /about/{slug}

Shows: name + tagline + zhihu link + follower stats (from zhihu.db.authors),
full persona (rendered as markdown), top 5 most-upvoted posts, last 10 posts.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bigv_twins.config import BY_SLUG, settings

from . import auth, db
from .db import BloggerOverride, User

router = APIRouter(prefix="/about")
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


def _open_zhihu_ro() -> sqlite3.Connection:
    uri = f"file:{settings.zhihu_db_path}?mode=ro&immutable=1"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


@router.get("/{slug}", response_class=HTMLResponse)
async def about_page(
    request: Request,
    slug: str,
    user: Annotated[User, Depends(auth.require_user)],
    session: Annotated[AsyncSession, Depends(db.get_session)],
):
    if slug not in BY_SLUG:
        raise HTTPException(status_code=404, detail="unknown blogger")

    # Visibility check (hidden bloggers → 404 even on about)
    hidden = await session.execute(
        select(BloggerOverride.slug).where(
            BloggerOverride.slug == slug, BloggerOverride.hidden.is_(True)
        )
    )
    if hidden.scalar_one_or_none() is not None:
        raise HTTPException(status_code=404, detail="blogger hidden")

    blogger = BY_SLUG[slug]

    # Live stats from zhihu.db
    src = _open_zhihu_ro()
    try:
        author_row = src.execute(
            "SELECT name, headline, follower_count, answer_count, article_count, "
            "pin_count, last_crawled_at FROM authors WHERE id = ?",
            (blogger.author_id,),
        ).fetchone()
        top_posts = src.execute(
            "SELECT zhihu_id, content_type, title, voteup_count, url, created_time "
            "FROM contents WHERE author_id = ? AND content IS NOT NULL "
            "ORDER BY voteup_count DESC LIMIT 5",
            (blogger.author_id,),
        ).fetchall()
        recent_posts = src.execute(
            "SELECT zhihu_id, content_type, title, voteup_count, url, created_time "
            "FROM contents WHERE author_id = ? AND content IS NOT NULL "
            "ORDER BY created_time DESC LIMIT 10",
            (blogger.author_id,),
        ).fetchall()
    finally:
        src.close()

    # Persona content (raw markdown — rendered by chat.js's marked on the page)
    persona_path = settings.persona_path(slug)
    persona_text = persona_path.read_text(encoding="utf-8") if persona_path.exists() else ""

    return templates.TemplateResponse(
        request=request,
        name="about/blogger.html",
        context={
            "user": user,
            "blogger": blogger,
            "author": dict(author_row) if author_row else {},
            "top_posts": [dict(r) for r in top_posts],
            "recent_posts": [dict(r) for r in recent_posts],
            "persona_text": persona_text,
        },
    )
