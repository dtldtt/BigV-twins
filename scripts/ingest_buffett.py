"""Build twins/buffett.db from the BuffettLetters markdown corpus.

Designed to run on a beefy "build" host (highper) that has both:
  - the markdown source at ``--data-dir``
    (default: /home/dtl/projects/data/BuffettLetters)
  - the bge-m3 model cached locally
The resulting ``twins/buffett.db`` is then rsynced to the serving host.

The corpus has two distinct content types:

  1. **Letters** (``letters/<year>.md``, 1977-2024, English):
     Clean markdown with ``#`` / ``##`` section headers. We split on those
     headers; each section becomes one or more chunks (sliding-window if
     the section body is long).
     content_type = 'letter'.

  2. **Annual Meeting Q&A** (``BRK-Annual-Meeting/<year>/*.md``,
     1994-current, Chinese-translated by 一朵喵):
     Each file is one half-day session (上午/下午, sometimes split further
     上/中/下). The body is structured as numbered Q&A blocks like
     ``### 1、明年需要更大的会议场地``. We split on ``###`` headers and
     use each numbered block as a chunk (or sub-split if very long).
     content_type = 'meeting'.

Schema reuses the standard chunks/chunks_vec tables (same as zhihu twins).
The ``zhihu_id`` column is repurposed as a unique source ID:
  - letters: ``letter-<year>-<section_idx>``
  - meetings: ``meeting-<year>-<filename_hash8>-<qa_idx>``

The ``meta`` table records the embedding model + dim, same guard as the
zhihu indexer (`src/bigv_twins/index.py`).
"""

from __future__ import annotations

import argparse
import hashlib
import logging
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

import sqlite_vec
from tqdm import tqdm

# Allow `python scripts/ingest_buffett.py` from project root
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from bigv_twins.chunk import split_markdown_sections, chunk_text  # noqa: E402
from bigv_twins.config import settings, BY_SLUG  # noqa: E402
from bigv_twins.embed import Embedder  # noqa: E402
from bigv_twins.index import _open_twin_rw  # noqa: E402

log = logging.getLogger("bigv_twins.ingest_buffett")


# ---------------------------------------------------------------- helpers


def _short_hash(s: str, n: int = 8) -> str:
    return hashlib.sha1(s.encode("utf-8")).hexdigest()[:n]


_LETTER_SOURCE_RE = re.compile(r"Source:\s*(https?://\S+)")


def _strip_letter_frontmatter(md: str) -> tuple[str, str | None]:
    """Letters start with a 4-line preamble:

        # Berkshire Hathaway Shareholder Letter — 1977
        Source: https://www.berkshirehathaway.com/letters/1977.html
        ---
        **BERKSHIRE HATHAWAY INC.**

    We strip the frontmatter and capture the source URL.
    """
    url_match = _LETTER_SOURCE_RE.search(md[:500])
    url = url_match.group(1) if url_match else None
    # Drop everything up to and including the "---" separator if present;
    # otherwise drop just the source-line area.
    parts = md.split("\n---\n", 1)
    body = parts[1] if len(parts) == 2 else md
    return body.strip(), url


_MEETING_URL_RE = re.compile(r"URL[：:]\s*(https?://\S+)")
_MEETING_BODY_MARK = re.compile(r"-{2,}正文-{2,}")


def _strip_meeting_frontmatter(md: str) -> tuple[str, str | None]:
    """Meeting Q&A files have a translator-credit preamble ending with:

        --------正文--------

    Capture the CNBC URL near the top, then keep only what follows the
    ``--------正文--------`` marker. Files without the marker get returned
    as-is (rare, defensive).
    """
    url_match = _MEETING_URL_RE.search(md[:1500])
    url = url_match.group(1) if url_match else None
    m = _MEETING_BODY_MARK.search(md)
    body = md[m.end() :] if m else md
    return body.strip(), url


# ---------------------------------------------------------------- ingest


def _insert_chunk(
    conn: sqlite3.Connection,
    *,
    source_id: str,
    chunk_index: int,
    content_type: str,
    title: str,
    text: str,
    url: str | None,
    column_title: str,
    created_time: str,
    embedding,
) -> None:
    cur = conn.execute(
        "INSERT INTO chunks (zhihu_id, chunk_index, content_type, title, text, "
        "voteup_count, comment_count, url, column_title, created_time, updated_time) "
        "VALUES (?,?,?,?,?,0,0,?,?,?,NULL)",
        (source_id, chunk_index, content_type, title, text, url, column_title, created_time),
    )
    conn.execute(
        "INSERT INTO chunks_vec(rowid, embedding) VALUES (?, ?)",
        (cur.lastrowid, sqlite_vec.serialize_float32(embedding.tolist())),
    )


