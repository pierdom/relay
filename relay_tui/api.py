from __future__ import annotations

import os
from dataclasses import dataclass

import requests

from relay.config import settings

_UNSET = object()

# ── Data classes ──────────────────────────────────────────────────────────────


@dataclass
class Post:
    id: int
    title: str
    content: str
    tags: list[str]
    source: str | None
    created_at: str
    updated_at: str | None = None
    expires_at: str | None = None
    updated_by: str | None = None
    # The version token a later edit sends back as `if_match`.
    etag: str = ""

    @classmethod
    def from_dict(cls, d: dict) -> Post:
        return cls(
            id=d["id"],
            title=d.get("title") or "",
            content=d.get("content", ""),
            tags=d.get("tags", []),
            source=d.get("source"),
            created_at=d.get("created_at", ""),
            updated_at=d.get("updated_at"),
            expires_at=d.get("expires_at"),
            updated_by=d.get("updated_by"),
            etag=d.get("etag") or "",
        )


@dataclass
class Tag:
    name: str
    count: int
    # The tag's expiry/retention config (relay #198, B-2), so the config
    # modal opens on what is set: saving replaces the whole config.
    ttl_hours: int | None = None
    expires_at: str | None = None
    keep_last: int | None = None


def _tags(data: dict) -> list[Tag]:
    return [
        Tag(name=t["tag"], count=t["count"], ttl_hours=t.get("ttl_hours"),
            expires_at=t.get("expires_at"), keep_last=t.get("keep_last"))
        for t in data.get("tags", [])
    ]


@dataclass
class Attachment:
    filename: str
    folder: str
    bytes: int
    ref: str


@dataclass
class Revision:
    sha: str
    short_sha: str
    when: str
    message: str
    path: str


@dataclass
class RevisionContent:
    sha: str
    short_sha: str
    when: str
    message: str
    title: str
    content: str
    tags: list[str]
    source: str | None


@dataclass
class DeletedPost:
    id: int
    title: str
    sha: str
    short_sha: str
    when: str
    reason: str  # "deleted" | "external" | "expiry"
    path: str


@dataclass
class CallerScope:
    """What the local key can do (relay #198, B-8) — the TUI's own mirror of
    GET /status's `caller` field."""

    mode: str  # "full" | "read" | "write"
    tags: list[str] | None = None


class Conflict(Exception):
    """A save's ``if_match`` no longer matched: the post changed since the
    editor opened. ``current`` is the post as it is now."""

    def __init__(self, current: Post | None) -> None:
        super().__init__("This post changed since you opened it")
        self.current = current


# ── Helpers ───────────────────────────────────────────────────────────────────


def _headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {settings.api_key}"}


def _base() -> str:
    return settings.relay_base_url.rstrip("/")


# ── API functions ─────────────────────────────────────────────────────────────


def list_posts(
    tag: str | None = None,
    folder: str | None = None,
    limit: int = 50,
    offset: int = 0,
    search: str | None = None,
    sort: str = "updated",
    order: str = "desc",
    author: str | None = None,
    mode: str = "keyword",
) -> tuple[list[Post], int, Post | None]:
    params: dict[str, object] = {"limit": limit, "offset": offset, "sort": sort, "order": order}
    if tag is not None:
        params["tag"] = tag
    if author is not None:
        params["author"] = author
    if search is not None and mode != "keyword":
        params["mode"] = mode
    if folder is not None:
        params["folder"] = folder
    if search is not None:
        params["search"] = search
    resp = requests.get(
        f"{_base()}/posts",
        headers=_headers(),
        params=params,
        timeout=10,
    )
    resp.raise_for_status()
    data = resp.json()
    posts = [Post.from_dict(item) for item in data.get("items", [])]
    total = data.get("total", len(posts))
    pinned = Post.from_dict(data["pinned"]) if data.get("pinned") else None
    return posts, total, pinned


def create_post(
    content: str,
    title: str,
    tags: list[str] | None = None,
    source: str | None = None,
    expires_at: str | None = None,
) -> Post:
    body: dict[str, object] = {"content": content, "title": title}
    if tags is not None:
        body["tags"] = tags
    if source is not None:
        body["source"] = source
    if expires_at is not None:
        body["expires_at"] = expires_at
    resp = requests.post(
        f"{_base()}/posts",
        headers=_headers(),
        json=body,
        timeout=10,
    )
    resp.raise_for_status()
    return Post.from_dict(resp.json())


def update_post(
    post_id: int,
    *,
    content: str | None = None,
    title: str | None = None,
    tags: list[str] | None = None,
    source: str | None = None,
    expires_at: object = _UNSET,
    if_match: str | None = None,
) -> Post:
    """PATCH a post. With ``if_match`` (the etag the editor opened on), a post
    changed meanwhile raises `Conflict` instead of being overwritten."""
    body: dict[str, object] = {}
    if content is not None:
        body["content"] = content
    if title is not None:
        body["title"] = title
    if tags is not None:
        body["tags"] = tags
    if source is not None:
        body["source"] = source
    if expires_at is not _UNSET:
        body["expires_at"] = expires_at
    if if_match:
        body["if_match"] = if_match
    resp = requests.patch(
        f"{_base()}/posts/{post_id}",
        headers=_headers(),
        json=body,
        timeout=10,
    )
    if resp.status_code == 409:
        detail = resp.json().get("detail") or {}
        current = detail.get("current") if isinstance(detail, dict) else None
        raise Conflict(Post.from_dict(current) if current else None)
    resp.raise_for_status()
    return Post.from_dict(resp.json())


