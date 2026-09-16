"""In-process MCP server exposed over Streamable HTTP at ``/mcp``.

Unlike the stdio proxy in ``relay_mcp/server.py`` (which runs on the client
machine and talks to a relay over REST), this server runs *inside* the relay
process and calls the shared ``relay.service`` layer directly — no network
hop, no schema duplication. Any MCP client that supports the Streamable HTTP
transport can connect remotely with the relay's bearer key.
"""
from __future__ import annotations

import re

from fastmcp import FastMCP
from fastmcp.utilities.types import Image
from mcp.types import Icon
from pydantic import ValidationError
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from . import __version__, changes, database, lint, metrics, service, status, vault
from .auth import bearer_matches
from .config import settings
from .models import AttachmentCreate, ChangeEntry, ChangeListResponse, PostCreate, PostUpdate, TagConfigCreate


def _first_error(exc: ValidationError) -> str:
    """Flatten a pydantic ValidationError to its first human-readable message."""
    errors = exc.errors()
    if errors:
        msg = errors[0].get("msg", "invalid input")
        return msg.removeprefix("Value error, ")
    return "invalid input"

INSTRUCTIONS = (
    "Relay is a personal knowledge base kept as a plain-Markdown vault; posts are files "
    "a human also edits directly in Obsidian, so write them to be read by a person. "
    "Clients subscribe to changes in real time. Before writing, read the master document "
    "with get_post(id=0) — it holds the index, tag taxonomy, naming conventions, and "
    "house rules. Keep one canonical post per topic and update it in place rather than "
    "creating duplicates. list_folders shows how the vault is already organised."
)


def _check_oauth_not_yet_supported() -> None:
    """Phase 1 spike (relay #313): fastmcp's OAuth wiring (MultiAuth/OIDCProxy against
    PocketID, replacing the old mcp SDK's AuthSettings/auth_server_provider) is Phase 2's
    job, not this one — this branch only proves out the tool/resource/transport swap with
    the static bearer key. Failing loud here beats silently running with OAuth dark:
    MCP_OAUTH_ENABLED=true would otherwise look "on" while /mcp quietly stayed
    static-bearer-only, same class of silent-degradation bug this migration exists to
    avoid elsewhere."""
    if settings.mcp_oauth_active:
        raise RuntimeError(
            "MCP_OAUTH_ENABLED is set, but this branch (relay #313 fastmcp migration, "
            "Phase 1) hasn't ported OAuth yet — that lands in Phase 2. Disable "
            "MCP_OAUTH_ENABLED to run this branch."
        )


def _brand_icons() -> list[Icon]:
    """Advertise relay's logo in the initialize serverInfo (MCP SEP-973).

    Clients that read `serverInfo.icons` (spec 2025-11-25) show these instead of
    the generic globe. `/assets` is public (no auth), so the src URLs resolve for
    an unauthenticated fetch. Claude's remote connectors don't render this yet
    (anthropics/claude-ai-mcp#152) but other clients already do, and it's the
    spec-correct place for it — so it lights up automatically when Claude ships.
    """
    base = settings.relay_base_url.rstrip("/")
    return [
        Icon(src=f"{base}/assets/relay-mark.svg", mimeType="image/svg+xml"),
        Icon(src=f"{base}/assets/relay-mark-512.png", mimeType="image/png", sizes=["512x512"]),
    ]


_check_oauth_not_yet_supported()

mcp = FastMCP(
    "relay",
    # serverInfo.version, which a client shows next to the server's name. Left
    # unset it is the empty string; under mcp 1.x it was the *SDK's* version
    # (a 1.6.1 relay announced itself as "1.29.0"), which was never what a user
    # reading it wanted. relay's own version is.
    version=__version__,
    instructions=INSTRUCTIONS,
    website_url=settings.relay_base_url.rstrip("/"),
    icons=_brand_icons(),
)


@mcp.custom_route("/mcp/oauth/callback", methods=["GET"], include_in_schema=False)
async def mcp_oauth_callback(request: Request) -> Response:
    """Return leg of the upstream PocketID login (unauthenticated by design)."""
    from .mcp_oauth.broker import handle_callback

    return await handle_callback(request)


