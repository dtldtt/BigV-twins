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

## Lookup tables

- `_zhihu_id_to_cid`: dict[zhihu_id_str → contents.id] —— built once from
  zhihu.db at first call, refreshed lazily on miss
- `_buffett_hash_to_file`: dict[filehash8 → (sub_dir, year, filename)] —— built
  once from filesystem at first call (~150 files)

Thread-safe enough for our scale (MCP server is single-process; small
race-window on the first-call init isn't a problem).
"""

from __future__ import annotations

import hashlib
import logging
import re
import sqlite3
from pathlib import Path
from urllib.parse import quote

from .config import settings

log = logging.getLogger("bigv_twins.archive_url")


# Where the archive site is served. Hard-coded for now (matches deploy.sh).
# If you ever move the site, change this and restart blogger MCP service.
ARCHIVE_BASE = "http://8.155.174.112:8000"


# Where the Buffett markdown lives on disk (used to map filehash → filename).
MASTERS_DIR = Path("/home/dtl/projects/zhihu/data/masters")


_zhihu_id_to_cid: dict[str, int] | None = None
_buffett_hash_to_file: dict[str, tuple[str, str, str]] | None = None


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


def _load_buffett_hash_lookup() -> None:
    """Build filehash8 → (sub, year, filename) by walking the masters tree."""
    global _buffett_hash_to_file
    out: dict[str, tuple[str, str, str]] = {}
    for sub in ("BRK-Annual-Meeting", "BRK-Annual-Meeting-Supplement"):
        base = MASTERS_DIR / "buffett" / sub
        if not base.exists():
            continue
        for year_dir in base.iterdir():
            if not year_dir.is_dir() or not year_dir.name.isdigit():
                continue
            for f in year_dir.glob("*.md"):
                if f.name == "README.md":
                    continue
                fh = hashlib.sha1(f.name.encode()).hexdigest()[:8]
                out[fh] = (sub, year_dir.name, f.name)
    _buffett_hash_to_file = out
    log.info("archive_url: buffett filehash lookup loaded %d files", len(out))


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
    """Map a buffett source_id + content_type to a /masters/buffett/... URL."""
    if content_type == "letter":
        m = _LETTER_PAT.match(source_id)
        if m:
            return f"{ARCHIVE_BASE}/masters/buffett/letter/{m.group(1)}"
    elif content_type == "meeting":
        m = _MEETING_PAT.match(source_id)
        if m:
            year, fh = m.group(1), m.group(2)
            global _buffett_hash_to_file
            if _buffett_hash_to_file is None:
                _load_buffett_hash_lookup()
            entry = _buffett_hash_to_file.get(fh)
            if entry:
                sub, _y, fname = entry
                suffix = "?supplement=true" if "Supplement" in sub else ""
                return (
                    f"{ARCHIVE_BASE}/masters/buffett/meeting/{year}/"
                    f"{quote(fname)}{suffix}"
                )
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
