"""Vault lint (relay #198, N-5): the rules in #0 enforced from memory today,
machine-checked instead.

Every rule here is a transcription of something already written down in the
master document's prose (tag axes, folder placement, the H1/title convention,
`Last updated` on hub/plan posts) or already computable from data another
module maintains (broken links via ``relay.links``, embedding coverage via
``relay.vectors``, deleted posts via ``relay.history``). Nothing here decides
vault policy — it only checks the vault against policy #0 already states.

Read-only and safe to run often: one pass over ``posts`` plus a handful of
cheap follow-up queries, no writes. A rule that depends on a feature that is
off (history, embeddings) is skipped and named in ``LintReport.skipped_rules``
rather than raising — a lint pass should degrade like ``/status``, not 503.

``LintFinding.match`` carries the exact matched substring (``link.raw``, e.g.
``"[[Title]]"`` or ``"#42"``) for ``broken_link``/``link_to_deleted_post``/
``broken_attachment_embed`` — the rules with a single, unambiguous spot in
the content to point at — so a client can jump straight to it instead of
making someone search a long post for a wikilink that looks just like the
surrounding text. Every other rule leaves it ``None``: there is no single
substring "missing a domain tag" is about.

Link scanning (``relay.links.extract_links``, used below) runs against
``markdown_scan.strip_code``-ped content, not the raw post — relay #198's
N-5 follow-up (first real run against a 132-post vault, ~230 findings, ~70%
false positives, all on ``broken_link``) found that a post *documenting*
relay's own link syntax got flagged for its own examples, and a post with a
fenced shell/TOML snippet containing ``[[...]]`` got flagged for that
snippet's syntax. Two more id-ref false positives from the same run don't
need code-exclusion: a bare ``#N`` below 10 or above the vault's current
id high-water-mark (``vault.read_id_counter()``) is excluded from
``broken_link``/``link_to_deleted_post`` entirely, on the read that a
footnote marker, a procedure step, or a GitHub issue/PR number collides
with a real post id far more often than a genuine cross-link does at either
end of that range — ids 1-9 predate this vault's own conventions, and
nothing above the high-water mark has ever been issued to anything.
``!``-prefixed refs with a file extension (``![[photo.png]]``) are Obsidian
attachment embeds, not post links — resolved against the attachment table
(``vault.list_attachments``) instead, under their own rule,
``broken_attachment_embed``, so an unresolved one reads as what it is
rather than as a link to a nonexistent post. An extension-less embed
(``![[Some Note]]``, a note transclusion) still resolves against post
titles like an ordinary wikilink, matching main.js's own renderer.

The master document (id=0) is exempt from the *convention* rules — see
``lint_this_post`` in ``run()``: ``zero_tags``/``missing_domain_tag``/
``missing_type_tag``, ``stale_inbox``, ``h1_missing``/``h1_title_mismatch``,
``stale_last_updated``, and ``zero_chunks`` — since a root index reasonably
breaks tag, folder, H1 and staleness conventions an ordinary post follows
(no domain tag by design, possibly zero tags at all, a stylized H1), and its
embedding coverage is no more a convention than any of those. It is not
exempt from link checks (``broken_link``/``link_to_deleted_post``): those
are factual defects, not conventions #0 might reasonably skip, and arguably
matter more there than anywhere else — an index's whole job is linking
correctly. It is also still walked for outbound links regardless (so posts
it references get credit for the backlink) and still covered by
``master_doc_post_count``, which checks its own stated claim against
reality.
"""
from __future__ import annotations

import re
from datetime import UTC, datetime

import aiosqlite

from . import database, folders, history, links, markdown_scan, vault, vectors
from .config import settings
from .models import LintFinding, LintReport
from .service._common import _tags_from_sentinel

# Type axis (relay #0's tag table, second column). Kept in sync by hand, same
# footing as folders.DOMAINS for the domain axis — #0 is the source of truth.
TYPE_TAGS = {"reference", "plan", "briefing", "digest", "project", "profile", "hub", "playlist"}

