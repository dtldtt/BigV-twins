"""Chat routes: blogger list, per-blogger page, conversation pages, SSE ask."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response, StreamingResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bigv_twins.config import BLOGGERS, BY_SLUG, Blogger
from bigv_twins.market_data import (
    detect_topics as md_detect,
    format_market_context_for_prompt as md_format,
    get_market_context as md_get,
)
from bigv_twins.prompt_loader import load_prompt

from . import auth, db, openclaw_client, qoder_call
from .db import BloggerOverride, Conversation, Message, User

log = logging.getLogger("bigv_twins.web.chat")
router = APIRouter(prefix="/chat")
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

# Recap 阈值配置
RECAP_FIRST_THRESHOLD = 8   # 首次触发 recap 的消息数
RECAP_UPDATE_INTERVAL = 6   # 之后每增加这么多消息更新一次 recap
RECAP_RECENT_ROUNDS = 3     # 使用 recap 时保留的最近对话轮数（1 轮 = user + assistant）


# ------------------------------------------------------------ helpers

async def hidden_slugs(session: AsyncSession) -> set[str]:
    result = await session.execute(
        select(BloggerOverride.slug).where(BloggerOverride.hidden.is_(True))
    )
    return {r[0] for r in result.all()}


def _ordered(bloggers: list[Blogger]) -> list[Blogger]:
    """Display order on /chat (left → right, top → bottom):
        1. Real archived bloggers (kind='blogger')   — in bloggers.json order
        2. Masters (kind='master', e.g. Buffett)    — in bloggers.json order
        3. Advisors (kind='advisor')                — always last

    Defensive — if more bloggers/masters are added later, advisor stays
    pinned to the bottom and masters stay grouped between bloggers and advisor.
    Stable within each group.
    """
    bs = [b for b in bloggers if b.is_blogger]
    masters = [b for b in bloggers if b.is_master]
    advs = [b for b in bloggers if b.is_advisor]
    return bs + masters + advs


async def visible_bloggers(session: AsyncSession) -> list[Blogger]:
    hidden = await hidden_slugs(session)
    return _ordered([b for b in BLOGGERS if b.slug not in hidden])


async def assert_visible(session: AsyncSession, slug: str) -> Blogger:
    if slug not in BY_SLUG:
        raise HTTPException(status_code=404, detail="unknown blogger")
    hidden = await hidden_slugs(session)
    if slug in hidden:
        raise HTTPException(status_code=404, detail="blogger hidden")
    return BY_SLUG[slug]


def _prompt_vars(blogger: Blogger) -> dict[str, str]:
    return {"blogger_slug": blogger.slug, "blogger_name": blogger.name}


def system_prompt_for(blogger: Blogger, mode: str | None = None) -> str:
    if mode == "challenge" and blogger.is_master:
        return load_prompt("chat/master-challenge.md", **_prompt_vars(blogger))
    if blogger.is_advisor:
        return load_prompt("chat/advisor.md")
    if blogger.is_master:
        return load_prompt("chat/master.md", **_prompt_vars(blogger))
    return load_prompt("chat/blogger.md", **_prompt_vars(blogger))


async def list_user_conversations(
    session: AsyncSession, user_id: int, slug: str | None = None, limit: int = 50,
) -> list[Conversation]:
    q = select(Conversation).where(Conversation.user_id == user_id)
    if slug:
        q = q.where(Conversation.blogger_slug == slug)
    q = q.order_by(Conversation.updated_at.desc()).limit(limit)
    result = await session.execute(q)
    return list(result.scalars())


# ------------------------------------------------------------ routes

@router.get("/", response_class=HTMLResponse)
async def chat_home(
    request: Request,
    user: Annotated[User, Depends(auth.require_user)],
    session: Annotated[AsyncSession, Depends(db.get_session)],
):
    hidden = await hidden_slugs(session)
    bloggers = _ordered([b for b in BLOGGERS if b.slug not in hidden])
    recent_all = await list_user_conversations(session, user.id, limit=50)
    recent = [c for c in recent_all if c.blogger_slug not in hidden][:15]
    return templates.TemplateResponse(
        request=request,
        name="chat/index.html",
        context={"user": user, "bloggers": bloggers, "recent": recent},
    )


@router.get("/{slug}", response_class=HTMLResponse)
async def blogger_page(
    request: Request,
    slug: str,
    user: Annotated[User, Depends(auth.require_user)],
    session: Annotated[AsyncSession, Depends(db.get_session)],
):
    blogger = await assert_visible(session, slug)
    convos = await list_user_conversations(session, user.id, slug=slug)
    if convos:
        return RedirectResponse(f"/chat/{slug}/{convos[0].id}", status_code=303)
    # No convo yet: show empty state on the same template
    return templates.TemplateResponse(
        request=request,
        name="chat/blogger.html",
        context={
            "user": user,
            "blogger": blogger,
            "bloggers": await visible_bloggers(session),
            "convos": [],
            "current_conv": None,
            "messages": [],
        },
    )


@router.post("/{slug}/new")
async def new_conversation(
    slug: str,
    request: Request,
    user: Annotated[User, Depends(auth.require_user)],
    session: Annotated[AsyncSession, Depends(db.get_session)],
):
    await assert_visible(session, slug)
    mode = request.query_params.get("mode")
    title = "(检验模式)" if mode == "challenge" else "(新对话)"
    conv = Conversation(
        user_id=user.id, blogger_slug=slug, title=title,
        mode=mode if mode in ("challenge",) else None,
    )
    session.add(conv)
    await session.flush()
    return RedirectResponse(f"/chat/{slug}/{conv.id}", status_code=303)


@router.get("/{slug}/{cid}", response_class=HTMLResponse)
async def conversation_page(
    request: Request,
    slug: str,
    cid: int,
    user: Annotated[User, Depends(auth.require_user)],
    session: Annotated[AsyncSession, Depends(db.get_session)],
):
    blogger = await assert_visible(session, slug)
    conv = await session.get(Conversation, cid)
    if conv is None or conv.user_id != user.id or conv.blogger_slug != slug:
        raise HTTPException(status_code=404, detail="conversation not found")
    msg_rows = await session.execute(
        select(Message).where(Message.conversation_id == cid).order_by(Message.created_at)
    )
    convos = await list_user_conversations(session, user.id, slug=slug)
    return templates.TemplateResponse(
        request=request,
        name="chat/blogger.html",
        context={
            "user": user,
            "blogger": blogger,
            "bloggers": await visible_bloggers(session),
            "convos": convos,
            "current_conv": conv,
            "messages": list(msg_rows.scalars()),
        },
    )


@router.post("/{cid}/delete")
async def delete_conversation(
    cid: int,
    user: Annotated[User, Depends(auth.require_user)],
    session: Annotated[AsyncSession, Depends(db.get_session)],
):
    conv = await session.get(Conversation, cid)
    if conv is None or conv.user_id != user.id:
        raise HTTPException(status_code=404)
    slug = conv.blogger_slug
    await session.delete(conv)
    return RedirectResponse(f"/chat/{slug}", status_code=303)


# ============================================================================
# Recap 生成
# ============================================================================

async def _generate_recap(cid: int) -> None:
    """生成或更新对话 recap。异步调用 OpenClaw，完成后写入 DB。"""
    try:
        # 读取完整对话历史
        async with db._SessionFactory() as session:
            conv = await session.get(Conversation, cid)
            if conv is None:
                return

            msg_rows = await session.execute(
                select(Message)
                .where(Message.conversation_id == cid)
                .order_by(Message.created_at)
            )
            messages = list(msg_rows.scalars())
            total_msg_count = len(messages)

        # 检查是否满足生成条件
        if total_msg_count < RECAP_FIRST_THRESHOLD:
            return
        if conv.recap and conv.recap_msg_count >= total_msg_count - RECAP_UPDATE_INTERVAL:
            return  # recap 还够新，不需要更新

        # 格式化对话历史
        history_lines = []
        for m in messages:
            role_label = "用户" if m.role == "user" else "助手"
            history_lines.append(f"【{role_label}】\n{m.content}\n")
        history_text = "\n".join(history_lines)

        # 加载 recap prompt
        recap_system_prompt = load_prompt("recap.md")

        # 调用 Qoder 生成 recap（避免 OpenClaw agent loop 的系统 prompt 冲突）
        full_prompt = f"{recap_system_prompt}\n\n以下是完整对话历史：\n\n{history_text}"

        recap_text = await qoder_call.call_qoder(
            prompt=full_prompt,
            task_type="recap",
            task_detail=f"cid={cid}",
            model="auto",
        )

        if not recap_text:
            log.warning("recap generation returned empty for cid=%s", cid)
            return

        recap_text = recap_text.strip()

        # 写入 DB
        async with db._SessionFactory() as session:
            conv = await session.get(Conversation, cid)
            if conv is not None:
                conv.recap = recap_text
                conv.recap_updated_at = datetime.now(timezone.utc)
                conv.recap_msg_count = total_msg_count
                await session.commit()
                log.info("generated recap for cid=%s (%d chars, %d msgs)",
                        cid, len(recap_text), total_msg_count)

    except Exception:
        log.exception("recap generation failed for cid=%s", cid)


# ============================================================================
# In-flight chat state — survives client disconnect
# ============================================================================
# Per-conversation dict tracking the active background LLM task.
# Cleaned up 60s after task completion to allow late reconnects.
_INFLIGHT: dict[int, dict] = {}


async def _run_chat_background(cid: int, messages: list, target_model: str):
    """Background task: stream LLM, accumulate to buf, save to DB.

    Runs independently of the HTTP request — client disconnect does NOT cancel.
    SSE handlers subscribe to state["queue"] for live deltas.
    """
    state = _INFLIGHT[cid]
    buf = state["buf"]
    queue = state["queue"]
    try:
        async for delta in openclaw_client.stream_chat(messages, model=target_model):
            buf.append(delta)
            # Push to all current subscribers (non-blocking)
            try:
                queue.put_nowait(("delta", delta))
            except asyncio.QueueFull:
                pass  # subscriber too slow; they'll get the full buf on next poll
    except Exception as exc:
        log.exception("background LLM failed for cid=%s", cid)
        state["error"] = str(exc)
        try:
            queue.put_nowait(("error", str(exc)))
        except asyncio.QueueFull:
            pass
    finally:
        state["done"].set()
        try:
            queue.put_nowait(("done", None))
        except asyncio.QueueFull:
            pass
        # Persist full reply to DB (independent of client)
        full = "".join(buf).strip()
        if full:
            try:
                async with db._SessionFactory() as s:
                    s.add(Message(conversation_id=cid, role="assistant", content=full))
                    conv = await s.get(Conversation, cid)
                    if conv is not None:
                        conv.updated_at = datetime.now(timezone.utc)
                    await s.commit()
                log.info("persisted assistant msg for cid=%s (%d chars)", cid, len(full))
            except Exception:
                log.exception("DB save failed for cid=%s", cid)

        # 触发 recap 生成（如果满足条件）
        asyncio.create_task(_generate_recap(cid))

        # Keep state alive for 60s so late reconnects can get the full reply
        await asyncio.sleep(60)
        _INFLIGHT.pop(cid, None)


async def _stream_from_inflight(cid: int):
    """SSE generator: subscribe to an active background task's stream.

    Replays any already-accumulated buffer first, then follows new deltas.
    """
    state = _INFLIGHT.get(cid)
    if not state:
        yield "data: [DONE]\n\n"
        return

    # Replay buffer (for reconnect after disconnect)
    if state["buf"]:
        joined = "".join(state["buf"])
        yield f"data: {json.dumps({'delta': joined}, ensure_ascii=False)}\n\n"

    # If already done, finish
    if state["done"].is_set():
        if state.get("error"):
            yield f"data: {json.dumps({'error': state['error']}, ensure_ascii=False)}\n\n"
        yield "data: [DONE]\n\n"
        return

    # Subscribe to new deltas via the queue. Each SSE handler creates its own
    # cursor by tracking the buf length it has already seen.
    last_idx = len(state["buf"])
    while not state["done"].is_set():
        # Wait a bit for new chunks; if buf grew, emit the new portion
        await asyncio.sleep(0.1)
        if len(state["buf"]) > last_idx:
            new_chunks = state["buf"][last_idx:]
            last_idx = len(state["buf"])
            joined = "".join(new_chunks)
            yield f"data: {json.dumps({'delta': joined}, ensure_ascii=False)}\n\n"
        if state.get("error"):
            yield f"data: {json.dumps({'error': state['error']}, ensure_ascii=False)}\n\n"
            break

    # Final flush: anything that arrived between last check and done
    if len(state["buf"]) > last_idx:
        new_chunks = state["buf"][last_idx:]
        joined = "".join(new_chunks)
        yield f"data: {json.dumps({'delta': joined}, ensure_ascii=False)}\n\n"

    yield "data: [DONE]\n\n"


@router.get("/{cid}/stream")
async def chat_stream_reconnect(
    cid: int,
    user: Annotated[User, Depends(auth.require_user)],
):
    """Reconnect to an in-flight LLM response.

    Used when user navigates away during a response then comes back.
    Frontend calls this on page load if last user msg has no assistant reply yet.
    """
    # Verify ownership
    async with db._SessionFactory() as session:
        conv = await session.get(Conversation, cid)
        if conv is None or conv.user_id != user.id:
            return Response(status_code=404, content="conversation not found")

    return StreamingResponse(
        _stream_from_inflight(cid),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/{cid}/ask")
async def ask(
    request: Request,
    cid: int,
    user: Annotated[User, Depends(auth.require_user)],
):
    """Append a user message; stream the assistant reply over SSE.

    Body: {"message": "..."}.  Response: SSE chunks `{"delta": "..."}` and a final
    `[DONE]`.  Server persists user msg before streaming and assistant msg after.
    """
    body = await request.json()
    user_text = (body.get("message") or "").strip()
    if not user_text:
        return Response(status_code=400, content="empty message")

    # Build the message history in our own session, then close it before streaming.
    async with db._SessionFactory() as session:
        conv = await session.get(Conversation, cid)
        if conv is None or conv.user_id != user.id:
            return Response(status_code=404, content="conversation not found")
        if conv.blogger_slug in await hidden_slugs(session):
            return Response(status_code=404, content="blogger hidden")
        blogger = BY_SLUG.get(conv.blogger_slug)
        if blogger is None:
            return Response(status_code=400, content="invalid blogger")

        msg_rows = await session.execute(
            select(Message)
            .where(Message.conversation_id == cid)
            .order_by(Message.created_at)
        )
        history = list(msg_rows.scalars())

        # Auto-detect macro topics in the new user message → fetch context →
        # append to system prompt so agent has it without needing a tool call.
        sys_prompt = system_prompt_for(blogger, mode=conv.mode)
        detected = md_detect(user_text)
        if detected:
            log.info("auto-detected market topics for cid=%s: %s", cid, detected)
            try:
                ctx = await asyncio.to_thread(md_get, detected)
                block = md_format(ctx)
                if block:
                    sys_prompt = sys_prompt + "\n\n" + block
            except Exception:
                log.exception("market_data fetch failed; continuing without context")

        # 构建 messages：如果消息数 >= 阈值且有 recap，使用 recap + 最近几轮
        total_msg_count = len(history) + 1  # +1 是即将加入的 user msg

        # Fallback: 即使没有 recap，超过 20 条消息也强制截断，只保留最近 6 轮
        if total_msg_count >= 20 and not conv.recap:
            log.warning("no recap for cid=%s but %d msgs — forcing truncation", cid, total_msg_count)
            recent_msgs = history[-12:] if len(history) >= 12 else history  # 最近 6 轮 = 12 条
            messages = [{"role": "system", "content": sys_prompt}]
            for m in recent_msgs:
                messages.append({"role": m.role, "content": m.content})
            messages.append({"role": "user", "content": user_text})

        elif total_msg_count >= RECAP_FIRST_THRESHOLD and conv.recap:
            # 检查 recap 是否还够新
            if conv.recap_msg_count >= total_msg_count - RECAP_UPDATE_INTERVAL:
                # 使用 recap + 最近 N 轮对话
                recap_block = f"[以下是本次对话的历史摘要，请在此基础上继续回答]\n\n{conv.recap}"

                # 取最近 N 轮（每轮 = user + assistant = 2 条消息）
                recent_msgs = history[-(RECAP_RECENT_ROUNDS * 2):] if len(history) >= RECAP_RECENT_ROUNDS * 2 else history

                messages = [
                    {"role": "system", "content": sys_prompt},
                    {"role": "user", "content": recap_block},
                ]
                for m in recent_msgs:
                    messages.append({"role": m.role, "content": m.content})
                messages.append({"role": "user", "content": user_text})

                log.info("using recap for cid=%s (total=%d, recap_msgs=%d, recent=%d)",
                        cid, total_msg_count, conv.recap_msg_count, len(recent_msgs))
            else:
                # recap 太旧了，先用完整历史，等这次回答完会触发更新
                messages = [{"role": "system", "content": sys_prompt}]
                for m in history:
                    messages.append({"role": m.role, "content": m.content})
                messages.append({"role": "user", "content": user_text})
        else:
            # 消息数不够或没有 recap，使用完整历史
            messages = [{"role": "system", "content": sys_prompt}]
            for m in history:
                messages.append({"role": m.role, "content": m.content})
            messages.append({"role": "user", "content": user_text})

        # Route to per-blogger OpenClaw agent (default "bigv" for archived bloggers,
        # "advisor" for the AI advisor card; configured via bloggers.json).
        target_model = f"openclaw/{blogger.agent}"

        # Persist user msg + maybe set title before streaming starts
        session.add(Message(conversation_id=cid, role="user", content=user_text))
        if conv.title == "(新对话)":
            conv.title = user_text.replace("\n", " ").strip()[:20] or "(新对话)"
        conv.updated_at = datetime.now(timezone.utc)
        await session.commit()

    # Spawn LLM as a detached background task — survives client disconnect.
    # 如果之前的 task 已完成（成功或失败），清理它再创建新 task
    if cid in _INFLIGHT:
        state = _INFLIGHT[cid]
        if state["done"].is_set():
            _INFLIGHT.pop(cid, None)
            log.info("cleaned up completed inflight state for cid=%s", cid)

    if cid not in _INFLIGHT:
        _INFLIGHT[cid] = {
            "buf": [],
            "queue": asyncio.Queue(maxsize=2000),
            "done": asyncio.Event(),
            "error": None,
        }
        asyncio.create_task(_run_chat_background(cid, messages, target_model))

    return StreamingResponse(
        _stream_from_inflight(cid),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
