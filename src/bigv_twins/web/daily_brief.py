"""投资日报 (/report) 后端逻辑 — 行情面板（实时按需 + 1 min 缓存）。

P2 scope: get_global_indices() + get_watchlist_quotes().
Subsequent phases will add: jin10 news cache, blogger_daily_brief generator,
watchlist news cross-ref.
"""

from __future__ import annotations

import logging
import time
from typing import Iterable

import httpx

from bigv_twins.stock_data import resolve_ticker

log = logging.getLogger("bigv_twins.web.daily_brief")


# ---------------------------------------------------------------- 全球行情

# (tencent_symbol, display_name, category, format)
# format: "index" (tilde-separated, fields[3]=current, [31]=change_amt, [32]=change_pct)
#         "hf"    (comma-separated, [0]=current, [1]=change_amt, [12]=name)
INDEX_SYMBOLS: list[tuple[str, str, str, str]] = [
    ("sh000001",  "上证综指",   "a-share", "index"),
    ("sz399006",  "创业板指",   "a-share", "index"),
    ("sh000688",  "科创50",     "a-share", "index"),
    ("sh000300",  "沪深300",    "a-share", "index"),
    ("hkHSI",     "恒生指数",   "hk",      "index"),
    ("hkHSTECH",  "恒生科技",   "hk",      "index"),
    ("usINX",     "标普500",    "us",      "index"),
    ("usIXIC",    "纳斯达克",   "us",      "index"),
    ("hf_XAU",    "伦敦金现",   "commodity", "hf"),
    ("hf_OIL",    "布伦特原油", "commodity", "hf"),
]


_CACHE: dict[str, tuple[float, dict]] = {}
_CACHE_TTL_S = 60


def _parse_index_tilde(symbol: str, payload: str) -> dict | None:
    """Standard Tencent qt format: tilde-separated, 50+ fields."""
    try:
        fields = payload.split("~")
        def _f(idx):
            return float(fields[idx]) if len(fields) > idx and fields[idx] not in ("", "-") else None
        return {
            "symbol": symbol,
            "name": fields[1] or symbol,
            "current": _f(3),
            "prev_close": _f(4),
            "change_amt": _f(31),
            "change_pct": _f(32),
            "pe": _f(39),
            "pb": _f(46),
            "market_cap": _f(45),
        }
    except (ValueError, IndexError) as e:
        log.warning("parse_index_tilde failed for %s: %s", symbol, e)
        return None


def _parse_hf(symbol: str, payload: str) -> dict | None:
    """Tencent international futures (hf_) format: comma-separated.
    e.g. "4524.31,-0.40,4526.20,4526.30,4547.00,4508.20,21:01:04,4542.50,..."
    fields: [0]=current, [1]=change_pct (not amount!), [2]=bid, [3]=ask,
            [4]=high, [5]=low, [6]=time, [7]=prev_settle, [8]=open, ...[12]=name
    Wait — looking at sample: "4524.31,-0.40,..." — that's change as PCT? Let me check.
    Actually -0.40 on 4524.31 ≈ 0.01% which would be a tiny pct, or 0.40 as
    absolute change ($0.40 on $4500 = ~0.01% pct). Sample: 黄金 -0.40, 现价
    4524.31; prev_settle 4542.50. (4524.31 - 4542.50) / 4542.50 = -0.40%. So
    field[1] IS the change PCT, NOT the absolute change.
    """
    try:
        fields = payload.split(",")
        current = float(fields[0]) if fields[0] else None
        change_pct = float(fields[1]) if fields[1] else None  # already a percentage
        prev = float(fields[7]) if len(fields) > 7 and fields[7] else None
        # Compute change_amt = current - prev
        change_amt = (current - prev) if (current is not None and prev is not None) else None
        name = fields[12] if len(fields) > 12 else symbol
        return {
            "symbol": symbol,
            "name": name or symbol,
            "current": current,
            "prev_close": prev,
            "change_amt": change_amt,
            "change_pct": change_pct,
        }
    except (ValueError, IndexError) as e:
        log.warning("parse_hf failed for %s: %s", symbol, e)
        return None


def _fetch_tencent_batch(symbols: list[str], timeout: float = 5.0) -> dict[str, str]:
    """Batched Tencent qt fetch. Returns dict symbol → payload string (the part
    inside the v_<sym>="..."; assignment). Symbols that error or aren't returned
    are simply absent from the result.
    """
    if not symbols:
        return {}
    url = f"http://qt.gtimg.cn/q={','.join(symbols)}"
    try:
        r = httpx.get(url, timeout=timeout)
    except Exception as e:
        log.warning("tencent batch fetch failed: %s", e)
        return {}
    text = r.content.decode("gbk", errors="replace")
    out: dict[str, str] = {}
    # Each line: v_<sym>="payload";
    for line in text.splitlines():
        if not line.startswith("v_"):
            continue
        eq = line.find("=")
        if eq < 0:
            continue
        sym = line[2:eq]
        # payload is between first " and last "
        first_q = line.find('"', eq)
        last_q = line.rfind('"')
        if first_q < 0 or last_q <= first_q:
            continue
        out[sym] = line[first_q + 1 : last_q]
    return out


