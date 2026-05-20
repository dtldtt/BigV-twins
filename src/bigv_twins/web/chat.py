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

from . import auth, db, openclaw_client
from .db import BloggerOverride, Conversation, Message, User

log = logging.getLogger("bigv_twins.web.chat")
router = APIRouter(prefix="/chat")
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


# ------------------------------------------------------------ helpers

async def hidden_slugs(session: AsyncSession) -> set[str]:
    result = await session.execute(
        select(BloggerOverride.slug).where(BloggerOverride.hidden.is_(True))
    )
    return {r[0] for r in result.all()}


async def visible_bloggers(session: AsyncSession) -> list[Blogger]:
    hidden = await hidden_slugs(session)
    return [b for b in BLOGGERS if b.slug not in hidden]


async def assert_visible(session: AsyncSession, slug: str) -> Blogger:
    if slug not in BY_SLUG:
        raise HTTPException(status_code=404, detail="unknown blogger")
    hidden = await hidden_slugs(session)
    if slug in hidden:
        raise HTTPException(status_code=404, detail="blogger hidden")
    return BY_SLUG[slug]


def system_prompt_for(blogger: Blogger) -> str:
    return (
        f"你**就是**投资博主「{blogger.name}」(slug: {blogger.slug})。"
        "用户在问你问题。你以你自己的视角、用你自己的口吻回答。\n\n"
        "## 回答前必须执行（顺序重要）\n\n"
        "1. **如果用户的问题里提到具体股票 / 标的**（出现 6 位代码、5 位港股代码、"
        "或常见股票名如 茅台 / 宁德时代 / 中芯国际 / 腾讯）：\n"
        "   **先**调 `bigv-twins.get_stock_snapshot`，参数 `{\"query\": <用户提到的股票>}`，"
        "拿当前 PE/PB/市值/控股结构/主营/大盘最近 10 天行情。这是真实数字，"
        "你的量化阈值（市值、PE、控股性质等）必须对照这些数字判断。\n"
        "   **如果用户问的是宏观/板块/资产**（如港股/黄金/AI/煤炭/新能源等），"
        "通常 system prompt 末尾已经自动附了「市场环境」段（系统帮你查了），"
        "**不要重复调** `get_market_context`。如要补充另一个主题再调。\n"
        f"2. 调 `bigv-twins.get_persona`，参数 `{{\"blogger\": \"{blogger.slug}\"}}`，"
        "读你自己的风格画像——投资框架、关注领域、典型用词、口头禅。这就是「你的特征」。\n"
        f"3. 调 `bigv-twins.search`，参数 `{{\"blogger\": \"{blogger.slug}\", "
        "\"query\": <用户问题原文或改写>, \"top_k\": 5}}`，检索你过往说过的相关内容。\n\n"
        "## 回答结构\n\n"
        "**如果有任何已采集的数据**（system prompt 末尾的「市场环境」段，"
        "或你调用 get_stock_snapshot / get_market_context 拿到的）：\n"
        "回答开头先给一段简短的「**市场速览**」（≤ 6 行），把这些真实数字精炼出来，比如：\n\n"
        "> 在回答之前我先看了下数据：\n"
        "> - 茅台 600519：现价 1324、PE 20、PB 6.1、市值 1.66 万亿、贵州省国资委控股 48.96%\n"
        "> - 上证指数最近 10 天 4160→4170（微涨）；港股恒生最近 1 周 +1.2%\n"
        "\n"
        "然后**再开始用第一人称回答**用户的问题，把这些数字逐条对照你的方法论。\n\n"
        "## 内容底线（不可妥协）\n\n"
        "- **只能基于 search/get_recent 返回的真实片段 + get_stock_snapshot/get_market_context 的真实数字**说话。\n"
        "- **每个观点必须能溯源**——引用必须**只用 search 实际返回的标题 + URL**，"
        "**禁止虚构标题**（不要把「《XX 宝典》」「《XX 心法》」这种臆测的书名当成自己写过的）。\n"
        "- 引用格式示意（`<具体标题>` 和 `<URL>` 必须替换为 search 返回的真实值）：\n"
        "    - 「我在《<具体标题>》里讲过 → [原文](<URL>)」\n"
        "    - 「<YYYY-MM-DD> 那条想法里说过 → [原文](<URL>)」\n"
        "- 检索不到相关内容时：诚实说「这个我之前没具体聊过这只股票」，"
        "**然后**可以用你的框架（持仓原则、行业偏好、估值阈值）"
        "对照 snapshot 的数字给出**初步判断**——这是合理外推，不是编造。\n"
        "- **绝不**伪造引文、伪造 URL、把没说过的具体观点说成是自己说的。\n\n"
        "## 风格（模仿你自己）\n\n"
        "- 用**第一人称**：「我认为」「我之前讲过」「在我看来」「我个人是不……的」。\n"
        "- 模仿你的语气、用词、比喻——persona 里「表达习惯」一段有真实引文，"
        "多用那种句式和口头禅。\n"
        f"- **不要**写「根据 {blogger.name}……」「{blogger.name} 认为……」"
        f"「以下基于归档」——**你就是 {blogger.name}**，这种第三人称叙述是错的。\n"
        "- 用户追问时延续同一身份，不要中途切回第三人称。\n\n"
        f"硬约束：blogger 参数必须始终是 \"{blogger.slug}\"。不要调其他博主的工具。"
    )


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
    bloggers = [b for b in BLOGGERS if b.slug not in hidden]
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
    user: Annotated[User, Depends(auth.require_user)],
    session: Annotated[AsyncSession, Depends(db.get_session)],
):
    await assert_visible(session, slug)
    conv = Conversation(
        user_id=user.id, blogger_slug=slug, title="(新对话)",
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
        sys_prompt = system_prompt_for(blogger)
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

        messages = [{"role": "system", "content": sys_prompt}]
        for m in history:
            messages.append({"role": m.role, "content": m.content})
        messages.append({"role": "user", "content": user_text})

        # Persist user msg + maybe set title before streaming starts
        session.add(Message(conversation_id=cid, role="user", content=user_text))
        if conv.title == "(新对话)":
            conv.title = user_text.replace("\n", " ").strip()[:20] or "(新对话)"
        conv.updated_at = datetime.now(timezone.utc)
        await session.commit()

    async def gen():
        buf: list[str] = []
        try:
            try:
                async for delta in openclaw_client.stream_chat(messages):
                    buf.append(delta)
                    yield f"data: {json.dumps({'delta': delta}, ensure_ascii=False)}\n\n"
            except Exception as exc:
                log.exception("stream_chat failed for cid=%s", cid)
                yield (
                    f"data: {json.dumps({'error': str(exc)}, ensure_ascii=False)}\n\n"
                )
            yield "data: [DONE]\n\n"
        finally:
            # Persist whatever we received even if the client disconnected mid-stream.
            full = "".join(buf).strip()
            if full:
                try:
                    async with db._SessionFactory() as s2:
                        s2.add(Message(conversation_id=cid, role="assistant", content=full))
                        conv2 = await s2.get(Conversation, cid)
                        if conv2 is not None:
                            conv2.updated_at = datetime.now(timezone.utc)
                        await s2.commit()
                except Exception:
                    log.exception("failed to persist assistant msg for cid=%s", cid)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
