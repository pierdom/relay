"""A queryable changelog of every post-affecting write (relay #198, N-4).

Git history (`relay.history`) already records every write, but it has no
efficient "give me everything since X" query and no cursor that can
represent "an existing post changed" (a post id only means "created" —
relay #253's semantic-search phases and the September 2026 audit's B-10/G-07
both ran into this: SSE reconnect replay only caught *new* posts because
there was nothing else to key a catch-up query on).

The `changes` table here is a materialized index over git history, not a
second source of truth — it is rebuilt/caught-up from `history.commits()` at
startup (`sync`) exactly the way `relay.vault.rebuild_index` re-derives
`posts` from vault files, and stays in `index.db` on that same "disposable,
re-derivable" footing. Going forward, `record_latest` catches the table up
after every existing `history.commit(...)` call — the only addition at each
of those call sites is passing the post id(s) *that* write cares about, so
its own `events.publish`/`publish_delete` reliably gets the right `seq` even
under a race with another concurrent write (see `record_latest`'s docstring).
"""
from __future__ import annotations

import asyncio
import logging
import re
from pathlib import PurePosixPath

import aiosqlite

from . import frontmatter, history
from .models import normalize_expires_at

logger = logging.getLogger(__name__)

# Serializes `_catch_up`'s read-then-insert: two concurrent callers (relay's
# whole premise is multiple agents writing at once) can both read the same
# "last recorded sha" before either has inserted anything new, both compute
# the identical `sha..HEAD` range, and both redundantly re-ingest it —
# duplicating every row once per overlapping caller. The range fix alone
# (see `_catch_up`'s docstring) closes the *data-loss* failure mode of a
# `-1`-only fetch; it does nothing about two callers racing the same range,
# which needs this lock, the same pattern `vault.write_lock`/`history._lock`
# already use for their own critical sections. Found live, under genuine
# concurrent requests — five parallel creates produced up to three duplicate
# rows per commit, none of them caught by any single-caller test.
_lock = asyncio.Lock()


# Mirrors `service._common.MAX_HISTORY_LIMIT`/`_clamp` (also what REST's own
# `Query(ge=1, le=200)` already enforces for this endpoint) — duplicated
# rather than imported for the same reason `HistoryUnavailable` below is its
# own class and not `service`'s: `changes.py` sits below `relay.service`
# (which already imports `changes`), so importing `relay.service._common`
# back into it would import the `relay.service` package first (running its
# `__init__.py`, which imports `posts`, which imports `changes` — a real
# cycle, not just a style mismatch).
_MAX_LIMIT = 200


def _clamp_limit(value: int) -> int:
    return max(1, min(int(value), _MAX_LIMIT))


class HistoryUnavailable(Exception):
    """Raised by `list_changes` when history is off or git is missing — the
    table would otherwise just look permanently empty rather than signalling
    the feature can't work at all, the same distinction
    `service.HistoryUnavailable` draws for `list_deleted_posts`/
    `get_post_history`. A separate class (not that one) because `changes.py`
    sits below `relay.service` and mustn't import from it — `relay.service`
    already imports `changes`, and `service.__init__` importing `posts`
    importing `changes` importing back into `service._common` would be a
    real cycle, not just a style preference.
    """

# Matches every relay-initiated single-post write: "post 42 create: Title",
# "post 42 restore: Title (from abc1234)", etc. — one regex covers all six
# because they share the "post <id> <verb>:" shape; the verb *is* the action.
_POST_ACTION_RE = re.compile(r"^post \d+ (create|update|edit|append|delete|restore):")


def _classify_action(message: str, status: str) -> str | None:
    """The kind of change a commit represents, or None to skip it entirely
    (only `vault: initial import` — the startup baseline, not a change
    anyone published). `status` (git's A/M/D for this specific path) only
    matters for the "external change" batch case below, where one commit
    can mix edits and deletes across different posts and the message alone
    can't tell them apart."""
    if message == "vault: initial import":
        return None
    if message.startswith("ttl expiry"):
        return "expiry"
    matched = _POST_ACTION_RE.match(message)
    if matched:
        return matched.group(1)
    if message.startswith("tag rename:"):
        return "tag_rename"
    if message.startswith("external"):
        return "external_delete" if status == "D" else "external_edit"
    # Defensive, not expected: a future write path added a history.commit()
    # call with a message shape nothing here recognises. Recorded rather
    # than silently dropped, same principle as vectors.py never failing a
    # write over derived data — a lint pass over `action = 'other'` rows is
    # cheap and finds the gap; silently losing the row would not be.
    return "other"


