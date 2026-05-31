from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator

FormatEnum = Literal["markdown", "text", "html", "json"]


class PostCreate(BaseModel):
    title: str | None = None
    content: str
    format: FormatEnum = "markdown"
    tags: list[str] = Field(default_factory=list)
    source: str | None = None
    expires_at: str | None = None

    @field_validator("tags")
    @classmethod
    def clean_tags(cls, v: list[str]) -> list[str]:
        return [t.strip().lower() for t in v if t.strip()]


class PostResponse(BaseModel):
    id: int
    title: str | None
    content: str
    format: FormatEnum
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
            format=row["format"],
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


class PostUpdate(BaseModel):
    title: str | None = None
    content: str | None = None
    format: FormatEnum | None = None
    tags: list[str] | None = None
    source: str | None = None
    expires_at: str | None = None

    @field_validator("tags")
    @classmethod
    def clean_tags(cls, v: list[str] | None) -> list[str] | None:
        if v is None:
            return None
        return [t.strip().lower() for t in v if t.strip()]


class TagRename(BaseModel):
    new_name: str

    @field_validator("new_name")
    @classmethod
    def clean(cls, v: str) -> str:
        v = v.strip().lower()
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
