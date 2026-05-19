"""One-shot CLI to add a new blogger to BigV-twins.

Prereqs (you handle these):
    1. The blogger is already being scraped by the zhihu project, so a row
       exists in zhihu.db.authors and contents are accumulating.
    2. You know that blogger's author_id (look at zhihu.db.authors).
    3. You pick a slug — short, lowercase, URL-safe (e.g. "newbie").
    4. (Optional) you write a one-line tagline shown on the chat card.

Usage:
    python scripts/add_blogger.py --author-id 5 --slug newbie \
        [--tagline "价值投资 · 重视分红"] [--name "新博主"] [--skip-skill-copy]

This script does, in order:
    1. Validate author_id exists in zhihu.db; look up url_token + name
    2. Append the new blogger entry to bloggers.json
    3. Run full incremental index for this blogger
    4. Generate persona summary via OpenClaw provider
    5. Render SKILL.md from template
    6. Copy skill folder to ~/.openclaw/workspace/skills/

Re-running with the same slug is rejected (use --force to overwrite the
bloggers.json entry; you'd then need to manually re-index/re-persona).
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

# Resolve project root from this script's location.
SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
BLOGGERS_JSON = PROJECT_ROOT / "bloggers.json"
WORKSPACE_SKILLS = Path.home() / ".openclaw" / "workspace" / "skills"


def _err(msg: str) -> "None":
    print(f"❌ {msg}", file=sys.stderr)
    sys.exit(1)


def _ok(msg: str) -> None:
    print(f"✓ {msg}")


def _lookup_author(zhihu_db_path: Path, author_id: int) -> dict:
    """Read author row from zhihu.db.authors."""
    if not zhihu_db_path.exists():
        _err(f"zhihu db not found: {zhihu_db_path}")
    uri = f"file:{zhihu_db_path}?mode=ro&immutable=1"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT id, url_token, name, follower_count, answer_count, "
            "article_count, pin_count FROM authors WHERE id = ?",
            (author_id,),
        ).fetchone()
        if row is None:
            _err(
                f"author_id={author_id} not found in zhihu.db.authors. "
                "Make sure the zhihu project has scraped this author at least once."
            )
        return dict(row)
    finally:
        conn.close()


def _load_bloggers() -> list[dict]:
    if not BLOGGERS_JSON.exists():
        return []
    return json.loads(BLOGGERS_JSON.read_text(encoding="utf-8"))


def _save_bloggers(data: list[dict]) -> None:
    BLOGGERS_JSON.write_text(
        json.dumps(data, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def _run(cmd: list[str], *, check: bool = True) -> int:
    print(f"\n  $ {' '.join(cmd)}")
    result = subprocess.run(cmd, cwd=PROJECT_ROOT)
    if check and result.returncode != 0:
        _err(f"command failed (exit {result.returncode})")
    return result.returncode


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--author-id", type=int, required=True, help="zhihu.db.authors.id")
    parser.add_argument("--slug", required=True, help="lowercase URL-safe slug (e.g. 'newbie')")
    parser.add_argument("--tagline", default="", help="一句话简介，显示在 /chat 卡片上")
    parser.add_argument("--name", default=None, help="override the name (default: zhihu.db.authors.name)")
    parser.add_argument("--force", action="store_true",
                        help="overwrite an existing entry with the same slug")
    parser.add_argument("--skip-index", action="store_true")
    parser.add_argument("--skip-persona", action="store_true")
    parser.add_argument("--skip-skill", action="store_true")
    parser.add_argument("--skip-skill-copy", action="store_true",
                        help="generate SKILL.md but don't copy to OpenClaw workspace")
    args = parser.parse_args()

    # 1. Validate slug shape
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", args.slug):
        _err(f"invalid slug {args.slug!r}: lowercase letters/digits/hyphens only, "
             "must start with letter or digit.")

    # Lazy import so the import doesn't trigger pydantic Settings load issues
    # before we've validated args.
    from bigv_twins.config import settings

    # 2. Look up author
    author = _lookup_author(settings.zhihu_db_path, args.author_id)
    print(f"\nFound in zhihu.db:")
    print(f"  id={author['id']}  url_token={author['url_token']!r}  name={author['name']!r}")
    print(f"  follower={author['follower_count']}  answer={author['answer_count']}  "
          f"article={author['article_count']}  pin={author['pin_count']}")

    # 3. Check bloggers.json for collisions
    bloggers = _load_bloggers()
    existing_by_slug = {b["slug"]: i for i, b in enumerate(bloggers)}
    existing_by_aid = {b["author_id"]: b["slug"] for b in bloggers}

    if args.author_id in existing_by_aid and existing_by_aid[args.author_id] != args.slug:
        _err(f"author_id={args.author_id} is already registered as slug "
             f"{existing_by_aid[args.author_id]!r}. Use that slug or remove it first.")

    if args.slug in existing_by_slug and not args.force:
        _err(f"slug {args.slug!r} already exists in bloggers.json. Use --force to overwrite.")

    entry = {
        "slug": args.slug,
        "author_id": args.author_id,
        "url_token": author["url_token"],
        "name": args.name or author["name"],
        "tagline": args.tagline,
    }

    # 4. Write bloggers.json
    if args.slug in existing_by_slug:
        bloggers[existing_by_slug[args.slug]] = entry
    else:
        bloggers.append(entry)
    _save_bloggers(bloggers)
    _ok(f"wrote bloggers.json (now {len(bloggers)} entries)")

    # 5. Run pipeline
    if not args.skip_index:
        _ok("indexing (may take a while for large authors)...")
        _run(["python", "-m", "bigv_twins.index", "--blogger", args.slug])

    if not args.skip_persona:
        _ok("generating persona (uses OpenClaw-configured provider)...")
        _run(["python", "scripts/generate_personas.py", "--blogger", args.slug])

    if not args.skip_skill:
        _ok("rendering SKILL.md...")
        _run(["python", "scripts/generate_skills.py", "--blogger", args.slug])

    # 6. Copy skill to OpenClaw workspace
    if not args.skip_skill and not args.skip_skill_copy:
        src = PROJECT_ROOT / "skills" / f"bigv-{args.slug}"
        if not src.is_dir():
            _err(f"skill source missing: {src}")
        dst = WORKSPACE_SKILLS / f"bigv-{args.slug}"
        if not WORKSPACE_SKILLS.exists():
            print(f"⚠ {WORKSPACE_SKILLS} missing; create it or set up OpenClaw first.")
        else:
            if dst.exists():
                shutil.rmtree(dst)
            shutil.copytree(src, dst)
            _ok(f"copied skill to {dst}")

    print()
    _ok(f"all done. blogger {entry['name']!r} (slug={args.slug}) registered.")
    print()
    print("Next steps:")
    print(f"  - Restart web app to pick up the new blogger (the cards refresh on each request,")
    print(f"    but the in-process settings need restart):")
    print(f"      systemctl --user restart bigv-twins-web")
    print(f"  - Test in browser:  https://your-host/chat/{args.slug}")


if __name__ == "__main__":
    main()
