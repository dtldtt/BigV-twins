"""Best-effort fetch of ETF prospectus dividend policy section.

Strategy:
1. Hit eastmoney JJGG API to find latest 招募说明书 announcement
2. Download PDF (cached on disk by ID)
3. Use pypdf to extract text
4. Find "收益分配" or "分红原则" section, return ~500-char snippet
5. Returns None on any failure — caller falls back to historical-only display
"""
from __future__ import annotations

import logging
import os
import re
from pathlib import Path

import httpx

log = logging.getLogger("bigv_twins.stock_data.etf_prospectus")

_CACHE_DIR = Path("/tmp/bigv_etf_prospectus")
_CACHE_DIR.mkdir(parents=True, exist_ok=True)

# In-process cache of parsed snippets
_SNIPPET_CACHE: dict[str, dict] = {}


def _find_latest_prospectus_id(code: str) -> str | None:
    """Query eastmoney JJGG API for the latest 招募说明书 announcement."""
    try:
        r = httpx.get(
            "https://api.fund.eastmoney.com/f10/JJGG",
            params={"fundcode": code, "pageIndex": 1, "pageSize": 30, "type": 1},
            headers={"User-Agent": "Mozilla/5.0", "Referer": "https://fundf10.eastmoney.com/"},
            timeout=8,
        )
        data = r.json()
    except Exception as e:
        log.warning("JJGG API failed for %s: %s", code, e)
        return None

    if not data.get("Data"):
        return None
    for item in data["Data"]:
        title = item.get("TITLE", "")
        if "招募说明书" in title and "更新" in title:
            return item.get("ID")
    # Fallback: any 招募说明书
    for item in data["Data"]:
        if "招募说明书" in item.get("TITLE", ""):
            return item.get("ID")
    return None


def _download_pdf(ann_id: str) -> Path | None:
    """Download PDF (with disk cache)."""
    cache_path = _CACHE_DIR / f"{ann_id}.pdf"
    if cache_path.exists() and cache_path.stat().st_size > 1000:
        return cache_path
    try:
        r = httpx.get(
            f"http://pdf.dfcfw.com/pdf/H2_{ann_id}_1.pdf",
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=20,
            follow_redirects=True,
        )
        if r.status_code == 200 and len(r.content) > 1000:
            cache_path.write_bytes(r.content)
            return cache_path
    except Exception as e:
        log.warning("PDF download failed for %s: %s", ann_id, e)
    return None


def _extract_distribution_snippet(pdf_path: Path) -> str | None:
    """Extract the 收益分配 / 分红原则 section from PDF text."""
    try:
        import pypdf
        reader = pypdf.PdfReader(str(pdf_path))
    except Exception as e:
        log.warning("pypdf failed for %s: %s", pdf_path, e)
        return None

    full_text = ""
    for p in reader.pages:
        try:
            full_text += p.extract_text() + "\n"
        except Exception:
            continue
    if len(full_text) < 500:
        return None

    # Look for the dividend section. Patterns observed in real prospectuses:
    #   "十二、基金的收益分配"  / "（一）收益分配原则"  / "基金收益分配原则"
    patterns = [
        r"基金\s*收益分配\s*原则",
        r"收益分配\s*原则",
        r"基金的\s*收益\s*分配",
        r"收益分配\s*方式",
        r"分红\s*条件",
        r"分红\s*原则",
    ]
    for pat in patterns:
        for m in re.finditer(pat, full_text):
            start = m.start()
            # Take ~800 chars after the heading
            snippet = full_text[start:start + 800]
            # Clean up: collapse whitespace, strip page numbers
            snippet = re.sub(r"\s+", " ", snippet)
            # Truncate at next major heading (e.g. "（二）" "二、" "十三、")
            cut_match = re.search(r"\s*[一二三四五六七八九十]+[、.]\s*", snippet[100:])
            if cut_match:
                snippet = snippet[:100 + cut_match.start()]
            snippet = snippet.strip()
            if len(snippet) > 50:
                return snippet
    return None


def fetch_etf_distribution_policy(code: str) -> dict | None:
    """Best-effort fetch ETF dividend distribution policy from prospectus.

    Returns {"snippet": "...", "source": "招募说明书", "ann_id": "..."} or None.
    """
    if code in _SNIPPET_CACHE:
        return _SNIPPET_CACHE[code]

    ann_id = _find_latest_prospectus_id(code)
    if not ann_id:
        _SNIPPET_CACHE[code] = None
        return None

    pdf_path = _download_pdf(ann_id)
    if not pdf_path:
        _SNIPPET_CACHE[code] = None
        return None

    snippet = _extract_distribution_snippet(pdf_path)
    if not snippet:
        _SNIPPET_CACHE[code] = None
        return None

    result = {
        "snippet": snippet,
        "source": "基金招募说明书",
        "ann_id": ann_id,
        "pdf_url": f"http://pdf.dfcfw.com/pdf/H2_{ann_id}_1.pdf",
    }
    _SNIPPET_CACHE[code] = result
    return result
