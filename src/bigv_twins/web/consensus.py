"""共识仪表板 — 展示博主观点的共识/分歧/独家视角。"""

from __future__ import annotations

import logging
from collections import defaultdict
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from bigv_twins.config import BY_SLUG

from . import auth, db
from .db import TickerOpinionLog, User

log = logging.getLogger("bigv_twins.web.consensus")
router = APIRouter(prefix="/consensus")

PKG_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(PKG_DIR / "templates"))


@router.get("", response_class=HTMLResponse)
async def consensus_page(
    request: Request,
    user: Annotated[User, Depends(auth.require_user)],
    session: Annotated[AsyncSession, Depends(db.get_session)],
    days: int = Query(7, ge=1, le=90),
):
    from datetime import date, timedelta
    cutoff = (date.today() - timedelta(days=days)).strftime("%Y-%m-%d")

    # Query all recent opinions
    rows = await session.execute(
        select(TickerOpinionLog).where(
            TickerOpinionLog.opinion_date >= cutoff
        )
    )
    all_opinions = list(rows.scalars())

    # Group by ticker
    by_ticker: dict[str, list] = defaultdict(list)
    for op in all_opinions:
        by_ticker[op.ticker].append(op)

    # Classify into categories
    high_consensus = []  # 3+ bloggers same direction
    high_divergence = []  # has both bullish and bearish
    solo_picks = []  # only 1 blogger mentioned

    for ticker, ops in by_ticker.items():
        bloggers = set(op.blogger_slug for op in ops)
        sentiments = set(op.sentiment for op in ops)
        bullish_count = sum(1 for op in ops if op.sentiment == "bullish")
        bearish_count = sum(1 for op in ops if op.sentiment in ("bearish", "avoid"))
        ticker_name = ops[0].ticker_name if ops else ticker

        entry = {
            "ticker": ticker,
            "ticker_name": ticker_name,
            "blogger_count": len(bloggers),
            "opinions": [{
                "blogger_slug": op.blogger_slug,
                "blogger_name": BY_SLUG[op.blogger_slug].name if op.blogger_slug in BY_SLUG else op.blogger_slug,
                "sentiment": op.sentiment,
                "summary": op.summary,
                "date": op.opinion_date,
            } for op in ops],
        }

        if bullish_count >= 3 or bearish_count >= 3:
            entry["direction"] = "bullish" if bullish_count >= 3 else "bearish"
            entry["count"] = max(bullish_count, bearish_count)
            high_consensus.append(entry)
        elif bullish_count >= 1 and bearish_count >= 1:
            high_divergence.append(entry)
        elif len(bloggers) == 1:
            solo_picks.append(entry)

    # Sort
    high_consensus.sort(key=lambda x: x["count"], reverse=True)
    high_divergence.sort(key=lambda x: x["blogger_count"], reverse=True)
    solo_picks.sort(key=lambda x: x["opinions"][0]["date"], reverse=True)

    return templates.TemplateResponse(
        request=request,
        name="consensus.html",
        context={
            "user": user,
            "days": days,
            "high_consensus": high_consensus,
            "high_divergence": high_divergence,
            "solo_picks": solo_picks[:10],
            "total_opinions": len(all_opinions),
            "total_tickers": len(by_ticker),
        },
    )
