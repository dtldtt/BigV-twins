"""Sentence-transformers wrapper. Default model is BGE-base-zh-v1.5 (CPU)."""

from __future__ import annotations

import threading
from typing import Sequence

import numpy as np
from sentence_transformers import SentenceTransformer

_QUERY_PREFIX_BGE_ZH = "为这个句子生成表示以用于检索相关文章："


class Embedder:
    def __init__(self, model_name: str, device: str = "cpu") -> None:
        self.model_name = model_name
        self._model = SentenceTransformer(model_name, device=device)
        self._lock = threading.Lock()
        self.dim: int = int(self._model.get_sentence_embedding_dimension())

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
        prefixed = _QUERY_PREFIX_BGE_ZH + query if "bge" in self.model_name.lower() else query
        with self._lock:
            v = self._model.encode(
                prefixed,
                show_progress_bar=False,
                normalize_embeddings=True,
                convert_to_numpy=True,
            )
        return v.astype(np.float32)
