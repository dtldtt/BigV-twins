"""Generic web search via Bing CN (https://cn.bing.com).

Layer 2 of the BigV-twins data architecture:
- Layer 1 is structured MCP tools (get_stock_snapshot / get_dividend_history / ...)
- Layer 2 (this) is general-purpose web search returning title+snippet+url lists
- Layer 3 is agent-browser (skill, in advisor workspace) for deep page fetching

Quirks we've observed with Bing CN:
- The `site:` operator is **silently ignored** for Chinese queries — we don't use it
- Quoted phrases ("...") don't reliably force phrase match either
- Short queries like "中国平安 财报" get hijacked by generic-term collisions
  ("中国" matches gov.cn, baike, news.cn) and return junk fallback pages
- Specific queries work fine — ticker code instead of name (`601318 财报` ✓),
  year glued to metric (`2025年报` ✓), or topical keywords (`分红`, `估值`)

What this module does:
1. Submits the raw query to Bing CN
2. Post-filters known junk domains (百科 / 政府门户 / 党媒) from results
3. If post-filter leaves ≥ 3 results → return them
4. Otherwise → empty results + a `note` field telling the agent to retry
   with a better-formed query (ticker / year-specific)

Usage:
    from bigv_twins.web_search import web_search
    res = web_search("601318 2025年报", top_k=5)
"""

from __future__ import annotations

import logging
import time
from typing import Any
from urllib.parse import quote_plus, urlparse

import httpx
from bs4 import BeautifulSoup

log = logging.getLogger("bigv_twins.web_search")


# Domains we silently drop from results — these consistently show up as Bing CN's
# fallback "your query made no sense, here's some Chinese institutional pages"
# noise when the query is too generic. None of them carry useful financial data.
_JUNK_HOSTS: tuple[str, ...] = (
    # Baidu encyclopedia + generic government / state-media — Bing CN consistently
    # falls back to these when query is too generic (matches "中国" / "贵州" / etc.
    # as a sub-token). None carry useful financial data; they're noise for our use.
    "baike.baidu.com",
    "wapbaike.baidu.com",
    "www.gov.cn",
    "english.www.gov.cn",
    "www.fmprc.gov.cn",
    "www.people.com.cn",
    "en.people.com.cn",
    "www.news.cn",
    "www.xinhuanet.com",
    "www.qstheory.cn",
    "www.china.com.cn",
    "www.chinanews.com.cn",
    "news.cctv.com",
    "www.cctv.com",
    "cn.chinadaily.com.cn",
    "www.chinadaily.com.cn",
    "news.qq.com",          # 腾讯新闻 — too generic, often state-rewrite content
)

import re as _re

_TICKER_RE = _re.compile(
    r"\b(?:"
    r"6\d{5}"             # 沪市主板/科创板（600xxx, 601xxx, 688xxx ...）
    r"|0\d{5}"            # 深市主板/中小（000xxx, 002xxx, 003xxx）
    r"|3\d{5}"            # 创业板（300xxx）
    r"|8\d{5}"            # 北交所（830xxx, 832xxx ...）
    r"|4\d{5}"            # 北交所老（430xxx, 836xxx 等）
    r"|0?\d{4,5}\.HK"     # 港股（00700.HK 之类）
    r")\b"
)


_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
_BING_BASE = "https://cn.bing.com/search"
_CACHE_TTL_S = 600  # 10 minutes
_CACHE: dict[tuple, tuple[float, dict]] = {}


def _is_junk(hit: dict) -> bool:
    """True if this Bing hit is from a domain we want to silently filter out."""
    domain = (hit.get("source") or "").lower()
    for junk in _JUNK_HOSTS:
        if junk in domain:
            return True
    return False


