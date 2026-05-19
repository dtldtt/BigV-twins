"""Generate per-blogger persona summaries via the OpenClaw-configured LLM.

v0.2: stratified sampling (B) + verification-and-revision pass (D).

Sampling (B):
    - top 20 by voteup_count           — captures popular themes
    - 10 most recent                   — captures current/late style
    - 10 longest articles (>= 1500ch)  — captures detailed reasoning
    Dedupe by zhihu_id, sort by voteup desc, take all (~40 distinct items).

Verification (D, when --verify):
    - First call: generate persona from training sample
    - Second call: take a fresh held-out sample (different 10 posts the model
      didn't see), ask the model to compare and revise. Output replaces v1.

Falls back to the simple top-N flow when --simple is passed.

Reads `~/.openclaw/openclaw.json` to discover the configured provider
(baseUrl + apiKey + model). Same billing path as everything else.
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

输入是一位中文投资博主的若干篇代表性作品（混合答案 / 文章 / 想法），
按高赞 + 近期 + 长文章三个维度分层采样。

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
口头禅、惯用比喻、典型句式。给 2–4 个具体的原文引文片段（用「」括起来，必须来自语料）。

## 不擅长 / 主动回避的话题
若语料中博主明确说过自己「不懂 X」或「不做 Y」，列出来；否则写「未在归档中明确表态」。

约束：
- 只基于给定语料，不编造、不外推。
- 不恭维博主，中性陈述。
- 表达习惯部分必须给真实引文片段。
- 直接从 H1 开始输出，纯 markdown，不要包代码块、不要免责声明 / 礼貌开场结尾。
"""


VERIFY_PROMPT = """你刚才为博主「{blogger_name}」写了下面这份风格指南：

==================== 你之前写的画像 ====================
{persona_v1}
==================== 画像结束 ====================

现在我又给你 10 篇你之前**没用来生成画像**的代表作。请评估你之前的画像，
然后输出一份**修订版的完整画像**（同样的章节结构）。

评估时请检查：
1. 画像里描述的投资框架 / 倾向，在这 10 篇里是否仍然吻合？哪些地方需要修正？
2. 画像里没提到的关注点 / 风格特征，这 10 篇里是否有体现？
3. 「表达习惯」段的引文，是否还有更鲜明的口头禅没收录？
4. 是否有自相矛盾或过度概括的地方需要打磨？

约束：
- 修订版仍然只基于全部语料（之前的 + 这次的 10 篇），不外推。
- 中性陈述，不恭维。
- 不要写「修订说明」之类的元信息——直接输出修订版的完整画像，从 H1 开始。
- 表达习惯部分的引文必须真实（来自任一篇语料）。

==================== 新增 10 篇 ====================
{validation_corpus}
"""


def _resolve_provider() -> tuple[str, str, str]:
    cfg_path = settings.openclaw_config_path
    if not cfg_path.exists():
        raise RuntimeError(f"openclaw config not found at {cfg_path}")
    cfg = json.loads(cfg_path.read_text())
    providers = (cfg.get("models") or {}).get("providers") or {}
    if not providers:
        raise RuntimeError("no providers found in openclaw config")
    primary = (cfg.get("agents") or {}).get("defaults", {}).get("model", {}).get("primary", "")
    if primary and "/" in primary:
        prov_name, model_id = primary.split("/", 1)
    else:
        prov_name = next(iter(providers))
        model_id = None
    p = providers[prov_name]
    base_url = p.get("baseUrl")
    api_key = p.get("apiKey")
    if not (base_url and api_key):
        raise RuntimeError(f"provider {prov_name!r} missing baseUrl or apiKey")
    if model_id is None:
        model_id = (p.get("models") or [{}])[0].get("id")
    return base_url, api_key, model_id


# ----- sampling -----------------------------------------------------