@mcp.custom_route("/mcp/oauth/consent", methods=["GET", "POST"], include_in_schema=False)
async def mcp_oauth_consent(request: Request) -> Response:
    """Per-client consent gate for an unapproved DCR client (relay #313
    Stopgap, unauthenticated by design — same standing as the callback above)."""
    from .mcp_oauth.consent import handle_consent_get, handle_consent_post

    if request.method == "POST":
        return await handle_consent_post(request)
    return await handle_consent_get(request)


_db = database.connect


@mcp.resource(
    "relay://master-document",
    name="Master Document",
    description=(
        "The relay master document (post id=0): index, tag taxonomy, naming "
        "conventions, and house rules. Read before publishing."
    ),
    mime_type="text/markdown",
)
async def master_document() -> str:
    async with _db() as db:
        post = await service.get_post(db, 0)
    return post.content if post is not None else "Master document not found."


@mcp.tool(
    description=(
        "Publish a post to the relay feed. Subscribers receive it in real time. The response's "
        "'similar' field (relay #198, N-7) is an advisory duplicate-guard: other posts already in "
        "the vault whose embedding is close enough to be worth a glance before you duplicate work "
        "— always empty if this relay hasn't got embeddings enabled, and never blocks or slows down "
        "the publish itself."
    )
)
async def publish_post(
    title: str,
    content: str,
    tags: list[str] | None = None,
    source: str | None = None,
    expires_at: str | None = None,
) -> dict:
    """`title` becomes the Markdown filename. `expires_at`: optional ISO 8601 datetime; overrides tag/global TTL."""
    metrics.record_tool_call("publish_post")
    body = PostCreate(
        content=content,
        title=title,
        tags=tags or [],
        source=source,
        expires_at=expires_at,
    )
    async with _db() as db:
        post = await service.create_post(db, body)
    return post.model_dump()


@mcp.tool(
    description=(
        "List posts from the relay feed, optionally filtered by tag, folder or search term. "
        "Returns metadata-only summaries (id, title, tags, folder, and a short excerpt) "
        "by default — call get_post(id) for a full body. Pass summary=false to get full "
        "content inline (heavier). sort is 'updated' (default, last modified — includes "
        "edits made directly in Obsidian) or 'created'; order is 'desc' (default) or 'asc'. "
        "Sort by created + asc to read a topic's posts in the order they were written. "
        "mode ranks 'search' (relay #253, proof of concept): 'keyword' (default, FTS5/bm25), "
        "'semantic' (embedding similarity), or 'hybrid' (fusion of both), and can be combined "
        "with tag/folder — semantic/hybrid return an error if this relay hasn't got embeddings "
        "enabled. A bare id or '#id' as search (e.g. '42' or '#42') is a lookup, not a ranked "
        "search: it answers with just that post as the response's 'pinned' field (items empty) "
        "and ignores mode/tag/folder entirely — a plain id lookup works the same whether or not "
        "this relay has embeddings enabled."
    )
)
async def list_posts(
    tag: str | None = None,
    folder: str | None = None,
    search: str | None = None,
    limit: int = 20,
    offset: int = 0,
    summary: bool = True,
    sort: str = "updated",
    order: str = "desc",
    mode: str = "keyword",
) -> dict:
    metrics.record_tool_call("list_posts")
    async with _db() as db:
        try:
            result = await service.list_posts(
                db, tag=tag, folder=folder, search=search, limit=limit, offset=offset,
                summary=summary, sort=sort, order=order, mode=mode,
            )
        except service.SemanticSearchUnavailable:
            return {"error": "Semantic search is not enabled on this relay."}
        except service.InvalidSearchMode:
            return {"error": "mode must be 'keyword', 'semantic', or 'hybrid'."}
    return result.model_dump()


@mcp.tool(description="Get a single post by its ID. Use id=0 for the master document.")
async def get_post(id: int) -> dict:
    metrics.record_tool_call("get_post")
    async with _db() as db:
        post = await service.get_post(db, id)
    if post is None:
        return {"error": f"Post #{id} not found."}
    return post.model_dump()


