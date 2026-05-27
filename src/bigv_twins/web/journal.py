"""投资决策日志 v3 — 完整持仓计算 + 自动加自选。"""

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
from .db import BloggerDailyBrief, DecisionJournal, InvestmentNote, User, UserWatchlist
from bigv_twins.stock_data import resolve_ticker
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
    await asyncio.sleep(0.5)
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
        journal.next_review_at = (date.today() + timedelta(days=7)).strftime("%Y-%m-%d")
        await session.commit()


async def _auto_add_watchlist(user_id: int, ticker: str, ticker_name: str):
    """If ticker not in user's watchlist, add it."""
    async with db._SessionFactory() as session:
        existing = await session.execute(
            select(UserWatchlist).where(
                UserWatchlist.user_id == user_id,
                UserWatchlist.ticker == ticker,
            )
        )
        if existing.scalar_one_or_none():
            return
        market = "A"
        if len(ticker) == 5:
            market = "HK"
        wl = UserWatchlist(
            user_id=user_id, ticker=ticker, name=ticker_name, market=market, note=""
        )
        session.add(wl)
        try:
            await session.commit()
            log.info("auto-added %s to watchlist for user %d", ticker, user_id)
        except Exception:
            pass  # duplicate or other constraint


def _build_portfolio(journals: list, price_map: dict, quote_map: dict,
                     total_capital: float | None) -> list[dict]:
    """Build portfolio from all journals. Calculates cost basis and avg buy price."""
    # Group journals by ticker, ordered by time
    from collections import defaultdict
    ticker_ops: dict[str, list] = defaultdict(list)
    ticker_name_map: dict[str, str] = {}

    for j in journals:
        if j.status != "active":
            continue
        ticker_ops[j.ticker].append(j)
        ticker_name_map[j.ticker] = j.ticker_name

    portfolio = []
    for ticker, ops in ticker_ops.items():
        ops.sort(key=lambda x: x.created_at or "")

        total_shares = 0
        total_cost = 0.0  # total money spent on buys (for cost basis)
        total_buy_shares = 0  # only buy ops (for avg buy price)
        total_buy_amount = 0.0  # only buy ops

        for j in ops:
            shares = j.shares or 0
            price = j.price_at_decision or 0

            if j.action in ("open", "add"):
                total_shares += shares
                total_cost += shares * price
                total_buy_shares += shares
                total_buy_amount += shares * price
            elif j.action == "retroactive":
                # Retroactive: set position directly
                total_shares = shares
                total_cost = shares * price
                total_buy_shares = shares
                total_buy_amount = shares * price
            elif j.action == "reduce":
                # Reduce: sell some shares at this price
                if total_shares > 0 and shares > 0:
                    # Cost basis per share before this sale
                    cost_per_share = total_cost / total_shares if total_shares else 0
                    sold_shares = min(shares, total_shares)
                    total_shares -= sold_shares
                    total_cost -= sold_shares * cost_per_share
            elif j.action == "close":
                # Full exit
                total_shares = 0
                total_cost = 0

        if total_shares <= 0:
            continue

        cost_basis = total_cost / total_shares if total_shares > 0 else 0
        avg_buy_price = total_buy_amount / total_buy_shares if total_buy_shares > 0 else 0
        cur_price = price_map.get(ticker)
        daily_chg = quote_map.get(ticker, {}).get("change_pct")
        market_value = cur_price * total_shares if cur_price else None
        pnl = (cur_price - cost_basis) * total_shares if cur_price else None
        pnl_pct = ((cur_price - cost_basis) / cost_basis * 100) if (cur_price and cost_basis) else None
        pct_of_total = (market_value / (total_capital * 10000) * 100) if (market_value and total_capital) else None

        portfolio.append({
            "ticker": ticker,
            "name": ticker_name_map.get(ticker, ticker),
            "shares": total_shares,
            "cost_basis": cost_basis,
            "avg_buy_price": avg_buy_price,
            "current_price": cur_price,
            "daily_chg": daily_chg,
            "market_value": market_value,
            "pnl": pnl,
            "pnl_pct": pnl_pct,
            "pct_of_total": pct_of_total,
        })

    portfolio.sort(key=lambda x: abs(x.get("market_value") or 0), reverse=True)
    return portfolio


