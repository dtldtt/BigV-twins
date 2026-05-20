"""FastMCP server exposing per-blogger retrieval tools for BigV-twins.

This server does NOT call any LLM. It only retrieves and formats data from the
read-only Zhihu archive plus per-blogger sqlite-vec indices. The agent on the
OpenClaw side does generation.
"""

from __future__ import annotations

import logging
import sqlite3
from typing import Annotated

from mcp.server.fastmcp import FastMCP
from pydantic import Field

from .chunk import html_to_text
from .config import BLOGGERS, BY_SLUG, settings
from .market_data import get_market_context as _get_market_context
from .search import search as _search
from .stock_data import get_stock_snapshot as _get_stock_snapshot

log = logging.getLogger("bigv_twins.server")

mcp = FastMCP(
    "bigv-twins",
    instructions=(
        "Per-blogger retrieval over a curated Zhihu archive of investment writers. "
        "Use `list_bloggers` to see what slugs exist, `search` for semantic retrieval "
        "of a blogger's stance on a topic, `get_recent` for recent posts, and `get_post` "
        "to fetch the full cleaned text of a specific post by its zhihu_id. "
        "Use `get_persona` (or the persona://blogger/{slug} resource) for the blogger's "
        "style summary. "
        "Use `get_stock_snapshot` when the user asks about a specific stock — it "
        "returns valuation / market cap / ownership / sector / index context, which "
        "lets the blogger apply their quantitative framework to concrete numbers."
    ),
    host=settings.mcp_host,
    port=settings.mcp_port,
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
                    "zhihu_url": f"https://www.zhihu.com/people/{b.url_token}",
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
    """
    Semantic search over a blogger's archived Zhihu content.

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
    """
    Return the N most recent posts for a blogger, ordered by created_time descending.

    Use when the user asks 'what is X talking about lately' or 'X recent views on Y'.
    Returns metadata + excerpt only (use get_post to fetch full text).
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
        return [
            {
                "zhihu_id": r["zhihu_id"],
                "content_type": r["content_type"],
                "title": r["title"],
                "excerpt": r["excerpt"],
                "voteup_count": r["voteup_count"],
                "comment_count": r["comment_count"],
                "url": r["url"],
                "column_title": r["column_title"],
                "created_time": r["created_time"],
            }
            for r in rows
        ]
    finally:
        src.close()


@mcp.tool()
def get_post(
    blogger: Annotated[str, Field(description="Blogger slug.")],
    zhihu_id: Annotated[
        str, Field(description="zhihu_id returned by search() or get_recent().")
    ],
) -> dict:
    """
    Fetch the full cleaned text of one post.

    Use when search results suggest the right post but you need the full text to
    reason about edge cases or quote precisely.
    """
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
            raise ValueError(
                f"post not found: blogger={blogger!r}, zhihu_id={zhihu_id!r}"
            )
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
    """
    Return the blogger's persona summary (style, focus, methodology).

    If the persona file has not been written yet, returns a placeholder asking the
    caller to ground answers in search/get_recent results.
    """
    _validate_blogger(blogger)
    path = settings.persona_path(blogger)
    if not path.exists():
        return {
            "slug": blogger,
            "available": False,
            "text": (
                f"(Persona for {blogger} has not been written yet. "
                "Ground answers in search() / get_recent() results.)"
            ),
        }
    return {
        "slug": blogger,
        "available": True,
        "text": path.read_text(encoding="utf-8"),
    }


@mcp.tool()
def get_market_context(
    topics: Annotated[
        list[str],
        Field(description="主题 id 列表（白名单见 topics.json），如 ['a-share', 'hk', 'gold', 'industry-coal']"),
    ],
) -> dict:
    """获取若干主题的近期市场行情（1 周 + 1 月走势），用于宏观/板块/资产类讨论的背景参考。

    支持的 topic id（见 topics.json，可热修改）：
    - 大盘指数：`a-share` / `gem` / `star` / `bse` / `hk` / `us`
    - 资产类：`gold`
    - 行业 ETF：`industry-bank` / `industry-baijiu` / `industry-coal` / `industry-lithium`
                 `industry-semi` / `industry-ai` / `industry-new-energy` / `industry-military`
                 `industry-consumer` / `industry-real-estate` / `industry-resources`

    每个 topic 返回若干 asset 的最近 1 周和 1 月走势（如有历史），或仅 real-time spot（如港股指数 backup 路径）。

    注：web 入口已在 prompt 组装阶段对常见关键词（"港股"/"黄金"/"大盘"等）做了**预扫描自动召回**——
    所以大多数情况你不需要主动调这个工具。**当用户提到新主题、或你判断需要补充另一个主题的背景时**才调。
    """
    return _get_market_context(topics)


@mcp.tool()
def get_stock_snapshot(
    query: Annotated[
        str,
        Field(description="股票代码 (如 600519/300750/688981/00700) 或常见股票名 (如 茅台/宁德时代/腾讯)"),
    ],
) -> dict:
    """获取股票的基本面快照 + 大盘环境，用于辅助博主分析具体标的。

    返回内容（best-effort，部分字段在某些股票上可能缺失）：
    - resolved: 解析后的代码、名称、市场、板块（主板/创业板/科创板/北交所/港股）
    - price: 现价 / 当日涨跌% / 52周高低 / 最近 1 年涨跌%
    - valuation: PE_TTM / PB
    - scale: 总市值（亿，显示形式如「1.93 万亿」）
    - ownership: 控股性质（央企控股/省属国资控股/民营企业/外资企业...）+ 实际控制人
    - business: 行业 + 主营业务文字描述
    - company: 公司全名 / 董事长 / 员工数
    - index_context: 上证指数最近 10 天；如果是创业板再附创业板指；科创板再附科创50

    **务必**在用户问到具体股票/标的时**先**调用此工具拿到真实数字，
    然后再调 search/get_persona——这样博主才能把自己的量化阈值
    （如「市值 < 200 亿」「PE < 30」「央企控股」等）跟股票的真实数字逐条对照。

    数据源（按可靠性）：Tencent 实时报价 → 同花顺主营业务 → 雪球控股结构 → 新浪 K 线。
    缓存 10 分钟，重复查询同一股票不再请求外部 API。
    """
    return _get_stock_snapshot(query)


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
        "starting BigV-twins MCP server on %s:%d (streamable-http)",
        settings.mcp_host,
        settings.mcp_port,
    )
    mcp.run(transport="streamable-http")


if __name__ == "__main__":
    main()