@mcp.tool(
    description=(
        "Update an existing post. Only provided fields change; omitted fields are left "
        "untouched. Providing tags replaces the list wholesale; an empty array clears them. "
        "Pass an empty string for expires_at (or source) to clear it. Pass if_match (a "
        "post's etag from a prior response) to reject the write with a conflict if the "
        "post has changed since — otherwise this silently overwrites like today."
    )
)
async def update_post(
    id: int,
    title: str | None = None,
    content: str | None = None,
    tags: list[str] | None = None,
    source: str | None = None,
    expires_at: str | None = None,
    if_match: str | None = None,
) -> dict:
    metrics.record_tool_call("update_post")
    # An omitted argument and an explicit null both arrive as None here, so a
    # None is "leave alone". PostUpdate turns "" into a clear for expires_at and
    # source — the documented way to unset either from MCP (AUDIT.md B-05).
    fields = {
        "title": title,
        "content": content,
        "tags": tags,
        "source": source,
        "expires_at": expires_at,
        "if_match": if_match,
    }
    body = PostUpdate(**{k: v for k, v in fields.items() if v is not None})
    async with _db() as db:
        try:
            post = await service.update_post(db, id, body)
        except service.PostNotFound:
            return {"error": f"Post #{id} not found."}
        except service.ConcurrentModification:
            current = await service.get_post(db, id)
            return {
                "error": f"post #{id} has changed since if_match was captured",
                "current": current.model_dump() if current is not None else None,
            }
    return post.model_dump()


@mcp.tool(
    description=(
        "Partial edit: old_str must match exactly once in the post's current content "
        "(like a code-agent str_replace) and is replaced with new_str — for changing a "
        "line or paragraph without resending the whole body. Add more surrounding "
        "context to old_str if it isn't unique. Pass if_match (a post's etag from a "
        "prior response) to also reject the edit if the post changed since you read it."
    )
)
async def edit_post(id: int, old_str: str, new_str: str, if_match: str | None = None) -> dict:
    metrics.record_tool_call("edit_post")
    async with _db() as db:
        try:
            post = await service.edit_post(db, id, old_str, new_str, if_match=if_match)
        except service.PostNotFound:
            return {"error": f"Post #{id} not found."}
        except service.EditNoChange:
            return {"error": "new_str must be different from old_str."}
        except service.EditTextNotFound:
            return {"error": f"old_str not found in post #{id}'s content."}
        except service.EditTextNotUnique as exc:
            return {"error": f"old_str matches {exc.count} times in post #{id}; must match exactly once."}
        except service.ConcurrentModification:
            current = await service.get_post(db, id)
            return {
                "error": f"post #{id} has changed since if_match was captured",
                "current": current.model_dump() if current is not None else None,
            }
    return post.model_dump()


@mcp.tool(
    description=(
        "Append content to the end of a post instead of resending the whole body. A "
        "blank line is inserted before it unless the post is currently empty. Pass "
        "if_match (a post's etag from a prior response) to reject the append if the "
        "post changed since you read it."
    )
)
async def append_post(id: int, content: str, if_match: str | None = None) -> dict:
    metrics.record_tool_call("append_post")
    async with _db() as db:
        try:
            post = await service.append_post(db, id, content, if_match=if_match)
        except service.PostNotFound:
            return {"error": f"Post #{id} not found."}
        except service.ConcurrentModification:
            current = await service.get_post(db, id)
            return {
                "error": f"post #{id} has changed since if_match was captured",
                "current": current.model_dump() if current is not None else None,
            }
    return post.model_dump()


@mcp.tool(
    description=(
        "List a post's revision history from the vault's git history, newest first. "
        "Works for a deleted post too (exists=false), which is the case most worth "
        "recovering. Each item has sha, short_sha, when, message, and path — pass a sha "
        "to restore_post. Returns an error if vault history is disabled."
    )
)
async def get_post_history(id: int, limit: int = 20) -> dict:
    metrics.record_tool_call("get_post_history")
    async with _db() as db:
        try:
            result = await service.get_post_history(db, id, limit=limit)
        except service.HistoryUnavailable:
            return {"error": "Vault history is disabled or git is unavailable."}
    return result.model_dump()


