"""趋势追踪 — 数据积累 + 简单时间线页面。

Phase 1: 积累数据（prediction_log + market_snapshot_daily）
后续: 验证 + LLM 分析 + 知识库
"""
from __future__ import annotations

import json
import logging
import re
from datetime import date, timedelta
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select

from . import auth, db
from .db import (
    BloggerDailyBrief, DailyDigest, DecisionJournal, InvestmentNote,
    MarketSnapshotDaily, PredictionLog, User,
)
from bigv_twins.config import BY_SLUG

log = logging.getLogger("bigv_twins.web.trends")
router = APIRouter(prefix="/report")
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


# ============================================================
# 数据积累：从 digest 观察清单提取可验证预测
# ============================================================

async def extract_predictions_from_digest(day_str: str) -> int:
    """从 daily_digest 的 digest_md 中提取「观察清单」里的可验证条目。

    用简单的正则匹配，不需要 LLM。
    返回新增条目数。
    """
    async with db._SessionFactory() as s:
        digest = await s.execute(
            select(DailyDigest).where(DailyDigest.digest_date == day_str)
        )
        d = digest.scalar_one_or_none()
        if not d:
            return 0

        # 已提取过？
        existing = await s.execute(
            select(PredictionLog.id).where(PredictionLog.digest_date == day_str).limit(1)
        )
        if existing.scalar_one_or_none() is not None:
            return 0

    md = d.digest_md or ""

    # 找观察清单 section
    match = re.search(r"(?:观察清单|今日观察)(.+?)(?=###|\Z)", md, re.S)
    if not match:
        return 0

    section = match.group(1)
    count = 0

    # 解析列表项：两种格式都匹配
    # 格式 1: - **关键词**：XXX（博主名）
    # 格式 2: - 内容（博主名）
    raw_items = re.findall(r"-\s+(.+?)(?=\n-|\n\n|\Z)", section, re.S)

    items = []
    for raw in raw_items:
        raw = raw.strip()
        if not raw:
            continue
        # 尝试拆 **title**：body
        m = re.match(r"\*\*(.+?)\*\*[：:]\s*(.+)", raw, re.S)
        if m:
            items.append((m.group(1).strip(), m.group(2).strip()))
        else:
            items.append(("", raw))

    async with db._SessionFactory() as s:
        for title, body in items:
            full_text = f"{title}：{body}" if title else body
            # 提取博主名
            blogger_match = re.search(r"[（(](.+?)[)）]", full_text)
            blogger_name = blogger_match.group(0).strip("（()）") if blogger_match else ""
            blogger_slug = ""
            for slug, b in BY_SLUG.items():
                if b.name == blogger_name:
                    blogger_slug = slug
                    break

            # 判断方向
            direction = "neutral"
            text_lower = full_text.lower()
            if any(w in text_lower for w in ["跌破", "破位", "下跌", "看空", "减仓", "恶性循环", "逆转"]):
                direction = "bearish"
            elif any(w in text_lower for w in ["守住", "突破", "看好", "看多", "加仓", "充分调整"]):
                direction = "bullish"

            # 判断类型
            pred_type = "observation"
            if re.search(r"\d{3,6}点|¥[\d.]+|\d{4}-\d{4}", full_text):
                pred_type = "price_level"
            elif re.search(r"\d月|下周|今晚|周末|6月|7月", full_text):
                pred_type = "event_window"

            # 验证日期：默认 1 周后
            verify_dt = (date.fromisoformat(day_str) + timedelta(days=7)).strftime("%Y-%m-%d")

            # 提取 ticker
            ticker_match = re.search(r"(\d{5,6})", title + body)
            ticker = ticker_match.group(1) if ticker_match else None

            s.add(PredictionLog(
                digest_date=day_str,
                blogger_slug=blogger_slug or "unknown",
                blogger_name=blogger_name or "综合",
                prediction_text=full_text[:200],
                prediction_type=pred_type,
                ticker=ticker,
                direction=direction,
                verify_by_date=verify_dt,
            ))
            count += 1
        await s.commit()

    log.info("extracted %d predictions from digest %s", count, day_str)
    return count