def _query(conn: sqlite3.Connection, sql: str, params: tuple) -> list[dict]:
    rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def _stratified_sample(
    author_id: int,
    *,
    top_voteup: int,
    recent: int,
    long: int,
    long_min_chars: int = 1500,
) -> tuple[list[dict], list[dict]]:
    """Returns (training_sample, validation_sample)."""
    uri = f"file:{settings.zhihu_db_path}?mode=ro&immutable=1"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row

    base_cols = (
        "zhihu_id, content_type, title, content, voteup_count, created_time, url"
    )

    top_q = _query(conn,
        f"SELECT {base_cols} FROM contents WHERE author_id=? "
        "AND content IS NOT NULL AND content<>'' "
        "ORDER BY voteup_count DESC LIMIT ?",
        (author_id, top_voteup))

    recent_q = _query(conn,
        f"SELECT {base_cols} FROM contents WHERE author_id=? "
        "AND content IS NOT NULL AND content<>'' "
        "ORDER BY created_time DESC LIMIT ?",
        (author_id, recent))

    long_q = _query(conn,
        f"SELECT {base_cols} FROM contents WHERE author_id=? "
        "AND content IS NOT NULL AND content<>'' "
        "AND length(content) >= ? "
        "ORDER BY length(content) DESC LIMIT ?",
        (author_id, long_min_chars, long))

    # Dedupe by zhihu_id, preserve "popular first" ordering
    seen: set[str] = set()
    training: list[dict] = []
    for src in (top_q, recent_q, long_q):
        for r in src:
            if r["zhihu_id"] in seen:
                continue
            seen.add(r["zhihu_id"])
            training.append(r)

    # Validation set: 10 different posts not in training, sampled by mid-voteup
    # (skip the very top and bottom; pick something representative)
    validation_q = _query(conn,
        f"SELECT {base_cols} FROM contents WHERE author_id=? "
        "AND content IS NOT NULL AND content<>'' "
        "AND zhihu_id NOT IN ({}) ".format(
            ",".join("?" for _ in seen) if seen else "''"
        ) +
        "ORDER BY voteup_count DESC LIMIT 30",
        tuple([author_id, *seen]) if seen else (author_id,))

    # Pick 10 spread across the 30 (every 3rd)
    validation = validation_q[::3][:10] if len(validation_q) >= 10 else validation_q

    conn.close()
    return training, validation


# ----- corpus formatting -------------------------------------------

def _format_corpus(blogger_name: str, posts: list[dict], header: str | None = None) -> str:
    parts = [header or f"博主：{blogger_name}\n以下是 {len(posts)} 篇代表作品：\n"]
    for i, p in enumerate(posts, 1):
        head = f"[{p['content_type']}]"
        if p.get("title"):
            head += f" {p['title']}"
        text = html_to_text(p["content"])
        # cap individual posts to ~4000 chars to keep prompt sane
        if len(text) > 4000:
            text = text[:4000] + "\n…(后续略)"
        parts.append(
            f"\n--- 第 {i} 篇 · {head} · 点赞 {p['voteup_count']} · "
            f"{p.get('created_time','?')} ---\n{text}"
        )
    return "\n".join(parts)


# ----- driver -------------------------------------------------------

