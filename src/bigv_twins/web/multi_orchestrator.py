"""Fan-out orchestration for multi-blogger conversations.

Given a user question and N selected blogger slugs:
  1. For each blogger, stream the answer in parallel via openclaw_client
  2. Multiplex all streams into one SSE flow (events tagged with blogger_slug)
  3. After all N blogger streams complete (success or error), generate a
     comparative summary by calling the `advisor` agent
  4. Persist everything to multi_sub_responses + multi_messages tables

The contract for SSE events emitted (all JSON after `data: `):

  {"event": "blogger_start",  "blogger": "eyu"}
  {"event": "blogger_delta",  "blogger": "eyu", "content": "茅台啊..."}
  {"event": "blogger_done",   "blogger": "eyu"}
  {"event": "blogger_error",  "blogger": "eyu", "error": "..."}
  {"event": "all_blogger_done"}
  {"event": "summary_delta",  "content": "..."}
  {"event": "summary_done"}
  [DONE]   (literal sentinel, not JSON)
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import AsyncIterator

from sqlalchemy import select

from bigv_twins.config import BY_SLUG, Blogger
from bigv_twins.market_data import (
    detect_topics as md_detect,
    format_market_context_for_prompt as md_format,
    get_market_context as md_get,
)

from . import db, openclaw_client
from .chat import system_prompt_for
from .db import MultiConversation, MultiMessage, MultiSubResponse

log = logging.getLogger("bigv_twins.web.multi_orchestrator")


def _sse(event: str, **kw) -> str:
    """Format one SSE event line."""
    return f"data: {json.dumps({'event': event, **kw}, ensure_ascii=False)}\n\n"


async def _build_messages_for_blogger(
    session,
    blogger: Blogger,
    conv_id: int,
    user_text: str,
    market_context_block: str | None,
) -> list[dict]:
    """For one blogger in this multi-conv, build their personal message thread.

    Each blogger sees ONLY their own prior sub_responses — they don't see what
    other bloggers said this turn (would cause voice contamination).
    """
    sys_prompt = system_prompt_for(blogger)
    if market_context_block:
        sys_prompt = sys_prompt + "\n\n" + market_context_block

    messages: list[dict] = [{"role": "system", "content": sys_prompt}]

    # Walk prior turns. For each role='user' MultiMessage in this conv (oldest
    # first), append it + this blogger's matching sub_response (if any, status=done).
    rows = await session.execute(
        select(MultiMessage)
        .where(MultiMessage.conversation_id == conv_id)
        .where(MultiMessage.role == "user")
        .order_by(MultiMessage.created_at)
    )
    prior_users = list(rows.scalars())
    for um in prior_users:
        messages.append({"role": "user", "content": um.content})
        sub = await session.execute(
            select(MultiSubResponse)
            .where(MultiSubResponse.user_message_id == um.id)
            .where(MultiSubResponse.blogger_slug == blogger.slug)
            .where(MultiSubResponse.status == "done")
        )
        sub_row = sub.scalar_one_or_none()
        if sub_row and sub_row.content:
            messages.append({"role": "assistant", "content": sub_row.content})

    messages.append({"role": "user", "content": user_text})
    return messages


async def _run_blogger_stream(
    blogger: Blogger,
    messages: list[dict],
    user_message_id: int,
    queue: asyncio.Queue,
) -> None:
    """Run one blogger's stream + push events into the shared queue.

    Always pushes a terminal event (`blogger_done` or `blogger_error`).
    Persists the full text to MultiSubResponse on completion.
    """
    target_model = f"openclaw/{blogger.agent}"
    buf: list[str] = []
    await queue.put(_sse("blogger_start", blogger=blogger.slug))
    error_msg: str | None = None
    try:
        async for delta in openclaw_client.stream_chat(messages, model=target_model):
            buf.append(delta)
            await queue.put(_sse("blogger_delta", blogger=blogger.slug, content=delta))
    except Exception as exc:
        error_msg = str(exc)[:300]
        log.exception("multi: blogger %s stream failed", blogger.slug)
        await queue.put(_sse("blogger_error", blogger=blogger.slug, error=error_msg))
    else:
        await queue.put(_sse("blogger_done", blogger=blogger.slug))
    finally:
        # Persist the sub_response regardless of success/failure
        full = "".join(buf).strip()
        try:
            async with db._SessionFactory() as s2:
                sub = MultiSubResponse(
                    user_message_id=user_message_id,
                    blogger_slug=blogger.slug,
                    content=full or "",
                    status="done" if error_msg is None else "error",
                    error_msg=error_msg,
                )
                s2.add(sub)
                await s2.commit()
        except Exception:
            log.exception("multi: failed to persist sub_response for %s", blogger.slug)


def _build_summary_prompt(question: str, responses: list[tuple[Blogger, str, str | None]]) -> str:
    """Build the system + user prompt for the summary LLM call.

    `responses` = list of (blogger, content_or_empty, error_or_none).
    """
    sys_prompt = (
        "你是「赛博大V」多人对话页面的**汇总者**——用户向 N 位投资视角（博主分身 / "
        "大师 / AI 投顾）问了同一个问题，已经拿到所有人的回答。你的任务是把这些"
        "视角**对照**起来，让用户一眼看出共识、分歧、和缺位。\n\n"
        "## 输出要求（严格遵守）\n\n"
        "1. **不要重复**任何博主的回答原文——他们的回答已经显示在你的上方。\n"
        "2. 用三段式输出：\n"
        "   - **对照表格**（markdown 表，列：视角 / 倾向 / 一句话观点 / 关键引用），"
        "每一行紧凑一句，让用户一眼看完。每位都要出现，包括失败/缺位的。\n"
        "   - **一致 / 分歧 / 缺位**三栏总结，列出哪些点共识、哪些点分歧、哪些视角缺位\n"
        "   - **综合判断**一段（≤ 80 字）：你作为中立汇总者的一句话观察，不下买卖断言\n"
        "3. **不要编造**博主没说过的观点 —— 严格基于上方文字\n"
        "4. **不要劝架** —— 分歧就是分歧，呈现差异比强行调和更有价值\n"
        "5. 标注「缺位 / 失败」：如果某位回答为空或错误，明确说「该视角未参与本轮」"
    )

    response_block_parts = [
        f"## 用户问题\n\n{question}\n\n## 各位视角的回答\n"
    ]
    for blogger, content, err in responses:
        head = f"\n### {blogger.name} (kind={blogger.kind}, slug={blogger.slug})\n"
        if err:
            response_block_parts.append(head + f"(回答失败：{err})\n")
        elif not content:
            response_block_parts.append(head + "(回答为空)\n")
        else:
            response_block_parts.append(head + content + "\n")
    user_prompt = "".join(response_block_parts) + (
        "\n---\n\n请按上方"
        "「对照表格 → 一致/分歧/缺位 → 综合判断」"
        "三段输出。"
    )
    return sys_prompt, user_prompt


async def _run_summary_stream(
    question: str,
    responses: list[tuple[Blogger, str, str | None]],
    multi_conv_id: int,
    queue: asyncio.Queue,
) -> None:
    """Generate the rollup summary via the advisor agent, stream to queue."""
    sys_prompt, user_prompt = _build_summary_prompt(question, responses)
    messages = [
        {"role": "system", "content": sys_prompt},
        {"role": "user", "content": user_prompt},
    ]
    buf: list[str] = []
    try:
        async for delta in openclaw_client.stream_chat(messages, model="openclaw/advisor"):
            buf.append(delta)
            await queue.put(_sse("summary_delta", content=delta))
    except Exception as exc:
        log.exception("multi: summary stream failed")
        await queue.put(_sse("summary_error", error=str(exc)[:300]))
    else:
        await queue.put(_sse("summary_done"))
    finally:
        full = "".join(buf).strip()
        if full:
            try:
                async with db._SessionFactory() as s3:
                    s3.add(MultiMessage(
                        conversation_id=multi_conv_id,
                        role="summary",
                        content=full,
                    ))
                    conv = await s3.get(MultiConversation, multi_conv_id)
                    if conv is not None:
                        conv.updated_at = datetime.now(timezone.utc)
                    await s3.commit()
            except Exception:
                log.exception("multi: failed to persist summary")


async def orchestrate(
    multi_conv_id: int,
    bloggers: list[Blogger],
    user_text: str,
    market_context_block: str | None,
    user_message_id: int,
) -> AsyncIterator[str]:
    """The SSE generator. Fans out N blogger streams, waits for all, then runs
    summary. Yields SSE-formatted lines suitable for StreamingResponse.

    Assumes:
      - The MultiMessage(role='user') row has already been written (id = user_message_id)
      - Each blogger's prior context is built independently by walking past sub_responses
    """
    # Pre-build each blogger's messages list (separate DB session, then close)
    blogger_messages: dict[str, list[dict]] = {}
    async with db._SessionFactory() as session:
        for b in bloggers:
            blogger_messages[b.slug] = await _build_messages_for_blogger(
                session, b, multi_conv_id, user_text, market_context_block,
            )

    # asyncio queue to merge all SSE events
    queue: asyncio.Queue = asyncio.Queue()
    sentinel = object()

    async def _runner_wrapper():
        # Fan out
        tasks = [
            asyncio.create_task(
                _run_blogger_stream(b, blogger_messages[b.slug], user_message_id, queue)
            )
            for b in bloggers
        ]
        await asyncio.gather(*tasks, return_exceptions=True)
        await queue.put(_sse("all_blogger_done"))

        # Build summary input from persisted sub_responses
        responses: list[tuple[Blogger, str, str | None]] = []
        async with db._SessionFactory() as s4:
            for b in bloggers:
                row = await s4.execute(
                    select(MultiSubResponse)
                    .where(MultiSubResponse.user_message_id == user_message_id)
                    .where(MultiSubResponse.blogger_slug == b.slug)
                )
                sub = row.scalar_one_or_none()
                if sub is None:
                    responses.append((b, "", "no row"))
                else:
                    responses.append((b, sub.content, sub.error_msg))
        await _run_summary_stream(user_text, responses, multi_conv_id, queue)

        # Final sentinel
        await queue.put(sentinel)

    runner_task = asyncio.create_task(_runner_wrapper())
    try:
        while True:
            item = await queue.get()
            if item is sentinel:
                break
            yield item
        yield "data: [DONE]\n\n"
    finally:
        if not runner_task.done():
            runner_task.cancel()


# ---------------------------------------------------------------- helpers


def market_context_block_for(user_text: str) -> str | None:
    """Auto-detect macro topics and pre-fetch a `市场环境` block — same logic
    as web/chat.py uses for single-blogger conversations. Synchronous network
    call wrapped in to_thread by caller.
    """
    topics = md_detect(user_text)
    if not topics:
        return None
    try:
        ctx = md_get(topics)
    except Exception:
        log.exception("market_data fetch failed")
        return None
    return md_format(ctx) or None
