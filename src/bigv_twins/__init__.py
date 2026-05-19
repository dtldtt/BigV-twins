"""bigv_twins — per-blogger digital twins on top of the Zhihu archive."""

from dotenv import load_dotenv

# Load .env into the process environment as early as possible so HF_ENDPOINT,
# proxy variables, etc. are visible to libraries that read os.environ directly
# (huggingface_hub, transformers).
load_dotenv()

from .config import BLOGGERS, BY_AUTHOR_ID, BY_SLUG, Blogger, settings  # noqa: E402

__all__ = ["BLOGGERS", "BY_AUTHOR_ID", "BY_SLUG", "Blogger", "settings"]