@mcp.tool(
    description=(
        "List posts that no longer exist but can still be restored, newest first. This is "
        "the discovery half of recovery: restore_post can put back any post whose id you "
        "know, and after a delete you do not know it. Each item carries id, title, sha, "
        "when, reason (deleted, external or expiry) and path — pass the id and sha "
        "straight to restore_post. TTL expiries are excluded unless include_expiry is "
        "true, since those are routine and would bury the deletion you are looking for. "
        "Nothing is moved on delete and there is nothing to purge: this reads the vault's "
        "git history. Returns an error if vault history is disabled."
    )
)
async def list_deleted_posts(limit: int = 50, include_expiry: bool = False) -> dict:
    metrics.record_tool_call("list_deleted_posts")
    async with _db() as db:
        try:
            result = await service.list_deleted_posts(db, limit=limit, include_expiry=include_expiry)
        except service.HistoryUnavailable:
            return {"error": "Vault history is disabled or git is unavailable."}
    return result.model_dump()


@mcp.tool(
    description=(
        "The vault changelog (relay #198, N-4): every post-affecting write, newest first — "
        "create, update, edit, append, delete, restore, tag rename, an external Obsidian "
        "edit/delete, or a TTL expiry. Each item has seq, id, title, action, when, sha (and "
        "author, always null until per-agent identity ships). Pass since as a seq from a "
        "prior response to page forward, or an ISO 8601 timestamp to see what moved after a "
        "given time — e.g. 'what did the schedulers publish overnight'. Omit since for the "
        "most recent `limit`. This is a flat feed over git history, not a second store — "
        "reading it costs nothing extra."
    )
)
async def list_changes(since: str | None = None, limit: int = 50) -> dict:
    metrics.record_tool_call("list_changes")
    async with _db() as db:
        try:
            rows = await changes.list_changes(db, since=since, limit=limit)
        except changes.HistoryUnavailable:
            return {"error": "Vault history is disabled or git is unavailable."}
    return ChangeListResponse(items=[ChangeEntry.from_row(r) for r in rows]).model_dump()


@mcp.tool(
    description=(
        "Read a post exactly as it was at one revision — title, content and tags. Use this "
        "to see what a restore would give back before calling restore_post: the history "
        "listing carries only metadata, and picking a sha out of it blind is a poor way to "
        "undo something. Works for a deleted post too, and accepts a short sha. Read-only; "
        "it changes nothing. Returns an error if vault history is disabled."
    )
)
async def get_post_revision(id: int, sha: str) -> dict:
    metrics.record_tool_call("get_post_revision")
    async with _db() as db:
        try:
            result = await service.get_post_revision(db, id, sha)
        except service.HistoryUnavailable:
            return {"error": "Vault history is disabled or git is unavailable."}
        except service.RevisionNotFound:
            return {"error": f"No revision '{sha}' in the history of post #{id}."}
    return result.model_dump()


@mcp.tool(
    description=(
        "List a post's backlinks — the other posts that link to it via [[Title]] or #id. "
        "Check this before rewriting or deleting a post: relay keeps one canonical post per "
        "topic and cross-links by id, so the posts listed here are the ones that break if it "
        "goes away or is renamed. Returns an error if the post does not exist."
    )
)
async def get_backlinks(id: int) -> dict:
    metrics.record_tool_call("get_backlinks")
    async with _db() as db:
        try:
            result = await service.get_backlinks(db, id)
        except service.PostNotFound:
            return {"error": f"Post #{id} not found."}
    return result.model_dump()


@mcp.tool(
    description=(
        "List posts related to this one by embedding similarity that it does NOT already "
        "cross-link via [[Title]] or #id, in either direction (relay #198, N-7) — an automatic "
        "to-do list of missing wikilinks, not a general 'more like this'. A post already linked "
        "has already had its relationship made explicit and won't show up here. Returns an error "
        "if the post doesn't exist or this relay hasn't got embeddings enabled."
    )
)
async def get_related(id: int) -> dict:
    metrics.record_tool_call("get_related")
    async with _db() as db:
        try:
            result = await service.get_related(db, id)
        except service.PostNotFound:
            return {"error": f"Post #{id} not found."}
        except service.SemanticSearchUnavailable:
            return {"error": "Semantic search is not enabled on this relay."}
    return result.model_dump()


