"""Rewrite external citation URLs to the local zhihu archive site.

Why: the bigv-blogger MCP returns `url` fields pointing at the source
(zhihu.com posts, berkshirehathaway.com letters, xueqiu.com translations,
buffett.cnbc.com videos). These external links can break:
  - 知乎博主删档 → the answer/article 404s
  - CNBC 视频改版 → URL changes
  - 雪球 / 第三方翻译 → may delete

The user runs his **own** zhihu archive site on `http://8.155.174.112:8000/`
which has:
  - `/content/{id}` for every zhihu post (snapshot kept even if author deletes)
  - `/masters/buffett/letter/{year}` for letters (markdown rendered)
  - `/masters/buffett/meeting/{year}/{filename}` for meetings (Chinese 译稿)

This module rewrites outbound MCP URLs to point at that local archive,
falling back to the original external URL when the local equivalent isn't
findable (e.g. a brand-new zhihu post not yet in the archive's content table).

## Lookup table

- `_zhihu_id_to_cid`: dict[zhihu_id_str → contents.id] —— built once from
  zhihu.db at first call, refreshed lazily on miss

(buffett meeting URLs use a short hash-based redirect at the zhihu side,
so we don't need a filename lookup here — see `_buffett_local_url` below.)

Thread-safe enough for our scale (MCP server is single-process; small
race-window on the first-call init isn't a problem).
"""

from __future__ import annotations

import logging
import re
import sqlite3

from .config import settings

log = logging.getLogger("bigv_twins.archive_url")


# Where the archive site is served. Hard-coded for now (matches deploy.sh).
# If you ever move the site, change this and restart blogger MCP service.
ARCHIVE_BASE = "http://8.155.174.112:8000"


_zhihu_id_to_cid: dict[str, int] | None = None


def _open_zhihu_ro() -> sqlite3.Connection:
    uri = f"file:{settings.zhihu_db_path}?mode=ro&immutable=1"
    return sqlite3.connect(uri, uri=True)


def _load_zhihu_lookup() -> None:
    """One-shot bulk load of zhihu_id → contents.id from zhihu.db."""
    global _zhihu_id_to_cid
    try:
        with _open_zhihu_ro() as c:
            _zhihu_id_to_cid = {
                row[1]: row[0]
                for row in c.execute(
                    "SELECT id, zhihu_id FROM contents WHERE zhihu_id IS NOT NULL"
                )
            }
        log.info("archive_url: zhihu lookup loaded %d entries", len(_zhihu_id_to_cid))
    except Exception:
        log.exception("archive_url: zhihu lookup load failed")
        _zhihu_id_to_cid = {}


def _zhihu_local_url(zhihu_id: str) -> str | None:
    """Look up zhihu_id → /content/{cid}. Lazy refresh on miss for new posts."""
    global _zhihu_id_to_cid
    if _zhihu_id_to_cid is None:
        _load_zhihu_lookup()
    cid = _zhihu_id_to_cid.get(zhihu_id)
    if cid is not None:
        return f"{ARCHIVE_BASE}/content/{cid}"
    # Maybe it's a brand-new post added since startup — live query
    try:
        with _open_zhihu_ro() as c:
            row = c.execute(
                "SELECT id FROM contents WHERE zhihu_id = ?", (zhihu_id,)
            ).fetchone()
        if row:
            _zhihu_id_to_cid[zhihu_id] = row[0]
            return f"{ARCHIVE_BASE}/content/{row[0]}"
    except Exception:
        log.exception("archive_url: zhihu live lookup failed for %s", zhihu_id)
    return None


_LETTER_PAT = re.compile(r"^letter-(\d{4})-")
_MEETING_PAT = re.compile(r"^meeting-(\d{4})-([a-f0-9]{8})-")


def _buffett_local_url(source_id: str, content_type: str) -> str | None:
    """Map a buffett source_id + content_type to a /masters/buffett/... URL.

    For meetings we emit a **short hash-based URL** (``/m/{year}/{hash8}``)
    instead of ``/meeting/{year}/{utf-8-filename}``. Reason: LLM streams URLs
    one token at a time; URL-encoded Chinese filenames sometimes get one or
    two bytes dropped/substituted in the model output, producing 404s.
    ASCII hash URLs are token-safe. The zhihu archive site has a redirect
    route at ``/masters/buffett/m/{year}/{hash}`` that 302s to the full
    filename URL.

    For letters we emit ``/letter/{year}`` directly — that URL has no Chinese,
    no risk of corruption.
    """
    if content_type == "letter":
        m = _LETTER_PAT.match(source_id)
        if m:
            return f"{ARCHIVE_BASE}/masters/buffett/letter/{m.group(1)}"
    elif content_type == "meeting":
        m = _MEETING_PAT.match(source_id)
        if m:
            year, fh = m.group(1), m.group(2)
            # Use hash-based short URL — zhihu's masters router redirects to
            # the full filename. We don't even need to look up the file here.
            return f"{ARCHIVE_BASE}/masters/buffett/m/{year}/{fh}"
    return None


def to_archive_url(
    blogger_slug: str,
    source_id: str | None,
    content_type: str | None,
    original_url: str | None,
) -> str | None:
    """Rewrite a chunk's URL to the local archive site if possible.

    Returns:
      - Local archive URL when we can resolve one (preferred)
      - Otherwise `original_url` (so agents still get a usable link)
      - Or None if both unavailable
    """
    if not source_id:
        return original_url
    # Buffett (and any future master) — source_id has letter-/meeting- prefix
    if source_id.startswith("letter-") or source_id.startswith("meeting-"):
        if blogger_slug == "buffett":
            local = _buffett_local_url(source_id, content_type or "")
            if local:
                return local
        return original_url
    # Regular zhihu blogger: source_id IS the zhihu post id
    local = _zhihu_local_url(source_id)
    return local or original_url
