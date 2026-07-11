from __future__ import annotations

import re

from pydantic import BaseModel, Field, field_validator


def _clean_tag_list(v: list[str]) -> list[str]:
    cleaned = []
    for t in v:
        t = re.sub(r"[^a-z0-9_-]", "", t.strip().lower())
        if t:
            cleaned.append(t)
    return cleaned


class PostCreate(BaseModel):
    title: str = Field(min_length=1)
    content: str
    tags: list[str] = Field(default_factory=list)
    source: str | None = None
    expires_at: str | None = None

    @field_validator("title")
    @classmethod
    def title_not_blank(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("title must not be empty")
        return v

    @field_validator("tags")
    @classmethod
    def clean_tags(cls, v: list[str]) -> list[str]:
        return _clean_tag_list(v)


class PostResponse(BaseModel):
    id: int
    title: str
    content: str
    tags: list[str]
    source: str | None
    created_at: str
    updated_at: str | None = None
    expires_at: str | None = None

    @classmethod
    def from_row(cls, row) -> PostResponse:
        keys = row.keys()
        return cls(
            id=row["id"],
            title=row["title"],
            content=row["content"],
            tags=[t for t in row["tags"].split(",") if t],
            source=row["source"],
            created_at=row["created_at"],
            updated_at=row["updated_at"] if "updated_at" in keys else None,
            expires_at=row["expires_at"] if "expires_at" in keys else None,
        )


class PostListResponse(BaseModel):
    items: list[PostResponse]
    total: int
    limit: int
    offset: int
    pinned: PostResponse | None = None  # master doc, on the home feed's first page


class PostUpdate(BaseModel):
    title: str | None = None
    content: str | None = None
    tags: list[str] | None = None
    source: str | None = None
    expires_at: str | None = None

    @field_validator("title")
    @classmethod
    def title_not_blank(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v = v.strip()
        if not v:
            raise ValueError("title must not be empty")
        return v

    @field_validator("tags")
    @classmethod
    def clean_tags(cls, v: list[str] | None) -> list[str] | None:
        return None if v is None else _clean_tag_list(v)


class FolderCount(BaseModel):
    folder: str
    count: int


class FolderListResponse(BaseModel):
    folders: list[FolderCount]


class LinkTarget(BaseModel):
    id: int
    title: str


class LinkIndexResponse(BaseModel):
    items: list[LinkTarget]


class BacklinksResponse(BaseModel):
    items: list[LinkTarget]


class AttachmentCreate(BaseModel):
    filename: str = Field(min_length=1)
    data: str = Field(description="Base64-encoded file bytes")
    post_id: int | None = None  # attach to this post (file under its folder)
    folder: str | None = None   # first-level folder for a standalone attachment
    embed: bool = True          # with post_id, also append ![[file]] to its body

    @field_validator("filename")
    @classmethod
    def filename_not_blank(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("filename must not be empty")
        return v


class AttachmentResponse(BaseModel):
    filename: str            # final on-disk name (may be collision-suffixed)
    ref: str                 # Obsidian embed to drop into a post, e.g. ![[file.png]]
    folder: str              # first-level folder the assets/ dir lives under
    post_id: int | None = None  # set when the embed was appended to a post


class AttachmentInfo(BaseModel):
    filename: str
    folder: str
    bytes: int
    ref: str  # ![[filename]]


class AttachmentListResponse(BaseModel):
    items: list[AttachmentInfo]


class TagRename(BaseModel):
    new_name: str

    @field_validator("new_name")
    @classmethod
    def clean(cls, v: str) -> str:
        v = re.sub(r"[^a-z0-9_-]", "", v.strip().lower())
        if not v:
            raise ValueError("new_name must not be empty")
        return v


class TagCount(BaseModel):
    tag: str
    count: int


class TagListResponse(BaseModel):
    tags: list[TagCount]


class TagConfigCreate(BaseModel):
    ttl_hours: int | None = Field(default=None, gt=0)
    expires_at: str | None = None


class TagConfigResponse(BaseModel):
    tag: str
    ttl_hours: int | None
    expires_at: str | None = None
