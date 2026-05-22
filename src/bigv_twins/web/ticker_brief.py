"""每天 08:00 / 12:30 / 18:00 跑：给每只「至少有一个用户关注」的股票生成
「相关动态」摘要。同 (ticker, today) UPSERT 覆盖。

数据源（HTTP / DB，都不烧 LLM token）：
  1. 跨表查 blogger_daily_brief.mentioned_tickers（brief_date = 前一日）反向收集
     博主提及；DB 查询。
  2. akshare stock_news_em(ticker)：东财个股新闻最新 5 条（标题+正文+时间+链接）
  3. akshare stock_notice_report(date=today)：当日全市场公告，按 ticker 过滤
     （每次 cron 跑只拉 1 次，全 ticker 共用）
  4. akshare stock_irm_cninfo(ticker)：仅深市，互动易问答最近 3 条（含董秘回复）
  5. web_search Bing snippets（兜底）

最后 1 次 LLM 调用（advisor）整合上面所有，输出 summary_md + verdict。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from datetime import date, datetime, timedelta

from sqlalchemy import select, distinct

from bigv_twins.web_search import web_search

from . import db, openclaw_client
from .db import BloggerDailyBrief, TickerDailyBrief, UserWatchlist

log = logging.getLogger("bigv_twins.web.ticker_brief")


def _today_str() -> str:
    return date.today().strftime("%Y-%m-%d")


def _yesterday_str() -> str:
    return (date.today() - timedelta(days=1)).strftime("%Y-%m-%d")


def _is_shenzhen(ticker: str) -> bool:
    """互动易只覆盖深市股票（含主板 / 中小板 / 创业板）。"""
    return ticker[:3] in ("000", "001", "002", "003", "300", "301")


def _is_a_share(ticker: str) -> bool:
    """A 股 6 位代码（沪市 6xx / 深市见上）。akshare 个股 API 都是 A 股 only。"""
    return len(ticker) == 6 and ticker.isdigit()


# ============================================================================
# Data fetchers — all sync HTTP, callers wrap in asyncio.to_thread
# ============================================================================


def _fetch_recent_news(ticker: str) -> list[dict]:
    """东财个股新闻最新 5 条。返回 [{title, content, time, source, url}]."""
    if not _is_a_share(ticker):
        return []
    try:
        import akshare as ak
        df = ak.stock_news_em(symbol=ticker)
    except Exception as e:
        log.warning("stock_news_em(%s) failed: %s", ticker, e)
        return []
    if df is None or len(df) == 0:
        return []
    out = []
    for _, row in df.head(5).iterrows():
        out.append({
            "title": str(row.get("新闻标题", ""))[:120],
            "content": str(row.get("新闻内容", ""))[:200],
            "time": str(row.get("发布时间", "")),
            "source": str(row.get("文章来源", "")),
            "url": str(row.get("新闻链接", "")),
        })
    return out


def _fetch_today_notices_df(day_str: str):
    """当日全市场公告 DataFrame（一次 HTTP 拉 1500-2000 行，全 ticker 复用）。

    day_str: 'YYYY-MM-DD'，akshare 要 'YYYYMMDD' 格式。
    返回 DataFrame 或 None（拉取失败/无数据）。
    """
    try:
        import akshare as ak
        date_compact = day_str.replace("-", "")
        df = ak.stock_notice_report(symbol="全部", date=date_compact)
    except Exception as e:
        log.warning("stock_notice_report(%s) failed: %s", day_str, e)
        return None
    if df is None or len(df) == 0:
        return None
    return df


def _filter_notices_for_ticker(df, ticker: str) -> list[dict]:
    """从全市场公告 DataFrame 里筛 ticker 的公告。返回 [{type, title, url}]."""
    if df is None or len(df) == 0:
        return []
    try:
        sub = df[df["代码"].astype(str) == ticker]
    except Exception as e:
        log.warning("filter notices for %s failed: %s", ticker, e)
        return []
    out = []
    for _, row in sub.head(8).iterrows():
        out.append({
            "type": str(row.get("公告类型", ""))[:32],
            "title": str(row.get("公告标题", ""))[:120],
            "url": str(row.get("网址", "")),
        })
    return out


def _fetch_recent_irm(ticker: str, days: int = 30) -> list[dict]:
    """互动易最近 N 天的 Q&A 最多 3 条（仅深市）。返回 [{question, answer, time}]."""
    if not _is_shenzhen(ticker):
        return []
    try:
        import akshare as ak
        df = ak.stock_irm_cninfo(symbol=ticker)
    except Exception as e:
        log.warning("stock_irm_cninfo(%s) failed: %s", ticker, e)
        return []
    if df is None or len(df) == 0:
        return []
    cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    out = []
    for _, row in df.iterrows():
        upd = str(row.get("更新时间", ""))
        if upd < cutoff:
            continue
        ans = str(row.get("回答内容", "")).strip()
        if not ans or ans == "nan":
            continue  # 没回复的问题不要
        out.append({
            "question": str(row.get("问题", "")).strip()[:150],
            "answer": ans[:250],
            "time": upd,
        })
        if len(out) >= 3:
            break
    return out


# ============================================================================
# DB helpers
# ============================================================================


async def _unique_watchlist_tickers() -> list[str]:
    """All tickers any user has in their watchlist (dedup)."""
    async with db._SessionFactory() as s:
        rows = await s.execute(select(distinct(UserWatchlist.ticker)))
        return [r[0] for r in rows.all()]


async def _bloggers_mentioning(ticker: str, brief_date: str) -> list[str]:
    """哪些博主在 brief_date 这天的日报里提到了 ticker？读 mentioned_tickers JSON。"""
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


# ============================================================================
# LLM summarizer
# ============================================================================


_VERDICT_SYS = (
    "你是「赛博大V 投资日报」的自选股摘要助手。给你一只 A 股的 ticker + 名称 + "
    "今日多源数据（公司公告、个股新闻、互动易董秘问答、博主提及、Bing 搜索 snippets），"
    "输出对该股的当日动态总结。\n\n"
    "## 输出 JSON（严格）\n\n"
    "{\n"
    '  "summary_md": "**公司动态**：（公告/新闻/Q&A 的关键事实，包含具体数字、事件、回复内容；'
    '若都为空，写「暂无重大公司动态」）\\n\\n'
    '**市场情绪**：（博主观点 + 综合判断；若都为空，写「市场关注度一般」）",\n'
    '  "verdict": "利好" | "利空" | "中性",\n'
    '  "verdict_reason": "<不超过 30 字的理由>"\n'
    "}\n\n"
    "## 规则\n\n"
    "1. 只输出 JSON，不要 markdown 代码块包裹\n"
    "2. summary_md 总长度 ≤ 200 字（不含 markdown 标记）；两小段都要有，公司动态在前\n"
    "3. 公告优先于新闻优先于 Q&A 优先于博主提及；同类按时间倒序\n"
    "4. **不要外推**给定信息之外的内容；没的就承认没\n"
    "5. 引述博主时用第三人称（「X 在日报里看好」），不伪装第一人称\n"
    "6. verdict 综合判断：有重大利好（中标/业绩超预期/政策利好）→ 利好；"
    "有重大利空（业绩预亏/监管处罚/异常波动公告）→ 利空；其余 → 中性\n"
    "7. **重要**：summary_md 字符串内若需引用产品名/概念名，必须用中文「」"
    "或不加引号，**绝对不要**用西文双引号 \"，否则会破坏 JSON\n"
)


async def _summarize_ticker(
    ticker: str,
    name: str,
    blogger_slugs: list[str],
    notices: list[dict],
    news_em: list[dict],
    irm_qa: list[dict],
    web_results: list[dict],
    quote_line: str = "",
) -> dict:
    """一次 LLM 调用，整合所有数据源 → {summary_md, verdict, verdict_reason}."""
    parts = [f"股票：{name} ({ticker})"]
    if quote_line:
        parts.append(quote_line)

    if notices:
        parts.append("\n== 公告（今日）==")
        for n in notices[:5]:
            parts.append(f"- [{n['type']}] {n['title']}")
    else:
        parts.append("\n== 公告（今日）==\n（无）")

    if news_em:
        parts.append("\n== 个股新闻（最近）==")
        for n in news_em:
            time_short = n["time"][:16] if n["time"] else ""
            parts.append(f"- [{n['source']} {time_short}] 【{n['title']}】 {n['content']}")
    else:
        parts.append("\n== 个股新闻（最近）==\n（无）")

    if irm_qa:
        parts.append("\n== 互动易董秘问答（近 30 天）==")
        for q in irm_qa:
            parts.append(f"- 【{q['time'][:10]}】问：{q['question']}\n  答：{q['answer']}")
    elif _is_shenzhen(ticker):
        parts.append("\n== 互动易董秘问答（近 30 天）==\n（无）")
    # 沪市/港股不显示这一节

    parts.append("\n== 博主提及（前一自然日）==")
    parts.append("、".join(blogger_slugs) if blogger_slugs else "（无）")

    if web_results:
        parts.append("\n== Bing 搜索补充 snippets ==")
        for r in web_results[:3]:
            parts.append(f"- 【{r.get('title','')}】 {r.get('snippet','')}")

    user_input = "\n".join(parts)

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
    obj = _parse_advisor_json(full, ticker)
    v = (obj.get("verdict") or "").strip()
    return {
        "summary_md": (obj.get("summary_md") or "")[:600],
        "verdict": v if v in ("利好", "利空", "中性") else "中性",
        "verdict_reason": (obj.get("verdict_reason") or "")[:60],
    }


def _parse_advisor_json(full: str, ticker: str) -> dict:
    """advisor 的输出经常因为字符串内不转义西文引号而 JSON 解析失败。
    先 strict 试一次；失败就用正则抠出三个字段。"""
    try:
        return json.loads(full)
    except json.JSONDecodeError:
        pass
    # 正则兜底：匹配每个字段值（DOTALL 让 . 跨多行）
    out = {}
    # summary_md：按 "summary_md": " 开始，到 ", "verdict" 之前结束
    m = re.search(
        r'"summary_md"\s*:\s*"(.*?)"\s*,\s*"verdict"',
        full, flags=re.DOTALL,
    )
    if m:
        out["summary_md"] = m.group(1).replace("\\n", "\n")
    m = re.search(r'"verdict"\s*:\s*"([^"]*)"', full)
    if m:
        out["verdict"] = m.group(1)
    m = re.search(r'"verdict_reason"\s*:\s*"([^"]*)"', full)
    if m:
        out["verdict_reason"] = m.group(1)
    if out:
        log.info("advisor JSON regex-recovered for %s: keys=%s", ticker, list(out.keys()))
        return out
    log.warning("advisor JSON parse + regex both failed for %s: %r", ticker, full[:300])
    return {"summary_md": full[:300] or "（解析失败）", "verdict": "中性", "verdict_reason": "解析失败"}


# ============================================================================
# Generation entry points
# ============================================================================


async def regenerate_one_ticker_brief(
    ticker: str,
    name: str,
    day_str: str,
    all_notices_df=None,
) -> bool:
    """Regenerate a single ticker brief for given day, UPSERT into ticker_daily_brief.

    Caller may pass pre-fetched `all_notices_df` to avoid redundant downloads
    when called in a batch (e.g. from generate_ticker_briefs_for_day).

    Returns True on success, False on error.
    """
    blogger_brief_date = _yesterday_str()  # 博主日报永远是「前一日」语义

    try:
        blogger_slugs = await _bloggers_mentioning(ticker, blogger_brief_date)

        # Fetch all sources in parallel via to_thread
        if all_notices_df is None:
            news_em, notices_df, irm_qa = await asyncio.gather(
                asyncio.to_thread(_fetch_recent_news, ticker),
                asyncio.to_thread(_fetch_today_notices_df, day_str),
                asyncio.to_thread(_fetch_recent_irm, ticker),
            )
            notices = _filter_notices_for_ticker(notices_df, ticker)
        else:
            news_em, irm_qa = await asyncio.gather(
                asyncio.to_thread(_fetch_recent_news, ticker),
                asyncio.to_thread(_fetch_recent_irm, ticker),
            )
            notices = _filter_notices_for_ticker(all_notices_df, ticker)

        # web_search 兜底
        search_query = f"{ticker} {name} 最新公告 新闻"
        search_result = await asyncio.to_thread(web_search, search_query, top_k=5)
        web_results = search_result.get("results", []) if search_result.get("ok") else []

        llm_out = await _summarize_ticker(
            ticker, name, blogger_slugs, notices, news_em, irm_qa, web_results,
        )
    except Exception as e:
        log.exception("regenerate_one_ticker_brief(%s) failed: %s", ticker, e)
        return False

    # UPSERT into ticker_daily_brief
    async with db._SessionFactory() as s:
        existing = await s.execute(
            select(TickerDailyBrief)
            .where(TickerDailyBrief.ticker == ticker)
            .where(TickerDailyBrief.brief_date == day_str)
        )
        row = existing.scalar_one_or_none()
        mentions_json = json.dumps(blogger_slugs, ensure_ascii=False)
        if row is None:
            row = TickerDailyBrief(
                ticker=ticker,
                brief_date=day_str,
                blogger_mentions=mentions_json,
                news_summary_md=llm_out["summary_md"],
                verdict=llm_out["verdict"],
                verdict_reason=llm_out["verdict_reason"],
            )
            s.add(row)
        else:
            row.blogger_mentions = mentions_json
            row.news_summary_md = llm_out["summary_md"]
            row.verdict = llm_out["verdict"]
            row.verdict_reason = llm_out["verdict_reason"]
            row.generated_at = datetime.utcnow()
        await s.commit()
    log.info("ticker_brief upserted: %s @ %s verdict=%s", ticker, day_str, llm_out["verdict"])
    return True


async def generate_ticker_briefs_for_day(day_str: str | None = None) -> dict:
    """Walk all unique watchlist tickers, UPSERT ticker_daily_brief for each.

    UPSERTS — same-day re-runs overwrite the row, so 08:00 / 12:30 / 18:00 cron
    each progressively refresh today's brief with the latest data.
    """
    if day_str is None:
        day_str = _today_str()

    t0 = time.time()
    tickers = await _unique_watchlist_tickers()
    log.info("ticker_briefs(%s): %d unique tickers across all watchlists", day_str, len(tickers))

    if not tickers:
        return {"generated": 0, "skipped_existing": 0, "errors": 0}

    # Fetch full daily notices ONCE, reuse across all tickers
    notices_df = await asyncio.to_thread(_fetch_today_notices_df, day_str)
    log.info("  daily notices DF: %s rows", len(notices_df) if notices_df is not None else "n/a")

    generated = errors = 0

    for ticker in tickers:
        # Get the user-friendly name from any user's watchlist
        async with db._SessionFactory() as s:
            wl = await s.execute(
                select(UserWatchlist).where(UserWatchlist.ticker == ticker).limit(1)
            )
            wl_row = wl.scalar_one_or_none()
            name = wl_row.name if wl_row else ticker

        ok = await regenerate_one_ticker_brief(ticker, name, day_str, all_notices_df=notices_df)
        if ok:
            generated += 1
        else:
            errors += 1

    log.info("ticker_briefs(%s) done in %.1fs: generated=%d errors=%d",
             day_str, time.time() - t0, generated, errors)
    # Keep "skipped_existing" key for back-compat with logs that grep it; always 0 now (UPSERT).
    return {"generated": generated, "skipped_existing": 0, "errors": errors}


async def get_briefs_for_tickers(tickers: list[str]) -> dict[str, TickerDailyBrief]:
    """For UI: today's brief for each ticker. Falls back to most recent if today's missing."""
    day_str = _today_str()
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
