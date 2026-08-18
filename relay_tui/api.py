from __future__ import annotations

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
        )


@dataclass
class Tag:
    name: str
    count: int


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
) -> tuple[list[Post], int, Post | None]:
    params: dict[str, object] = {"limit": limit, "offset": offset, "sort": sort, "order": order}
    if tag is not None:
        params["tag"] = tag
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
) -> Post:
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
    resp = requests.patch(
        f"{_base()}/posts/{post_id}",
        headers=_headers(),
        json=body,
        timeout=10,
    )
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


def list_tags() -> list[Tag]:
    resp = requests.get(
        f"{_base()}/tags",
        headers=_headers(),
        timeout=10,
    )
    resp.raise_for_status()
    data = resp.json()
    return [Tag(name=item["tag"], count=item["count"]) for item in data.get("tags", [])]


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
    return [Tag(name=t["tag"], count=t["count"]) for t in resp.json().get("tags", [])]


def set_tag_config(
    tag: str,
    ttl_hours: int | None = None,
    expires_at: str | None = None,
) -> None:
    body: dict[str, object] = {}
    if ttl_hours is not None:
        body["ttl_hours"] = ttl_hours
    if expires_at is not None:
        body["expires_at"] = expires_at
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
