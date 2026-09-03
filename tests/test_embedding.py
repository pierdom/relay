from __future__ import annotations

import numpy as np

from relay import embedding
from relay.config import settings
from relay.embedding import EMBEDDING_DIM, FakeBackend, FastEmbedBackend


def test_fake_backend_is_deterministic():
    backend = FakeBackend()
    assert backend.embed_query("hello") == backend.embed_query("hello")


def test_fake_backend_differs_by_text():
    backend = FakeBackend()
    assert backend.embed_query("hello") != backend.embed_query("goodbye")


def test_fake_backend_dim_matches_fixed_schema():
    backend = FakeBackend()
    assert backend.dim == EMBEDDING_DIM
    assert len(backend.embed_query("x")) == EMBEDDING_DIM
    assert all(len(v) == EMBEDDING_DIM for v in backend.embed_documents(["a", "b"]))


def test_fake_backend_vectors_are_unit_normalized():
    backend = FakeBackend()
    v = backend.embed_query("some text")
    norm = sum(x * x for x in v) ** 0.5
    assert abs(norm - 1.0) < 1e-9


def _stub_backend(model_id: str, captured: list[list[str]]) -> FastEmbedBackend:
    def fake_embed(texts):
        captured.append(list(texts))
        return iter([np.zeros(4) for _ in texts])

    backend = FastEmbedBackend.__new__(FastEmbedBackend)
    backend.model_id = model_id
    backend._e5_prefixes = "e5" in model_id.lower()

    class _StubModel:
        embed = staticmethod(fake_embed)

    backend._model = _StubModel()
    return backend


def test_fastembed_backend_applies_e5_prefixes_for_e5_models():
    """e5 models require distinct query:/passage: prefixes (relay #253's
    explicit gotcha) — verified against the underlying TextEmbedding.embed
    call without loading a real model."""
    captured: list[list[str]] = []
    backend = _stub_backend("intfloat/multilingual-e5-large", captured)

    backend.embed_query("what is x")
    backend.embed_documents(["doc one", "doc two"])

    assert captured[0] == ["query: what is x"]
    assert captured[1] == ["passage: doc one", "passage: doc two"]


def test_fastembed_backend_skips_prefixes_for_non_e5_models():
    """A model not trained with the e5 convention (the current default —
    e5-small isn't in fastembed's registry) must NOT get query:/passage:
    prefixes forced onto it; that would degrade quality just as omitting them
    does for a model that actually needs them."""
    captured: list[list[str]] = []
    backend = _stub_backend("sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2", captured)

    backend.embed_query("what is x")
    backend.embed_documents(["doc one", "doc two"])

    assert captured[0] == ["what is x"]
    assert captured[1] == ["doc one", "doc two"]


def test_fastembed_backend_passes_cache_dir_and_threads(monkeypatch, tmp_path):
    """Both must reach the real TextEmbedding constructor — cache_dir per the
    relay #253 production incidents (unwritable HOME under the container's
    UID), threads per the post-v1.1.1 memory-footprint fix (onnxruntime's own
    default thread count carries real memory cost, and embedding here is
    inherently sequential — no parallelism to lose by capping it)."""
    captured: dict = {}

    class _StubTextEmbedding:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr(settings, "vault_path", str(tmp_path))
    monkeypatch.setattr(settings, "embedding_threads", 1)
    monkeypatch.setattr("fastembed.TextEmbedding", _StubTextEmbedding)

    FastEmbedBackend()

    assert captured["cache_dir"] == settings.embedding_cache_dir
    assert captured["threads"] == 1


# ── get_backend / unload_if_idle (relay #253's memory-footprint fix) ────────
#
# _backend/_last_used are module globals — every test sets them explicitly
# rather than relying on get_backend()'s real construction path (no real
# model load in this file), and restores them afterward so tests don't leak
# state into each other.


def _reset_module_state():
    embedding._backend = None
    embedding._last_used = 0.0


def test_get_backend_updates_last_used_without_reconstructing(monkeypatch):
    """A pre-loaded backend must be reused (not rebuilt) — only its
    last-used timestamp moves, which is exactly what should reset the idle
    clock on real usage."""
    _reset_module_state()
    try:
        sentinel = object()
        embedding._backend = sentinel
        clock = iter([100.0])
        monkeypatch.setattr(embedding.time, "monotonic", lambda: next(clock))

        result = embedding.get_backend()

        assert result is sentinel  # not reconstructed
        assert embedding._last_used == 100.0
    finally:
        _reset_module_state()


def test_unload_if_idle_noop_when_nothing_loaded():
    _reset_module_state()
    try:
        assert embedding.unload_if_idle() is False
    finally:
        _reset_module_state()


def test_unload_if_idle_noop_when_disabled(monkeypatch):
    _reset_module_state()
    try:
        embedding._backend = object()
        embedding._last_used = 0.0
        monkeypatch.setattr(settings, "embedding_idle_unload_seconds", 0)
        monkeypatch.setattr(embedding.time, "monotonic", lambda: 10_000.0)

        assert embedding.unload_if_idle() is False
        assert embedding._backend is not None
    finally:
        _reset_module_state()


def test_unload_if_idle_noop_before_the_threshold(monkeypatch):
    _reset_module_state()
    try:
        embedding._backend = object()
        embedding._last_used = 100.0
        monkeypatch.setattr(settings, "embedding_idle_unload_seconds", 300)
        monkeypatch.setattr(embedding.time, "monotonic", lambda: 100.0 + 299)

        assert embedding.unload_if_idle() is False
        assert embedding._backend is not None
    finally:
        _reset_module_state()


def test_unload_if_idle_unloads_past_the_threshold(monkeypatch):
    _reset_module_state()
    try:
        embedding._backend = object()
        embedding._last_used = 100.0
        monkeypatch.setattr(settings, "embedding_idle_unload_seconds", 300)
        monkeypatch.setattr(embedding.time, "monotonic", lambda: 100.0 + 300)

        assert embedding.unload_if_idle() is True
        assert embedding._backend is None
    finally:
        _reset_module_state()
