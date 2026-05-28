"""ETF dividend history fetcher with auto-frequency detection.

Strategy:
1. Primary: akshare fund_etf_dividend_sina (accumulated dividends, diff to get events)
2. Fallback: scrape eastmoney HTML if sina fails or is incomplete
3. Auto-detect frequency from inter-event days: monthly/quarterly/annual
4. Apply algorithm 1 (historical yield) according to frequency
5. Compute coefficient of variation (CV = stddev/mean) for stability indicator
"""

import logging
import re
import statistics
from datetime import date, datetime, timedelta, timezone
from typing import Optional

import httpx

log = logging.getLogger("bigv_twins.stock_data.etf_div")


def _is_etf(code: str) -> bool:
    """A股 ETF 代码规则：51xxxx(沪) / 15xxxx(深) / 56xxxx(跨市)."""
    return code[:2] in ("51", "15", "56") and len(code) == 6


def _fetch_etf_dividend_sina(code: str) -> list[dict]:
    """Fetch ETF dividend events via akshare sina API.

    Returns list of {date, amount_per_share} sorted oldest first.
    Each event = (this_cumulative - prev_cumulative).
    """
    try:
        import akshare as ak
        prefix = "sh" if code.startswith(("51", "56")) else "sz"
        df = ak.fund_etf_dividend_sina(symbol=f"{prefix}{code}")
        if df is None or df.empty:
            return []
    except Exception as e:
        log.warning("fund_etf_dividend_sina failed for %s: %s", code, e)
        return []

    # df has 日期 + 累计分红 (cumulative)
    df = df.sort_values("日期").reset_index(drop=True)
    out = []
    prev_cum = 0.0
    for _, row in df.iterrows():
        try:
            d = str(row["日期"])[:10]
            cum = float(row["累计分红"])
            amount = round(cum - prev_cum, 6)
            prev_cum = cum
            if amount > 0:
                out.append({"date": d, "amount_per_share": amount})
        except Exception:
            continue
    return out


def _fetch_etf_dividend_em_html(code: str) -> list[dict]:
    """Fallback: scrape Eastmoney FHSP HTML page for ETF dividend table.

    Returns list of {date, amount_per_share}, oldest first. Empty on failure.
    """
    url = f"https://fundf10.eastmoney.com/fhsp_{code}.html"
    try:
        r = httpx.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=8)
        if r.status_code != 200:
            return []
        html = r.text
    except Exception as e:
        log.warning("eastmoney FHSP fetch failed for %s: %s", code, e)
        return []

    # Find the dividend table by looking for headers including 权益登记日 / 每份分红
    tables = re.findall(r'<table[^>]*>(.*?)</table>', html, re.S)
    out = []
    for tbl in tables:
        headers = [re.sub(r'<[^>]+>', '', h).strip()
                   for h in re.findall(r'<th[^>]*>(.*?)</th>', tbl, re.S)]
        if "权益登记日" not in headers or "每份分红" not in headers:
            continue
        # Find column indices
        try:
            ex_idx = headers.index("除息日")
            div_idx = headers.index("每份分红")
        except ValueError:
            continue

        rows = re.findall(r'<tr[^>]*>(.*?)</tr>', tbl, re.S)
        for row in rows[1:]:  # skip header
            cells = [re.sub(r'<[^>]+>', '', c).strip()
                     for c in re.findall(r'<td[^>]*>(.*?)</td>', row, re.S)]
            if len(cells) <= max(ex_idx, div_idx):
                continue
            ex_date = cells[ex_idx]
            div_text = cells[div_idx]
            # Parse "每份派现金0.1430元" → 0.1430
            m = re.search(r'(\d+\.?\d*)\s*元', div_text)
            if not m:
                continue
            try:
                amount = float(m.group(1))
                out.append({"date": ex_date, "amount_per_share": amount})
            except ValueError:
                continue
        break  # only need first matching table

    out.sort(key=lambda x: x["date"])
    return out


def _detect_frequency(events: list[dict]) -> str:
    """Detect distribution frequency based on inter-event days.

    Returns 'monthly' / 'quarterly' / 'annual' / 'irregular'.
    """
    if len(events) < 2:
        return "annual"

    # Use the most recent 12 events (or all if fewer)
    recent = events[-12:] if len(events) >= 12 else events
    dates = []
    for e in recent:
        try:
            dates.append(datetime.strptime(e["date"], "%Y-%m-%d").date())
        except (ValueError, TypeError):
            continue
    if len(dates) < 2:
        return "annual"

    dates.sort()
    gaps = [(dates[i+1] - dates[i]).days for i in range(len(dates)-1)]
    avg_gap = sum(gaps) / len(gaps)

    if avg_gap <= 45:
        return "monthly"
    elif avg_gap <= 130:
        return "quarterly"
    elif avg_gap <= 400:
        return "annual"
    return "irregular"


