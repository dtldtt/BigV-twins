"""FastMCP server: 博主语料工具 (search / persona / recent / post / list).

This is one of the two BigV-twins MCP servers. The other is `market_server.py`
which hosts stock / index data tools. Splitting them lets us register them
separately in OpenClaw so the `advisor` agent only sees market data and can't
peek into blogger archives.

Listens on `MCP_BLOGGER_PORT` (default 8770).
"""

from __future__ import annotations

import logging
import sqlite3
from typing import Annotated

from mcp.server.fastmcp import FastMCP
from pydantic import Field

from .archive_url import to_archive_url
from .chunk import html_to_text
from .config import BLOGGERS, BY_SLUG, settings
from .search import search as _search


def _rewrite(blogger: str, d: dict) -> dict:
    """Rewrite the `url` field of a hit/row dict to the local archive site.
    No-op if the rewrite can't find a local equivalent (keeps original URL).
    """
    d["url"] = to_archive_url(
        blogger,
        d.get("zhihu_id"),
        d.get("content_type"),
        d.get("url"),
    )
    return d

log = logging.getLogger("bigv_twins.blogger_server")

mcp = FastMCP(
    "bigv-blogger",
    instructions=(
        "Per-blogger retrieval over the curated Zhihu archive. These tools are for "
        "AGENTS that role-play one of the archived bloggers. Generic AI advisors "
        "should NOT call them.\n\n"
        "Tool selection by question shape:\n"
        "- 「X 怎么看 Y / X 对 Y 的观点」  → `search(blogger=X, query=Y)`\n"
        "- 「X 最近在聊什么 / X 这周说过什么」 → `get_recent(blogger=X, n=10)` (NOT search)\n"
        "- 「X 和 Y 对 Z 的看法有什么不同」 → 分别 `search(X, Z)` + `search(Y, Z)` "
        "(or `search_multi_query` if you need many sub-queries on one blogger)\n"
        "- 「X 在《XX 文章》里说了什么」 → `get_recent` to find zhihu_id, then `get_post`\n\n"
        "Retry / fallback rules (Agentic):\n"
        "- If `search` returns 0 hits OR top distance > 1.05 (low relevance): rephrase the\n"
        "  query (synonyms / shorter / domain term) and search ONE more time.\n"
        "- If both searches still come up empty: tell the user honestly the blogger has\n"
        "  not specifically discussed this — DO NOT make up an answer.\n"
        "- Hard cap: ≤ 3 retrieval calls per user turn. More than that wastes tokens."
    ),
    host=settings.mcp_host,
    port=settings.mcp_blogger_port,
)


def _open_zhihu_ro() -> sqlite3.Connection:
    uri = f"file:{settings.zhihu_db_path}?mode=ro&immutable=1"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _validate_blogger(slug: str) -> None:
    if slug not in BY_SLUG:
        valid = ", ".join(b.slug for b in BLOGGERS)
        raise ValueError(f"unknown blogger {slug!r}; valid slugs: {valid}")


@mcp.tool()
def list_bloggers() -> list[dict]:
    """List every blogger BigV-twins knows about, with metadata and persona availability."""
    out: list[dict] = []
    src = _open_zhihu_ro()
    try:
        for b in BLOGGERS:
            row = src.execute(
                "SELECT name, headline, follower_count, answer_count, article_count, "
                "pin_count, last_crawled_at FROM authors WHERE id = ?",
                (b.author_id,),
            ).fetchone()
            persona_path = settings.persona_path(b.slug)
            out.append(
                {
                    "slug": b.slug,
                    "name": b.name,
                    "url_token": b.url_token,
                    "zhihu_url": f"https://www.zhihu.com/people/{b.url_token}" if b.url_token else None,
                    "headline": row["headline"] if row else None,
                    "follower_count": row["follower_count"] if row else None,
                    "answer_count": row["answer_count"] if row else None,
                    "article_count": row["article_count"] if row else None,
                    "pin_count": row["pin_count"] if row else None,
                    "last_crawled_at": row["last_crawled_at"] if row else None,
                    "has_persona": persona_path.exists(),
                    "twin_db_exists": settings.twin_db_path(b.slug).exists(),
                }
            )
    finally:
        src.close()
    return out