def ingest_letters(
    data_dir: Path,
    dst: sqlite3.Connection,
    embedder: Embedder,
    *,
    year_range: tuple[int, int] | None = None,
) -> int:
    """Process ``letters/<year>.md`` files. Returns chunks added."""
    letters_dir = data_dir / "letters"
    if not letters_dir.exists():
        log.warning("letters dir not found: %s", letters_dir)
        return 0

    paths = sorted(letters_dir.glob("*.md"))
    if year_range:
        lo, hi = year_range
        paths = [p for p in paths if p.stem.isdigit() and lo <= int(p.stem) <= hi]

    chunks_added = 0
    for path in tqdm(paths, desc="letters", unit="letter"):
        try:
            year = int(path.stem)
        except ValueError:
            log.warning("skipping non-year letter file: %s", path.name)
            continue

        md = path.read_text(encoding="utf-8")
        body, url = _strip_letter_frontmatter(md)

        # Letters use # / ## / #### headers irregularly; split on the top
        # two levels which give the natural section boundaries.
        sections = split_markdown_sections(
            body,
            chunk_size=settings.chunk_size,
            chunk_overlap=settings.chunk_overlap,
            min_level=1,
            max_level=2,
        )
        if not sections:
            continue

        # Embed all chunks of this letter in one pass for speed.
        all_chunks = [(s, c) for s in sections for c in s.chunks]
        if not all_chunks:
            continue
        embeddings = embedder.encode_passages([c.text for _s, c in all_chunks])

        for idx, ((sec, chunk), emb) in enumerate(zip(all_chunks, embeddings)):
            source_id = f"letter-{year}-{idx}"
            title = f"{year} 致股东信 — {sec.title}"
            _insert_chunk(
                dst,
                source_id=source_id,
                chunk_index=0,
                content_type="letter",
                title=title,
                text=chunk.text,
                url=url,
                column_title="致股东信",
                created_time=f"{year}-01-01",
                embedding=emb,
            )
            chunks_added += 1

        dst.commit()

    return chunks_added


# Match `### 1、xxx` numbered Q&A headers (Chinese full-width comma; meetings
# also occasionally use ``###1、`` without space — handle both).
_MEETING_QA_HEADER = re.compile(r"(?m)^###\s*(\d+)、\s*(.+?)\s*$")


def ingest_meetings(
    data_dir: Path,
    dst: sqlite3.Connection,
    embedder: Embedder,
    *,
    year_range: tuple[int, int] | None = None,
) -> int:
    """Process all annual-meeting Q&A markdown. Returns chunks added.

    Includes both BRK-Annual-Meeting/ and BRK-Annual-Meeting-Supplement/.
    """
    chunks_added = 0
    roots = [data_dir / "BRK-Annual-Meeting", data_dir / "BRK-Annual-Meeting-Supplement"]

    for root in roots:
        if not root.exists():
            continue
        for year_dir in sorted(root.iterdir()):
            if not year_dir.is_dir() or not year_dir.name.isdigit():
                continue
            year = int(year_dir.name)
            if year_range and not (year_range[0] <= year <= year_range[1]):
                continue
            for path in sorted(year_dir.glob("*.md")):
                if path.name == "README.md":
                    continue
                chunks_added += _ingest_one_meeting_file(
                    path, year, dst, embedder, group=root.name
                )
                dst.commit()
    return chunks_added


