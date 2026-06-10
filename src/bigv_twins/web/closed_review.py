"""已清仓标的月度复盘 — 每月 1 号 07:00 执行。

扫描上月清仓的所有 (user, ticker)，逐个生成复盘报告。
数据收集 → 行情摘要预计算 → Qoder auto → 存入 decision_review。
"""
from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta

from sqlalchemy import func, select, text

from bigv_twins.config import settings
from bigv_twins.prompt_loader import load_prompt

from . import db
from .db import DecisionJournal, DecisionReview, InvestmentNote
from .qoder_call import call_qoder

log = logging.getLogger("bigv_twins.web.closed_review")


def _last_month_range() -> tuple[str, str]:
    today = date.today()
    first_of_this = today.replace(day=1)
    last_of_prev = first_of_this - timedelta(days=1)
    first_of_prev = last_of_prev.replace(day=1)
    return first_of_prev.strftime("%Y-%m-%d"), last_of_prev.strftime("%Y-%m-%d")


async def _find_closed_tickers_in_period(start: str, end: str) -> list[tuple[int, str, str]]:
    """找上月清仓的 (user_id, ticker, ticker_name)。"""
    async with db._SessionFactory() as s:
        rows = await s.execute(
            select(
                DecisionJournal.user_id,
                DecisionJournal.ticker,
                DecisionJournal.ticker_name,
            )
            .where(DecisionJournal.action == "close")
            .where(DecisionJournal.created_at >= start)
            .where(DecisionJournal.created_at <= end + " 23:59:59")
            .distinct()
        )
        return [(r[0], r[1], r[2]) for r in rows]


async def _collect_trade_data(user_id: int, ticker: str) -> dict:
    """收集一笔完整交易的所有数据。"""
    async with db._SessionFactory() as s:
        rows = await s.execute(
            select(DecisionJournal)
            .where(DecisionJournal.user_id == user_id)
            .where(DecisionJournal.ticker == ticker)
            .order_by(DecisionJournal.created_at)
        )
        entries = list(rows.scalars())

    if not entries:
        return {}

    # 操作记录
    ops = []
    total_buy_cost = 0
    total_buy_shares = 0
    close_price = None
    close_date = None
    open_date = None
    dividend_total = 0

    for e in entries:
        op = {
            "action": e.action,
            "price": e.price_at_decision,
            "shares": e.shares or 0,
            "date": e.created_at.strftime("%Y-%m-%d") if e.created_at else "",
            "reasoning": e.reasoning or "",
            "plan": e.action_detail or "",
            "critique": e.self_critique or "",
        }
        ops.append(op)

        if e.action in ("open", "add", "buy"):
            total_buy_cost += (e.price_at_decision or 0) * (e.shares or 0)
            total_buy_shares += (e.shares or 0)
            if open_date is None:
                open_date = e.created_at
        elif e.action == "close":
            close_price = e.price_at_decision
            close_date = e.created_at
        elif e.action == "dividend":
            dividend_total += (e.price_at_decision or 0) * (e.shares or 0)

    if not total_buy_shares or not close_price or not open_date:
        return {}

    avg_price = total_buy_cost / total_buy_shares
    total_return = close_price * total_buy_shares + dividend_total
    pnl = total_return - total_buy_cost
    pnl_pct = pnl / total_buy_cost * 100
    hold_days = (close_date - open_date).days if close_date and open_date else 0
    simple_hold_pnl = (close_price - (entries[0].price_at_decision or avg_price)) / (entries[0].price_at_decision or avg_price) * 100

    # 随笔
    async with db._SessionFactory() as s:
        note_rows = await s.execute(
            select(InvestmentNote)
            .where(InvestmentNote.user_id == user_id)
            .where(InvestmentNote.created_at >= open_date)
            .where(InvestmentNote.created_at <= close_date)
            .order_by(InvestmentNote.created_at)
        )
        notes = list(note_rows.scalars())

    return {
        "ops": ops,
        "total_buy_cost": total_buy_cost,
        "total_buy_shares": total_buy_shares,
        "avg_price": avg_price,
        "close_price": close_price,
        "dividend_total": dividend_total,
        "total_return": total_return,
        "pnl": pnl,
        "pnl_pct": pnl_pct,
        "hold_days": hold_days,
        "simple_hold_pnl": simple_hold_pnl,
        "open_date": open_date.strftime("%Y-%m-%d") if open_date else "",
        "close_date": close_date.strftime("%Y-%m-%d") if close_date else "",
        "notes": notes,
    }


