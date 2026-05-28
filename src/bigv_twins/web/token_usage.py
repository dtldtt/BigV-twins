"""Token usage tracker — scan OpenClaw session JSONL, aggregate hourly.

Runs on a schedule. No LLM calls. Just file scanning + DB UPSERT.

Storage:
  token_usage_hourly table — primary key (hour_bucket)
    hour_bucket: 'YYYY-MM-DDTHH' (UTC-aligned to local Asia/Shanghai)
    total_calls / total_input / total_output / cache_read / cache_create
    by_agent_json: {"advisor": {"calls":N, "input":N, ...}, ...}
    by_model_json: {"qwen3.6-flash": {"calls":N, ...}, ...}
    updated_at: ISO timestamp
"""
from __future__ import annotations

import json
import logging
import os
import time
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path

log = logging.getLogger("bigv_twins.web.token_usage")

# Local timezone (Asia/Shanghai = UTC+8)
LOCAL_TZ_OFFSET = timedelta(hours=8)

OPENCLAW_AGENTS_DIR = Path.home() / ".openclaw" / "agents"


def _parse_iso_to_local_hour(iso_ts: str) -> str | None:
    """Convert ISO 8601 timestamp (UTC) to local hour bucket 'YYYY-MM-DDTHH'."""
    try:
        # Strip Z if present, parse as UTC
        dt = datetime.fromisoformat(iso_ts.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        # Convert to local time
        local_dt = dt + LOCAL_TZ_OFFSET
        return local_dt.strftime("%Y-%m-%dT%H")
    except (ValueError, TypeError):
        return None


def _scan_sessions() -> dict[str, dict]:
    """Walk all OpenClaw session JSONL files, aggregate by (hour, agent, model).

    Returns: {hour_bucket: {by_agent: {agent: {calls, input, output, ...}},
                            by_model: {model: {...}}, totals: {...}}}
    """
    by_hour: dict[str, dict] = defaultdict(lambda: {
        "by_agent": defaultdict(lambda: {
            "calls": 0, "input": 0, "output": 0,
            "cache_read": 0, "cache_create": 0,
        }),
        "by_model": defaultdict(lambda: {
            "calls": 0, "input": 0, "output": 0,
            "cache_read": 0, "cache_create": 0,
        }),
        "totals": {
            "calls": 0, "input": 0, "output": 0,
            "cache_read": 0, "cache_create": 0,
        },
    })

    if not OPENCLAW_AGENTS_DIR.exists():
        log.warning("OpenClaw agents dir not found: %s", OPENCLAW_AGENTS_DIR)
        return {}

    for agent_dir in OPENCLAW_AGENTS_DIR.iterdir():
        if not agent_dir.is_dir():
            continue
        agent_name = agent_dir.name
        sess_dir = agent_dir / "sessions"
        if not sess_dir.is_dir():
            continue

        for jsonl_path in sess_dir.glob("*.jsonl"):
            try:
                with jsonl_path.open("r", encoding="utf-8") as f:
                    for line in f:
                        try:
                            d = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        if d.get("type") != "message":
                            continue
                        msg = d.get("message", {})
                        usage = msg.get("usage")
                        if not usage:
                            continue

                        hour = _parse_iso_to_local_hour(d.get("timestamp", ""))
                        if not hour:
                            continue
                        model = msg.get("model", "unknown")

                        inp = int(usage.get("input", 0) or 0)
                        out = int(usage.get("output", 0) or 0)
                        cr = int(usage.get("cache_read", 0) or 0)
                        cc = int(usage.get("cache_creation",
                                          usage.get("cache_create", 0)) or 0)

                        bh = by_hour[hour]
                        for bucket in (bh["by_agent"][agent_name],
                                       bh["by_model"][model],
                                       bh["totals"]):
                            bucket["calls"] += 1
                            bucket["input"] += inp
                            bucket["output"] += out
                            bucket["cache_read"] += cr
                            bucket["cache_create"] += cc
            except (IOError, OSError) as e:
                log.warning("failed to read %s: %s", jsonl_path, e)
                continue

    # Convert defaultdicts to regular dicts for clean JSON
    out = {}
    for hour, data in by_hour.items():
        out[hour] = {
            "by_agent": {k: dict(v) for k, v in data["by_agent"].items()},
            "by_model": {k: dict(v) for k, v in data["by_model"].items()},
            "totals": dict(data["totals"]),
        }
    return out


async def refresh_token_usage() -> dict:
    """Cron job entry point: rescan all sessions, UPSERT into DB.

    Runs in ~2-5 seconds. No LLM. Idempotent (replaces existing rows).
    """
    t0 = time.time()
    agg = _scan_sessions()
    log.info("token usage: scanned %d hours", len(agg))

    from . import db
    from .db import TokenUsageHourly

    async with db._SessionFactory() as session:
        from sqlalchemy import select
        for hour, data in agg.items():
            existing = await session.execute(
                select(TokenUsageHourly).where(TokenUsageHourly.hour == hour)
            )
            row = existing.scalar_one_or_none()
            totals = data["totals"]
            if row is None:
                row = TokenUsageHourly(
                    hour=hour,
                    total_calls=totals["calls"],
                    total_input=totals["input"],
                    total_output=totals["output"],
                    total_cache_read=totals["cache_read"],
                    total_cache_create=totals["cache_create"],
                    by_agent_json=json.dumps(data["by_agent"], ensure_ascii=False),
                    by_model_json=json.dumps(data["by_model"], ensure_ascii=False),
                )
                session.add(row)
            else:
                row.total_calls = totals["calls"]
                row.total_input = totals["input"]
                row.total_output = totals["output"]
                row.total_cache_read = totals["cache_read"]
                row.total_cache_create = totals["cache_create"]
                row.by_agent_json = json.dumps(data["by_agent"], ensure_ascii=False)
                row.by_model_json = json.dumps(data["by_model"], ensure_ascii=False)
                row.updated_at = datetime.now(timezone.utc)
        await session.commit()

    log.info("token usage refresh done in %.1fs, %d hours updated",
             time.time() - t0, len(agg))
    return {"hours_updated": len(agg), "elapsed_s": round(time.time() - t0, 1)}


# Pricing per 1M tokens (元) — credit = 元 × 100
MODEL_PRICING = {
    "qwen3.6-flash": {
        "input": 1.2, "output": 7.2,
        "cache_read": 0.12, "cache_create": 1.5,
        "label": "qwen3.6-flash",
        "tier": "经济",
    },
    "qwen3.6-plus": {
        "input": 2.0, "output": 12.0,
        "cache_read": 0.2, "cache_create": 2.5,
        "label": "qwen3.6-plus",
        "tier": "均衡",
    },
    "qwen3.7-max": {
        # ORIGINAL prices (no promo discount applied per user request)
        "input": 12.0, "output": 36.0,
        "cache_read": 1.2, "cache_create": 15.0,
        "label": "qwen3.7-max",
        "tier": "旗舰",
    },
}


def tokens_to_credits(input_t: int, output_t: int, cache_r: int = 0,
                     cache_c: int = 0, model: str = "qwen3.6-flash") -> float:
    """Apply model pricing to compute credits. 1 credit = 0.01 元."""
    p = MODEL_PRICING.get(model, MODEL_PRICING["qwen3.6-flash"])
    yuan = (
        input_t * p["input"] / 1_000_000
        + output_t * p["output"] / 1_000_000
        + cache_r * p["cache_read"] / 1_000_000
        + cache_c * p["cache_create"] / 1_000_000
    )
    return round(yuan * 100, 2)


def _local_now() -> datetime:
    """Local Shanghai time."""
    return datetime.now(timezone.utc) + LOCAL_TZ_OFFSET


def _month_cycle_range() -> tuple[str, str]:
    """Compute current 'billing month' boundary (27th to next 27th)."""
    now = _local_now()
    if now.day >= 27:
        start = now.replace(day=27, hour=0, minute=0, second=0, microsecond=0)
    else:
        # Previous month's 27th
        prev = now.replace(day=1) - timedelta(days=1)  # last day of prev month
        start = prev.replace(day=27, hour=0, minute=0, second=0, microsecond=0)
    # End = start + ~30 days (next 27th)
    next_month = start.replace(day=1) + timedelta(days=32)
    end = next_month.replace(day=27, hour=0, minute=0, second=0, microsecond=0)
    return (start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d"))


async def get_dashboard_stats(model: str = "qwen3.6-flash") -> dict:
    """Build dashboard data: intraday / daily / monthly + headline totals.

    Returns:
      {
        "intraday": [{hour: '15', calls, input, output, credits}, ... 24 buckets],
        "daily":    [{day: '2026-05-28', calls, input, output, credits}, ... last 30 days],
        "monthly":  [{month: '2026-05', calls, input, output, credits}, ... last 12 months],
        "today":    {calls, input, output, credits},
        "this_month": {calls, input, output, credits, range: 'YYYY-MM-DD to YYYY-MM-DD'},
        "model":    "qwen3.6-flash",
      }
    """
    from . import db
    from .db import TokenUsageHourly
    from sqlalchemy import select

    now = _local_now()
    today_str = now.strftime("%Y-%m-%d")
    month_start, month_end = _month_cycle_range()

    async with db._SessionFactory() as session:
        # Fetch last 90 days of hourly data (enough for all views)
        cutoff = (now - timedelta(days=90)).strftime("%Y-%m-%dT%H")
        rows = await session.execute(
            select(TokenUsageHourly).where(TokenUsageHourly.hour >= cutoff)
            .order_by(TokenUsageHourly.hour)
        )
        all_rows = list(rows.scalars())

    # ----- Intraday (today, 24 hour buckets)
    intraday_map = {h: {"calls": 0, "input": 0, "output": 0,
                       "cache_read": 0, "cache_create": 0}
                   for h in range(24)}
    for r in all_rows:
        if not r.hour.startswith(today_str):
            continue
        try:
            h = int(r.hour[-2:])
        except ValueError:
            continue
        intraday_map[h]["calls"] += r.total_calls or 0
        intraday_map[h]["input"] += r.total_input or 0
        intraday_map[h]["output"] += r.total_output or 0
        intraday_map[h]["cache_read"] += r.total_cache_read or 0
        intraday_map[h]["cache_create"] += r.total_cache_create or 0

    intraday = []
    for h in range(24):
        d = intraday_map[h]
        intraday.append({
            "label": f"{h:02d}:00",
            "calls": d["calls"],
            "input": d["input"],
            "output": d["output"],
            "credits": tokens_to_credits(d["input"], d["output"],
                                         d["cache_read"], d["cache_create"],
                                         model),
        })

    # ----- Daily (last 30 days)
    daily_map = defaultdict(lambda: {
        "calls": 0, "input": 0, "output": 0, "cache_read": 0, "cache_create": 0,
    })
    for r in all_rows:
        day = r.hour[:10]
        d = daily_map[day]
        d["calls"] += r.total_calls or 0
        d["input"] += r.total_input or 0
        d["output"] += r.total_output or 0
        d["cache_read"] += r.total_cache_read or 0
        d["cache_create"] += r.total_cache_create or 0

    # Pad to last 30 days
    daily = []
    for i in range(30, 0, -1):
        day = (now - timedelta(days=i - 1)).strftime("%Y-%m-%d")
        d = daily_map.get(day, {
            "calls": 0, "input": 0, "output": 0, "cache_read": 0, "cache_create": 0,
        })
        daily.append({
            "label": day[5:],  # MM-DD
            "calls": d["calls"],
            "input": d["input"],
            "output": d["output"],
            "credits": tokens_to_credits(d["input"], d["output"],
                                         d["cache_read"], d["cache_create"], model),
        })

    # ----- Monthly (last 6 months by calendar month)
    monthly_map = defaultdict(lambda: {
        "calls": 0, "input": 0, "output": 0, "cache_read": 0, "cache_create": 0,
    })
    for r in all_rows:
        month = r.hour[:7]  # YYYY-MM
        m = monthly_map[month]
        m["calls"] += r.total_calls or 0
        m["input"] += r.total_input or 0
        m["output"] += r.total_output or 0
        m["cache_read"] += r.total_cache_read or 0
        m["cache_create"] += r.total_cache_create or 0

    monthly = []
    for i in range(5, -1, -1):
        # Walk back 6 months
        ref = now.replace(day=15) - timedelta(days=i * 30)
        month = ref.strftime("%Y-%m")
        m = monthly_map.get(month, {
            "calls": 0, "input": 0, "output": 0, "cache_read": 0, "cache_create": 0,
        })
        monthly.append({
            "label": month,
            "calls": m["calls"],
            "input": m["input"],
            "output": m["output"],
            "credits": tokens_to_credits(m["input"], m["output"],
                                         m["cache_read"], m["cache_create"], model),
        })

    # ----- Today total
    today_totals = {"calls": 0, "input": 0, "output": 0,
                   "cache_read": 0, "cache_create": 0}
    for r in all_rows:
        if r.hour.startswith(today_str):
            today_totals["calls"] += r.total_calls or 0
            today_totals["input"] += r.total_input or 0
            today_totals["output"] += r.total_output or 0
            today_totals["cache_read"] += r.total_cache_read or 0
            today_totals["cache_create"] += r.total_cache_create or 0
    today_totals["credits"] = tokens_to_credits(
        today_totals["input"], today_totals["output"],
        today_totals["cache_read"], today_totals["cache_create"], model)

    # ----- This billing month (27th cycle)
    month_totals = {"calls": 0, "input": 0, "output": 0,
                   "cache_read": 0, "cache_create": 0}
    for r in all_rows:
        day = r.hour[:10]
        if month_start <= day < month_end:
            month_totals["calls"] += r.total_calls or 0
            month_totals["input"] += r.total_input or 0
            month_totals["output"] += r.total_output or 0
            month_totals["cache_read"] += r.total_cache_read or 0
            month_totals["cache_create"] += r.total_cache_create or 0
    month_totals["credits"] = tokens_to_credits(
        month_totals["input"], month_totals["output"],
        month_totals["cache_read"], month_totals["cache_create"], model)
    month_totals["range"] = f"{month_start} ~ {month_end}"

    return {
        "intraday": intraday,
        "daily": daily,
        "monthly": monthly,
        "today": today_totals,
        "this_month": month_totals,
        "model": model,
        "model_label": MODEL_PRICING.get(model, MODEL_PRICING["qwen3.6-flash"])["label"],
        "models_available": list(MODEL_PRICING.keys()),
    }



def _billing_cycle_for(target_date) -> tuple[str, str]:
    """Given any date, return its billing cycle (27th → next 27th).

    Returns (start_str, end_str) both YYYY-MM-DD.
    Start is the 27th of (this or previous) month, end is the 27th of next month.
    """
    if target_date.day >= 27:
        start = target_date.replace(day=27)
    else:
        # Prev month's 27th
        prev = target_date.replace(day=1) - timedelta(days=1)
        start = prev.replace(day=27)
    next_month = start.replace(day=1) + timedelta(days=32)
    end = next_month.replace(day=27)
    return (start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d"))


async def get_monthly_reports(max_months: int = 12) -> list[dict]:
    """Generate per-billing-cycle reports.

    Each cycle = 27th → next 27th. Returns list ordered most-recent first.
    """
    from . import db
    from .db import TokenUsageHourly
    from sqlalchemy import select

    async with db._SessionFactory() as session:
        # Determine date range we have data for
        first_row = await session.execute(
            select(TokenUsageHourly.hour).order_by(TokenUsageHourly.hour).limit(1)
        )
        first = first_row.scalar_one_or_none()
        if not first:
            return []
        first_day = datetime.strptime(first[:10], "%Y-%m-%d")

        rows = await session.execute(
            select(TokenUsageHourly).order_by(TokenUsageHourly.hour)
        )
        all_rows = list(rows.scalars())

    # Walk backward from today by billing cycles
    now = _local_now()
    reports = []
    cycle_date = now

    for _ in range(max_months):
        start, end = _billing_cycle_for(cycle_date)
        start_dt = datetime.strptime(start, "%Y-%m-%d")
        end_dt = datetime.strptime(end, "%Y-%m-%d")

        # Aggregate
        totals = {"calls": 0, "input": 0, "output": 0,
                  "cache_read": 0, "cache_create": 0}
        for r in all_rows:
            day = r.hour[:10]
            if start <= day < end:
                totals["calls"] += r.total_calls or 0
                totals["input"] += r.total_input or 0
                totals["output"] += r.total_output or 0
                totals["cache_read"] += r.total_cache_read or 0
                totals["cache_create"] += r.total_cache_create or 0

        # Cost per model
        cost_by_model = {}
        for model_key in MODEL_PRICING.keys():
            cost_by_model[model_key] = tokens_to_credits(
                totals["input"], totals["output"],
                totals["cache_read"], totals["cache_create"],
                model=model_key,
            )

        # Status (in-progress / complete / partial-data)
        today_str = now.strftime("%Y-%m-%d")
        if end > today_str:
            status = "in_progress"
        elif start_dt < first_day:
            status = "partial_data"
        else:
            status = "complete"

        # Days elapsed (for daily avg)
        if status == "in_progress":
            days_elapsed = (now.replace(tzinfo=None) - start_dt).days + 1
        else:
            days_elapsed = (end_dt - start_dt).days
        days_elapsed = max(days_elapsed, 1)

        # Recommendation
        # User's monthly limit (assume 25000 credits)
        LIMIT = 25000
        BUFFER = 0.8  # leave 20% buffer
        budget = LIMIT * BUFFER

        if status == "in_progress":
            # Project full month based on current daily average
            total_days = 30
            projected = {k: round(v / days_elapsed * total_days, 1)
                        for k, v in cost_by_model.items()}
        else:
            projected = cost_by_model

        # Build recommendation
        recommendation = _build_recommendation(projected, budget, LIMIT, status)

        report = {
            "cycle_start": start,
            "cycle_end": end,
            "status": status,
            "days_elapsed": days_elapsed,
            "totals": totals,
            "cost_by_model": cost_by_model,
            "projected_full_month": projected if status == "in_progress" else None,
            "recommendation": recommendation,
            "markdown": _render_monthly_markdown(
                start, end, status, days_elapsed, totals,
                cost_by_model, projected if status == "in_progress" else None,
                recommendation, LIMIT, budget,
            ),
        }
        reports.append(report)

        # Move back one cycle
        cycle_date = start_dt - timedelta(days=1)
        if start_dt < first_day - timedelta(days=30):
            break

    return reports


def _build_recommendation(projected: dict, budget: float, limit: float,
                         status: str) -> dict:
    """Decide which models are affordable for this usage level."""
    affordable = []
    over_budget = []
    for model, credits in sorted(projected.items(), key=lambda x: x[1]):
        ratio = credits / limit if limit else 0
        tier = MODEL_PRICING.get(model, {}).get("tier", "")
        if credits <= budget:
            affordable.append({"model": model, "tier": tier, "credits": credits, "ratio": ratio})
        else:
            over_budget.append({"model": model, "tier": tier, "credits": credits, "ratio": ratio})

    # Top recommendation = most expensive affordable
    if affordable:
        recommended = affordable[-1]
    else:
        recommended = None

    return {
        "affordable": affordable,
        "over_budget": over_budget,
        "recommended": recommended,
        "monthly_limit": limit,
        "safe_budget": budget,
    }


def _render_monthly_markdown(start, end, status, days, totals, cost_by_model,
                             projected, rec, limit, budget) -> str:
    """Pure markdown template fill — no LLM."""
    NL = "\n"
    status_label = {
        "in_progress": f"📊 进行中（第 {days} 天）",
        "complete": "✅ 已结束",
        "partial_data": "⚠️ 数据不完整（部分日期未采集）",
    }.get(status, status)

    lines = []
    lines.append(f"# 月度账单 · {start} → {end}")
    lines.append("")
    lines.append(f"**状态**：{status_label}")
    lines.append(f"**期内统计**：{totals['calls']:,} 次 LLM 调用 · {totals['input']:,} input tokens · {totals['output']:,} output tokens")
    lines.append("")
    lines.append("## 各模型成本对比")
    lines.append("")
    lines.append("| 模型 | 档次 | 期内 credits | 占限额 |")
    lines.append("|------|------|------------|--------|")
    for model_key, p in MODEL_PRICING.items():
        c = cost_by_model.get(model_key, 0)
        ratio = c / limit * 100 if limit else 0
        lines.append(f"| {p['label']} | {p['tier']} | {c:.1f} | {ratio:.1f}% |")

    if status == "in_progress" and projected:
        lines.append("")
        lines.append("## 全月预测（按当前节奏外推到 30 天）")
        lines.append("")
        lines.append("| 模型 | 预测 credits | 占限额 | 是否可负担 |")
        lines.append("|------|-------------|--------|-----------|")
        for model_key, p in MODEL_PRICING.items():
            c = projected.get(model_key, 0)
            ratio = c / limit * 100 if limit else 0
            ok = "✅ 安全" if c <= budget else ("⚠️ 接近上限" if c <= limit else "❌ 超限")
            lines.append(f"| {p['label']} | {c:.0f} | {ratio:.1f}% | {ok} |")

    lines.append("")
    lines.append("## 推荐")
    lines.append("")
    lines.append(f"月限额：**{int(limit)} credits** · 安全预算（留 20% 冗余）：**{int(budget)} credits**")
    lines.append("")

    if rec["recommended"]:
        r = rec["recommended"]
        lines.append(f"✅ **推荐使用**：`{r['model']}`（{r['tier']}档）")
        lines.append("")
        lines.append(f"- 期内成本仅 {r['credits']:.0f} credits（占限额 {r['ratio']*100:.1f}%）")
        lines.append("- 这是你能负担起的**最高档**模型")
        lines.append("")

    if rec["affordable"]:
        lines.append("### 可负担模型（按 credits 升序）")
        lines.append("")
        for a in rec["affordable"]:
            lines.append(f"- `{a['model']}` ({a['tier']}): {a['credits']:.0f} credits ({a['ratio']*100:.1f}%)")

    if rec["over_budget"]:
        lines.append("")
        lines.append("### 超出预算的模型")
        lines.append("")
        for o in rec["over_budget"]:
            lines.append(f"- `{o['model']}` ({o['tier']}): 需要 {o['credits']:.0f} credits ({o['ratio']*100:.1f}%) — 不推荐")

    return NL.join(lines)