# Tags whose posts are dated, disposable snapshots (briefings, daily digests):
# nothing is expected to link back to a specific day's issue, so zero backlinks
# on these is normal, not a finding. Mirrors folders.FALLBACK's digest-shaped
# entries exactly (daily-digest/news-digest included) — that table already
# names every tag this vault treats as "routes to Digests/, not linked back to".
_BACKLINK_EXEMPT_TAGS = {"digest", "news", "daily-digest", "news-digest", "briefing", "financial-analyst"}

# Hub/plan posts are meant to be kept current; flag one that hasn't been
# touched in this long. No config surface yet (#198 N-5 floats moving this to
# tags.yml) — a single constant until a second caller needs it configurable.
STALE_HUB_PLAN_DAYS = 60

# An unresolved #N below this is excluded from broken_link/link_to_deleted_post
# entirely — see the module docstring's L-5 paragraph. Single digits predate
# this vault's own conventions and collide completely with footnote markers
# and procedure steps in ordinary prose.
_MIN_MEANINGFUL_ID_REF = 10

# Requires a middot/dash immediately before the number — #0's own convention
# is "*Last updated: <date> · N post*" — rather than a bare `\d+\s*post\b`,
# which would also match the first "N post(s)" substring anywhere in the
# header block regardless of what it's actually counting (e.g. a per-domain
# breakdown mentioned before the real total).
_POST_COUNT_RE = re.compile(r"[·-]\s*(\d+)\s*post\b", re.IGNORECASE)
_H1_RE = re.compile(r"^\s*#\s+(.+?)\s*$", re.MULTILINE)


def _finding(
    rule: str, severity: str, detail: str, *,
    post_id: int | None = None, title: str | None = None, match: str | None = None,
) -> LintFinding:
    return LintFinding(rule=rule, severity=severity, post_id=post_id, title=title, detail=detail, match=match)


def _days_since(iso: str | None) -> float | None:
    if not iso:
        return None
    try:
        when = datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except ValueError:
        return None
    return (datetime.now(UTC) - when).total_seconds() / 86400