def _ingest_one_meeting_file(
    path: Path,
    year: int,
    dst: sqlite3.Connection,
    embedder: Embedder,
    *,
    group: str,
) -> int:
    md = path.read_text(encoding="utf-8")
    body, url = _strip_meeting_frontmatter(md)
    if not body:
        return 0

    # Find Q&A boundaries
    matches = list(_MEETING_QA_HEADER.finditer(body))
    if not matches:
        # No numbered Q sections — fall back to plain paragraph chunking
        chunks = chunk_text(body, size=settings.chunk_size, overlap=settings.chunk_overlap)
        if not chunks:
            return 0
        embeddings = embedder.encode_passages([c.text for c in chunks])
        for c, emb in zip(chunks, embeddings):
            source_id = f"meeting-{year}-{_short_hash(path.name)}-c{c.chunk_index}"
            _insert_chunk(
                dst,
                source_id=source_id,
                chunk_index=c.chunk_index,
                content_type="meeting",
                title=f"{year} 股东会 — {path.stem}",
                text=c.text,
                url=url,
                column_title=group,
                created_time=f"{year}-01-01",
                embedding=emb,
            )
        return len(chunks)

    file_hash = _short_hash(path.name)
    qa_blocks: list[tuple[int, str, str]] = []  # (qa_num, qa_title, qa_body)
    for i, m in enumerate(matches):
        qa_num = int(m.group(1))
        qa_title = m.group(2).strip()
        qa_body_start = m.end()
        qa_body_end = matches[i + 1].start() if i + 1 < len(matches) else len(body)
        qa_body = body[qa_body_start:qa_body_end].strip()
        if qa_body:
            qa_blocks.append((qa_num, qa_title, qa_body))

    if not qa_blocks:
        return 0

    # Build chunk list — each Q&A block produces one or more chunks. We use a
    # global ``seq`` counter (monotonic within this file) rather than the
    # parsed ``qa_num`` to guarantee uniqueness in the source_id, in case the
    # source markdown has duplicated `### N、` numbering somewhere.
    chunk_records: list[tuple[int, int, str, str, int]] = []
    # tuple = (seq, qa_num, qa_title, text, sub_idx)
    seq = 0
    for qa_num, qa_title, qa_body in qa_blocks:
        sub_chunks = chunk_text(
            qa_body, size=settings.chunk_size, overlap=settings.chunk_overlap
        )
        for c in sub_chunks:
            chunk_records.append((seq, qa_num, qa_title, c.text, c.chunk_index))
            seq += 1

    if not chunk_records:
        return 0

    embeddings = embedder.encode_passages([t for _s, _n, _ti, t, _i in chunk_records])
    for (s, qa_num, qa_title, text, sub_idx), emb in zip(chunk_records, embeddings):
        # source_id is unique-by-construction (seq counter); chunk_index is
        # the sub-chunk index inside the Q&A block, useful for ordering.
        source_id = f"meeting-{year}-{file_hash}-s{s:04d}"
        title = f"{year} 股东会 Q{qa_num} — {qa_title}"
        _insert_chunk(
            dst,
            source_id=source_id,
            chunk_index=sub_idx,
            content_type="meeting",
            title=title,
            text=text,
            url=url,
            column_title=group,
            created_time=f"{year}-01-01",
            embedding=emb,
        )
    return len(chunk_records)


# ---------------------------------------------------------------- main


def main():
    parser = argparse.ArgumentParser(description="Build twins/buffett.db from BuffettLetters")
    parser.add_argument(
        "--data-dir",
        default="/home/dtl/projects/data/BuffettLetters",
        help="Path to the BuffettLetters checkout",
    )
    parser.add_argument(
        "--rebuild", action="store_true",
        help="DROP existing chunks and re-ingest from scratch",
    )
    parser.add_argument(
        "--letters-only", action="store_true",
        help="Skip annual-meeting Q&A (debugging)",
    )
    parser.add_argument(
        "--meetings-only", action="store_true",
        help="Skip letters (debugging)",
    )
    parser.add_argument(
        "--year-from", type=int, default=None,
        help="Lower bound on year (inclusive)",
    )
    parser.add_argument(
        "--year-to", type=int, default=None,
        help="Upper bound on year (inclusive)",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    if "buffett" not in BY_SLUG:
        raise SystemExit(
            "buffett not in bloggers.json — add it before running this ingester"
        )
    blogger = BY_SLUG["buffett"]

    log.info("loading embedder: %s", settings.embedding_model)
    embedder = Embedder(settings.embedding_model)
    log.info("embedder ready (dim=%d)", embedder.dim)

    data_dir = Path(args.data_dir).resolve()
    if not data_dir.exists():
        raise SystemExit(f"--data-dir does not exist: {data_dir}")

    year_range = None
    if args.year_from is not None or args.year_to is not None:
        year_range = (args.year_from or 1900, args.year_to or 9999)

    dst = _open_twin_rw(blogger.slug, embedder, rebuild=args.rebuild)
    try:
        added = 0
        if not args.meetings_only:
            log.info("ingesting letters from %s/letters", data_dir)
            added += ingest_letters(data_dir, dst, embedder, year_range=year_range)
        if not args.letters_only:
            log.info("ingesting meetings from %s/BRK-Annual-Meeting{,_Supplement}", data_dir)
            added += ingest_meetings(data_dir, dst, embedder, year_range=year_range)

        dst.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
            ("last_indexed_at", datetime.now(timezone.utc).isoformat()),
        )
        dst.commit()
        log.info("done: %d chunks added to twins/buffett.db", added)
    finally:
        dst.close()


if __name__ == "__main__":
    main()
