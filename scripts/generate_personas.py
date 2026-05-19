"""Generate per-blogger persona summaries using whichever LLM provider OpenClaw has configured.

Reads `~/.openclaw/openclaw.json` to discover the configured provider (baseUrl + apiKey
+ default model) and calls it directly via the OpenAI SDK. This is a "raw model" call —
no agent loop, no system prompt bloat, same billing path as OpenClaw uses internally.

Run once at setup. Re-run with --force only when a blogger's style has drifted.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sqlite3
import sys
from pathlib import Path

from openai import OpenAI

from bigv_twins.chunk import html_to_text
from bigv_twins.config import BLOGGERS, BY_SLUG, settings


SYSTEM_PROMPT = """你是一个为投资博主建立「风格指南」的研究员。

输入是一位中文投资博主的若干篇高赞作品（混合答案 / 文章 / 想法）。
请输出一份 800–1500 字的中文 markdown，结构如下：

# {blogger_name} · 风格指南

## 投资框架
核心投资哲学与方法论。1–3 段文字。

## 关注的领域 / 行业
列举 3–6 个他 / 她经常分析的具体领域（行业、资产类别、宏观主题等）。

## 决策依据 / 关键指标
做投资判断时倚重的信号、指标、模型、历史类比。具体到名称。

## 立场鲜明的观点
明确反对的事情，或反复强调的判断。3–6 条，每条一句话。

## 表达习惯
口头禅、惯用比喻、典型句式。给 2–4 个具体的原文引文片段（用「」括起来）。

## 不擅长 / 主动回避的话题
若语料中博主明确说过自己「不懂 X」或「不做 Y」，列出来；否则写「未在归档中明确表态」。

约束：
- 只基于给定语料，不编造、不外推。
- 不恭维博主，中性陈述。
- 表达习惯部分必须给真实引文片段。
- 直接从 H1 开始输出，纯 markdown，不要包代码块、不要免责声明 / 礼貌开场结尾。
"""


def _resolve_provider(override_provider: str | None = None) -> tuple[str, str, str]:
    """Read OpenClaw's config and return (base_url, api_key, model_id).

    If `override_provider` is set, use that provider's config; otherwise use
    the provider named in `agents.defaults.model.primary` (e.g. "bailian/qwen3.5-plus").
    Falls back to the first provider with at least one model.
    """
    cfg_path = Path.home() / ".openclaw" / "openclaw.json"
    if not cfg_path.exists():
        raise RuntimeError(f"openclaw config not found at {cfg_path}")
    cfg = json.loads(cfg_path.read_text())

    providers = (cfg.get("models") or {}).get("providers") or {}
    if not providers:
        raise RuntimeError("no providers found in openclaw config")

    if override_provider:
        prov_name = override_provider
        model_id = None
    else:
        primary = (
            (cfg.get("agents") or {})
            .get("defaults", {})
            .get("model", {})
            .get("primary")
        )
        if primary and "/" in primary:
            prov_name, model_id = primary.split("/", 1)
        else:
            prov_name = next(iter(providers))
            model_id = None

    if prov_name not in providers:
        raise RuntimeError(
            f"provider {prov_name!r} not found; available: {list(providers)}"
        )
    p = providers[prov_name]
    base_url = p.get("baseUrl")
    api_key = p.get("apiKey")
    if not base_url or not api_key:
        raise RuntimeError(f"provider {prov_name!r} missing baseUrl or apiKey")

    if model_id is None:
        models = p.get("models") or []
        if not models:
            raise RuntimeError(f"provider {prov_name!r} has no models declared")
        model_id = models[0]["id"]

    return base_url, api_key, model_id


def _fetch_top_posts(author_id: int, n: int) -> list[dict]:
    uri = f"file:{settings.zhihu_db_path}?mode=ro&immutable=1"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT zhihu_id, content_type, title, content, voteup_count, created_time "
        "FROM contents WHERE author_id = ? AND content IS NOT NULL AND content <> '' "
        "ORDER BY voteup_count DESC LIMIT ?",
        (author_id, n),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def _build_user_prompt(blogger_name: str, posts: list[dict]) -> str:
    parts = [
        f"博主：{blogger_name}\n以下是 {len(posts)} 篇按点赞数排序的高赞作品：\n"
    ]
    for i, p in enumerate(posts, 1):
        head = f"[{p['content_type']}]"
        if p["title"]:
            head += f" {p['title']}"
        text = html_to_text(p["content"])
        parts.append(
            f"\n--- 第 {i} 篇 · {head} · 点赞 {p['voteup_count']} · "
            f"{p['created_time']} ---\n{text}"
        )
    return "\n".join(parts)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--blogger", default=None, help="slug; default = all")
    parser.add_argument(
        "--provider",
        default=None,
        help="override openclaw provider name (e.g. 'bailian'); default = primary",
    )
    parser.add_argument(
        "--model",
        default=None,
        help="override model id (e.g. 'qwen3.5-plus'); default = provider primary",
    )
    parser.add_argument("--top-n", type=int, default=30)
    parser.add_argument("--max-tokens", type=int, default=4096)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--force", action="store_true", help="overwrite existing files")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
    log = logging.getLogger("gen-personas")

    if not args.dry_run:
        try:
            base_url, api_key, default_model = _resolve_provider(args.provider)
        except RuntimeError as e:
            sys.exit(f"failed to resolve openclaw provider: {e}")
        model_id = args.model or default_model
        log.info("using provider base_url=%s model=%s", base_url, model_id)
        client = OpenAI(base_url=base_url, api_key=api_key)
    else:
        client = None
        model_id = None

    targets = [BY_SLUG[args.blogger]] if args.blogger else list(BLOGGERS)
    settings.personas_dir.mkdir(parents=True, exist_ok=True)

    for b in targets:
        path = settings.persona_path(b.slug)
        if path.exists() and not args.force and not args.dry_run:
            log.info("[%s] %s already exists, skip (--force to overwrite)", b.slug, path)
            continue

        posts = _fetch_top_posts(b.author_id, n=args.top_n)
        if not posts:
            log.warning("[%s] no posts found, skip", b.slug)
            continue

        sys_msg = SYSTEM_PROMPT.format(blogger_name=b.name)
        user_msg = _build_user_prompt(b.name, posts)
        log.info(
            "[%s] system=%d chars, user=%d chars (%d posts)",
            b.slug, len(sys_msg), len(user_msg), len(posts),
        )

        if args.dry_run:
            continue

        try:
            resp = client.chat.completions.create(
                model=model_id,
                messages=[
                    {"role": "system", "content": sys_msg},
                    {"role": "user", "content": user_msg},
                ],
                max_tokens=args.max_tokens,
                temperature=0.3,
            )
        except Exception as e:
            log.error("[%s] inference failed: %s", b.slug, e)
            continue

        text = (resp.choices[0].message.content or "").strip()
        if not text:
            log.error("[%s] empty response: %s", b.slug, resp)
            continue

        path.write_text(text, encoding="utf-8")
        usage = getattr(resp, "usage", None)
        log.info(
            "[%s] wrote %s (%d chars; usage in=%s out=%s)",
            b.slug, path, len(text),
            getattr(usage, "prompt_tokens", "?"),
            getattr(usage, "completion_tokens", "?"),
        )


if __name__ == "__main__":
    main()
