from __future__ import annotations

import numpy as np
import pytest

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


def test_resolve_dim_looks_up_the_real_fastembed_registry():
    """Pure metadata lookup — no model download, safe to run unstubbed in CI
    (relay #253's offline-CI invariant)."""
    assert embedding.resolve_dim("sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2") == 384


def test_resolve_dim_rejects_an_unknown_model():
    with pytest.raises(ValueError, match="Unknown fastembed model"):
        embedding.resolve_dim("not-a-real-model")


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


def test_fastembed_backend_dim_is_resolved_per_instance(monkeypatch, tmp_path):
    """dim used to be a fixed class constant; it's now looked up per instance
    from whichever model EMBEDDING_MODEL actually names, so two backends
    configured for differently-sized models must report different dims."""

    class _StubTextEmbedding:
        def __init__(self, **kwargs):
            pass

    monkeypatch.setattr(settings, "vault_path", str(tmp_path))
    monkeypatch.setattr("fastembed.TextEmbedding", _StubTextEmbedding)
    monkeypatch.setattr(embedding, "resolve_dim", lambda model_id: {"model-a": 4, "model-b": 8}[model_id])

    monkeypatch.setattr(settings, "embedding_model", "model-a")
    assert FastEmbedBackend().dim == 4

    monkeypatch.setattr(settings, "embedding_model", "model-b")
    assert FastEmbedBackend().dim == 8


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
    # resolve_dim looks up the real fastembed registry, which _StubTextEmbedding
    # doesn't carry — stub it directly, this test is about cache_dir/threads.
    monkeypatch.setattr(embedding, "resolve_dim", lambda model_id: EMBEDDING_DIM)

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


# ── resolve_size_mb / is_loaded (relay #253's /status embedding diagnostics) ─


def test_resolve_size_mb_looks_up_the_real_fastembed_registry():
    """Same pure-metadata guarantee as resolve_dim — no download, safe in CI."""
    mb = embedding.resolve_size_mb("sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2")
    assert 200 < mb < 250  # registry reports 0.22 GB for this model


def test_resolve_size_mb_rejects_an_unknown_model():
    with pytest.raises(ValueError, match="Unknown fastembed model"):
        embedding.resolve_size_mb("not-a-real-model")


def test_is_loaded_false_when_nothing_loaded():
    _reset_module_state()
    try:
        assert embedding.is_loaded() is False
    finally:
        _reset_module_state()


def test_is_loaded_true_when_a_backend_is_resident():
    _reset_module_state()
    try:
        embedding._backend = object()
        assert embedding.is_loaded() is True
    finally:
        _reset_module_state()
