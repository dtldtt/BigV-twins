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
from fastapi.responses import HTMLResponse, RedirectResponse
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



def _fetch_roe_approx(ticker: str) -> dict:
    """Estimate ROE from latest annual report's EPS / BPS.

    Uses akshare stock_fhps_detail_em which has both fields per fiscal year.
    Returns {} on any failure. Slow (~2s) so call from async via to_thread.
    """
    try:
        import akshare as ak
        df = ak.stock_fhps_detail_em(symbol=ticker)
        if df is None or df.empty:
            return {}
        # Most recent annual report (报告期 ending in 12-31)
        annual = df[df["报告期"].astype(str).str.endswith("12-31")].sort_values("报告期", ascending=False)
        if annual.empty:
            return {}
        row = annual.iloc[0]
        eps = float(row.get("每股收益") or 0)
        bps = float(row.get("每股净资产") or 0)
        if bps <= 0:
            return {}
        roe_pct = (eps / bps) * 100
        return {
            "roe_pct": round(roe_pct, 2),
            "eps": round(eps, 3),
            "bps": round(bps, 3),
            "report_period": str(row.get("报告期", "")),
            "div_yield_pct": float(row.get("现金分红-股息率") or 0) or None,
        }
    except Exception as e:
        log.warning("ROE fetch failed for %s: %s", ticker, e)
        return {}


@router.get("/{ticker}", response_class=HTMLResponse)
async def stock_page(
    request: Request,
    ticker: str,
    user: Annotated[User, Depends(auth.require_user)],
    session: Annotated[AsyncSession, Depends(db.get_session)],
):
    """个股研究聚合页。"""
    import asyncio

    # If ticker is a Chinese name (not a code), resolve and redirect to code URL
    if not ticker.isdigit():
        info = resolve_ticker(ticker)
        if info and info.code != ticker:
            return RedirectResponse(f"/stock/{info.code}", status_code=302)

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
    # Also resolve the name for matching (user might have stored by name)
    _resolved = resolve_ticker(ticker)
    _match_values = [ticker]
    if _resolved and _resolved.name != ticker:
        _match_values.append(_resolved.name)
    journal_rows = await session.execute(
        select(DecisionJournal).where(
            DecisionJournal.user_id == user.id,
            or_(
                DecisionJournal.ticker.in_(_match_values),
                DecisionJournal.ticker_name.in_(_match_values),
            ),
        ).order_by(DecisionJournal.created_at.desc()).limit(10)
    )
    _raw_entries = list(journal_rows.scalars())
    # Eagerly extract attributes to avoid lazy-load issues after session closes
    journal_entries = []
    for j in _raw_entries:
        journal_entries.append({
            "id": j.id,
            "action": j.action,
            "price_at_decision": j.price_at_decision,
            "shares": j.shares,
            "created_at": j.created_at,
            "reasoning": j.reasoning,
            "action_detail": j.action_detail,
            "target_price": j.target_price,
            "stop_loss_price": j.stop_loss_price,
        })

    # Fetch ROE asynchronously (only A-share stocks, not ETF, not HK)
    roe = {}
    if ticker.isdigit() and len(ticker) == 6 and not _is_etf(ticker):
        try:
            roe = await asyncio.to_thread(_fetch_roe_approx, ticker)
        except Exception:
            pass

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
            "journal_entries": journal_entries,
            "ticker_type": ticker_type,
            "roe": roe,
        },
    )
