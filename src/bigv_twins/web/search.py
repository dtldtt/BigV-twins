"""全站搜索 — SQLite FTS5 (trigram tokenizer) 索引 blogger_brief / ticker_brief / cached_news。

设计取舍：
- FTS5 trigram 对中文友好，但要求查询 ≥ 3 字符；< 3 字符的查询走 LIKE 兜底
- contentless 表 — 我们手动管 INSERT/DELETE，不用 triggers（少耦合）
- 重建策略：lifespan 启动跑一次 + 每个 cron job 完后跑一次 + 路由 `/search/rebuild`
- 单次 rebuild 在当前数据量下 < 1s（150 行 blogger_brief + ~10 ticker_brief + ~50 news）

未来可加：
- chat messages 索引（数据量大，先不加）
- 增量更新（每次 brief 生成后只 index 那一行）
"""

from __future__ import annotations

import logging
import re
import time
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from bigv_twins.config import BY_SLUG

from . import auth, db
from .db import BloggerDailyBrief, CachedNews, TickerDailyBrief, User

log = logging.getLogger("bigv_twins.web.search")
router = APIRouter(prefix="/search")
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


SOURCES = ("blogger_brief", "ticker_brief", "news")


# ============================================================================
# Indexing
# ============================================================================


async def rebuild_search_index() -> dict[str, int]:
    """Wipe and rebuild from scratch. Call after batch jobs."""
    t0 = time.time()
    counts = {s: 0 for s in SOURCES}
    async with db._SessionFactory() as s:
        await s.execute(text("DELETE FROM search_index"))

        # blogger briefs
        rows = await s.execute(select(BloggerDailyBrief))
        for br in rows.scalars():
            blogger = BY_SLUG.get(br.blogger_slug)
            name = blogger.name if blogger else br.blogger_slug
            await s.execute(text("""
                INSERT INTO search_index (source, title, body, ref_id, ref_url, ref_date)
                VALUES ('blogger_brief', :title, :body, :ref_id, :ref_url, :ref_date)
            """), {
                "title": f"{name} · {br.brief_date}",
                "body": br.brief_md or "",
                "ref_id": str(br.id),
                "ref_url": f"/report/history?date={br.brief_date}",
                "ref_date": br.brief_date,
            })
            counts["blogger_brief"] += 1

        # ticker briefs
        rows = await s.execute(select(TickerDailyBrief))
        for tb in rows.scalars():
            await s.execute(text("""
                INSERT INTO search_index (source, title, body, ref_id, ref_url, ref_date)
                VALUES ('ticker_brief', :title, :body, :ref_id, :ref_url, :ref_date)
            """), {
                "title": f"{tb.ticker} · {tb.brief_date}",
                "body": (tb.news_summary_md or "") + " " + (tb.verdict_reason or ""),
                "ref_id": str(tb.id),
                "ref_url": f"/report/history?date={tb.brief_date}",
                "ref_date": tb.brief_date,
            })
            counts["ticker_brief"] += 1

        # cached news
        rows = await s.execute(select(CachedNews))
        for n in rows.scalars():
            await s.execute(text("""
                INSERT INTO search_index (source, title, body, ref_id, ref_url, ref_date)
                VALUES ('news', :title, :body, :ref_id, :ref_url, :ref_date)
            """), {
                "title": n.title or "",
                "body": (n.content or "") + " " + (n.verdict_reason or ""),
                "ref_id": str(n.id),
                "ref_url": n.link or "",
                "ref_date": (n.jin10_time or "")[:10],
            })
            counts["news"] += 1

        await s.commit()
    log.info("search index rebuilt in %.2fs: %s", time.time() - t0, counts)
    return counts


# ============================================================================
# Query
# ============================================================================


def _build_fts_query(query: str) -> str:
    """Sanitize user input → FTS5 MATCH query.

    Strip operator chars to avoid syntax errors; quote each token for phrase-AND.
    """
    safe = re.sub(r"[^\w一-鿿\s]", " ", query)
    tokens = [t for t in safe.split() if t]
    if not tokens:
        return ""
    return " ".join(f'"{t}"' for t in tokens)


async def search(query: str, source: str = "", limit: int = 50) -> list[dict]:
    """Full-text search. Returns list of {source, title, snippet, ref_url, ref_date, rank}."""
    query = (query or "").strip()
    if not query:
        return []

    # < 3 chars: trigram has nothing to match against → LIKE fallback
    if len(query) < 3:
        async with db._SessionFactory() as s:
            base_sql = """
                SELECT source, title,
                    substr(body, max(1, instr(lower(body), lower(:q)) - 30), 120) AS snippet,
                    ref_url, ref_date
                FROM search_index
                WHERE (body LIKE :pat OR title LIKE :pat)
            """
            params = {"q": query, "pat": f"%{query}%", "lim": limit}
            if source and source in SOURCES:
                base_sql += " AND source = :src"
                params["src"] = source
            base_sql += " ORDER BY ref_date DESC LIMIT :lim"
            rows = await s.execute(text(base_sql), params)
            return [{"source": r[0], "title": r[1], "snippet": r[2],
                     "ref_url": r[3], "ref_date": r[4], "rank": 0.0}
                    for r in rows.fetchall()]

    # ≥ 3 chars: FTS5 MATCH
    fts_q = _build_fts_query(query)
    if not fts_q:
        return []
    async with db._SessionFactory() as s:
        base_sql = """
            SELECT source, title,
                snippet(search_index, 2, '<mark>', '</mark>', '…', 30) AS snippet,
                ref_url, ref_date, rank
            FROM search_index
            WHERE search_index MATCH :q
        """
        params = {"q": fts_q, "lim": limit}
        if source and source in SOURCES:
            base_sql += " AND source = :src"
            params["src"] = source
        base_sql += " ORDER BY rank LIMIT :lim"
        try:
            rows = await s.execute(text(base_sql), params)
            return [{"source": r[0], "title": r[1], "snippet": r[2],
                     "ref_url": r[3], "ref_date": r[4], "rank": r[5]}
                    for r in rows.fetchall()]
        except Exception as e:
            log.warning("FTS query failed for %r: %s", query, e)
            return []


# ============================================================================
# HTTP routes
# ============================================================================


@router.get("", response_class=HTMLResponse)
async def search_page(
    request: Request,
    user: Annotated[User, Depends(auth.require_user)],
    session: Annotated[AsyncSession, Depends(db.get_session)],
    q: str = "",
    source: str = "",
):
    results = await search(q, source=source, limit=80) if q else []
    return templates.TemplateResponse(
        request=request,
        name="search.html",
        context={
            "user": user, "q": q, "source": source, "results": results,
            "sources": SOURCES,
            "source_labels": {
                "blogger_brief": "博主日报",
                "ticker_brief": "自选股 brief",
                "news": "金十事件",
            },
        },
    )


@router.post("/rebuild")
async def search_rebuild(
    user: Annotated[User, Depends(auth.require_user)],
):
    """Manually rebuild the full-text index."""
    counts = await rebuild_search_index()
    log.info("manual rebuild by %s: %s", user.username, counts)
    return RedirectResponse("/search", status_code=303)