def _coefficient_of_variation(values: list[float]) -> Optional[float]:
    """CV = stddev / mean. Returns None if mean is 0 or list has < 2 values.

    CV interpretation:
        < 0.15 → very stable
        0.15-0.30 → fairly stable
        0.30-0.50 → moderate volatility
        > 0.50 → high volatility
    """
    if len(values) < 2:
        return None
    mean = sum(values) / len(values)
    if mean <= 0:
        return None
    stdev = statistics.stdev(values)
    return round(stdev / mean, 3)


def _stability_label(cv: Optional[float]) -> str:
    if cv is None:
        return "数据不足"
    if cv < 0.15:
        return "非常稳定"
    if cv < 0.30:
        return "较为稳定"
    if cv < 0.50:
        return "中等波动"
    return "波动较大"


def get_etf_dividend_yield(query: str, info, current_price: Optional[float],
                          last_n: int = 24) -> dict:
    """Compute ETF dividend yield based on detected frequency.

    Algorithm 1 only — no forecast (algorithm 2). Frequency-aware:
    - monthly: sum past 12 months, CV stability indicator
    - quarterly: sum past 4 quarters + last full calendar year + prev year (if data)
    - annual: past 3 annual dividends + CV
    """
    code = info.code
    # Try sina first, fallback to eastmoney
    events = _fetch_etf_dividend_sina(code)
    if not events:
        events = _fetch_etf_dividend_em_html(code)

    # Best-effort: fetch dividend policy snippet from prospectus (招募说明书)
    distribution_policy = None
    try:
        from .etf_prospectus import fetch_etf_distribution_policy
        distribution_policy = fetch_etf_distribution_policy(code)
    except Exception:
        pass

    if not events:
        return {
            "ok": True, "query": query,
            "resolved": {"code": code, "name": info.name, "market": "a-share-etf"},
            "kind": "etf",
            "current_price": current_price,
            "history": [],
            "frequency": "unknown",
            "frequency_label": "未知（无历史分红）",
            "distribution_policy": distribution_policy,
            "algorithm_1_historical": {"note": "未查询到分红记录（可能从未分红或新上市）"},
            "source": "akshare/fund_etf_dividend_sina + eastmoney FHSP HTML",
            "note": "未查询到 ETF 分红记录",
        }

    frequency = _detect_frequency(events)
    today = date.today()

    # Helper: filter events by date range
    def in_range(start: date, end: date):
        out = []
        for e in events:
            try:
                d = datetime.strptime(e["date"], "%Y-%m-%d").date()
                if start <= d <= end:
                    out.append(e)
            except (ValueError, TypeError):
                continue
        return out

    algo1: dict = {"frequency": frequency}

    if frequency == "monthly":
        # Past 12 months
        start = today - timedelta(days=365)
        recent = in_range(start, today)
        amounts = [e["amount_per_share"] for e in recent]
        total = round(sum(amounts), 6)
        yield_pct = (total / current_price * 100) if current_price else None
        cv = _coefficient_of_variation(amounts)
        calc = (
            f"算法 1（月度分红 ETF 历史口径）：\n"
            f"  - 频率：月度分红\n"
            f"  - 过去 12 个月共 {len(amounts)} 次分红\n"
            f"  - 累计分红 = {total} 元/份\n"
            f"  - 当前价 = {current_price} 元\n"
            f"  - 股息率 = {total} / {current_price} = {yield_pct:.2f}%\n"
            f"  - 分红波动（CV）= {cv}（{_stability_label(cv)}）"
            if current_price else f"算法 1：缺当前价"
        )
        algo1.update({
            "period": "past_12_months",
            "events_count": len(amounts),
            "total_dividend_per_share": total,
            "current_price": current_price,
            "yield_pct": round(yield_pct, 2) if yield_pct is not None else None,
            "cv": cv,
            "stability": _stability_label(cv),
            "events": recent,
            "calculation": calc,
        })

    elif frequency == "quarterly":
        # Past 4 quarters (last 365 days)
        start = today - timedelta(days=365)
        recent = in_range(start, today)
        rec_amounts = [e["amount_per_share"] for e in recent]
        rec_total = round(sum(rec_amounts), 6)
        rec_yield = (rec_total / current_price * 100) if current_price else None

        # Last full calendar year
        last_year = today.year - 1
        last_year_events = in_range(date(last_year, 1, 1), date(last_year, 12, 31))
        ly_amounts = [e["amount_per_share"] for e in last_year_events]
        ly_total = round(sum(ly_amounts), 6)
        ly_yield = (ly_total / current_price * 100) if current_price else None

        # Year before that
        prev_year = today.year - 2
        py_events = in_range(date(prev_year, 1, 1), date(prev_year, 12, 31))
        py_amounts = [e["amount_per_share"] for e in py_events]
        py_total = round(sum(py_amounts), 6)
        py_yield = (py_total / current_price * 100) if current_price else None

        # Stability across all known quarters (recent)
        all_recent = (events[-8:] if len(events) >= 8 else events)
        all_amounts = [e["amount_per_share"] for e in all_recent]
        cv = _coefficient_of_variation(all_amounts)

        calc = (
            f"算法 1（季度分红 ETF 历史口径，多窗口对照）：\n"
            f"  - 频率：季度分红\n"
            f"  - 过去 4 个季度（rolling 12 个月）：{rec_total} 元/份 → "
            f"股息率 {rec_yield:.2f}%（{len(rec_amounts)} 次分红）\n"
            f"  - {last_year} 完整年度：{ly_total} 元/份 → 股息率 "
            f"{ly_yield:.2f}%（{len(ly_amounts)} 次分红）\n"
            f"  - {prev_year} 完整年度：{py_total} 元/份 → 股息率 "
            f"{py_yield:.2f}%（{len(py_amounts)} 次分红）\n"
            f"  - 当前价 = {current_price} 元\n"
            f"  - 历次季度分红波动（CV）= {cv}（{_stability_label(cv)}）"
            if current_price else "算法 1：缺当前价"
        )
        algo1.update({
            "rolling_12_months": {
                "total": rec_total, "yield_pct": round(rec_yield, 2) if rec_yield else None,
                "events_count": len(rec_amounts),
            },
            "last_full_year": {
                "year": last_year, "total": ly_total,
                "yield_pct": round(ly_yield, 2) if ly_yield else None,
                "events_count": len(ly_amounts),
            },
            "prev_year": {
                "year": prev_year, "total": py_total,
                "yield_pct": round(py_yield, 2) if py_yield else None,
                "events_count": len(py_amounts),
            },
            "cv": cv,
            "stability": _stability_label(cv),
            "current_price": current_price,
            "calculation": calc,
        })

    else:  # annual or irregular
        # Past 3 annual dividends
        # Group by year, sum within year
        from collections import defaultdict
        by_year = defaultdict(float)
        by_year_count = defaultdict(int)
        for e in events:
            try:
                y = int(e["date"][:4])
                by_year[y] += e["amount_per_share"]
                by_year_count[y] += 1
            except (ValueError, TypeError):
                continue
        recent_years = sorted(by_year.keys(), reverse=True)[:3]
        annual_amounts = [by_year[y] for y in recent_years]
        per_year_yields = []
        for y, amt in zip(recent_years, annual_amounts):
            per_year_yields.append({
                "year": y,
                "total": round(amt, 6),
                "events_count": by_year_count[y],
                "yield_pct": round(amt / current_price * 100, 2) if current_price else None,
            })
        cv = _coefficient_of_variation(annual_amounts)

        # Latest annual yield (for headline)
        latest_yield = per_year_yields[0]["yield_pct"] if per_year_yields else None
        latest_total = per_year_yields[0]["total"] if per_year_yields else 0

        calc_lines = [f"算法 1（年度分红 ETF 历史口径）：", f"  - 频率：{frequency}（按自然年聚合）"]
        for py in per_year_yields:
            calc_lines.append(
                f"  - {py['year']} 年：{py['total']} 元/份 ({py['events_count']} 次) "
                f"→ 股息率 {py['yield_pct']}%"
                if py['yield_pct'] is not None else
                f"  - {py['year']} 年：{py['total']} 元/份"
            )
        calc_lines.append(f"  - 当前价 = {current_price} 元")
        calc_lines.append(f"  - 年度分红波动（CV）= {cv}（{_stability_label(cv)}）")
        calc = "\n".join(calc_lines)

        algo1.update({
            "period": "past_3_years",
            "annual_history": per_year_yields,
            "latest_yield_pct": latest_yield,
            "latest_total": latest_total,
            "cv": cv,
            "stability": _stability_label(cv),
            "current_price": current_price,
            "calculation": calc,
        })

    freq_label_map = {"monthly": "月度分红", "quarterly": "季度分红",
                       "annual": "年度分红", "irregular": "不规律分红"}

    return {
        "ok": True, "query": query,
        "resolved": {"code": code, "name": info.name, "market": "a-share-etf"},
        "kind": "etf",
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "current_price": current_price,
        "frequency": frequency,
        "frequency_label": freq_label_map.get(frequency, frequency),
        "distribution_policy": distribution_policy,
        "history": events[-last_n:],
        "algorithm_1_historical": algo1,
        "algorithm_2_forecast": {
            "note": "ETF 不适用预测算法 — 分红依赖底层指数/成分股，不外推"
        },
        "source": "akshare/fund_etf_dividend_sina + eastmoney FHSP HTML + 招募说明书 PDF",
    }
