"""Qoder SDK 通用调用封装 + usage 自动记录。

所有需要调 Qoder performance 的地方统一走这里。
"""
from __future__ import annotations

import logging

from bigv_twins.config import settings

log = logging.getLogger("bigv_twins.web.qoder_call")


async def call_qoder(prompt: str, task_type: str, task_detail: str = "",
                      model: str = "ultimate") -> str | None:
    """调 Qoder SDK，返回文本结果。自动记录 token usage。"""
    if not settings.qoder_personal_access_token:
        log.warning("qoder %s/%s skipped: no PAT", task_type, task_detail)
        return None
    try:
        from qoder_agent_sdk import (
            AssistantMessage, ResultMessage, QoderAgentOptions, access_token, query,
        )
    except ImportError as e:
        log.warning("qoder_agent_sdk import failed: %s", e)
        return None

    options = QoderAgentOptions(
        auth=access_token(settings.qoder_personal_access_token),
        model=model,
    )
    pieces: list[str] = []
    result_msg = None
    try:
        async for msg in query(prompt=prompt, options=options):
            if isinstance(msg, AssistantMessage):
                content = getattr(msg, "content", None)
                if isinstance(content, list):
                    for c in content:
                        if isinstance(c, dict) and c.get("type") == "text":
                            pieces.append(c.get("text", ""))
                        elif hasattr(c, "text"):
                            pieces.append(c.text)
                elif isinstance(content, str):
                    pieces.append(content)
            elif isinstance(msg, ResultMessage):
                result_msg = msg
    except Exception as e:
        log.warning("qoder %s/%s failed: %s", task_type, task_detail, e)
        return None

    if result_msg:
        try:
            from . import db
            await db.log_qoder_usage(task_type, task_detail, result_msg, model=model)
        except Exception:
            log.warning("failed to log qoder usage for %s/%s", task_type, task_detail)

    text = "".join(pieces).strip()
    return text or None