@mcp.tool(
    description=(
        "Rename a tag across every post that carries it, in one atomic pass. Use this to fix "
        "taxonomy rather than retagging posts one at a time — that is slower and leaves the "
        "vault half-migrated if it stops partway. The new name is normalised the same way "
        "tags always are (lowercased; only letters, digits, hyphen and underscore survive). "
        "Renaming to a tag that already exists merges the two. Returns the full tag list."
    )
)
async def rename_tag(tag: str, new_name: str) -> dict:
    metrics.record_tool_call("rename_tag")
    cleaned = re.sub(r"[^a-z0-9_-]", "", new_name.strip().lower())
    if not cleaned:
        return {"error": "new_name must contain at least one letter, digit, hyphen or underscore."}
    async with _db() as db:
        try:
            result = await service.rename_tag(db, tag, cleaned)
        except service.InvalidTag:
            # `InvalidTag` carries no message (REST supplies its own static
            # detail too — see routes/tags.py) — `str(exc)` here would be "".
            return {"error": "tag must contain at least one letter, digit, hyphen or underscore."}
    return result.model_dump()


@mcp.tool(
    description=(
        "Restore a post to an earlier revision, recreating it if it was deleted. Pass a "
        "sha from get_post_history. The restore is itself recorded in history, so it can "
        "be undone the same way. Use this to undo a bad overwrite rather than "
        "reconstructing the body by hand."
    )
)
async def restore_post(id: int, sha: str) -> dict:
    metrics.record_tool_call("restore_post")
    async with _db() as db:
        try:
            post = await service.restore_post(db, id, sha)
        except service.HistoryUnavailable:
            return {"error": "Vault history is disabled or git is unavailable."}
        except service.RevisionNotFound:
            return {"error": f"No revision '{sha}' in the history of post #{id}."}
    return post.model_dump()


@mcp.tool(
    description=(
        "Report this relay's runtime status: version, uptime, which vault it is serving, counts of "
        "posts/tags/folders/attachments, semantic-search embedding coverage and backend state, and which "
        "features are actually working. Use it to confirm you are talking to the vault you think you are, "
        "and to check features that degrade silently — vault history is off when git is missing (writes "
        "would be unrecoverable), search falls back to substring matching without FTS5, and external "
        "edits are not picked up when the watcher is off."   )
)
async def get_status() -> dict:
    metrics.record_tool_call("get_status")
    async with _db() as db:
        result = await status.build(db)
    return result.model_dump()


@mcp.tool(
    description=(
        "Lint the vault: check it against the rules already written down in #0 instead of "
        "relying on someone reading every post (relay #198, N-5): posts missing a domain and/or type tag, "
        "a post with zero tags, notes stuck in Inbox despite carrying a domain tag, broken #NNN "
        "refs and dangling [[wikilinks]] (and specifically links pointing at a deleted post), a "
        "dangling attachment embed (![[file]]) or a plain [[wikilink]] whose target is a filename "
        "(should be an embed or attachment instead) — each its own rule, since the fix differs from "
        "an ordinary broken post link — an H1 missing entirely, or one that drifted from the title "
        "(the classic case: relay's filename sanitizer strips a character like ':' that the body's "
        "H1 still has), a hub/plan post not updated in a while, a post with zero backlinks, a "
        "tags.yml entry with zero posts, a post that embedded to zero chunks, and #0's own stated "
        "post count going stale. Link/embed/heading scanning ignores fenced code and inline code "
        "spans (a post documenting relay's own syntax doesn't get flagged for its own examples), "
        "a bare #N below 10 or above the vault's id high-water mark is never treated as a post "
        "reference (footnote markers, procedure steps and GitHub issue/PR numbers collide with real "
        "ids far more often than a genuine cross-link does at either extreme), and a link to a "
        "deleted post that carried a rotation tag like digest at deletion time is suppressed rather "
        "than re-reported every run. The four link rules (broken_link, link_to_deleted_post, "
        "broken_attachment_embed, wikilink_to_filename) report one finding per distinct broken "
        "target per post, not one per mention, with the real count in occurrences, and carry a "
        "match field — the exact broken text — for jumping straight to it in an editor. The master "
        "document (id=0) is exempt from the tag/folder/H1/staleness/embedding-coverage rules — a "
        "root index reasonably breaks conventions an ordinary post follows — but not from the link "
        "checks: a broken cross-link in #0 is a factual defect, not a convention, and matters more "
        "there than anywhere else. Findings are ordered by post id. Read-only. A rule that needs a "
        "disabled feature (history, embeddings) is skipped and named in skipped_rules rather than "
        "erroring."
    )
)
async def lint_vault() -> dict:
    metrics.record_tool_call("lint_vault")
    async with _db() as db:
        result = await lint.run(db)
    return result.model_dump()


