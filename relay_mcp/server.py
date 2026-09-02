from __future__ import annotations

from pathlib import Path

import httpx
import mcp.server.stdio
import mcp.types as types
from mcp.server import Server
from mcp.server.lowlevel.helper_types import ReadResourceContents
from pydantic import AnyUrl

from relay.config import settings

RELAY_BASE_URL = settings.relay_base_url
_AUTH = {"Authorization": f"Bearer {settings.api_key}"}


async def _upload_local_path(arguments: dict) -> list[types.TextContent]:
    """Handle add_attachment(path=…): read a file on the machine running this proxy
    and upload it to relay via a presigned slot (create → PUT bytes → finalize), so
    no base64 blob ever passes through the model. Proxy-only — the remote HTTP MCP
    server can't (and mustn't) read the client's filesystem."""
    p = Path(arguments["path"]).expanduser()
    if not p.is_file():
        return [types.TextContent(type="text", text=f"Local file not found: {arguments['path']}")]
    filename = arguments.get("filename") or p.name
    async with httpx.AsyncClient() as client:
        slot_resp = await client.post(f"{RELAY_BASE_URL}/attachments/uploads", headers=_AUTH, timeout=10)
        slot_resp.raise_for_status()
        slot = slot_resp.json()
        size = p.stat().st_size
        if size > slot["max_bytes"]:
            return [types.TextContent(
                type="text",
                text=f"'{p.name}' is {size} bytes — over the server's {slot['max_bytes']}-byte limit.",
            )]
        put = await client.put(
            f"{RELAY_BASE_URL}/attachments/uploads/{slot['upload_id']}",
            content=p.read_bytes(), headers=_AUTH, timeout=120,
        )
        if put.status_code == 413:
            return [types.TextContent(type="text", text=put.json().get("detail", "Attachment too large."))]
        put.raise_for_status()
        final = {"upload_id": slot["upload_id"], "filename": filename}
        for k in ("post_id", "folder"):
            if arguments.get(k) is not None:
                final[k] = arguments[k]
        response = await client.post(f"{RELAY_BASE_URL}/attachments", json=final, headers=_AUTH, timeout=30)
        if response.status_code == 404:
            return [types.TextContent(type="text", text=f"Post #{arguments.get('post_id')} not found.")]
        if response.status_code in (400, 413):
            return [types.TextContent(type="text", text=response.json().get("detail", "Attachment error."))]
        response.raise_for_status()
        a = response.json()
    where = f" appended to post #{a['post_id']}" if a.get("post_id") is not None else ""
    return [types.TextContent(
        type="text",
        text=f"Uploaded '{a['filename']}' ({size} bytes) to {a['folder']}/assets{where}.\nEmbed: {a['ref']}",
    )]


INSTRUCTIONS = (
    "Relay is a personal knowledge base kept as a plain-Markdown vault; posts are files "
    "a human also edits directly in Obsidian, so write them to be read by a person. "
    "Clients subscribe to changes in real time. Before writing, read the master document "
    "with get_post(id=0) — it holds the index, tag taxonomy, naming conventions, and "
    "house rules. Keep one canonical post per topic and update it in place rather than "
    "creating duplicates."
)

MASTER_DOC_URI = "relay://master-document"

server = Server("relay", instructions=INSTRUCTIONS)


@server.list_resources()
async def list_resources() -> list[types.Resource]:
    return [
        types.Resource(
            uri=AnyUrl(MASTER_DOC_URI),
            name="Master Document",
            description=(
                "The relay master document (post id=0): index, tag taxonomy, naming "
                "conventions, and house rules. Read before publishing."
            ),
            mimeType="text/markdown",
        )
    ]


@server.read_resource()
async def read_resource(uri: AnyUrl) -> list[ReadResourceContents]:
    if str(uri) != MASTER_DOC_URI:
        raise ValueError(f"Unknown resource: {uri}")
    async with httpx.AsyncClient() as client:
        response = await client.get(
            f"{RELAY_BASE_URL}/posts/0",
            headers={"Authorization": f"Bearer {settings.api_key}"},
            timeout=10,
        )
        response.raise_for_status()
        post = response.json()
    return [ReadResourceContents(content=post["content"], mime_type="text/markdown")]


