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
in ``_link_findings`` for why (relay #198 N-5 follow-up, L-10).

The master document (id=0) is exempt from every *convention* rule —
``zero_tags``/``missing_domain_tag``/``missing_type_tag``, ``stale_inbox``,
``h1_missing``/``h1_title_mismatch``, ``stale_last_updated``, ``zero_chunks``
— since a root index reasonably breaks tag, folder, H1 and staleness
conventions an ordinary post follows. It is *not* exempt from the link
rules: a broken cross-link is a factual defect there too, arguably more so
(an index's whole job is linking correctly), and it's still walked for
outbound links so posts it references get backlink credit.

Disposable snapshots (``folders.is_disposable``: digests, briefings) are exempt
from ``zero_backlinks``, and a link to a *deleted* one isn't reported: nothing
is expected to link back to a specific day's issue.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime

import aiosqlite

from . import database, folders, frontmatter, history, links, markdown_scan, vault, vectors
from .config import settings
from .models import LintFinding, LintReport, tags_from_sentinel

# Type axis (relay #0's tag table, second column). Kept in sync by hand, same
# footing as folders.DOMAINS for the domain axis — #0 is the source of truth.
TYPE_TAGS = {"reference", "plan", "briefing", "digest", "project", "profile", "hub", "playlist"}

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


@dataclass
class _LinkIndex:
    """What a post's links are classified against, built once per run."""

    title_to_id: dict[str, int]
    ids: set[int]
    attachment_names: set[str]
    high_water_mark: int
    # Deleted posts, keyed by id and by normalized title (both to the same
    # Deletion), so a broken link can be classified as "points at something
    # that used to exist" rather than a plain typo. Empty when history is off.
    deleted_by_id: dict[int, history.Deletion] = field(default_factory=dict)
    deleted_by_title: dict[str, history.Deletion] = field(default_factory=dict)
    # Backlinks credited so far, filled in by _link_findings.
    inbound_counts: dict[int, int] = field(default_factory=dict)
    _ephemeral: dict[int, bool] = field(default_factory=dict)

    async def is_ephemeral(self, d: history.Deletion) -> bool:
        """Whether a deleted post carried a disposable tag. A digest linking to
        last week's now-retention-deleted digests is expected to keep happening,
        forever, so it's suppressed rather than re-reported every run.
        Best-effort (a blob read can fail) and memoized per post id: more than
        one referencing post can point at the same deleted one."""
        if d.post_id not in self._ephemeral:
            text = await history.blob(d.sha, d.path)
            tags = frontmatter.parse(text)[0]["tags"] if text is not None else []
            self._ephemeral[d.post_id] = folders.is_disposable(tags)
        return self._ephemeral[d.post_id]


def _tag_findings(pid: int, title: str, path: str, tags: list[str]) -> list[LintFinding]:
    if not tags:
        return [_finding(
            "zero_tags", "error", "Post has no tags at all — untaggable, unfiled, unfindable by tag.",
            post_id=pid, title=title,
        )]
    found = []
    has_domain = any(t in folders.DOMAINS for t in tags)
    if not has_domain:
        found.append(_finding(
            "missing_domain_tag", "error",
            f"No domain tag ({', '.join(sorted(folders.DOMAINS))}).", post_id=pid, title=title,
        ))
    if not any(t in TYPE_TAGS for t in tags):
        found.append(_finding(
            "missing_type_tag", "error",
            f"No type tag ({', '.join(sorted(TYPE_TAGS))}).", post_id=pid, title=title,
        ))
    if has_domain and folders.folder_of(path, default=folders.INBOX) == folders.INBOX:
        found.append(_finding(
            "stale_inbox", "warning",
            "Has a domain tag but is still filed in Inbox/ — should have moved on its first tag.",
            post_id=pid, title=title,
        ))
    return found


def _h1_finding(pid: int, title: str, content: str) -> LintFinding | None:
    # strip_fences, not strip_code: a heading-shaped line inside a fenced
    # block must not count as the real H1 (posts 193, 257, 269 in the L-6
    # follow-up), but a real H1 containing inline code is real content of
    # that line, not a syntax example — strip_code's inline-span blanking
    # corrupted the comparison below for any post whose H1 legitimately had
    # one (relay #198 N-5, L-9).
    h1_match = _H1_RE.search(markdown_scan.strip_fences(content))
    if h1_match is None:
        return _finding(
            "h1_missing", "warning",
            "No H1 in the body at all — legacy post, predates the H1/title convention.",
            post_id=pid, title=title,
        )
    # Backticks are stripped for the comparison only: the title side never has
    # any (the filename sanitizer strips them), so an H1 that styles part of
    # itself as code would always compare unequal. The finding still quotes
    # raw_h1, backticks and all — the exact text a person needs to diagnose a
    # real mismatch (relay #198 N-5, L-9).
    raw_h1 = _collapse_whitespace(h1_match.group(1))
    title_text = _collapse_whitespace(title)
    if raw_h1.replace("`", "") == title_text:
        return None
    return _finding(
        "h1_title_mismatch", "warning",
        f"H1 reads {raw_h1!r}, filename/title is {title_text!r} — "
        "drifted after a rename (a sanitized filename does not rewrite the body's H1).",
        post_id=pid, title=title,
    )


def _staleness_finding(
    pid: int, title: str, tags: list[str], updated_at: str | None, created_at: str,
) -> LintFinding | None:
    if "hub" not in tags and "plan" not in tags:
        return None
    # updated_at is NULL until a post's first edit — falling back to created_at
    # (the same COALESCE the "updated" sort uses) is what lets a hub/plan post
    # created once and never edited again, the most common way one goes stale,
    # be old enough to flag at all.
    age = _days_since(updated_at or created_at)
    if age is None or age <= STALE_HUB_PLAN_DAYS:
        return None
    return _finding(
        "stale_last_updated", "warning",
        f"Tagged hub/plan but not updated in {int(age)} days (> {STALE_HUB_PLAN_DAYS}).",
        post_id=pid, title=title,
    )


async def _link_findings(pid: int, title: str, content: str, index: _LinkIndex) -> list[LintFinding]:
    """Link rules, credited backlinks as a side effect (``index.inbound_counts``).

    Collected as (rule, match) -> occurrences rather than one finding per
    mention: a footnote-style ref repeated several times in one post used to
    produce one identical finding each (relay #198 N-5 follow-up, L-10)."""
    link_hits: dict[tuple[str, str], int] = {}
    deleted_hit_source: dict[str, history.Deletion] = {}

    def hit(rule: str, raw: str) -> None:
        link_hits[(rule, raw)] = link_hits.get((rule, raw), 0) + 1

    for link in links.extract_links(content, index.title_to_id, index.ids):
        if link.kind == "embed":
            # Attachment embeds never count as a post backlink and never go
            # through the post classification below — they resolve against files.
            if link.target not in index.attachment_names:
                hit("broken_attachment_embed", link.raw)
            continue
        if link.resolved_id is not None:
            index.inbound_counts[link.resolved_id] = index.inbound_counts.get(link.resolved_id, 0) + 1
            continue
        if link.kind == "id" and not (_MIN_MEANINGFUL_ID_REF <= int(link.target) <= index.high_water_mark):
            # Not a plausible post reference at all (footnote, procedure step,
            # GitHub issue/PR, or an id never issued) — excluded outright rather
            # than downgraded: a false positive at a lower severity is still one.
            continue
        if link.kind == "wiki" and links.ATTACHMENT_EXT_RE.search(link.target):
            # A plain [[...]] to a filename was never going to resolve as a
            # post title — the wrong syntax, not a missing post (L-7).
            hit("wikilink_to_filename", link.raw)
            continue
        deletion = (
            index.deleted_by_id.get(int(link.target)) if link.kind == "id"
            else index.deleted_by_title.get(links.norm_title(link.target))
        )
        hit("link_to_deleted_post" if deletion is not None else "broken_link", link.raw)
        if deletion is not None:
            deleted_hit_source[link.raw] = deletion

    found = []
    for (rule, match), occurrences in link_hits.items():
        if rule == "link_to_deleted_post" and await index.is_ephemeral(deleted_hit_source[match]):
            continue
        severity, template = _LINK_RULES[rule]
        found.append(_finding(
            rule, severity, template.format(match=match),
            post_id=pid, title=title, match=match, occurrences=occurrences,
        ))
    return found


def _zero_chunks_finding(pid: int, title: str, content: str) -> LintFinding:
    # "likely a code-only body" is a real cause but not the only one — an
    # empty body chunks to zero rows too (relay #198 N-5 follow-up L-8).
    reason = "the body is empty" if not content.strip() else "likely a body that is entirely a fenced code block"
    return _finding("zero_chunks", "warning", f"Embedded to zero chunks — {reason}.", post_id=pid, title=title)


async def _empty_tag_config_findings(db: aiosqlite.Connection, live_tags: set[str]) -> list[LintFinding]:
    async with db.execute("SELECT tag FROM tag_config") as cur:
        configured = [r["tag"] for r in await cur.fetchall()]
    return [
        _finding(
            "empty_tag_config", "warning",
            f"tags.yml has an expiry or retention rule for '{tag}' but no post carries it.",
        )
        for tag in configured if tag not in live_tags
    ]


def _master_post_count_finding(master_row, post_count: int) -> LintFinding | None:
    m = _POST_COUNT_RE.search(master_row["content"][:600])
    if not m or int(m.group(1)) == post_count:
        return None
    return _finding(
        "master_doc_post_count", "warning",
        f"#0 says {m.group(1)} post(s), vault actually has {post_count}.",
        post_id=0, title=master_row["title"],
    )


async def run(db: aiosqlite.Connection) -> LintReport:
    findings: list[LintFinding] = []
    skipped: list[str] = []

    async with db.execute("SELECT id, title, path, content, tags, updated_at, created_at FROM posts") as cur:
        rows = await cur.fetchall()
    tags_by_id = {r["id"]: tags_from_sentinel(r["tags"]) for r in rows}

    ids = {r["id"] for r in rows}
    index = _LinkIndex(
        title_to_id={links.norm_title(r["title"]): r["id"] for r in rows},
        ids=ids,
        attachment_names={name for name, _folder, _size in vault.list_attachments()},
        high_water_mark=vault.read_id_counter(),
        inbound_counts=dict.fromkeys(ids, 0),
    )
    if history.enabled():
        for d in await history.deletions(limit=200):
            if d.post_id not in ids:
                index.deleted_by_id[d.post_id] = d
                index.deleted_by_title[links.norm_title(d.title)] = d
    else:
        skipped.append("link_to_deleted_post: vault history is disabled")

    # Zero-chunk posts: relay #253's own posts_missing_ids, reused rather than
    # re-deriving chunking here.
    if database.VEC_ENABLED and settings.embedding_enabled:
        zero_chunk_ids = set(await vectors.unembedded_post_ids(db, limit=len(rows) or 1))
    else:
        zero_chunk_ids = set()
        skipped.append("zero_chunks: semantic search is not enabled on this relay")

    for row in rows:
        pid, title, content, tags = row["id"], row["title"], row["content"], tags_by_id[row["id"]]
        # #0 is exempt from the convention rules but not the link rules — see
        # the module docstring.
        if pid != 0:
            findings += _tag_findings(pid, title, row["path"], tags)
            findings += filter(None, (
                _h1_finding(pid, title, content),
                _staleness_finding(pid, title, tags, row["updated_at"], row["created_at"]),
            ))
        findings += await _link_findings(pid, title, content, index)
        if pid != 0 and pid in zero_chunk_ids:
            findings.append(_zero_chunks_finding(pid, title, content))

    # Backlinks only once every post's links are counted; disposable snapshots
    # are exempt (nothing is expected to link back to a specific day's issue).
    findings += [
        _finding("zero_backlinks", "warning", "No other post links here via [[title]] or #id.",
                 post_id=pid, title=title)
        for pid, title in ((r["id"], r["title"]) for r in rows)
        if pid != 0 and not folders.is_disposable(tags_by_id[pid]) and index.inbound_counts[pid] == 0
    ]
    findings += await _empty_tag_config_findings(db, {t for tags in tags_by_id.values() for t in tags})
    master_row = next((r for r in rows if r["id"] == 0), None)
    if master_row is not None and (count_finding := _master_post_count_finding(master_row, len(rows))):
        findings.append(count_finding)

    # Post id ascending, not insertion order: zero_backlinks is computed in its
    # own pass and would otherwise land grouped at the end. A rule with no
    # single post (empty_tag_config) sorts last; sort() is stable, so findings
    # that share a post id keep their relative order.
    findings.sort(key=lambda f: (f.post_id is None, f.post_id or 0))
    return LintReport(items=findings, checked_posts=len(rows), skipped_rules=skipped)
