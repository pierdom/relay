"""YAML front-matter parsing/serialization and Obsidian-style filename rules.

A post on disk is a Markdown file: a YAML front-matter block (``id``, ``tags``,
``source``, ``created_at``, ``updated_at``, ``expires_at``) followed by the body.
The post *title* is NOT stored here — it is the filename stem (Obsidian-native).
"""
from __future__ import annotations

import datetime as _dt
import re
from pathlib import Path

import yaml

# Fields carried in front-matter. ``title`` is intentionally absent — it is the
# filename. Order here is the order written to disk.
_DATETIME_FIELDS = ("created_at", "updated_at", "expires_at")
_FIELD_ORDER = ("id", "tags", "source", *_DATETIME_FIELDS)

_FM_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", re.DOTALL)

# Chars Obsidian forbids in note names (filesystem-illegal + wikilink-breaking).
_ILLEGAL = re.compile(r'[/\\:*?"<>|\[\]#^]')
_MAX_STEM = 180  # leave headroom under the 255-byte filesystem limit


def _to_iso(value: object) -> str | None:
    """Coerce a YAML scalar (str or parsed datetime/date) back to an ISO string."""
    if value is None:
        return None
    if isinstance(value, _dt.datetime):
        s = value.astimezone(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        return s
    if isinstance(value, _dt.date):
        return value.strftime("%Y-%m-%dT00:00:00Z")
    return str(value)


def parse(text: str) -> tuple[dict, str]:
    """Split a file's text into (metadata, body). No front-matter → ({}, text)."""
    m = _FM_RE.match(text)
    if not m:
        return {}, text
    raw = yaml.safe_load(m.group(1)) or {}
    if not isinstance(raw, dict):
        return {}, text
    meta: dict = {}
    if raw.get("id") is not None:
        try:
            meta["id"] = int(raw["id"])
        except (TypeError, ValueError):
            pass
    tags = raw.get("tags") or []
    if isinstance(tags, str):
        tags = [t.strip() for t in tags.split(",") if t.strip()]
    meta["tags"] = [str(t) for t in tags]
    meta["source"] = raw.get("source")
    for f in _DATETIME_FIELDS:
        meta[f] = _to_iso(raw.get(f))
    return meta, m.group(2)


def serialize(meta: dict, body: str) -> str:
    """Render front-matter + body. ``meta`` uses the same keys ``parse`` returns."""
    ordered: dict = {}
    for key in _FIELD_ORDER:
        val = meta.get(key)
        if key == "tags":
            ordered[key] = list(val or [])
        elif val is not None:
            ordered[key] = val
    fm = yaml.safe_dump(
        ordered,
        sort_keys=False,
        default_flow_style=None,  # scalars block-style, the tags list inline
        allow_unicode=True,
    )
    body = body or ""
    if body and not body.endswith("\n"):
        body += "\n"
    return f"---\n{fm}---\n\n{body}"


def sanitize_title(title: str) -> str:
    """Turn a title into an Obsidian-safe filename stem (no extension)."""
    stem = _ILLEGAL.sub(" ", title or "")
    stem = re.sub(r"\s+", " ", stem).strip()
    stem = stem.strip(". ")  # Windows forbids trailing dot/space
    if len(stem) > _MAX_STEM:
        stem = stem[:_MAX_STEM].rstrip()
    return stem or "Untitled"


def sanitize_attachment_name(name: str) -> str:
    """Turn a user-supplied attachment filename into a safe basename (keeps the
    extension). Strips any path, replaces illegal chars, never traverses."""
    base = Path(name or "").name  # drop any directory component
    base = _ILLEGAL.sub("_", base)
    base = re.sub(r"\s+", " ", base).strip().strip(". ")
    if len(base) > _MAX_STEM:
        stem, dot, ext = base.rpartition(".")
        base = (stem[: _MAX_STEM - len(ext) - 1] + dot + ext) if dot else base[:_MAX_STEM]
    return base or "attachment"


def unique_path(vault_dir: Path, stem: str, *, exclude: Path | None = None) -> Path:
    """First free ``<stem>.md`` in ``vault_dir``, Obsidian-style ` 2`, ` 3` suffixing.

    ``exclude`` is treated as free (so renaming a file onto its own name is a no-op).
    """
    candidate = vault_dir / f"{stem}.md"
    if not candidate.exists() or (exclude and candidate == exclude):
        return candidate
    n = 2
    while True:
        candidate = vault_dir / f"{stem} {n}.md"
        if not candidate.exists() or (exclude and candidate == exclude):
            return candidate
        n += 1
