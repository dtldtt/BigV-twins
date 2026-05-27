"""观点时间线 — 展示某只股票的博主观点演变。"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bigv_twins.config import BY_SLUG

from . import auth, db
from .db import TickerOpinionLog, User

log = logging.getLogger("bigv_twins.web.timeline")
router = APIRouter(prefix="/timeline")

PKG_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(PKG_DIR / "templates"))


@router.get("/{ticker}", response_class=HTMLResponse)
async def timeline_page(
    request: Request,
    ticker: str,
    user: Annotated[User, Depends(auth.require_user)],
    session: Annotated[AsyncSession, Depends(db.get_session)],
):
    from bigv_twins.stock_data import resolve_ticker

    # Resolve name
    info = resolve_ticker(ticker)
    stock_name = info.name if info else ticker

    # Query all opinions for this ticker
    rows = await session.execute(
        select(TickerOpinionLog).where(
            TickerOpinionLog.ticker == ticker,
        ).order_by(TickerOpinionLog.opinion_date.desc()).limit(100)
    )
    opinions = []
    for op in rows.scalars():
        blogger = BY_SLUG.get(op.blogger_slug)
        opinions.append({
            "blogger_slug": op.blogger_slug,
            "blogger_name": blogger.name if blogger else op.blogger_slug,
            "date": op.opinion_date,
            "sentiment": op.sentiment,
            "summary": op.summary,
            "price": op.price_at_opinion,
        })

    # Group by date for timeline display
    from collections import OrderedDict
    by_date: dict[str, list] = OrderedDict()
    for op in opinions:
        by_date.setdefault(op["date"], []).append(op)

    return templates.TemplateResponse(
        request=request,
        name="timeline.html",
        context={
            "user": user,
            "ticker": ticker,
            "stock_name": stock_name,
            "by_date": by_date,
            "total_count": len(opinions),
        },
    )
