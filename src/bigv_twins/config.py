from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


@dataclass(frozen=True)
class Blogger:
    slug: str
    author_id: int
    url_token: str
    name: str

    @property
    def db_filename(self) -> str:
        return f"{self.slug}.db"

    @property
    def persona_filename(self) -> str:
        return f"{self.slug}.md"


BLOGGERS: tuple[Blogger, ...] = (
    Blogger(slug="mr-dang", author_id=1, url_token="mr-dang-77",       name="MR Dang"),
    Blogger(slug="eyu",     author_id=2, url_token="chen-ze-xin-49-22", name="寒武纪的鳄鱼"),
    Blogger(slug="sanren",  author_id=3, url_token="10-64-17-85-40",    name="水又三人禾"),
    Blogger(slug="shen",    author_id=4, url_token="shen-chen-7-10",    name="阳光下的沈同学"),
)

BY_SLUG: dict[str, Blogger] = {b.slug: b for b in BLOGGERS}
BY_AUTHOR_ID: dict[int, Blogger] = {b.author_id: b for b in BLOGGERS}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    zhihu_db_path: Path = Path("/home/dtl/projects/zhihu/data/zhihu.db")
    twins_dir: Path = Path("/home/dtl/projects/BigV-twins/twins")
    personas_dir: Path = Path("/home/dtl/projects/BigV-twins/personas")

    embedding_model: str = "BAAI/bge-base-zh-v1.5"
    embedding_dim: int = 768

    chunk_size: int = 600
    chunk_overlap: int = 80

    mcp_host: str = "127.0.0.1"
    mcp_port: int = 8770

    anthropic_api_key: str = ""

    def twin_db_path(self, slug: str) -> Path:
        return self.twins_dir / f"{slug}.db"

    def persona_path(self, slug: str) -> Path:
        return self.personas_dir / f"{slug}.md"


settings = Settings()
