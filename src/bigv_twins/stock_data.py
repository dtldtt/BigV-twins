"""Stock fundamentals + index context fetcher for the agent.

Composite from multiple sources (most reliable first; akshare per-source fallbacks):

- Tencent http://qt.gtimg.cn/        — current price, PE, PB, market cap, 52w hi/lo  (most reliable)
- akshare 同花顺 stock_zyjs_ths       — 主营业务
- akshare 雪球 stock_individual_basic_info_xq — 实际控制人 + 控股分类 + 行业 + 公司全名
- akshare 新浪 stock_zh_a_daily       — 1y price change
- akshare 新浪 stock_zh_index_daily   — 上证 / 创业板 / 科创 50 最近 10 天

For HK, only Tencent quote is wired in v1. US is best-effort name passthrough.
A 10-minute in-process cache per ticker keeps repeated questions fast.
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

import httpx
from pathlib import Path

log = logging.getLogger("bigv_twins.stock_data")

# ----- caches ---------------------------------------------------------

_NAME_MAP_CACHE: dict[str, Any] = {"ts": 0.0, "df": None}
_SNAPSHOT_CACHE: dict[str, tuple[float, dict]] = {}
SNAPSHOT_TTL_S = 600    # 10 min
NAME_MAP_TTL_S = 3600   # 1 hour
NAME_MAP_DISK_TTL_S = 86400 * 3  # 3 天内的磁盘缓存直接信任
_NAME_MAP_DISK_PATH = Path("/tmp/bigv_a_share_names.csv")


# ----- ticker resolution ---------------------------------------------

@dataclass(frozen=True)
class TickerInfo:
    code: str       # "600519"
    name: str       # "贵州茅台"
    prefix: str     # "sh" / "sz" / "bj" / "hk" / "us"
    market: str     # "a-share" / "hk" / "us"
    board: str      # "main" / "gem" / "star" / "bse" / "hk" / "us"

    @property
    def tencent_symbol(self) -> str:
        return f"{self.prefix}{self.code}"

    @property
    def xq_symbol(self) -> str:
        return f"{self.prefix.upper()}{self.code}"


def _a_share_prefix(code: str) -> str:
    if code.startswith(("60", "68")):
        return "sh"
    if code.startswith(("00", "30", "20", "15", "16", "18")):
        return "sz"
    if code.startswith(("8", "4", "9", "92")) and len(code) == 6:
        return "bj"
    return "sh"


def _a_share_board(code: str) -> str:
    if code.startswith("688"):
        return "star"
    if code.startswith(("300", "301")):
        return "gem"
    if code.startswith("8") and len(code) == 6:
        return "bse"
    return "main"


def _is_etf(code: str) -> bool:
    """A股 ETF 代码规则：51xxxx(沪) / 15xxxx(深) / 56xxxx(跨市)。"""
    return code[:2] in ("51", "15", "56") and len(code) == 6


def _load_name_map():
    """Cache code→name for A-share lookups.

    三层缓存：内存 1h → 磁盘 3 天 → 网络回源。
    冷启动时（内存空但磁盘有），读磁盘几乎瞬时（< 50ms），避免每次 systemd
    重启用户首次访问 /report 都要等 ~8s 拉 akshare 全市场名单。
    """
    now = time.time()
    if _NAME_MAP_CACHE["df"] is not None and now - _NAME_MAP_CACHE["ts"] < NAME_MAP_TTL_S:
        return _NAME_MAP_CACHE["df"]

    # 磁盘缓存：3 天内的直接信任（A 股新增/改名频率极低）
    if _NAME_MAP_DISK_PATH.exists():
        try:
            mtime = _NAME_MAP_DISK_PATH.stat().st_mtime
            if now - mtime < NAME_MAP_DISK_TTL_S:
                import pandas as pd
                df = pd.read_csv(_NAME_MAP_DISK_PATH, dtype={"code": str})
                _NAME_MAP_CACHE["df"] = df
                _NAME_MAP_CACHE["ts"] = mtime
                return df
        except Exception as e:
            log.warning("name map disk read failed (will refetch): %s", e)

    # 网络回源
    import akshare as ak
    for attempt in range(3):
        try:
            df = ak.stock_info_a_code_name()
            _NAME_MAP_CACHE["df"] = df
            _NAME_MAP_CACHE["ts"] = now
            try:
                df.to_csv(_NAME_MAP_DISK_PATH, index=False)
            except Exception as e:
                log.warning("name map disk write failed: %s", e)
            return df
        except Exception as e:
            log.warning("name map fetch attempt %d failed: %s", attempt + 1, e)
            time.sleep(1)
    return None


def resolve_ticker(query: str) -> Optional[TickerInfo]:
    query = (query or "").strip()
    if not query:
        return None

    # pure 6-digit A-share code (includes ETF)
    if re.fullmatch(r"\d{6}", query):
        df = _load_name_map()
        name = query
        if df is not None:
            row = df[df["code"] == query]
            if not row.empty:
                name = row.iloc[0]["name"]
        board = "etf" if _is_etf(query) else _a_share_board(query)
        return TickerInfo(
            code=query, name=name,
            prefix=_a_share_prefix(query),
            market="a-share", board=board,
        )

    # HK code: 4-5 digits, optionally suffixed .HK
    m = re.fullmatch(r"(\d{4,5})(\.HK)?", query.upper())
    if m:
        code = m.group(1).zfill(5)
        return TickerInfo(code=code, name=code, prefix="hk", market="hk", board="hk")

    # US ticker: 1-5 letters (no digits)
    if re.fullmatch(r"[A-Z]{1,5}", query.upper()):
        return TickerInfo(
            code=query.upper(), name=query.upper(),
            prefix="us", market="us", board="us",
        )

    # Chinese name → A-share lookup
    df = _load_name_map()
    if df is not None:
        exact = df[df["name"] == query]
        if not exact.empty:
            r = exact.iloc[0]
        else:
            partial = df[df["name"].str.contains(query, na=False, regex=False)]
            if partial.empty:
                return None
            r = partial.iloc[0]
        return TickerInfo(
            code=r["code"], name=r["name"],
            prefix=_a_share_prefix(r["code"]),
            market="a-share", board=_a_share_board(r["code"]),
        )

    return None


# ----- Tencent (primary for spot/valuation) --------------------------

def _tencent_fetch(symbol: str) -> dict:
    """`symbol` like 'sh600519' / 'sz000001' / 'hk00700' / 'sh000001'.

    Tencent's gtimg endpoint returns a ~50-field tilde-separated string.
    Field positions are stable.
    """
    url = f"http://qt.gtimg.cn/q={symbol}"
    try:
        r = httpx.get(url, timeout=5)
    except Exception as e:
        log.warning("tencent fetch failed for %s: %s", symbol, e)
        return {}
    text = r.content.decode("gbk", errors="replace")
    if '"' not in text:
        return {}
    inner = text.split('"', 2)[1]
    fields = inner.split("~")

    def f(i, cast=str, default=None):
        try:
            v = fields[i]
            if v in ("", "-"):
                return default
            if cast is float:
                return float(v)
            if cast is int:
                return int(float(v))
            return v
        except (ValueError, IndexError):
            return default

    return {
        "name": f(1),
        "code": f(2),
        "current": f(3, float),
        "yesterday_close": f(4, float),
        "change": f(31, float),
        "change_pct": f(32, float),
        "high_today": f(33, float),
        "low_today": f(34, float),
        "turnover_rate": f(38, float),
        "pe_ttm": f(39, float),
        # NOTE: fields 41/42 are NOT 52-week hi/lo — they duplicate today's
        # high/low (or a recent-window range). Tencent qt.gtimg has no real
        # 52w field exposed here. We compute the actual 52w hi/lo from akshare
        # daily history in `_one_year_stats()`.
        "amplitude": f(43, float),
        "circulating_mc_yi": f(44, float),
        "total_mc_yi": f(45, float),
        "pb": f(46, float),
    }


# ----- akshare per-source helpers (each independently fallable) ------

def _ths_main_business(code: str) -> dict:
    try:
        import akshare as ak
        df = ak.stock_zyjs_ths(symbol=code)
        if df.empty:
            return {}
        return df.iloc[0].to_dict()
    except Exception as e:
        log.warning("ths zyjs failed for %s: %s", code, e)
        return {}


def _xq_basic_info(xq_symbol: str) -> dict:
    """Two retries for the 雪球 endpoint."""
    for attempt in range(2):
        try:
            import akshare as ak
            df = ak.stock_individual_basic_info_xq(symbol=xq_symbol)
            return dict(zip(df["item"], df["value"]))
        except Exception as e:
            log.warning("xq basic_info attempt %d for %s failed: %s",
                        attempt + 1, xq_symbol, e)
            time.sleep(1)
    return {}


def _one_year_stats(symbol: str) -> dict:
    """Pull last ~252 trading days from akshare and derive:
       - 1y price change pct (current vs ~252 days ago, qfq-adjusted)
       - 52w high / low (max/min of high/low columns over the window)

    Returns {} on failure. Single network call covers both, so we do it once.
    """
    try:
        import akshare as ak
        df = ak.stock_zh_a_daily(symbol=symbol, adjust="qfq")
        if df is None or len(df) < 30:
            return {}
        # 52w window: last 252 trading days (or whatever we have if <252)
        win = df.tail(252)
        out: dict = {}
        try:
            out["high_52w"] = round(float(win["high"].max()), 2)
            out["low_52w"] = round(float(win["low"].min()), 2)
        except Exception:
            pass
        if len(df) >= 252:
            try:
                current = float(df.iloc[-1]["close"])
                year_ago = float(df.iloc[-252]["close"])
                out["change_1y_pct"] = round((current / year_ago - 1) * 100, 1)
            except Exception:
                pass
        return out
    except Exception as e:
        log.warning("1y stats failed for %s: %s", symbol, e)
        return {}


def _one_year_change_pct(symbol: str) -> Optional[float]:
    """Back-compat shim — prefer _one_year_stats() which returns 52w hi/lo too."""
    return _one_year_stats(symbol).get("change_1y_pct")


def _dividend_history_a_share(code: str) -> list[dict]:
    """Pull A-share dividend history via akshare → 新浪 stock_history_dividend_detail.

    Returns list of dividend events sorted **most-recent first** with normalized fields:
      announce_date / amount_per_10 (元) / amount_per_share / ex_date /
      record_date / status (实施/预案/...) / has_split

    Empty list on any failure or no history. Stock-split entries (送股/转增 > 0)
    are kept but flagged so callers can treat them differently.
    """
    try:
        import akshare as ak
        df = ak.stock_history_dividend_detail(symbol=code, indicator="分红")
    except Exception as e:
        log.warning("stock_history_dividend_detail failed for %s: %s", code, e)
        return []
    if df is None or df.empty:
        return []

    # Columns per akshare doc: 公告日期 / 送股 / 转增 / 派息 / 进度 /
    #                         除权除息日 / 股权登记日 / 红股上市日
    def _date_str(v) -> str | None:
        """Coerce pandas Timestamp / datetime / str / NaT / NaN → 'YYYY-MM-DD' or None.

        ``hasattr(v, 'strftime')`` is true for ``pd.NaT`` but calling it raises;
        screen with ``str(v) == 'NaT'`` first.
        """
        if v is None:
            return None
        s = str(v)
        if s in ("NaT", "nan", "None", ""):
            return None
        if hasattr(v, "strftime"):
            try:
                return v.strftime("%Y-%m-%d")
            except (ValueError, TypeError):
                return None
        return s[:10]

    out: list[dict] = []
    for _, row in df.iterrows():
        try:
            announce = _date_str(row.get("公告日期"))
            ex = _date_str(row.get("除权除息日"))
            rec = _date_str(row.get("股权登记日"))
            amount_per_10 = float(row.get("派息") or 0)
            songgu = float(row.get("送股") or 0)
            zhuanzeng = float(row.get("转增") or 0)
            out.append({
                "announce_date": announce,
                "amount_per_10": round(amount_per_10, 4),
                "amount_per_share": round(amount_per_10 / 10, 4),
                "ex_date": ex,
                "record_date": rec,
                "status": str(row.get("进度") or ""),
                "songgu_per_10": songgu,
                "zhuanzeng_per_10": zhuanzeng,
                "has_split": (songgu > 0 or zhuanzeng > 0),
            })
        except Exception as e:
            log.warning("dividend row parse failed for %s: %s", code, e)
            continue

    # Sort by announce_date desc (akshare usually returns oldest-first)
    out.sort(key=lambda r: r["announce_date"] or "", reverse=True)
    return out


def _fhps_em_rows(code: str) -> list[dict]:
    """Pull akshare 东财 stock_fhps_detail_em — gives us BOTH dividend amounts
    and per-period EPS keyed by 报告期 (H1 → YYYY-06-30, FY → YYYY-12-31).

    This is the data feed for Algorithm 1 (per-fiscal-year history) and
    Algorithm 2 (forecast yield). Returns most-recent-first list of dicts:
      {period: 'YYYY-MM-DD', fy: int, half: 'H1'|'FY',
       dividend_per_10: float, eps: float, status: str}
    """
    try:
        import akshare as ak
        df = ak.stock_fhps_detail_em(symbol=code)
    except Exception as e:
        log.warning("stock_fhps_detail_em failed for %s: %s", code, e)
        return []
    if df is None or df.empty:
        return []

    out: list[dict] = []
    for _, row in df.iterrows():
        try:
            period = row.get("报告期")
            period_s = str(period)[:10] if period is not None else ""
            if not period_s or period_s in ("NaT", "nan"):
                continue
            try:
                fy = int(period_s[:4])
            except ValueError:
                continue
            mmdd = period_s[5:10]
            if mmdd == "06-30":
                half = "H1"
            elif mmdd == "12-31":
                half = "FY"
            else:
                # quarterly or unusual; skip — we only model H1 + FY
                continue

            div_per_10 = float(row.get("现金分红-现金分红比例") or 0)
            eps = row.get("每股收益")
            try:
                eps_f = float(eps) if eps is not None and str(eps) not in ("nan", "NaT") else None
            except (ValueError, TypeError):
                eps_f = None
            status = str(row.get("方案进度") or "").strip()

            out.append({
                "period": period_s,
                "fy": fy,
                "half": half,
                "dividend_per_10": round(div_per_10, 4),
                "eps": eps_f,
                "status": status,
            })
        except Exception as e:
            log.warning("fhps_em row parse failed for %s: %s", code, e)
            continue

    # Sort most-recent first
    out.sort(key=lambda r: r["period"], reverse=True)
    return out


def _group_by_fiscal_year(fhps_rows: list[dict]) -> list[dict]:
    """Group fhps rows by FY (calendar year). Returns most-recent-first:
      [{fy: int, h1_div: float|None, fy_div: float|None,
        total_div_per_10: float, total_div_per_share: float,
        eps_fy: float|None, payout_ratio: float|None,
        is_announced: bool, statuses: list[str]}, ...]

    is_announced = True iff both H1 and 年报 (or only 年报 if no H1 dividend
    declared that year) have a row with status containing 「实施」 or
    「股东大会决议通过」 — i.e. the announcement is locked in even if not
    yet ex-dividend.
    """
    by_fy: dict[int, dict] = {}
    for r in fhps_rows:
        fy = r["fy"]
        bucket = by_fy.setdefault(fy, {"fy": fy, "h1": None, "fy_row": None})
        if r["half"] == "H1":
            bucket["h1"] = r
        else:  # FY
            bucket["fy_row"] = r

    out: list[dict] = []
    for fy in sorted(by_fy, reverse=True):
        b = by_fy[fy]
        h1 = b["h1"]
        fy_row = b["fy_row"]
        h1_div = h1["dividend_per_10"] if h1 else None
        fy_div = fy_row["dividend_per_10"] if fy_row else None
        total_div_per_10 = (h1_div or 0) + (fy_div or 0)
        eps_fy = fy_row["eps"] if fy_row else None  # 年报 row carries full-year EPS

        payout_ratio = None
        if eps_fy and eps_fy > 0 and total_div_per_10 > 0:
            # total_div_per_share = total_div_per_10 / 10
            payout_ratio = round((total_div_per_10 / 10) / eps_fy, 4)

        statuses: list[str] = []
        if h1:
            statuses.append(h1["status"])
        if fy_row:
            statuses.append(fy_row["status"])

        # FY is "announced" if the 年报 dividend has at least 决议通过 (i.e. board approved),
        # even if not yet ex-dividend. Standalone H1 isn't "complete" by itself.
        announced_keywords = ("实施", "决议通过")
        fy_announced = bool(fy_row and any(k in fy_row["status"] for k in announced_keywords))
        h1_announced = bool(h1 and any(k in h1["status"] for k in announced_keywords))
        # A FY counts as fully announced if 年报 is announced (with or without H1)
        # OR if the company doesn't pay 年报 but does pay H1 (rare) — for now,
        # require FY annual to be announced.
        is_announced = fy_announced

        out.append({
            "fy": fy,
            "h1_div_per_10": h1_div,
            "fy_div_per_10": fy_div,
            "total_div_per_10": round(total_div_per_10, 4),
            "total_div_per_share": round(total_div_per_10 / 10, 4),
            "eps_fy": eps_fy,
            "payout_ratio": payout_ratio,
            "is_announced": is_announced,
            "h1_announced": h1_announced,
            "fy_announced": fy_announced,
            "statuses": statuses,
        })
    return out


def _algorithm_1_historical(by_fy: list[dict], current_price: float | None) -> dict:
    """Algorithm 1: 按最近一个已公告完成的财年汇总分红 / 当前股价。

    选择最近一个 `is_announced=True` 的财年（年报至少决议通过），用其
    H1 + 年报 累计每股分红 / 现价，得出"历史口径"股息率。
    """
    method = (
        "选取最近一个已公告完成的财年（年报至少经股东大会决议通过），"
        "汇总该财年所有现金分红 (中报 + 年报)，除以当前股价。"
        "代表「按上一财年实际派息水平算的股息率」。"
    )
    out: dict[str, Any] = {
        "method": method,
        "current_price": current_price,
        "fiscal_year": None,
        "h1_dividend_per_share": None,
        "fy_dividend_per_share": None,
        "total_dividend_per_share": None,
        "yield_pct": None,
        "calculation": None,
        "note": None,
    }

    candidate = next((fy for fy in by_fy if fy["is_announced"]), None)
    if candidate is None:
        out["note"] = "没有任何财年的年报分红被公告，无法计算算法 1"
        return out
    if current_price is None or current_price <= 0:
        out["note"] = "现价不可用，无法计算算法 1 的股息率（已给出每股分红数据）"

    out["fiscal_year"] = candidate["fy"]
    h1_share = (candidate["h1_div_per_10"] or 0) / 10
    fy_share = (candidate["fy_div_per_10"] or 0) / 10
    total_share = h1_share + fy_share
    out["h1_dividend_per_share"] = round(h1_share, 4) if candidate["h1_div_per_10"] else None
    out["fy_dividend_per_share"] = round(fy_share, 4) if candidate["fy_div_per_10"] else None
    out["total_dividend_per_share"] = round(total_share, 4)

    if current_price and current_price > 0 and total_share > 0:
        y = total_share / current_price * 100
        out["yield_pct"] = round(y, 2)
        parts = []
        if h1_share > 0:
            parts.append(f"{h1_share:.2f} (中报)")
        if fy_share > 0:
            parts.append(f"{fy_share:.2f} (年报)")
        sum_str = " + ".join(parts) if len(parts) > 1 else f"{total_share:.2f}"
        out["calculation"] = (
            f"FY{candidate['fy']} 总分红 = {sum_str} = {total_share:.2f} 元/股；"
            f"算法 1 股息率 = {total_share:.2f} / {current_price:.2f} = {y:.2f}%"
        )
    return out


def _algorithm_2_forecast(by_fy: list[dict], current_price: float | None,
                          lookback: int = 3) -> dict:
    """Algorithm 2: 用近 N 年平均派息率 × 预测下一年 EPS / 当前股价 = 预测股息率。

    Steps:
      1. 取最近 N 个 EPS 完整 (eps_fy > 0) + 分红已公告 (is_announced=True) 的财年
      2. 各年派息率 = (年内总分红/股) / EPS_FY
      3. 平均派息率 = 各年派息率均值
      4. 各年 YoY EPS 增长率 = EPS_t / EPS_{t-1} - 1
      5. 平均 EPS 增长率 = 各年增长率均值
      6. 预测下一年 EPS = 最新 FY EPS × (1 + 平均增长率)
      7. 预测下一年分红 = 预测 EPS × 平均派息率
      8. 算法 2 股息率 = 预测分红 / 当前股价
    """
    method = (
        "用近 N 年的派息率均值 × 用 EPS 增长率外推得到的下一年预测 EPS / 当前股价。"
        "代表「假设公司维持过去派息率和盈利增速时，当前价位买入未来一年的预期股息率」。"
        "**预测值仅供参考**——一次性损益、行业拐点、回购政策变化都会让实际偏离。"
    )
    out: dict[str, Any] = {
        "method": method,
        "current_price": current_price,
        "lookback_target": lookback,
        "fiscal_years_used": [],
        "per_year": [],
        "avg_payout_ratio": None,
        "avg_eps_growth_pct": None,
        "latest_eps": None,
        "predicted_next_eps": None,
        "predicted_next_dividend_per_share": None,
        "yield_pct": None,
        "calculation": None,
        "note": None,
    }

    # Eligible = 已公告 + EPS 正且非空 + 派息率计算出来了
    eligible = [
        fy for fy in by_fy
        if fy["is_announced"] and fy["eps_fy"] is not None
        and fy["eps_fy"] > 0 and fy["payout_ratio"] is not None
    ]
    used = eligible[:lookback]  # most-recent first
    if len(used) < 2:
        out["note"] = (
            f"算法 2 需要至少 2 个完整财年数据（含分红+EPS），目前只有 {len(used)} 个，"
            "无法计算预测股息率"
        )
        return out

    out["fiscal_years_used"] = [fy["fy"] for fy in used]
    out["per_year"] = [
        {
            "fy": fy["fy"],
            "dividend_per_share": fy["total_div_per_share"],
            "eps": fy["eps_fy"],
            "payout_ratio": fy["payout_ratio"],
        }
        for fy in used
    ]

    avg_payout = sum(fy["payout_ratio"] for fy in used) / len(used)
    out["avg_payout_ratio"] = round(avg_payout, 4)

    # YoY growth: used is most-recent first, so iterate adjacent pairs
    growth_rates: list[float] = []
    for i in range(len(used) - 1):
        newer = used[i]
        older = used[i + 1]
        if older["eps_fy"] and older["eps_fy"] > 0:
            g = (newer["eps_fy"] / older["eps_fy"]) - 1
            growth_rates.append(g)
    if not growth_rates:
        out["note"] = "EPS 增长率无法计算（缺少历史 EPS）"
        return out
    avg_growth = sum(growth_rates) / len(growth_rates)
    out["avg_eps_growth_pct"] = round(avg_growth * 100, 2)

    latest_eps = used[0]["eps_fy"]
    out["latest_eps"] = latest_eps
    predicted_eps = latest_eps * (1 + avg_growth)
    out["predicted_next_eps"] = round(predicted_eps, 4)
    predicted_div = predicted_eps * avg_payout
    out["predicted_next_dividend_per_share"] = round(predicted_div, 4)

    if current_price and current_price > 0:
        y = predicted_div / current_price * 100
        out["yield_pct"] = round(y, 2)
        payout_pcts = ", ".join(f"FY{p['fy']}={p['payout_ratio']*100:.1f}%" for p in out["per_year"])
        growth_pcts = ", ".join(f"{g*100:+.1f}%" for g in growth_rates)
        out["calculation"] = (
            f"派息率序列 [{payout_pcts}] → 平均 {avg_payout*100:.1f}%；"
            f"EPS 增长率序列 [{growth_pcts}] → 平均 {avg_growth*100:+.1f}%；"
            f"预测下一年 EPS = {latest_eps:.2f} × (1 + {avg_growth:+.3f}) = {predicted_eps:.2f}；"
            f"预测下一年分红 = {predicted_eps:.2f} × {avg_payout:.3f} = {predicted_div:.2f} 元/股；"
            f"算法 2 预测股息率 = {predicted_div:.2f} / {current_price:.2f} = {y:.2f}%"
        )

    # Flag anomalous EPS jumps that distort the forecast (mainly 一次性 gains)
    if any(abs(g) > 0.4 for g in growth_rates):
        out["note"] = (
            "近期 EPS 单年波动 >40%（可能含一次性损益），算法 2 的外推会被放大，"
            "解读时**重点关注**这条注意。"
        )
    return out


def _dividend_history_hk(code: str) -> list[dict]:
    """Pull HK dividend history via akshare → 东财 stock_hk_dividend_payout_em.

    Returns list of dividend events sorted **most-recent first** with normalized fields:
      announce_date / fiscal_year / per_share_hkd / ex_date / dispatch_date /
      div_type (年度分配/中期分配/特别分配/季度分配)

    Empty list on any failure.
    """
    try:
        import akshare as ak
        df = ak.stock_hk_dividend_payout_em(symbol=code)
    except Exception as e:
        log.warning("stock_hk_dividend_payout_em failed for %s: %s", code, e)
        return []
    if df is None or df.empty:
        return []

    def _date_str(v):
        if v is None: return None
        s = str(v)
        if s in ("NaT", "nan", "None", ""): return None
        if hasattr(v, "strftime"):
            try: return v.strftime("%Y-%m-%d")
            except (ValueError, TypeError): return None
        return s[:10]

    # Parse 分红方案 to extract HKD per-share amount
    # Examples:
    #   "每股派港币5.3元" → 5.3
    #   "每股派美元0.1元(相当于港币0.783595元(计算值))" → 0.783595
    #   "特殊说明:..." → 0 (skip non-cash dividends)
    import re
    RE_HKD = re.compile(r"港币\s*([\d.]+)\s*元")
    RE_HKD_INLINE = re.compile(r"派港币\s*([\d.]+)\s*元")

    out = []
    for _, row in df.iterrows():
        try:
            scheme = str(row.get("分红方案") or "")
            # Try inline pattern first, then fallback to any HKD mention
            m = RE_HKD_INLINE.search(scheme)
            if not m:
                m = RE_HKD.search(scheme)
            per_share_hkd = float(m.group(1)) if m else 0.0
            # Skip non-cash dividends (stock splits etc)
            if per_share_hkd <= 0 and ("派" not in scheme or "股份" in scheme):
                continue

            out.append({
                "announce_date": _date_str(row.get("最新公告日期")),
                "fiscal_year": str(row.get("财政年度") or ""),
                "per_share_hkd": round(per_share_hkd, 4),
                "div_type": str(row.get("分配类型") or ""),
                "ex_date": _date_str(row.get("除净日")),
                "dispatch_date": _date_str(row.get("发放日")),
                "scheme_raw": scheme,
            })
        except Exception as e:
            log.warning("HK dividend row parse failed for %s: %s", code, e)
            continue

    out.sort(key=lambda r: r["announce_date"] or "", reverse=True)
    return out


def _get_dividend_history_hk(query: str, info, last_n: int = 10) -> dict:
    """HK dividend yield — only Algorithm 1 (historical), no forecasts (EPS data unreliable)."""
    history = _dividend_history_hk(info.code)

    # Get current price
    current_price = None
    try:
        tc = _tencent_fetch(info.tencent_symbol)
        c = tc.get("current")
        if c and c > 0:
            current_price = float(c)
    except Exception:
        pass

    if not history:
        return {
            "ok": True, "query": query,
            "resolved": {"code": info.code, "name": info.name, "market": "hk"},
            "current_price": current_price,
            "history": [],
            "algorithm_1_historical": {"note": "无分红记录"},
            "algorithm_2_forecast": {"note": "港股暂不支持预测算法（EPS 数据不全）"},
            "source": "akshare/stock_hk_dividend_payout_em",
            "note": "未查询到港股分红记录",
        }

    # Group by fiscal_year, sum all dividends per year
    from collections import defaultdict
    by_fy = defaultdict(lambda: {"per_share_total_hkd": 0.0, "events": []})
    for h in history:
        fy = h["fiscal_year"]
        if not fy:
            continue
        by_fy[fy]["per_share_total_hkd"] = round(by_fy[fy]["per_share_total_hkd"] + h["per_share_hkd"], 4)
        by_fy[fy]["events"].append({
            "announce_date": h["announce_date"],
            "ex_date": h["ex_date"],
            "per_share_hkd": h["per_share_hkd"],
            "div_type": h["div_type"],
        })

    fy_list = sorted(by_fy.items(), key=lambda x: x[0], reverse=True)

    # Algorithm 1: take latest complete year (skip current year if incomplete)
    # Heuristic: a year is "complete" if its earliest event has ex_date or if it has 年度分配
    algo1 = None
    for fy, info_fy in fy_list:
        has_annual = any(e["div_type"] in ("年度分配", "末期分配") for e in info_fy["events"])
        # Use this year if it has annual distribution OR if it's at least 1 year old
        from datetime import date
        latest_announce = max((e.get("announce_date") or "" for e in info_fy["events"]), default="")
        is_old_enough = latest_announce < (date.today().replace(year=date.today().year - 1).strftime("%Y-%m-%d"))

        if has_annual or is_old_enough:
            total_div = info_fy["per_share_total_hkd"]
            yield_pct = (total_div / current_price * 100) if current_price else None
            calc = (
                f"算法 1（历史口径）：\n"
                f"  - 选取最近完整财年: {fy}\n"
                f"  - 该年全部派息（含中期/末期/特别）: {total_div} 港币/股\n"
                f"  - 当前股价: {current_price} 港币\n"
                f"  - 股息率 = {total_div} / {current_price} = {yield_pct:.2f}%" if current_price else
                f"算法 1：缺当前股价"
            )
            algo1 = {
                "fiscal_year": fy,
                "total_dividend_per_share_hkd": total_div,
                "current_price_hkd": current_price,
                "yield_pct": round(yield_pct, 2) if yield_pct is not None else None,
                "events": info_fy["events"],
                "calculation": calc,
            }
            break

    if algo1 is None:
        algo1 = {"note": "未找到完整财年数据"}

    return {
        "ok": True, "query": query,
        "resolved": {"code": info.code, "name": info.name, "market": "hk"},
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "current_price": current_price,
        "history": history[:last_n],
        "by_fiscal_year": [{"fy": fy, **dict(info_fy)} for fy, info_fy in fy_list[:5]],
        "algorithm_1_historical": algo1,
        "algorithm_2_forecast": {
            "note": "港股暂不支持预测算法 — akshare 港股 EPS 数据不全，无法外推。建议参考算法 1 历史口径。"
        },
        "source": "akshare/stock_hk_dividend_payout_em",
    }


def get_dividend_history(query: str, *, last_n: int = 10) -> dict:
    """Public dividend-history fetcher with two yield algorithms. A-share only.

    Returns:
        {ok, query, resolved, fetched_at,
         history: [..raw events..],
         ttm: {..backwards-compat trailing-12-month..},
         by_fiscal_year: [..H1+FY grouped..],
         algorithm_1_historical: {..yield by last complete FY..},
         algorithm_2_forecast: {..predicted yield from payout × growth..},
         source}
    """
    info = resolve_ticker(query)
    if info is None:
        return {"ok": False, "query": query, "error": f"无法识别股票：{query!r}"}
    if info.market == "hk":
        return _get_dividend_history_hk(query, info, last_n)
    if info.market == "a-share" and info.board == "etf":
        # ETF: route to dedicated frequency-aware handler
        from .etf_dividend import get_etf_dividend_yield
        # Get current price first
        cp = None
        try:
            tc = _tencent_fetch(info.tencent_symbol)
            cp = tc.get("current")
            if cp and cp > 0:
                cp = float(cp)
        except Exception:
            cp = None
        return get_etf_dividend_yield(query, info, cp, last_n=last_n)
    if info.market != "a-share":
        return {
            "ok": False, "query": query,
            "resolved": {"code": info.code, "name": info.name, "market": info.market},
            "error": f"分红数据当前只支持 A 股、港股、A 股 ETF；{info.market} 暂未实现",
        }

    history = _dividend_history_a_share(info.code)
    fhps_rows = _fhps_em_rows(info.code)

    # Pull current price once for both yield algorithms + TTM
    current_price: float | None = None
    try:
        tc = _tencent_fetch(info.tencent_symbol)
        c = tc.get("current")
        if c and c > 0:
            current_price = float(c)
    except Exception:
        pass

    if not history and not fhps_rows:
        return {
            "ok": True, "query": query,
            "resolved": {"code": info.code, "name": info.name, "market": "a-share"},
            "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "current_price": current_price,
            "ttm": {"total_per_share": 0, "events": 0, "yield_pct": None},
            "by_fiscal_year": [],
            "algorithm_1_historical": {"note": "无数据"},
            "algorithm_2_forecast": {"note": "无数据"},
            "history": [],
            "source": "akshare/stock_history_dividend_detail + stock_fhps_detail_em",
            "note": "未查询到分红记录（可能是非派现公司或新上市）",
        }

    # --- TTM (legacy, backwards-compat) — kept for callers that still read this
    from datetime import timedelta as _td
    cutoff = (datetime.now(timezone.utc) - _td(days=365)).strftime("%Y-%m-%d")
    ttm_events = [
        h for h in history
        if h.get("ex_date") and h["ex_date"] >= cutoff and h["status"] == "实施"
    ]
    ttm_total = round(sum(h["amount_per_share"] for h in ttm_events), 4)
    ttm_yield_pct = None
    if ttm_total > 0 and current_price:
        ttm_yield_pct = round(ttm_total / current_price * 100, 2)

    # --- New: fiscal-year grouping + the two algorithms
    by_fy = _group_by_fiscal_year(fhps_rows)
    algo1 = _algorithm_1_historical(by_fy, current_price)
    algo2 = _algorithm_2_forecast(by_fy, current_price)

    return {
        "ok": True,
        "query": query,
        "resolved": {"code": info.code, "name": info.name, "market": "a-share"},
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "current_price": current_price,
        "ttm": {  # legacy, prefer algorithm_1 for "current yield"
            "total_per_share": ttm_total,
            "events": len(ttm_events),
            "yield_pct": ttm_yield_pct,
            "window_days": 365,
            "deprecation_note": (
                "TTM (滚动 12 月) 容易跨财年导致解读歧义；优先用 algorithm_1_historical "
                "(按完整财年汇总) 或 algorithm_2_forecast (预测下一年)。"
            ),
        },
        "by_fiscal_year": by_fy[:max(last_n, 5)],
        "algorithm_1_historical": algo1,
        "algorithm_2_forecast": algo2,
        "history": history[:last_n],
        "source": "akshare/stock_history_dividend_detail + stock_fhps_detail_em",
    }


def _index_recent_10d(symbol: str, label: str) -> dict:
    try:
        import akshare as ak
        df = ak.stock_zh_index_daily(symbol=symbol)
        last = df.tail(11)
        rows = []
        prev_close = None
        for _, row in last.iterrows():
            close = float(row["close"])
            pct = None if prev_close is None else round((close / prev_close - 1) * 100, 2)
            rows.append({
                "date": str(row["date"])[:10],
                "close": round(close, 2),
                "change_pct": pct,
            })
            prev_close = close
        # drop the seed row (no change_pct)
        rows = rows[1:] if rows and rows[0]["change_pct"] is None else rows
        return {"name": label, "symbol": symbol, "recent_10d": rows}
    except Exception as e:
        log.warning("index %s (%s) failed: %s", label, symbol, e)
        return {"name": label, "symbol": symbol, "recent_10d": [], "error": str(e)[:120]}


def _format_market_cap(mc_yi: float | None) -> str | None:
    if mc_yi is None:
        return None
    if mc_yi >= 10000:
        return f"{mc_yi/10000:.2f} 万亿"
    return f"{mc_yi:.0f} 亿"


# ----- public API ----------------------------------------------------

def get_stock_snapshot(query: str) -> dict:
    """Build a composite snapshot. Returns a dict the agent can pass to the LLM.

    Best-effort: each source can fail independently; missing fields are just absent.
    """
    info = resolve_ticker(query)
    if info is None:
        return {"ok": False, "query": query, "error": f"无法识别股票：{query!r}"}

    cache_key = info.tencent_symbol
    cached = _SNAPSHOT_CACHE.get(cache_key)
    if cached and time.time() - cached[0] < SNAPSHOT_TTL_S:
        return cached[1]

    result: dict[str, Any] = {
        "ok": True,
        "query": query,
        "resolved": {
            "code": info.code,
            "name": info.name,
            "market": info.market,
            "board": info.board,
        },
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }

    # 1. Tencent (price + PE/PB + market cap; most reliable single call)
    tc = _tencent_fetch(info.tencent_symbol)
    if tc.get("name") and not info.name.startswith(info.code):
        # leave resolved name as-is
        pass
    elif tc.get("name"):
        result["resolved"]["name"] = tc["name"]

    if tc:
        result["price"] = {
            "current": tc.get("current"),
            "change_today_pct": tc.get("change_pct"),
            "high_today": tc.get("high_today"),
            "low_today": tc.get("low_today"),
        }
        result["valuation"] = {
            "pe_ttm": tc.get("pe_ttm"),
            "pb": tc.get("pb"),
        }
        result["scale"] = {
            "total_market_cap_yi": tc.get("total_mc_yi"),
            "total_market_cap_display": _format_market_cap(tc.get("total_mc_yi")),
        }

    # A-share specific enrichment
    if info.market == "a-share":
        # 2. 1y stats (52w hi/lo + 1y change pct from 新浪 K-line, single call)
        ystats = _one_year_stats(info.tencent_symbol)
        if "change_1y_pct" in ystats:
            result.setdefault("price", {})["change_1y_pct"] = ystats["change_1y_pct"]
        if "high_52w" in ystats:
            result.setdefault("price", {})["high_52w"] = ystats["high_52w"]
        if "low_52w" in ystats:
            result.setdefault("price", {})["low_52w"] = ystats["low_52w"]

        # 3. 同花顺 主营业务
        zy = _ths_main_business(info.code)
        if zy:
            result.setdefault("business", {}).update({
                "main_business": zy.get("主营业务"),
                "product_type": zy.get("产品类型"),
                "products": zy.get("产品名称"),
            })

        # 4. 雪球: 控股 + 行业 + 公司全名
        xq = _xq_basic_info(info.xq_symbol)
        if xq:
            ind = xq.get("affiliate_industry")
            industry_name = None
            if isinstance(ind, dict):
                industry_name = ind.get("ind_name")
            elif isinstance(ind, str):
                try:
                    industry_name = json.loads(ind.replace("'", '"')).get("ind_name")
                except Exception:
                    industry_name = None
            if industry_name:
                result.setdefault("business", {})["industry"] = industry_name

            result["ownership"] = {
                "actual_controller": xq.get("actual_controller"),
                "ownership_class": xq.get("classi_name"),
            }
            result["company"] = {
                "full_name": xq.get("org_name_cn"),
                "short_name": xq.get("org_short_name_cn"),
                "chairman": xq.get("chairman"),
                "staff_num": xq.get("staff_num"),
            }

    # 5. Index context (last 10 days)
    indices: list[tuple[str, str]] = []
    if info.market == "a-share":
        indices.append(("sh000001", "上证指数"))
        if info.board == "gem":
            indices.append(("sz399006", "创业板指"))
        elif info.board == "star":
            indices.append(("sh000688", "科创50"))
    elif info.market == "hk":
        # Hang Seng — Tencent has it but we'd need a different parser; skip in v1
        pass

    if indices:
        result["index_context"] = [_index_recent_10d(sym, lbl) for sym, lbl in indices]

    _SNAPSHOT_CACHE[cache_key] = (time.time(), result)
    return result


# Convenience for the MCP tool's docstring or debugging
def format_snapshot_human(snap: dict) -> str:
    """Render the snapshot as a markdown-ish blob (used in tests + as fallback view)."""
    if not snap.get("ok"):
        return f"❌ {snap.get('error','')}"
    r = snap.get("resolved", {})
    lines = [f"## {r.get('name','?')} ({r.get('code','?')}) — {r.get('market','?')}/{r.get('board','?')}"]
    p = snap.get("price", {})
    v = snap.get("valuation", {})
    s = snap.get("scale", {})
    o = snap.get("ownership", {})
    b = snap.get("business", {})
    if p:
        lines.append(f"- 现价 {p.get('current','?')} · 1y涨跌 {p.get('change_1y_pct','?')}% · "
                     f"52w hi/lo {p.get('high_52w','?')}/{p.get('low_52w','?')}")
    if v:
        lines.append(f"- 估值 PE_TTM {v.get('pe_ttm','?')} / PB {v.get('pb','?')}")
    if s.get('total_market_cap_display'):
        lines.append(f"- 市值 {s['total_market_cap_display']}")
    if o:
        lines.append(f"- 控股 {o.get('ownership_class','?')} · {o.get('actual_controller','?')}")
    if b:
        lines.append(f"- 行业 {b.get('industry','?')} · 主营 {b.get('main_business','?')}")
    for idx in snap.get("index_context", []):
        last = idx.get("recent_10d", [])
        if last:
            first_d, last_d = last[0], last[-1]
            total_pct = round(
                (last_d.get("close", 0) / (first_d.get("close", 0) or 1) - 1) * 100, 2
            )
            lines.append(f"- {idx['name']} 最近10日：{first_d['date']} {first_d['close']} → "
                         f"{last_d['date']} {last_d['close']}（合计 {total_pct:+.2f}%）")
    return "\n".join(lines)