async def run(db: aiosqlite.Connection) -> LintReport:
    findings: list[LintFinding] = []
    skipped: list[str] = []

    async with db.execute("SELECT id, title, path, content, tags, updated_at, created_at FROM posts") as cur:
        rows = await cur.fetchall()

    title_to_id = {links.norm_title(r["title"]): r["id"] for r in rows}
    ids = {r["id"] for r in rows}
    inbound_counts = dict.fromkeys(ids, 0)
    attachment_names = {name for name, _folder, _size in vault.list_attachments()}
    high_water_mark = vault.read_id_counter()

    # Deleted-post lookup, for classifying a broken link as "points at
    # something that used to exist" rather than a plain typo. Best-effort:
    # skipped (not an error) when history is off, same as /status's approach
    # to a degraded feature.
    deleted_ids: set[int] = set()
    deleted_titles: set[str] = set()
    if history.enabled():
        for d in await history.deletions(limit=200):
            if d.post_id not in ids:
                deleted_ids.add(d.post_id)
                deleted_titles.add(links.norm_title(d.title))
    else:
        skipped.append("link_to_deleted_post: vault history is disabled")

    # Zero-chunk posts (relay #253's own posts_missing_ids, reused rather than
    # re-deriving chunking here — see vectors.unembedded_post_ids).
    if database.VEC_ENABLED and settings.embedding_enabled:
        zero_chunk_ids = set(await vectors.unembedded_post_ids(db, limit=len(rows) or 1))
    else:
        zero_chunk_ids = set()
        skipped.append("zero_chunks: semantic search is not enabled on this relay")

    master_row = next((r for r in rows if r["id"] == 0), None)

    for row in rows:
        pid, title, path, content, tags_raw, updated_at, created_at = (
            row["id"], row["title"], row["path"], row["content"], row["tags"],
            row["updated_at"], row["created_at"],
        )
        tags = _tags_from_sentinel(tags_raw)
        # The master document is exempt from every rule gated on this flag
        # below (tags, folder placement, H1/title, staleness, and embedding
        # coverage) — a root index reasonably breaks conventions an ordinary
        # post follows (no domain tag by design, possibly zero tags at all,
        # a stylized H1). It is NOT exempt from the link checks further down
        # (those run unconditionally): a broken cross-link is a factual
        # defect, not a convention #0 might reasonably skip. It is also
        # still walked for outbound links regardless, so posts it references
        # count their backlink from it, and master_doc_post_count (below,
        # after this loop) still checks its own stated count against
        # reality — that rule is about #0's accuracy, not a convention either.
        lint_this_post = pid != 0

        if lint_this_post:
            if not tags:
                findings.append(_finding(
                    "zero_tags", "error", "Post has no tags at all — untaggable, unfiled, unfindable by tag.",
                    post_id=pid, title=title,
                ))
            else:
                if not any(t in folders.DOMAINS for t in tags):
                    findings.append(_finding(
                        "missing_domain_tag", "error",
                        f"No domain tag ({', '.join(sorted(folders.DOMAINS))}).", post_id=pid, title=title,
                    ))
                if not any(t in TYPE_TAGS for t in tags):
                    findings.append(_finding(
                        "missing_type_tag", "error",
                        f"No type tag ({', '.join(sorted(TYPE_TAGS))}).", post_id=pid, title=title,
                    ))

            current_folder = folders.folder_of(path, default=folders.INBOX)
            if current_folder == folders.INBOX and any(t in folders.DOMAINS for t in tags):
                findings.append(_finding(
                    "stale_inbox", "warning",
                    "Has a domain tag but is still filed in Inbox/ — should have moved on its first tag.",
                    post_id=pid, title=title,
                ))

            # Scans stripped content: three of the L-6 follow-up's "no H1"
            # false readings (posts 193, 257, 269) were the rule picking up
            # the first heading-shaped line inside a fenced code block —
            # e.g. a banner comment like "# --- SYSTEM METRICS ---" — instead
            # of correctly finding no real H1 at all.
            h1_match = _H1_RE.search(markdown_scan.strip_code(content))
            if h1_match is None:
                findings.append(_finding(
                    "h1_missing", "warning",
                    "No H1 in the body at all — legacy post, predates the H1/title convention.",
                    post_id=pid, title=title,
                ))
            elif h1_match.group(1).strip() != title.strip():
                findings.append(_finding(
                    "h1_title_mismatch", "warning",
                    f"H1 reads {h1_match.group(1).strip()!r}, filename/title is {title!r} — "
                    "drifted after a rename (a sanitized filename does not rewrite the body's H1).",
                    post_id=pid, title=title,
                ))

            if ("hub" in tags or "plan" in tags):
                # updated_at is NULL until a post's first edit (service/posts.py
                # never backfills it at creation) — falling back to created_at
                # is the same COALESCE service/posts.py's own "updated" sort
                # already relies on, and without it a hub/plan post created
                # once and never edited again (the single most common way one
                # goes stale) would never be old enough to flag: _days_since(None)
                # is None, and the guard below skips it outright.
                age = _days_since(updated_at or created_at)
                if age is not None and age > STALE_HUB_PLAN_DAYS:
                    findings.append(_finding(
                        "stale_last_updated", "warning",
                        f"Tagged hub/plan but not updated in {int(age)} days (> {STALE_HUB_PLAN_DAYS}).",
                        post_id=pid, title=title,
                    ))

        # Link checks run for every post, master document included — unlike
        # the tag/folder/staleness conventions above, a broken cross-link is
        # a factual defect in #0 too (arguably more worth catching there: an
        # index's whole job is to link correctly), not a convention #0 might
        # reasonably not follow.
        for link in links.extract_links(content, title_to_id, ids):
            if link.kind == "embed":
                # Attachment embeds never count as a post backlink and never
                # go through the id-ref/deleted-post classification below —
                # they resolve against files, not posts.
                if link.target not in attachment_names:
                    findings.append(_finding(
                        "broken_attachment_embed", "error",
                        f"{link.raw} does not resolve to any existing attachment.",
                        post_id=pid, title=title, match=link.raw,
                    ))
                continue
            if link.resolved_id is not None:
                inbound_counts[link.resolved_id] = inbound_counts.get(link.resolved_id, 0) + 1
                continue
            if link.kind == "id" and not (_MIN_MEANINGFUL_ID_REF <= int(link.target) <= high_water_mark):
                # Below _MIN_MEANINGFUL_ID_REF or above the id high-water
                # mark: not a plausible post reference at all (footnote,
                # procedure step, GitHub issue/PR, or an id never issued) —
                # see the module docstring's L-5 paragraph. Excluded outright
                # rather than downgraded to a warning: a false positive
                # softened to a lower severity is still a false positive.
                continue
            points_at_deleted = (
                (link.kind == "id" and int(link.target) in deleted_ids)
                or (link.kind == "wiki" and links.norm_title(link.target) in deleted_titles)
            )
            if points_at_deleted:
                findings.append(_finding(
                    "link_to_deleted_post", "error",
                    f"{link.raw} points at a deleted post — restore it or drop the link.",
                    post_id=pid, title=title, match=link.raw,
                ))
            else:
                findings.append(_finding(
                    "broken_link", "error",
                    f"{link.raw} does not resolve to any existing post.",
                    post_id=pid, title=title, match=link.raw,
                ))

        if lint_this_post and pid in zero_chunk_ids:
            # "likely a code-only body" is a real cause but not the only
            # one — an empty body chunks to zero rows too, and asserting the
            # code-fence cause there is simply wrong (found on a post with no
            # content at all, relay #198 N-5 follow-up L-8). Only claim the
            # specific cause when the body isn't just empty.
            reason = (
                "the body is empty" if not content.strip()
                else "likely a body that is entirely a fenced code block"
            )
            findings.append(_finding(
                "zero_chunks", "warning", f"Embedded to zero chunks — {reason}.",
                post_id=pid, title=title,
            ))

    for row in rows:
        pid, title, tags = row["id"], row["title"], _tags_from_sentinel(row["tags"])
        if pid == 0 or any(t in _BACKLINK_EXEMPT_TAGS for t in tags):
            continue
        if inbound_counts.get(pid, 0) == 0:
            findings.append(_finding(
                "zero_backlinks", "warning", "No other post links here via [[title]] or #id.",
                post_id=pid, title=title,
            ))

    async with db.execute("SELECT tag FROM tag_config") as cur:
        configured_tags = [r["tag"] for r in await cur.fetchall()]
    live_tags: set[str] = set()
    for row in rows:
        live_tags.update(_tags_from_sentinel(row["tags"]))
    for tag in configured_tags:
        if tag not in live_tags:
            findings.append(_finding(
                "empty_tag_config", "warning",
                f"tags.yml has an expiry rule for '{tag}' but no post carries it.",
            ))

    if master_row is not None:
        m = _POST_COUNT_RE.search(master_row["content"][:600])
        if m and int(m.group(1)) != len(rows):
            findings.append(_finding(
                "master_doc_post_count", "warning",
                f"#0 says {m.group(1)} post(s), vault actually has {len(rows)}.",
                post_id=0, title=master_row["title"],
            ))

    # Post id ascending, not insertion order: zero_backlinks used to be
    # computed (and therefore appended) in its own pass after every other
    # rule, so it landed grouped at the end regardless of which post each
    # row was about — everything else was already interleaved by post id
    # purely by accident of loop order. A rule with no single post it's
    # about (empty_tag_config) sorts last; sort() is stable, so findings
    # that share a post id keep their original relative order.
    findings.sort(key=lambda f: (f.post_id is None, f.post_id or 0))

    return LintReport(items=findings, checked_posts=len(rows), skipped_rules=skipped)
