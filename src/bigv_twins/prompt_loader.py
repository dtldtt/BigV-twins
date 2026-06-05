"""从 prompts/ 目录加载 .md 模板文件，做 {{变量}} 替换。

用法:
    from bigv_twins.prompt_loader import load_prompt

    prompt = load_prompt("chat/advisor.md")
    prompt = load_prompt("chat/master.md", blogger_slug="buffett", blogger_name="沃伦·巴菲特")
"""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

_PROMPTS_DIR = Path(__file__).resolve().parent.parent.parent / "prompts"


@lru_cache(maxsize=64)
def _read_template(rel_path: str) -> str:
    fp = _PROMPTS_DIR / rel_path
    if not fp.exists():
        raise FileNotFoundError(f"prompt template not found: {fp}")
    return fp.read_text(encoding="utf-8")


def load_prompt(rel_path: str, **kwargs: str) -> str:
    """读取 prompts/<rel_path> 并替换 {{key}} 占位符。

    >>> load_prompt("chat/master.md", blogger_slug="buffett", blogger_name="沃伦·巴菲特")
    """
    tpl = _read_template(rel_path)
    if kwargs:
        def _replace(m: re.Match) -> str:
            key = m.group(1).strip()
            return kwargs.get(key, m.group(0))
        tpl = re.sub(r"\{\{(\s*[\w.]+\s*)\}\}", _replace, tpl)
    return tpl


def reload_all() -> None:
    """清除缓存，强制下次调用重读文件。开发/热更新时用。"""
    _read_template.cache_clear()
