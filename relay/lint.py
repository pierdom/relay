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

``LintFinding.match`` carries the exact matched substring for the four link
rules (``broken_link``, ``link_to_deleted_post``, ``broken_attachment_embed``,
``wikilink_to_filename``) — the ones with a single, unambiguous spot in the
content to point at — so a client can jump straight to it instead of making
someone search a long post for a wikilink that looks just like the
surrounding text. Every other rule leaves it ``None``. Those same four rules
report one finding per distinct ``(rule, match)`` pair per post, not one per
mention, with the real count in ``occurrences`` — see the per-post link loop
in ``run()`` for why and how (relay #198 N-5 follow-up, L-10).

The master document (id=0) is exempt from every *convention* rule —
``zero_tags``/``missing_domain_tag``/``missing_type_tag``, ``stale_inbox``,
``h1_missing``/``h1_title_mismatch``, ``stale_last_updated``, ``zero_chunks``
— since a root index reasonably breaks tag, folder, H1 and staleness
conventions an ordinary post follows. It is *not* exempt from the link
rules: a broken cross-link is a factual defect there too, arguably more so
(an index's whole job is linking correctly), and it's still walked for
outbound links so posts it references get backlink credit. See
``lint_this_post`` in ``run()``.
"""
from __future__ import annotations

import re
from datetime import UTC, datetime

import aiosqlite

from . import database, folders, frontmatter, history, links, markdown_scan, vault, vectors
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
# Reused below for link_to_deleted_post: a deleted post that carried one of
# these tags was never going to stay linkable either — same "dated, expected
# to rot" judgment, one constant for both.
_BACKLINK_EXEMPT_TAGS = {"digest", "news", "daily-digest", "news-digest", "briefing", "financial-analyst"}

# Hub/plan posts are meant to be kept current; flag one that hasn't been
# touched in this long. No config surface yet (#198 N-5 floats moving this to
# tags.yml) — a single constant until a second caller needs it configurable.
STALE_HUB_PLAN_DAYS = 60

# An unresolved #N below this is excluded from broken_link/link_to_deleted_post
# entirely: single digits predate this vault's own conventions and collide
# completely with footnote markers and procedure steps in ordinary prose.
_MIN_MEANINGFUL_ID_REF = 10

# Requires a middot/dash immediately before the number — #0's own convention
# is "*Last updated: <date> · N post*" — rather than a bare `\d+\s*post\b`,
# which would also match the first "N post(s)" substring anywhere in the
# header block regardless of what it's actually counting (e.g. a per-domain
# breakdown mentioned before the real total).
_POST_COUNT_RE = re.compile(r"[·-]\s*(\d+)\s*post\b", re.IGNORECASE)
_H1_RE = re.compile(r"^\s*#\s+(.+?)\s*$", re.MULTILINE)
_WHITESPACE_RE = re.compile(r"\s+")

# Per-post link findings, keyed by (rule, exact matched text) so a target
# mentioned several times collapses into one finding with occurrences > 1
# instead of one per mention. Severity and detail wording live together here
# rather than at each append site.
_LINK_RULES: dict[str, tuple[str, str]] = {
    "broken_link": ("error", "{match} does not resolve to any existing post."),
    "link_to_deleted_post": ("error", "{match} points at a deleted post — restore it or drop the link."),
    "broken_attachment_embed": ("error", "{match} does not resolve to any existing attachment."),
    "wikilink_to_filename": (
        "warning",
        "{match} is a plain wikilink to a filename — embed it (![[...]]) or attach and drop the link.",
    ),
}


def _collapse_whitespace(text: str) -> str:
    return _WHITESPACE_RE.sub(" ", text).strip()


def _finding(
    rule: str, severity: str, detail: str, *,
    post_id: int | None = None, title: str | None = None, match: str | None = None, occurrences: int = 1,
) -> LintFinding:
    return LintFinding(
        rule=rule, severity=severity, post_id=post_id, title=title,
        detail=detail, match=match, occurrences=occurrences,
    )


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
    # to a degraded feature. Keyed by id and by normalized title, both to the
    # same Deletion, so a link_to_deleted_post candidate can look up what
    # tags that post carried when it went away — see _is_ephemeral below.
    deleted_by_id: dict[int, history.Deletion] = {}
    deleted_by_title: dict[str, history.Deletion] = {}
    if history.enabled():
        for d in await history.deletions(limit=200):
            if d.post_id not in ids:
                deleted_by_id[d.post_id] = d
                deleted_by_title[links.norm_title(d.title)] = d
    else:
        skipped.append("link_to_deleted_post: vault history is disabled")

    # A link to a deleted post that carried a rotation/expiry tag (the same
    # ones zero_backlinks already exempts, above) is expected to keep
    # happening — a digest referencing last week's now-retention-deleted
    # digests, forever — so it's suppressed rather than re-reported every
    # run. Best-effort (a blob read can fail) and memoized per post id: more
    # than one referencing post can point at the same deleted one.
    _ephemeral_cache: dict[int, bool] = {}

    async def _is_ephemeral(d: history.Deletion) -> bool:
        if d.post_id not in _ephemeral_cache:
            text = await history.blob(d.sha, d.path)
            tags = frontmatter.parse(text)[0]["tags"] if text is not None else []
            _ephemeral_cache[d.post_id] = any(t in _BACKLINK_EXEMPT_TAGS for t in tags)
        return _ephemeral_cache[d.post_id]

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
        # Gates every convention rule below, but not the link checks further
        # down (those run unconditionally) — see the module docstring for why.
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

            # strip_fences, not strip_code: a heading-shaped line inside a
            # fenced block must not count as the real H1 (posts 193, 257, 269
            # in the L-6 follow-up), but a real H1 containing inline code is
            # real content of that line, not a syntax example — strip_code's
            # inline-span blanking corrupted the comparison below for any
            # post whose H1 legitimately had one (relay #198 N-5, L-9).
            h1_match = _H1_RE.search(markdown_scan.strip_fences(content))
            if h1_match is None:
                findings.append(_finding(
                    "h1_missing", "warning",
                    "No H1 in the body at all — legacy post, predates the H1/title convention.",
                    post_id=pid, title=title,
                ))
            else:
                # A backtick is markdown formatting on an otherwise-matching
                # word, not sanitizer-relevant content — the title side is
                # implicitly backtick-free already (the filename sanitizer
                # strips them), so comparing raw text on both sides made an
                # H1 that legitimately styles part of itself as code compare
                # unequal to a title that never had backticks to begin with
                # (relay #198 N-5, L-9). Stripped only for *this* comparison:
                # the finding below still quotes raw_h1 (backticks and all)
                # once it decides there's a real mismatch, since that's the
                # exact text a person needs to see to diagnose it (P1 of the
                # same follow-up — the old blanked-and-collapsed detail text
                # was what made these false positives hard to tell apart
                # from real drift).
                raw_h1 = _collapse_whitespace(h1_match.group(1))
                h1_text = raw_h1.replace("`", "")
                title_text = _collapse_whitespace(title)
                if h1_text != title_text:
                    findings.append(_finding(
                        "h1_title_mismatch", "warning",
                        f"H1 reads {raw_h1!r}, filename/title is {title_text!r} — "
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
        #
        # Collected per post as (rule, match) -> occurrences rather than
        # appended straight to findings: a footnote-style ref repeated
        # several times in one post used to produce one identical finding
        # per mention (relay #198 N-5 follow-up, L-10). Emitted below once
        # this post's links are all classified.
        link_hits: dict[tuple[str, str], int] = {}
        deleted_hit_source: dict[str, history.Deletion] = {}
        for link in links.extract_links(content, title_to_id, ids):
            if link.kind == "embed":
                # Attachment embeds never count as a post backlink and never
                # go through the id-ref/deleted-post classification below —
                # they resolve against files, not posts.
                if link.target not in attachment_names:
                    key = ("broken_attachment_embed", link.raw)
                    link_hits[key] = link_hits.get(key, 0) + 1
                continue
            if link.resolved_id is not None:
                inbound_counts[link.resolved_id] = inbound_counts.get(link.resolved_id, 0) + 1
                continue
            if link.kind == "id" and not (_MIN_MEANINGFUL_ID_REF <= int(link.target) <= high_water_mark):
                # Not a plausible post reference at all (footnote, procedure
                # step, GitHub issue/PR, or an id never issued) — excluded
                # outright rather than downgraded to a warning: a false
                # positive softened to a lower severity is still one.
                continue
            if link.kind == "wiki" and links.ATTACHMENT_EXT_RE.search(link.target):
                # A *plain* [[...]] whose target is a filename was never
                # going to resolve as a post title — the wrong syntax, not a
                # missing post (relay #198 N-5 follow-up, L-7).
                key = ("wikilink_to_filename", link.raw)
                link_hits[key] = link_hits.get(key, 0) + 1
                continue
            deletion = (
                deleted_by_id.get(int(link.target)) if link.kind == "id"
                else deleted_by_title.get(links.norm_title(link.target))
            )
            rule = "link_to_deleted_post" if deletion is not None else "broken_link"
            key = (rule, link.raw)
            link_hits[key] = link_hits.get(key, 0) + 1
            if deletion is not None:
                deleted_hit_source[link.raw] = deletion

        for (rule, match), occurrences in link_hits.items():
            if rule == "link_to_deleted_post" and await _is_ephemeral(deleted_hit_source[match]):
                # A digest linking to last week's already-rotated digests
                # will make this finding again next week, forever — the
                # vault-side fix is separate; this is defence in depth
                # against re-reporting the same expected rot every run.
                continue
            severity, template = _LINK_RULES[rule]
            findings.append(_finding(
                rule, severity, template.format(match=match),
                post_id=pid, title=title, match=match, occurrences=occurrences,
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