def _fetch_market_summary(ticker: str, start: str, end: str) -> dict:
    """拉持仓期间的行情摘要（akshare）。失败返回空 dict。"""
    try:
        import akshare as ak
        import pandas as pd

        df = ak.stock_zh_a_hist(
            symbol=ticker, period="daily",
            start_date=start.replace("-", ""),
            end_date=end.replace("-", ""),
            adjust="qfq",
        )
        if df is None or df.empty:
            return {}

        close = df["收盘"].astype(float)
        high = df["最高"].astype(float)
        low = df["最低"].astype(float)

        return {
            "max_price": float(high.max()),
            "max_date": str(df.loc[high.idxmax(), "日期"]),
            "min_price": float(low.min()),
            "min_date": str(df.loc[low.idxmin(), "日期"]),
            "total_days": len(close),
        }
    except Exception as e:
        log.warning("market summary failed for %s: %s", ticker, e)
        return {}


def _build_prompt_input(ticker_name: str, ticker: str, trade: dict, market: dict) -> str:
    """构建喂给 LLM 的完整输入。"""
    parts = [f"## 操作记录\n\n标的：{ticker_name} {ticker}\n"]

    for op in trade["ops"]:
        action_label = {"open": "建仓", "add": "加仓", "reduce": "减仓",
                        "close": "清仓", "dividend": "分红", "buy": "买入"}.get(op["action"], op["action"])
        line = f"### {action_label} {op['date']} ¥{op['price']:.2f} × {op['shares']}股" if op["price"] else f"### {action_label} {op['date']}"
        parts.append(line)
        if op["reasoning"]:
            parts.append(f"操作思路：{op['reasoning']}")
        if op["plan"]:
            parts.append(f"操作计划：{op['plan']}")
        if op["critique"]:
            parts.append(f"用户自评：{op['critique']}")
        if not op["reasoning"] and not op["critique"] and op["action"] != "dividend":
            parts.append("用户未记录操作理由和自评。")
        parts.append("")

    # 客观交易数据
    parts.append("## 客观交易数据")
    parts.append(f"- 持有时间：{trade['open_date']} ~ {trade['close_date']}（{trade['hold_days']} 天）")
    parts.append(f"- 总投入：¥{trade['total_buy_cost']:.0f}")
    parts.append(f"- 买入均价：¥{trade['avg_price']:.2f}")
    parts.append(f"- 清仓价：¥{trade['close_price']:.2f}")
    if trade["dividend_total"] > 0:
        parts.append(f"- 分红收入：¥{trade['dividend_total']:.0f}")
    parts.append(f"- 总回收：¥{trade['total_return']:.0f}")
    parts.append(f"- 盈亏：¥{trade['pnl']:+.0f}（{trade['pnl_pct']:+.1f}%）")
    parts.append(f"- 简单持有收益（建仓价→清仓价）：{trade['simple_hold_pnl']:+.1f}%")
    parts.append("")

    # 行情摘要
    if market:
        parts.append("## 持仓期间行情摘要")
        parts.append(f"- 区间最高：¥{market['max_price']:.2f}（{market['max_date']}）")
        parts.append(f"- 区间最低：¥{market['min_price']:.2f}（{market['min_date']}）")
        max_profit = (market["max_price"] - trade["avg_price"]) / trade["avg_price"] * 100
        max_loss = (market["min_price"] - trade["avg_price"]) / trade["avg_price"] * 100
        parts.append(f"- 最大浮盈：{max_profit:+.1f}%")
        parts.append(f"- 最大浮亏：{max_loss:+.1f}%")
        parts.append("")
    else:
        parts.append("## 持仓期间行情摘要\n（行情数据暂不可用）\n")

    # 随笔
    if trade["notes"]:
        parts.append("## 用户同期投资随笔")
        for n in trade["notes"]:
            dt = n.created_at.strftime("%Y-%m-%d") if n.created_at else ""
            parts.append(f"- {dt}：{n.content[:200]}")
    else:
        parts.append("## 用户同期投资随笔\n无。")

    return "\n".join(parts)


