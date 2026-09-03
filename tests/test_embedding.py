from __future__ import annotations

import numpy as np

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
