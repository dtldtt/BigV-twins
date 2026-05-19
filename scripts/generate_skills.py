"""Render one OpenClaw Skill per blogger from a shared template.

Each skill is a thin behavioral wrapper around the bigv-twins MCP server.
Skill bodies deliberately do NOT embed the persona text — the agent calls
`bigv-twins.get_persona` at runtime to read the current version.
"""

from __future__ import annotations

import argparse
import logging
from pathlib import Path

from bigv_twins.config import BLOGGERS, BY_SLUG


ALIASES: dict[str, str] = {
    "mr-dang": "Dang、dang 哥、当哥、MR Dang",
    "eyu":     "鳄鱼、寒武纪、寒武纪鳄鱼、寒武纪的鳄鱼",
    "sanren":  "三人禾、三人、水又三人禾",
    "shen":    "沈同学、沈、阳光下沈、阳光下的沈同学",
}


SKILL_TEMPLATE = """---
name: bigv-{slug}
description: 咨询投资博主「{name}」在知乎归档中的观点与框架。问「{name} 怎么看 X」/「@{name}」时触发。
version: 0.1.0
metadata:
  openclaw:
    homepage: {homepage}
---

# 何时启用

用户提到 **{name}**（或常用别称：{aliases}），并明确想知道此人对某个话题的看法、框架、立场时启用此 skill。
也可以在用户笼统问「投资博主们怎么看 X」时与其他 `bigv-*` skill 并列触发。

# 如何回答（必须按此顺序）

1. **了解风格**：调 MCP 工具 `bigv-twins.get_persona`，参数 `{{"blogger":"{slug}"}}`，读出博主的投资框架、关注领域、决策依据。
2. **检索证据**：调 `bigv-twins.search`，参数 `{{"blogger":"{slug}","query":<用户问题原文或改写>,"top_k":5}}`。
   - 若用户问「最近怎么看 X」，**先**调 `bigv-twins.get_recent`（n=10）看博主最近发言里是否直接谈过 X，再决定要不要 `search`。
   - 若检索 top1 距离 > 1.1，换查询用词重试一次（同义词 / 更具体 / 更抽象）。
3. **生成回答**：仅以返回的片段为依据。每个观点必须附引用：`[摘要片段](url) — YYYY-MM-DD`。
4. **检索为空 / 不相关**：明确说「在 {name} 的归档中没找到对此问题的明确表述」，可附 1-2 个相关但不直接的片段供用户自判。**不要外推、不要编造**。
5. 用相对中性的语气，不要刻意模仿博主的语气。开头注明「以下基于 {name} 在知乎的归档」。
6. 用户要看完整原文：调 `bigv-twins.get_post`，参数 `{{"blogger":"{slug}","zhihu_id":<上一步结果中的 zhihu_id>}}`。

# 输出格式

```
以下基于 {name} 在知乎的归档：

<2-5 句话的简明回答，基于检索片段>

依据：
- 「<片段摘要 1>」 — [原文](URL)，YYYY-MM-DD，type=answer/article/pin
- 「<片段摘要 2>」 — [原文](URL)，YYYY-MM-DD
…
```

# 红线

- ❌ 不凭训练知识 / 印象回答 {name} 的观点；只用 MCP 检索结果。
- ❌ 不编造引文。检索结果中没有的话直接说没有。
- ❌ 不为了「像博主」而改写语气；保持中性陈述。
- ❌ 不在没有证据的情况下用「{name} 应该会认为…」/「按他的风格估计…」。
"""


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--blogger", default=None, help="slug; default = all")
    parser.add_argument("--out-dir", type=Path, default=Path("skills"))
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
    log = logging.getLogger("gen-skills")

    targets = [BY_SLUG[args.blogger]] if args.blogger else list(BLOGGERS)

    for b in targets:
        skill_dir = args.out_dir / f"bigv-{b.slug}"
        skill_dir.mkdir(parents=True, exist_ok=True)
        body = SKILL_TEMPLATE.format(
            slug=b.slug,
            name=b.name,
            aliases=ALIASES.get(b.slug, b.name),
            homepage=f"https://www.zhihu.com/people/{b.url_token}",
        )
        path = skill_dir / "SKILL.md"
        path.write_text(body, encoding="utf-8")
        log.info("[%s] wrote %s (%d bytes)", b.slug, path, len(body))


if __name__ == "__main__":
    main()