def get_global_indices() -> list[dict]:
    """Return the global index/commodity panel data. 1-min in-process cache
    keyed by a constant; all 10 symbols batched in one HTTP call.
    """
    cache_key = "__global_indices__"
    now = time.time()
    cached = _CACHE.get(cache_key)
    if cached and now - cached[0] < _CACHE_TTL_S:
        return cached[1]["items"]

    symbols = [s[0] for s in INDEX_SYMBOLS]
    payloads = _fetch_tencent_batch(symbols)

    items: list[dict] = []
    for symbol, label, category, fmt in INDEX_SYMBOLS:
        p = payloads.get(symbol)
        if not p:
            items.append({
                "symbol": symbol, "label": label, "category": category,
                "current": None, "change_pct": None, "change_amt": None,
                "ok": False, "error": "fetch failed",
            })
            continue
        parsed = _parse_index_tilde(symbol, p) if fmt == "index" else _parse_hf(symbol, p)
        if parsed is None:
            items.append({
                "symbol": symbol, "label": label, "category": category,
                "ok": False, "error": "parse failed",
                "current": None, "change_pct": None, "change_amt": None,
            })
            continue
        # Use our display label rather than the Chinese name from Tencent
        # (which may have markup like "Hang Seng Index" for some HK symbols)
        items.append({
            "symbol": symbol,
            "label": label,
            "category": category,
            "current": parsed["current"],
            "change_pct": parsed["change_pct"],
            "change_amt": parsed["change_amt"],
            "ok": True,
        })

    _CACHE[cache_key] = (now, {"items": items})
    return items


# ---------------------------------------------------------------- 自选股行情


def get_watchlist_quotes(watchlist_items: Iterable) -> list[dict]:
    """Given a list of UserWatchlist ORM objects, fetch real-time quotes.

    Batched single Tencent call (same as indices). 1-min cache per ticker.
    Returns aligned list — for each input item, output has {ticker, name,
    market, note, current, change_pct, change_amt, ok}.
    """
    items_list = list(watchlist_items)
    if not items_list:
        return []

    # Build the tencent symbol for each ticker via resolve_ticker (handles
    # 6xxxxx / 0xxxxx / 3xxxxx / 8xxxxx → sh/sz/bj prefix; HK → hk prefix)
    ticker_to_sym: dict[str, str | None] = {}
    for w in items_list:
        info = resolve_ticker(w.ticker)
        ticker_to_sym[w.ticker] = info.tencent_symbol if info else None

    # Cache: per-symbol 60s. Build the to-fetch list.
    now = time.time()
    payloads: dict[str, str] = {}
    syms_to_fetch: list[str] = []
    sym_to_cached: dict[str, dict] = {}
    for sym in {s for s in ticker_to_sym.values() if s}:
        cache_entry = _CACHE.get(f"q:{sym}")
        if cache_entry and now - cache_entry[0] < _CACHE_TTL_S:
            sym_to_cached[sym] = cache_entry[1]
        else:
            syms_to_fetch.append(sym)

    if syms_to_fetch:
        fresh = _fetch_tencent_batch(syms_to_fetch)
        for sym, p in fresh.items():
            parsed = _parse_index_tilde(sym, p)  # stocks use the same tilde format
            if parsed:
                _CACHE[f"q:{sym}"] = (now, parsed)
                sym_to_cached[sym] = parsed

    out: list[dict] = []
    for w in items_list:
        sym = ticker_to_sym.get(w.ticker)
        cached = sym_to_cached.get(sym) if sym else None
        if cached:
            out.append({
                "ticker": w.ticker, "name": w.name, "market": w.market,
                "note": w.note, "wid": w.id,
                "current": cached["current"],
                "change_pct": cached["change_pct"],
                "change_amt": cached["change_amt"],
                "pe": cached.get("pe"),
                "pb": cached.get("pb"),
                "market_cap": cached.get("market_cap"),
                "ok": True,
            })
        else:
            out.append({
                "ticker": w.ticker, "name": w.name, "market": w.market,
                "note": w.note, "wid": w.id,
                "current": None, "change_pct": None, "change_amt": None,
                "ok": False,
            })
    return out
