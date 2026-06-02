"""A 股分红自动同步 — 拉历史分红 → 找用户持仓期内的事件
→ 自动新建 action='dividend' 的 journal 行 + 累加到 user.cny_dividend。

只处理 A 股个股（含主板/创业板/科创板）。ETF 分红逻辑另一套（etf_dividend.py），
HK 暂不支持。

数据源：akshare.stock_history_dividend_detail
派息字段单位：每 10 股派 X 元（A 股惯例）

幂等：检查 (user_id, ticker, action='dividend', ex_date) 三元组，已存在跳过
"""
from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, timedelta

import pandas as pd
from sqlalchemy import select

from . import db
from .db import DecisionJournal, User
from bigv_twins.stock_data import resolve_ticker, _is_etf

log = logging.getLogger("bigv_twins.web.dividend_sync")


def _fetch_a_share_dividends(ticker: str) -> list[dict]:
    """返回已实施的分红列表 [{ex_date: 'YYYY-MM-DD', per_10: float}]"""
    import akshare as ak
    try:
        df = ak.stock_history_dividend_detail(symbol=ticker, indicator="分红")
    except Exception as e:
        log.warning("fetch dividend failed for %s: %s", ticker, e)
        return []
    out = []
    for _, row in df.iterrows():
        if row.get("进度") != "实施":
            continue
        ex = row.get("除权除息日")
        if ex is None or pd.isna(ex):
            continue
        per_10 = row.get("派息")
        if not per_10 or pd.isna(per_10) or per_10 == 0:
            continue
        try:
            out.append({"ex_date": str(ex)[:10], "per_10": float(per_10)})
        except (ValueError, TypeError):
            continue
    return out


async def _shares_held_on(user_id: int, ticker: str, ex_date_str: str) -> int:
    """重建 user 在 ex_date 这一天对该 ticker 的持仓股数。

    取截至 ex_date 当日（含）的所有 journal 操作，按时间顺序累计。
    cycle 边界：close 重置归零。
    """
    cutoff = datetime.fromisoformat(ex_date_str)
    async with db._SessionFactory() as s:
        rows = await s.execute(
            select(DecisionJournal).where(
                DecisionJournal.user_id == user_id,
                DecisionJournal.ticker == ticker,
                DecisionJournal.created_at <= cutoff,
            ).order_by(DecisionJournal.created_at)
        )
        entries = list(rows.scalars())
    shares = 0
    for j in entries:
        n = j.shares or 0
        if j.action in ("open", "add"):
            shares += n
        elif j.action == "retroactive":
            shares = n
        elif j.action == "reduce":
            shares -= n
        elif j.action == "close":
            shares = 0
        # dividend 不影响股数
    return max(0, shares)


async def sync_user_dividends(user_id: int) -> dict:
    """对该用户的所有 A 股 ticker 同步历史分红。"""
    async with db._SessionFactory() as s:
        rows = await s.execute(
            select(DecisionJournal.ticker)
            .where(DecisionJournal.user_id == user_id)
            .distinct()
        )
        tickers = [r[0] for r in rows]

    a_share_tickers = []
    for t in tickers:
        info = resolve_ticker(t)
        if not info:
            continue
        if info.market != "a-share":
            continue
        if info.board == "etf":
            continue  # ETF 走单独的 etf_dividend.py
        a_share_tickers.append(t)

    log.info("syncing dividends for user=%d, tickers=%d (A股 only, no ETF)",
             user_id, len(a_share_tickers))
    n_new = 0
    n_skip = 0
    total_amount = 0.0
    detail = []

    for ticker in a_share_tickers:
        divs = await asyncio.to_thread(_fetch_a_share_dividends, ticker)
        if not divs:
            continue

        for d in divs:
            ex_date = d["ex_date"]
            per_10 = d["per_10"]
            shares = await _shares_held_on(user_id, ticker, ex_date)
            if shares <= 0:
                continue

            ex_dt = datetime.fromisoformat(ex_date)
            div_per_share = per_10 / 10.0
            total_div = div_per_share * shares

            async with db._SessionFactory() as s:
                # 幂等检查
                existing = await s.scalar(
                    select(DecisionJournal.id).where(
                        DecisionJournal.user_id == user_id,
                        DecisionJournal.ticker == ticker,
                        DecisionJournal.action == "dividend",
                        DecisionJournal.created_at == ex_dt,
                    ).limit(1)
                )
                if existing:
                    n_skip += 1
                    continue

                # ticker_name 取最新一条 journal
                tname = await s.scalar(
                    select(DecisionJournal.ticker_name)
                    .where(
                        DecisionJournal.user_id == user_id,
                        DecisionJournal.ticker == ticker,
                    )
                    .order_by(DecisionJournal.created_at.desc())
                    .limit(1)
                )

                # 新建 dividend 记录
                entry = DecisionJournal(
                    user_id=user_id,
                    ticker=ticker,
                    ticker_name=tname or ticker,
                    action="dividend",
                    price_at_decision=div_per_share,
                    shares=shares,
                    reasoning=f"A 股现金分红：每 10 股派 ¥{per_10:.2f}（除权日 {ex_date}），持仓 {shares} 股共得 ¥{total_div:.2f}（毛额，未扣税）",
                    status="active",
                    created_at=ex_dt,
                )
                s.add(entry)
                # v0.7: 不再维护 user.cny_dividend — 改由 /journal 路由实时 SUM
                # （SUM 会过滤掉未来除权日的分红，自然处理 "未到账" 状态）
                await s.commit()
                n_new += 1
                total_amount += total_div
                detail.append({
                    "ticker": ticker, "name": tname or ticker,
                    "ex_date": ex_date, "shares": shares,
                    "per_10": per_10, "amount": total_div,
                })
                log.info("dividend recorded: %s/%s ex=%s shares=%d amount=%.2f",
                         tname or ticker, ticker, ex_date, shares, total_div)

    return {
        "new": n_new, "skipped": n_skip, "total_amount": total_amount,
        "detail": detail,
    }


async def sync_all_users_dividends() -> dict:
    """daily cron 入口 — 给所有有 active 持仓的 user 跑一次同步。"""
    async with db._SessionFactory() as s:
        rows = await s.execute(
            select(DecisionJournal.user_id).distinct()
        )
        user_ids = [r[0] for r in rows]
    summary = {}
    for uid in user_ids:
        try:
            r = await sync_user_dividends(uid)
            summary[uid] = r
        except Exception as e:
            log.exception("dividend sync failed user=%d: %s", uid, e)
            summary[uid] = {"error": str(e)}
    return summary
