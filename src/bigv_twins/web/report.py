"""投资日报 (/report) — per-user daily morning brief.

P1 implementation: just the watchlist management UI. Subsequent phases bolt on:
  P2 — 全球行情 + 自选股行情条
  P3 — 金十重要事件
  P4 — 博主每日总结
  P5 — 自选股相关动态
  P6 — nav 入口 + README

Single GET /report endpoint that progressively renders more sections as
each phase lands.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from bigv_twins.stock_data import resolve_ticker

from bigv_twins.config import BY_SLUG

from . import auth, db
from .blogger_brief import get_latest_briefs
from .daily_brief import get_global_indices, get_watchlist_quotes
from .db import User, UserWatchlist
from .news_scraper import get_cached_news
from .ticker_brief import get_briefs_for_tickers

log = logging.getLogger("bigv_twins.web.report")
router = APIRouter(prefix="/report")
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


MAX_WATCHLIST = 30


# ------------------------------------------------------------ helpers


async def _list_watchlist(session: AsyncSession, user_id: int) -> list[UserWatchlist]:
    rows = await session.execute(
        select(UserWatchlist)
        .where(UserWatchlist.user_id == user_id)
        .order_by(UserWatchlist.sort_order, UserWatchlist.id)
    )
    return list(rows.scalars())


# ------------------------------------------------------------ routes


@router.get("", response_class=HTMLResponse)
async def report_index(
    request: Request,
    user: Annotated[User, Depends(auth.require_user)],
    session: Annotated[AsyncSession, Depends(db.get_session)],
):
    """Main /report page.

    Wires P1 (watchlist) + P2 (live quotes). httpx calls are sync, so wrap
    in to_thread to avoid blocking the asyncio event loop while waiting for
    Tencent (~50-200ms typical).
    """
    import asyncio
    import json as _json
    watchlist = await _list_watchlist(session, user.id)
    indices = await asyncio.to_thread(get_global_indices)
    watchlist_quotes = await asyncio.to_thread(get_watchlist_quotes, watchlist)
    news = await get_cached_news(limit=10)
    briefs = await get_latest_briefs()
    # P5: attach per-ticker daily brief (cross-ref blogger mentions + news verdict)
    ticker_briefs = await get_briefs_for_tickers([w.ticker for w in watchlist])
    for q in watchlist_quotes:
        tb = ticker_briefs.get(q["ticker"])
        if tb:
            try:
                q["mentions"] = _json.loads(tb.blogger_mentions or "[]")
            except _json.JSONDecodeError:
                q["mentions"] = []
            q["summary_md"] = tb.news_summary_md
            q["verdict"] = tb.verdict
            q["verdict_reason"] = tb.verdict_reason
            q["brief_date"] = tb.brief_date
        else:
            q["mentions"] = []
            q["summary_md"] = ""
            q["verdict"] = ""
            q["verdict_reason"] = ""
    # blogger_briefs context
    blogger_brief_pairs = []
    for br in briefs:
        b = BY_SLUG.get(br.blogger_slug)
        if b is None:
            continue
        try:
            tickers = _json.loads(br.mentioned_tickers or "[]")
        except _json.JSONDecodeError:
            tickers = []
        blogger_brief_pairs.append((b, br, tickers))
    return templates.TemplateResponse(
        request=request,
        name="report/index.html",
        context={
            "user": user,
            "watchlist": watchlist,
            "watchlist_quotes": watchlist_quotes,
            "indices": indices,
            "news": news,
            "blogger_briefs": blogger_brief_pairs,
            "max_watchlist": MAX_WATCHLIST,
        },
    )


@router.post("/watchlist/add")
async def watchlist_add(
    user: Annotated[User, Depends(auth.require_user)],
    session: Annotated[AsyncSession, Depends(db.get_session)],
    query: Annotated[str, Form()],
    note: Annotated[str, Form()] = "",
):
    """Add a stock to the user's watchlist. `query` can be a name or ticker code.

    We resolve to canonical (code, name, market) via stock_data.resolve_ticker,
    then insert. Duplicates are blocked by the UNIQUE(user_id, ticker) constraint.
    Over MAX_WATCHLIST → 400.
    """
    q = query.strip()
    if not q:
        raise HTTPException(status_code=400, detail="请输入股票名或代码")

    # Resolve to canonical ticker
    info = resolve_ticker(q)
    if info is None:
        raise HTTPException(
            status_code=400,
            detail=f"无法识别股票：{q!r}（支持 A 股代码、港股代码、常见股票名）",
        )

    # Check cap
    current = await _list_watchlist(session, user.id)
    if len(current) >= MAX_WATCHLIST:
        raise HTTPException(
            status_code=400,
            detail=f"自选股已达上限 {MAX_WATCHLIST} 只，先删一些再加",
        )

    # Insert
    item = UserWatchlist(
        user_id=user.id,
        ticker=info.code,
        name=info.name,
        market=info.market,
        note=note.strip()[:200] or None,
        sort_order=len(current),  # append at end
    )
    session.add(item)
    try:
        await session.flush()
    except IntegrityError:
        # UNIQUE constraint hit — already in watchlist
        await session.rollback()
        raise HTTPException(
            status_code=400,
            detail=f"{info.name} ({info.code}) 已经在你的自选里了",
        )
    return RedirectResponse("/report", status_code=303)


@router.post("/watchlist/{wid}/delete")
async def watchlist_delete(
    wid: int,
    user: Annotated[User, Depends(auth.require_user)],
    session: Annotated[AsyncSession, Depends(db.get_session)],
):
    item = await session.get(UserWatchlist, wid)
    if item is None or item.user_id != user.id:
        raise HTTPException(status_code=404)
    await session.delete(item)
    return RedirectResponse("/report", status_code=303)


@router.post("/watchlist/{wid}/note")
async def watchlist_note(
    wid: int,
    user: Annotated[User, Depends(auth.require_user)],
    session: Annotated[AsyncSession, Depends(db.get_session)],
    note: Annotated[str, Form()] = "",
):
    """Update note for a watchlist item."""
    item = await session.get(UserWatchlist, wid)
    if item is None or item.user_id != user.id:
        raise HTTPException(status_code=404)
    item.note = note.strip()[:200] or None
    return RedirectResponse("/report", status_code=303)