# ============================================================
# 数据积累：每日行情快照
# ============================================================

async def save_market_snapshot(day_str: str | None = None) -> int:
    """存储关键标的的当日行情快照。

    标的来源：用户自选股 + 常用指数。
    """
    if day_str is None:
        day_str = date.today().strftime("%Y-%m-%d")

    from .daily_brief import get_watchlist_quotes

    # 常用指数 + 用户自选股的 ticker
    key_tickers = ["000001", "399006", "399001", "000688", "HSI", "IXIC"]

    # 加上用户自选股
    async with db._SessionFactory() as s:
        from .db import User as U
        users = await s.execute(select(U.id))
        for uid_row in users:
            uid = uid_row[0]
            from sqlalchemy import text
            wl = await s.execute(
                text("SELECT ticker FROM user_watchlist WHERE user_id = :uid"),
                {"uid": uid}
            )
            for row in wl:
                if row[0] not in key_tickers:
                    key_tickers.append(row[0])

    # 拉行情
    class FakeItem:
        def __init__(self, t):
            self.ticker = t
            self.name = t
            self.market = "A"
            self.note = ""
            self.id = 0

    import asyncio
    quotes = await asyncio.to_thread(get_watchlist_quotes, [FakeItem(t) for t in key_tickers[:30]])

    count = 0
    async with db._SessionFactory() as s:
        for q in quotes:
            if not q.get("ok"):
                continue
            try:
                s.add(MarketSnapshotDaily(
                    snapshot_date=day_str,
                    ticker=q["ticker"],
                    ticker_name=q.get("name", q["ticker"]),
                    close_price=q.get("current"),
                    change_pct=q.get("change_pct"),
                    pe=q.get("pe"),
                    pb=q.get("pb"),
                    market_cap=q.get("market_cap"),
                ))
                await s.flush()
                count += 1
            except Exception:
                await s.rollback()
                continue
        await s.commit()

    log.info("saved %d market snapshots for %s", count, day_str)
    return count


# ============================================================
# 页面路由
# ============================================================

@router.get("/trends", response_class=HTMLResponse)
async def trends_page(
    request: Request,
    user: Annotated[User, Depends(auth.require_user)],
    session: Annotated[db.AsyncSession, Depends(db.get_session)],
):
    """趋势追踪时间线页面。"""
    # 最近 14 天的 digest
    fourteen_ago = (date.today() - timedelta(days=14)).strftime("%Y-%m-%d")
    digest_rows = await session.execute(
        select(DailyDigest)
        .where(DailyDigest.digest_date >= fourteen_ago)
        .order_by(DailyDigest.digest_date.desc())
    )
    digests = list(digest_rows.scalars())

    # 最近的预测
    pred_rows = await session.execute(
        select(PredictionLog)
        .order_by(PredictionLog.digest_date.desc())
        .limit(20)
    )
    predictions = list(pred_rows.scalars())

    # 最近 14 天的用户随笔
    note_rows = await session.execute(
        select(InvestmentNote)
        .where(InvestmentNote.user_id == user.id)
        .where(InvestmentNote.created_at >= fourteen_ago)
        .order_by(InvestmentNote.created_at.desc())
    )
    notes = list(note_rows.scalars())

    # 最近 14 天的用户操作
    journal_rows = await session.execute(
        select(DecisionJournal)
        .where(DecisionJournal.user_id == user.id)
        .where(DecisionJournal.created_at >= fourteen_ago)
        .order_by(DecisionJournal.created_at.desc())
    )
    journals = list(journal_rows.scalars())

    # archive base
    req_host = request.url.hostname or "8.155.174.112"
    req_scheme = request.url.scheme or "http"
    archive_base = f"{req_scheme}://{req_host}:8000"

    return templates.TemplateResponse(
        request=request,
        name="report/trends.html",
        context={
            "user": user,
            "digests": digests,
            "predictions": predictions,
            "notes": notes,
            "journals": journals,
            "archive_base": archive_base,
        },
    )
