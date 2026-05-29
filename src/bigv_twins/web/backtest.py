"""博主预测回测系统。

核心问题：博主在某天提到某只股，30 天后该股 vs 沪深300 跑赢了吗？
聚合到博主层就是「胜率 X%、平均超额收益 Y%」。

数据流：
  blogger_daily_brief.mentioned_tickers  (JSON list of ticker codes)
    → 对每只 ticker 算 30 天前/后股价 vs 沪深300
    → 写入 backtest_entries 表 (UPSERT)

价格源：akshare.stock_zh_a_hist(symbol, period='daily', adjust='')  # 用原始市场价
  之前用过 'hfq'（后复权），导致 /stock 页面显示的 entry/exit 价比真实
  市场价高几倍（hfq 会把历史价格按累计分红 forward-adjust）。14 天窗口
  内分红极少见，用原始价对收益率几乎没影响，对用户更直观。
基准：akshare.index_zh_a_hist(symbol='000300') 沪深300

调度：每天 03:50 跑（blogger_brief_daily 03:30 之后）
  - 新 brief 的 entry：算
  - 旧 brief 但 status='pending'：检查窗口到期没，到期就算 exit
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from bigv_twins.config import BY_SLUG, BLOGGERS

from . import auth, db
from .db import BacktestEntry, BloggerDailyBrief, User

log = logging.getLogger("bigv_twins.web.backtest")
router = APIRouter(prefix="/report")
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


DEFAULT_WINDOW = 14          # 天（30 天太长，数据回填只有 30 天深度，所有 30 天窗口都还没到期）
BENCHMARK_INDEX = "000300"   # 沪深300


# ============================================================================
# Price fetching helpers (sync, wrap in to_thread)
# ============================================================================


def _is_a_share(ticker: str) -> bool:
    return len(ticker) == 6 and ticker.isdigit()


# Tiny in-process cache for one compute_all run; cleared between calls
_price_cache: dict[tuple[str, str, str], object] = {}


def _fetch_price_hist(ticker: str, start_yyyymmdd: str, end_yyyymmdd: str):
    """Fetch daily OHLCV via akshare（原始市场价）。返回 DataFrame 或 None。

    EastMoney 后端容易在密集请求时回 RemoteDisconnected；带 3 次指数退避重试。
    """
    key = (ticker, start_yyyymmdd, end_yyyymmdd)
    if key in _price_cache:
        return _price_cache[key]
    import akshare as ak
    df = None
    for attempt in range(3):
        try:
            df = ak.stock_zh_a_hist(symbol=ticker, period="daily",
                                    start_date=start_yyyymmdd, end_date=end_yyyymmdd,
                                    adjust="")
            break
        except Exception as e:
            if attempt == 2:
                log.warning("stock_zh_a_hist(%s) failed after 3 tries: %s", ticker, e)
                _price_cache[key] = None
                return None
            time.sleep(0.3 * (2 ** attempt))  # 0.3s, 0.6s
    if df is None or len(df) == 0:
        _price_cache[key] = None
        return None
    _price_cache[key] = df
    return df


def _fetch_benchmark_hist(start_yyyymmdd: str, end_yyyymmdd: str):
    """Fetch 沪深300 history via Sina API. Returns DataFrame with normalized columns
    ('日期' as str, '收盘' as float), matching stock_zh_a_hist's column names.

    Sina's stock_zh_index_daily returns ALL history with English cols + date objects;
    we filter to the requested range and rename columns so the downstream
    _get_close_on_or_after code can work uniformly.
    """
    key = (BENCHMARK_INDEX, start_yyyymmdd, end_yyyymmdd)
    if key in _price_cache:
        return _price_cache[key]
    try:
        import akshare as ak
        df = ak.stock_zh_index_daily(symbol=f"sh{BENCHMARK_INDEX}")
    except Exception as e:
        log.warning("stock_zh_index_daily(sh%s) failed: %s", BENCHMARK_INDEX, e)
        _price_cache[key] = None
        return None
    if df is None or len(df) == 0:
        _price_cache[key] = None
        return None
    # Normalize: rename to match stock_zh_a_hist + stringify date
    df = df.rename(columns={"date": "日期", "close": "收盘"})
    df["日期"] = df["日期"].astype(str)
    # Filter to requested range
    start_dash = f"{start_yyyymmdd[:4]}-{start_yyyymmdd[4:6]}-{start_yyyymmdd[6:]}"
    end_dash = f"{end_yyyymmdd[:4]}-{end_yyyymmdd[4:6]}-{end_yyyymmdd[6:]}"
    df = df[(df["日期"] >= start_dash) & (df["日期"] <= end_dash)].reset_index(drop=True)
    _price_cache[key] = df
    return df


def _get_close_on_or_after(df, target_date: str) -> tuple[str, float] | None:
    """返回 df 中 ≥ target_date 的第一个交易日的 (日期str, 收盘价) 或 None。

    target_date: YYYY-MM-DD; df 的 '日期' 列也是 YYYY-MM-DD 格式 (akshare 默认)
    """
    if df is None:
        return None
    try:
        col_date = "日期"
        col_close = "收盘"
        mask = df[col_date].astype(str) >= target_date
        sub = df[mask]
        if len(sub) == 0:
            return None
        row = sub.iloc[0]
        return (str(row[col_date]), float(row[col_close]))
    except Exception as e:
        log.warning("_get_close_on_or_after error: %s", e)
        return None


# ============================================================================
# Compute one entry
# ============================================================================


async def compute_one(blogger_slug: str, brief_date: str, ticker: str,
                       window_days: int = DEFAULT_WINDOW,
                       benchmark_df=None) -> dict:
    """Compute (or refresh) one backtest entry. UPSERTs into backtest_entries.

    Returns the computed result dict for telemetry.
    """
    if not _is_a_share(ticker):
        # 港股 / 非 A 股先跳过（akshare 接口不同）
        await _upsert(blogger_slug, brief_date, ticker, window_days,
                      {"status": "no_data"})
        return {"status": "skip_non_a"}

    try:
        entry_dt = datetime.strptime(brief_date, "%Y-%m-%d").date()
    except ValueError:
        await _upsert(blogger_slug, brief_date, ticker, window_days,
                      {"status": "no_data"})
        return {"status": "bad_date"}

    exit_target = entry_dt + timedelta(days=window_days)
    today = date.today()

    # Fetch price history: entry_date - 7 days to exit_target + 14 days (handle weekends)
    start = (entry_dt - timedelta(days=7)).strftime("%Y%m%d")
    end = (exit_target + timedelta(days=14)).strftime("%Y%m%d")
    if exit_target > today:
        end = today.strftime("%Y%m%d")  # 窗口未到期，只能拉到今天

    df = await asyncio.to_thread(_fetch_price_hist, ticker, start, end)
    if df is None:
        await _upsert(blogger_slug, brief_date, ticker, window_days,
                      {"status": "no_data"})
        return {"status": "no_price_data"}

    entry = _get_close_on_or_after(df, brief_date)
    if entry is None:
        await _upsert(blogger_slug, brief_date, ticker, window_days,
                      {"status": "no_data"})
        return {"status": "no_entry_price"}

    entry_actual_date, entry_px = entry

    # Benchmark
    if benchmark_df is None:
        benchmark_df = await asyncio.to_thread(_fetch_benchmark_hist, start, end)

    b_entry = _get_close_on_or_after(benchmark_df, brief_date) if benchmark_df is not None else None
    if b_entry is None:
        await _upsert(blogger_slug, brief_date, ticker, window_days,
                      {"status": "no_data",
                       "entry_date_actual": entry_actual_date,
                       "entry_price": entry_px})
        return {"status": "no_benchmark"}

    # Check if exit window has elapsed
    if exit_target > today:
        # pending
        await _upsert(blogger_slug, brief_date, ticker, window_days, {
            "status": "pending",
            "entry_date_actual": entry_actual_date,
            "entry_price": entry_px,
            "benchmark_entry": b_entry[1],
        })
        return {"status": "pending", "entry_price": entry_px}

    # Exit
    exit_target_str = exit_target.strftime("%Y-%m-%d")
    exit = _get_close_on_or_after(df, exit_target_str)
    b_exit = _get_close_on_or_after(benchmark_df, exit_target_str)
    if exit is None or b_exit is None:
        await _upsert(blogger_slug, brief_date, ticker, window_days, {
            "status": "pending",
            "entry_date_actual": entry_actual_date,
            "entry_price": entry_px,
            "benchmark_entry": b_entry[1],
        })
        return {"status": "no_exit_data"}

    exit_actual_date, exit_px = exit
    b_exit_actual_date, b_exit_px = b_exit
    ticker_return = (exit_px / entry_px - 1.0) * 100.0
    benchmark_return = (b_exit_px / b_entry[1] - 1.0) * 100.0
    excess = ticker_return - benchmark_return
    hit = bool(excess > 0)

    await _upsert(blogger_slug, brief_date, ticker, window_days, {
        "status": "complete",
        "entry_date_actual": entry_actual_date,
        "exit_date_actual": exit_actual_date,
        "entry_price": entry_px,
        "exit_price": exit_px,
        "benchmark_entry": b_entry[1],
        "benchmark_exit": b_exit_px,
        "ticker_return": ticker_return,
        "benchmark_return": benchmark_return,
        "excess_return": excess,
        "hit": hit,
    })
    return {"status": "complete", "excess": excess, "hit": hit}


async def _upsert(blogger_slug: str, brief_date: str, ticker: str,
                  window_days: int, fields: dict) -> None:
    async with db._SessionFactory() as s:
        existing = await s.execute(
            select(BacktestEntry)
            .where(BacktestEntry.blogger_slug == blogger_slug)
            .where(BacktestEntry.brief_date == brief_date)
            .where(BacktestEntry.ticker == ticker)
            .where(BacktestEntry.window_days == window_days)
        )
        row = existing.scalar_one_or_none()
        if row is None:
            row = BacktestEntry(
                blogger_slug=blogger_slug,
                brief_date=brief_date,
                ticker=ticker,
                window_days=window_days,
                **fields,
            )
            s.add(row)
        else:
            for k, v in fields.items():
                setattr(row, k, v)
            row.computed_at = datetime.utcnow()
        await s.commit()


# ============================================================================
# Batch: compute all entries
# ============================================================================


async def compute_all_entries(window_days: int = DEFAULT_WINDOW) -> dict:
    """走全部 blogger_daily_brief，给每个 (blogger, date, mentioned_ticker) 算回测。

    既计算新的（没 entry 的），也刷新 status='pending' 的（窗口可能已到期）。
    """
    global _price_cache
    _price_cache = {}  # reset per run

    t0 = time.time()
    n_new = n_pending = n_skip = n_err = 0

    # Pre-fetch benchmark for the whole range (1 HTTP call)
    today = date.today()
    earliest = today - timedelta(days=180)  # 半年前足够 cover 历史
    bench_df = await asyncio.to_thread(_fetch_benchmark_hist,
                                       earliest.strftime("%Y%m%d"),
                                       (today + timedelta(days=1)).strftime("%Y%m%d"))
    log.info("backtest: benchmark df has %s rows",
             len(bench_df) if bench_df is not None else "n/a")

    # Iterate all blogger briefs
    async with db._SessionFactory() as s:
        rows = await s.execute(select(BloggerDailyBrief).order_by(BloggerDailyBrief.brief_date))
        briefs = list(rows.scalars())
    log.info("backtest: processing %d briefs", len(briefs))

    for br in briefs:
        try:
            tickers = json.loads(br.mentioned_tickers or "[]")
        except json.JSONDecodeError:
            tickers = []
        for t in tickers:
            t = str(t).strip()
            if not t:
                continue
            # check if already complete (skip recompute)
            async with db._SessionFactory() as s:
                existing = await s.execute(
                    select(BacktestEntry.status)
                    .where(BacktestEntry.blogger_slug == br.blogger_slug)
                    .where(BacktestEntry.brief_date == br.brief_date)
                    .where(BacktestEntry.ticker == t)
                    .where(BacktestEntry.window_days == window_days)
                )
                existing_status = existing.scalar_one_or_none()
            if existing_status == "complete":
                n_skip += 1
                continue

            try:
                r = await compute_one(br.blogger_slug, br.brief_date, t,
                                       window_days=window_days,
                                       benchmark_df=bench_df)
            except Exception as e:
                log.exception("backtest %s/%s/%s failed: %s",
                              br.blogger_slug, br.brief_date, t, e)
                n_err += 1
                continue
            if r["status"] == "complete":
                n_new += 1
            elif r["status"] == "pending":
                n_pending += 1
            else:
                n_err += 1

    _price_cache = {}  # release memory

    log.info("backtest done in %.1fs: new=%d pending=%d skip=%d err=%d",
             time.time() - t0, n_new, n_pending, n_skip, n_err)
    return {"new": n_new, "pending": n_pending, "skipped_complete": n_skip,
            "errors": n_err, "elapsed_s": time.time() - t0}


# ============================================================================
# Queries for UI
# ============================================================================


async def leaderboard() -> list[dict]:
    """聚合：每位博主 (hit_count, total_count, hit_rate, avg_excess)，按 hit_rate 降序。"""
    async with db._SessionFactory() as s:
        # 1. count per slug
        rows = await s.execute(text("""
            SELECT blogger_slug,
                   SUM(CASE WHEN hit = 1 THEN 1 ELSE 0 END) AS hits,
                   SUM(CASE WHEN status = 'complete' THEN 1 ELSE 0 END) AS completes,
                   SUM(CASE WHEN status = 'pending' THEN 1 ELSE 0 END) AS pending,
                   AVG(CASE WHEN status = 'complete' THEN excess_return ELSE NULL END) AS avg_excess,
                   AVG(CASE WHEN status = 'complete' THEN ticker_return ELSE NULL END) AS avg_ticker_return,
                   AVG(CASE WHEN status = 'complete' THEN benchmark_return ELSE NULL END) AS avg_bench_return
            FROM backtest_entries
            GROUP BY blogger_slug
        """))
        results = []
        for r in rows.fetchall():
            slug, hits, completes, pending, avg_excess, avg_t, avg_b = r
            blogger = BY_SLUG.get(slug)
            results.append({
                "slug": slug,
                "name": blogger.name if blogger else slug,
                "hits": int(hits or 0),
                "completes": int(completes or 0),
                "pending": int(pending or 0),
                "hit_rate": (hits / completes * 100.0) if completes else 0.0,
                "avg_excess": float(avg_excess) if avg_excess is not None else 0.0,
                "avg_ticker_return": float(avg_t) if avg_t is not None else 0.0,
                "avg_bench_return": float(avg_b) if avg_b is not None else 0.0,
            })
    # sort by hit_rate desc (must have ≥ 3 completes to rank, otherwise sink)
    results.sort(key=lambda r: (r["completes"] >= 3, r["hit_rate"], r["avg_excess"]),
                 reverse=True)
    return results


async def blogger_records(slug: str, limit: int = 50) -> list[BacktestEntry]:
    async with db._SessionFactory() as s:
        rows = await s.execute(
            select(BacktestEntry)
            .where(BacktestEntry.blogger_slug == slug)
            .order_by(BacktestEntry.brief_date.desc(), BacktestEntry.ticker)
            .limit(limit)
        )
        return list(rows.scalars())


async def blogger_summary(slug: str) -> dict:
    async with db._SessionFactory() as s:
        rows = await s.execute(text("""
            SELECT
                SUM(CASE WHEN hit = 1 THEN 1 ELSE 0 END) AS hits,
                SUM(CASE WHEN status = 'complete' THEN 1 ELSE 0 END) AS completes,
                SUM(CASE WHEN status = 'pending' THEN 1 ELSE 0 END) AS pending,
                AVG(CASE WHEN status = 'complete' THEN excess_return ELSE NULL END) AS avg_excess,
                MAX(CASE WHEN status = 'complete' THEN excess_return ELSE NULL END) AS best_excess,
                MIN(CASE WHEN status = 'complete' THEN excess_return ELSE NULL END) AS worst_excess
            FROM backtest_entries
            WHERE blogger_slug = :s
        """), {"s": slug})
        r = rows.fetchone()
        if r is None:
            return {"hits": 0, "completes": 0, "pending": 0,
                    "hit_rate": 0, "avg_excess": 0, "best_excess": 0, "worst_excess": 0}
        hits, completes, pending, avg_excess, best, worst = r
        return {
            "hits": int(hits or 0),
            "completes": int(completes or 0),
            "pending": int(pending or 0),
            "hit_rate": (hits / completes * 100.0) if completes else 0.0,
            "avg_excess": float(avg_excess) if avg_excess is not None else 0.0,
            "best_excess": float(best) if best is not None else 0.0,
            "worst_excess": float(worst) if worst is not None else 0.0,
        }


# ============================================================================
# HTTP routes
# ============================================================================


@router.get("/leaderboard", response_class=HTMLResponse)
async def leaderboard_page(
    request: Request,
    user: Annotated[User, Depends(auth.require_user)],
):
    rows = await leaderboard()
    return templates.TemplateResponse(
        request=request,
        name="report/leaderboard.html",
        context={"user": user, "rows": rows, "window": DEFAULT_WINDOW},
    )


@router.post("/backtest/rebuild")
async def backtest_rebuild(
    user: Annotated[User, Depends(auth.require_user)],
):
    """Manually trigger full backtest pass."""
    asyncio.create_task(compute_all_entries())
    return RedirectResponse("/report/leaderboard", status_code=303)


# A separate router for the per-blogger page (under /about, not /report)
about_track_router = APIRouter(prefix="/about")


@about_track_router.get("/{slug}/track-record", response_class=HTMLResponse)
async def track_record_page(
    request: Request,
    slug: str,
    user: Annotated[User, Depends(auth.require_user)],
):
    blogger = BY_SLUG.get(slug)
    if blogger is None:
        raise HTTPException(status_code=404)
    summary = await blogger_summary(slug)
    records = await blogger_records(slug, limit=200)
    return templates.TemplateResponse(
        request=request,
        name="report/track_record.html",
        context={
            "user": user, "blogger": blogger, "summary": summary,
            "records": records, "window": DEFAULT_WINDOW,
        },
    )
