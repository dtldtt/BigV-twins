"""Multi-blogger conversation routes ("问所有人" 横向对比模式).

Mirrors web/chat.py for single-blogger, but with fan-out + summary.
Persists to multi_conversations / multi_messages / multi_sub_responses tables
(independent from single-blogger Conversation/Message).
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response, StreamingResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bigv_twins.config import BLOGGERS, BY_SLUG, Blogger

from . import auth, db
from .chat import hidden_slugs, visible_bloggers
from .db import MultiConversation, MultiMessage, MultiSubResponse, User
from .multi_orchestrator import market_context_block_for, orchestrate

log = logging.getLogger("bigv_twins.web.multi")
router = APIRouter(prefix="/multi")
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


MIN_PARTICIPANTS = 2
MAX_PARTICIPANTS = 10


# ------------------------------------------------------------ helpers


def _parse_participants(s: str) -> list[str]:
    try:
        v = json.loads(s)
        if isinstance(v, list) and all(isinstance(x, str) for x in v):
            return v
    except (json.JSONDecodeError, TypeError):
        pass
    return []


async def _assert_owned(session: AsyncSession, user_id: int, cid: int) -> MultiConversation:
    conv = await session.get(MultiConversation, cid)
    if conv is None or conv.user_id != user_id:
        raise HTTPException(status_code=404, detail="multi conversation not found")
    return conv


async def _list_user_multi(
    session: AsyncSession, user_id: int, limit: int = 50,
) -> list[MultiConversation]:
    result = await session.execute(
        select(MultiConversation)
        .where(MultiConversation.user_id == user_id)
        .order_by(MultiConversation.updated_at.desc())
        .limit(limit)
    )
    return list(result.scalars())


async def _resolve_participants(
    session: AsyncSession, slugs: list[str],
) -> list[Blogger]:
    """Filter / validate the requested participant slugs.

    Drop any that are unknown or currently hidden. Returns unique Blogger
    objects in the order they were requested (with duplicates removed).
    """
    hidden = await hidden_slugs(session)
    seen: set[str] = set()
    out: list[Blogger] = []
    for s in slugs:
        if s in seen or s not in BY_SLUG or s in hidden:
            continue
        # Skip entries without a configured agent (defensive)
        b = BY_SLUG[s]
        if not getattr(b, "agent", None):
            continue
        seen.add(s)
        out.append(b)
    return out


def _ordered_view(bloggers: list[Blogger]) -> list[Blogger]:
    """Same ordering as /chat home: bloggers → masters → advisors."""
    bs = [b for b in bloggers if b.is_blogger]
    masters = [b for b in bloggers if b.is_master]
    advs = [b for b in bloggers if b.is_advisor]
    return bs + masters + advs


# ------------------------------------------------------------ routes


@router.get("", response_class=HTMLResponse)
async def multi_index(
    request: Request,
    user: Annotated[User, Depends(auth.require_user)],
    session: Annotated[AsyncSession, Depends(db.get_session)],
):
    """List page — all multi-convos for current user + button to start a new one."""
    convos = await _list_user_multi(session, user.id)
    # Build a display-friendly version with parsed participant slugs + names
    items = []
    for c in convos:
        slugs = _parse_participants(c.participant_slugs)
        names = [BY_SLUG[s].name for s in slugs if s in BY_SLUG]
        items.append({"conv": c, "slugs": slugs, "names": names})

    return templates.TemplateResponse(
        request=request,
        name="multi/index.html",
        context={"user": user, "items": items},
    )


@router.get("/new", response_class=HTMLResponse)
async def multi_new_form(
    request: Request,
    user: Annotated[User, Depends(auth.require_user)],
    session: Annotated[AsyncSession, Depends(db.get_session)],
):
    """Show the checkbox form to pick participants for a new multi-conv."""
    bloggers = _ordered_view(await visible_bloggers(session))
    return templates.TemplateResponse(
        request=request,
        name="multi/select.html",
        context={
            "user": user,
            "bloggers": bloggers,
            "min": MIN_PARTICIPANTS,
            "max": MAX_PARTICIPANTS,
        },
    )


@router.post("/new")
async def multi_new_submit(
    user: Annotated[User, Depends(auth.require_user)],
    session: Annotated[AsyncSession, Depends(db.get_session)],
    title: Annotated[str, Form()] = "",
    participants: Annotated[list[str], Form()] = (),
):
    """Create a new multi-conversation. Form posts checked slugs."""
    chosen = await _resolve_participants(session, list(participants))
    if len(chosen) < MIN_PARTICIPANTS or len(chosen) > MAX_PARTICIPANTS:
        raise HTTPException(
            status_code=400,
            detail=f"请选择 {MIN_PARTICIPANTS}-{MAX_PARTICIPANTS} 位参与者，当前 {len(chosen)} 位",
        )

    conv = MultiConversation(
        user_id=user.id,
        title=(title or "").strip()[:120] or "(新多人对话)",
        participant_slugs=json.dumps([b.slug for b in chosen], ensure_ascii=False),
    )
    session.add(conv)
    await session.flush()
    return RedirectResponse(f"/multi/{conv.id}", status_code=303)


@router.get("/{cid}", response_class=HTMLResponse)
async def multi_conversation_page(
    request: Request,
    cid: int,
    user: Annotated[User, Depends(auth.require_user)],
    session: Annotated[AsyncSession, Depends(db.get_session)],
):
    conv = await _assert_owned(session, user.id, cid)
    slugs = _parse_participants(conv.participant_slugs)
    participants = [BY_SLUG[s] for s in slugs if s in BY_SLUG]

    # Load all messages + sub_responses for this conv (oldest first)
    msg_rows = await session.execute(
        select(MultiMessage)
        .where(MultiMessage.conversation_id == cid)
        .order_by(MultiMessage.created_at)
    )
    msgs = list(msg_rows.scalars())

    # For each role='user' msg, group its sub_responses by blogger_slug for the template
    turns: list[dict] = []  # [{user_msg, sub_responses_by_slug, summary_msg?}]
    pending_turn: dict | None = None
    for m in msgs:
        if m.role == "user":
            if pending_turn is not None:
                turns.append(pending_turn)
            sub_rows = await session.execute(
                select(MultiSubResponse)
                .where(MultiSubResponse.user_message_id == m.id)
            )
            sub_map = {r.blogger_slug: r for r in sub_rows.scalars()}
            pending_turn = {
                "user_msg": m,
                "sub_responses_by_slug": sub_map,
                "summary_msg": None,
            }
        elif m.role == "summary" and pending_turn is not None:
            pending_turn["summary_msg"] = m
            turns.append(pending_turn)
            pending_turn = None
    if pending_turn is not None:
        turns.append(pending_turn)

    sidebar_convos = await _list_user_multi(session, user.id)
    return templates.TemplateResponse(
        request=request,
        name="multi/conversation.html",
        context={
            "user": user,
            "current_conv": conv,
            "participants": participants,
            "turns": turns,
            "sidebar_convos": sidebar_convos,
        },
    )


@router.post("/{cid}/delete")
async def multi_delete(
    cid: int,
    user: Annotated[User, Depends(auth.require_user)],
    session: Annotated[AsyncSession, Depends(db.get_session)],
):
    conv = await _assert_owned(session, user.id, cid)
    await session.delete(conv)  # cascade: messages + sub_responses
    return RedirectResponse("/multi", status_code=303)


@router.post("/{cid}/ask")
async def multi_ask(
    request: Request,
    cid: int,
    user: Annotated[User, Depends(auth.require_user)],
):
    """Receive a user question; fan out to all participants over SSE.

    Body: {"message": "..."}
    Output: multiplexed SSE stream — see multi_orchestrator for event schema.
    """
    body = await request.json()
    user_text = (body.get("message") or "").strip()
    if not user_text:
        return Response(status_code=400, content="empty message")

    # Validate ownership + resolve participants in our own session
    async with db._SessionFactory() as s:
        conv = await s.get(MultiConversation, cid)
        if conv is None or conv.user_id != user.id:
            return Response(status_code=404, content="conversation not found")
        slugs = _parse_participants(conv.participant_slugs)
        participants = await _resolve_participants(s, slugs)
        if not participants:
            return Response(status_code=400, content="no valid participants")

        # Auto-detect market topics; this can take a network round-trip so
        # offload to a thread (function is sync httpx).
        market_block = await asyncio.to_thread(market_context_block_for, user_text)

        # Persist user message first to get its id (sub_responses FK back to it)
        um = MultiMessage(conversation_id=cid, role="user", content=user_text)
        s.add(um)
        await s.flush()
        user_message_id = um.id

        # Set conversation title from first question, if it's still the default
        if conv.title == "(新多人对话)":
            conv.title = user_text.replace("\n", " ").strip()[:30] or "(新多人对话)"

        from datetime import datetime, timezone
        conv.updated_at = datetime.now(timezone.utc)
        await s.commit()

    async def gen():
        try:
            async for event in orchestrate(
                multi_conv_id=cid,
                bloggers=participants,
                user_text=user_text,
                market_context_block=market_block,
                user_message_id=user_message_id,
            ):
                yield event
        except Exception as exc:
            log.exception("multi: orchestrate failed for cid=%s", cid)
            yield (
                f"data: {json.dumps({'event': 'fatal_error', 'error': str(exc)}, ensure_ascii=False)}\n\n"
            )
            yield "data: [DONE]\n\n"

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
