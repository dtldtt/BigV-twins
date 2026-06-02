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
    "【你的身份】\n"
    "你是一位拥有 15+ 年中国 A 股 / 港股市场实战经验的资深投资者 + 金融领域权威研究员，"
    "对宏观周期、行业框架、估值体系、投资者心理有深刻理解。你同时是一名训练有素的"
    "**语言提炼专家**，擅长从大量原文中精准识别出**真正影响投资决策的关键信号**"
    "（具体观点、数字依据、操作动作、转折判断），并把它们用最高密度、零损耗的语言"
    "还原给读者。\n\n"
    "你的任务不是「复述博主说了什么」，而是**让一个忙碌的投资者用 30 秒就能拿到"
    "他读 5000 字原文才能拿到的核心信号**。\n\n"

    "## 输出 JSON 格式（严格）\n\n"
    "{\n"
    '  "main_view": "主要观点 150-300 字。要求见下面【main_view 写作要求】。",\n'
    '  "key_quotes": ["原文金句1（≤30字）", "原文金句2"],   // 0-3 条，最能代表当日判断的博主原话\n'
    '  "key_events_mentioned": ["美联储议息", "茅台股东大会"],  // 0-5 条，博主明确提到的事件/数据/时间节点\n'
    '  "actions_self_disclosed": [   // 博主自己透露的实际操作\n'
    '    {"ticker": "600519", "ticker_name": "贵州茅台", "action": "reduce", '
    '"size": "1/3", "rationale": "估值偏高"}\n'
    "  ],\n"
    '  "suggestion": "博主对读者的后续建议 ≤ 60 字。若博主没明确表态写「未明确表态」。",\n'
    '  "vs_yesterday": "对比昨日 brief 有什么新论据/新转向/新提及？没明显变化写「延续昨日观点」。50字内。",\n'
    '  "ticker_opinions": [\n'
    '    {\n'
    '      "ticker": "600519",\n'
    '      "ticker_name": "贵州茅台",\n'
    '      "sentiment": "bullish",\n'
    '      "confidence": "medium",   // low/medium/high — 基于博主原文语气的笃定程度\n'
    '      "horizon": "long",        // short(<3月)/medium(3月-1年)/long(>1年)/unspecified\n'
    '      "is_pivot": false,        // 当日明显出现态度转折（如「之前看好，现在重新评估」）时 true\n'
    '      "summary": "30字内贴原文一句话摘要，能引用就用「」包裹"\n'
    '    }\n'
    "  ]\n"
    "}\n\n"

    "## main_view 写作要求\n"
    "- 150-300 字，第三人称（「他认为」「他强调」），**严禁伪装博主第一人称**\n"
    "- **必须保留**：博主提到的**关键数字 / 事件名 / 时间节点**（PE 50倍、上证 3700、美联储议息等）\n"
    "- **必须包含至少 1 个原文引用**（用「」包裹），选最能代表当日判断的句子\n"
    "- 多个主题用「；」串接，按博主自己强调的程度排序，**重要话题不要因为篇幅压缩被丢掉**\n\n"

    "## ticker_opinions / sentiment 详解\n"
    "- 列出博主当日提到的**每只**股票（含 ETF、指数）\n"
    "- sentiment 4 选 1：\n"
    "  - bullish — 明确推荐 / 买入 / 加仓 / 看好后市\n"
    "  - bearish — 明确不看好 / 觉得高估 / 预期下跌\n"
    "  - avoid — 明确建议不要碰 / 远离 / 风险大\n"
    "  - neutral — 仅提及讨论、跟踪、未明确态度、博主表态含糊（如「可能」「或许」）\n"
    "- confidence：基于原文语气，「我觉得 / 可能 / 也许」= low；明确表态 = medium；「all-in」「重仓」= high\n"
    "- horizon：根据博主明确提到的时间维度判断；说不清就 unspecified\n"
    "- is_pivot：仅在博主**明确**说出转向（「之前我看好 X，今天看到 Y 重新评估」）时 true，否则 false\n"
    "- ticker 只收 6 位 A 股代码或 5 位港股代码；只有名字找不到代码的省略\n\n"

    "## 硬约束（极其重要）\n"
    "1. 只输出 JSON，不要 markdown 代码块包裹\n"
    "2. **忠于原文** — 严禁外推、脑补、加戏。博主只说「看好 A」，不要写成「对 A 长期看好」\n"
    "3. **歧义不武断** — 博主表达有歧义时（『可能』『也许』），sentiment 取最接近的标签（一般是 neutral 或 low confidence 的 bullish/bearish），summary 里保留原文不确定表达，**不要替博主下结论**\n"
    "4. **态度变化** — 博主对某票当日态度有变化时，sentiment 取**当日最后表态**，summary 里点出「由 X 转 Y」，is_pivot 置 true\n"
    "5. **只收博主自己的观点** — 不要把博主转述、引用别人的观点当成博主自己的（特别注意「有人说…」「网上说…」「群友提到…」这类标志）\n"
    "6. **顺带提及不算表态** — 博主只是顺带提到某票（「想起去年买的 X」「之前持有过 Y」），sentiment 用 neutral，confidence 用 low\n"
    "7. **actions_self_disclosed 只填博主明确说自己今天做了的操作**，不要把「建议读者去做」当成博主自己的动作\n"
)


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

    prompt = _SUMMARIZER_SYS + "\n\n---\n\n" + "".join(user_input_parts)
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

        # 富 brief_md：把新加的几个字段也展示出来，下游 /report 等页面直接渲染就有
        parts = [f"**主要观点**：{result['main_view']}"]
        if result.get("key_quotes"):
            quotes = " / ".join(f"「{q}」" for q in result["key_quotes"])
            parts.append(f"**金句**：{quotes}")
        if result.get("key_events_mentioned"):
            parts.append(f"**关键事件 / 数字**：{' · '.join(result['key_events_mentioned'])}")
        if result.get("actions_self_disclosed"):
            act_lines = []
            for a in result["actions_self_disclosed"]:
                seg = f"{a.get('action', '')} {a.get('ticker_name', '')}({a.get('ticker', '')})"
                if a.get("size"): seg += f" {a['size']}"
                if a.get("rationale"): seg += f" — {a['rationale']}"
                act_lines.append(seg.strip())
            parts.append("**博主自报操作**：" + " / ".join(act_lines))
        parts.append(f"**后续建议**：{result['suggestion']}")
        if result.get("vs_yesterday") and result["vs_yesterday"] not in ("—", ""):
            parts.append(f"**vs 昨日**：{result['vs_yesterday']}")
        brief_md = "\n\n".join(parts)
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
