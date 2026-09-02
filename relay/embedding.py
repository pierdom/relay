"""Embedding backends — a swappable seam (relay #253) so the model is a
one-line config change, and so the default test suite never has to load one.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Protocol

from .config import settings

# Must run before huggingface_hub is imported anywhere in the process (fastembed
# pulls it in transitively) — HF_XET_CACHE, the xet fast-transfer backend's own
# cache/log path, is derived from HF_HOME as a plain module-level constant in
# huggingface_hub.constants, computed once at that module's import time. Setting
# HF_HOME later, or passing TextEmbedding(cache_dir=...) at all, has no effect on
# it — cache_dir only redirects the actual model snapshot, a separate mechanism.
# relay's container runs as an arbitrary host UID with no matching /etc/passwd
# entry (docker-compose.yml's `user:`), so $HOME is unset and every HOME-derived
# default resolves to an unwritable path under `/`. HF_HUB_DISABLE_XET sidesteps
# the whole native xet subsystem rather than chasing every path it might derive
# from HF_HOME — plain HTTPS downloads are plenty fast for one small model. This
# module is imported unconditionally at app startup (relay.vectors imports it
# regardless of embedding_enabled), so it's early enough even before any opt-in.
os.environ.setdefault("HF_HOME", str(Path(settings.relay_dir) / "hf-home"))
os.environ.setdefault("HF_HUB_DISABLE_XET", "1")

EMBEDDING_DIM = 384  # fixed at vec0 table creation — see relay/vectors.py


class EmbeddingBackend(Protocol):
    model_id: str
    dim: int

    def embed_documents(self, texts: list[str]) -> list[list[float]]: ...
    def embed_query(self, text: str) -> list[float]: ...


class FastEmbedBackend:
    """In-process ONNX inference via fastembed. intfloat's e5 family *requires*
    asymmetric prefixes (query: / passage:) — applied structurally here, in the
    two separate methods, so a call site can't forget one (relay #253's
    explicit gotcha: omitting them degrades quality silently, with no error).

    Applied only when the configured model is actually e5 — a non-e5 model
    (the current default; e5-small isn't in fastembed 0.8.0's registry, see
    Settings.embedding_model) wasn't trained with this convention, and forcing
    the prefixes on it would silently degrade quality the same way omitting
    them does on a model that needs them."""

    dim = EMBEDDING_DIM

    def __init__(self) -> None:
        from fastembed import TextEmbedding

        self.model_id = settings.embedding_model
        self._e5_prefixes = "e5" in self.model_id.lower()
        # Explicit cache_dir — see Settings.embedding_cache_dir's docstring.
        # Without it, huggingface_hub's snapshot_download falls back to a
        # HOME-based default that's unwritable under relay's actual runtime UID.
        Path(settings.embedding_cache_dir).mkdir(parents=True, exist_ok=True)
        self._model = TextEmbedding(model_name=self.model_id, cache_dir=settings.embedding_cache_dir)

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        if self._e5_prefixes:
            texts = [f"passage: {t}" for t in texts]
        return [v.tolist() for v in self._model.embed(texts)]

    def embed_query(self, text: str) -> list[float]:
        if self._e5_prefixes:
            text = f"query: {text}"
        return next(iter(self._model.embed([text]))).tolist()


class FakeBackend:
    """Deterministic hash-derived unit vectors — no model, no I/O. Covers every
    plumbing test fast and offline (relay #253's CI strategy). ``dim`` matches
    the real backend so both write into the same fixed vec0 schema."""

    model_id = "fake-v1"
    dim = EMBEDDING_DIM

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [self._vector(t) for t in texts]

    def embed_query(self, text: str) -> list[float]:
        return self._vector(text)

    def _vector(self, text: str) -> list[float]:
        digest = hashlib.sha256(text.encode()).digest()
        tiled = (digest * (self.dim // len(digest) + 1))[: self.dim]
        raw = [b / 255.0 for b in tiled]
        norm = sum(x * x for x in raw) ** 0.5 or 1.0
        return [x / norm for x in raw]


_backend: EmbeddingBackend | None = None


def get_backend() -> EmbeddingBackend:
    """Lazy singleton — constructing (and thus model-loading) ``FastEmbedBackend``
    only happens on first real call. Tests monkeypatch this function directly."""
    global _backend
    if _backend is None:
        _backend = FastEmbedBackend()
    return _backend
