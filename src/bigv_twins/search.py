"""Retrieval over per-blogger sqlite-vec indices."""

from __future__ import annotations

import argparse
import logging
import sqlite3
from dataclasses import asdict, dataclass

import sqlite_vec

from .config import BY_SLUG, settings
from .embed import Embedder

log = logging.getLogger("bigv_twins.search")


@dataclass
class Hit:
    chunk_id: int
    zhihu_id: str
    chunk_index: int
    content_type: str
    title: str | None
    text: str
    voteup_count: int
    url: str | None
    column_title: str | None
    created_time: str | None
    distance: float

    def to_dict(self) -> dict:
        return asdict(self)


def _open_twin_ro(slug: str) -> sqlite3.Connection:
    path = settings.twin_db_path(slug)
    if not path.exists():
        raise FileNotFoundError(f"twin db missing for {slug!r}: {path}")
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    return conn


_EMBEDDER: Embedder | None = None


def get_embedder() -> Embedder:
    global _EMBEDDER
    if _EMBEDDER is None:
        log.info("loading embedder %s", settings.embedding_model)
        _EMBEDDER = Embedder(settings.embedding_model)
    return _EMBEDDER


def search(
    blogger_slug: str,
    query: str,
    *,
    top_k: int = 5,
    content_type: str | None = None,
) -> list[Hit]:
    if blogger_slug not in BY_SLUG:
        raise ValueError(f"unknown blogger slug: {blogger_slug!r}")
    if not query.strip():
        return []

    qvec = get_embedder().encode_query(query)
    qbytes = sqlite_vec.serialize_float32(qvec.tolist())

    conn = _open_twin_ro(blogger_slug)
    try:
        candidate_k = top_k * 5 if content_type else top_k
        rows = conn.execute(
            """
            SELECT v.rowid AS chunk_id, v.distance AS distance,
                   c.zhihu_id, c.chunk_index, c.content_type, c.title, c.text,
                   c.voteup_count, c.url, c.column_title, c.created_time
            FROM chunks_vec v
            JOIN chunks c ON c.id = v.rowid
            WHERE v.embedding MATCH ? AND k = ?
            ORDER BY v.distance
            """,
            (qbytes, candidate_k),
        ).fetchall()

        if content_type:
            rows = [r for r in rows if r["content_type"] == content_type]
        rows = rows[:top_k]

        return [
            Hit(
                chunk_id=r["chunk_id"],
                zhihu_id=r["zhihu_id"],
                chunk_index=r["chunk_index"],
                content_type=r["content_type"],
                title=r["title"],
                text=r["text"],
                voteup_count=r["voteup_count"] or 0,
                url=r["url"],
                column_title=r["column_title"],
                created_time=r["created_time"],
                distance=r["distance"],
            )
            for r in rows
        ]
    finally:
        conn.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--blogger", required=True)
    parser.add_argument("--query", required=True)
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument(
        "--content-type", default=None, choices=["answer", "article", "pin"]
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )

    hits = search(
        args.blogger, args.query, top_k=args.top_k, content_type=args.content_type
    )

    if not hits:
        print("(no results)")
        return

    for i, h in enumerate(hits, 1):
        print(
            f"--- #{i} dist={h.distance:.4f} type={h.content_type} "
            f"voteup={h.voteup_count} ---"
        )
        if h.title:
            print(f"  title : {h.title}")
        if h.url:
            print(f"  url   : {h.url}")
        if h.created_time:
            print(f"  date  : {h.created_time}")
        snippet = h.text[:240].replace("\n", " ")
        more = "…" if len(h.text) > 240 else ""
        print(f"  text  : {snippet}{more}")
        print()


if __name__ == "__main__":
    main()