@server.list_tools()
async def list_tools() -> list[types.Tool]:
    return [
        types.Tool(
            name="publish_post",
            description='Publish a post to the relay feed. Subscribers receive it in real time.',
            inputSchema={
                "type": "object",
                "required": ["title", "content"],
                "properties": {
                    "title": {"type": "string", "description": "Title — becomes the Markdown filename"},
                    "content": {"type": "string", "description": "Post body (Markdown)"},
                    "tags": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional tags, e.g. ['news', 'ai']",
                    },
                    "source": {"type": "string", "description": "Optional source URL or label"},
                    "expires_at": {
                        "type": "string",
                        "description": "Optional ISO 8601 datetime after which the post expires, e.g. '2026-06-30T00:00:00Z'. Overrides tag/global TTL.",
                    },
                },
            },
        ),
        types.Tool(
            name="list_tags",
            description="List all tags in the relay feed with their post counts.",
            inputSchema={"type": "object", "properties": {}},
        ),
        types.Tool(
            name="list_posts",
            description=(
                "List posts from the relay feed, optionally filtered by tag, folder or search term. Returns "
                "metadata-only summaries (id, title, tags, folder, and a short excerpt) by default — call "
                "get_post(id) for a full body. Pass summary=false to get full content inline (heavier). sort "
                "is 'updated' (default, last modified — includes edits made directly in Obsidian) or "
                "'created'; order is 'desc' (default) or 'asc'. Sort by created + asc to read a topic's posts "
                "in the order they were written. mode ranks 'search' (relay #253, proof of concept): "
                "'keyword' (default, FTS5/bm25), 'semantic' (embedding similarity), or 'hybrid' (fusion of "
                "both) — semantic/hybrid return an error if this relay hasn't got embeddings enabled, or if "
                "combined with tag/folder (the ranked path doesn't apply them)."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "tag": {"type": "string", "description": "Filter by tag"},
                    "folder": {"type": "string", "description": "Filter by first-level folder"},
                    "search": {"type": "string", "description": "Search title, content and source"},
                    "limit": {"type": "integer", "description": "Max posts to return (default 20)"},
                    "offset": {"type": "integer", "description": "Offset for pagination (default 0)"},
                    "summary": {
                        "type": "boolean",
                        "description": "Metadata + excerpt only, no bodies (default true)",
                    },
                    "sort": {
                        "type": "string",
                        "description": "'updated' (default) or 'created'",
                    },
                    "order": {
                        "type": "string",
                        "description": "'desc' (default) or 'asc'",
                    },
                    "mode": {
                        "type": "string",
                        "description": "'keyword' (default), 'semantic', or 'hybrid'",
                    },
                },
            },
        ),
        types.Tool(
            name="get_post",
            description='Get a single post by its ID. Use id=0 for the master document.',
            inputSchema={
                "type": "object",
                "required": ["id"],
                "properties": {
                    "id": {"type": "integer", "description": "Post ID"},
                },
            },
        ),
        types.Tool(
            name="get_post_history",
            description=(
                "List a post's revision history from the vault's git history, newest first. "
                "Works for a deleted post too (exists=false), which is the case most worth "
                "recovering. Each item has sha, short_sha, when, message, and path — pass a sha "
                "to restore_post. Returns an error if vault history is disabled."
            ),
            inputSchema={
                "type": "object",
                "required": ["id"],
                "properties": {
                    "id": {"type": "integer", "description": "Post ID"},
                    "limit": {"type": "integer", "description": "Max revisions to return (default 20)"},
                },
            },
        ),
        types.Tool(
            name="list_deleted_posts",
            description=(
                "List posts that no longer exist but can still be restored, newest first. This is "
                "the discovery half of recovery: restore_post can put back any post whose id you "
                "know, and after a delete you do not know it. Each item carries id, title, sha, "
                "when, reason (deleted, external or expiry) and path — pass the id and sha "
                "straight to restore_post. TTL expiries are excluded unless include_expiry is "
                "true, since those are routine and would bury the deletion you are looking for. "
                "Nothing is moved on delete and there is nothing to purge: this reads the vault's "
                "git history. Returns an error if vault history is disabled."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "description": "Max deletions to return (default 50)"},
                    "include_expiry": {
                        "type": "boolean",
                        "description": "Include posts swept by a tag TTL (default false)",
                    },
                },
            },
        ),
        types.Tool(
            name="get_post_revision",
            description=(
                "Read a post exactly as it was at one revision — title, content and tags. Use this to see "
                "what a restore would give back before calling restore_post: the history listing carries only "
                "metadata, and picking a sha out of it blind is a poor way to undo something. Works for a "
                "deleted post too, and accepts a short sha. Read-only; it changes nothing. Returns an error "
                "if vault history is disabled."
            ),
            inputSchema={
                "type": "object",
                "required": ["id", "sha"],
                "properties": {
                    "id": {"type": "integer", "description": "Post ID"},
                    "sha": {"type": "string", "description": "Revision sha from get_post_history"},
                },
            },
        ),
        types.Tool(
            name="get_backlinks",
            description=(
                "List the posts that link to this one via [[Title]] or #id — its linked mentions. Check this "
                "before rewriting or deleting a post: relay keeps one canonical post per topic and "
                "cross-links by id, so the posts listed here are the ones that break if it goes away or is "
                "renamed. Returns an error if the post does not exist."
            ),
            inputSchema={
                "type": "object",
                "required": ["id"],
                "properties": {
                    "id": {"type": "integer", "description": "Post ID"},
                },
            },
        ),
        types.Tool(
            name="rename_tag",
            description=(
                "Rename a tag across every post that carries it, in one atomic pass. Use this to fix taxonomy "
                "rather than retagging posts one at a time — that is slower and leaves the vault "
                "half-migrated if it stops partway. The new name is normalised the same way tags always are "
                "(lowercased; only letters, digits, hyphen and underscore survive). Renaming to a tag that "
                "already exists merges the two. Returns the full tag list."
            ),
            inputSchema={
                "type": "object",
                "required": ["tag", "new_name"],
                "properties": {
                    "tag": {"type": "string", "description": "Tag to rename"},
                    "new_name": {"type": "string", "description": "New tag name"},
                },
            },
        ),
        types.Tool(
            name="restore_post",
            description=(
                "Restore a post to an earlier revision, recreating it if it was deleted. Pass a "
                "sha from get_post_history. The restore is itself recorded in history, so it can "
                "be undone the same way. Use this to undo a bad overwrite rather than "
                "reconstructing the body by hand."
            ),
            inputSchema={
                "type": "object",
                "required": ["id", "sha"],
                "properties": {
                    "id": {"type": "integer", "description": "Post ID"},
                    "sha": {"type": "string", "description": "Revision sha from get_post_history"},
                },
            },
        ),
        types.Tool(
            name="get_status",
            description="Report this relay's runtime status: version, uptime, which vault it is serving, counts of posts/tags/folders/attachments, and which features are actually working. Use it to confirm you are talking to the vault you think you are, and to check features that degrade silently — vault history is off when git is missing (writes would be unrecoverable), search falls back to substring matching without FTS5, and external edits are not picked up when the watcher is off.",
            inputSchema={"type": "object", "properties": {}},
        ),
        types.Tool(
            name="delete_post",
            description='Delete a post from the relay feed by its ID. The master document (id=0) cannot be deleted.',
            inputSchema={
                "type": "object",
                "required": ["id"],
                "properties": {
                    "id": {"type": "integer", "description": "Post ID to delete"},
                },
            },
        ),
        types.Tool(
            name="update_post",
            description=(
                'Update an existing post. Only provided fields change; omitted fields are left untouched. Providing tags replaces the list wholesale; an empty array clears them. Pass expires_at=null to clear an existing expiry.'
            ),
            inputSchema={
                "type": "object",
                "required": ["id"],
                "properties": {
                    "id": {"type": "integer", "description": "ID of the post to update"},
                    "title": {"type": "string", "description": "New title (renames the file)"},
                    "content": {"type": "string", "description": "New post body"},
                    "tags": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Replacement tag list (empty array clears all tags)",
                    },
                    "source": {"type": "string", "description": "Source URL or label"},
                    "expires_at": {
                        "type": "string",
                        "description": "ISO 8601 datetime after which the post expires. Pass null to clear an existing expiry.",
                    },
                },
            },
        ),
        types.Tool(
            name="add_attachment",
            description=(
                "Attach a file (image, PDF, …) to the vault. Provide the bytes exactly one way: "
                "'path' (a file on THIS machine — the proxy reads it and streams it to relay; best "
                "for a real local file), 'source_url' (an http(s) URL the server fetches), "
                "'data' (base64 — only viable for tiny files, since you must emit the whole blob), or "
                "'upload_id' from create_upload (bytes PUT out-of-band). With 'post_id', the file is "
                "filed under that post's folder and its ![[file]] embed is appended to the post body; "
                "otherwise it goes to 'folder' (or Inbox) and you place the returned ref yourself. "
                "'filename' is required with 'data'; with 'path'/'source_url'/'upload_id' it's derived when omitted."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "filename": {"type": "string", "description": "Attachment filename, e.g. 'diagram.png'. Required with data; derived when omitted for path/source_url/upload_id"},
                    "path": {"type": "string", "description": "Path to a file on the machine running this proxy; the proxy reads and uploads it (streamed, no base64)"},
                    "data": {"type": "string", "description": "Base64-encoded file bytes (tiny files only)"},
                    "source_url": {"type": "string", "description": "http(s) URL the server fetches the bytes from"},
                    "upload_id": {"type": "string", "description": "Id of a filled presigned upload slot (see create_upload)"},
                    "post_id": {"type": "integer", "description": "Post to attach to (appends ![[file]] to its body)"},
                    "folder": {"type": "string", "description": "First-level folder for a standalone attachment (default Inbox)"},
                },
            },
        ),
        types.Tool(
            name="create_upload",
            description=(
                'Mint a presigned upload slot for a file too large to pass as base64. Returns {upload_id, upload_url, method, max_bytes, expires_at}: PUT the raw bytes to `upload_url` (out-of-band — not through this tool call), then call add_attachment with the `upload_id` to file it. Use when you can reach the relay host to PUT.'
            ),
            inputSchema={"type": "object", "properties": {}},
        ),
        types.Tool(
            name="delete_attachment",
            description=(
                'Delete an attachment from the vault by its filename. Returns the removed name and any post ids that still embed/link it (now dangling) so you can fix them.'
            ),
            inputSchema={
                "type": "object",
                "required": ["name"],
                "properties": {
                    "name": {"type": "string", "description": "Attachment filename, e.g. 'diagram.png'"},
                },
            },
        ),
        types.Tool(
            name="list_attachments",
            description=(
                "List attachments stored in the vault (filename, folder, size, and the ![[…]] embed ref). Scope with `post_id` (that post's folder) or `folder`; omit both to list every attachment. Use the returned filename with get_attachment."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "post_id": {"type": "integer", "description": "List attachments in this post's folder"},
                    "folder": {"type": "string", "description": "List attachments in this first-level folder"},
                },
            },
        ),
        types.Tool(
            name="get_attachment",
            description=(
                'Retrieve an attachment from the vault by its filename (as used in ![[file]]). Images are returned so they can be viewed inline; other files return a note with the vault path.'
            ),
            inputSchema={
                "type": "object",
                "required": ["name"],
                "properties": {
                    "name": {"type": "string", "description": "Attachment filename, e.g. 'diagram.png'"},
                },
            },
        ),
        types.Tool(
            name="set_tag_config",
            description=(
                "Set expiry configuration for a tag. Provide ttl_hours (relative to each post's creation), expires_at (absolute cutoff), or both. Only affects posts without their own expires_at."
            ),
            inputSchema={
                "type": "object",
                "required": ["tag"],
                "properties": {
                    "tag": {"type": "string", "description": "Tag name to configure"},
                    "ttl_hours": {
                        "type": "integer",
                        "description": "Hours after creation before posts with this tag expire",
                    },
                    "expires_at": {
                        "type": "string",
                        "description": "Absolute ISO 8601 datetime after which all posts with this tag expire",
                    },
                },
            },
        ),
    ]