@mcp.tool(
    description=(
        "Re-run the embedding backfill without restarting relay — resumes from the content-addressed "
        "cache by default, or pass force=true to wipe every embedded chunk/vector/cache row first and "
        "re-embed the whole vault from scratch. Returns the same object as get_status's 'embeddings' "
        "field. Errors if embeddings aren't enabled on this relay, or if a backfill is already running."
    )
)
async def trigger_embedding_backfill(force: bool = False) -> dict:
    metrics.record_tool_call("trigger_embedding_backfill")
    async with _db() as db:
        try:
            result = await status.trigger_backfill(db, force=force)
        except status.EmbeddingsUnavailable:
            return {"error": "Semantic search is not enabled on this relay."}
        except status.BackfillAlreadyRunning:
            return {"error": "A backfill is already running."}
    return result.model_dump()


@mcp.tool(
    description=(
        "Turn semantic/hybrid search on or off at runtime, without a restart. Enabling only resumes "
        "against whatever model and vector schema are already on disk and kicks off a backfill for "
        "any posts written while it was off; changing EMBEDDING_MODEL to a different dimension still "
        "needs a restart. Disabling frees the embedding model's memory immediately rather than waiting "
        "for the idle timeout. In-memory only — a restart reverts to whatever .env says. Returns the "
        "same object as get_status's 'embeddings' field."
    )
)
async def set_embeddings_enabled(enabled: bool) -> dict:
    metrics.record_tool_call("set_embeddings_enabled")
    async with _db() as db:
        try:
            result = await status.set_embeddings_enabled(db, enabled)
        except status.EmbeddingsUnavailable:
            return {
                "error": "sqlite-vec is not available on this relay, or EMBEDDING_MODEL is not a known "
                "fastembed model."
            }
        except status.EmbeddingDimensionMismatch:
            return {
                "error": "EMBEDDING_MODEL's dimension doesn't match the vector schema already on disk. "
                "Restart relay to rebuild it before enabling."
            }
    return result.model_dump()


@mcp.tool(description="Delete a post from the relay feed by its ID. The master document (id=0) cannot be deleted.")
async def delete_post(id: int) -> dict:
    metrics.record_tool_call("delete_post")
    async with _db() as db:
        try:
            await service.delete_post(db, id)
        except service.ProtectedPost:
            return {"error": "Master document (id=0) cannot be deleted."}
        except service.PostNotFound:
            return {"error": f"Post #{id} not found."}
    return {"ok": True, "deleted": id}


