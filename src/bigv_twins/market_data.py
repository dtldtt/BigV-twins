"""Topic-driven market context fetcher.

Reads topics.json (project root) for keyword → topic → asset mapping, then fetches
recent (1 week + 1 month) performance for each asset. Each fetch has a primary
source + Tencent quote backup, and individual failures don't take down the whole call.

Uses:
- akshare `stock_zh_index_daily(sh*/sz*)`        : A-share indices history
- akshare `stock_hk_index_daily_em(HSI/HSCEI/...)`: HK indices history
- akshare `index_us_stock_sina(.DJI/.IXIC/.INX)` : US indices history
- Tencent `qt.gtimg.cn/q=<symbol>`               : real-time spot for ETFs / fallback
- Tencent `qt.gtimg.cn/q=hf_GC`                  : 现货黄金 (special parser)
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import httpx

log = logging.getLogger("bigv_twins.market_data")

# ----- topics.json loader (cached 1h) ------------------------------

_TOPICS_PATH = Path(__file__).resolve().parent.parent.parent / "topics.json"
_TOPICS_CACHE: dict[str, Any] = {"ts": 0.0, "data": None, "path": None}


def _load_topics() -> dict[str, Any]:
    now = time.time()
    if _TOPICS_CACHE["data"] is not None and now - _TOPICS_CACHE["ts"] < 3600:
        return _TOPICS_CACHE["data"]
    if not _TOPICS_PATH.exists():
        log.warning("topics.json not found at %s; topic detection disabled.", _TOPICS_PATH)
        _TOPICS_CACHE["data"] = {"topics": {}, "assets": {}}
        _TOPICS_CACHE["ts"] = now
        return _TOPICS_CACHE["data"]
    _TOPICS_CACHE["data"] = json.loads(_TOPICS_PATH.read_text(encoding="utf-8"))
    _TOPICS_CACHE["ts"] = now
    _TOPICS_CACHE["path"] = str(_TOPICS_PATH)
    return _TOPICS_CACHE["data"]


# ----- topic detection (used by web/chat.py for L1 pre-fetch) ------

def detect_topics(text: str, *, max_topics: int = 5) -> list[str]:
    """Return list of topic ids matched by keywords in `text`."""
    if not text:
        return []
    cfg = _load_topics()
    out: list[str] = []
    for topic_id, topic in cfg.get("topics", {}).items():
        for kw in topic.get("keywords", []):
            if kw in text:
                out.append(topic_id)
                break
        if len(out) >= max_topics:
            break
    return out


# ----- snapshot models ---------------------------------------------

@dataclass
class AssetSnapshot:
    code: str
    name: str
    current: float | None = None
    summary_1w: str | None = None
    summary_1m: str | None = None
    recent_1w: list[dict] = field(default_factory=list)   # [{date, close, change_pct}]
    recent_1m_brief: dict | None = None                   # {start_date, end_date, start_close, end_close, change_pct}
    source: str | None = None
    error: str | None = None

    def to_dict(self) -> dict:
        return {
            "code": self.code,
            "name": self.name,
            "current": self.current,
            "summary_1w": self.summary_1w,
            "summary_1m": self.summary_1m,
            "recent_1w": self.recent_1w,
            "recent_1m_brief": self.recent_1m_brief,
            "source": self.source,
            "error": self.error,
        }


# ----- fetchers (one per source type) -------------------------------

def _build_from_close_series(rows: list[tuple[str, float]],
                              code: str, name: str, source: str) -> AssetSnapshot:
    """rows = [(date_str, close_float), ...] sorted ascending."""
    if not rows:
        return AssetSnapshot(code=code, name=name, error="empty series", source=source)
    last_date, last_close = rows[-1]
    snap = AssetSnapshot(code=code, name=name, current=round(last_close, 2), source=source)

    if len(rows) >= 6:
        wk = rows[-6:]
        prev = wk[0][1]
        recent_1w = []
        for d, c in wk[1:]:
            recent_1w.append({"date": d, "close": round(c, 2),
                              "change_pct": round((c/prev - 1)*100, 2)})
            prev = c
        snap.recent_1w = recent_1w
        first_close = wk[0][1]
        snap.summary_1w = (f"{first_close:.2f} → {last_close:.2f} "
                            f"({(last_close/first_close - 1)*100:+.2f}%)")
    else:
        snap.summary_1w = "(数据不足 1 周)"

    if len(rows) >= 22:
        m = rows[-22:]
    elif len(rows) >= 2:
        m = rows
    else:
        m = []
    if m:
        first_close = m[0][1]
        snap.summary_1m = (f"{first_close:.2f} → {last_close:.2f} "
                            f"({(last_close/first_close - 1)*100:+.2f}%)")
        snap.recent_1m_brief = {
            "start_date": m[0][0], "end_date": last_date,
            "start_close": round(first_close, 2), "end_close": round(last_close, 2),
            "change_pct": round((last_close/first_close - 1)*100, 2),
        }

    return snap


def _fetch_ak_a_index(symbol: str, name: str) -> AssetSnapshot:
    import akshare as ak
    df = ak.stock_zh_index_daily(symbol=symbol)
    rows = [(str(r["date"])[:10], float(r["close"])) for _, r in df.tail(30).iterrows()]
    return _build_from_close_series(rows, symbol, name, source="akshare/sina_a_index")


def _fetch_ak_hk_index(symbol: str, name: str) -> AssetSnapshot:
    import akshare as ak
    df = ak.stock_hk_index_daily_em(symbol=symbol)
    rows = [(str(r["date"])[:10], float(r["latest"])) for _, r in df.tail(30).iterrows()]
    return _build_from_close_series(rows, symbol, name, source="akshare/em_hk_index")


def _fetch_ak_us_index(symbol: str, name: str) -> AssetSnapshot:
    import akshare as ak
    df = ak.index_us_stock_sina(symbol=symbol)
    rows = [(str(r["date"])[:10], float(r["close"])) for _, r in df.tail(30).iterrows()]
    return _build_from_close_series(rows, symbol, name, source="akshare/sina_us_index")


def _fetch_tencent_quote(symbol: str, name: str) -> AssetSnapshot:
    """Real-time spot only; for stocks/ETFs/HK indices."""
    url = f"http://qt.gtimg.cn/q={symbol}"
    r = httpx.get(url, timeout=5)
    text = r.content.decode("gbk", errors="replace")
    if '"' not in text or "pv_none_match" in text:
        raise RuntimeError(f"empty/no-match for {symbol}")
    inner = text.split('"', 2)[1]
    fields = inner.split("~")
    name_from_src = fields[1] if len(fields) > 1 else name
    current = float(fields[3]) if len(fields) > 3 and fields[3] else None
    yesterday = float(fields[4]) if len(fields) > 4 and fields[4] else None
    change_pct = None
    if current and yesterday:
        change_pct = round((current/yesterday - 1) * 100, 2)
    return AssetSnapshot(
        code=symbol, name=name_from_src or name, current=current,
        summary_1w="(real-time only; 无 1 周历史)" if change_pct is None else f"今日 {current:.2f}, 较昨日 {change_pct:+.2f}%",
        summary_1m="(real-time only; 无 1 月历史)",
        source="tencent",
    )


def _fetch_tencent_hf(symbol: str, name: str) -> AssetSnapshot:
    """Tencent 'hf_*' format for spot commodities (different from stock format)."""
    url = f"http://qt.gtimg.cn/q={symbol}"
    r = httpx.get(url, timeout=5)
    text = r.content.decode("gbk", errors="replace")
    if '"' not in text or "pv_none_match" in text:
        raise RuntimeError(f"hf empty for {symbol}")
    inner = text.split('"', 2)[1]
    fields = inner.split(",")
    if len(fields) < 7:
        raise RuntimeError(f"hf bad format: {fields[:5]}")
    current = float(fields[0])
    change_pct = float(fields[1])
    high_today = float(fields[4])
    low_today = float(fields[5])
    return AssetSnapshot(
        code=symbol, name=name, current=current,
        summary_1w=f"现价 {current:.2f}，今日涨跌 {change_pct:+.2f}% (高 {high_today:.2f} / 低 {low_today:.2f})",
        summary_1m="(spot only)",
        source="tencent_hf",
    )


_FETCHERS = {
    "ak_a_index":     _fetch_ak_a_index,
    "ak_hk_index":    _fetch_ak_hk_index,
    "ak_us_index":    _fetch_ak_us_index,
    "tencent_quote":  _fetch_tencent_quote,
    "tencent_hf":     _fetch_tencent_hf,
}


def _fetch_asset(asset_id: str) -> Optional[AssetSnapshot]:
    cfg = _load_topics()
    asset_def = cfg.get("assets", {}).get(asset_id)
    if not asset_def:
        return None

    name = asset_def["name"]
    type_ = asset_def["type"]
    primary_symbol = asset_def["primary"]
    backup_tencent = asset_def.get("backup_tencent")

    fetcher = _FETCHERS.get(type_)
    if fetcher is None:
        log.warning("unknown asset type %r for %s", type_, asset_id)
        return AssetSnapshot(code=primary_symbol, name=name, error=f"unknown type {type_}")

    try:
        return fetcher(primary_symbol, name)
    except Exception as e:
        log.warning("primary fetch failed for %s (%s): %s", asset_id, type_, e)

    if backup_tencent:
        try:
            return _fetch_tencent_quote(backup_tencent, name)
        except Exception as e:
            log.warning("tencent backup also failed for %s: %s", asset_id, e)

    return AssetSnapshot(code=primary_symbol, name=name, error="all sources failed")


# ----- public API ---------------------------------------------------

_CONTEXT_CACHE: dict[tuple[str, ...], tuple[float, dict]] = {}
CONTEXT_TTL_S = 600   # 10 min


def get_market_context(topics: list[str]) -> dict:
    """Fetch each topic's assets. Best-effort, with per-asset fallback.

    Returns:
        {
          "ok": True,
          "fetched_at": "...",
          "topics": {
            "<topic_id>": {
              "label": "...",
              "items": [<asset snapshot dict>, ...]
            },
            ...
          }
        }
    """
    if not topics:
        return {"ok": True, "topics": {}, "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}

    cache_key = tuple(sorted(set(topics)))
    cached = _CONTEXT_CACHE.get(cache_key)
    if cached and time.time() - cached[0] < CONTEXT_TTL_S:
        return cached[1]

    cfg = _load_topics()
    out_topics: dict[str, Any] = {}
    for topic_id in cache_key:
        topic_def = cfg.get("topics", {}).get(topic_id)
        if not topic_def:
            continue
        items = []
        for asset_id in topic_def.get("assets", []):
            snap = _fetch_asset(asset_id)
            if snap is not None:
                items.append(snap.to_dict())
        out_topics[topic_id] = {
            "label": topic_def.get("label", topic_id),
            "items": items,
        }

    result = {
        "ok": True,
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "topics": out_topics,
    }
    _CONTEXT_CACHE[cache_key] = (time.time(), result)
    return result


def format_market_context_for_prompt(ctx: dict) -> str:
    """Convert context dict into a compact markdown blob to embed in the system prompt."""
    if not ctx.get("ok") or not ctx.get("topics"):
        return ""
    lines = ["## 市场环境（系统已自动采集，回答时如用得上请自然引用）", ""]
    for tid, t in ctx["topics"].items():
        lines.append(f"### {t['label']}")
        for it in t["items"]:
            line = f"- **{it['name']}**"
            if it.get("current") is not None:
                line += f" 现价 {it['current']:.2f}"
            if it.get("summary_1w"):
                line += f" · 近1周 {it['summary_1w']}"
            if it.get("summary_1m") and "无 1 月" not in it["summary_1m"] and "spot only" not in it["summary_1m"]:
                line += f" · 近1月 {it['summary_1m']}"
            if it.get("error"):
                line += f" · ⚠ {it['error']}"
            lines.append(line)
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"
