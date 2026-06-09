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
from datetime import date, timedelta
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import distinct, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from bigv_twins.stock_data import resolve_ticker

from bigv_twins.config import BY_SLUG

from . import auth, db
from .blogger_brief import get_latest_briefs
from .digest import get_latest_digest, get_digest_for_date
from .daily_brief import get_global_indices, get_watchlist_quotes
from .db import BloggerDailyBrief, CachedNews, TickerDailyBrief, User, UserWatchlist
from .news_scraper import get_cached_news
from .ticker_brief import (
    _today_str, get_briefs_for_tickers, regenerate_one_ticker_brief,
)

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
    # Find which tickers have an active decision journal for this user (Phase 1B mark)
    from .db import DecisionJournal
    journal_rows = await session.execute(
        select(DecisionJournal.ticker).where(
            DecisionJournal.user_id == user.id,
            DecisionJournal.status == "active",
        ).distinct()
    )
    active_journal_tickers = {row[0] for row in journal_rows.all()}

    for q in watchlist_quotes:
        q["has_journal"] = q["ticker"] in active_journal_tickers
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

    # 根据用户访问方式构建 zhihu 归档站 base URL（同主机 :8000）
    req_host = request.url.hostname or "8.155.174.112"
    req_scheme = request.url.scheme or "http"
    archive_base = f"{req_scheme}://{req_host}:8000"

    # Daily Digest（优先展示；如有日期参数则查指定日期）
    from .db import DailyDigest as DD
    digest_date = request.query_params.get("digest_date")
    if digest_date:
        digest = await get_digest_for_date(digest_date)
    else:
        digest = await get_latest_digest()

    # 可选日期列表（供下拉框）
    async with db._SessionFactory() as ds:
        dd_rows = await ds.execute(
            select(DD.digest_date).order_by(DD.digest_date.desc()).limit(30)
        )
        digest_dates = [r[0] for r in dd_rows]

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
            "digest": digest,
            "digest_dates": digest_dates,
            "archive_base": archive_base,
            "today_str": date.today().strftime("%Y-%m-%d"),
            "max_watchlist": MAX_WATCHLIST,
        },
    )


@router.post("/briefs/regenerate")
async def briefs_regenerate(
    request: Request,
    user: Annotated[User, Depends(auth.require_user)],
):
    """手动重新生成博主日报（删旧 + 重跑）。"""
    from .blogger_brief import generate_briefs_for_day, _yesterday_str
    from .db import BloggerDailyBrief
    day_str = request.query_params.get("date") or _yesterday_str()
    async with db._SessionFactory() as s:
        await s.execute(
            BloggerDailyBrief.__table__.delete().where(
                BloggerDailyBrief.brief_date == day_str
            )
        )
        await s.commit()
    import asyncio
    asyncio.create_task(generate_briefs_for_day(day_str))
    from fastapi.responses import RedirectResponse
    return RedirectResponse("/report?brief_regenerating=1", status_code=303)


@router.get("/api/digest")
async def digest_api(
    request: Request,
    user: Annotated[User, Depends(auth.require_user)],
    date: str = "",
):
    """AJAX: 返回指定日期的 digest_md。"""
    if not date:
        d = await get_latest_digest()
    else:
        d = await get_digest_for_date(date)
    req_host = request.url.hostname or "8.155.174.112"
    req_scheme = request.url.scheme or "http"
    ab = f"{req_scheme}://{req_host}:8000"
    if not d:
        # 无 digest，回退查 brief
        from .db import BloggerDailyBrief
        async with db._SessionFactory() as s:
            br_rows = await s.execute(
                select(BloggerDailyBrief)
                .where(BloggerDailyBrief.brief_date == date)
                .where(BloggerDailyBrief.post_count > 0)
            )
            briefs = list(br_rows.scalars())
        if briefs:
            combined = "\n\n---\n\n".join(
                f"**{BY_SLUG[br.blogger_slug].name if br.blogger_slug in BY_SLUG else br.blogger_slug}**\n\n"
                + (br.brief_md or "").replace("__ARCHIVE__", ab)
                for br in briefs
            )
            return {"status": "brief_fallback", "date": date,
                    "digest_md": f"*该日期暂无 Digest，以下为各博主独立摘要：*\n\n{combined}"}
        return {"status": "not_found", "date": date, "digest_md": ""}
    return {
        "status": "ok", "date": d.digest_date,
        "blogger_count": d.blogger_count,
        "digest_md": (d.digest_md or "").replace("__ARCHIVE__", ab),
    }


@router.post("/digest/regenerate")
async def digest_regenerate(
    request: Request,
    user: Annotated[User, Depends(auth.require_user)],
):
    """手动重新生成 daily digest（删旧 + 重跑）。"""
    from .digest import generate_daily_digest
    from .db import DailyDigest as DD
    day_str = request.query_params.get("date") or _yesterday_str()
    async with db._SessionFactory() as s:
        await s.execute(DD.__table__.delete().where(DD.digest_date == day_str))
        await s.commit()
    import asyncio
    asyncio.create_task(generate_daily_digest(day_str))
    from fastapi.responses import RedirectResponse
    return RedirectResponse("/report?digest_regenerating=1", status_code=303)


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
    # Commit so the brief generator (which opens its own session) can see this row
    await session.commit()

    # Kick off brief generation. Wait up to 8s — most calls finish in 5-10s,
    # so the user usually sees the brief on the redirected page. If it takes
    # longer, the task continues running in background (asyncio.shield) and
    # the row will be filled in by then for the next page load.
    import asyncio
    task = asyncio.create_task(
        regenerate_one_ticker_brief(info.code, info.name, _today_str())
    )
    try:
        await asyncio.wait_for(asyncio.shield(task), timeout=8)
    except asyncio.TimeoutError:
        log.info("watchlist_add: brief gen for %s exceeded 8s, will continue in bg", info.code)
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


