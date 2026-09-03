"""Embedding backends — a swappable seam (relay #253) so the model is a
one-line config change, and so the default test suite never has to load one.
"""
from __future__ import annotations

import ctypes
import gc
import hashlib
import logging
import os
import time
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

logger = logging.getLogger(__name__)

EMBEDDING_DIM = 384  # FakeBackend's fixed test dim; also vectors.py's schema
# default while embedding_enabled=False (nothing has resolved a real model's
# dim yet, so the schema is pre-built at the shipped default — see resolve_dim).


def _model_entry(model_id: str) -> dict:
    """Look up ``model_id`` in fastembed's static model registry — pure
    in-memory metadata (``TextEmbedding.EMBEDDINGS_REGISTRY``), no download
    and no ONNX session constructed. Shared by ``resolve_dim`` and
    ``resolve_size_mb`` so there's one lookup, not two."""
    from fastembed import TextEmbedding

    for m in TextEmbedding.list_supported_models():
        if m["model"] == model_id:
            return m
    raise ValueError(f"Unknown fastembed model: {model_id!r}")


def resolve_dim(model_id: str) -> int:
    """``model_id``'s embedding dimension. Lets ``vectors.init_vec`` learn the
    dimension a model change requires *before* paying for a model load, and
    makes a typo in ``EMBEDDING_MODEL`` fail fast at startup instead of
    surfacing later as a dimension-mismatch error on the first real embed
    call."""
    return _model_entry(model_id)["dim"]


def resolve_size_mb(model_id: str) -> float:
    """``model_id``'s on-disk size in MB, per fastembed's registry — surfaced
    in ``/status`` so choosing a bigger model (relay #253 backlog) comes with
    a concrete number, not just a dimension."""
    return round(_model_entry(model_id)["size_in_GB"] * 1024, 1)


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

    def __init__(self) -> None:
        from fastembed import TextEmbedding

        self.model_id = settings.embedding_model
        # dim is per-instance, not a fixed class constant — EMBEDDING_MODEL is
        # a one-line .env change to any fastembed model, and different models
        # carry different dims (relay #253 backlog: bigger multilingual models
        # are 768d/1024d, not 384d). vectors.init_vec resolves the same value
        # ahead of constructing this backend, to size vec_chunks before the
        # model ever loads.
        self.dim = resolve_dim(self.model_id)
        self._e5_prefixes = "e5" in self.model_id.lower()
        # Explicit cache_dir — see Settings.embedding_cache_dir's docstring.
        # Without it, huggingface_hub's snapshot_download falls back to a
        # HOME-based default that's unwritable under relay's actual runtime UID.
        Path(settings.embedding_cache_dir).mkdir(parents=True, exist_ok=True)
        # threads caps onnxruntime's own thread pool — see Settings.embedding_threads.
        self._model = TextEmbedding(
            model_name=self.model_id,
            cache_dir=settings.embedding_cache_dir,
            threads=settings.embedding_threads,
        )

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
_last_used: float = 0.0


def get_backend() -> EmbeddingBackend:
    """Lazy singleton — constructing (and thus model-loading) ``FastEmbedBackend``
    only happens on first real call. Tests monkeypatch this function directly.

    Records ``_last_used`` on every call (not just the first) — see
    ``unload_if_idle``, which reads it to decide whether the model has been
    sitting unused long enough to give its memory back."""
    global _backend, _last_used
    if _backend is None:
        _backend = FastEmbedBackend()
    _last_used = time.monotonic()
    return _backend


def is_loaded() -> bool:
    """Whether the backend is currently resident in memory — surfaced in
    ``/status`` so the idle-unload cycle (relay #253, v1.1.3) is observable
    instead of only inferred from RSS."""
    return _backend is not None


def unload_if_idle() -> bool:
    """Drop the loaded backend if nothing has used it in
    ``settings.embedding_idle_unload_seconds``. Returns whether it actually
    unloaded something. Meant to be polled periodically from a background
    task (main.py's lifespan), never from a request path.

    Constructing FastEmbedBackend costs ~570MB of RSS by itself (onnxruntime
    session + model weights, measured locally: ~67MB -> ~637MB before a
    single embed call), and that cost doesn't grow with usage afterward — a
    single query and a 20-doc batch both left RSS flat. So capping
    embedding_threads (a thread-pool size) never touched this: the memory is
    the model being loaded at all, not per-call or per-thread. Unloading
    between uses trades that ~570MB for a several-second reload delay on the
    next embed call — a real tradeoff, tuned via
    ``settings.embedding_idle_unload_seconds`` (default 300s; 0 disables and
    keeps the model resident forever, the pre-v1.1.3 behavior).

    ``gc.collect()`` alone only gave back ~200MB of that ~570MB in local
    testing — the rest sat in glibc's malloc arenas as free-but-not-returned-
    to-the-OS memory (normal glibc behavior, not a leak). ``malloc_trim(0)``
    is what actually returns it: same test dropped to within ~65MB of the
    pre-load baseline. Best-effort — wrapped in a broad except so a platform
    without glibc's malloc_trim (musl, non-Linux dev boxes) still unloads the
    Python object correctly, just without the extra reclaim."""
    if _backend is None or settings.embedding_idle_unload_seconds <= 0:
        return False
    if time.monotonic() - _last_used < settings.embedding_idle_unload_seconds:
        return False
    _do_unload()
    return True


def force_unload() -> bool:
    """Unconditionally drop the backend, bypassing the idle-time check —
    for ``PATCH /embeddings`` (relay #253, v1.3.0) disabling the feature: if
    you're explicitly turning it off, you want the ~570MB back now, not
    after ``EMBEDDING_IDLE_UNLOAD_SECONDS`` next elapses. Returns whether
    anything was actually unloaded."""
    if _backend is None:
        return False
    _do_unload()
    return True


def _do_unload() -> None:
    """The actual reclaim, shared by ``unload_if_idle`` and ``force_unload``
    — only the trigger condition differs between them."""
    global _backend
    _backend = None
    gc.collect()
    try:
        ctypes.CDLL("libc.so.6").malloc_trim(0)
    except Exception:
        logger.debug("malloc_trim unavailable on this platform — unloaded anyway", exc_info=True)
