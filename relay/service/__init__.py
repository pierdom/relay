"""Shared business logic for posts, history, attachments and tags.

Both the REST routes (``relay.routes.*``) and the in-process MCP server
(``relay.mcp_server``) call into this layer. Writes go file-first through
``relay.vault`` (canonical), then mirror into the SQLite index; reads are served
straight from the index. Every function takes an open ``aiosqlite`` connection
with ``row_factory = aiosqlite.Row``.

One module per seam — ``posts``, ``revisions``, ``attachments``, ``tags`` — with
the public names re-exported here so callers keep writing ``service.create_post``.
"""
from __future__ import annotations

from ._common import (
    AttachmentError,
    AttachmentSourceError,
    HistoryUnavailable,
    InvalidFolder,
    InvalidSearchMode,
    InvalidTag,
    PostNotFound,
    ProtectedPost,
    RevisionNotFound,
    SemanticSearchUnavailable,
)
from .attachments import (
    add_attachment,
    create_upload_slot,
    decode_attachment_b64,
    delete_attachment,
    ingest_attachment,
    list_attachments,
    referenced_attachment_names,
)
from .posts import (
    _RANKED_POOL_CAP,
    create_post,
    delete_post,
    get_backlinks,
    get_post,
    link_index,
    list_posts,
    update_post,
)
from .revisions import get_post_history, get_post_revision, list_deleted_posts, restore_post
from .tags import list_folders, list_tags, rename_tag, set_tag_config

__all__ = [
    "AttachmentError", "AttachmentSourceError", "HistoryUnavailable", "InvalidFolder", "InvalidSearchMode",
    "InvalidTag", "PostNotFound", "ProtectedPost", "RevisionNotFound", "SemanticSearchUnavailable",
    "add_attachment", "create_upload_slot", "decode_attachment_b64", "delete_attachment", "ingest_attachment",
    "list_attachments", "referenced_attachment_names",
    "_RANKED_POOL_CAP", "create_post", "delete_post", "get_backlinks", "get_post", "link_index", "list_posts",
    "update_post",
    "get_post_history", "get_post_revision", "list_deleted_posts", "restore_post",
    "list_folders", "list_tags", "rename_tag", "set_tag_config",
]
