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
    "新帖子（答案 / 文章 / 想法的原文），你的任务是**准确还原博主的真实表达**，"
    "让用户不用读原文也能精准 grasp 博主今天的判断、情绪、动作。\n\n"
    "## 输出 JSON 格式（严格）\n\n"
    "{\n"
    '  "main_view": "主要观点 80-180 字。多个主题用「；」串接并按重要性排序。",\n'
    '  "suggestion": "后续建议 ≤ 40 字。'
    "若博主没明确表态写「未明确表态」。\",\n"
    '  "ticker_opinions": [\n'
    '    {"ticker": "600519", "ticker_name": "贵州茅台", "sentiment": "bullish", '
    '"summary": "30字内一句话原文摘要"}\n'
    "  ]\n"
    "}\n\n"
    "## ticker_opinions 字段说明\n\n"
    "- 列出博主当日提到的**每只**股票。\n"
    "- sentiment 4 选 1：\n"
    "  - bullish（看多）— 明确推荐 / 买入 / 加仓 / 看好后市\n"
    "  - bearish（看空）— 明确不看好 / 觉得高估 / 预期下跌\n"
    "  - avoid（回避）— 明确建议不要碰 / 远离 / 风险大\n"
    "  - neutral（中性）— 仅提及讨论、跟踪、未明确态度\n"
    "- summary 必须**贴着原文**写，30 字以内，能引用就直接引用博主原话\n"
    "- ticker 只收 6 位 A 股代码或 5 位港股代码；只有名字找不到代码的省略\n\n"
    "## 写作硬约束\n\n"
    "1. 只输出 JSON，不要 markdown 代码块包裹\n"
    "2. main_view 用第三人称（「他认为」「他强调」），不要伪装博主第一人称\n"
    "3. **忠于原文** — 严禁外推、脑补、加戏；博主只说「看好 A」，不要写成「对 A 长期看好」\n"
    "4. 多个帖子讲不同主题时，main_view 用「；」分主题，按博主自己强调的程度排\n"
    "   重要的话题不要因为篇幅压缩被丢掉\n"
    "5. 博主对某票的态度有变化时（早上看多下午改口），sentiment 取**当日最后表态**，"
    "   summary 里要点出「由X转Y」\n"
    "6. 如果博主提了某票但只是顺带（如「想起去年买 X」），sentiment 用 neutral\n"
    "7. 不要把别人的观点（博主转述、引用别人的话）当成博主自己的观点\n"
)


_VALID_SENTIMENTS = {"bullish", "bearish", "avoid", "neutral"}


async def summarize_blogger(blogger_slug: str, blogger_name: str,
                            posts: list[dict]) -> dict:
    """LLM single call → {"main_view", "suggestion", "mentioned_tickers", "ticker_opinions"}.

    走 Qoder SDK performance（推理重，且需要严格忠于原文，flash 会丢信息 / 加戏）。
    ticker_opinions 是新加的：每只票直接带 sentiment + summary，省一次 LLM
    （之前 opinion_extractor 是单独一次调用解析这步的输出反推情绪）。

    Empty posts → returns a placeholder dict rather than calling LLM.
    """
    if not posts:
        return {
            "main_view": "（当日无新帖子）",
            "suggestion": "—",
            "mentioned_tickers": [],
            "ticker_opinions": [],
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

    prompt = _SUMMARIZER_SYS + "\n\n---\n\n" + "".join(user_input_parts)
    raw = await _call_qoder_brief(prompt, blogger_slug)
    if raw is None:
        return {
            "main_view": "（LLM 总结失败）",
            "suggestion": "—",
            "mentioned_tickers": [],
            "ticker_opinions": [],
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
            "main_view": full[:500] or "（解析失败）",
            "suggestion": "—",
            "mentioned_tickers": [],
            "ticker_opinions": [],
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
            ticker_opinions.append({
                "ticker": tcode,
                "ticker_name": str(op.get("ticker_name") or tcode)[:60],
                "sentiment": sent,
                "summary": str(op.get("summary") or "")[:80],
            })

    # mentioned_tickers 从 ticker_opinions 派生，向后兼容老消费者（backtest 等）
    mentioned = [op["ticker"] for op in ticker_opinions]

    return {
        "main_view": (obj.get("main_view") or "")[:800],
        "suggestion": (obj.get("suggestion") or "")[:80],
        "mentioned_tickers": mentioned,
        "ticker_opinions": ticker_opinions,
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
                # 把 brief LLM 一次性输出的 ticker_opinions 直接写入 ticker_opinion_log
                # （之前是再调一次 LLM 反推情绪 → 又慢又有信息损失）
                if result.get("ticker_opinions"):
                    await _write_opinion_log(b.slug, day_str, row.id,
                                              result["ticker_opinions"])
            except IntegrityError:
                await s.rollback()
                skipped += 1  # race with another instance

    log.info("generate_briefs_for_day(%s) done in %.1fs: generated=%d skipped=%d errors=%d",
             day_str, time.time() - t0, generated, skipped, errors)
    return {"generated": generated, "skipped_existing": skipped, "errors": errors}


async def _write_opinion_log(blogger_slug: str, brief_date: str,
                              brief_id: int, opinions: list[dict]) -> int:
    """把 ticker_opinions 列表写入 ticker_opinion_log 表。重复条目静默跳过。"""
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
