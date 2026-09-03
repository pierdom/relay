from __future__ import annotations

import re

from pydantic import BaseModel, Field, field_validator, model_validator


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


class SearchTiming(BaseModel):
    """Cold-start observability for a ranked (semantic/hybrid) search (relay
    #253 usage report, Issue 5): with the embedding model idle-unloaded
    (``EMBEDDING_IDLE_UNLOAD_SECONDS``), the first ranked query after a quiet
    period pays a multi-hundred-MB model reload plus the query embed itself,
    indistinguishable from a slow vault without this. Only present on
    ``mode="semantic"``/``"hybrid"`` responses — plain keyword search never
    touches the embedding backend."""

    cold_start: bool = Field(description="Whether this query triggered a model load, vs. finding it already resident")
    embedding_ms: float = Field(description="Wall-clock time spent embedding the query (plus the model load, if cold)")


class PostListResponse(BaseModel):
    items: list[PostResponse]
    total: int
    limit: int
    offset: int
    pinned: PostResponse | None = None  # master doc, on the home feed's first page
    search_timing: SearchTiming | None = None  # set only for mode="semantic"/"hybrid"


_FRONTMATTER_RE = re.compile(r"\A---\r?\n.*?\r?\n---\r?\n", re.DOTALL)
_FENCED_CODE_RE = re.compile(r"```.*?```", re.DOTALL)
_IMAGE_EMBED_RE = re.compile(r"!\[\[[^\]]*\]\]")
_IMAGE_MD_RE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_LINK_MD_RE = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_WIKILINK_RE = re.compile(r"\[\[(?:[^\]|]*\|)?([^\]]*)\]\]")
# Emphasis/heading/quote/code markers. Underscores are left alone on purpose —
# this vault is full of snake_case identifiers (RELAY_VAULT_PATH, tag names) that
# an excerpt shouldn't mangle; literal `_emphasis_` surviving is the lesser evil.
_MD_SYNTAX_RE = re.compile(r"[`*>#~]")


def make_excerpt(content: str, limit: int = 240) -> str:
    """A short plain-text preview of a post body for summary listings.

    Strips YAML front-matter (defensive — index content normally excludes it),
    fenced code, Obsidian/Markdown embeds and links, inline emphasis/heading
    markers and table pipes, then collapses whitespace and truncates to ~``limit``
    characters on a word boundary. Never returns markdown syntax to render.
    """
    text = _FRONTMATTER_RE.sub("", content or "")
    text = _FENCED_CODE_RE.sub(" ", text)
    text = _IMAGE_EMBED_RE.sub(" ", text)
    text = _IMAGE_MD_RE.sub(" ", text)
    text = _LINK_MD_RE.sub(r"\1", text)
    text = _WIKILINK_RE.sub(r"\1", text)
    text = _MD_SYNTAX_RE.sub("", text)
    text = text.replace("|", " ")
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > limit:
        text = text[:limit].rsplit(" ", 1)[0].rstrip() + "…"
    return text


