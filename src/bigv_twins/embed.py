"""Sentence-transformers wrapper.

A ``MODEL_REGISTRY`` maps each supported embedding model to its
canonical dim + query prefix scheme. This is the **single place** to add
support for a new model — `index.py` and `search.py` consult it via the
``Embedder`` instance, never via hardcoded names.
"""

from __future__ import annotations

import threading
from typing import Sequence

import numpy as np
from sentence_transformers import SentenceTransformer


# ----- model registry --------------------------------------------------------

# Each entry pins:
#   - dim: must match the actual sentence-transformer's hidden size; cross-checked
#          against runtime dim in __init__ as a sanity guard
#   - query_prefix: prepended to user queries during retrieval (only). Different
#          BGE generations want different (or no) prefix.
MODEL_REGISTRY: dict[str, dict] = {
    "BAAI/bge-base-zh-v1.5": {
        "dim": 768,
        "query_prefix": "为这个句子生成表示以用于检索相关文章：",
    },
    "BAAI/bge-large-zh-v1.5": {
        "dim": 1024,
        "query_prefix": "为这个句子生成表示以用于检索相关文章：",
    },
    "BAAI/bge-m3": {
        # bge-m3 is multilingual + larger context; its training does NOT use
        # the bge-zh-style instruction prefix, and adding one degrades recall.
        "dim": 1024,
        "query_prefix": "",
    },
}


class Embedder:
    def __init__(self, model_name: str, device: str = "cpu") -> None:
        if model_name not in MODEL_REGISTRY:
            raise ValueError(
                f"unknown embedding model {model_name!r}. "
                f"Add it to MODEL_REGISTRY in embed.py first. "
                f"Known: {sorted(MODEL_REGISTRY)}"
            )
        self.model_name = model_name
        self._spec = MODEL_REGISTRY[model_name]
        self._model = SentenceTransformer(model_name, device=device)
        self._lock = threading.Lock()
        self.dim: int = int(self._model.get_sentence_embedding_dimension())
        if self.dim != self._spec["dim"]:
            raise RuntimeError(
                f"{model_name} dim mismatch: registry says {self._spec['dim']}, "
                f"runtime says {self.dim}. Update MODEL_REGISTRY."
            )

    @property
    def query_prefix(self) -> str:
        return self._spec["query_prefix"]

    def encode_passages(
        self, texts: Sequence[str], *, batch_size: int = 32
    ) -> np.ndarray:
        if not texts:
            return np.zeros((0, self.dim), dtype=np.float32)
        with self._lock:
            return self._model.encode(
                list(texts),
                batch_size=batch_size,
                show_progress_bar=False,
                normalize_embeddings=True,
                convert_to_numpy=True,
            ).astype(np.float32)

    def encode_query(self, query: str) -> np.ndarray:
        prefixed = self.query_prefix + query if self.query_prefix else query
        with self._lock:
            v = self._model.encode(
                prefixed,
                show_progress_bar=False,
                normalize_embeddings=True,
                convert_to_numpy=True,
            )
        return v.astype(np.float32)
