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

from .chunk import html_to_text
from .config import BLOGGERS, BY_SLUG, settings
from .search import search as _search

log = logging.getLogger("bigv_twins.blogger_server")

mcp = FastMCP(
    "bigv-blogger",
    instructions=(
        "Per-blogger retrieval over the curated Zhihu archive. Five tools: "
        "`list_bloggers` enumerates known slugs; `search` does semantic retrieval "
        "of a blogger's stance on a topic; `get_recent` lists their N latest posts; "
        "`get_post` fetches the full cleaned text of one post by zhihu_id; "
        "`get_persona` returns the blogger's style summary (also exposed as a "
        "resource at persona://blogger/{slug}). "
        "These tools are for AGENTS that role-play one of the archived bloggers. "
        "Generic AI advisors should NOT call them."
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

    Returns the most relevant chunks ranked by cosine distance, each with original
    URL, voteup count, date, content_type, and title (when available). Use this
    every time the user asks for a specific blogger's view, opinion, or framework.
    """
    _validate_blogger(blogger)
    if content_type and content_type not in {"answer", "article", "pin"}:
        raise ValueError("content_type must be one of: answer | article | pin (or omit)")
    hits = _search(blogger, query, top_k=top_k, content_type=content_type)
    return [h.to_dict() for h in hits]


@mcp.tool()
def get_recent(
    blogger: Annotated[str, Field(description="Blogger slug.")],
    n: Annotated[int, Field(ge=1, le=50)] = 10,
    content_type: Annotated[
        str | None,
        Field(description="Filter to one of: answer | article | pin"),
    ] = None,
) -> list[dict]:
    """Return the N most recent posts for a blogger, ordered by created_time descending."""
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
        return [dict(r) for r in rows]
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
        return {
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