def _tags_to_sentinel(tags: object) -> str:
    if not isinstance(tags, list):
        return ""
    return "," + ",".join(str(t) for t in tags) + "," if tags else ""


async def _resolve(status: str, path: str, sha: str) -> tuple[int, str, str] | None:
    """A post's (id, title, tags-as-sentinel-string) for one (status, path)
    touched at `sha`. Reads the blob at `sha` for an add/modify (the post as
    it now is) or at `sha^` for a delete (the post as it was just before,
    the only place its id/title/tags still exist) — same split
    `history._deletions_sync` already uses for the same reason. Tags are
    stored here (not joined from `posts` at query time) so a tag-filtered
    SSE reconnect can filter a *deleted* post's catch-up entry too — a
    deleted post has no `posts` row left to join against.
    """
    blob_sha = sha if status != "D" else f"{sha}^"
    text = await history.blob(blob_sha, path)
    if text is None:
        return None
    try:
        meta, _ = frontmatter.parse(text)
    except Exception:  # a half-written revision is simply not a match
        return None
    post_id = meta.get("id")
    if not isinstance(post_id, int):
        return None
    return post_id, PurePosixPath(path).stem, _tags_to_sentinel(meta.get("tags"))


async def _ingest(db: aiosqlite.Connection, commits: list[history.CommitPaths]) -> dict[int, int]:
    """Insert one `changes` row per (commit, post) pair. Returns
    ``{post_id: seq}`` for every row inserted (a later commit's entry wins
    if the same post appears more than once across `commits`)."""
    assigned: dict[int, int] = {}
    for commit in commits:
        # One post can appear under two paths in the same commit (a title
        # rename unlinks the old path and creates the new one in the same
        # write) — dedupe to the add/modify side, which reflects the post's
        # current name; a bare delete has only the one (D) path anyway.
        resolved: dict[int, tuple[str, str, str]] = {}   # post_id -> (title, tags, status)
        for status, path in commit.changes:
            if not path.endswith(".md"):
                continue   # attachments etc. — history.commits() doesn't filter by extension
            hit = await _resolve(status, path, commit.sha)
            if hit is None:
                continue
            post_id, title, tags = hit
            if post_id not in resolved or resolved[post_id][2] == "D":
                resolved[post_id] = (title, tags, status)
        for post_id, (title, tags, status) in resolved.items():
            action = _classify_action(commit.message, status)
            if action is None:
                continue
            # git's %aI carries the commit's own numeric UTC offset (e.g.
            # "+02:00"), not the "...Z" shape every other stored timestamp
            # in this codebase uses — normalize so `list_changes`'s lexical
            # `at > ?` comparison is actually valid across differing
            # offsets, the same reasoning `normalize_expires_at` exists for.
            try:
                at = normalize_expires_at(commit.when) or commit.when
            except ValueError:
                at = commit.when   # malformed timestamp: keep it rather than drop the row
            cur = await db.execute(
                "INSERT INTO changes (post_id, title, tags, action, at, sha, author) VALUES (?, ?, ?, ?, ?, ?, NULL)",
                (post_id, title, tags, action, at, commit.sha),
            )
            assigned[post_id] = cur.lastrowid
    if assigned:
        await db.commit()
    return assigned


