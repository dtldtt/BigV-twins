"""Incremental indexer: zhihu.db (read-only) -> twins/{slug}.db (rw)."""

from __future__ import annotations

import argparse
import hashlib
import logging
import sqlite3
from datetime import datetime, timezone
from typing import Iterable

import sqlite_vec
from tqdm import tqdm

from .chunk import chunk_text, html_to_text
from .config import BLOGGERS, BY_SLUG, Blogger, settings
from .embed import Embedder

log = logging.getLogger("bigv_twins.index")


_BASE_SCHEMA = """
CREATE TABLE IF NOT EXISTS chunks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    zhihu_id TEXT NOT NULL,
    chunk_index INTEGER NOT NULL,
    content_type TEXT NOT NULL,
    title TEXT,
    text TEXT NOT NULL,
    voteup_count INTEGER DEFAULT 0,
    comment_count INTEGER DEFAULT 0,
    url TEXT,
    column_title TEXT,
    created_time TEXT,
    updated_time TEXT,
    UNIQUE(zhihu_id, chunk_index)
);
CREATE INDEX IF NOT EXISTS idx_chunks_zhihu_id ON chunks(zhihu_id);
CREATE INDEX IF NOT EXISTS idx_chunks_content_type ON chunks(content_type);
CREATE INDEX IF NOT EXISTS idx_chunks_created_time ON chunks(created_time);

CREATE TABLE IF NOT EXISTS indexed_contents (
    zhihu_id TEXT PRIMARY KEY,
    content_hash TEXT NOT NULL,
    indexed_at TEXT NOT NULL,
    chunk_count INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def _open_zhihu_ro() -> sqlite3.Connection:
    uri = f"file:{settings.zhihu_db_path}?mode=ro&immutable=1"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _open_twin_rw(slug: str, dim: int) -> sqlite3.Connection:
    settings.twins_dir.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(settings.twin_db_path(slug))
    conn.row_factory = sqlite3.Row
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    conn.executescript(_BASE_SCHEMA)
    conn.execute(
        f"CREATE VIRTUAL TABLE IF NOT EXISTS chunks_vec USING vec0(embedding float[{dim}])"
    )
    conn.commit()
    return conn


def _content_hash(content: str, updated_time: str | None) -> str:
    h = hashlib.sha1()
    h.update(content.encode("utf-8"))
    h.update(b"\x00")
    h.update((updated_time or "").encode("utf-8"))
    return h.hexdigest()


def _delete_chunks_for(dst: sqlite3.Connection, zhihu_id: str) -> int:
    rows = dst.execute(
        "SELECT id FROM chunks WHERE zhihu_id = ?", (zhihu_id,)
    ).fetchall()
    if not rows:
        return 0
    dst.executemany(
        "DELETE FROM chunks_vec WHERE rowid = ?", [(r["id"],) for r in rows]
    )
    dst.execute("DELETE FROM chunks WHERE zhihu_id = ?", (zhihu_id,))
    return len(rows)


def index_blogger(
    blogger: Blogger,
    *,
    embedder: Embedder,
    force: bool = False,
    limit: int | None = None,
) -> dict[str, int]:
    counts = {
        "source_rows": 0,
        "skipped_unchanged": 0,
        "new": 0,
        "reindexed": 0,
        "chunks_added": 0,
        "chunks_deleted": 0,
        "empty_after_clean": 0,
    }

    src = _open_zhihu_ro()
    dst = _open_twin_rw(blogger.slug, embedder.dim)
    try:
        sql = (
            "SELECT zhihu_id, content_type, title, content, voteup_count, "
            "comment_count, url, column_title, created_time, updated_time "
            "FROM contents WHERE author_id = ? AND content IS NOT NULL "
            "AND content <> '' ORDER BY created_time"
        )
        params: tuple = (blogger.author_id,)
        if limit:
            sql += " LIMIT ?"
            params = (blogger.author_id, limit)
        rows = src.execute(sql, params).fetchall()

        counts["source_rows"] = len(rows)
        log.info("[%s] %d source rows", blogger.slug, len(rows))

        for row in tqdm(rows, desc=blogger.slug, unit="item"):
            zid = row["zhihu_id"]
            content_hash = _content_hash(row["content"], row["updated_time"])

            existing = dst.execute(
                "SELECT content_hash FROM indexed_contents WHERE zhihu_id = ?",
                (zid,),
            ).fetchone()

            if existing and existing["content_hash"] == content_hash and not force:
                counts["skipped_unchanged"] += 1
                continue

            deleted = _delete_chunks_for(dst, zid)
            counts["chunks_deleted"] += deleted
            if deleted:
                counts["reindexed"] += 1
            else:
                counts["new"] += 1

            text = html_to_text(row["content"])
            chunks = chunk_text(
                text, size=settings.chunk_size, overlap=settings.chunk_overlap
            )

            if not chunks:
                counts["empty_after_clean"] += 1
                dst.execute(
                    "INSERT OR REPLACE INTO indexed_contents "
                    "(zhihu_id, content_hash, indexed_at, chunk_count) VALUES (?,?,?,0)",
                    (zid, content_hash, datetime.now(timezone.utc).isoformat()),
                )
                dst.commit()
                continue

            embeddings = embedder.encode_passages([c.text for c in chunks])

            for c, emb in zip(chunks, embeddings):
                cur = dst.execute(
                    "INSERT INTO chunks (zhihu_id, chunk_index, content_type, title, "
                    "text, voteup_count, comment_count, url, column_title, "
                    "created_time, updated_time) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        zid, c.chunk_index, row["content_type"], row["title"],
                        c.text, row["voteup_count"] or 0, row["comment_count"] or 0,
                        row["url"], row["column_title"],
                        row["created_time"], row["updated_time"],
                    ),
                )
                dst.execute(
                    "INSERT INTO chunks_vec(rowid, embedding) VALUES (?, ?)",
                    (cur.lastrowid, sqlite_vec.serialize_float32(emb.tolist())),
                )
                counts["chunks_added"] += 1

            dst.execute(
                "INSERT OR REPLACE INTO indexed_contents "
                "(zhihu_id, content_hash, indexed_at, chunk_count) VALUES (?,?,?,?)",
                (zid, content_hash, datetime.now(timezone.utc).isoformat(), len(chunks)),
            )
            dst.commit()

        dst.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
            ("last_indexed_at", datetime.now(timezone.utc).isoformat()),
        )
        dst.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
            ("embedding_model", embedder.model_name),
        )
        dst.commit()
    finally:
        dst.close()
        src.close()

    return counts


def main() -> None:
    parser = argparse.ArgumentParser(description="BigV-twins incremental indexer")
    parser.add_argument("--blogger", default=None, help="slug; default = all")
    parser.add_argument("--force", action="store_true", help="reindex even if unchanged")
    parser.add_argument("--limit", type=int, default=None, help="cap rows (testing)")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    targets: Iterable[Blogger]
    if args.blogger:
        if args.blogger not in BY_SLUG:
            raise SystemExit(f"unknown blogger slug: {args.blogger}")
        targets = [BY_SLUG[args.blogger]]
    else:
        targets = BLOGGERS

    log.info("loading embedder: %s", settings.embedding_model)
    embedder = Embedder(settings.embedding_model)
    log.info("embedder ready (dim=%d)", embedder.dim)

    for b in targets:
        c = index_blogger(b, embedder=embedder, force=args.force, limit=args.limit)
        log.info("[%s] done %s", b.slug, c)


if __name__ == "__main__":
    main()
