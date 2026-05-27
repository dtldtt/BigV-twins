"""Sentiment extraction — runs after each blogger brief is generated.

Extracts per-ticker sentiment from the brief text using one LLM call,
writes to ticker_opinion_log table.
"""

from __future__ import annotations

import json
import logging

from sqlalchemy import select

from . import db, openclaw_client
from .db import BloggerDailyBrief, TickerOpinionLog

log = logging.getLogger("bigv_twins.web.opinion_extractor")

_EXTRACT_PROMPT = """以下是投资博主「{blogger_name}」在 {date} 的内容摘要：

{brief_md}

该博主当日提到了这些股票：{tickers}

请对其中每只股票给出博主的情感判断。
输出严格 JSON 数组，不要任何其他文字：
[{{"ticker":"股票代码","ticker_name":"名称","sentiment":"bullish|bearish|neutral|avoid","summary":"一句话概括博主对该股的看法"}}]

规则：
- sentiment 只能是 bullish（看多）/ bearish（看空）/ neutral（中性/观望）/ avoid（明确回避）
- summary 限 30 字以内
- 如果博主只是顺带提到但没明确态度，用 neutral
"""


async def extract_opinions_from_brief(
    blogger_slug: str,
    blogger_name: str,
    brief_date: str,
    brief_md: str,
    mentioned_tickers: list[str],
    brief_id: int | None = None,
) -> int:
    """Extract sentiment for each mentioned ticker. Returns count of new records."""
    if not mentioned_tickers:
        return 0

    prompt = _EXTRACT_PROMPT.format(
        blogger_name=blogger_name,
        date=brief_date,
        brief_md=brief_md,
        tickers=", ".join(mentioned_tickers),
    )

    try:
        response_text = ""
        async for delta in openclaw_client.stream_chat(
            [{"role": "user", "content": prompt}],
            model="openclaw/advisor",
        ):
            response_text += delta

        # Parse JSON from response
        response_text = response_text.strip()
        if response_text.startswith("```"):
            response_text = response_text.split("\n", 1)[1].rsplit("```", 1)[0]
        opinions = json.loads(response_text)
    except (json.JSONDecodeError, Exception) as e:
        log.warning("opinion extraction failed for %s/%s: %s", blogger_slug, brief_date, e)
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

    log.info("extracted %d opinions for %s/%s", count, blogger_slug, brief_date)
    return count