async def _catch_up(db: aiosqlite.Connection) -> dict[int, int]:
    """Ingest every commit since the last recorded sha (or, on a genuinely
    empty table, the whole history) — the one operation both `sync` and
    `record_latest` are, at different times.

    Deliberately a *range* (`sha..HEAD`), not "just the latest commit
    (`-1`)": `history._lock` only wraps the git commit itself, not this
    read-and-insert afterward, so two concurrent writers' `history.commit()`
    calls can land in either order relative to each other's `_catch_up`
    call. A `-1`-based fetch used here first (relay #198, N-4's initial
    version) had two bugs from that same gap: calling it again with nothing
    new re-ingested the unchanged HEAD (`history.commit()` no-ops, e.g. the
    watcher's debounced reconcile over a batch that turned out to be all
    self-writes, are called unconditionally at every site) duplicating a
    row per extra call; and if *two* commits landed before either caller's
    turn to catch up, only the newer one was ever looked at — the older
    commit's row was silently lost until the next full restart's backfill.
    A range has neither failure mode: it returns empty when nothing changed
    (idempotent under a no-op or a repeated call) and spans however many
    commits landed since the last one recorded, however many callers were
    racing to record them.

    That range still has to be computed and ingested under `_lock`: without
    it, two concurrent callers can both read the same "last recorded sha"
    before either has inserted anything, both compute the identical range,
    and both redundantly re-ingest every commit in it — found live, under
    genuine concurrent requests, not by any single-caller test.
    """
    async with _lock:
        async with db.execute("SELECT sha FROM changes ORDER BY seq DESC LIMIT 1") as cur:
            row = await cur.fetchone()
        range_or_single = f"{row['sha']}..HEAD" if row is not None else "HEAD"
        commits = await history.commits(range_or_single)
        if not commits:
            return {}
        return await _ingest(db, commits)


async def sync(db: aiosqlite.Connection) -> None:
    """Catch the `changes` table up to git history. Called once at startup,
    after `vault.rebuild_index` — a normal restart finds ~0 new commits
    (cheap); only a fresh deploy or a lost/emptied table pays the one-time
    full-history walk, mirroring `history.init()`'s own "only do the
    expensive thing once" shape.
    """
    assigned = await _catch_up(db)
    if assigned:
        logger.info("Changelog: caught up %d change(s)", len(assigned))


async def record_latest(db: aiosqlite.Connection, *, post_ids: tuple[int, ...] = ()) -> dict[int, int]:
    """Catch up (see `_catch_up`), then guarantee every id in `post_ids` is
    present in the result even if a concurrent caller's own catch-up already
    recorded it first — a narrow race (two commits landing close enough
    together that both land before either side calls this), cheap and rare
    to check for directly. Called right after every existing
    `history.commit(...)` call with the post id(s) *that* call cares about,
    so its own `events.publish`/`publish_delete` reliably gets the right
    `seq` rather than occasionally losing the attribution race to whichever
    caller's catch-up ran first (the changes-log row itself is never lost
    either way — only which caller's return value reports the seq is at
    stake here).
    """
    assigned = dict(await _catch_up(db))
    for post_id in post_ids:
        if post_id in assigned:
            continue
        async with db.execute(
            "SELECT seq FROM changes WHERE post_id = ? ORDER BY seq DESC LIMIT 1", (post_id,)
        ) as cur:
            row = await cur.fetchone()
        if row is not None:
            assigned[post_id] = row["seq"]
    return assigned


async def list_changes(
    db: aiosqlite.Connection, *, since: str | None = None, limit: int = 50
) -> list[aiosqlite.Row]:
    """Changes newest-first, optionally starting after a `seq` (as a plain
    integer string) or an ISO-8601 `at` value. Neither given: the most
    recent `limit`.

    Raises `HistoryUnavailable` when history is off — the table is
    entirely derived from it, so it would otherwise just look permanently
    empty rather than signalling the feature can't work here at all.
    """
    if not history.enabled():
        raise HistoryUnavailable
    conditions: list[str] = []
    params: list[object] = []
    if since:
        if since.isdigit():
            conditions.append("seq > ?")
            params.append(int(since))
        else:
            conditions.append("at > ?")
            params.append(since)
    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
    params.append(_clamp_limit(limit))
    async with db.execute(
        f"SELECT * FROM changes {where} ORDER BY seq DESC LIMIT ?", params
    ) as cur:
        return list(await cur.fetchall())