def get_post(post_id: int) -> Post:
    resp = requests.get(f"{_base()}/posts/{post_id}", headers=_headers(), timeout=10)
    resp.raise_for_status()
    return Post.from_dict(resp.json())


def link_targets() -> list[tuple[int, str]]:
    """(id, title) for every post — for resolving wikilinks and the link picker."""
    resp = requests.get(f"{_base()}/links", headers=_headers(), timeout=10)
    resp.raise_for_status()
    return [(i["id"], i["title"]) for i in resp.json().get("items", [])]


def get_backlinks(post_id: int) -> list[tuple[int, str]]:
    """Posts that link to ``post_id`` — list of (id, title)."""
    resp = requests.get(f"{_base()}/posts/{post_id}/backlinks", headers=_headers(), timeout=10)
    resp.raise_for_status()
    return [(i["id"], i["title"]) for i in resp.json().get("items", [])]


def delete_post(post_id: int) -> None:
    resp = requests.delete(
        f"{_base()}/posts/{post_id}",
        headers=_headers(),
        timeout=10,
    )
    resp.raise_for_status()


@dataclass
class ServerStatus:
    """What the TUI needs from GET /status: the key's own scope, and whether
    deletes are undoable (history) and semantic search is on."""

    caller: CallerScope
    history: bool = False
    embeddings: bool = False


def get_status() -> ServerStatus:
    resp = requests.get(f"{_base()}/status", headers=_headers(), timeout=10)
    resp.raise_for_status()
    data = resp.json()
    caller = data.get("caller")
    scope = CallerScope(mode=caller["mode"], tags=caller.get("tags")) if caller else CallerScope(mode="full")
    features = data.get("features") or {}
    return ServerStatus(
        caller=scope,
        history=bool((features.get("history") or {}).get("effective")),
        embeddings=bool((features.get("search") or {}).get("embeddings")),
    )


def list_tags() -> list[Tag]:
    resp = requests.get(
        f"{_base()}/tags",
        headers=_headers(),
        timeout=10,
    )
    resp.raise_for_status()
    data = resp.json()
    return _tags(data)


def list_folders() -> list[tuple[str, int]]:
    """First-level vault folders with counts, for the sidebar tree view."""
    resp = requests.get(f"{_base()}/folders", headers=_headers(), timeout=10)
    resp.raise_for_status()
    return [(f["folder"], f["count"]) for f in resp.json().get("folders", [])]


def list_attachments(folder: str | None = None, post_id: int | None = None) -> list[Attachment]:
    params: dict[str, object] = {}
    if folder is not None:
        params["folder"] = folder
    if post_id is not None:
        params["post_id"] = post_id
    resp = requests.get(f"{_base()}/attachments", params=params, headers=_headers(), timeout=10)
    resp.raise_for_status()
    return [
        Attachment(filename=i["filename"], folder=i["folder"], bytes=i["bytes"], ref=i["ref"])
        for i in resp.json().get("items", [])
    ]


def delete_attachment(name: str) -> dict:
    from urllib.parse import quote

    resp = requests.delete(f"{_base()}/attachments/{quote(name)}", headers=_headers(), timeout=10)
    resp.raise_for_status()
    return resp.json()


def attachment_url(folder: str, filename: str) -> str:
    from urllib.parse import quote

    return f"{_base()}/attachments/{quote(folder)}/assets/{quote(filename)}"


def rename_tag(old: str, new: str) -> list[Tag]:
    resp = requests.patch(
        f"{_base()}/tags/{old}",
        headers=_headers(),
        json={"new_name": new},
        timeout=10,
    )
    resp.raise_for_status()
    return _tags(resp.json())


def set_tag_config(
    tag: str,
    ttl_hours: int | None = None,
    expires_at: str | None = None,
    keep_last: int | None = None,
) -> None:
    """Replace the tag's whole config; all None removes it."""
    body: dict[str, object] = {}
    if ttl_hours is not None:
        body["ttl_hours"] = ttl_hours
    if expires_at is not None:
        body["expires_at"] = expires_at
    if keep_last is not None:
        body["keep_last"] = keep_last
    resp = requests.post(
        f"{_base()}/tags/{tag}/config",
        headers=_headers(),
        json=body,
        timeout=10,
    )
    resp.raise_for_status()


