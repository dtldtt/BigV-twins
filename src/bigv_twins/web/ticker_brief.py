"""每日 03:35 跑：给每只「至少有一个用户关注」的股票生成「相关动态」摘要。

依赖前置：03:30 blogger_daily_brief 跑完（这里读它的 mentioned_tickers 字段）。

Steps per ticker:
  1. 跨表查 blogger_daily_brief (brief_date = 前一日)，收集所有 mentioned_tickers
     包含此 ticker 的博主 slug 列表（free，纯 DB 查询）
  2. 用 web_search 拉「<ticker> 今日 公告 新闻」相关 snippets
  3. 把 (blogger_mentions + news snippets) 喂 advisor，输出 news_summary + verdict
  4. 存到 ticker_daily_brief（UNIQUE(ticker, date)）
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from datetime import date, timedelta

from sqlalchemy import select, distinct
from sqlalchemy.exc import IntegrityError

from bigv_twins.config import BY_SLUG
from bigv_twins.web_search import web_search

from . import db, openclaw_client
from .db import BloggerDailyBrief, TickerDailyBrief, UserWatchlist

log = logging.getLogger("bigv_twins.web.ticker_brief")


def _yesterday_str() -> str:
    return (date.today() - timedelta(days=1)).strftime("%Y-%m-%d")


async def _unique_watchlist_tickers() -> list[str]:
    """All tickers any user has in their watchlist (dedup)."""
    async with db._SessionFactory() as s:
        rows = await s.execute(select(distinct(UserWatchlist.ticker)))
        return [r[0] for r in rows.all()]


async def _bloggers_mentioning(ticker: str, brief_date: str) -> list[str]:
    """Which blogger slugs mentioned this ticker on the given date?

    Read from blogger_daily_brief.mentioned_tickers JSON.
    """
    async with db._SessionFactory() as s:
        rows = await s.execute(
            select(BloggerDailyBrief).where(BloggerDailyBrief.brief_date == brief_date)
        )
        out = []
        for br in rows.scalars():
            try:
                tickers = json.loads(br.mentioned_tickers or "[]")
            except json.JSONDecodeError:
                tickers = []
            if ticker in tickers:
                out.append(br.blogger_slug)
        return out


_VERDICT_SYS = (
    "你是「赛博大V 投资日报」的自选股摘要助手。给你一只 A 股的 ticker + 名称 + "
    "今日相关公开新闻 snippets（来自 Bing 搜索）+ 提到该股的博主 slug 列表，"
    "输出对该股的当日动态总结。\n\n"
    "## 输出 JSON（严格）\n\n"
    "{\n"
    '  "summary_md": "<对该股今日动态的 1-2 句话总结。如有新闻引用 snippet 的关键事实；'
    '无新闻就基于博主提及说，如「无具体新闻，X 博主在日报里提到」。**≤ 120 字**。>",\n'
    '  "verdict": "利好" | "利空" | "中性",\n'
    '  "verdict_reason": "<不超过 30 字的理由>"\n'
    "}\n\n"
    "## 规则\n\n"
    "1. 只输出 JSON，不要 markdown 代码块包裹\n"
    "2. summary_md 只基于给你的 snippets / 博主提及，**不要外推**\n"
    "3. 没有显著事件就直接「中性」+「无重大动态」\n"
    "4. 区分「公告/新闻」(snippet) vs 「博主观点」(blogger mentions)\n"
)


async def _summarize_ticker(ticker: str, name: str, blogger_slugs: list[str],
                            news_results: list[dict]) -> dict:
    """LLM single call. Returns {summary_md, verdict, verdict_reason}."""
    blogger_block = (
        "提到该股的博主：" + (", ".join(blogger_slugs) if blogger_slugs else "无")
    )
    news_lines = []
    for n in news_results[:5]:  # cap at top-5 snippets
        title = n.get("title", "")
        snippet = n.get("snippet", "")
        news_lines.append(f"- 【{title}】 {snippet}")
    news_block = "今日相关新闻 snippets：\n" + (
        "\n".join(news_lines) if news_lines else "（暂无新闻）"
    )
    user_input = f"股票：{name} ({ticker})\n\n{blogger_block}\n\n{news_block}"

    messages = [
        {"role": "system", "content": _VERDICT_SYS},
        {"role": "user", "content": user_input},
    ]
    buf: list[str] = []
    try:
        async for delta in openclaw_client.stream_chat(messages, model="openclaw/advisor"):
            buf.append(delta)
    except Exception as e:
        log.exception("ticker brief LLM call failed for %s: %s", ticker, e)
        return {
            "summary_md": f"（LLM 总结失败：{e}）",
            "verdict": "中性",
            "verdict_reason": "判断失败",
        }
    full = "".join(buf).strip()
    if full.startswith("```"):
        full = re.sub(r"^```(?:json)?\n", "", full)
        full = re.sub(r"\n```$", "", full)
    try:
        obj = json.loads(full)
    except json.JSONDecodeError:
        log.warning("ticker brief JSON parse failed for %s: %r", ticker, full[:300])
        return {"summary_md": full[:200] or "（解析失败）", "verdict": "中性", "verdict_reason": "解析失败"}
    v = (obj.get("verdict") or "").strip()
    return {
        "summary_md": (obj.get("summary_md") or "")[:300],
        "verdict": v if v in ("利好", "利空", "中性") else "中性",
        "verdict_reason": (obj.get("verdict_reason") or "")[:60],
    }


async def generate_ticker_briefs_for_day(day_str: str | None = None) -> dict:
    """Walk all unique watchlist tickers, generate ticker_daily_brief for each.

    Idempotent — skips tickers already with a brief for this date.
    """
    if day_str is None:
        day_str = _yesterday_str()

    t0 = time.time()
    tickers = await _unique_watchlist_tickers()
    log.info("ticker_briefs(%s): %d unique tickers across all watchlists", day_str, len(tickers))

    generated = skipped = errors = 0

    for ticker in tickers:
        # Skip if already done
        async with db._SessionFactory() as s:
            existing = await s.execute(
                select(TickerDailyBrief)
                .where(TickerDailyBrief.ticker == ticker)
                .where(TickerDailyBrief.brief_date == day_str)
            )
            if existing.scalar_one_or_none() is not None:
                skipped += 1
                continue

        # Get the user-friendly name from any user's watchlist (they all
        # have the same canonical name since resolve_ticker normalized it)
        async with db._SessionFactory() as s:
            wl = await s.execute(
                select(UserWatchlist).where(UserWatchlist.ticker == ticker).limit(1)
            )
            wl_row = wl.scalar_one_or_none()
            name = wl_row.name if wl_row else ticker

        try:
            blogger_slugs = await _bloggers_mentioning(ticker, day_str)
            # web_search is sync httpx → run in thread
            search_query = f"{ticker} {name} 最新公告 新闻"
            search_result = await asyncio.to_thread(web_search, search_query, top_k=5)
            news_results = search_result.get("results", []) if search_result.get("ok") else []
            llm_out = await _summarize_ticker(ticker, name, blogger_slugs, news_results)
        except Exception as e:
            log.exception("ticker_brief %s failed: %s", ticker, e)
            errors += 1
            continue

        async with db._SessionFactory() as s:
            row = TickerDailyBrief(
                ticker=ticker,
                brief_date=day_str,
                blogger_mentions=json.dumps(blogger_slugs, ensure_ascii=False),
                news_summary_md=llm_out["summary_md"],
                verdict=llm_out["verdict"],
                verdict_reason=llm_out["verdict_reason"],
            )
            s.add(row)
            try:
                await s.commit()
                generated += 1
            except IntegrityError:
                await s.rollback()
                skipped += 1

    log.info("ticker_briefs(%s) done in %.1fs: generated=%d skipped=%d errors=%d",
             day_str, time.time() - t0, generated, skipped, errors)
    return {"generated": generated, "skipped_existing": skipped, "errors": errors}


async def get_briefs_for_tickers(tickers: list[str]) -> dict[str, TickerDailyBrief]:
    """For UI: yesterday's brief for each given ticker. Returns dict ticker → brief.

    Falls back to most recent brief if yesterday's isn't there.
    """
    day_str = _yesterday_str()
    out: dict[str, TickerDailyBrief] = {}
    if not tickers:
        return out
    async with db._SessionFactory() as s:
        rows = await s.execute(
            select(TickerDailyBrief)
            .where(TickerDailyBrief.ticker.in_(tickers))
            .where(TickerDailyBrief.brief_date == day_str)
        )
        for r in rows.scalars():
            out[r.ticker] = r
        # Fallback to latest for any missing
        for ticker in tickers:
            if ticker in out:
                continue
            fb = await s.execute(
                select(TickerDailyBrief)
                .where(TickerDailyBrief.ticker == ticker)
                .order_by(TickerDailyBrief.brief_date.desc())
                .limit(1)
            )
            r = fb.scalar_one_or_none()
            if r is not None:
                out[ticker] = r
    return out
