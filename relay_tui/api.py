from __future__ import annotations

from dataclasses import dataclass, field

import requests

from relay.config import settings

_UNSET = object()

# ── Data classes ──────────────────────────────────────────────────────────────


@dataclass
class Post:
    id: int
    title: str | None
    content: str
    format: str
    tags: list[str]
    source: str | None
    created_at: str
    updated_at: str | None = None
    expires_at: str | None = None

    @classmethod
    def from_dict(cls, d: dict) -> "Post":
        return cls(
            id=d["id"],
            title=d.get("title"),
            content=d.get("content", ""),
            format=d.get("format", "markdown"),
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


# ── Helpers ───────────────────────────────────────────────────────────────────


def _headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {settings.api_key}"}


def _base() -> str:
    return settings.relay_base_url.rstrip("/")


# ── API functions ─────────────────────────────────────────────────────────────


def list_posts(
    tag: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[Post], int]:
    params: dict[str, object] = {"limit": limit, "offset": offset}
    if tag is not None:
        params["tag"] = tag
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
    return posts, total


def create_post(
    content: str,
    title: str | None = None,
    tags: list[str] | None = None,
    fmt: str = "markdown",
    source: str | None = None,
    expires_at: str | None = None,
) -> Post:
    body: dict[str, object] = {"content": content, "format": fmt}
    if title is not None:
        body["title"] = title
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
    fmt: str | None = None,
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
    if fmt is not None:
        body["format"] = fmt
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
