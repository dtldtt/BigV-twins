"""金十数据「重要事件」爬虫 + LLM 利好/利空判断 + 30 min 后台刷新。

抓 https://flash-api.jin10.com/get_flash_list (无 auth，只需要 x-app-id 头)
返回 21 条最新快讯，筛 important>=1 取前 10，调 advisor 一次性批量判断
对 A 股的影响（利好/利空/中性 + 一句话理由），写入 cached_news 表。

每 30 分钟在 APScheduler 里触发一次。

Design notes:
- We dedupe by jin10 item id (string of digits like "20260522210420428800")
- New items get classified by LLM and stored; existing items unchanged
- The display query pulls latest N items with `important=1`, ordered by time desc
- Stale items are NOT deleted — keep history for trend analysis later
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from . import db, openclaw_client
from .db import CachedNews

log = logging.getLogger("bigv_twins.web.news_scraper")


JIN10_API = "https://flash-api.jin10.com/get_flash_list"
JIN10_DETAIL_URL = "https://flash.jin10.com/detail"
JIN10_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "x-app-id": "bVBF4FyRTn5NJF5n",
    "x-version": "1.0.0",
    "Accept": "application/json",
}

# How many items to keep / classify per refresh
TOP_N = 10


def _strip_html(s: str) -> str:
    """Jin10 occasionally includes inline <font> markup in content. Strip."""
    return re.sub(r"<[^>]+>", "", s or "").strip()


def fetch_jin10_raw(timeout: float = 10.0) -> list[dict[str, Any]]:
    """Pull the latest 21 flash items from jin10. No dedup, no filtering."""
    try:
        r = httpx.get(
            JIN10_API,
            params={"channel": "-8200", "vip": "1", "max_time": ""},
            headers=JIN10_HEADERS,
            timeout=timeout,
        )
    except Exception as e:
        log.warning("jin10 fetch failed: %s", e)
        return []
    if r.status_code != 200:
        log.warning("jin10 returned %d", r.status_code)
        return []
    try:
        j = r.json()
    except Exception:
        log.exception("jin10 JSON parse failed")
        return []
    if j.get("status") != 200:
        log.warning("jin10 status not OK: %r", j.get("status"))
        return []
    return j.get("data") or []


def _normalize_item(raw: dict) -> dict | None:
    """Pull the fields we care about out of a jin10 flash record."""
    item_id = raw.get("id")
    if not item_id:
        return None
    d = raw.get("data") or {}
    content = _strip_html(d.get("content") or "")
    title = (d.get("title") or "").strip()
    text = content or title
    if not text:
        return None  # skip image-only / empty items
    return {
        "jin10_id": str(item_id),
        "time": raw.get("time") or "",
        "title": title or text[:60],
        "content": text,
        "importance": int(raw.get("important") or 0),
        "link": f"{JIN10_DETAIL_URL}/{item_id}",
    }


def filter_important(items: list[dict], top_n: int = TOP_N) -> list[dict]:
    """Keep only items with importance >= 1, return at most top_n (newest first)."""
    important = [it for it in items if it["importance"] >= 1]
    return important[:top_n]


# ---------------------------------------------------------------- LLM verdict


_VERDICT_SYS_PROMPT = (
    "你是一个财经新闻速判助手。给你一组今天的国际/中国财经快讯，"
    "对每一条判断**对 A 股大盘**的短期（1-3 天）影响：\n"
    "  - 利好  (positive — 大盘倾向上涨)\n"
    "  - 利空  (negative — 大盘倾向下跌)\n"
    "  - 中性  (neutral — 不直接影响 / 已被定价 / 模糊不清)\n\n"
    "用**严格 JSON** 输出（不要 markdown 代码块包裹），格式：\n"
    '[{"id":"<id>","verdict":"利好|利空|中性","reason":"<一句不超过 30 字的理由>"}]\n\n'
    "规则：\n"
    "1. 只输出 JSON 数组，不要任何说明文字\n"
    "2. id 必须跟输入完全对应\n"
    "3. reason ≤ 30 字\n"
    "4. 不确定时倾向「中性」"
)


async def classify_items_via_llm(items: list[dict]) -> dict[str, dict]:
    """One LLM call classifies all items at once. Returns dict id → {verdict, reason}.

    Items without a usable LLM verdict get {"verdict": "中性", "reason": "未判断"}.
    """
    if not items:
        return {}
    user_input = "\n".join(
        f'{i+1}. id={it["jin10_id"]}  {it["content"][:200]}'
        for i, it in enumerate(items)
    )
    messages = [
        {"role": "system", "content": _VERDICT_SYS_PROMPT},
        {"role": "user", "content": user_input},
    ]
    buf: list[str] = []
    try:
        async for delta in openclaw_client.stream_chat(messages, model="openclaw/advisor"):
            buf.append(delta)
    except Exception as e:
        log.exception("LLM verdict call failed: %s", e)
        return {it["jin10_id"]: {"verdict": "中性", "reason": "判断失败"} for it in items}

    full = "".join(buf).strip()
    # Strip markdown code fence if model wrapped it
    if full.startswith("```"):
        full = re.sub(r"^```(?:json)?\n", "", full)
        full = re.sub(r"\n```$", "", full)
    try:
        arr = json.loads(full)
    except json.JSONDecodeError:
        log.warning("LLM verdict JSON parse failed: %r", full[:400])
        return {it["jin10_id"]: {"verdict": "中性", "reason": "解析失败"} for it in items}

    out: dict[str, dict] = {}
    for entry in arr if isinstance(arr, list) else []:
        if not isinstance(entry, dict):
            continue
        i = str(entry.get("id") or "").strip()
        v = (entry.get("verdict") or "").strip()
        r = (entry.get("reason") or "").strip()[:80]
        if i and v in ("利好", "利空", "中性"):
            out[i] = {"verdict": v, "reason": r or "（无理由）"}
    # Fill in missing
    for it in items:
        out.setdefault(it["jin10_id"], {"verdict": "中性", "reason": "未判断"})
    return out


# ---------------------------------------------------------------- refresh job


async def refresh_jin10_news() -> dict[str, int]:
    """Pull jin10, dedupe against cached_news, classify new items, persist.

    Returns {"fetched": N, "new": M, "errors": K}. Safe to call concurrently
    (UNIQUE constraint on jin10_id protects against double-insert).
    """
    t0 = time.time()
    log.info("refresh_jin10_news: starting")
    raw = fetch_jin10_raw()
    items = [n for n in (_normalize_item(r) for r in raw) if n]
    important = filter_important(items)
    log.info("refresh_jin10_news: fetched %d raw, %d normalized, %d important",
             len(raw), len(items), len(important))

    if not important:
        return {"fetched": len(raw), "new": 0, "errors": 0}

    # Dedupe: find which jin10_ids are already in DB
    async with db._SessionFactory() as s:
        existing_rows = await s.execute(
            select(CachedNews.jin10_id).where(
                CachedNews.jin10_id.in_([it["jin10_id"] for it in important])
            )
        )
        existing_ids = {r[0] for r in existing_rows.all()}

    new_items = [it for it in important if it["jin10_id"] not in existing_ids]
    log.info("refresh_jin10_news: %d new items to store (no LLM classify)", len(new_items))

    if not new_items:
        return {"fetched": len(raw), "new": 0, "errors": 0}

    # NOTE: LLM verdict classification REMOVED to save token cost.
    # News are short enough for users to read raw. UI omits the verdict tag.

    # Persist (verdict left empty so UI can render without color tag)
    errors = 0
    async with db._SessionFactory() as s:
        for it in new_items:
            row = CachedNews(
                jin10_id=it["jin10_id"],
                jin10_time=it["time"],
                title=it["title"][:200],
                content=it["content"],
                link=it["link"],
                importance=it["importance"],
                verdict="",
                verdict_reason="",
            )
            s.add(row)
            try:
                await s.flush()
            except IntegrityError:
                await s.rollback()
                errors += 1
        await s.commit()

    log.info("refresh_jin10_news: done in %.1fs, new=%d, errors=%d",
             time.time() - t0, len(new_items) - errors, errors)
    return {"fetched": len(raw), "new": len(new_items) - errors, "errors": errors}


async def get_cached_news(limit: int = TOP_N) -> list[CachedNews]:
    """Read latest classified news from DB (newest first)."""
    async with db._SessionFactory() as s:
        rows = await s.execute(
            select(CachedNews).order_by(CachedNews.jin10_time.desc()).limit(limit)
        )
        return list(rows.scalars())


def run_refresh_sync() -> None:
    """Sync wrapper for APScheduler (which runs in a thread pool)."""
    asyncio.run(refresh_jin10_news())
