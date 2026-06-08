"""投资成长复盘引擎 — 把单笔级的 review 升级为用户级跨时段综合复盘。

数据流：
  - 全部 closed journals + active journals 实时盈亏
  - 全部 self_critique（用户事后自评）
  - 全部 investment_notes（随笔）
  - 历史 decision_review 报告（让引擎知道用户之前的判断）
        ↓
  Python 算客观快照（盈亏 / 胜率 / 持仓天数 / vs 沪深300 等，硬数据 LLM 容易算错）
        ↓
  Qoder performance 写 4 段成长报告，**严格成长型语言**（禁贬损）
        ↓
  growth_reports 表 + /growth 页面

语气基线：观察者+引导者，不是裁判。亏损是信息不是错误。优势同样要说。
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections import defaultdict
from datetime import date, datetime, timedelta

from sqlalchemy import select, func

from bigv_twins.config import settings
from bigv_twins.prompt_loader import load_prompt

from . import db
from .db import (
    DecisionJournal, DecisionReview, InvestmentNote, GrowthReport,
)
from .daily_brief import get_watchlist_quotes

log = logging.getLogger("bigv_twins.web.reflection_engine")


class _FakeW:
    def __init__(self, ticker):
        self.ticker = ticker
        self.name = ticker
        self.market = "A"
        self.note = ""
        self.id = 0


_GROWTH_PROMPT = load_prompt("review/monthly-review.md")


# ============================================================================
# Stats computation
# ============================================================================

async def compute_period_stats(
    user_id: int, period_start: date, period_end: date,
) -> tuple[dict, list, list]:
    """计算客观数据快照。

    返回 (stats_dict, closed_trades_in_period, active_trades_snapshot).
    """
    start_str = period_start.strftime("%Y-%m-%d")
    end_str = period_end.strftime("%Y-%m-%d")

    async with db._SessionFactory() as s:
        # 本期 closed（按 closed_at 落在窗口内）
        rows = await s.execute(
            select(DecisionJournal).where(
                DecisionJournal.user_id == user_id,
                DecisionJournal.status == "closed",
                DecisionJournal.closed_at.isnot(None),
                DecisionJournal.closed_at >= datetime.combine(period_start, datetime.min.time()),
                DecisionJournal.closed_at <= datetime.combine(period_end, datetime.max.time()),
            )
        )
        closed = list(rows.scalars())

        # 全部 active（当前在手）
        rows = await s.execute(
            select(DecisionJournal).where(
                DecisionJournal.user_id == user_id,
                DecisionJournal.status == "active",
            )
        )
        active = list(rows.scalars())

    # === 已平仓统计 ===
    # 按 ticker 聚合一下，避免 close/open/add/reduce 等同票多行重复算
    # 简化口径：以 close action 为准（一次清仓 = 一笔完整交易）
    closed_trades = [t for t in closed if t.action == "close"]

    realized_pnl_yuan = 0.0
    win_count = 0
    loss_count = 0
    hold_days_list: list[int] = []
    for t in closed_trades:
        if t.price_at_decision is None or t.closed_price is None or t.shares is None:
            continue
        # close action 的 price_at_decision 是卖出价；需要找该 ticker 的 open / add 平均成本
        # 简化：用 (closed_price - 该 ticker 在窗口内首次买入价) * shares
        async with db._SessionFactory() as s:
            buy_rows = await s.execute(
                select(DecisionJournal).where(
                    DecisionJournal.user_id == user_id,
                    DecisionJournal.ticker == t.ticker,
                    DecisionJournal.action.in_(["open", "add", "retroactive"]),
                ).order_by(DecisionJournal.created_at)
            )
            buys = list(buy_rows.scalars())
        if not buys:
            continue
        total_cost = sum((b.price_at_decision or 0) * (b.shares or 0) for b in buys)
        total_shares = sum(b.shares or 0 for b in buys)
        if total_shares == 0:
            continue
        avg_cost = total_cost / total_shares
        pnl = (t.closed_price - avg_cost) * (t.shares or 0)
        realized_pnl_yuan += pnl
        if pnl > 0:
            win_count += 1
        else:
            loss_count += 1
        if buys[0].created_at and t.closed_at:
            hold_days_list.append((t.closed_at - buys[0].created_at).days)

    win_rate = (win_count / (win_count + loss_count) * 100) if (win_count + loss_count) > 0 else None
    avg_hold = sum(hold_days_list) / len(hold_days_list) if hold_days_list else None

    # === active 浮盈浮亏 ===
    # 每个 active ticker 取最早 buy 的均成本，跟实时价对比
    active_tickers = list({t.ticker for t in active})
    quotes = {}
    if active_tickers:
        loop = asyncio.get_running_loop()
        q_list = await loop.run_in_executor(
            None, get_watchlist_quotes, [_FakeW(t) for t in active_tickers]
        )
        for q in q_list:
            if q.get("ok") and q.get("current"):
                quotes[q["ticker"]] = q["current"]

    # 按 ticker 聚合 active
    by_ticker: dict[str, list] = defaultdict(list)
    for t in active:
        by_ticker[t.ticker].append(t)

    active_snapshot = []
    unrealized_pnl_yuan = 0.0
    for ticker, lots in by_ticker.items():
        buys = [l for l in lots if l.action in ("open", "add", "retroactive")]
        reduces = [l for l in lots if l.action == "reduce"]
        total_cost = sum((b.price_at_decision or 0) * (b.shares or 0) for b in buys)
        total_buy_shares = sum(b.shares or 0 for b in buys)
        sold_shares = sum(r.shares or 0 for r in reduces)
        cur_shares = total_buy_shares - sold_shares
        if total_buy_shares == 0 or cur_shares <= 0:
            continue
        avg_cost = total_cost / total_buy_shares
        cur_price = quotes.get(ticker)
        market_value = cur_shares * (cur_price or avg_cost)
        pnl = (cur_price - avg_cost) * cur_shares if cur_price else 0.0
        unrealized_pnl_yuan += pnl
        latest = sorted(lots, key=lambda x: x.created_at, reverse=True)[0]
        active_snapshot.append({
            "ticker": ticker,
            "ticker_name": latest.ticker_name,
            "shares": cur_shares,
            "avg_cost": avg_cost,
            "current_price": cur_price,
            "market_value": market_value,
            "pnl": pnl,
            "pnl_pct": ((cur_price / avg_cost - 1) * 100) if cur_price else None,
        })

    # === 沪深300 同期 ===
    csi_ret = None
    try:
        from .backtest import _fetch_benchmark_hist, _get_close_on_or_after
        df = await asyncio.get_running_loop().run_in_executor(
            None, _fetch_benchmark_hist,
            period_start.strftime("%Y%m%d"),
            (period_end + timedelta(days=1)).strftime("%Y%m%d"),
        )
        if df is not None and len(df) > 0:
            start = _get_close_on_or_after(df, start_str)
            end = _get_close_on_or_after(df, end_str)
            if start and end:
                csi_ret = (end[1] / start[1] - 1.0) * 100.0
    except Exception as e:
        log.warning("csi300 fetch for period stats failed: %s", e)

    stats = {
        "period_start": start_str,
        "period_end": end_str,
        "closed_trade_count": len(closed_trades),
        "active_position_count": len(active_snapshot),
        "realized_pnl_yuan": round(realized_pnl_yuan, 2),
        "unrealized_pnl_yuan": round(unrealized_pnl_yuan, 2),
        "win_count": win_count,
        "loss_count": loss_count,
        "win_rate_pct": round(win_rate, 1) if win_rate is not None else None,
        "avg_hold_days": round(avg_hold, 1) if avg_hold is not None else None,
        "csi300_return_pct": round(csi_ret, 2) if csi_ret is not None else None,
    }

    return stats, closed_trades, active_snapshot


# ============================================================================
# Report generation
# ============================================================================

def _format_stats_md(stats: dict) -> str:
    lines = [
        f"- 周期：{stats['period_start']} → {stats['period_end']}",
        f"- 本期清仓交易：{stats['closed_trade_count']} 笔",
        f"- 当前在手仓位：{stats['active_position_count']} 只",
        f"- 已实现盈亏：¥{stats['realized_pnl_yuan']:+.2f}",
        f"- 浮动盈亏：¥{stats['unrealized_pnl_yuan']:+.2f}",
    ]
    if stats["win_rate_pct"] is not None:
        lines.append(f"- 本期胜率：{stats['win_rate_pct']}%（{stats['win_count']} 赚 / {stats['loss_count']} 亏）")
    if stats["avg_hold_days"] is not None:
        lines.append(f"- 平均持仓天数：{stats['avg_hold_days']} 天")
    if stats["csi300_return_pct"] is not None:
        lines.append(f"- 同期沪深300涨跌：{stats['csi300_return_pct']:+.2f}%")
    return "\n".join(lines)


def _format_closed_md(closed_trades: list) -> str:
    if not closed_trades:
        return "（本期无清仓交易）"
    lines = []
    for t in closed_trades[:30]:  # cap
        d = t.closed_at.strftime("%Y-%m-%d") if t.closed_at else "?"
        lines.append(
            f"- [{d}] {t.ticker_name} ({t.ticker}) 清仓 ¥{t.closed_price or '—'} × {t.shares or '—'}股"
            f"，原因：{t.closed_reason or '（未填）'}"
        )
    if len(closed_trades) > 30:
        lines.append(f"... 另有 {len(closed_trades) - 30} 笔")
    return "\n".join(lines)


def _format_active_md(active_snapshot: list) -> str:
    if not active_snapshot:
        return "（无在手仓位）"
    lines = []
    for p in sorted(active_snapshot, key=lambda x: x.get("market_value", 0), reverse=True)[:30]:
        pnl_str = f"{p['pnl_pct']:+.1f}%" if p["pnl_pct"] is not None else "—"
        lines.append(
            f"- {p['ticker_name']} ({p['ticker']}) {p['shares']}股，"
            f"成本 ¥{p['avg_cost']:.2f}，现价 ¥{p['current_price'] or '—'}，"
            f"浮盈 {pnl_str}"
        )
    return "\n".join(lines)


async def _fetch_critiques_and_notes(user_id: int) -> tuple[str, str]:
    """读全部 self_critique 和 notes，按时间倒序拼成 markdown。"""
    async with db._SessionFactory() as s:
        rows = await s.execute(
            select(DecisionJournal.ticker_name, DecisionJournal.ticker,
                   DecisionJournal.self_critique, DecisionJournal.created_at)
            .where(
                DecisionJournal.user_id == user_id,
                DecisionJournal.self_critique.isnot(None),
            )
            .order_by(DecisionJournal.created_at.desc())
            .limit(30)
        )
        crit_lines = []
        for name, code, crit, ct in rows:
            d = ct.strftime("%Y-%m-%d") if ct else "?"
            crit_lines.append(f"- [{d}] {name} ({code})：{crit}")
        critiques_md = "\n".join(crit_lines) if crit_lines else "（用户暂未写过自评）"

        note_rows = await s.execute(
            select(InvestmentNote.content, InvestmentNote.created_at)
            .where(InvestmentNote.user_id == user_id)
            .order_by(InvestmentNote.created_at.desc())
            .limit(20)
        )
        note_lines = []
        for content, ct in note_rows:
            d = ct.strftime("%Y-%m-%d") if ct else "?"
            note_lines.append(f"- [{d}] {content}")
        notes_md = "\n".join(note_lines) if note_lines else "（用户暂未写过随笔）"

    return critiques_md, notes_md


async def _fetch_prior_reviews(user_id: int, since: date) -> str:
    """读最近一段时间内的 single-journal review，让引擎不要跟自己重复结论。"""
    since_dt = datetime.combine(since, datetime.min.time())
    async with db._SessionFactory() as s:
        rows = await s.execute(
            select(DecisionReview.review_report_md, DecisionReview.created_at,
                   DecisionJournal.ticker_name, DecisionJournal.ticker)
            .join(DecisionJournal, DecisionReview.journal_id == DecisionJournal.id)
            .where(
                DecisionReview.user_id == user_id,
                DecisionReview.created_at >= since_dt,
                DecisionReview.review_report_md.isnot(None),
            )
            .order_by(DecisionReview.created_at.desc())
            .limit(10)
        )
        lines = []
        for md, ct, name, code in rows:
            d = ct.strftime("%Y-%m-%d") if ct else "?"
            excerpt = (md or "")[:200].replace("\n", " ")
            lines.append(f"- [{d}] {name} ({code})：{excerpt}...")
    return "\n".join(lines) if lines else "（本期暂无单笔 AI 回顾报告）"


async def _compute_allocation(active_snapshot: list, stats: dict) -> str:
    """资产配置 + 集中度 + 现金占比 一段 md."""
    if not active_snapshot:
        return "（当前无持仓）"
    total_mv = sum(p.get("market_value") or 0 for p in active_snapshot)
    if total_mv <= 0:
        return "（持仓市值为 0）"
    # Top 3 集中度
    sorted_p = sorted(active_snapshot, key=lambda x: x.get("market_value") or 0, reverse=True)
    top1 = sorted_p[0]
    top1_pct = (top1["market_value"] or 0) / total_mv * 100
    top3_mv = sum((p["market_value"] or 0) for p in sorted_p[:3])
    top3_pct = top3_mv / total_mv * 100
    lines = [
        f"- 持仓只数：{len(active_snapshot)} 只",
        f"- 单仓最重：{top1['ticker_name']}（{top1['ticker']}）占持仓 {top1_pct:.1f}%",
        f"- Top 3 集中度：{top3_pct:.1f}%",
    ]
    return "\n".join(lines)


async def _fetch_prev_period_data(user_id: int, period_start: date, period_end: date):
    """计算上一同等周期的 stats + critiques。"""
    delta = period_end - period_start
    prev_end = period_start - timedelta(days=1)
    prev_start = prev_end - delta
    prev_stats, prev_closed, prev_active = await compute_period_stats(user_id, prev_start, prev_end)

    # 上期的 self_critique 拉一遍（按 created_at 落在上期窗口）
    async with db._SessionFactory() as s:
        rows = await s.execute(
            select(DecisionJournal.ticker_name, DecisionJournal.ticker,
                   DecisionJournal.self_critique, DecisionJournal.created_at)
            .where(
                DecisionJournal.user_id == user_id,
                DecisionJournal.self_critique.isnot(None),
                DecisionJournal.created_at >= datetime.combine(prev_start, datetime.min.time()),
                DecisionJournal.created_at <= datetime.combine(prev_end, datetime.max.time()),
            )
            .order_by(DecisionJournal.created_at.desc())
            .limit(30)
        )
        crit_lines = []
        for name, code, crit, ct in rows:
            d = ct.strftime("%Y-%m-%d") if ct else "?"
            crit_lines.append(f"- [{d}] {name} ({code})：{crit}")
    prev_critiques_md = "\n".join(crit_lines) if crit_lines else "（上期暂无自评）"
    return prev_stats, prev_critiques_md


async def _fetch_prior_growth_report(user_id: int) -> str:
    """拉用户最近一份成长报告核心结论（最多取前 800 字摘要）。"""
    async with db._SessionFactory() as s:
        rows = await s.execute(
            select(GrowthReport)
            .where(GrowthReport.user_id == user_id)
            .order_by(GrowthReport.created_at.desc())
            .limit(1)
        )
        r = rows.scalar_one_or_none()
    if not r:
        return "（无历史成长报告 — 这是首份）"
    parts = [f"上一份报告：{r.period_start} → {r.period_end} ({r.period_type})"]
    if r.report_md:
        parts.append("摘要：" + (r.report_md[:800]).replace("\n", " ") + ("…" if len(r.report_md) > 800 else ""))
    if r.key_lessons_json:
        try:
            ls = json.loads(r.key_lessons_json)
            if ls:
                parts.append("当时提炼出的可执行规则：" + " / ".join(ls))
        except json.JSONDecodeError:
            pass
    return "\n".join(parts)


async def generate_growth_report(
    user_id: int, period_type: str,
    period_start: date, period_end: date,
) -> GrowthReport | None:
    """生成一份成长复盘报告并写入 growth_reports 表。返回 ORM 对象。"""
    stats, closed, active = await compute_period_stats(user_id, period_start, period_end)
    critiques_md, notes_md = await _fetch_critiques_and_notes(user_id)
    prior_reviews_md = await _fetch_prior_reviews(user_id, period_start)
    # v0.7+: 上一期对比数据
    prev_stats, prev_critiques_md = await _fetch_prev_period_data(user_id, period_start, period_end)
    allocation_md = await _compute_allocation(active, stats)
    prior_growth_report_md = await _fetch_prior_growth_report(user_id)

    prompt = _GROWTH_PROMPT.format(
        stats_md=_format_stats_md(stats),
        prev_stats_md=_format_stats_md(prev_stats),
        allocation_md=allocation_md,
        closed_trades_md=_format_closed_md(closed),
        active_trades_md=_format_active_md(active),
        critiques_md=critiques_md,
        notes_md=notes_md,
        prev_critiques_md=prev_critiques_md,
        prior_growth_report_md=prior_growth_report_md,
        prior_reviews_md=prior_reviews_md,
    )

    from .qoder_call import call_qoder as _qoder
    report_text = await _qoder(prompt, "monthly_review", f"user{user_id}/{period_type}")
    if not report_text:
        return None

    # 抓 JSON 块里的 key_lessons
    key_lessons: list[str] = []
    import re
    m = re.search(r"```json\s*(\{.*?\})\s*```", report_text, re.S)
    if m:
        try:
            obj = json.loads(m.group(1))
            if isinstance(obj.get("key_lessons"), list):
                key_lessons = [str(x)[:80] for x in obj["key_lessons"]][:10]
        except json.JSONDecodeError:
            pass

    async with db._SessionFactory() as s:
        report = GrowthReport(
            user_id=user_id,
            period_type=period_type,
            period_start=period_start.strftime("%Y-%m-%d"),
            period_end=period_end.strftime("%Y-%m-%d"),
            report_md=report_text,
            stats_json=json.dumps(stats, ensure_ascii=False),
            key_lessons_json=json.dumps(key_lessons, ensure_ascii=False),
        )
        s.add(report)
        await s.commit()
        await s.refresh(report)
    log.info("growth report generated: user=%d period=%s %s~%s, lessons=%d",
             user_id, period_type, period_start, period_end, len(key_lessons))
    return report



# _call_qoder removed — now uses shared qoder_call.call_qoder


# ============================================================================
# Periodic schedulers
# ============================================================================

def _last_month_window(today: date | None = None) -> tuple[date, date]:
    """上个月 1 号 → 上个月最后一天。"""
    today = today or date.today()
    first_this = today.replace(day=1)
    last_prev = first_this - timedelta(days=1)
    first_prev = last_prev.replace(day=1)
    return first_prev, last_prev


def _last_quarter_window(today: date | None = None) -> tuple[date, date]:
    """上一个完整季度。"""
    today = today or date.today()
    q = (today.month - 1) // 3 + 1
    if q == 1:
        return date(today.year - 1, 10, 1), date(today.year - 1, 12, 31)
    start_month = (q - 2) * 3 + 1
    end_month = start_month + 2
    last_day = 31 if end_month in (3, 12) else 30
    return date(today.year, start_month, 1), date(today.year, end_month, last_day)


async def run_monthly_growth_reports() -> int:
    """月度 cron — 给每个有交易的用户生成上个月的成长报告。"""
    start, end = _last_month_window()
    count = 0
    async with db._SessionFactory() as s:
        rows = await s.execute(
            select(DecisionJournal.user_id).distinct()
        )
        user_ids = [r[0] for r in rows]
    for uid in user_ids:
        try:
            r = await generate_growth_report(uid, "month", start, end)
            if r:
                count += 1
        except Exception as e:
            log.exception("monthly report failed user=%d: %s", uid, e)
    return count


async def run_quarterly_growth_reports() -> int:
    start, end = _last_quarter_window()
    count = 0
    async with db._SessionFactory() as s:
        rows = await s.execute(select(DecisionJournal.user_id).distinct())
        user_ids = [r[0] for r in rows]
    for uid in user_ids:
        try:
            r = await generate_growth_report(uid, "quarter", start, end)
            if r:
                count += 1
        except Exception as e:
            log.exception("quarterly report failed user=%d: %s", uid, e)
    return count
