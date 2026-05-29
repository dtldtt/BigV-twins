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


_REVIEW_PROMPT = """你是一个投资回顾助手。下面是一笔交易的所有相关数据，请生成事后回顾。

## 原始决策
- 标的：{ticker_name}（{ticker}）
- 操作：{action_zh}
- 决策日：{decision_date}
- 决策价：¥{decision_price}
- 决策理由：{reasoning_section}
{plan_section}{fundamentals_then_section}
## 当前状态
- 当前价：¥{current_price}
- 距决策涨跌：{pnl_pct}
- 持有天数：{days_passed} 天
{fundamentals_now_section}{benchmark_section}{opinions_section}{self_critique_section}
## 输出要求

用 Markdown 输出 4 段，总长 300 字以内：

1. **表现回顾**：客观数据复述 — 涨跌幅、跑赢/跑输沪深300多少、估值（PE/PB）变化、博主情绪变化。一段话讲完。

2. **逻辑验证**：{verify_instruction}

3. **结合你自己的反思**：{self_critique_instruction}

4. **下一步建议**：基于上面所有客观数据，给出一个具体可执行的建议（继续持有 / 加仓 / 减仓 / 清仓），并说明理由。不要含糊地说"密切关注"。

【硬约束】
- 不要编造任何不在上面数据里的信息（PE / 市值 / 行业新闻 / 财报数字 / 同行对比都禁止瞎说）
- {reasoning_constraint}
"""


def _format_fundamentals_then(journal: DecisionJournal) -> str:
    """从 journal.stock_snapshot 拼决策时基本面。没快照就返回 ''。"""
    if not journal.stock_snapshot:
        return ""
    try:
        snap = json.loads(journal.stock_snapshot)
    except (json.JSONDecodeError, TypeError):
        return ""
    bits = []
    if snap.get("pe") is not None:
        bits.append(f"PE {snap['pe']:.1f}")
    if snap.get("pb") is not None:
        bits.append(f"PB {snap['pb']:.2f}")
    if snap.get("market_cap") is not None:
        bits.append(f"市值 {snap['market_cap']:.0f} 亿")
    if not bits:
        return ""
    return f"- 决策时基本面：{' / '.join(bits)}\n"


def _format_fundamentals_now(quote: dict) -> str:
    bits = []
    if quote.get("pe") is not None:
        bits.append(f"PE {quote['pe']:.1f}")
    if quote.get("pb") is not None:
        bits.append(f"PB {quote['pb']:.2f}")
    if quote.get("market_cap") is not None:
        bits.append(f"市值 {quote['market_cap']:.0f} 亿")
    if not bits:
        return ""
    return f"- 当前基本面：{' / '.join(bits)}\n"


def _fetch_csi300_return(start_date_str: str) -> float | None:
    """拉 start_date 到今天的沪深 300 涨跌幅 %。失败返回 None。"""
    try:
        from .backtest import _fetch_benchmark_hist, _get_close_on_or_after
        from datetime import datetime
        start_dt = datetime.strptime(start_date_str, "%Y-%m-%d").date()
        today_dt = date.today()
        df = _fetch_benchmark_hist(
            start_dt.strftime("%Y%m%d"),
            (today_dt + timedelta(days=1)).strftime("%Y%m%d"),
        )
        if df is None or len(df) == 0:
            return None
        start_close = _get_close_on_or_after(df, start_date_str)
        end_close = _get_close_on_or_after(df, today_dt.strftime("%Y-%m-%d"))
        if not start_close or not end_close:
            return None
        return (end_close[1] / start_close[1] - 1.0) * 100.0
    except Exception as e:
        log.warning("csi300 return fetch failed: %s", e)
        return None


