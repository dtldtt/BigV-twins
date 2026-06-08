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
from bigv_twins.prompt_loader import load_prompt

from . import db
from .db import BloggerDailyBrief, TickerOpinionLog

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

    Returns oldest-first list of dicts with zhihu_id, content_type, title, text, url, archive_url, votes, created_time.
    """
    src = _open_zhihu_ro()
    try:
        sql = (
            "SELECT id, zhihu_id, content_type, title, content, voteup_count, url, created_time "
            "FROM contents WHERE author_id = ? AND content IS NOT NULL AND content <> '' "
            "AND date(created_time) = ? ORDER BY created_time"
        )
        rows = src.execute(sql, (author_id, day_str)).fetchall()
        out = []
        for r in rows:
            db_id, zid, ctype, title, content, votes, url, ct = r
            out.append({
                "zhihu_id": zid,
                "content_type": ctype,
                "title": title or "",
                "text": _strip_html(content)[:3000],
                "url": url or "",
                "archive_url": f"https://8-155-174-112.nip.io:8000/content/{db_id}",
                "voteup_count": int(votes or 0),
                "created_time": ct,
            })
        return out
    finally:
        src.close()



def _get_summarizer_sys() -> str:
    return load_prompt("brief/blogger-daily.md")



_VALID_SENTIMENTS = {"bullish", "bearish", "avoid", "neutral"}


async def _fetch_yesterday_brief(blogger_slug: str, today_date_str: str) -> str:
    """拉该博主昨天的 brief（如有），用于 prompt 里"vs_yesterday" 对比。"""
    from datetime import datetime as _dt, timedelta as _td
    try:
        today_dt = _dt.strptime(today_date_str, "%Y-%m-%d").date()
    except ValueError:
        return ""
    yesterday_str = (today_dt - _td(days=1)).strftime("%Y-%m-%d")
    async with db._SessionFactory() as s:
        row = await s.execute(
            select(BloggerDailyBrief)
            .where(BloggerDailyBrief.blogger_slug == blogger_slug)
            .where(BloggerDailyBrief.brief_date == yesterday_str)
            .limit(1)
        )
        br = row.scalar_one_or_none()
    return br.brief_md if br and br.brief_md else ""


async def summarize_blogger(blogger_slug: str, blogger_name: str,
                            posts: list[dict]) -> dict:
    """LLM single call → 7-field JSON (main_view, key_quotes, key_events,
    actions_self_disclosed, suggestion, vs_yesterday, ticker_opinions)。

    走 Qoder SDK performance（推理重，且需要严格忠于原文）。
    """
    if not posts:
        return {
            "main_view": "（当日无新帖子）",
            "suggestion": "—",
            "mentioned_tickers": [],
            "ticker_opinions": [],
            "key_quotes": [], "key_events_mentioned": [],
            "actions_self_disclosed": [], "vs_yesterday": "—",
        }

    today_date_str = posts[0]["created_time"][:10]
    yesterday_md = await _fetch_yesterday_brief(blogger_slug, today_date_str)

    user_input_parts = [f"博主：{blogger_name} (slug={blogger_slug})\n",
                        f"日期：{today_date_str}\n",
                        f"当日新帖共 {len(posts)} 篇\n"]
    if yesterday_md:
        user_input_parts.append(f"\n=== 昨日 brief（仅供「今天有什么新/转向」对比，不要复述） ===\n{yesterday_md}\n=== 昨日 brief 结束 ===\n")
    user_input_parts.append("\n---\n")
    for i, p in enumerate(posts, 1):
        head = f"\n### 帖子 {i} ({p['content_type']}, voteup={p['voteup_count']})"
        if p["title"]:
            head += f" — {p['title']}"
        user_input_parts.append(head + "\n")
        user_input_parts.append(p["text"] + "\n")

    prompt = _get_summarizer_sys() + "\n\n---\n\n" + "".join(user_input_parts)
    raw = await _call_qoder_brief(prompt, blogger_slug)
    if raw is None:
        return {
            "main_view": "（LLM 总结失败）", "suggestion": "—",
            "mentioned_tickers": [], "ticker_opinions": [],
            "key_quotes": [], "key_events_mentioned": [],
            "actions_self_disclosed": [], "vs_yesterday": "—",
        }

    full = raw.strip()
    if full.startswith("```"):
        full = re.sub(r"^```(?:json)?\n", "", full)
        full = re.sub(r"\n```$", "", full)
    try:
        obj = json.loads(full)
    except json.JSONDecodeError:
        log.warning("blogger_brief JSON parse failed for %s: %r", blogger_slug, full[:400])
        return {
            "main_view": full[:600] or "（解析失败）", "suggestion": "—",
            "mentioned_tickers": [], "ticker_opinions": [],
            "key_quotes": [], "key_events_mentioned": [],
            "actions_self_disclosed": [], "vs_yesterday": "—",
        }

    # 校验 + 归一 ticker_opinions
    raw_ops = obj.get("ticker_opinions") or []
    ticker_opinions: list[dict] = []
    if isinstance(raw_ops, list):
        for op in raw_ops:
            if not isinstance(op, dict):
                continue
            tcode = str(op.get("ticker", "")).strip()
            if not (tcode.isdigit() and len(tcode) in (5, 6)):
                continue
            sent = op.get("sentiment", "neutral")
            if sent not in _VALID_SENTIMENTS:
                sent = "neutral"
            conf = op.get("confidence", "medium")
            if conf not in ("low", "medium", "high"):
                conf = "medium"
            hor = op.get("horizon", "unspecified")
            if hor not in ("short", "medium", "long", "unspecified"):
                hor = "unspecified"
            ticker_opinions.append({
                "ticker": tcode,
                "ticker_name": str(op.get("ticker_name") or tcode)[:60],
                "sentiment": sent,
                "confidence": conf,
                "horizon": hor,
                "is_pivot": bool(op.get("is_pivot", False)),
                "summary": str(op.get("summary") or "")[:100],
            })
    mentioned = [op["ticker"] for op in ticker_opinions]

    # 校验新加的列表字段
    def _str_list(key: str, max_len: int = 5, item_cap: int = 60) -> list[str]:
        v = obj.get(key) or []
        if not isinstance(v, list):
            return []
        return [str(x)[:item_cap] for x in v if x][:max_len]

    raw_actions = obj.get("actions_self_disclosed") or []
    actions: list[dict] = []
    if isinstance(raw_actions, list):
        for a in raw_actions:
            if not isinstance(a, dict):
                continue
            actions.append({
                "ticker": str(a.get("ticker") or "")[:16],
                "ticker_name": str(a.get("ticker_name") or "")[:60],
                "action": str(a.get("action") or "")[:20],
                "size": str(a.get("size") or "")[:40],
                "rationale": str(a.get("rationale") or "")[:200],
            })

    return {
        "main_view": (obj.get("main_view") or "")[:1200],
        "suggestion": (obj.get("suggestion") or "")[:120],
        "mentioned_tickers": mentioned,
        "ticker_opinions": ticker_opinions,
        # 新加的 4 个字段
        "key_quotes": _str_list("key_quotes", max_len=3, item_cap=80),
        "key_events_mentioned": _str_list("key_events_mentioned", max_len=5, item_cap=80),
        "actions_self_disclosed": actions[:10],
        "vs_yesterday": (obj.get("vs_yesterday") or "")[:200],
    }


async def _call_qoder_brief(prompt: str, blogger_slug: str) -> str | None:
    """走 Qoder SDK performance 跑博主日报总结。失败返回 None。"""
    if not settings.qoder_personal_access_token:
        log.warning("brief %s skipped: QODER_PERSONAL_ACCESS_TOKEN not set", blogger_slug)
        return None
    try:
        from qoder_agent_sdk import (
            AssistantMessage, QoderAgentOptions, access_token, query,
        )
    except ImportError as e:
        log.warning("qoder_agent_sdk import failed: %s", e)
        return None
    options = QoderAgentOptions(
        auth=access_token(settings.qoder_personal_access_token),
        model="performance",
    )
    pieces: list[str] = []
    try:
        async for msg in query(prompt=prompt, options=options):
            if isinstance(msg, AssistantMessage):
                content = getattr(msg, "content", None)
                if isinstance(content, list):
                    for c in content:
                        if isinstance(c, dict) and c.get("type") == "text":
                            pieces.append(c.get("text", ""))
                        elif hasattr(c, "text"):
                            pieces.append(c.text)
                elif isinstance(content, str):
                    pieces.append(content)
    except Exception as e:
        log.warning("qoder brief failed for %s: %s", blogger_slug, e)
        return None
    text = "".join(pieces).strip()
    return text or None


def _render_brief_md(result: dict, posts: list[dict] | None = None) -> str:
    """把 summarize_blogger 返回的结构化 dict 渲染成 brief_md markdown。"""
    _SENT_LABEL = {"bullish": "📈 看多", "bearish": "📉 看空",
                   "avoid": "⚠️ 回避", "neutral": "➖ 中性"}

    parts = [f"### 主要观点\n\n{result['main_view']}"]

    if result.get("key_quotes"):
        quotes = "\n".join(f"- 「{q}」" for q in result["key_quotes"])
        parts.append(f"### 金句\n\n{quotes}")

    if result.get("key_events_mentioned"):
        events = "\n".join(f"- {e}" for e in result["key_events_mentioned"])
        parts.append(f"### 关键事件\n\n{events}")

    if result.get("actions_self_disclosed"):
        act_lines = []
        for a in result["actions_self_disclosed"]:
            seg = f"**{a.get('action', '')}** {a.get('ticker_name', '')}({a.get('ticker', '')})"
            if a.get("size"):
                seg += f" {a['size']}"
            if a.get("rationale"):
                seg += f" — {a['rationale']}"
            act_lines.append(f"- {seg.strip()}")
        parts.append("### 博主自报操作\n\n" + "\n".join(act_lines))

    if result.get("ticker_opinions"):
        op_lines = []
        for op in result["ticker_opinions"]:
            label = _SENT_LABEL.get(op["sentiment"], op["sentiment"])
            line = f"- **{op['ticker_name']}**({op.get('ticker', '')}) {label}"
            conf = op.get("confidence", "")
            if conf:
                line += f" · {conf}"
            if op.get("is_pivot"):
                line += " **[转向]**"
            if op.get("summary"):
                line += f"\n  > {op['summary']}"
            op_lines.append(line)
        parts.append("### 个股情绪\n\n" + "\n".join(op_lines))

    parts.append(f"### 后续建议\n\n{result['suggestion']}")

    if result.get("vs_yesterday") and result["vs_yesterday"] not in ("—", ""):
        parts.append(f"### vs 昨日\n\n{result['vs_yesterday']}")

    if posts:
        links = []
        for p in posts:
            ctype_label = {"answer": "回答", "article": "文章", "pin": "想法"}.get(
                p["content_type"], p["content_type"])
            title = p["title"] or f"{ctype_label}（{p['created_time'][:16]}）"
            archive = p.get("archive_url", p.get("url", ""))
            if archive:
                links.append(f"- [{title}]({archive})")
            else:
                links.append(f"- {title}")
        parts.append("### 原文链接\n\n" + "\n".join(links))

    return "\n\n".join(parts)


async def _generate_one_brief(b, day_str: str) -> tuple[str, str, dict | None, int]:
    """为单个博主生成 brief。返回 (slug, status, result, post_count)。
    status: "generated" | "skipped" | "error"
    """
    async with db._SessionFactory() as s:
        existing = await s.execute(
            select(BloggerDailyBrief)
            .where(BloggerDailyBrief.blogger_slug == b.slug)
            .where(BloggerDailyBrief.brief_date == day_str)
        )
        if existing.scalar_one_or_none() is not None:
            return (b.slug, "skipped", None, 0)

    try:
        posts = fetch_blogger_posts_for_day(b.author_id, day_str)
        log.info("  [%s] %d posts for %s", b.slug, len(posts), day_str)
        result = await summarize_blogger(b.slug, b.name, posts)
    except Exception as e:
        log.exception("blogger %s brief generation failed: %s", b.slug, e)
        return (b.slug, "error", None, 0)

    brief_md = _render_brief_md(result, posts=posts)
    async with db._SessionFactory() as s:
        row = BloggerDailyBrief(
            blogger_slug=b.slug,
            brief_date=day_str,
            brief_md=brief_md,
            brief_json=json.dumps(result, ensure_ascii=False),
            mentioned_tickers=json.dumps(result["mentioned_tickers"], ensure_ascii=False),
            post_count=len(posts),
        )
        s.add(row)
        try:
            await s.commit()
            if result.get("ticker_opinions"):
                await _write_opinion_log(b.slug, day_str, row.id,
                                          result["ticker_opinions"])
            return (b.slug, "generated", result, len(posts))
        except IntegrityError:
            await s.rollback()
            return (b.slug, "skipped", None, len(posts))


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

    import asyncio
    results = await asyncio.gather(
        *[_generate_one_brief(b, day_str) for b in bloggers],
        return_exceptions=True,
    )

    generated = skipped = errors = 0
    for r in results:
        if isinstance(r, Exception):
            log.exception("unexpected brief generation error: %s", r)
            errors += 1
        else:
            _slug, status, _result, _pc = r
            if status == "generated":
                generated += 1
            elif status == "skipped":
                skipped += 1
            else:
                errors += 1

    log.info("generate_briefs_for_day(%s) done in %.1fs: generated=%d skipped=%d errors=%d",
             day_str, time.time() - t0, generated, skipped, errors)
    return {"generated": generated, "skipped_existing": skipped, "errors": errors}


async def _write_opinion_log(blogger_slug: str, brief_date: str,
                              brief_id: int, opinions: list[dict]) -> int:
    """把 ticker_opinions 列表写入 ticker_opinion_log 表。"""
    count = 0
    async with db._SessionFactory() as session:
        for op in opinions:
            try:
                session.add(TickerOpinionLog(
                    ticker=op["ticker"],
                    ticker_name=op.get("ticker_name", op["ticker"]),
                    blogger_slug=blogger_slug,
                    opinion_date=brief_date,
                    sentiment=op["sentiment"],
                    confidence=op.get("confidence", "medium"),
                    horizon=op.get("horizon", "unspecified"),
                    is_pivot=op.get("is_pivot", False),
                    summary=op.get("summary", "")[:100],
                    source_brief_id=brief_id,
                ))
                await session.flush()
                count += 1
            except Exception:
                await session.rollback()
                continue
        await session.commit()
    return count


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
