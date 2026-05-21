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

log = logging.getLogger("bigv_twins.stock_data")

# ----- caches ---------------------------------------------------------

_NAME_MAP_CACHE: dict[str, Any] = {"ts": 0.0, "df": None}
_SNAPSHOT_CACHE: dict[str, tuple[float, dict]] = {}
SNAPSHOT_TTL_S = 600    # 10 min
NAME_MAP_TTL_S = 3600   # 1 hour


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


def _load_name_map():
    """Cache code→name for A-share lookups."""
    now = time.time()
    if _NAME_MAP_CACHE["df"] is not None and now - _NAME_MAP_CACHE["ts"] < NAME_MAP_TTL_S:
        return _NAME_MAP_CACHE["df"]
    import akshare as ak
    for attempt in range(3):
        try:
            df = ak.stock_info_a_code_name()
            _NAME_MAP_CACHE["df"] = df
            _NAME_MAP_CACHE["ts"] = now
            return df
        except Exception as e:
            log.warning("name map fetch attempt %d failed: %s", attempt + 1, e)
            time.sleep(1)
    return None


def resolve_ticker(query: str) -> Optional[TickerInfo]:
    query = (query or "").strip()
    if not query:
        return None

    # pure 6-digit A-share code
    if re.fullmatch(r"\d{6}", query):
        df = _load_name_map()
        name = query
        if df is not None:
            row = df[df["code"] == query]
            if not row.empty:
                name = row.iloc[0]["name"]
        return TickerInfo(
            code=query, name=name,
            prefix=_a_share_prefix(query),
            market="a-share", board=_a_share_board(query),
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


def get_dividend_history(query: str, *, last_n: int = 10) -> dict:
    """Public dividend-history fetcher. A-share only for v1.

    Returns:
        {ok, query, resolved, fetched_at, ttm: {total_per_share, events, yield_pct},
         history: [...up to last_n events...], source}
    """
    info = resolve_ticker(query)
    if info is None:
        return {"ok": False, "query": query, "error": f"无法识别股票：{query!r}"}
    if info.market != "a-share":
        return {
            "ok": False, "query": query,
            "resolved": {"code": info.code, "name": info.name, "market": info.market},
            "error": f"分红数据当前只支持 A 股；{info.market} 暂未实现",
        }

    history = _dividend_history_a_share(info.code)
    if not history:
        return {
            "ok": True, "query": query,
            "resolved": {"code": info.code, "name": info.name, "market": "a-share"},
            "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "ttm": {"total_per_share": 0, "events": 0, "yield_pct": None},
            "history": [],
            "source": "akshare/stock_history_dividend_detail",
            "note": "未查询到分红记录（可能是非派现公司或新上市）",
        }

    # TTM (trailing 12 months) — sum dividend events whose ex_date is within
    # the last 365 days. Use ex_date for the cash-actually-paid date.
    from datetime import timedelta as _td
    cutoff = (datetime.now(timezone.utc) - _td(days=365)).strftime("%Y-%m-%d")
    ttm_events = [
        h for h in history
        if h.get("ex_date") and h["ex_date"] >= cutoff and h["status"] == "实施"
    ]
    ttm_total = round(sum(h["amount_per_share"] for h in ttm_events), 4)

    # Annualized yield (%) using current Tencent price; best-effort.
    yield_pct = None
    if ttm_total > 0:
        try:
            tc = _tencent_fetch(info.tencent_symbol)
            current = tc.get("current")
            if current and current > 0:
                yield_pct = round(ttm_total / current * 100, 2)
        except Exception:
            pass

    return {
        "ok": True,
        "query": query,
        "resolved": {"code": info.code, "name": info.name, "market": "a-share"},
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "ttm": {
            "total_per_share": ttm_total,
            "events": len(ttm_events),
            "yield_pct": yield_pct,
            "window_days": 365,
        },
        "history": history[:last_n],
        "source": "akshare/stock_history_dividend_detail",
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