@mcp.tool()
def search(
    blogger: Annotated[str, Field(description="Blogger slug; see list_bloggers.")],
    query: Annotated[str, Field(description="Natural-language Chinese query.")],
    top_k: Annotated[int, Field(ge=1, le=20)] = 5,
    content_type: Annotated[
        str | None,
        Field(description="Filter to one of: answer | article | pin"),
    ] = None,
) -> list[dict]:
    """Semantic search over a blogger's archived Zhihu content.

    Returns the most relevant chunks ranked by cosine distance (lower = closer),
    each with original URL, voteup count, date, content_type, and title.

    When to use:
    - **Topical questions** about the blogger's view / framework / opinion on
      something they may have written about ("X 怎么看 Y", "X 的方法论是什么")

    Reading the result quality (gating signal, bge-m3 scale):
    - `distance < 0.85` → very relevant, can cite directly
    - `distance ~ 0.85-1.0` → topical match, usable
    - `distance > 1.05` → low relevance; **rephrase and try once more** before giving up
    - empty result     → same — try a synonym or shorter query

    Anti-patterns (DO NOT):
    - Don't call `search` for time-based questions ("最近", "这周", "今年")—use
      `get_recent` instead.
    - Don't call `search` ≥ 3 times in one turn—either the blogger hasn't covered
      this, or your query terms are off. Tell the user honestly.
    - Don't fabricate citations. If `search` didn't return a chunk, you can't cite it.
    """
    _validate_blogger(blogger)
    if content_type and content_type not in {"answer", "article", "pin"}:
        raise ValueError("content_type must be one of: answer | article | pin (or omit)")
    hits = _search(blogger, query, top_k=top_k, content_type=content_type)
    return [_rewrite(blogger, h.to_dict()) for h in hits]


@mcp.tool()
def search_multi_query(
    blogger: Annotated[str, Field(description="Blogger slug.")],
    queries: Annotated[
        list[str],
        Field(description="2-5 related sub-queries / synonyms / aspects of the same question."),
    ],
    top_k_each: Annotated[int, Field(ge=1, le=10)] = 3,
    content_type: Annotated[
        str | None,
        Field(description="Filter to one of: answer | article | pin"),
    ] = None,
) -> list[dict]:
    """Parallel multi-query search with dedup. Use when ONE search isn't enough.

    Each query searches independently for `top_k_each` hits; results are merged
    and deduped by `chunk_id` (best distance kept), sorted by ascending distance.

    When to use:
    - **Decomposable** questions: "X 对 AI 算力 + 半导体 + 设备的看法"
      → queries=["AI 算力", "半导体", "设备投资"]
    - **Synonym-heavy** topics: "X 怎么看人形机器人"
      → queries=["人形机器人", "具身智能", "机器人产业链"]
    - **Different angles** of one question: "X 对煤炭的逻辑"
      → queries=["煤炭股", "高股息煤炭", "煤价周期"]

    DO NOT use as a generic "search harder" — it's for genuinely multi-faceted
    questions. For unrelated topics, do separate `search` calls.

    Hard cap: max 5 sub-queries per call (more = diminishing returns).
    """
    _validate_blogger(blogger)
    if content_type and content_type not in {"answer", "article", "pin"}:
        raise ValueError("content_type must be one of: answer | article | pin (or omit)")
    if not queries:
        return []
    if len(queries) > 5:
        raise ValueError("max 5 sub-queries per multi-query call")

    seen: dict[int, dict] = {}
    for q in queries:
        if not q.strip():
            continue
        for hit in _search(blogger, q, top_k=top_k_each, content_type=content_type):
            d = _rewrite(blogger, hit.to_dict())
            cid = d["chunk_id"]
            if cid not in seen or d["distance"] < seen[cid]["distance"]:
                seen[cid] = d
    merged = list(seen.values())
    merged.sort(key=lambda h: h["distance"])
    return merged


