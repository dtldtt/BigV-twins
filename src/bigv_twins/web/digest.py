"""Daily Digest — 每日博主观点全局汇总。

在 blogger_brief 跑完之后执行（03:35 或手动触发），
读取每个博主的原文帖子 + brief_json，用方案 C 输入喂给 Qoder ultimate，
生成跨博主的全局 digest。

存储到 daily_digest 表（UNIQUE(digest_date)，重复跑覆盖）。
"""
from __future__ import annotations

import json
import logging
import re
from datetime import date, timedelta

from sqlalchemy import select

from bigv_twins.config import BLOGGERS, settings
from bigv_twins.prompt_loader import load_prompt

from . import db
from .db import BloggerDailyBrief, DailyDigest
from .blogger_brief import fetch_blogger_posts_for_day
from .qoder_call import call_qoder

log = logging.getLogger("bigv_twins.web.digest")


def _yesterday_str() -> str:
    return (date.today() - timedelta(days=1)).strftime("%Y-%m-%d")


def _eligible_bloggers():
    return [b for b in BLOGGERS if b.source == "zhihu" and b.is_blogger]


async def _get_briefs_for_day(day_str: str) -> dict:
    """从 DB 读已生成的 brief_json。"""
    result = {}
    async with db._SessionFactory() as s:
        rows = await s.execute(
            select(BloggerDailyBrief)
            .where(BloggerDailyBrief.brief_date == day_str)
            .where(BloggerDailyBrief.post_count > 0)
        )
        for br in rows.scalars():
            try:
                result[br.blogger_slug] = json.loads(br.brief_json)
            except (json.JSONDecodeError, TypeError):
                pass
    return result


def _build_input_c(briefs: dict, posts_data: dict) -> str:
    """方案 C：原文 + brief_json 混合输入。"""
    parts = []
    for slug, pdata in posts_data.items():
        parts.append(f"\n=== 博主: {pdata['name']} ===\n")
        bdata = briefs.get(slug)
        if bdata:
            parts.append("--- 结构化标注（已由第一轮 LLM 提取） ---")
            parts.append(json.dumps(bdata, ensure_ascii=False, indent=2))
            parts.append("")
        parts.append("--- 原文帖子 ---")
        for i, p in enumerate(pdata["posts"], 1):
            head = f"\n### 帖子 {i} ({p['content_type']}, voteup={p['voteup_count']})"
            if p["title"]:
                head += f" — {p['title']}"
            parts.append(head + "\n")
            parts.append(p["text"] + "\n")
    return "\n".join(parts)


def _append_blogger_links(digest_md: str, posts_data: dict) -> str:
    """在速览 section 里，给每个博主名后面自动附加原文链接（每行一个，用帖子标题）。"""
    for slug, pdata in posts_data.items():
        name = pdata["name"]
        links = []
        for p in pdata["posts"]:
            url = p.get("archive_url", "")
            if not url:
                continue
            ctype_label = {"answer": "回答", "article": "文章", "pin": "想法"}.get(
                p.get("content_type", ""), "帖子")
            title = p.get("title") or f"{ctype_label}（{p.get('created_time', '')[:16]}）"
            links.append(f"[{title}]({url})")
        if not links:
            continue
        links_md = "\n".join(f"  {lk}" for lk in links)
        pattern = re.compile(rf"(\*\*{re.escape(name)}\*\* — .+)")
        replacement = rf"\1\n{links_md}"
        digest_md = pattern.sub(replacement, digest_md, count=1)
    return digest_md


async def generate_daily_digest(day_str: str | None = None) -> dict:
    """生成某天的 daily digest。幂等 — 已存在则跳过。

    Returns {"status": "generated"/"skipped"/"no_data"/"error", ...}
    """
    if day_str is None:
        day_str = _yesterday_str()

    # 幂等检查
    async with db._SessionFactory() as s:
        existing = await s.execute(
            select(DailyDigest).where(DailyDigest.digest_date == day_str)
        )
        if existing.scalar_one_or_none() is not None:
            return {"status": "skipped", "date": day_str}

    # 收集数据
    briefs = await _get_briefs_for_day(day_str)
    posts_data = {}
    for b in _eligible_bloggers():
        posts = fetch_blogger_posts_for_day(b.author_id, day_str)
        if posts:
            posts_data[b.slug] = {"name": b.name, "posts": posts}

    if not posts_data:
        return {"status": "no_data", "date": day_str}

    # 构建输入（方案 C）
    user_input = _build_input_c(briefs, posts_data)
    prompt = load_prompt("brief/daily-digest.md")
    full_prompt = prompt + "\n\n---\n\n" + f"日期：{day_str}\n\n" + user_input

    # 调 Qoder ultimate
    log.info("generating digest for %s, %d bloggers, input %d chars",
             day_str, len(posts_data), len(user_input))
    raw = await call_qoder(full_prompt, "daily_digest", day_str, model="ultimate")

    if not raw:
        return {"status": "error", "date": day_str, "reason": "qoder returned empty"}

    # 后处理：给速览 section 补上原文链接
    digest_md = _append_blogger_links(raw, posts_data)

    # 存储
    async with db._SessionFactory() as s:
        row = DailyDigest(
            digest_date=day_str,
            digest_md=digest_md,
            digest_json=None,  # Phase 2 再做结构化解析
            input_type="C",
            model="ultimate",
            blogger_count=len(posts_data),
        )
        s.add(row)
        await s.commit()

    log.info("digest generated for %s: %d chars", day_str, len(digest_md))
    return {"status": "generated", "date": day_str, "chars": len(digest_md)}


async def get_digest_for_date(day_str: str) -> DailyDigest | None:
    """取某天的 digest（供 UI 展示）。"""
    async with db._SessionFactory() as s:
        row = await s.execute(
            select(DailyDigest).where(DailyDigest.digest_date == day_str)
        )
        return row.scalar_one_or_none()


async def get_latest_digest() -> DailyDigest | None:
    """取最近一份有内容的 digest。"""
    async with db._SessionFactory() as s:
        row = await s.execute(
            select(DailyDigest)
            .order_by(DailyDigest.digest_date.desc())
            .limit(1)
        )
        return row.scalar_one_or_none()
