"""每日 03:30 跑：给每位 zhihu 博主生成「前一日观点」总结。

依赖前置 timers：
  03:01  zhihu daily crawler — 拉新内容到 zhihu.db
  03:21  bigv-twins-daily.service — 把新内容 embedding 进 twins/*.db
  03:30  本任务 — 读前一日 zhihu.db 新增内容 → advisor LLM 总结 → DB 缓存

存储到 blogger_daily_brief 表（UNIQUE(slug, date)，重复跑覆盖）。
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import time
from datetime import date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from bigv_twins.config import BLOGGERS, settings

from . import db, openclaw_client
from .db import BloggerDailyBrief

log = logging.getLogger("bigv_twins.web.blogger_brief")


# Bloggers to generate briefs for. Only zhihu-source bloggers (masters like
# Buffett don't have daily new content; advisor has no corpus).
def _eligible_bloggers():
    return [b for b in BLOGGERS if b.source == "zhihu" and b.is_blogger]


def _open_zhihu_ro() -> sqlite3.Connection:
    uri = f"file:{settings.zhihu_db_path}?mode=ro&immutable=1"
    return sqlite3.connect(uri, uri=True)


def _yesterday_str() -> str:
    """前一自然日 in Asia/Shanghai. Naive but acceptable: server is CST."""
    return (date.today() - timedelta(days=1)).strftime("%Y-%m-%d")


def _strip_html(s: str) -> str:
    if not s:
        return ""
    return re.sub(r"<[^>]+>", "", s).strip()


def fetch_blogger_posts_for_day(author_id: int, day_str: str) -> list[dict]:
    """Pull all answer/article/pin entries created_time on the given day.

    Returns oldest-first list of {zhihu_id, content_type, title, text, url, votes, created_time}.
    """
    src = _open_zhihu_ro()
    try:
        sql = (
            "SELECT zhihu_id, content_type, title, content, voteup_count, url, created_time "
            "FROM contents WHERE author_id = ? AND content IS NOT NULL AND content <> '' "
            "AND date(created_time) = ? ORDER BY created_time"
        )
        rows = src.execute(sql, (author_id, day_str)).fetchall()
        out = []
        for r in rows:
            zid, ctype, title, content, votes, url, ct = r
            out.append({
                "zhihu_id": zid,
                "content_type": ctype,
                "title": title or "",
                "text": _strip_html(content)[:1500],  # cap to keep prompt manageable
                "url": url or "",
                "voteup_count": int(votes or 0),
                "created_time": ct,
            })
        return out
    finally:
        src.close()


_SUMMARIZER_SYS = (
    "你是「赛博大V 投资日报」的总结助手。给你一位投资博主在某一天发的所有"
    "新帖子（答案 / 文章 / 想法的全文或截断），输出**当日主要观点** + "
    "**后续建议**两段，让用户一眼读懂这位博主今天在想什么。\n\n"
    "## 输出 JSON 格式（严格）\n\n"
    "{\n"
    '  "main_view": "主要观点 80-150 字。多个主题用「；」连接。不要列条 bullet。",\n'
    '  "suggestion": "后续建议 ≤ 40 字。如「继续持有 / 等回调 / 关注 X / 现金为王」之类。'
    "若该博主当日没明确建议倾向，写「未明确表态」。\",\n"
    '  "mentioned_tickers": ["600519", "00700"]  (该博主当日提到的股票 ticker；'
    "若有名字无代码可省略；空列表表示当日没具体股票)\n"
    "}\n\n"
    "## 规则\n\n"
    "1. 只输出 JSON，不要 markdown 代码块包裹\n"
    "2. main_view 用第三人称（「他认为」「他强调」），不要伪装成博主第一人称\n"
    "3. 忠于原文，**不要外推**博主没说的话\n"
    "4. 若多个帖子讲不同主题，main_view 用「；」分点，按重要性排序\n"
    "5. mentioned_tickers 只收 6 位 A 股代码（如 600519 / 002475 / 300750）和 5 位港股代码\n"
)


async def summarize_blogger(blogger_slug: str, blogger_name: str,
                            posts: list[dict]) -> dict:
    """LLM single call → {"main_view", "suggestion", "mentioned_tickers"}.

    Empty posts → returns a placeholder dict rather than calling LLM.
    """
    if not posts:
        return {
            "main_view": "（当日无新帖子）",
            "suggestion": "—",
            "mentioned_tickers": [],
        }

    user_input_parts = [f"博主：{blogger_name} (slug={blogger_slug})\n",
                        f"日期：{posts[0]['created_time'][:10]}\n",
                        f"当日新帖共 {len(posts)} 篇\n\n---\n"]
    for i, p in enumerate(posts, 1):
        head = f"\n### 帖子 {i} ({p['content_type']}, voteup={p['voteup_count']})"
        if p["title"]:
            head += f" — {p['title']}"
        user_input_parts.append(head + "\n")
        user_input_parts.append(p["text"] + "\n")

    messages = [
        {"role": "system", "content": _SUMMARIZER_SYS},
        {"role": "user", "content": "".join(user_input_parts)},
    ]
    buf: list[str] = []
    try:
        async for delta in openclaw_client.stream_chat(messages, model="openclaw/advisor"):
            buf.append(delta)
    except Exception as e:
        log.exception("blogger_brief LLM call failed for %s: %s", blogger_slug, e)
        return {
            "main_view": f"（LLM 总结失败：{e}）",
            "suggestion": "—",
            "mentioned_tickers": [],
        }

    full = "".join(buf).strip()
    if full.startswith("```"):
        full = re.sub(r"^```(?:json)?\n", "", full)
        full = re.sub(r"\n```$", "", full)
    try:
        obj = json.loads(full)
    except json.JSONDecodeError:
        log.warning("blogger_brief JSON parse failed for %s: %r", blogger_slug, full[:400])
        # Fall back: store raw LLM output as main_view
        return {
            "main_view": full[:500] or "（解析失败）",
            "suggestion": "—",
            "mentioned_tickers": [],
        }

    return {
        "main_view": (obj.get("main_view") or "")[:600],
        "suggestion": (obj.get("suggestion") or "")[:80],
        "mentioned_tickers": [str(t) for t in (obj.get("mentioned_tickers") or [])
                              if str(t).isdigit() and len(str(t)) in (5, 6)],
    }


async def generate_briefs_for_day(day_str: str | None = None) -> dict[str, int]:
    """Main entry. Generate one brief per eligible blogger for given day
    (default: 前一自然日). Idempotent — UPSERTs via (slug, date) UNIQUE constraint.

    Returns {"generated": N, "skipped_existing": M, "errors": K}.
    """
    if day_str is None:
        day_str = _yesterday_str()

    t0 = time.time()
    bloggers = _eligible_bloggers()
    log.info("generate_briefs_for_day(%s) — %d bloggers", day_str, len(bloggers))

    generated = 0
    skipped = 0
    errors = 0

    for b in bloggers:
        # Skip if brief already exists for this slug+date
        async with db._SessionFactory() as s:
            existing = await s.execute(
                select(BloggerDailyBrief)
                .where(BloggerDailyBrief.blogger_slug == b.slug)
                .where(BloggerDailyBrief.brief_date == day_str)
            )
            if existing.scalar_one_or_none() is not None:
                skipped += 1
                continue

        try:
            posts = fetch_blogger_posts_for_day(b.author_id, day_str)
            log.info("  [%s] %d posts for %s", b.slug, len(posts), day_str)
            result = await summarize_blogger(b.slug, b.name, posts)
        except Exception as e:
            log.exception("blogger %s brief generation failed: %s", b.slug, e)
            errors += 1
            continue

        brief_md = (
            f"**主要观点**：{result['main_view']}\n\n"
            f"**后续建议**：{result['suggestion']}"
        )
        async with db._SessionFactory() as s:
            row = BloggerDailyBrief(
                blogger_slug=b.slug,
                brief_date=day_str,
                brief_md=brief_md,
                mentioned_tickers=json.dumps(result["mentioned_tickers"], ensure_ascii=False),
                post_count=len(posts),
            )
            s.add(row)
            try:
                await s.commit()
                generated += 1
            except IntegrityError:
                await s.rollback()
                skipped += 1  # race with another instance

    log.info("generate_briefs_for_day(%s) done in %.1fs: generated=%d skipped=%d errors=%d",
             day_str, time.time() - t0, generated, skipped, errors)
    return {"generated": generated, "skipped_existing": skipped, "errors": errors}


async def get_latest_briefs() -> list[BloggerDailyBrief]:
    """For UI: get yesterday's brief for each eligible blogger.

    If yesterday's brief doesn't exist (e.g. job hasn't run yet today), fall
    back to the most recent available brief for that blogger.
    """
    day_str = _yesterday_str()
    bloggers = _eligible_bloggers()
    out: list[BloggerDailyBrief] = []
    async with db._SessionFactory() as s:
        for b in bloggers:
            row = await s.execute(
                select(BloggerDailyBrief)
                .where(BloggerDailyBrief.blogger_slug == b.slug)
                .where(BloggerDailyBrief.brief_date == day_str)
            )
            r = row.scalar_one_or_none()
            if r is None:
                # fallback: latest available
                row = await s.execute(
                    select(BloggerDailyBrief)
                    .where(BloggerDailyBrief.blogger_slug == b.slug)
                    .order_by(BloggerDailyBrief.brief_date.desc())
                    .limit(1)
                )
                r = row.scalar_one_or_none()
            if r is not None:
                out.append(r)
    return out
