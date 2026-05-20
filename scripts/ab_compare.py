"""A/B compare retrieval quality: BGE-base-zh-v1.5 vs bge-m3.

Runs on private after the new DBs have been staged at twins/.bgem3-staging/.
For each (blogger, query) pair: search old DB with old embedder, search new DB
with new embedder, print top-K side-by-side. Eyeballed comparison — no automatic
metric, the goal is "no obvious regression".

Usage:
    python scripts/ab_compare.py
    python scripts/ab_compare.py --blogger eyu  # one blogger only
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

import sqlite_vec

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from bigv_twins.embed import Embedder  # noqa: E402
from bigv_twins.config import settings  # noqa: E402


# Representative queries per blogger — chosen because we know each blogger
# has talked extensively about these topics. If old retrieves something
# relevant and new returns garbage (or vice versa), we'll see it here.
TEST_QUERIES: dict[str, list[str]] = {
    "mr-dang": [
        "高股息策略",
        "弱者体系怎么理解",
        "信息差套利",
    ],
    "eyu": [
        "鳄鱼的八条选股原则",
        "为什么不买银行股",
        "央企国企的估值修复",
    ],
    "sanren": [
        "黄金的周期判断",
        "不预测点位的趋势跟随",
        "美股周期交易",
    ],
    "shen": [
        "闲钱投资 严禁杠杆",
        "知识变现",
        "趋势加价值",
    ],
    "paipi": [
        "924 后长牛",
        "锂电产业链",
        "港股科网",
    ],
}


def _open_ro(path: Path):
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    return conn


def _search_db(conn, embedder: Embedder, query: str, top_k: int = 5):
    qvec = embedder.encode_query(query)
    qbytes = sqlite_vec.serialize_float32(qvec.tolist())
    rows = conn.execute(
        """
        SELECT v.distance AS distance, c.zhihu_id, c.content_type, c.title,
               substr(c.text, 1, 80) AS text_preview, c.voteup_count, c.created_time
        FROM chunks_vec v JOIN chunks c ON c.id = v.rowid
        WHERE v.embedding MATCH ? AND k = ?
        ORDER BY v.distance
        """,
        (qbytes, top_k),
    ).fetchall()
    return rows


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--blogger", default=None, help="Only compare this slug")
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument(
        "--old-model", default="BAAI/bge-base-zh-v1.5",
        help="The model the existing twins/<slug>.db was built with",
    )
    parser.add_argument(
        "--new-model", default="BAAI/bge-m3",
        help="The model the staged twins/.bgem3-staging/<slug>.db was built with",
    )
    parser.add_argument(
        "--staging-subdir", default=".bgem3-staging",
        help="Subdir under twins/ where the new (bge-m3) DBs live",
    )
    args = parser.parse_args()

    twins_dir = settings.twins_dir
    staging_dir = twins_dir / args.staging_subdir
    if not staging_dir.exists():
        sys.exit(f"staging dir not found: {staging_dir}")

    print(f"loading {args.old_model} ...", flush=True)
    old_emb = Embedder(args.old_model)
    print(f"loading {args.new_model} ...", flush=True)
    new_emb = Embedder(args.new_model)

    bloggers = [args.blogger] if args.blogger else list(TEST_QUERIES.keys())
    for slug in bloggers:
        if slug not in TEST_QUERIES:
            print(f"\n!!! no test queries defined for {slug}, skipping")
            continue
        old_path = twins_dir / f"{slug}.db"
        new_path = staging_dir / f"{slug}.db"
        if not old_path.exists():
            print(f"\n!!! old db missing: {old_path}")
            continue
        if not new_path.exists():
            print(f"\n!!! new db missing: {new_path}")
            continue

        old_conn = _open_ro(old_path)
        new_conn = _open_ro(new_path)
        try:
            for query in TEST_QUERIES[slug]:
                print("\n" + "=" * 80)
                print(f"  blogger={slug}  query={query!r}")
                print("=" * 80)

                old_hits = _search_db(old_conn, old_emb, query, args.top_k)
                new_hits = _search_db(new_conn, new_emb, query, args.top_k)

                print(f"\n--- OLD ({args.old_model}) ---")
                for i, h in enumerate(old_hits, 1):
                    print(f"  {i}. dist={h['distance']:.3f}  votes={h['voteup_count']}  "
                          f"date={(h['created_time'] or '')[:10]}  type={h['content_type']}")
                    print(f"     {h['text_preview']}...")

                print(f"\n--- NEW ({args.new_model}) ---")
                for i, h in enumerate(new_hits, 1):
                    print(f"  {i}. dist={h['distance']:.3f}  votes={h['voteup_count']}  "
                          f"date={(h['created_time'] or '')[:10]}  type={h['content_type']}")
                    print(f"     {h['text_preview']}...")
        finally:
            old_conn.close()
            new_conn.close()


if __name__ == "__main__":
    main()