@mcp.tool(
    description=(
        "Attach a file (image, PDF, …) to the vault. Provide the bytes exactly one way: "
        "`data` (base64 — only viable for tiny files, since you must emit the whole blob), "
        "`source_url` (an http(s) URL the server fetches — preferred for real files), or "
        "`upload_id` from create_upload (bytes PUT out-of-band). With `post_id`, the file is "
        "filed under that post's folder and its ![[file]] embed is appended to the post body; "
        "otherwise it goes to `folder`, or to the folder `tags` would file a post under, or Inbox — and you place "
        "the returned `ref` yourself. Pass embed=false to file a post's attachment without touching its body. "
        "`filename` is required with `data`; with `source_url`/`upload_id` it's derived when omitted."
    )
)
async def add_attachment(
    filename: str | None = None,
    data: str | None = None,
    source_url: str | None = None,
    upload_id: str | None = None,
    post_id: int | None = None,
    folder: str | None = None,
    tags: list[str] | None = None,
    embed: bool = True,
) -> dict:
    """Returns {filename, ref, folder, post_id}. `ref` is the ![[…]] embed to drop into a post."""
    metrics.record_tool_call("add_attachment")
    try:
        body = AttachmentCreate(
            filename=filename, data=data, source_url=source_url,
            upload_id=upload_id, post_id=post_id, folder=folder, tags=tags or [], embed=embed,
        )
    except ValidationError as exc:
        return {"error": _first_error(exc)}
    async with _db() as db:
        try:
            result = await service.ingest_attachment(
                db, filename=body.filename, data=body.data, source_url=body.source_url,
                upload_id=body.upload_id, post_id=body.post_id, folder=body.folder,
                tags=body.tags, embed=body.embed,
            )
        except ValueError:
            return {"error": "data is not valid base64"}
        except service.AttachmentSourceError as exc:
            return {"error": str(exc)}
        except service.PostNotFound:
            return {"error": f"Post #{post_id} not found."}
        except service.AttachmentError as exc:
            return {"error": str(exc)}
    return result.model_dump()


@mcp.tool(
    description=(
        "Mint a presigned upload slot for a file too large to pass as base64. Returns "
        "{upload_id, upload_url, method, max_bytes, expires_at}: PUT the raw bytes to "
        "`upload_url` (out-of-band — not through this tool call), then call add_attachment "
        "with the `upload_id` to file it. Use when you can reach the relay host to PUT."
    )
)
async def create_upload() -> dict:
    metrics.record_tool_call("create_upload")
    return service.create_upload_slot().model_dump()


@mcp.tool(
    description=(
        "Retrieve an attachment from the vault by its filename (as used in ![[file]]). "
        "Images are returned so they can be viewed inline; other files return a note with "
        "the vault path."
    )
)
async def get_attachment(name: str):
    """Returns image content for images, else a dict describing the file."""
    metrics.record_tool_call("get_attachment")
    try:
        result = vault.read_attachment(name, max_bytes=settings.attachment_max_bytes)
    except ValueError:
        return {"error": f"Attachment '{name}' is too large to return inline "
                         f"(over {settings.attachment_max_mb} MB)."}
    if result is None:
        return {"error": f"Attachment '{name}' not found."}
    path, raw, mime = result
    if mime.startswith("image/"):
        # Derive format from the mime (image/jpeg → 'jpeg') so Image doesn't emit
        # a non-standard type like image/jpg from the '.jpg' suffix.
        return Image(data=raw, format=mime.split("/", 1)[1])
    return {"filename": path.name, "mime": mime, "bytes": len(raw),
            "note": "Non-image attachment; not shown inline."}


@mcp.tool(
    description=(
        "Delete an attachment from the vault by its filename. Returns the removed name and "
        "any post ids that still embed/link it (now dangling) so you can fix them."
    )
)
async def delete_attachment(name: str) -> dict:
    metrics.record_tool_call("delete_attachment")
    async with _db() as db:
        result = await service.delete_attachment(db, name)
    if result is None:
        return {"error": f"Attachment '{name}' not found."}
    return result.model_dump()


@mcp.tool(
    description=(
        "List attachments stored in the vault (filename, folder, size, and the ![[…]] "
        "embed ref). Scope with `post_id` (that post's folder) or `folder`; omit both to "
        "list every attachment. Use the returned filename with get_attachment."
    )
)
async def list_attachments(post_id: int | None = None, folder: str | None = None) -> dict:
    metrics.record_tool_call("list_attachments")
    async with _db() as db:
        try:
            result = await service.list_attachments(db, post_id=post_id, folder=folder)
        except service.PostNotFound:
            return {"error": f"Post #{post_id} not found."}
        except service.InvalidFolder as exc:
            return {"error": str(exc)}
    return result.model_dump()