def _bing_serp_get(query: str, *, top_k: int, timeout: float = 10.0) -> list[dict]:
    """Single Bing CN SERP fetch + BS4 parse. Returns the raw hit list (pre-filter)."""
    url = f"{_BING_BASE}?q={quote_plus(query)}&setlang=zh-CN&cc=CN"
    try:
        r = httpx.get(
            url,
            headers={"User-Agent": _USER_AGENT, "Accept-Language": "zh-CN,zh;q=0.9"},
            timeout=timeout,
            follow_redirects=True,
        )
    except Exception as e:
        log.warning("bing fetch failed for %r: %s", query, e)
        return []
    if r.status_code != 200:
        log.warning("bing returned %d for %r", r.status_code, query)
        return []

    soup = BeautifulSoup(r.text, "html.parser")
    hits: list[dict] = []
    # Over-pull a bit so post-filter still leaves enough; cap at 20.
    for li in soup.select("li.b_algo")[: max(top_k * 3, 10)]:
        h2 = li.select_one("h2 a")
        if not h2:
            continue
        href = (h2.get("href") or "").strip()
        if not href.startswith("http"):
            continue
        title = h2.get_text(strip=True)
        snippet_el = (
            li.select_one(".b_caption p")
            or li.select_one(".b_lineclamp4")
            or li.select_one(".b_lineclamp2")
            or li
        )
        snippet = snippet_el.get_text(" ", strip=True).replace("翻译此结果·", "").strip()
        # Domain — prefer cite element (cleaner), fall back to URL parse
        cite_el = li.select_one("cite")
        domain = (
            cite_el.get_text(strip=True).split(" › ")[0]
            if cite_el
            else urlparse(href).netloc
        )
        hits.append({
            "title": title[:200],
            "url": href,
            "snippet": snippet[:500],
            "source": domain,
        })
    return hits


def web_search(
    query: str,
    *,
    top_k: int = 5,
) -> dict[str, Any]:
    """Public entry. Returns ``{ok, query, results, source, note?}``.

    - 10-minute cache keyed by (query, top_k)
    - Post-filters known junk domains (百科 / 政府门户 / 党媒)
    - If filtered results < 3 → return empty with ``note`` asking agent to retry
      with a better query (ticker / year-specific keywords)
    """
    cache_key = (query.strip(), top_k)
    cached = _CACHE.get(cache_key)
    if cached and time.time() - cached[0] < _CACHE_TTL_S:
        return cached[1]

    raw = _bing_serp_get(query, top_k=top_k)
    filtered = [h for h in raw if not _is_junk(h)]

    # Smarter signal: if query has a ticker code but NO result mentions it in
    # title/url/snippet, Bing returned an unrelated set (very common Bing CN
    # quirk when query is "中国平安 XX" — Bing decides to show 中国 generic news).
    tickers_in_query = _TICKER_RE.findall(query)
    if tickers_in_query:
        ticker_match = lambda h: any(  # noqa: E731
            t in (h.get("title", "") + " " + h.get("url", "") + " " + h.get("snippet", ""))
            for t in tickers_in_query
        )
        filtered = [h for h in filtered if ticker_match(h)]

    filtered = filtered[:top_k]

    result: dict[str, Any] = {
        "ok": True,
        "query": query,
        "results": filtered,
        "source": "cn.bing.com",
    }

    if len(filtered) < 3:
        # Either junk filter dropped most, or ticker check found no on-topic hits.
        # Tell the agent how to fix instead of letting it loop on bad results.
        if tickers_in_query and len(raw) > len(filtered):
            result["note"] = (
                f"搜索返回的结果与 ticker {tickers_in_query} 无关——Bing CN 把 query 误解了。"
                "试试改写：(a) 把 ticker 放到 query 最前面 / 加引号 / 加更多具体关键词，"
                "或 (b) 把 ticker 换成股名后再加具体年份指标（如「中国平安 2025年报」）。"
            )
        else:
            result["note"] = (
                "搜索结果质量不佳（多为通用站如百科 / 政府网等被过滤）。"
                "Bing CN 对短查询容易被「中国 / 贵州」等大词带偏。"
                "建议：(a) 用股票代码 ticker（如 601318 替代「中国平安」），"
                "或 (b) 加具体年份且紧贴指标（如「2025年报」连写、不要中间加空格）。"
            )

    _CACHE[cache_key] = (time.time(), result)
    return result
