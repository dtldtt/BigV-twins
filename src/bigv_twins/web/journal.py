"""投资决策日志 — CRUD + 异步环境快照采集。"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import date, timedelta
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bigv_twins.config import BY_SLUG

from . import auth, db
from .db import BloggerDailyBrief, DecisionJournal, User
from .daily_brief import get_watchlist_quotes

log = logging.getLogger("bigv_twins.web.journal")
router = APIRouter(prefix="/journal")

PKG_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(PKG_DIR / "templates"))


class _FakeW:
    def __init__(self, ticker):
        self.ticker = ticker
        self.name = ticker
        self.market = "A"
        self.note = ""
        self.id = 0


def _collect_stock_snapshot(ticker: str) -> dict:
    """Sync: fetch real-time quote + fundamentals from Tencent."""
    quotes = get_watchlist_quotes([_FakeW(ticker)])
    q = quotes[0] if quotes else {}
    return {
        "price": q.get("current"),
        "change_pct": q.get("change_pct"),
        "pe": q.get("pe"),
        "pb": q.get("pb"),
        "market_cap": q.get("market_cap"),
    }


async def _collect_blogger_opinions(ticker: str) -> list[dict]:
    """Async: find recent blogger mentions of this ticker."""
    fourteen_days_ago = (date.today() - timedelta(days=14)).strftime("%Y-%m-%d")
    async with db._SessionFactory() as session:
        rows = await session.execute(
            select(BloggerDailyBrief).where(
                BloggerDailyBrief.brief_date >= fourteen_days_ago
            ).order_by(BloggerDailyBrief.brief_date.desc())
        )
        opinions = []
        for br in rows.scalars():
            try:
                mentioned = json.loads(br.mentioned_tickers or "[]")
            except json.JSONDecodeError:
                continue
            if ticker in mentioned:
                blogger = BY_SLUG.get(br.blogger_slug)
                opinions.append({
                    "slug": br.blogger_slug,
                    "name": blogger.name if blogger else br.blogger_slug,
                    "date": br.brief_date,
                    "excerpt": (br.brief_md or "")[:150],
                })
        return opinions[:5]


async def _fill_snapshot(journal_id: int):
    """Background task: fill stock_snapshot + blogger_opinions after creation."""
    import asyncio as _aio
    await _aio.sleep(0.5)  # slight delay to let transaction commit
    async with db._SessionFactory() as session:
        journal = await session.get(DecisionJournal, journal_id)
        if not journal:
            return
        try:
            loop = asyncio.get_running_loop()
            snapshot = await loop.run_in_executor(None, _collect_stock_snapshot, journal.ticker)
            journal.stock_snapshot = json.dumps(snapshot, ensure_ascii=False)
        except Exception as e:
            log.warning("stock snapshot failed for %s: %s", journal.ticker, e)

        try:
            opinions = await _collect_blogger_opinions(journal.ticker)
            journal.blogger_opinions = json.dumps(opinions, ensure_ascii=False)
        except Exception as e:
            log.warning("blogger opinions failed for %s: %s", journal.ticker, e)

        # Set first review date = 7 days from now
        journal.next_review_at = (date.today() + timedelta(days=7)).strftime("%Y-%m-%d")
        await session.commit()
        log.info("snapshot filled for journal #%d (%s)", journal_id, journal.ticker)


@router.get("", response_class=HTMLResponse)
async def journal_list(
    request: Request,
    user: Annotated[User, Depends(auth.require_user)],
    session: Annotated[AsyncSession, Depends(db.get_session)],
):
    status_filter = request.query_params.get("status", "active")
    q = select(DecisionJournal).where(DecisionJournal.user_id == user.id)
    if status_filter != "all":
        q = q.where(DecisionJournal.status == status_filter)
    q = q.order_by(DecisionJournal.created_at.desc())
    rows = await session.execute(q)
    journals = list(rows.scalars())

    # Fetch current prices for active journals (batch)
    active_tickers = list({j.ticker for j in journals if j.status == "active"})
    price_map = {}
    if active_tickers:
        import asyncio
        quotes = await asyncio.to_thread(
            get_watchlist_quotes, [_FakeW(t) for t in active_tickers]
        )
        for qq in quotes:
            if qq.get("ok"):
                price_map[qq["ticker"]] = qq["current"]

    return templates.TemplateResponse(
        request=request,
        name="journal/list.html",
        context={
            "user": user,
            "journals": journals,
            "price_map": price_map,
            "status_filter": status_filter,
        },
    )


@router.get("/new", response_class=HTMLResponse)
async def journal_create_form(
    request: Request,
    user: Annotated[User, Depends(auth.require_user)],
):
    ticker = request.query_params.get("ticker", "")
    name = request.query_params.get("name", "")
    return templates.TemplateResponse(
        request=request,
        name="journal/create.html",
        context={"user": user, "prefill_ticker": ticker, "prefill_name": name},
    )


@router.post("/new")
async def journal_create(
    request: Request,
    background_tasks: BackgroundTasks,
    user: Annotated[User, Depends(auth.require_user)],
    session: Annotated[AsyncSession, Depends(db.get_session)],
    ticker: str = Form(...),
    ticker_name: str = Form(...),
    action: str = Form(...),
    action_detail: str = Form(""),
    price_at_decision: float = Form(None),
    reasoning: str = Form(...),
    hold_conditions: str = Form(""),
    exit_signals: str = Form(""),
    target_price: float = Form(None),
    stop_loss_price: float = Form(None),
    expected_hold_period: str = Form(""),
    if_drop_10pct: str = Form(""),
    position_pct: float = Form(None),
):
    journal = DecisionJournal(
        user_id=user.id,
        ticker=ticker.strip(),
        ticker_name=ticker_name.strip(),
        action=action,
        action_detail=action_detail or None,
        price_at_decision=price_at_decision,
        position_pct=position_pct,
        reasoning=reasoning,
        hold_conditions=hold_conditions or None,
        exit_signals=exit_signals or None,
        target_price=target_price,
        stop_loss_price=stop_loss_price,
        expected_hold_period=expected_hold_period or None,
        if_drop_10pct=if_drop_10pct or None,
        status="active",
    )
    session.add(journal)
    await session.flush()
    journal_id = journal.id
    await session.commit()

    # Async: fill snapshot in background
    background_tasks.add_task(_fill_snapshot, journal_id)

    return RedirectResponse(f"/journal/{journal_id}", status_code=303)


@router.get("/{jid}", response_class=HTMLResponse)
async def journal_detail(
    request: Request,
    jid: int,
    user: Annotated[User, Depends(auth.require_user)],
    session: Annotated[AsyncSession, Depends(db.get_session)],
):
    journal = await session.get(DecisionJournal, jid)
    if not journal or journal.user_id != user.id:
        raise HTTPException(status_code=404)

    # Current price
    import asyncio
    quotes = await asyncio.to_thread(get_watchlist_quotes, [_FakeW(journal.ticker)])
    current_price = quotes[0].get("current") if quotes else None
    pnl_pct = None
    if current_price and journal.price_at_decision:
        pnl_pct = (current_price - journal.price_at_decision) / journal.price_at_decision * 100

    # Parse JSON fields
    snapshot = json.loads(journal.stock_snapshot) if journal.stock_snapshot else None
    opinions = json.loads(journal.blogger_opinions) if journal.blogger_opinions else []

    return templates.TemplateResponse(
        request=request,
        name="journal/detail.html",
        context={
            "user": user,
            "j": journal,
            "current_price": current_price,
            "pnl_pct": pnl_pct,
            "snapshot": snapshot,
            "opinions": opinions,
        },
    )


@router.post("/{jid}/close")
async def journal_close(
    jid: int,
    user: Annotated[User, Depends(auth.require_user)],
    session: Annotated[AsyncSession, Depends(db.get_session)],
    closed_price: float = Form(...),
    closed_reason: str = Form(""),
):
    journal = await session.get(DecisionJournal, jid)
    if not journal or journal.user_id != user.id:
        raise HTTPException(status_code=404)
    from datetime import datetime
    journal.status = "closed"
    journal.closed_at = datetime.now()
    journal.closed_price = closed_price
    journal.closed_reason = closed_reason or None
    await session.commit()
    return RedirectResponse(f"/journal/{jid}", status_code=303)
