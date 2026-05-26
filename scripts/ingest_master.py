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
    if re.match(r"^\d{4}", filename):
        return "speech"
    return "book"


def _speech_url(slug: str, filename: str) -> str:
    stem = Path(filename).stem
    return f"/masters/{slug}/speech/{stem}"


def _book_chapter_url(slug: str, book_dir: str, chapter_filename: str) -> str:
    """URL for a single chapter, e.g.
    /masters/munger/book/《穷查理宝典》/05-鸣谢
    """
    stem = Path(chapter_filename).stem
    return f"/masters/{slug}/book/{book_dir}/{stem}"


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


def _ingest_speech_file(
    path: Path,
    slug: str,
    dst: sqlite3.Connection,
    embedder: Embedder,
) -> int:
    """Read one speech markdown (flat file in markdown/ dir), chunk, embed."""
    md = path.read_text(encoding="utf-8")
    meta, body = _parse_frontmatter(md)
    if not body.strip():
        return 0
    file_hash = _short_hash(path.name)
    year = meta.get("year") or _maybe_year_from_filename(path.name) or "1970"
    speech_title = meta.get("title", path.stem)
    url = _speech_url(slug, path.name)

    sections = split_markdown_sections(
        body,
        chunk_size=settings.chunk_size,
        chunk_overlap=settings.chunk_overlap,
        min_level=1, max_level=2,
    )
    if not sections:
        chunks = chunk_text(body, size=settings.chunk_size, overlap=settings.chunk_overlap)
        if not chunks:
            return 0
        embeddings = embedder.encode_passages([c.text for c in chunks])
        for c, emb in zip(chunks, embeddings):
            sid = f"speech-{file_hash}-c{c.chunk_index:04d}"
            _insert_chunk(
                dst, source_id=sid, chunk_index=c.chunk_index, content_type="speech",
                title=speech_title[:240], text=c.text, url=url,
                column_title=speech_title[:80],
                created_time=f"{year}-01-01", embedding=emb,
            )
        return len(chunks)

    all_chunks = [(s, c) for s in sections for c in s.chunks]
    if not all_chunks:
        return 0
    embeddings = embedder.encode_passages([c.text for _s, c in all_chunks])
    for idx, ((sec, chunk), emb) in enumerate(zip(all_chunks, embeddings)):
        sid = f"speech-{file_hash}-c{idx:04d}"
        title = f"{speech_title} — {sec.title}" if sec.title else speech_title
        _insert_chunk(
            dst, source_id=sid, chunk_index=chunk.chunk_index, content_type="speech",
            title=title[:240], text=chunk.text, url=url,
            column_title=speech_title[:80],
            created_time=f"{year}-01-01", embedding=emb,
        )
    return len(all_chunks)