@server.call_tool()
async def call_tool(
    name: str, arguments: dict
) -> list[types.TextContent | types.ImageContent]:
    if name == "add_attachment":
        if sum(bool(arguments.get(k)) for k in ("path", "data", "source_url", "upload_id")) > 1:
            return [types.TextContent(
                type="text",
                text="Provide exactly one of: path, data, source_url, upload_id.",
            )]
        if arguments.get("path"):
            return await _upload_local_path(arguments)
        payload = {k: v for k, v in arguments.items() if v is not None and k != "path"}
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{RELAY_BASE_URL}/attachments",
                json=payload,
                headers={"Authorization": f"Bearer {settings.api_key}"},
                timeout=30,
            )
            if response.status_code == 404:
                return [types.TextContent(type="text", text=f"Post #{arguments.get('post_id')} not found.")]
            if response.status_code == 400:
                return [types.TextContent(type="text", text=response.json().get("detail", "Invalid attachment source."))]
            if response.status_code == 422:
                detail = response.json().get("detail")
                msg = detail[0]["msg"] if isinstance(detail, list) and detail else "Invalid attachment request."
                return [types.TextContent(type="text", text=msg.removeprefix("Value error, "))]
            if response.status_code == 413:
                return [types.TextContent(type="text", text=response.json().get("detail", "Attachment too large."))]
            response.raise_for_status()
            a = response.json()
        where = f" appended to post #{a['post_id']}" if a.get("post_id") is not None else ""
        return [types.TextContent(
            type="text",
            text=f"Stored attachment '{a['filename']}' in {a['folder']}/assets{where}.\nEmbed: {a['ref']}",
        )]

    if name == "create_upload":
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{RELAY_BASE_URL}/attachments/uploads",
                headers={"Authorization": f"Bearer {settings.api_key}"},
                timeout=10,
            )
            response.raise_for_status()
            s = response.json()
        return [types.TextContent(
            type="text",
            text=(f"Upload slot ready. PUT the raw bytes (up to {s['max_bytes']} bytes) to:\n"
                  f"  {s['method']} {s['upload_url']}\n"
                  f"then call add_attachment with upload_id='{s['upload_id']}'. "
                  f"Expires {s['expires_at']}."),
        )]

    if name == "delete_attachment":
        async with httpx.AsyncClient() as client:
            response = await client.delete(
                f"{RELAY_BASE_URL}/attachments/{arguments['name']}",
                headers={"Authorization": f"Bearer {settings.api_key}"},
                timeout=10,
            )
            if response.status_code == 404:
                return [types.TextContent(type="text", text=f"Attachment '{arguments['name']}' not found.")]
            response.raise_for_status()
            d = response.json()
        warn = (f"  Still referenced by posts: {', '.join('#' + str(i) for i in d['referenced_by'])} — "
                "remove the dangling embeds." if d["referenced_by"] else "")
        return [types.TextContent(type="text", text=f"Deleted attachment '{d['filename']}'.{warn}")]

    if name == "list_attachments":
        params = {k: v for k, v in arguments.items() if k in ("post_id", "folder") and v is not None}
        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"{RELAY_BASE_URL}/attachments",
                params=params,
                headers={"Authorization": f"Bearer {settings.api_key}"},
                timeout=10,
            )
            if response.status_code == 404:
                return [types.TextContent(type="text", text=f"Post #{arguments.get('post_id')} not found.")]
            response.raise_for_status()
            items = response.json()["items"]
        if not items:
            return [types.TextContent(type="text", text="No attachments found.")]
        lines = [f"{a['ref']}  —  {a['folder']}/assets ({a['bytes']} bytes)" for a in items]
        return [types.TextContent(type="text", text="\n".join(lines))]

    if name == "get_attachment":
        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"{RELAY_BASE_URL}/attachments/{arguments['name']}",
                headers={"Authorization": f"Bearer {settings.api_key}"},
                timeout=30,
            )
            if response.status_code == 404:
                return [types.TextContent(type="text", text=f"Attachment '{arguments['name']}' not found.")]
            response.raise_for_status()
            mime = response.headers.get("content-type", "application/octet-stream").split(";")[0]
            raw = response.content
        max_bytes = settings.attachment_max_mb * 1024 * 1024
        if len(raw) > max_bytes:
            return [types.TextContent(
                type="text",
                text=f"Attachment '{arguments['name']}' is {len(raw)} bytes — too large to return inline.",
            )]
        if mime.startswith("image/"):
            import base64 as _b64
            return [types.ImageContent(type="image", data=_b64.b64encode(raw).decode(), mimeType=mime)]
        return [types.TextContent(
            type="text",
            text=f"Retrieved '{arguments['name']}' ({mime}, {len(raw)} bytes) — not an image, can't show inline.",
        )]

    if name == "list_tags":
        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"{RELAY_BASE_URL}/tags",
                headers={"Authorization": f"Bearer {settings.api_key}"},
                timeout=10,
            )
            response.raise_for_status()
            tags = response.json()["tags"]

        text = "\n".join(f"{t['tag']} ({t['count']} posts)" for t in tags) if tags else "No tags yet."
        return [types.TextContent(type="text", text=text)]

    if name == "list_posts":
        summary = arguments.get("summary", True)
        params = {
            k: v for k, v in arguments.items()
            if k in ("tag", "folder", "search", "limit", "offset", "sort", "order", "mode")
        }
        params["summary"] = "true" if summary else "false"
        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"{RELAY_BASE_URL}/posts",
                params=params,
                headers={"Authorization": f"Bearer {settings.api_key}"},
                timeout=10,
            )
            if response.status_code == 503:
                return [types.TextContent(type="text", text="Semantic search is not enabled on this relay.")]
            if response.status_code == 400:
                return [types.TextContent(type="text", text=response.json().get("detail", "Invalid request."))]
            if response.status_code == 422:
                detail = response.json().get("detail")
                msg = detail[0]["msg"] if isinstance(detail, list) and detail else "Invalid list_posts request."
                return [types.TextContent(type="text", text=msg.removeprefix("Value error, "))]
            response.raise_for_status()
            data = response.json()
        posts = data.get("items", [])
        if not posts:
            return [types.TextContent(type="text", text="No posts found.")]
        lines = []
        for p in posts:
            header = f"#{p['id']}"
            if p.get("title"):
                header += f" — {p['title']}"
            if p.get("tags"):
                header += f" [{p['tags']}]"
            lines.append(header)
            body = p.get("excerpt") if summary else p.get("content", "")
            if body:
                lines.append(body)
            lines.append("")
        return [types.TextContent(type="text", text="\n".join(lines).strip())]

    if name == "get_post":
        post_id = arguments["id"]
        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"{RELAY_BASE_URL}/posts/{post_id}",
                headers={"Authorization": f"Bearer {settings.api_key}"},
                timeout=10,
            )
            response.raise_for_status()
            p = response.json()
        header = f"#{p['id']}"
        if p.get("title"):
            header += f" — {p['title']}"
        if p.get("tags"):
            header += f" [{p['tags']}]"
        return [types.TextContent(type="text", text=f"{header}\n\n{p['content']}")]

    if name == "get_post_history":
        post_id = arguments["id"]
        params = {"limit": arguments.get("limit", 20)}
        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"{RELAY_BASE_URL}/posts/{post_id}/history",
                params=params,
                headers={"Authorization": f"Bearer {settings.api_key}"},
                timeout=30,
            )
            if response.status_code == 503:
                return [types.TextContent(type="text", text="Vault history is disabled or git is unavailable.")]
            response.raise_for_status()
            data = response.json()
        items = data.get("items", [])
        if not items:
            return [types.TextContent(type="text", text=f"No history recorded for post #{post_id}.")]
        state = "exists" if data.get("exists") else "deleted — restorable"
        lines = [f"#{post_id} ({state}) — {len(items)} revision(s):"]
        lines += [f"  {r['short_sha']}  {r['when']}  {r['message']}" for r in items]
        return [types.TextContent(type="text", text="\n".join(lines))]

    if name == "list_deleted_posts":
        params = {
            "limit": arguments.get("limit", 50),
            "include_expiry": arguments.get("include_expiry", False),
        }
        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"{RELAY_BASE_URL}/posts/deleted",
                params=params,
                headers={"Authorization": f"Bearer {settings.api_key}"},
                timeout=30,
            )
            if response.status_code == 503:
                return [types.TextContent(type="text", text="Vault history is disabled or git is unavailable.")]
            response.raise_for_status()
            data = response.json()
        items = data.get("items", [])
        if not items:
            return [types.TextContent(type="text", text="Nothing deleted — or nothing left to recover.")]
        lines = [f"{len(items)} restorable deletion(s):"]
        lines += [
            f"  #{d['id']}  {d['title']}  [{d['reason']}]  {d['when']}  sha {d['short_sha']}"
            for d in items
        ]
        lines.append("Restore with restore_post(id=<id>, sha=<sha>).")
        return [types.TextContent(type="text", text="\n".join(lines))]

    if name == "get_post_revision":
        post_id, sha = arguments["id"], arguments["sha"]
        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"{RELAY_BASE_URL}/posts/{post_id}/history/{sha}",
                headers={"Authorization": f"Bearer {settings.api_key}"},
                timeout=30,
            )
            if response.status_code == 503:
                return [types.TextContent(type="text", text="Vault history is disabled or git is unavailable.")]
            if response.status_code == 404:
                return [types.TextContent(
                    type="text", text=f"No revision '{sha}' in the history of post #{post_id}.")]
            response.raise_for_status()
            d = response.json()
        head = f"#{post_id} at {d['short_sha']} ({d['when']}) — {d['message']}"
        tags = d.get("tags") or []
        return [types.TextContent(
            type="text",
            text=f"{head}\ntitle: {d['title']}\ntags: {', '.join(tags) or 'none'}\n\n{d['content']}",
        )]

    if name == "get_backlinks":
        post_id = arguments["id"]
        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"{RELAY_BASE_URL}/posts/{post_id}/backlinks",
                headers={"Authorization": f"Bearer {settings.api_key}"},
                timeout=10,
            )
            if response.status_code == 404:
                return [types.TextContent(type="text", text=f"Post #{post_id} not found.")]
            response.raise_for_status()
            items = response.json().get("items", [])
        if not items:
            return [types.TextContent(type="text", text=f"Nothing links to post #{post_id}.")]
        lines = [f"{len(items)} post(s) link to #{post_id}:"]
        lines += [f"  #{i['id']}  {i['title']}" for i in items]
        return [types.TextContent(type="text", text="\n".join(lines))]

    if name == "rename_tag":
        tag, new_name = arguments["tag"], arguments["new_name"]
        async with httpx.AsyncClient() as client:
            response = await client.patch(
                f"{RELAY_BASE_URL}/tags/{tag}",
                json={"new_name": new_name},
                headers={"Authorization": f"Bearer {settings.api_key}"},
                timeout=30,
            )
            if response.status_code == 422:
                return [types.TextContent(
                    type="text",
                    text="new_name must contain at least one letter, digit, hyphen or underscore.")]
            response.raise_for_status()
            # The key is `tags`, not `items` — TagListResponse does not follow the
            # `items` convention the post/attachment listings use.
            tags = response.json().get("tags", [])
        listing = ", ".join(f"{t['tag']} ({t['count']})" for t in tags) or "none"
        return [types.TextContent(type="text", text=f"Renamed. Tags now: {listing}")]

    if name == "restore_post":
        post_id = arguments["id"]
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{RELAY_BASE_URL}/posts/{post_id}/restore",
                json={"sha": arguments["sha"]},
                headers={"Authorization": f"Bearer {settings.api_key}"},
                timeout=30,
            )
            if response.status_code == 503:
                return [types.TextContent(type="text", text="Vault history is disabled or git is unavailable.")]
            if response.status_code == 404:
                return [types.TextContent(
                    type="text",
                    text=f"No revision '{arguments['sha']}' in the history of post #{post_id}.",
                )]
            response.raise_for_status()
            p = response.json()
        return [types.TextContent(
            type="text",
            text=f"Restored post #{p['id']} — {p['title']} (from {arguments['sha'][:7]}).",
        )]

    if name == "get_status":
        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"{RELAY_BASE_URL}/status",
                headers={"Authorization": f"Bearer {settings.api_key}"},
                timeout=30,
            )
            response.raise_for_status()
            d = response.json()
        v, f = d["vault"], d["features"]
        lines = [
            f"relay {d['version']} — up {d['uptime_seconds']}s ({RELAY_BASE_URL})",
            f"vault {v['path']}: {v['posts']} post(s), {v['tags']} tag(s), "
            f"{v['folders']} folder(s), {v['attachments']} attachment(s)",
            f"history: {'on' if f['history']['effective'] else 'OFF'} (git {f['history']['git'] or 'missing'})",
            f"search: {'FTS5' if f['search']['fts5'] else 'LIKE fallback'}",
            f"watcher: {'running' if f['watcher']['running'] else 'stopped'}",
            f"auth: oidc={f['auth']['oidc']} mcp_oauth={f['auth']['mcp_oauth']}",
        ]
        return [types.TextContent(type="text", text="\n".join(lines))]

    if name == "delete_post":
        post_id = arguments["id"]
        async with httpx.AsyncClient() as client:
            response = await client.delete(
                f"{RELAY_BASE_URL}/posts/{post_id}",
                headers={"Authorization": f"Bearer {settings.api_key}"},
                timeout=10,
            )
            if response.status_code == 404:
                return [types.TextContent(type="text", text=f"Post #{post_id} not found.")]
            response.raise_for_status()
        return [types.TextContent(type="text", text=f"Deleted post #{post_id}.")]

    if name == "update_post":
        post_id = arguments["id"]
        payload = {k: v for k, v in arguments.items() if k != "id"}
        async with httpx.AsyncClient() as client:
            response = await client.patch(
                f"{RELAY_BASE_URL}/posts/{post_id}",
                json=payload,
                headers={"Authorization": f"Bearer {settings.api_key}"},
                timeout=10,
            )
            if response.status_code == 404:
                return [types.TextContent(type="text", text=f"Post #{post_id} not found.")]
            response.raise_for_status()
            p = response.json()
        header = f"#{p['id']}"
        if p.get("title"):
            header += f" — {p['title']}"
        if p.get("tags"):
            header += f" [{', '.join(p['tags'])}]"
        return [types.TextContent(type="text", text=f"Updated post {header}\n\n{p['content']}")]

    if name == "set_tag_config":
        tag = arguments["tag"]
        payload = {k: v for k, v in arguments.items() if k != "tag"}
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{RELAY_BASE_URL}/tags/{tag}/config",
                json=payload,
                headers={"Authorization": f"Bearer {settings.api_key}"},
                timeout=10,
            )
            response.raise_for_status()
            cfg = response.json()
        parts = [f"Tag '{cfg['tag']}' configured:"]
        if cfg.get("ttl_hours"):
            parts.append(f"  ttl_hours = {cfg['ttl_hours']}")
        if cfg.get("expires_at"):
            parts.append(f"  expires_at = {cfg['expires_at']}")
        return [types.TextContent(type="text", text="\n".join(parts))]

    if name != "publish_post":
        raise ValueError(f"Unknown tool: {name}")

    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"{RELAY_BASE_URL}/posts",
            json=arguments,
            headers={"Authorization": f"Bearer {settings.api_key}"},
            timeout=10,
        )
        response.raise_for_status()
        post = response.json()

    return [
        types.TextContent(
            type="text",
            text=f"Published post #{post['id']} — tags: {post['tags'] or 'none'}",
        )
    ]


def main() -> None:
    import asyncio

    async def _run() -> None:
        async with mcp.server.stdio.stdio_server() as (read_stream, write_stream):
            await server.run(read_stream, write_stream, server.create_initialization_options())

    asyncio.run(_run())


if __name__ == "__main__":
    main()
