"""Persona 月度自动更新 — 每月 1 号 06:00 执行。

读取旧 persona + 上月 brief 摘要 + 情绪聚合 → Qoder ultimate → 覆盖 persona 文件。
更新前后自动 git commit 归档。
"""
from __future__ import annotations

import json
import logging
import subprocess
from datetime import date, timedelta
from pathlib import Path

from sqlalchemy import func, select

from bigv_twins.config import BLOGGERS, settings
from bigv_twins.prompt_loader import load_prompt

from . import db
from .db import BloggerDailyBrief, TickerOpinionLog
from .qoder_call import call_qoder

log = logging.getLogger("bigv_twins.web.persona_updater")

_PERSONAS_DIR = Path(settings.personas_dir)
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent


def _eligible_bloggers():
    return [b for b in BLOGGERS if b.source == "zhihu" and b.is_blogger]


def _last_month_range() -> tuple[str, str]:
    """返回上个月的起止日期。"""
    today = date.today()
    first_of_this_month = today.replace(day=1)
    last_of_prev = first_of_this_month - timedelta(days=1)
    first_of_prev = last_of_prev.replace(day=1)
    return first_of_prev.strftime("%Y-%m-%d"), last_of_prev.strftime("%Y-%m-%d")


def _month_label() -> str:
    today = date.today()
    first_of_this_month = today.replace(day=1)
    last_of_prev = first_of_this_month - timedelta(days=1)
    return last_of_prev.strftime("%Y-%m")


async def _collect_monthly_briefs(slug: str, start: str, end: str) -> str:
    """收集上月的 brief 摘要。"""
    async with db._SessionFactory() as s:
        rows = await s.execute(
            select(BloggerDailyBrief)
            .where(BloggerDailyBrief.blogger_slug == slug)
            .where(BloggerDailyBrief.brief_date >= start)
            .where(BloggerDailyBrief.brief_date <= end)
            .where(BloggerDailyBrief.post_count > 0)
            .order_by(BloggerDailyBrief.brief_date)
        )
        briefs = list(rows.scalars())

    if not briefs:
        return "（本月无 brief 数据）"

    parts = []
    for br in briefs:
        bj = None
        try:
            bj = json.loads(br.brief_json) if br.brief_json else None
        except (json.JSONDecodeError, TypeError):
            pass

        if bj:
            mv = bj.get("main_view", "")[:300]
            quotes = bj.get("key_quotes", [])
            opinions = bj.get("ticker_opinions", [])
            actions = bj.get("actions_self_disclosed", [])

            day_parts = [f"### {br.brief_date}（{br.post_count} 篇）", f"主要观点：{mv}"]
            if quotes:
                day_parts.append("金句：" + " / ".join(f"「{q}」" for q in quotes[:3]))
            if opinions:
                op_strs = []
                for op in opinions[:5]:
                    op_strs.append(f"{op.get('ticker_name', '')} {op.get('sentiment', '')}({op.get('confidence', '')})")
                day_parts.append("个股情绪：" + " · ".join(op_strs))
            if actions:
                act_strs = [f"{a.get('action', '')} {a.get('ticker_name', '')}" for a in actions[:3]]
                day_parts.append("自报操作：" + " · ".join(act_strs))
            parts.append("\n".join(day_parts))
        elif br.brief_md:
            parts.append(f"### {br.brief_date}\n{br.brief_md[:300]}")

    return "\n\n".join(parts)