async def review_one_closed(user_id: int, ticker: str, ticker_name: str) -> dict:
    """对一笔已清仓交易生成复盘。"""
    trade = await _collect_trade_data(user_id, ticker)
    if not trade:
        return {"status": "skip", "ticker": ticker, "reason": "no trade data"}

    import asyncio
    market = await asyncio.to_thread(
        _fetch_market_summary, ticker,
        trade["open_date"], trade["close_date"]
    )

    prompt = load_prompt("review/closed-review.md")
    user_input = _build_prompt_input(ticker_name, ticker, trade, market)
    full_prompt = prompt + "\n\n---\n\n" + user_input

    log.info("closed review for user=%d ticker=%s, input %d chars", user_id, ticker, len(user_input))
    report = await call_qoder(full_prompt, "closed_review", f"{ticker}/{ticker_name}", model="auto")

    if not report:
        return {"status": "error", "ticker": ticker, "reason": "qoder returned empty"}

    # 存到 decision_review
    async with db._SessionFactory() as s:
        # 找该 ticker 最新的一条 journal 的 id（decision_review 需要 journal_id）
        latest = await s.execute(
            select(DecisionJournal.id)
            .where(DecisionJournal.user_id == user_id)
            .where(DecisionJournal.ticker == ticker)
            .order_by(DecisionJournal.created_at.desc())
            .limit(1)
        )
        jid = latest.scalar_one_or_none() or 0

        review = DecisionReview(
            journal_id=jid,
            user_id=user_id,
            ticker=ticker,
            review_type="closed",
            current_price=trade["close_price"],
            review_report_md=report,
        )
        s.add(review)
        await s.commit()

    log.info("closed review saved: user=%d ticker=%s, %d chars", user_id, ticker, len(report))
    return {"status": "generated", "ticker": ticker, "chars": len(report)}


async def run_monthly_closed_reviews() -> dict:
    """月度入口 — 扫描上月清仓的标的，逐个生成复盘。"""
    start, end = _last_month_range()
    log.info("scanning closed tickers for %s ~ %s", start, end)

    closed = await _find_closed_tickers_in_period(start, end)
    log.info("found %d closed (user, ticker) pairs", len(closed))

    results = {}
    for user_id, ticker, ticker_name in closed:
        # 检查是否已有 closed review
        async with db._SessionFactory() as s:
            existing = await s.execute(
                select(DecisionReview.id)
                .where(DecisionReview.user_id == user_id)
                .where(DecisionReview.ticker == ticker)
                .where(DecisionReview.review_type == "closed")
                .limit(1)
            )
            if existing.scalar_one_or_none() is not None:
                results[f"{user_id}/{ticker}"] = {"status": "skipped", "reason": "already exists"}
                continue

        try:
            r = await review_one_closed(user_id, ticker, ticker_name)
            results[f"{user_id}/{ticker}"] = r
        except Exception as e:
            log.exception("closed review failed: user=%d ticker=%s: %s", user_id, ticker, e)
            results[f"{user_id}/{ticker}"] = {"status": "error", "reason": str(e)}

    log.info("monthly closed reviews done: %s", results)
    return results