@router.get("", response_class=HTMLResponse)
async def journal_list(
    request: Request,
    user: Annotated[User, Depends(auth.require_user)],
    session: Annotated[AsyncSession, Depends(db.get_session)],
):
    status_filter = request.query_params.get("status", "active")
    q = select(DecisionJournal).where(DecisionJournal.user_id == user.id)
    if status_filter == "active":
        q = q.where(DecisionJournal.status == "active")
    elif status_filter == "closed":
        q = q.where(DecisionJournal.status == "closed")
    q = q.order_by(DecisionJournal.created_at.desc())
    rows = await session.execute(q)
    journals = list(rows.scalars())

    # Also get ALL active journals for portfolio (even if filter is 'closed')
    all_q = select(DecisionJournal).where(
        DecisionJournal.user_id == user.id, DecisionJournal.status == "active"
    )
    all_rows = await session.execute(all_q)
    all_active = list(all_rows.scalars())

    # Fetch prices for all tickers
    all_tickers = list({j.ticker for j in journals} | {j.ticker for j in all_active})
    price_map = {}
    quote_map = {}
    if all_tickers:
        quotes = await asyncio.to_thread(
            get_watchlist_quotes, [_FakeW(t) for t in all_tickers]
        )
        for qq in quotes:
            if qq.get("ok") and qq.get("current"):
                price_map[qq["ticker"]] = qq["current"]
                quote_map[qq["ticker"]] = qq

    total_capital = user.total_capital or None
    portfolio = _build_portfolio(all_active, price_map, quote_map, total_capital)

    # Total portfolio stats
    total_market_value = sum(p["market_value"] or 0 for p in portfolio)
    total_pnl = sum(p["pnl"] or 0 for p in portfolio)
    total_cost = sum((p["cost_basis"] * p["shares"]) for p in portfolio if p["cost_basis"])
    total_pnl_pct = (total_pnl / total_cost * 100) if total_cost > 0 else None

    # Adjust displayed capital to include unrealized PnL
    display_capital = total_capital
    if total_capital and total_pnl:
        display_capital = total_capital + total_pnl / 10000  # pnl is in yuan, capital in 万

    # Fetch investment notes
    notes_q = select(InvestmentNote).where(
        InvestmentNote.user_id == user.id
    ).order_by(InvestmentNote.created_at.desc()).limit(20)
    notes_rows = await session.execute(notes_q)
    notes = list(notes_rows.scalars())

    return templates.TemplateResponse(
        request=request,
        name="journal/list.html",
        context={
            "user": user,
            "journals": journals,
            "price_map": price_map,
            "status_filter": status_filter,
            "portfolio": portfolio,
            "total_capital": display_capital,
            "base_capital": total_capital,
            "total_market_value": total_market_value,
            "total_pnl": total_pnl,
            "total_pnl_pct": total_pnl_pct,
            "notes": notes,
        },
    )


@router.post("/capital")
async def set_capital(
    request: Request,
    user: Annotated[User, Depends(auth.require_user)],
    session: Annotated[AsyncSession, Depends(db.get_session)],
    mode: str = Form("set"),
    total_capital: float = Form(None),
    amount: float = Form(None),
):
    user_obj = await session.get(User, user.id)
    log.info("set_capital called: mode=%s total_capital=%s amount=%s user_id=%s current=%s",
             mode, total_capital, amount, user.id, user_obj.total_capital)
    if mode == "set" and total_capital is not None:
        user_obj.total_capital = total_capital
    elif mode == "deposit" and amount:
        user_obj.total_capital = (user_obj.total_capital or 0) + amount
    elif mode == "withdraw" and amount:
        user_obj.total_capital = max(0, (user_obj.total_capital or 0) - amount)
    log.info("set_capital after: total_capital=%s", user_obj.total_capital)
    await session.commit()
    log.info("set_capital committed")
    return RedirectResponse("/journal", status_code=303)


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
    price_at_decision: float = Form(None),
    shares: int = Form(None),
    reasoning: str = Form(...),
    action_plan: str = Form(""),
    target_price: float = Form(None),
    stop_loss_price: float = Form(None),
    expected_hold_period: str = Form(""),
    if_drop_10pct: str = Form(""),
):
    # Resolve ticker: user can input just name OR just code
    raw_ticker = ticker.strip()
    raw_name = ticker_name.strip()
    if raw_ticker and not raw_name:
        info = resolve_ticker(raw_ticker)
        if info:
            raw_ticker = info.code
            raw_name = info.name
    elif raw_name and not raw_ticker:
        info = resolve_ticker(raw_name)
        if info:
            raw_ticker = info.code
            raw_name = info.name
    elif raw_ticker:
        info = resolve_ticker(raw_ticker)
        if info and raw_name == raw_ticker:
            raw_name = info.name

    journal = DecisionJournal(
        user_id=user.id,
        ticker=raw_ticker,
        ticker_name=raw_name,
        action=action,
        action_detail=action_plan or None,
        price_at_decision=price_at_decision,
        position_pct=None,
        shares=shares,
        reasoning=reasoning,
        hold_conditions=None,
        exit_signals=None,
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
    background_tasks.add_task(_fill_snapshot, journal_id)
    # Auto-add to watchlist
    background_tasks.add_task(_auto_add_watchlist, user.id, ticker.strip(), ticker_name.strip())
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

    quotes = await asyncio.to_thread(get_watchlist_quotes, [_FakeW(journal.ticker)])
    current_price = quotes[0].get("current") if quotes else None
    pnl_pct = None
    if current_price and journal.price_at_decision:
        pnl_pct = (current_price - journal.price_at_decision) / journal.price_at_decision * 100

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


@router.post("/note")
async def create_note(
    user: Annotated[User, Depends(auth.require_user)],
    session: Annotated[AsyncSession, Depends(db.get_session)],
    content: str = Form(...),
):
    note = InvestmentNote(user_id=user.id, content=content.strip())
    session.add(note)
    await session.commit()
    return RedirectResponse("/journal#notes", status_code=303)


@router.post("/note/{nid}/delete")
async def delete_note(
    nid: int,
    user: Annotated[User, Depends(auth.require_user)],
    session: Annotated[AsyncSession, Depends(db.get_session)],
):
    note = await session.get(InvestmentNote, nid)
    if not note or note.user_id != user.id:
        raise HTTPException(status_code=404)
    await session.delete(note)
    await session.commit()
    return RedirectResponse("/journal#notes", status_code=303)