async def _collect_sentiment_summary(slug: str, start: str, end: str) -> str:
    """聚合上月的 ticker_opinion_log。"""
    async with db._SessionFactory() as s:
        rows = await s.execute(
            select(
                TickerOpinionLog.ticker_name,
                TickerOpinionLog.sentiment,
                func.count().label("cnt"),
            )
            .where(TickerOpinionLog.blogger_slug == slug)
            .where(TickerOpinionLog.opinion_date >= start)
            .where(TickerOpinionLog.opinion_date <= end)
            .group_by(TickerOpinionLog.ticker_name, TickerOpinionLog.sentiment)
            .order_by(func.count().desc())
        )
        data = list(rows)

    if not data:
        return "（本月无情绪数据）"

    # 按 ticker_name 分组
    by_ticker: dict[str, list] = {}
    for name, sent, cnt in data:
        by_ticker.setdefault(name, []).append(f"{sent}×{cnt}")

    lines = []
    for name, sents in sorted(by_ticker.items(), key=lambda x: -sum(int(s.split("×")[1]) for s in x[1])):
        lines.append(f"- {name}：{' / '.join(sents)}")

    return "\n".join(lines[:20])


def _git_commit(message: str) -> None:
    """在项目根目录执行 git add + commit。"""
    try:
        subprocess.run(["git", "add", "personas/"], cwd=_PROJECT_ROOT,
                       capture_output=True, timeout=10)
        subprocess.run(["git", "commit", "-m", message,
                        "--author", "PersonaBot <persona@bigv-twins>"],
                       cwd=_PROJECT_ROOT, capture_output=True, timeout=10)
    except Exception as e:
        log.warning("git commit failed: %s", e)


async def update_one_persona(slug: str, name: str) -> dict:
    """更新一个博主的 persona。返回 {status, slug, ...}。"""
    persona_path = _PERSONAS_DIR / f"{slug}.md"
    if not persona_path.exists():
        return {"status": "skip", "slug": slug, "reason": "no persona file"}

    old_persona = persona_path.read_text(encoding="utf-8")
    start, end = _last_month_range()
    month = _month_label()

    # 收集数据
    briefs_md = await _collect_monthly_briefs(slug, start, end)
    sentiment_md = await _collect_sentiment_summary(slug, start, end)

    if "本月无 brief 数据" in briefs_md:
        return {"status": "skip", "slug": slug, "reason": "no brief data"}

    # 构建 prompt
    prompt_tpl = load_prompt("persona/update-persona.md", month=month)
    user_input = (
        f"博主：{name}（slug={slug}）\n"
        f"更新月份：{month}\n\n"
        f"=== 旧 persona ===\n\n{old_persona}\n\n"
        f"=== 本月 brief 摘要（{start} ~ {end}）===\n\n{briefs_md}\n\n"
        f"=== 本月情绪分布 ===\n\n{sentiment_md}\n"
    )
    full_prompt = prompt_tpl + "\n\n---\n\n" + user_input

    # 调 Qoder
    log.info("updating persona for %s (%s), input %d chars", name, slug, len(user_input))
    new_persona = await call_qoder(full_prompt, "persona_update", slug, model="ultimate")

    if not new_persona or len(new_persona) < 200:
        return {"status": "error", "slug": slug, "reason": "LLM output too short"}

    # 写入文件
    persona_path.write_text(new_persona, encoding="utf-8")
    log.info("persona updated for %s: %d → %d chars", slug, len(old_persona), len(new_persona))

    return {"status": "updated", "slug": slug,
            "old_len": len(old_persona), "new_len": len(new_persona)}


async def run_monthly_persona_update() -> dict:
    """月度入口 — 更新所有知乎博主的 persona。"""
    month = _month_label()

    # 更新前 commit 旧版
    _git_commit(f"persona: backup before {month} monthly update")

    bloggers = _eligible_bloggers()
    results = {}
    for b in bloggers:
        try:
            r = await update_one_persona(b.slug, b.name)
            results[b.slug] = r
        except Exception as e:
            log.exception("persona update failed for %s: %s", b.slug, e)
            results[b.slug] = {"status": "error", "slug": b.slug, "reason": str(e)}

    # 更新后 commit 新版
    _git_commit(f"persona: monthly update {month} (auto-generated)")

    log.info("monthly persona update done: %s", results)
    return results