class PostSummary(BaseModel):
    """Metadata-only view of a post: everything but the full body, plus a short
    plain-text ``excerpt``. Lets agents dedupe/browse without pulling every body
    into context. See ``make_excerpt``; ``folder`` is derived from the vault path."""

    id: int
    title: str
    tags: list[str]
    source: str | None
    folder: str
    excerpt: str
    created_at: str
    updated_at: str | None = None
    expires_at: str | None = None

    @classmethod
    def from_row(cls, row) -> PostSummary:
        keys = row.keys()
        path = row["path"] if "path" in keys else ""
        return cls(
            id=row["id"],
            title=row["title"],
            tags=[t for t in row["tags"].split(",") if t],
            source=row["source"],
            folder=path.split("/", 1)[0] if "/" in path else "",
            excerpt=make_excerpt(row["content"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"] if "updated_at" in keys else None,
            expires_at=row["expires_at"] if "expires_at" in keys else None,
        )


class PostSummaryListResponse(BaseModel):
    items: list[PostSummary]
    total: int
    limit: int
    offset: int
    pinned: PostSummary | None = None  # master doc, on the home feed's first page
    search_timing: SearchTiming | None = None  # set only for mode="semantic"/"hybrid"


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
    # Exactly one byte source: inline base64 (`data`), a URL the server fetches
    # (`source_url`), or a presigned upload slot already filled (`upload_id`).
    filename: str | None = None  # required for data/upload_id; optional for source_url (derived from the response)
    data: str | None = Field(default=None, description="Base64-encoded file bytes")
    source_url: str | None = None  # server fetches the bytes from this http(s) URL
    upload_id: str | None = None   # id of a filled presigned upload slot
    post_id: int | None = None  # attach to this post (file under its folder)
    folder: str | None = None   # explicit first-level folder for a standalone attachment
    tags: list[str] = Field(default_factory=list)  # derive the folder from these (compose)
    embed: bool = True          # with post_id, also append ![[file]] to its body

    @field_validator("tags")
    @classmethod
    def clean_tags(cls, v: list[str]) -> list[str]:
        return _clean_tag_list(v)

    @field_validator("filename")
    @classmethod
    def filename_not_blank(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v = v.strip()
        return v or None

    @model_validator(mode="after")
    def one_source(self) -> AttachmentCreate:
        sources = [s for s in (self.data, self.source_url, self.upload_id) if s]
        if len(sources) != 1:
            raise ValueError("provide exactly one of: data, source_url, upload_id")
        # Only source_url can derive a name (from Content-Disposition / URL path);
        # data and upload_id carry no name, so require an explicit filename.
        if (self.data is not None or self.upload_id is not None) and not self.filename:
            raise ValueError("filename is required with data or upload_id")
        return self


class UploadSlotResponse(BaseModel):
    upload_id: str
    upload_url: str          # PUT the raw bytes here (out-of-band, not through the model)
    method: str = "PUT"
    max_bytes: int
    expires_at: str          # ISO 8601; slot is purged after this


class UploadStatusResponse(BaseModel):
    upload_id: str
    bytes: int
    ready: bool


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


class AttachmentDeleteResponse(BaseModel):
    filename: str
    referenced_by: list[int]  # post ids that still embed/link this file (now dangling)


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


class VaultStatus(BaseModel):
    path: str
    posts: int
    tags: int
    folders: int
    attachments: int
    attachment_bytes: int


class HistoryStatus(BaseModel):
    enabled: bool = Field(description="RELAY_HISTORY_ENABLED")
    git: str | None = Field(description="git version string, or null when the binary is missing")
    effective: bool = Field(description="Whether a write would actually be recorded")


class SearchStatus(BaseModel):
    fts5: bool = Field(description="False means search fell back to LIKE substring matching")
    embeddings: bool = Field(
        description=(
            "Whether mode='semantic'/'hybrid' is available (relay #253, proof of concept, off by "
            "default) — sqlite-vec loaded and RELAY_EMBEDDING_ENABLED=true"
        )
    )


class WatcherStatus(BaseModel):
    enabled: bool
    running: bool


class AuthStatus(BaseModel):
    oidc: bool
    mcp_oauth: bool = Field(description="True only when the flag *and* an OIDC client are set")


class FeatureStatus(BaseModel):
    history: HistoryStatus
    search: SearchStatus
    watcher: WatcherStatus
    auth: AuthStatus


class EmbeddingBackfillStatus(BaseModel):
    running: bool = Field(description="Whether the startup backfill task is active right now")
    checked: int = Field(description="Posts checked so far in the current or most recent run")
    total: int = Field(description="Posts to check in the current or most recent run")
    started_at: str | None = Field(description="Null if no backfill has run yet this process lifetime")
    completed_at: str | None = Field(description="Null while running, or before the first run has finished")


class EmbeddingToggle(BaseModel):
    enabled: bool = Field(description="Turn semantic/hybrid search on or off at runtime, without a restart")


class EmbeddingStatus(BaseModel):
    enabled: bool = Field(
        description=(
            "RELAY_EMBEDDING_ENABLED's current value — its live, in-memory state, which PATCH "
            "/embeddings can change; a restart reverts to whatever .env says"
        )
    )
    available: bool = Field(description="Same as features.search.embeddings: sqlite-vec loaded and enabled")
    model: str | None = Field(description="EMBEDDING_MODEL; null when embeddings aren't available")
    dimension: int | None = Field(description="The configured model's vector width")
    model_size_mb: float | None = Field(description="On-disk size of the model, from fastembed's registry")
    backend_loaded: bool = Field(
        description="Whether the model is currently resident in memory, or unloaded (EMBEDDING_IDLE_UNLOAD_SECONDS)"
    )
    idle_unload_seconds: int = Field(description="EMBEDDING_IDLE_UNLOAD_SECONDS; 0 = never unload")
    threads: int = Field(description="EMBEDDING_THREADS")
    posts_total: int
    posts_embedded: int = Field(description="Posts with at least one chunk currently embedded")
    posts_missing: int = Field(description="posts_total minus posts_embedded")
    posts_missing_ids: list[int] = Field(
        default_factory=list,
        description=(
            "Up to 50 ids behind posts_missing. The only way a post lands here while backfill runs "
            "cleanly is chunking.chunk_post producing zero chunks for it — e.g. a body that's "
            "entirely a fenced code block, stripped before chunking ever sees it"
        ),
    )
    chunks_total: int
    cache_entries: int = Field(
        description="Rows in the embedding cache; can exceed chunks_total (old model/deleted-post rows aren't pruned)"
    )
    backfill: EmbeddingBackfillStatus


class StatusResponse(BaseModel):
    version: str
    uptime_seconds: int
    started_at: str | None
    sse_clients: int
    vault: VaultStatus
    features: FeatureStatus
    embeddings: EmbeddingStatus


class PostRevision(BaseModel):
    """One commit in a post's history."""

    sha: str
    short_sha: str
    when: str
    message: str
    path: str


class PostRevisionContent(BaseModel):
    """A post as it was at one revision — the payload behind a restore preview."""

    id: int
    sha: str
    short_sha: str
    when: str
    message: str
    path: str
    title: str
    content: str
    tags: list[str]
    source: str | None


class PostHistoryResponse(BaseModel):
    id: int
    exists: bool = Field(description="False when the post is deleted but recoverable")
    items: list[PostRevision]


class DeletedPost(BaseModel):
    """A post whose file is gone but which history can still put back."""

    id: int
    title: str
    sha: str = Field(description="Commit that removed it — pass to /restore")
    short_sha: str
    when: str
    reason: str = Field(description='"deleted" (API/UI), "expiry" (TTL) or "external" (Obsidian)')
    path: str


class DeletedPostsResponse(BaseModel):
    items: list[DeletedPost]


class PostRestore(BaseModel):
    sha: str = Field(min_length=4, description="Revision to restore, from GET /posts/{id}/history")


class TagConfigCreate(BaseModel):
    ttl_hours: int | None = Field(default=None, gt=0)
    expires_at: str | None = None


class TagConfigResponse(BaseModel):
    tag: str
    ttl_hours: int | None
    expires_at: str | None = None
