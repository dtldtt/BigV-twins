"""Sentiment extraction —— 历史 brief 回填专用。

v0.6 起，新生成的 brief 由 blogger_brief.summarize_blogger 一次性输出
ticker_opinions，不再走这里。这个模块保留给一次性回填脚本用
（180 条历史 brief 是老架构生成的，没带情绪标签）。

同样切到 Qoder performance — 跟 brief 生成保持质量一致。
"""

from __future__ import annotations

import json
import logging

from sqlalchemy import select, text

from bigv_twins.config import settings

from . import db
from .db import BloggerDailyBrief, TickerOpinionLog

log = logging.getLogger("bigv_twins.web.opinion_extractor")

_EXTRACT_PROMPT = """以下是投资博主「{blogger_name}」在 {date} 的当日观点摘要：

{brief_md}

该博主当日提到了这些股票（代码）：{tickers}

请逐只给出博主的态度。**忠于摘要原文，禁止外推**：
- 若摘要只是顺带提到某票而没明确表态 → neutral
- 若摘要明确说看好 / 建议买 / 看好后市 → bullish
- 若摘要明确说不看好 / 高估 / 预期下跌 → bearish
- 若摘要明确说不要碰 / 远离 / 风险大 → avoid

输出严格 JSON 数组（不要任何其他文字，不要 markdown 代码块）：
[{{"ticker":"代码","ticker_name":"名称","sentiment":"bullish|bearish|neutral|avoid","summary":"30字内贴原文摘要"}}]
"""


async def extract_opinions_from_brief(
    blogger_slug: str,
    blogger_name: str,
    brief_date: str,
    brief_md: str,
    mentioned_tickers: list[str],
    brief_id: int | None = None,
) -> int:
    """从已生成的 brief_md 反推每只 ticker 的情绪。用 Qoder performance。

    新生成流程不该调本函数（让 summarize_blogger 一次性输出更好），
    本函数留给历史回填脚本。
    """
    if not mentioned_tickers:
        return 0
    if not settings.qoder_personal_access_token:
        log.warning("opinion extract %s/%s skipped: QODER token not set", blogger_slug, brief_date)
        return 0

    prompt = _EXTRACT_PROMPT.format(
        blogger_name=blogger_name,
        date=brief_date,
        brief_md=brief_md,
        tickers=", ".join(mentioned_tickers),
    )

    response_text = await _call_qoder(prompt, blogger_slug, brief_date)
    if response_text is None:
        return 0
    response_text = response_text.strip()
    if response_text.startswith("```"):
        response_text = response_text.split("\n", 1)[1].rsplit("```", 1)[0]
    try:
        opinions = json.loads(response_text)
    except json.JSONDecodeError as e:
        log.warning("opinion JSON parse failed for %s/%s: %s — raw: %r",
                    blogger_slug, brief_date, e, response_text[:200])
        return 0

    if not isinstance(opinions, list):
        return 0

    count = 0
    async with db._SessionFactory() as session:
        for op in opinions:
            ticker = op.get("ticker", "")
            if not ticker:
                continue
            try:
                row = TickerOpinionLog(
                    ticker=ticker,
                    ticker_name=op.get("ticker_name", ticker),
                    blogger_slug=blogger_slug,
                    opinion_date=brief_date,
                    sentiment=op.get("sentiment", "neutral"),
                    summary=op.get("summary", "")[:100],
                    source_brief_id=brief_id,
                )
                session.add(row)
                await session.flush()
                count += 1
            except Exception:
                await session.rollback()
                # Likely UNIQUE constraint — already extracted for this date
                continue
        await session.commit()

    # Async backfill price_at_opinion
    if count > 0:
        try:
            from .daily_brief import get_watchlist_quotes
            class _FW:
                def __init__(s, t): s.ticker=t; s.name=t; s.market='A'; s.note=''; s.id=0
            tickers_to_fill = [op.get("ticker") for op in opinions if op.get("ticker")]
            if tickers_to_fill:
                import asyncio
                loop = asyncio.get_running_loop()
                quotes = await loop.run_in_executor(
                    None, get_watchlist_quotes, [_FW(t) for t in set(tickers_to_fill)]
                )
                price_map = {q["ticker"]: q.get("current") for q in quotes if q.get("ok")}
                async with db._SessionFactory() as s2:
                    for t, price in price_map.items():
                        if price:
                            await s2.execute(
                                text("UPDATE ticker_opinion_log SET price_at_opinion = :p "
                                     "WHERE ticker = :t AND opinion_date = :d AND price_at_opinion IS NULL"),
                                {"p": price, "t": t, "d": brief_date},
                            )
                    await s2.commit()
                log.info("backfilled prices for %d tickers", len(price_map))
        except Exception as e:
            log.warning("price backfill failed: %s", e)

    log.info("extracted %d opinions for %s/%s", count, blogger_slug, brief_date)
    return count


async def _call_qoder(prompt: str, blogger_slug: str, brief_date: str) -> str | None:
    try:
        from qoder_agent_sdk import (
            AssistantMessage, QoderAgentOptions, access_token, query,
        )
    except ImportError as e:
        log.warning("qoder_agent_sdk import failed: %s", e)
        return None
    options = QoderAgentOptions(
        auth=access_token(settings.qoder_personal_access_token),
        model="performance",
    )
    pieces: list[str] = []
    try:
        async for msg in query(prompt=prompt, options=options):
            if isinstance(msg, AssistantMessage):
                content = getattr(msg, "content", None)
                if isinstance(content, list):
                    for c in content:
                        if isinstance(c, dict) and c.get("type") == "text":
                            pieces.append(c.get("text", ""))
                        elif hasattr(c, "text"):
                            pieces.append(c.text)
                elif isinstance(content, str):
                    pieces.append(content)
    except Exception as e:
        log.warning("qoder opinion extract failed for %s/%s: %s",
                    blogger_slug, brief_date, e)
        return None
    text = "".join(pieces).strip()
    return text or None
