"""Build twins/<slug>.db for a master from MasterCorpus/<slug>/markdown/*.md.

Generic ingest for masters whose corpus is markdown files (books + speeches),
i.e. munger, graham, lynch. Buffett uses a separate ingest_buffett.py because
it has a more structured corpus (letters by year + Q&A by year×session).

Designed to run on a beefy "build" host (highper) that has the bge-m3 model
cached locally. The resulting twins/<slug>.db is then rsynced to the serving host.

Per master, we read ``<data-dir>/markdown/*.md`` (where data-dir is e.g.
``/home/dtl/projects/data/MasterCorpus/munger``), split each file on the top
two markdown header levels (already promoted by convert_books.py from
calibre's bold-line output), then chunk each section with sliding-window if
long. Embed in one pass per file.

Run:
    python scripts/ingest_master.py munger
    python scripts/ingest_master.py graham
    python scripts/ingest_master.py lynch
    python scripts/ingest_master.py munger --rebuild   # drop existing twins/munger.db chunks first

Schema is the same chunks/chunks_vec tables as zhihu twins; ``zhihu_id`` is
repurposed as a generic ``source_id`` (book-<hash>-c<idx> or speech-<year>-<hash>-c<idx>).
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

import sqlite_vec
from tqdm import tqdm

# Allow `python scripts/ingest_master.py` from project root
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from bigv_twins.chunk import split_markdown_sections, chunk_text  # noqa: E402
from bigv_twins.config import settings  # noqa: E402
from bigv_twins.embed import Embedder  # noqa: E402
from bigv_twins.index import _open_twin_rw  # noqa: E402

log = logging.getLogger("bigv_twins.ingest_master")


# Front matter detection (YAML between --- ... ---)
_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)
_KEY_RE = re.compile(r"^(\w+):\s*(.+)$", re.M)


def _short_hash(s: str, n: int = 8) -> str:
    return hashlib.sha1(s.encode("utf-8")).hexdigest()[:n]


def _parse_frontmatter(md: str) -> tuple[dict, str]:
    """Return (meta_dict, body_without_frontmatter)."""
    m = _FRONTMATTER_RE.match(md)
    if not m:
        return {}, md
    meta = {}
    for km in _KEY_RE.finditer(m.group(1)):
        v = km.group(2).strip().strip("'\"")
        meta[km.group(1)] = v
    body = md[m.end():]
    return meta, body


def _classify_kind(filename: str, meta: dict) -> str:
    """speech | book — used in source_id and content_type."""
    kind = meta.get("kind", "").lower()
    if kind in ("speech", "book", "interview", "article"):
        return "book" if kind == "book" else "speech"
    if filename.startswith("book-"):
        return "book"
    # 4-digit-year prefix strongly suggests a speech / dated piece
    if re.match(r"^\d{4}", filename):
        return "speech"
    return "book"


def _file_url(slug: str, filename: str, kind: str) -> str:
    """Citation URL for a file (rendered by zhihu archive site once routes wired up)."""
    stem = Path(filename).stem
    return f"/masters/{slug}/{kind}/{stem}"


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


def _ingest_one_file(
    path: Path,
    slug: str,
    dst: sqlite3.Connection,
    embedder: Embedder,
) -> int:
    """Read one markdown file, chunk, embed, insert. Returns chunk count."""
    md = path.read_text(encoding="utf-8")
    meta, body = _parse_frontmatter(md)
    if not body.strip():
        return 0
    kind = _classify_kind(path.name, meta)
    file_hash = _short_hash(path.name)

    # Try header-based split. If the file has no `#`/`##` headers (rare; e.g.
    # speech with single big body), fall back to sliding-window plain chunks.
    sections = split_markdown_sections(
        body,
        chunk_size=settings.chunk_size,
        chunk_overlap=settings.chunk_overlap,
        min_level=1,
        max_level=2,
    )
    if not sections:
        # plain fallback
        plain_chunks = chunk_text(body, size=settings.chunk_size, overlap=settings.chunk_overlap)
        if not plain_chunks:
            return 0
        embeddings = embedder.encode_passages([c.text for c in plain_chunks])
        for c, emb in zip(plain_chunks, embeddings):
            sid = f"{kind}-{file_hash}-c{c.chunk_index:04d}"
            title = meta.get("title", path.stem)
            year = meta.get("year") or _maybe_year_from_filename(path.name)
            _insert_chunk(
                dst,
                source_id=sid, chunk_index=c.chunk_index, content_type=kind,
                title=title, text=c.text,
                url=_file_url(slug, path.name, kind),
                column_title=meta.get("title", path.stem),
                created_time=f"{year}-01-01" if year else "1970-01-01",
                embedding=emb,
            )
        return len(plain_chunks)

    # Header-based: each section produces 1+ chunks
    all_chunks = [(s, c) for s in sections for c in s.chunks]
    if not all_chunks:
        return 0
    embeddings = embedder.encode_passages([c.text for _s, c in all_chunks])
    year = meta.get("year") or _maybe_year_from_filename(path.name)
    book_or_speech_title = meta.get("title", path.stem)

    for idx, ((sec, chunk), emb) in enumerate(zip(all_chunks, embeddings)):
        sid = f"{kind}-{file_hash}-c{idx:04d}"
        # Title: include section title for context if we have one
        title = f"{book_or_speech_title} — {sec.title}" if sec.title else book_or_speech_title
        _insert_chunk(
            dst,
            source_id=sid, chunk_index=chunk.chunk_index, content_type=kind,
            title=title[:240], text=chunk.text,
            url=_file_url(slug, path.name, kind),
            column_title=book_or_speech_title[:80],
            created_time=f"{year}-01-01" if year else "1970-01-01",
            embedding=emb,
        )
    return len(all_chunks)


def _maybe_year_from_filename(filename: str) -> str | None:
    m = re.match(r"^(\d{4})", filename)
    return m.group(1) if m else None


def ingest_master(slug: str, data_dir: Path,
                   model_name: str = "BAAI/bge-m3",
                   device: str = "cpu",
                   rebuild: bool = False) -> int:
    """Main entry. Returns total chunks added.

    Output db is at ``settings.twin_db_path(slug)`` — typically
    ``<BigV-twins>/twins/<slug>.db``.
    """
    md_dir = data_dir / "markdown"
    if not md_dir.exists():
        raise SystemExit(f"markdown dir not found: {md_dir}\n"
                         f"run scripts/convert_books.py first")
    md_files = sorted(p for p in md_dir.glob("*.md"))
    if not md_files:
        raise SystemExit(f"no .md files in {md_dir}")
    log.info("[%s] %d markdown files to ingest", slug, len(md_files))

    embedder = Embedder(model_name=model_name, device=device)
    log.info("[%s] embedder %s loaded (dim=%d)", slug, model_name, embedder.dim)

    dst = _open_twin_rw(slug, embedder=embedder, rebuild=rebuild)
    dst_path = settings.twin_db_path(slug)

    total = 0
    for path in tqdm(md_files, desc=f"{slug}", unit="file"):
        try:
            n = _ingest_one_file(path, slug, dst, embedder)
            total += n
            dst.commit()
            log.info("[%s] %s → %d chunks", slug, path.name, n)
        except Exception as e:
            log.exception("[%s] %s failed: %s", slug, path.name, e)

    # Write meta
    dst.execute(
        "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
        ("last_indexed_at", datetime.now(timezone.utc).isoformat(timespec="seconds")),
    )
    dst.commit()
    dst.close()

    log.info("[%s] DONE → %s (%d chunks total)", slug, dst_path, total)
    return total


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("slug", help="master slug (munger / graham / lynch)")
    p.add_argument("--data-dir", default=None,
                   help="MasterCorpus/<slug> path (default: /home/dtl/projects/data/MasterCorpus/<slug>)")
    p.add_argument("--model", default="BAAI/bge-m3")
    p.add_argument("--device", default="cuda",
                   help="cpu | cuda | mps (default cuda; falls back to cpu if unavailable)")
    p.add_argument("--rebuild", action="store_true", help="drop existing chunks first")
    args = p.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s: %(message)s",
                        stream=sys.stdout)

    data_dir = Path(args.data_dir or f"/home/dtl/projects/data/MasterCorpus/{args.slug}")

    # device fallback
    device = args.device
    if device == "cuda":
        try:
            import torch
            if not torch.cuda.is_available():
                log.warning("CUDA not available, falling back to CPU")
                device = "cpu"
        except ImportError:
            device = "cpu"

    n = ingest_master(args.slug, data_dir, model_name=args.model,
                      device=device, rebuild=args.rebuild)
    log.info("=" * 50)
    log.info("ingest_master(%s): %d chunks", args.slug, n)


if __name__ == "__main__":
    main()