@mcp.tool(
    description=(
        "List the vault's first-level folders with their post counts. These are the names list_posts and l"
        "ist_attachments accept as `folder`; a post is filed by its first domain tag at creation, so t"
        "his is the map of what the vault already has before you choose one."
    )
)
async def list_folders() -> dict:
    metrics.record_tool_call("list_folders")
    async with _db() as db:
        result = await service.list_folders(db)
    return result.model_dump()


@mcp.tool(description="List all tags in the relay feed with their post counts.")
async def list_tags() -> dict:
    metrics.record_tool_call("list_tags")
    async with _db() as db:
        result = await service.list_tags(db)
    return result.model_dump()


@mcp.tool(
    description=(
        "Set expiry configuration for a tag. Provide ttl_hours (relative to each post's "
        "creation), expires_at (absolute cutoff), or both. Only affects posts without their "
        "own expires_at. Provide neither to remove the tag's expiry configuration."
    )
)
async def set_tag_config(
    tag: str,
    ttl_hours: int | None = None,
    expires_at: str | None = None,
) -> dict:
    metrics.record_tool_call("set_tag_config")
    body = TagConfigCreate(ttl_hours=ttl_hours, expires_at=expires_at)
    async with _db() as db:
        result = await service.set_tag_config(db, tag, body)
    return result.model_dump()


class BearerAuthASGI:
    """Minimal ASGI wrapper that gates the MCP app behind the static bearer key."""

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = dict(scope.get("headers") or [])
        auth = headers.get(b"authorization", b"").decode("latin-1")
        token = auth[7:] if auth.startswith("Bearer ") else ""
        if not bearer_matches(token):
            await JSONResponse({"detail": "Invalid API key"}, status_code=401)(scope, receive, send)
            return
        await self.app(scope, receive, send)


# Built once at import time (relay #313 Phase 1 spike) rather than per-call: fastmcp's
# http_app() constructs the Starlette app and its (not-yet-running) session manager
# together, and main.py's own lifespan needs this *exact* instance's `.lifespan` context
# manager — mounted sub-apps don't get their lifespan run automatically, so relay drives
# it from its own lifespan (see main.py). Building a second instance there would give
# FastAPI a session manager different from the one actually mounted.
#
# host_origin_protection=False matches today's explicit disable of the old SDK's
# DNS-rebinding protection: relay is mounted into FastAPI behind a public reverse proxy,
# not run standalone, so a real Host header (e.g. relay.geon.im) must not be rejected.
# fastmcp's own default ("auto") is smarter than the old SDK's — it only enforces on a
# loopback bind — but False pins relay to the same explicit, audited behavior it has
# always had rather than an implicit heuristic. Revisit in Phase 5.
#
# The old SDK's 4 MiB max_request_body_size cap is still very much active — verified
# live (a ~6 MB /mcp POST body 413s with "Request body too large"). fastmcp's http_app()
# has no parameter to configure it, but internally still builds the old SDK's
# TransportSecuritySettings when constructing the session manager and only overrides
# enable_dns_rebinding_protection on it (see host_origin_protection above), leaving
# max_request_body_size at the SDK's own DEFAULT_MAX_REQUEST_BODY_SIZE. So this is now
# an unexposed fastmcp-internal default rather than a relay-configurable setting — worth
# reconfirming across future fastmcp versions (Phase 5·E), not a Phase 1 gap.
mcp_http_app = mcp.http_app(
    path="/mcp",
    transport="streamable-http",
    stateless_http=True,
    host_origin_protection=False,
)


def mcp_asgi_app():
    """Return the Streamable HTTP MCP app to mount on FastAPI.

    OAuth isn't wired up on this branch yet (Phase 2) — `_check_oauth_not_yet_supported()`
    above already fails loud at import time if MCP_OAUTH_ENABLED is set, so by the time
    this runs OAuth is guaranteed off and the static-bearer gate is always the right one.
    """
    return BearerAuthASGI(mcp_http_app)