def generate_for(slug: str, *, args, client: OpenAI, model_id: str, log: logging.Logger) -> None:
    b = BY_SLUG[slug]

    if args.simple:
        # legacy top-N flow
        uri = f"file:{settings.zhihu_db_path}?mode=ro&immutable=1"
        conn = sqlite3.connect(uri, uri=True)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT zhihu_id, content_type, title, content, voteup_count, created_time "
            "FROM contents WHERE author_id=? AND content IS NOT NULL "
            "ORDER BY voteup_count DESC LIMIT ?",
            (b.author_id, args.top_n),
        ).fetchall()
        conn.close()
        training = [dict(r) for r in rows]
        validation: list[dict] = []
    else:
        training, validation = _stratified_sample(
            b.author_id,
            top_voteup=args.top_voteup,
            recent=args.recent,
            long=args.long,
        )

    if not training:
        log.warning("[%s] no posts found, skip", slug)
        return

    log.info(
        "[%s] training=%d (top=%d, recent=%d, long=%d, dedup); validation=%d",
        slug, len(training), args.top_voteup, args.recent, args.long, len(validation),
    )

    # ---- 1st call: generate v1 ----
    sys_msg = SYSTEM_PROMPT.format(blogger_name=b.name)
    user_msg = _format_corpus(b.name, training)
    log.info("[%s] call 1 (initial): %d chars input", slug, len(user_msg))
    resp = client.chat.completions.create(
        model=model_id,
        messages=[
            {"role": "system", "content": sys_msg},
            {"role": "user", "content": user_msg},
        ],
        max_tokens=args.max_tokens,
        temperature=0.3,
    )
    persona_v1 = (resp.choices[0].message.content or "").strip()
    usage = resp.usage
    log.info(
        "[%s] call 1 usage: in=%s out=%s",
        slug, getattr(usage, "prompt_tokens", "?"),
        getattr(usage, "completion_tokens", "?"),
    )

    final_persona = persona_v1

    # ---- 2nd call (verify): revise against held-out validation set ----
    if args.verify and validation:
        validation_corpus = _format_corpus(
            b.name, validation,
            header=f"以下是博主「{b.name}」的另外 {len(validation)} 篇你没看过的代表作：\n",
        )
        verify_user = VERIFY_PROMPT.format(
            blogger_name=b.name,
            persona_v1=persona_v1,
            validation_corpus=validation_corpus,
        )
        log.info("[%s] call 2 (verify): %d chars input", slug, len(verify_user))
        resp2 = client.chat.completions.create(
            model=model_id,
            messages=[
                {"role": "system", "content": sys_msg},
                {"role": "user", "content": verify_user},
            ],
            max_tokens=args.max_tokens,
            temperature=0.3,
        )
        persona_v2 = (resp2.choices[0].message.content or "").strip()
        u2 = resp2.usage
        log.info(
            "[%s] call 2 usage: in=%s out=%s",
            slug, getattr(u2, "prompt_tokens", "?"),
            getattr(u2, "completion_tokens", "?"),
        )
        if persona_v2:
            final_persona = persona_v2
            log.info("[%s] used verified version", slug)
        else:
            log.warning("[%s] verify pass empty; keeping v1", slug)

    if not final_persona:
        log.error("[%s] empty persona, skip write", slug)
        return

    path = settings.persona_path(slug)
    path.write_text(final_persona, encoding="utf-8")
    log.info("[%s] wrote %s (%d chars)", slug, path, len(final_persona))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--blogger", default=None, help="slug; default = all")
    parser.add_argument("--force", action="store_true", help="overwrite existing files")
    parser.add_argument("--dry-run", action="store_true")

    # Sampling controls (B)
    parser.add_argument("--simple", action="store_true",
                        help="use legacy top-N voteup sampling (no stratification)")
    parser.add_argument("--top-n", type=int, default=30,
                        help="(simple mode only) number of high-voteup posts")
    parser.add_argument("--top-voteup", type=int, default=20)
    parser.add_argument("--recent", type=int, default=10)
    parser.add_argument("--long", type=int, default=10)

    # Verification pass (D)
    parser.add_argument("--verify", action="store_true",
                        help="run a 2nd pass to verify+revise against a held-out sample")

    parser.add_argument("--max-tokens", type=int, default=4096)
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
    log = logging.getLogger("gen-personas")

    if args.dry_run:
        targets = [BY_SLUG[args.blogger]] if args.blogger else list(BLOGGERS)
        for b in targets:
            if args.simple:
                log.info("[%s] would sample top-%d by voteup", b.slug, args.top_n)
            else:
                tr, val = _stratified_sample(
                    b.author_id, top_voteup=args.top_voteup,
                    recent=args.recent, long=args.long,
                )
                log.info("[%s] would train on %d posts; verify on %d", b.slug, len(tr), len(val))
        return

    try:
        base_url, api_key, default_model = _resolve_provider()
    except RuntimeError as e:
        sys.exit(f"failed to resolve openclaw provider: {e}")
    client = OpenAI(base_url=base_url, api_key=api_key)
    log.info("using provider base_url=%s model=%s", base_url, default_model)

    targets = [BY_SLUG[args.blogger]] if args.blogger else list(BLOGGERS)
    settings.personas_dir.mkdir(parents=True, exist_ok=True)

    for b in targets:
        path = settings.persona_path(b.slug)
        if path.exists() and not args.force:
            log.info("[%s] %s exists, skip (--force to overwrite)", b.slug, path)
            continue
        try:
            generate_for(b.slug, args=args, client=client, model_id=default_model, log=log)
        except Exception as e:
            log.exception("[%s] failed: %s", b.slug, e)
            continue


if __name__ == "__main__":
    main()