async def generate_review_for_journal(journal: DecisionJournal) -> str | None:
    """Generate one review report for a journal entry. Returns markdown or None."""
    loop = asyncio.get_running_loop()
    quotes = await loop.run_in_executor(None, get_watchlist_quotes, [_FakeW(journal.ticker)])
    quote = quotes[0] if quotes else {}
    current_price = quote.get("current")

    if not current_price or not journal.price_at_decision:
        return None

    pnl_pct = (current_price - journal.price_at_decision) / journal.price_at_decision * 100
    days_passed = (date.today() - journal.created_at.date()).days if journal.created_at else 0

    # 基本面对比段
    fundamentals_then_section = _format_fundamentals_then(journal)
    fundamentals_now_section = _format_fundamentals_now(quote)

    # 沪深300 同期对比段
    benchmark_section = ""
    if journal.created_at:
        decision_date_str = journal.created_at.strftime("%Y-%m-%d")
        csi_ret = await loop.run_in_executor(None, _fetch_csi300_return, decision_date_str)
        if csi_ret is not None:
            excess = pnl_pct - csi_ret
            benchmark_section = (
                f"- 同期沪深300涨跌：{csi_ret:+.1f}%，本仓位超额 {excess:+.1f}%\n"
            )

    # 决策后博主观点
    opinions_section = ""
    async with db._SessionFactory() as session:
        opinion_rows = await session.execute(
            select(TickerOpinionLog).where(
                TickerOpinionLog.ticker == journal.ticker,
                TickerOpinionLog.opinion_date > (journal.created_at.strftime("%Y-%m-%d") if journal.created_at else ""),
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

    # 用户自评（self_critique）
    if journal.self_critique and journal.self_critique.strip():
        self_critique_section = f"\n## 用户事后自评（按时间累积）\n{journal.self_critique}\n"
        self_critique_instruction = (
            "用户已经写了上述自评。把它跟你看到的客观数据对照：哪些观察一致？"
            "哪些用户没注意到但数据能体现？给出一段综合性的反思（不是简单重复用户原话）。"
        )
    else:
        self_critique_section = ""
        self_critique_instruction = (
            "用户还没写过自评。基于客观数据指出一个最值得用户事后写一笔自评的点（"
            "比如：当初仓位是不是太重、卖飞了某个加仓机会、或者反过来没及时止损）。"
        )

    # reasoning 空时的硬约束 —— 用户明确要求：不要推测买入逻辑
    if journal.reasoning and journal.reasoning.strip():
        reasoning_section = journal.reasoning[:300]
        verify_instruction = "结合上面的决策理由，看当初的判断现在站得住吗？引用具体数字。"
        reasoning_constraint = "决策理由是用户自己写的，可以基于它做验证"
    else:
        reasoning_section = "（用户没有记录当时的思路）"
        verify_instruction = (
            "用户当时没记录思路。**严格禁止推测**当初的买入逻辑。"
            "本节请改成纯客观数据点评：涨跌幅、基本面变化、跟沪深300的差距，"
            "陈述事实，不要替用户脑补 \"当时可能是因为 PE 低\" 这类心理活动。"
        )
        reasoning_constraint = (
            "用户没记录理由 — **绝对不要推测**他当初为什么买（会误导他）。"
            "在 \"逻辑验证\" 段只复述客观数据，不要替他构造心理活动"
        )

    prompt = _REVIEW_PROMPT.format(
        ticker_name=journal.ticker_name,
        ticker=journal.ticker,
        action_zh=_ACTION_ZH.get(journal.action, journal.action),
        decision_date=journal.created_at.strftime("%Y-%m-%d") if journal.created_at else "?",
        decision_price=f"{journal.price_at_decision:.2f}",
        reasoning_section=reasoning_section,
        plan_section=plan_section,
        fundamentals_then_section=fundamentals_then_section,
        current_price=f"{current_price:.2f}",
        pnl_pct=f"{pnl_pct:+.1f}%",
        days_passed=days_passed,
        fundamentals_now_section=fundamentals_now_section,
        benchmark_section=benchmark_section,
        opinions_section=opinions_section,
        self_critique_section=self_critique_section,
        verify_instruction=verify_instruction,
        self_critique_instruction=self_critique_instruction,
        reasoning_constraint=reasoning_constraint,
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
