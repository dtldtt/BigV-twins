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
        "high_52w": f(41, float),
        "low_52w": f(42, float),
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


def _one_year_change_pct(symbol: str) -> Optional[float]:
    try:
        import akshare as ak
        df = ak.stock_zh_a_daily(symbol=symbol, adjust="qfq")
        if df is None or len(df) < 252:
            return None
        current = float(df.iloc[-1]["close"])
        year_ago = float(df.iloc[-252]["close"])
        return round((current / year_ago - 1) * 100, 1)
    except Exception as e:
        log.warning("1y change failed for %s: %s", symbol, e)
        return None


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
            "high_52w": tc.get("high_52w"),
            "low_52w": tc.get("low_52w"),
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
        # 2. 1y price change (新浪 K-line)
        y1 = _one_year_change_pct(info.tencent_symbol)
        if y1 is not None:
            result.setdefault("price", {})["change_1y_pct"] = y1

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
