"""个股研究页 — 聚合一只股票的所有相关信息。

数据全部复用现有模块，零额外 LLM 调用：
- 基本面（PE/PB/市值）：腾讯行情（get_watchlist_quotes 同源）
- 博主提及：blogger_daily_brief.mentioned_tickers 聚合
- ticker_brief：最近一期的新闻摘要 + verdict
- 回测记录：backtest_entries
"""

from __future__ import annotations

import json
import logging
from datetime import date, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from bigv_twins.config import BY_SLUG

from . import auth, db
from .db import BacktestEntry, BloggerDailyBrief, DecisionJournal, TickerDailyBrief, User
from bigv_twins.stock_data import resolve_ticker, _is_etf
from .daily_brief import get_watchlist_quotes

log = logging.getLogger("bigv_twins.web.stock")
router = APIRouter(prefix="/stock")

from pathlib import Path
PKG_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(PKG_DIR / "templates"))


class _FakeWatchlistItem:
    """Minimal duck-type to reuse get_watchlist_quotes."""
    def __init__(self, ticker: str):
        self.ticker = ticker
        self.name = ticker
        self.market = "A"
        self.note = ""
        self.id = 0


@router.get("/{ticker}", response_class=HTMLResponse)
async def stock_page(
    request: Request,
    ticker: str,
    user: Annotated[User, Depends(auth.require_user)],
    session: Annotated[AsyncSession, Depends(db.get_session)],
):
    """个股研究聚合页。"""
    import asyncio

    # 1. Real-time quote + fundamentals (reuse tencent API)
    quotes = await asyncio.to_thread(get_watchlist_quotes, [_FakeWatchlistItem(ticker)])
    quote = quotes[0] if quotes else {}
    if not quote.get("ok"):
        # Try anyway — might be a valid ticker with temporary API issue
        pass

    # 2. Blogger mentions in last 30 days
    thirty_days_ago = (date.today() - timedelta(days=30)).strftime("%Y-%m-%d")
    rows = await session.execute(
        select(BloggerDailyBrief).where(
            BloggerDailyBrief.brief_date >= thirty_days_ago
        ).order_by(BloggerDailyBrief.brief_date.desc())
    )
    mentions = []
    for br in rows.scalars():
        try:
            tickers_mentioned = json.loads(br.mentioned_tickers or "[]")
        except json.JSONDecodeError:
            continue
        if ticker in tickers_mentioned:
            blogger = BY_SLUG.get(br.blogger_slug)
            mentions.append({
                "blogger_slug": br.blogger_slug,
                "blogger_name": blogger.name if blogger else br.blogger_slug,
                "date": br.brief_date,
                "brief_excerpt": (br.brief_md or "")[:200],
            })

    # 3. Latest ticker brief (news summary + verdict)
    tb_row = await session.execute(
        select(TickerDailyBrief).where(
            TickerDailyBrief.ticker == ticker
        ).order_by(TickerDailyBrief.brief_date.desc()).limit(1)
    )
    ticker_brief = tb_row.scalar_one_or_none()

    # 4. Backtest entries for this ticker
    bt_rows = await session.execute(
        select(BacktestEntry).where(
            BacktestEntry.ticker == ticker
        ).order_by(BacktestEntry.brief_date.desc()).limit(10)
    )
    backtests = []
    for bt in bt_rows.scalars():
        blogger = BY_SLUG.get(bt.blogger_slug)
        backtests.append({
            "blogger_name": blogger.name if blogger else bt.blogger_slug,
            "entry_date": bt.entry_date_actual or bt.brief_date,
            "entry_price": bt.entry_price,
            "exit_price": bt.exit_price,
            "ticker_return": bt.ticker_return,
            "bench_return": bt.bench_return,
            "excess_return": bt.excess_return,
            "status": "complete" if bt.exit_price else "pending",
        })

    # 5. User's journal entries for this ticker (match code OR name)
    journal_rows = await session.execute(
        select(DecisionJournal).where(
            DecisionJournal.user_id == user.id,
            or_(
                DecisionJournal.ticker == ticker,
                DecisionJournal.ticker_name == ticker,
            ),
        ).order_by(DecisionJournal.created_at.desc()).limit(10)
    )
    journal_entries = list(journal_rows.scalars())

    # Determine ticker type for conditional display
    is_etf = _is_etf(ticker)
    is_hk = len(ticker) == 5 and ticker.isdigit()
    ticker_type = "etf" if is_etf else ("hk" if is_hk else "stock")

    # Stock name: prefer from quote, fallback to ticker
    stock_name = quote.get("name") or ticker

    return templates.TemplateResponse(
        request=request,
        name="stock.html",
        context={
            "user": user,
            "ticker": ticker,
            "stock_name": stock_name,
            "quote": quote,
            "mentions": mentions,
            "ticker_brief": ticker_brief,
            "backtests": backtests,
        },
    )
