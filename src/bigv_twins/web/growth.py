"""/growth 路由 — 成长复盘报告时间线 + 详情 + 手动触发。"""
from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pathlib import Path
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from . import auth, db
from .db import GrowthReport, User
from .reflection_engine import (
    generate_growth_report, _last_month_window, _last_quarter_window,
)

log = logging.getLogger("bigv_twins.web.growth")

router = APIRouter(prefix="/growth")
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


@router.get("", response_class=HTMLResponse)
async def growth_timeline(
    request: Request,
    user: Annotated[User, Depends(auth.require_user)],
    session: Annotated[AsyncSession, Depends(db.get_session)],
):
    rows = await session.execute(
        select(GrowthReport)
        .where(GrowthReport.user_id == user.id)
        .order_by(GrowthReport.period_end.desc(), GrowthReport.created_at.desc())
    )
    reports = list(rows.scalars())

    # 把每份报告的 key_lessons 解析出来做 \"个人知识库\" 汇总
    all_lessons: list[tuple[str, str, str]] = []  # (lesson, period_label, report_id)
    for r in reports:
        try:
            lessons = json.loads(r.key_lessons_json or "[]")
            label = f"{r.period_start} → {r.period_end}"
            for l in lessons:
                all_lessons.append((str(l), label, r.id))
        except json.JSONDecodeError:
            continue

    # 已清仓复盘（按月分组）
    from .db import DecisionReview
    from sqlalchemy import func as sqlfunc
    closed_rows = await session.execute(
        select(DecisionReview)
        .where(DecisionReview.user_id == user.id)
        .where(DecisionReview.review_type == "closed")
        .order_by(DecisionReview.created_at.desc())
    )
    closed_reviews = list(closed_rows.scalars())
    # 按月分组
    closed_by_month: dict[str, list] = {}
    for cr in closed_reviews:
        month = cr.created_at.strftime("%Y-%m") if cr.created_at else "unknown"
        closed_by_month.setdefault(month, []).append(cr)

    return templates.TemplateResponse(
        request=request, name="growth/timeline.html",
        context={
            "user": user, "reports": reports, "all_lessons": all_lessons,
            "closed_by_month": closed_by_month,
        },
    )


@router.post("/new")
async def growth_new(
    user: Annotated[User, Depends(auth.require_user)],
    period: str = Form("month"),
):
    """手动触发一次报告生成。period: month / quarter."""
    if period == "month":
        start, end = _last_month_window()
        ptype = "month"
    elif period == "quarter":
        start, end = _last_quarter_window()
        ptype = "quarter"
    else:
        raise HTTPException(400, "period must be month or quarter")

    report = await generate_growth_report(user.id, ptype, start, end)
    if not report:
        raise HTTPException(500, "报告生成失败（看 logs/web.log）")
    return RedirectResponse(f"/growth/{report.id}", status_code=303)


@router.get("/{rid}", response_class=HTMLResponse)
async def growth_detail(
    request: Request, rid: int,
    user: Annotated[User, Depends(auth.require_user)],
    session: Annotated[AsyncSession, Depends(db.get_session)],
):
    report = await session.get(GrowthReport, rid)
    if not report or report.user_id != user.id:
        raise HTTPException(404)
    stats = {}
    lessons: list[str] = []
    try:
        stats = json.loads(report.stats_json or "{}")
    except json.JSONDecodeError:
        pass
    try:
        lessons = json.loads(report.key_lessons_json or "[]")
    except json.JSONDecodeError:
        pass
    return templates.TemplateResponse(
        request=request, name="growth/detail.html",
        context={"user": user, "report": report, "stats": stats, "lessons": lessons},
    )
