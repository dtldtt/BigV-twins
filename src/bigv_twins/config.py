from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


@dataclass(frozen=True)
class Blogger:
    """A "Blogger" entry in bloggers.json.

    For real archived Zhihu bloggers (kind='blogger'):
      author_id, url_token, name, tagline are all populated.
      `agent` should be "bigv".

    For the synthetic "AI 投资顾问" (kind='advisor'):
      author_id=-1, url_token="" and only name/tagline are meaningful.
      `agent` should be "advisor".
    """
    slug: str
    name: str
    author_id: int = -1
    url_token: str = ""
    tagline: str = ""
    kind: str = "blogger"     # "blogger" | "advisor"
    agent: str = "bigv"       # which OpenClaw agent to route to (openclaw/<agent>)

    @property
    def db_filename(self) -> str:
        return f"{self.slug}.db"

    @property
    def persona_filename(self) -> str:
        return f"{self.slug}.md"

    @property
    def is_advisor(self) -> bool:
        return self.kind == "advisor"

    @property
    def is_blogger(self) -> bool:
        return self.kind == "blogger"


# Project root: src/bigv_twins/config.py -> ../../../
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_BLOGGERS_JSON = _PROJECT_ROOT / "bloggers.json"


def _load_bloggers() -> tuple[Blogger, ...]:
    """Read bloggers from bloggers.json (canonical source of truth)."""
    if _BLOGGERS_JSON.exists():
        data = json.loads(_BLOGGERS_JSON.read_text(encoding="utf-8"))
        return tuple(Blogger(**b) for b in data)
    return (
        Blogger(slug="mr-dang", author_id=1, url_token="mr-dang-77",       name="MR Dang"),
        Blogger(slug="eyu",     author_id=2, url_token="chen-ze-xin-49-22", name="寒武纪的鳄鱼"),
        Blogger(slug="sanren",  author_id=3, url_token="10-64-17-85-40",    name="水又三人禾"),
        Blogger(slug="shen",    author_id=4, url_token="shen-chen-7-10",    name="阳光下的沈同学"),
    )


BLOGGERS: tuple[Blogger, ...] = _load_bloggers()
BY_SLUG: dict[str, Blogger] = {b.slug: b for b in BLOGGERS}
BY_AUTHOR_ID: dict[int, Blogger] = {b.author_id: b for b in BLOGGERS if b.author_id > 0}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Data sources
    zhihu_db_path: Path = Path("/home/dtl/projects/zhihu/data/zhihu.db")
    twins_dir: Path = Path("/home/dtl/projects/BigV-twins/twins")
    personas_dir: Path = Path("/home/dtl/projects/BigV-twins/personas")

    # Embedding
    embedding_model: str = "BAAI/bge-base-zh-v1.5"
    embedding_dim: int = 768
    chunk_size: int = 600
    chunk_overlap: int = 80

    # MCP servers (now split into two; keep mcp_port for back-compat read)
    mcp_host: str = "127.0.0.1"
    mcp_blogger_port: int = 8770   # blogger-corpus tools (search / persona / recent / post)
    mcp_market_port: int = 8771    # market data tools (stock_snapshot / market_context)
    mcp_port: int = 8770           # deprecated alias = mcp_blogger_port

    # Web chat app
    web_host: str = "127.0.0.1"
    web_port: int = 8001
    web_secret_key: str = ""

    # OpenClaw gateway
    openclaw_base_url: str = "http://127.0.0.1:18789"
    openclaw_config_path: Path = Path.home() / ".openclaw" / "openclaw.json"
    openclaw_agent_timeout_s: int = 180

    # Optional persona-gen fallback
    anthropic_api_key: str = ""

    def twin_db_path(self, slug: str) -> Path:
        return self.twins_dir / f"{slug}.db"

    def persona_path(self, slug: str) -> Path:
        return self.personas_dir / f"{slug}.md"

    @property
    def chats_db_path(self) -> Path:
        return self.twins_dir.parent / "chats.db"

    @property
    def bloggers_json_path(self) -> Path:
        return _BLOGGERS_JSON


settings = Settings()