@router.get("/history", response_class=HTMLResponse)
async def report_history(
    request: Request,
    user: Annotated[User, Depends(auth.require_user)],
    session: Annotated[AsyncSession, Depends(db.get_session)],
    date: str = "",
):
    """时间机器视图：渲染指定日期那天的博主观点 + 你自选股的 brief + 当日金十事件。

    日期来源：blogger_daily_brief.brief_date distinct list，按降序。
    self-watchlist 是当前的（历史 watchlist 没保留 — 没必要）。
    """
    import json as _json

    # available dates: union of blogger + ticker brief dates
    dates_q = await session.execute(
        select(distinct(BloggerDailyBrief.brief_date))
        .order_by(BloggerDailyBrief.brief_date.desc())
    )
    available_dates = [r[0] for r in dates_q.all()]

    if not available_dates:
        return templates.TemplateResponse(
            request=request,
            name="report/history.html",
            context={"user": user, "date": None, "available_dates": [],
                     "watch_display": [], "blogger_briefs": [], "news": []},
        )

    if not date:
        date = available_dates[0]
    if date not in available_dates:
        raise HTTPException(status_code=404, detail=f"没有 {date} 的 brief 数据")

    # blogger briefs that day
    bb_rows = await session.execute(
        select(BloggerDailyBrief).where(BloggerDailyBrief.brief_date == date)
    )
    blogger_briefs = list(bb_rows.scalars())

    # ticker briefs for user's current watchlist, on that day
    watchlist = await _list_watchlist(session, user.id)
    tickers = [w.ticker for w in watchlist]
    ticker_briefs_map: dict[str, TickerDailyBrief] = {}
    if tickers:
        tb_rows = await session.execute(
            select(TickerDailyBrief)
            .where(TickerDailyBrief.brief_date == date)
            .where(TickerDailyBrief.ticker.in_(tickers))
        )
        ticker_briefs_map = {tb.ticker: tb for tb in tb_rows.scalars()}

    watch_display = []
    for w in watchlist:
        tb = ticker_briefs_map.get(w.ticker)
        if tb is None:
            continue
        try:
            mentions = _json.loads(tb.blogger_mentions or "[]")
        except _json.JSONDecodeError:
            mentions = []
        watch_display.append({
            "ticker": w.ticker, "name": w.name, "market": w.market,
            "verdict": tb.verdict, "verdict_reason": tb.verdict_reason,
            "summary_md": tb.news_summary_md, "mentions": mentions,
        })

    # news from that day (jin10_time is "YYYY-MM-DD HH:MM:SS")
    news_rows = await session.execute(
        select(CachedNews)
        .where(CachedNews.jin10_time.like(f"{date}%"))
        .order_by(CachedNews.jin10_time.desc())
        .limit(20)
    )
    news = list(news_rows.scalars())

    # blogger brief context (b, br, tickers)
    blogger_brief_pairs = []
    for br in blogger_briefs:
        b = BY_SLUG.get(br.blogger_slug)
        if b is None:
            continue
        try:
            tickers_mentioned = _json.loads(br.mentioned_tickers or "[]")
        except _json.JSONDecodeError:
            tickers_mentioned = []
        blogger_brief_pairs.append((b, br, tickers_mentioned))

    return templates.TemplateResponse(
        request=request,
        name="report/history.html",
        context={
            "user": user,
            "date": date,
            "available_dates": available_dates,
            "watch_display": watch_display,
            "blogger_briefs": blogger_brief_pairs,
            "news": news,
        },
    )


@router.post("/ticker/{ticker}/refresh")
async def ticker_refresh(
    ticker: str,
    user: Annotated[User, Depends(auth.require_user)],
    session: Annotated[AsyncSession, Depends(db.get_session)],
):
    """Manually re-generate today's brief for a single ticker. Sync (≤30s)."""
    import asyncio
    # 该用户必须 own 这只 ticker（防止乱刷别人的）
    row = await session.execute(
        select(UserWatchlist)
        .where(UserWatchlist.user_id == user.id)
        .where(UserWatchlist.ticker == ticker)
        .limit(1)
    )
    item = row.scalar_one_or_none()
    if item is None:
        raise HTTPException(status_code=404, detail="该股票不在你的自选里")
    try:
        await asyncio.wait_for(
            regenerate_one_ticker_brief(item.ticker, item.name, _today_str()),
            timeout=30,
        )
    except asyncio.TimeoutError:
        log.warning("ticker_refresh: %s timed out after 30s", ticker)
    return RedirectResponse("/report", status_code=303)
