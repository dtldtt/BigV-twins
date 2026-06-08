"""Admin routes: dashboard, invites rotate, users, blogger visibility, cleanup."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from bigv_twins.config import BLOGGERS, BY_SLUG

from . import auth, db, invites
from .db import BloggerOverride, Conversation, Message, User

router = APIRouter(prefix="/admin")
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


# ------------------------------------------------------------ dashboard

@router.get("/", response_class=HTMLResponse)
async def dashboard(
    request: Request,
    admin_user: Annotated[User, Depends(auth.require_admin)],
    session: Annotated[AsyncSession, Depends(db.get_session)],
):
    counts = {
        "users": (await session.execute(select(func.count(User.id)))).scalar(),
        "conversations": (await session.execute(select(func.count(Conversation.id)))).scalar(),
        "messages": (await session.execute(select(func.count(Message.id)))).scalar(),
    }
    tok_in = (await session.execute(select(func.coalesce(func.sum(Message.token_usage_in), 0)))).scalar()
    tok_out = (await session.execute(select(func.coalesce(func.sum(Message.token_usage_out), 0)))).scalar()
    week_cutoff = datetime.now(timezone.utc) - timedelta(days=7)
    msgs_7d = (await session.execute(
        select(func.count(Message.id)).where(Message.created_at >= week_cutoff)
    )).scalar()
    active = await invites.get_active(session)
    hidden_slugs = {r[0] for r in (await session.execute(
        select(BloggerOverride.slug).where(BloggerOverride.hidden.is_(True))
    )).all()}
    return templates.TemplateResponse(
        request=request, name="admin/dashboard.html",
        context={
            "user": admin_user,
            "counts": counts,
            "tok_in": tok_in, "tok_out": tok_out,
            "msgs_7d": msgs_7d,
            "active_invite": active,
            "hidden_count": len(hidden_slugs),
            "blogger_total": len(BLOGGERS),
        },
    )




@router.get("/cost", response_class=HTMLResponse)
async def cost_dashboard(
    request: Request,
    admin_user: Annotated[User, Depends(auth.require_admin)],
):
    """Token 仪表盘 — Qoder + Qwen 分开统计。"""
    from datetime import date, timedelta
    from .db import QoderUsageLog
    from sqlalchemy import func as sqlfunc

    seven_days_ago = (date.today() - timedelta(days=7)).strftime("%Y-%m-%d")
    qoder_stats = {}

    async with db._SessionFactory() as s:
        qr = await s.execute(
            select(
                sqlfunc.count().label("calls"),
                sqlfunc.sum(QoderUsageLog.input_tokens).label("input"),
                sqlfunc.sum(QoderUsageLog.output_tokens).label("output"),
                sqlfunc.sum(QoderUsageLog.duration_ms).label("duration"),
            ).where(QoderUsageLog.created_at >= seven_days_ago)
        )
        row = qr.one()
        qoder_stats["calls"] = row.calls or 0
        qoder_stats["input"] = row.input or 0
        qoder_stats["output"] = row.output or 0
        qoder_stats["duration_min"] = round((row.duration or 0) / 60000, 1)

        qr2 = await s.execute(
            select(
                QoderUsageLog.task_type,
                sqlfunc.count().label("calls"),
                sqlfunc.sum(QoderUsageLog.input_tokens + QoderUsageLog.output_tokens).label("tokens"),
            ).where(QoderUsageLog.created_at >= seven_days_ago)
            .group_by(QoderUsageLog.task_type)
        )
        qoder_stats["by_type"] = [{"type": r.task_type, "calls": r.calls, "tokens": r.tokens or 0} for r in qr2]

        qr3 = await s.execute(
            select(
                sqlfunc.date(QoderUsageLog.created_at).label("day"),
                sqlfunc.sum(QoderUsageLog.input_tokens + QoderUsageLog.output_tokens).label("tokens"),
            ).where(QoderUsageLog.created_at >= seven_days_ago)
            .group_by(sqlfunc.date(QoderUsageLog.created_at))
            .order_by(sqlfunc.date(QoderUsageLog.created_at))
        )
        qoder_stats["daily"] = [{"day": r.day, "tokens": r.tokens or 0} for r in qr3]

        qr4 = await s.execute(
            select(QoderUsageLog).order_by(QoderUsageLog.created_at.desc()).limit(15)
        )
        qoder_stats["recent"] = list(qr4.scalars())

        # Qoder: by model
        qr5 = await s.execute(
            select(
                QoderUsageLog.model,
                sqlfunc.count().label("calls"),
                sqlfunc.sum(QoderUsageLog.input_tokens).label("input"),
                sqlfunc.sum(QoderUsageLog.output_tokens).label("output"),
            ).where(QoderUsageLog.created_at >= seven_days_ago)
            .group_by(QoderUsageLog.model)
        )
        qoder_stats["by_model"] = [{"model": r.model, "calls": r.calls,
                                     "input": r.input or 0, "output": r.output or 0} for r in qr5]

        # Qoder: monthly archive
        qr6 = await s.execute(
            select(
                sqlfunc.strftime("%Y-%m", QoderUsageLog.created_at).label("month"),
                QoderUsageLog.model,
                sqlfunc.count().label("calls"),
                sqlfunc.sum(QoderUsageLog.input_tokens).label("input"),
                sqlfunc.sum(QoderUsageLog.output_tokens).label("output"),
                sqlfunc.sum(QoderUsageLog.duration_ms).label("duration"),
            ).group_by(sqlfunc.strftime("%Y-%m", QoderUsageLog.created_at), QoderUsageLog.model)
            .order_by(sqlfunc.strftime("%Y-%m", QoderUsageLog.created_at).desc())
        )
        monthly_raw = {}
        for r in qr6:
            mo = monthly_raw.setdefault(r.month, {"month": r.month, "models": [], "total_calls": 0, "total_input": 0, "total_output": 0})
            mo["models"].append({"model": r.model, "calls": r.calls, "input": r.input or 0, "output": r.output or 0, "duration_min": round((r.duration or 0)/60000, 1)})
            mo["total_calls"] += r.calls
            mo["total_input"] += (r.input or 0)
            mo["total_output"] += (r.output or 0)
        qoder_stats["monthly"] = list(monthly_raw.values())

    return templates.TemplateResponse(
        request=request, name="admin/cost.html",
        context={"user": admin_user, "qoder_stats": qoder_stats},
    )


@router.get("/api/token-usage")
async def token_usage_api(
    admin_user: Annotated[User, Depends(auth.require_admin)],
    model: str = "qwen3.6-flash",
):
    """Return aggregated token usage stats for charts."""
    from .token_usage import get_dashboard_stats
    return await get_dashboard_stats(model=model)


@router.post("/api/token-usage/refresh")
async def token_usage_refresh_now(
    admin_user: Annotated[User, Depends(auth.require_admin)],
):
    """Manual trigger to refresh token usage from session files."""
    from .token_usage import refresh_token_usage
    result = await refresh_token_usage()
    return result


@router.get("/api/monthly-reports")
async def monthly_reports_api(
    admin_user: Annotated[User, Depends(auth.require_admin)],
):
    """Return list of monthly billing-cycle reports."""
    from .token_usage import get_monthly_reports
    return await get_monthly_reports()

# ------------------------------------------------------------ invites

@router.get("/invites", response_class=HTMLResponse)
async def invites_page(
    request: Request,
    admin_user: Annotated[User, Depends(auth.require_admin)],
    session: Annotated[AsyncSession, Depends(db.get_session)],
):
    all_inv = await invites.list_all(session)
    # Count users registered against each invite
    rows = await session.execute(
        select(User.invite_id, func.count(User.id))
        .where(User.invite_id.is_not(None))
        .group_by(User.invite_id)
    )
    usage = {iid: cnt for iid, cnt in rows.all()}
    return templates.TemplateResponse(
        request=request, name="admin/invites.html",
        context={"user": admin_user, "invites": all_inv, "usage": usage},
    )


@router.post("/invites/rotate")
async def rotate_invite(
    admin_user: Annotated[User, Depends(auth.require_admin)],
    session: Annotated[AsyncSession, Depends(db.get_session)],
):
    await invites.rotate(session, created_by_user_id=admin_user.id)
    return RedirectResponse("/admin/invites", status_code=303)


# ------------------------------------------------------------ users

@router.get("/users", response_class=HTMLResponse)
async def users_page(
    request: Request,
    admin_user: Annotated[User, Depends(auth.require_admin)],
    session: Annotated[AsyncSession, Depends(db.get_session)],
):
    users = list((await session.execute(
        select(User).order_by(User.created_at.desc())
    )).scalars())
    # Per-user conversation + token stats
    conv_counts = dict((await session.execute(
        select(Conversation.user_id, func.count(Conversation.id))
        .group_by(Conversation.user_id)
    )).all())
    tok_rows = await session.execute(
        select(
            Conversation.user_id,
            func.coalesce(func.sum(Message.token_usage_in), 0),
            func.coalesce(func.sum(Message.token_usage_out), 0),
        )
        .join(Message, Message.conversation_id == Conversation.id)
        .group_by(Conversation.user_id)
    )
    tok_stats = {uid: (tin, tout) for uid, tin, tout in tok_rows.all()}
    return templates.TemplateResponse(
        request=request, name="admin/users.html",
        context={
            "user": admin_user, "users": users,
            "conv_counts": conv_counts, "tok_stats": tok_stats,
        },
    )


@router.post("/users/{uid}/delete")
async def delete_user(
    uid: int,
    admin_user: Annotated[User, Depends(auth.require_admin)],
    session: Annotated[AsyncSession, Depends(db.get_session)],
):
    if uid == admin_user.id:
        raise HTTPException(status_code=400, detail="cannot delete yourself")
    target = await session.get(User, uid)
    if target is None:
        raise HTTPException(status_code=404)
    if target.role == "admin":
        raise HTTPException(status_code=400, detail="cannot delete another admin")
    await session.delete(target)
    return RedirectResponse("/admin/users", status_code=303)


# ------------------------------------------------------------ blogger visibility

@router.get("/bloggers", response_class=HTMLResponse)
async def bloggers_page(
    request: Request,
    admin_user: Annotated[User, Depends(auth.require_admin)],
    session: Annotated[AsyncSession, Depends(db.get_session)],
):
    overrides = {r.slug: r for r in (
        await session.execute(select(BloggerOverride))
    ).scalars()}
    rows = []
    for b in BLOGGERS:
        ov = overrides.get(b.slug)
        rows.append({
            "blogger": b,
            "hidden": bool(ov and ov.hidden),
            "hidden_at": ov.hidden_at if ov else None,
        })
    return templates.TemplateResponse(
        request=request, name="admin/bloggers.html",
        context={"user": admin_user, "rows": rows},
    )


@router.post("/bloggers/{slug}/toggle")
async def toggle_blogger(
    slug: str,
    admin_user: Annotated[User, Depends(auth.require_admin)],
    session: Annotated[AsyncSession, Depends(db.get_session)],
):
    if slug not in BY_SLUG:
        raise HTTPException(status_code=404)
    ov = await session.get(BloggerOverride, slug)
    now = datetime.now(timezone.utc)
    if ov is None:
        ov = BloggerOverride(
            slug=slug, hidden=True,
            hidden_at=now, hidden_by_user_id=admin_user.id,
        )
        session.add(ov)
    else:
        ov.hidden = not ov.hidden
        ov.hidden_at = now if ov.hidden else None
        ov.hidden_by_user_id = admin_user.id if ov.hidden else None
    return RedirectResponse("/admin/bloggers", status_code=303)


# ------------------------------------------------------------ cleanup

@router.get("/cleanup", response_class=HTMLResponse)
async def cleanup_page(
    request: Request,
    admin_user: Annotated[User, Depends(auth.require_admin)],
    session: Annotated[AsyncSession, Depends(db.get_session)],
):
    # Preview: how many conversations would be hit at common cutoffs
    presets = []
    now = datetime.now(timezone.utc)
    for days in [7, 30, 90, 180, 365]:
        cutoff = now - timedelta(days=days)
        n = (await session.execute(
            select(func.count(Conversation.id)).where(Conversation.updated_at < cutoff)
        )).scalar()
        presets.append({"days": days, "count": n})
    return templates.TemplateResponse(
        request=request, name="admin/cleanup.html",
        context={"user": admin_user, "presets": presets},
    )


@router.post("/cleanup")
async def cleanup_submit(
    admin_user: Annotated[User, Depends(auth.require_admin)],
    session: Annotated[AsyncSession, Depends(db.get_session)],
    older_than_days: Annotated[int, Form()],
    scope: Annotated[str, Form()] = "all",   # "all" or "self"
):
    if older_than_days < 1:
        raise HTTPException(status_code=400, detail="days must be >= 1")
    cutoff = datetime.now(timezone.utc) - timedelta(days=older_than_days)
    q = delete(Conversation).where(Conversation.updated_at < cutoff)
    if scope == "self":
        q = q.where(Conversation.user_id == admin_user.id)
    await session.execute(q)
    return RedirectResponse("/admin/cleanup", status_code=303)
