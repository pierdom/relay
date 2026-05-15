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

    @classmethod
    def from_row(cls, row) -> PostResponse:
        return cls(
            id=row["id"],
            title=row["title"],
            content=row["content"],
            format=row["format"],
            tags=[t for t in row["tags"].split(",") if t],
            source=row["source"],
            created_at=row["created_at"],
        )


class PostListResponse(BaseModel):
    items: list[PostResponse]
    total: int
    limit: int
    offset: int


class TagCount(BaseModel):
    tag: str
    count: int


class TagListResponse(BaseModel):
    tags: list[TagCount]


class TagConfigCreate(BaseModel):
    ttl_hours: int = Field(gt=0)


class TagConfigResponse(BaseModel):
    tag: str
    ttl_hours: int
