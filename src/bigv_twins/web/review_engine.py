"""决策回顾引擎 — 自动定期回顾用户的投资决策。

APScheduler 每日 20:00 扫描 decision_journal WHERE status='active' AND next_review_at <= today，
自动生成回顾报告。
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import date, timedelta

from sqlalchemy import select

from bigv_twins.config import settings

from . import db
from .db import DecisionJournal, DecisionReview, TickerOpinionLog
from .daily_brief import get_watchlist_quotes

log = logging.getLogger("bigv_twins.web.review_engine")

# 中文动作标签 — 给模型的 prompt 用，比 'open'/'add' 这种英文 enum 更准确
_ACTION_ZH = {
    "open": "建仓（首次买入）",
    "add": "加仓",
    "reduce": "减仓",
    "close": "清仓",
    "retroactive": "补录（已持有的旧仓位）",
}


class _FakeW:
    def __init__(self, ticker):
        self.ticker = ticker
        self.name = ticker
        self.market = "A"
        self.note = ""
        self.id = 0


_REVIEW_PROMPT = """你是一个投资回顾助手。请基于以下信息为这笔交易写一份简短的事后回顾。

## 原始决策
- 标的：{ticker_name}（{ticker}）
- 操作：{action_zh}
- 决策日：{decision_date}
- 决策价：¥{decision_price}
- 决策理由：{reasoning_section}
{plan_section}
## 当前状态
- 当前价：¥{current_price}
- 距决策涨跌：{pnl_pct}
- 持有天数：{days_passed} 天
{opinions_section}
## 输出要求

用 Markdown 输出三段，限 200 字以内：

1. **表现回顾**：一句话概括 — 涨/跌多少，相对当时是赚是亏。
2. **逻辑验证**：{verify_instruction}
3. **建议思考**：给用户提 1 个值得反思的具体问题（不要泛泛的"是否要止盈"，要结合上面提到的事实）。

不要编造任何不在上述信息里的数据（PE、市值、行业新闻等都不要瞎说）。
"""


async def generate_review_for_journal(journal: DecisionJournal) -> str | None:
    """Generate one review report for a journal entry. Returns markdown or None."""
    # Get current price
    loop = asyncio.get_running_loop()
    quotes = await loop.run_in_executor(None, get_watchlist_quotes, [_FakeW(journal.ticker)])
    current_price = quotes[0].get("current") if quotes else None

    if not current_price or not journal.price_at_decision:
        return None

    pnl_pct = (current_price - journal.price_at_decision) / journal.price_at_decision * 100
    days_passed = (date.today() - journal.created_at.date()).days if journal.created_at else 0

    # Get recent opinions
    opinions_section = ""
    async with db._SessionFactory() as session:
        opinion_rows = await session.execute(
            select(TickerOpinionLog).where(
                TickerOpinionLog.ticker == journal.ticker,
                TickerOpinionLog.opinion_date > journal.created_at.strftime("%Y-%m-%d") if journal.created_at else "",
            ).order_by(TickerOpinionLog.opinion_date.desc()).limit(5)
        )
        opinions = list(opinion_rows.scalars())
        if opinions:
            opinions_section = "\n## 决策后的博主观点\n"
            for op in opinions:
                opinions_section += f"- {op.opinion_date} [{op.blogger_slug}] {op.sentiment}: {op.summary}\n"

    plan_section = ""
    if journal.action_detail:
        plan_section = f"- 操作计划：{journal.action_detail}\n"
    if journal.target_price:
        plan_section += f"- 目标价：¥{journal.target_price}\n"
    if journal.stop_loss_price:
        plan_section += f"- 止损价：¥{journal.stop_loss_price}\n"

    # 没填理由（比如 CSV 批量补录的）→ 改变 "逻辑验证" 的指令措辞
    if journal.reasoning and journal.reasoning.strip():
        reasoning_section = journal.reasoning[:300]
        verify_instruction = "结合上面的决策理由，看当初的判断现在站得住吗？"
    else:
        reasoning_section = "（用户没有记录当时的思路）"
        verify_instruction = (
            "用户当时没记录思路。结合决策后的博主观点和现价表现，"
            "推测当时的买入逻辑可能是什么，并说明现在看是否成立。"
        )

    prompt = _REVIEW_PROMPT.format(
        ticker_name=journal.ticker_name,
        ticker=journal.ticker,
        action_zh=_ACTION_ZH.get(journal.action, journal.action),
        decision_date=journal.created_at.strftime("%Y-%m-%d") if journal.created_at else "?",
        decision_price=f"{journal.price_at_decision:.2f}",
        reasoning_section=reasoning_section,
        plan_section=plan_section,
        current_price=f"{current_price:.2f}",
        pnl_pct=f"{pnl_pct:+.1f}%",
        days_passed=days_passed,
        opinions_section=opinions_section,
        verify_instruction=verify_instruction,
    )

    return await _call_qoder(prompt, journal.id)


async def _call_qoder(prompt: str, journal_id: int) -> str | None:
    """走 Qoder SDK performance 模式（推理重的任务比 flash 强很多，不会乱编 PE）。"""
    if not settings.qoder_personal_access_token:
        log.warning("review %d skipped: QODER_PERSONAL_ACCESS_TOKEN not set", journal_id)
        return None
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
        log.warning("qoder review failed for journal %d: %s", journal_id, e)
        return None
    text = "".join(pieces).strip()
    return text or None


# Review interval: 7→30→90→180 days
_REVIEW_INTERVALS = [7, 30, 90, 180]


async def run_scheduled_reviews() -> int:
    """Scan journals due for review, generate reports. Called daily by APScheduler."""
    today = date.today().strftime("%Y-%m-%d")
    count = 0

    async with db._SessionFactory() as session:
        rows = await session.execute(
            select(DecisionJournal).where(
                DecisionJournal.status == "active",
                DecisionJournal.next_review_at <= today,
            )
        )
        journals = list(rows.scalars())

    log.info("review engine: %d journals due for review", len(journals))

    for journal in journals:
        report_md = await generate_review_for_journal(journal)
        if not report_md:
            continue

        # Determine review type
        review_count = journal.review_count or 0
        if review_count == 0:
            review_type = "1week"
        elif review_count == 1:
            review_type = "1month"
        elif review_count == 2:
            review_type = "3month"
        else:
            review_type = "6month"

        # Get current price for record
        quotes = get_watchlist_quotes([_FakeW(journal.ticker)])
        current_price = quotes[0].get("current") if quotes else None
        pnl_pct = None
        if current_price and journal.price_at_decision:
            pnl_pct = (current_price - journal.price_at_decision) / journal.price_at_decision * 100

        # Save review
        async with db._SessionFactory() as session:
            review = DecisionReview(
                journal_id=journal.id,
                user_id=journal.user_id,
                review_type=review_type,
                current_price=current_price,
                price_change_pct=pnl_pct,
                review_report_md=report_md,
            )
            session.add(review)

            # Update journal: next review date + increment count
            j = await session.get(DecisionJournal, journal.id)
            j.review_count = review_count + 1
            next_idx = min(review_count + 1, len(_REVIEW_INTERVALS) - 1)
            j.next_review_at = (date.today() + timedelta(days=_REVIEW_INTERVALS[next_idx])).strftime("%Y-%m-%d")

            await session.commit()
            count += 1

    log.info("review engine: generated %d reviews", count)
    return count
