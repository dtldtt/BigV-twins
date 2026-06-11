"""Shared MCP tool-call stats — written by each MCP server, read by admin dashboard.

Each MCP server (blogger / market) imports `track_tool` and wraps its tool
functions. Counts and last-call timestamps are flushed to a shared JSON file
that the admin dashboard reads on each page load.
"""

from __future__ import annotations

import functools
import json
import logging
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("bigv_twins.mcp_stats")

_STATS_PATH = Path("/home/dtl/projects/BigV-twins/data/mcp_stats.json")
_lock = threading.Lock()
_in_memory: dict[str, dict] = {}


def _flush():
    """Write in-memory stats to disk atomically."""
    try:
        _STATS_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = _STATS_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(_in_memory, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(_STATS_PATH)
    except Exception:
        log.exception("failed to flush MCP stats to disk")


def track_tool(server_name: str, tool_name: str):
    """Decorator: increment call count + update last_called_at for a tool."""
    def decorator(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            now = datetime.now(timezone.utc).isoformat()
            with _lock:
                entry = _in_memory.setdefault(server_name, {"tools": {}})
                tool_entry = entry["tools"].setdefault(tool_name, {"count": 0})
                tool_entry["count"] += 1
                tool_entry["last_called_at"] = now
                entry["updated_at"] = now
            _flush()
            return fn(*args, **kwargs)
        return wrapper
    return decorator


def load_stats() -> dict:
    """Read stats from disk (for admin dashboard)."""
    if not _STATS_PATH.exists():
        return {}
    try:
        return json.loads(_STATS_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}