@mcp.tool()
def get_recent(
    blogger: Annotated[str, Field(description="Blogger slug.")],
    n: Annotated[int, Field(ge=1, le=50)] = 10,
    content_type: Annotated[
        str | None,
        Field(description="Filter to one of: answer | article | pin"),
    ] = None,
) -> list[dict]:
    """Return the N most recent posts for a blogger, ordered by created_time descending.

    Use this — NOT `search` — for any time-anchored question:
    - 「X 最近怎么看 …」, 「X 这两周聊了什么」, 「X 今年说过 …」
    - 「X 最近一段时间的关注点」

    Returns excerpt only (not full text); pair with `get_post` to drill into a
    specific zhihu_id when one excerpt looks promising.
    """
    _validate_blogger(blogger)
    if content_type and content_type not in {"answer", "article", "pin"}:
        raise ValueError("content_type must be one of: answer | article | pin (or omit)")
    b = BY_SLUG[blogger]
    src = _open_zhihu_ro()
    try:
        sql = (
            "SELECT zhihu_id, content_type, title, excerpt, voteup_count, comment_count,"
            " url, column_title, created_time"
            " FROM contents WHERE author_id = ? AND content IS NOT NULL"
        )
        params: list = [b.author_id]
        if content_type:
            sql += " AND content_type = ?"
            params.append(content_type)
        sql += " ORDER BY created_time DESC LIMIT ?"
        params.append(n)
        rows = src.execute(sql, params).fetchall()
        return [_rewrite(blogger, dict(r)) for r in rows]
    finally:
        src.close()


@mcp.tool()
def get_post(
    blogger: Annotated[str, Field(description="Blogger slug.")],
    zhihu_id: Annotated[str, Field(description="zhihu_id from search() or get_recent().")],
) -> dict:
    """Fetch the full cleaned text of one post."""
    _validate_blogger(blogger)
    b = BY_SLUG[blogger]
    src = _open_zhihu_ro()
    try:
        row = src.execute(
            "SELECT zhihu_id, content_type, title, content, voteup_count, "
            "comment_count, url, column_title, created_time, updated_time "
            "FROM contents WHERE author_id = ? AND zhihu_id = ?",
            (b.author_id, zhihu_id),
        ).fetchone()
        if not row:
            raise ValueError(f"post not found: blogger={blogger!r}, zhihu_id={zhihu_id!r}")
        d = {
            "zhihu_id": row["zhihu_id"],
            "content_type": row["content_type"],
            "title": row["title"],
            "text": html_to_text(row["content"]),
            "voteup_count": row["voteup_count"],
            "comment_count": row["comment_count"],
            "url": row["url"],
            "column_title": row["column_title"],
            "created_time": row["created_time"],
            "updated_time": row["updated_time"],
        }
        return _rewrite(blogger, d)
    finally:
        src.close()


@mcp.tool()
def get_persona(
    blogger: Annotated[str, Field(description="Blogger slug.")],
) -> dict:
    """Return the blogger's persona summary (style, focus, methodology)."""
    _validate_blogger(blogger)
    path = settings.persona_path(blogger)
    if not path.exists():
        return {
            "slug": blogger, "available": False,
            "text": (
                f"(Persona for {blogger} has not been written yet. "
                "Ground answers in search() / get_recent() results.)"
            ),
        }
    return {"slug": blogger, "available": True, "text": path.read_text(encoding="utf-8")}


@mcp.resource("persona://blogger/{slug}")
def persona_resource(slug: str) -> str:
    """Per-blogger persona summary as a readable resource."""
    if slug not in BY_SLUG:
        return f"unknown blogger: {slug}"
    path = settings.persona_path(slug)
    if not path.exists():
        return f"(persona file not yet written: {path})"
    return path.read_text(encoding="utf-8")


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    log.info(
        "starting BigV-twins BLOGGER MCP server on %s:%d (streamable-http)",
        settings.mcp_host, settings.mcp_blogger_port,
    )
    mcp.run(transport="streamable-http")


if __name__ == "__main__":
    main()