def _ingest_book_chapter(
    chapter_path: Path,
    slug: str,
    book_meta: dict,
    book_dir_name: str,
    dst: sqlite3.Connection,
    embedder: Embedder,
) -> int:
    """Read one chapter markdown from a book's《...》/ dir, chunk, embed.

    The book dir contains _meta.json (book-level info) + NN-章名.md files.
    Each chapter has YAML frontmatter with chapter_idx / chapter_title / part.
    """
    md = chapter_path.read_text(encoding="utf-8")
    chap_meta, body = _parse_frontmatter(md)
    if not body.strip():
        return 0

    book_title = book_meta.get("title", book_dir_name)
    chapter_idx_str = chap_meta.get("chapter_idx", "00")
    try:
        chapter_idx = int(chapter_idx_str)
    except (ValueError, TypeError):
        chapter_idx = 0
    chapter_title = chap_meta.get("chapter_title", chapter_path.stem)
    part = chap_meta.get("part") or ""

    book_hash = _short_hash(book_dir_name)
    url = _book_chapter_url(slug, book_dir_name, chapter_path.name)
    # column_title 是 chat citation 显示的「来源」短标签
    if part:
        column_title = f"{book_title} · {part} · {chapter_title}"
    else:
        column_title = f"{book_title} · {chapter_title}"

    sections = split_markdown_sections(
        body,
        chunk_size=settings.chunk_size,
        chunk_overlap=settings.chunk_overlap,
        min_level=1, max_level=3,
    )
    if not sections:
        chunks = chunk_text(body, size=settings.chunk_size, overlap=settings.chunk_overlap)
        if not chunks:
            return 0
        embeddings = embedder.encode_passages([c.text for c in chunks])
        for c, emb in zip(chunks, embeddings):
            sid = f"book-{book_hash}-ch{chapter_idx:02d}-c{c.chunk_index:04d}"
            _insert_chunk(
                dst, source_id=sid, chunk_index=c.chunk_index, content_type="book",
                title=f"{book_title} {chapter_title}"[:240],
                text=c.text, url=url, column_title=column_title[:80],
                created_time="1970-01-01", embedding=emb,
            )
        return len(chunks)

    all_chunks = [(s, c) for s in sections for c in s.chunks]
    if not all_chunks:
        return 0
    embeddings = embedder.encode_passages([c.text for _s, c in all_chunks])
    for idx, ((sec, chunk), emb) in enumerate(zip(all_chunks, embeddings)):
        sid = f"book-{book_hash}-ch{chapter_idx:02d}-c{idx:04d}"
        title = f"{book_title} {chapter_title}"
        if sec.title and sec.title != chapter_title:
            title = f"{title} — {sec.title}"
        _insert_chunk(
            dst, source_id=sid, chunk_index=chunk.chunk_index, content_type="book",
            title=title[:240], text=chunk.text, url=url,
            column_title=column_title[:80],
            created_time="1970-01-01", embedding=emb,
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

    Walks two kinds of content in <data_dir>/markdown/:
      - 《book-name》/  (directory)  → book chapters (uses _meta.json + each chapter)
      - speeches/<NN>.md  (in speeches subdir)  → speech files
      - <flat>.md  (top-level flat md, NOT under speeches/)  → also treated as speech
        (for backward compat, like the early Munger fs.blog md before reorg)

    Output db is at ``settings.twin_db_path(slug)``.
    """
    md_dir = data_dir / "markdown"
    if not md_dir.exists():
        raise SystemExit(f"markdown dir not found: {md_dir}\n"
                         f"run scripts/convert_books.py + rebuild_books.py first")

    # Collect books (subdirs named 《...》)
    book_dirs = [p for p in md_dir.iterdir() if p.is_dir() and p.name.startswith("《")]
    # Collect speeches: speeches/ subdir's .md files + flat .md files at top level
    speech_files: list[Path] = []
    speeches_dir = md_dir / "speeches"
    if speeches_dir.is_dir():
        speech_files.extend(sorted(speeches_dir.glob("*.md")))
    # Flat top-level .md files = speech-style. Skip the OLD `book-*.md` flat
    # files that have been superseded by the《...》/ chapter dirs.
    for p in sorted(md_dir.glob("*.md")):
        if p.is_file() and not p.name.startswith("book-"):
            speech_files.append(p)

    if not book_dirs and not speech_files:
        raise SystemExit(f"no content in {md_dir}")

    log.info("[%s] %d books + %d speeches to ingest", slug, len(book_dirs), len(speech_files))

    embedder = Embedder(model_name=model_name, device=device)
    log.info("[%s] embedder %s loaded (dim=%d)", slug, model_name, embedder.dim)

    dst = _open_twin_rw(slug, embedder=embedder, rebuild=rebuild)
    dst_path = settings.twin_db_path(slug)

    import json as _json
    total = 0

    # 1) Speeches (flat md files at top level OR in speeches/ subdir)
    for path in tqdm(speech_files, desc=f"{slug} speeches", unit="file"):
        try:
            n = _ingest_speech_file(path, slug, dst, embedder)
            total += n
            dst.commit()
            log.info("[%s] speech %s → %d chunks", slug, path.name, n)
        except Exception as e:
            log.exception("[%s] speech %s failed: %s", slug, path.name, e)

    # 2) Books (each 《...》/ dir has _meta.json + chapter md files)
    for book_dir in tqdm(book_dirs, desc=f"{slug} books", unit="book"):
        meta_path = book_dir / "_meta.json"
        if meta_path.is_file():
            try:
                book_meta = _json.loads(meta_path.read_text(encoding="utf-8"))
            except Exception as e:
                log.warning("[%s] failed to read %s: %s", slug, meta_path, e)
                book_meta = {"title": book_dir.name}
        else:
            book_meta = {"title": book_dir.name}
        book_chunks = 0
        for chapter_path in sorted(book_dir.glob("*.md")):
            try:
                n = _ingest_book_chapter(
                    chapter_path, slug, book_meta, book_dir.name, dst, embedder
                )
                total += n
                book_chunks += n
                dst.commit()
            except Exception as e:
                log.exception("[%s] book chapter %s failed: %s",
                              slug, chapter_path.name, e)
        log.info("[%s] book %s → %d chunks", slug, book_dir.name, book_chunks)

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
