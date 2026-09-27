"""Errors a caller can act on, shared by every surface.

Each carries its HTTP status and a default message. REST maps them in one
exception handler (``main.py``) and MCP in one decorator (``mcp_server._tool``),
so a route or tool never translates them by hand. A leaf module on purpose:
``changes``/``status`` sit below ``relay.service`` and raise these too.
"""
from __future__ import annotations


class ServiceError(Exception):
    status = 400
    message = "Invalid request"

    def __init__(self, message: str | None = None) -> None:
        super().__init__(message or self.message)

    @property
    def detail(self) -> str | dict:
        """The REST ``detail`` / MCP error payload."""
        return str(self)


class PostNotFound(ServiceError):
    status, message = 404, "Post not found"


class ProtectedPost(ServiceError):
    status, message = 403, "Master document (id=0) cannot be deleted"


class ConcurrentModification(ServiceError):
    """A write's ``if_match`` no longer matches the post (relay #198, N-1).
    Carries the post's current state so the caller can rebase."""

    status = 409

    def __init__(self, post_id: int, current: dict | None) -> None:
        super().__init__(f"post #{post_id} has changed since if_match was captured")
        self.current = current

    @property
    def detail(self) -> dict:
        return {"error": str(self), "current": self.current}


class EditNoChange(ServiceError):
    status, message = 422, "new_str must be different from old_str"


class EditTextNotFound(ServiceError):
    status, message = 422, "old_str not found in the post's content"


class EditTextNotUnique(ServiceError):
    """``old_str`` matches more than once — add context to disambiguate."""

    status = 422

    def __init__(self, count: int) -> None:
        super().__init__(f"old_str matches {count} times; must match exactly once")
        self.count = count


class RevisionNotFound(ServiceError):
    status, message = 404, "No such revision in this post's history"


class HistoryUnavailable(ServiceError):
    status, message = 503, "Vault history is disabled or git is unavailable"


class SemanticSearchUnavailable(ServiceError):
    """mode=semantic/hybrid (or related posts) asked of a relay without
    embeddings. Loud on purpose: an empty result would read as "no matches"."""

    status, message = 503, "Semantic search is not enabled on this relay"


class InvalidSearchMode(ServiceError):
    status, message = 422, "mode must be 'keyword', 'semantic', or 'hybrid'"


class AttachmentError(ServiceError):
    status, message = 413, "Attachment is empty or too large"


class AttachmentSourceError(ServiceError):
    """The attachment's bytes could not be obtained (bad base64, a failed
    source_url fetch, an unknown/expired/unfilled upload slot)."""


class InvalidTag(ServiceError):
    """A tag that normalises to nothing (``"!!"``) — AUDIT.md B-08."""

    status, message = 422, "tag must contain at least one letter, digit, hyphen or underscore"


class InvalidFolder(ServiceError):
    """Not a plain first-level folder name — see ``folders.is_valid_name``."""


class ScopeDenied(ServiceError):
    """The key's scope forbids this write (relay #198, B-8): a tag-restricted
    key outside its tags, or any non-full key on a vault-wide operation. The
    coarse read-only gate lives in ``auth.require_api_key`` / MCP's
    ``AuthMiddleware``; this is the finer, per-post check."""

    status, message = 403, "This API key's scope does not permit this write"


class EmbeddingsUnavailable(ServiceError):
    status, message = 503, "Semantic search is not enabled on this relay"


class BackfillAlreadyRunning(ServiceError):
    status, message = 409, "A backfill is already running"


class EmbeddingDimensionMismatch(ServiceError):
    """Enabling live can't rebuild ``vec_chunks`` under in-flight reads; that
    migration only runs at startup (``vectors.init_vec``)."""

    status = 409
    message = (
        "EMBEDDING_MODEL's dimension doesn't match the vector schema already on disk. "
        "Restart relay to rebuild it before enabling."
    )