def get_post_history(post_id: int, limit: int = 20) -> tuple[list[Revision], bool]:
    resp = requests.get(
        f"{_base()}/posts/{post_id}/history",
        headers=_headers(),
        params={"limit": limit},
        timeout=10,
    )
    resp.raise_for_status()
    data = resp.json()
    revisions = [
        Revision(
            sha=r["sha"],
            short_sha=r["short_sha"],
            when=r["when"],
            message=r["message"],
            path=r["path"],
        )
        for r in data.get("items", [])
    ]
    return revisions, data.get("exists", True)


def get_post_revision(post_id: int, sha: str) -> RevisionContent:
    resp = requests.get(
        f"{_base()}/posts/{post_id}/history/{sha}",
        headers=_headers(),
        timeout=10,
    )
    resp.raise_for_status()
    d = resp.json()
    return RevisionContent(
        sha=d["sha"],
        short_sha=d["short_sha"],
        when=d["when"],
        message=d["message"],
        title=d["title"],
        content=d.get("content", ""),
        tags=d.get("tags", []),
        source=d.get("source"),
    )


def restore_post(post_id: int, sha: str) -> Post:
    resp = requests.post(
        f"{_base()}/posts/{post_id}/restore",
        headers=_headers(),
        json={"sha": sha},
        timeout=15,
    )
    resp.raise_for_status()
    return Post.from_dict(resp.json())


def list_deleted_posts(limit: int = 50, include_expiry: bool = False) -> list[DeletedPost]:
    params: dict[str, object] = {"limit": limit}
    if include_expiry:
        params["include_expiry"] = "true"
    resp = requests.get(
        f"{_base()}/posts/deleted",
        headers=_headers(),
        params=params,
        timeout=10,
    )
    resp.raise_for_status()
    data = resp.json()
    return [
        DeletedPost(
            id=i["id"],
            title=i["title"],
            sha=i["sha"],
            short_sha=i["short_sha"],
            when=i["when"],
            reason=i["reason"],
            path=i["path"],
        )
        for i in data.get("items", [])
    ]


def get_status_report() -> dict:
    """GET /status as it comes — the status panel shows most of it."""
    resp = requests.get(f"{_base()}/status", headers=_headers(), timeout=10)
    resp.raise_for_status()
    return resp.json()


def list_changes(limit: int = 12) -> list[dict]:
    """Recent vault changes, newest first: {seq, id, title, action, when, sha, author}."""
    resp = requests.get(f"{_base()}/changes", headers=_headers(), params={"limit": limit}, timeout=10)
    resp.raise_for_status()
    return resp.json().get("items", [])


def get_lint() -> dict:
    """GET /lint: {items: [{rule, severity, post_id, title, detail, match}], checked_posts, skipped_rules}."""
    resp = requests.get(f"{_base()}/lint", headers=_headers(), timeout=30)
    resp.raise_for_status()
    return resp.json()


def get_related(post_id: int) -> list[tuple[int, str]]:
    """Similar posts this one doesn't link either way — (id, title). The server
    answers 503 with semantic search off; callers ask only when it is on."""
    resp = requests.get(f"{_base()}/posts/{post_id}/related", headers=_headers(), timeout=20)
    resp.raise_for_status()
    return [(i["id"], i["title"]) for i in resp.json().get("items", [])]


def set_embeddings_enabled(enabled: bool) -> dict:
    resp = requests.patch(f"{_base()}/embeddings", headers=_headers(), json={"enabled": enabled}, timeout=30)
    resp.raise_for_status()
    return resp.json()


def run_embedding_backfill() -> dict:
    resp = requests.post(f"{_base()}/embeddings/backfill", headers=_headers(), timeout=30)
    resp.raise_for_status()
    return resp.json()


def upload_attachment(path: str, *, post_id: int | None = None, tags: list[str] | None = None) -> dict:
    """Upload a local file through a presigned slot (the bytes stream, never
    base64) and return the server's {filename, ref, folder, …}. With
    ``post_id`` it files beside that post; with ``tags`` (a new post) where the
    post will file. The caller inserts ``ref`` itself (``embed`` is false)."""
    slot = requests.post(f"{_base()}/attachments/uploads", headers=_headers(), timeout=10)
    slot.raise_for_status()
    upload_id = slot.json()["upload_id"]
    with open(path, "rb") as fh:
        put = requests.put(f"{_base()}/attachments/uploads/{upload_id}", headers=_headers(), data=fh, timeout=300)
    _raise_with_detail(put)
    body: dict[str, object] = {"upload_id": upload_id, "filename": os.path.basename(path), "embed": False}
    if post_id is not None:
        body["post_id"] = post_id
    elif tags:
        body["tags"] = tags
    resp = requests.post(f"{_base()}/attachments", headers=_headers(), json=body, timeout=30)
    _raise_with_detail(resp)
    return resp.json()


def _raise_with_detail(resp: requests.Response) -> None:
    """raise_for_status, carrying the server's own `detail` (a size cap, a
    scope refusal) instead of a bare status line."""
    if resp.ok:
        return
    try:
        detail = resp.json().get("detail")
    except ValueError:
        detail = None
    if detail:
        raise RuntimeError(detail if isinstance(detail, str) else str(detail))
    resp.raise_for_status()
