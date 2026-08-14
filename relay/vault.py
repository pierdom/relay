"""Canonical filesystem layer: posts are Markdown files in an Obsidian vault.

Files are the source of truth; the SQLite index (``relay.database``) is a
disposable mirror rebuilt from these files. Every write goes file-first, then
mirrors into the index. ``id`` lives in front-matter; the title is the filename.
"""
from __future__ import annotations

import asyncio
import datetime as _dt
import hashlib
import logging
import os
import tempfile
from pathlib import Path

import aiosqlite

from . import folders, frontmatter
from .config import settings

logger = logging.getLogger(__name__)

# Serializes id allocation + file write + index upsert for create/update.
write_lock = asyncio.Lock()

MASTER_ID = 0
MASTER_TITLE = "Master Document"
MASTER_CONTENT = (
    "# Master Document\n\n"
    "Index, naming conventions, and instructions for AI agents interacting with this relay.\n"
)

# Self-write suppression so the watcher ignores changes relay itself made.
_written: dict[str, str] = {}   # abspath -> sha256 of the text we last wrote
_deleted: set[str] = set()      # abspaths we just unlinked


def utcnow_iso() -> str:
    return _dt.datetime.now(_dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def vault_dir() -> Path:
    return Path(settings.vault_path)


def relpath(path: Path) -> str:
    return str(Path(path).resolve().relative_to(vault_dir().resolve()))


def abspath(relpath: str) -> Path:
    return (vault_dir() / relpath).resolve()


def _tags_to_sentinel(tags: list[str]) -> str:
    return "," + ",".join(tags) + "," if tags else ""


# ── self-write suppression (used by the watcher) ────────────────────────────


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def note_write(path: Path, text: str) -> None:
    _written[str(path.resolve())] = _sha(text)


def note_delete(path: Path) -> None:
    _deleted.add(str(path.resolve()))


def was_self_write(path: Path, current_text: str) -> bool:
    return _written.get(str(path.resolve())) == _sha(current_text)


def was_self_delete(path: Path) -> bool:
    """True if relay just unlinked this path (consumes the suppression)."""
    key = str(path.resolve())
    if key in _deleted:
        _deleted.discard(key)
        return True
    return False


# ── file I/O ────────────────────────────────────────────────────────────────


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def write_file(
    *,
    id: int,
    title: str,
    content: str,
    tags: list[str],
    source: str | None,
    created_at: str,
    updated_at: str | None,
    expires_at: str | None,
    old_path: Path | None = None,
    move_to_folder: str | None = None,
) -> Path:
    """Write a post to disk; rename from ``old_path`` if the title changed.

    Folder placement: a file edited in place stays in its current directory
    (``old_path``'s parent) — relay never relocates a post on retag, *except* when
    ``move_to_folder`` is given (used only for the Inbox→domain move on first tag).
    A brand-new file (no ``old_path``) is filed by ``folders.folder_for``.

    Returns the (possibly new) path. Records the write for watcher suppression.
    """
    stem = frontmatter.sanitize_title(title)
    if move_to_folder is not None:
        target_dir = vault_dir() / move_to_folder
    elif old_path is not None:
        target_dir = old_path.parent
    else:
        target_dir = vault_dir() / folders.folder_for(id, tags)
    new_path = frontmatter.unique_path(target_dir, stem, exclude=old_path)
    meta = {
        "id": id,
        "tags": tags,
        "source": source,
        "created_at": created_at,
        "updated_at": updated_at,
        "expires_at": expires_at,
    }
    text = frontmatter.serialize(meta, content)
    _atomic_write(new_path, text)
    note_write(new_path, text)
    if old_path is not None and old_path.resolve() != new_path.resolve():
        try:
            os.unlink(old_path)
            note_delete(old_path)
        except OSError:
            pass
    return new_path


def delete_file(path: Path) -> None:
    try:
        os.unlink(path)
        note_delete(path)
    except FileNotFoundError:
        pass


def read_file(path: Path) -> tuple[dict, str]:
    return frontmatter.parse(Path(path).read_text(encoding="utf-8"))


def resolve_attachment(name: str) -> Path | None:
    """Resolve an attachment reference to a real file inside the vault.

    ``name`` comes from an Obsidian embed (``![[file]]`` / ``[[file.pdf]]``) or a
    vault-relative path. A bare filename is located by scanning ``*/assets/``
    dirs (the convention for attachments); a path with separators is resolved
    directly. Returns an existing file **inside the vault and outside ``.relay``**,
    or ``None`` — the security boundary against path traversal.
    """
    name = (name or "").strip()
    if not name:
        return None
    vault = vault_dir().resolve()
    relay = Path(settings.relay_dir).resolve()

    def _ok(candidate: Path) -> Path | None:
        try:
            rp = candidate.resolve()
        except OSError:
            return None
        if not rp.is_file() or not rp.is_relative_to(vault):
            return None
        if rp == relay or relay in rp.parents:
            return None
        return rp

    if "/" in name or "\\" in name:
        return _ok(vault / name)
    # Bare filename: look for an exact match under any ``assets/`` folder. Join the
    # literal name (never glob it — a name like ``*.png`` must not act as a pattern).
    for assets in vault.rglob(ATTACHMENTS_DIRNAME):
        if assets.is_dir():
            hit = _ok(assets / name)
            if hit is not None:
                return hit
    return None


ATTACHMENTS_DIRNAME = "assets"


def attachment_dir_for(folder: str) -> Path:
    """The ``assets/`` directory for a first-level folder (``Inbox`` when blank)."""
    safe = Path(folder or folders.INBOX).name or folders.INBOX  # no separators/traversal
    return vault_dir() / safe / ATTACHMENTS_DIRNAME


def _unique_attachment_name(name: str) -> str:
    """A filename free across *every* ``assets/`` dir, Obsidian-style ` N` suffixed
    before the extension. Vault-global (not per-folder) so a bare ``![[name]]``
    resolves to exactly one file — two folders can't both hold ``chart.png``."""
    taken = {n.lower() for (n, _folder, _size) in list_attachments()}
    if name.lower() not in taken:
        return name
    stem, dot, ext = name.rpartition(".")
    if not dot:
        stem, ext = name, ""
    n = 1
    while True:
        candidate = f"{stem} {n}.{ext}" if ext else f"{stem} {n}"
        if candidate.lower() not in taken:
            return candidate
        n += 1


def write_attachment(folder: str, filename: str, data: bytes) -> Path:
    """Write ``data`` into ``<folder>/assets/`` under a sanitized, vault-globally
    unique name. Attachments aren't `.md`, so the index/watcher ignore them — no
    self-write suppression needed. Returns the written path."""
    target_dir = attachment_dir_for(folder)
    target_dir.mkdir(parents=True, exist_ok=True)
    name = _unique_attachment_name(frontmatter.sanitize_attachment_name(filename))
    path = target_dir / name
    fd, tmp = tempfile.mkstemp(dir=str(target_dir), suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return path


# Extensions mimetypes often can't guess but that relay treats as images.
_IMAGE_MIME_FALLBACK = {
    ".avif": "image/avif", ".bmp": "image/bmp", ".svg": "image/svg+xml", ".webp": "image/webp",
}


def attachment_mime(path: Path) -> str:
    import mimetypes

    mime, _ = mimetypes.guess_type(str(path))
    return mime or _IMAGE_MIME_FALLBACK.get(path.suffix.lower(), "application/octet-stream")


def list_attachments(folder: str | None = None) -> list[tuple[str, str, int]]:
    """``(filename, folder, size)`` for files under ``assets/`` dirs. ``folder``
    limits the scan to that first-level folder; ``None`` scans the whole vault."""
    root = vault_dir().resolve()
    dirs = [attachment_dir_for(folder)] if folder else [
        p for p in root.rglob(ATTACHMENTS_DIRNAME) if p.is_dir()
    ]
    results: list[tuple[str, str, int]] = []
    for d in dirs:
        if not d.is_dir():
            continue
        try:
            rel = d.resolve().relative_to(root)
        except ValueError:
            continue
        folder_name = rel.parts[0] if rel.parts else ""
        for f in sorted(d.iterdir()):
            # skip transient .tmp write artifacts (left only by a crashed write)
            if f.is_file() and f.suffix != ".tmp":
                results.append((f.name, folder_name, f.stat().st_size))
    return results


def move_attachment(from_folder: str, to_folder: str, name: str) -> bool:
    """Move ``name`` from ``from_folder/assets`` to ``to_folder/assets``. No-op
    (returns False) if the source is missing or the destination already exists."""
    src = attachment_dir_for(from_folder) / name
    if not src.is_file():
        return False
    dst_dir = attachment_dir_for(to_folder)
    dst = dst_dir / name
    if dst.exists():
        return False
    dst_dir.mkdir(parents=True, exist_ok=True)
    os.replace(src, dst)
    return True


def delete_attachment(name: str) -> Path | None:
    """Resolve and unlink an attachment (bare name or vault-relative path). Returns
    the removed path, or ``None`` if it didn't resolve inside the vault."""
    path = resolve_attachment(name)
    if path is None:
        return None
    try:
        path.unlink()
    except OSError:
        return None
    return path


def read_attachment(name: str, *, max_bytes: int | None = None) -> tuple[Path, bytes, str] | None:
    """Resolve and read an attachment. Returns ``(path, bytes, mime)`` or ``None``.

    Stats the file before reading; raises ``ValueError`` if it exceeds ``max_bytes``
    so a huge file is never slurped into memory / an LLM context.
    """
    path = resolve_attachment(name)
    if path is None:
        return None
    if max_bytes is not None and path.stat().st_size > max_bytes:
        raise ValueError(f"attachment is larger than {max_bytes} bytes")
    return path, path.read_bytes(), attachment_mime(path)


# ── index mirror ─────────────────────────────────────────────────────────────


async def allocate_id(db: aiosqlite.Connection) -> int:
    async with db.execute("SELECT COALESCE(MAX(id), 0) FROM posts") as cur:
        row = await cur.fetchone()
    return int(row[0]) + 1


async def path_for_id(db: aiosqlite.Connection, post_id: int) -> Path | None:
    async with db.execute("SELECT path FROM posts WHERE id = ?", (post_id,)) as cur:
        row = await cur.fetchone()
    return abspath(row[0]) if row is not None else None


async def index_upsert(
    db: aiosqlite.Connection,
    *,
    id: int,
    title: str,
    path: Path,
    content: str,
    tags: list[str],
    source: str | None,
    created_at: str,
    updated_at: str | None,
    expires_at: str | None,
) -> None:
    await db.execute(
        """
        INSERT INTO posts (id, title, path, content, tags, source, created_at, updated_at, expires_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            title=excluded.title, path=excluded.path, content=excluded.content,
            tags=excluded.tags, source=excluded.source, created_at=excluded.created_at,
            updated_at=excluded.updated_at, expires_at=excluded.expires_at
        """,
        (id, title, relpath(path), content, _tags_to_sentinel(tags), source,
         created_at, updated_at, expires_at),
    )


async def index_insert(
    db: aiosqlite.Connection,
    *,
    id: int,
    title: str,
    path: Path,
    content: str,
    tags: list[str],
    source: str | None,
    created_at: str,
    updated_at: str | None,
    expires_at: str | None,
) -> None:
    """Plain INSERT for a brand-new post — **no** ``ON CONFLICT``.

    Unlike :func:`index_upsert`, a duplicate ``id`` raises ``IntegrityError``
    instead of silently overwriting the existing row. ``create_post`` relies on
    that so a lost allocate/insert race surfaces loudly (and is retried) rather
    than clobbering a post.
    """
    await db.execute(
        """
        INSERT INTO posts (id, title, path, content, tags, source, created_at, updated_at, expires_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (id, title, relpath(path), content, _tags_to_sentinel(tags), source,
         created_at, updated_at, expires_at),
    )


async def index_delete(db: aiosqlite.Connection, post_id: int) -> None:
    await db.execute("DELETE FROM posts WHERE id = ?", (post_id,))


# ── startup rebuild ───────────────────────────────────────────────────────────


def _iter_notes() -> list[Path]:
    """All ``.md`` notes in the vault, recursively, excluding the ``.relay`` dir."""
    relay_dir = str(Path(settings.relay_dir).resolve())
    return [
        p for p in vault_dir().rglob("*.md")
        if not str(p.resolve()).startswith(relay_dir)
    ]


def _ensure_master_file() -> None:
    """Create Master Document.md (id=0) if no file already claims id=0."""
    for path in _iter_notes():
        try:
            meta, _ = read_file(path)
        except (OSError, UnicodeDecodeError):
            continue
        if meta.get("id") == MASTER_ID:
            return
    write_file(
        id=MASTER_ID,
        title=MASTER_TITLE,
        content=MASTER_CONTENT,
        tags=[],
        source=None,
        created_at=utcnow_iso(),
        updated_at=None,
        expires_at=None,
    )


async def rebuild_index(db: aiosqlite.Connection) -> int:
    """Wipe and repopulate the index from the vault. Stamps ids into id-less files.

    Returns the number of posts indexed.
    """
    vault_dir().mkdir(parents=True, exist_ok=True)
    Path(settings.relay_dir).mkdir(parents=True, exist_ok=True)
    _ensure_master_file()

    files = sorted(
        _iter_notes(),
        key=lambda p: (p.stat().st_mtime, p.name),
    )

    parsed: list[tuple[Path, dict, str]] = []
    seen_ids: set[int] = set()
    max_id = 0
    needs_id: list[tuple[Path, dict, str]] = []

    for path in files:
        try:
            meta, body = read_file(path)
        except (OSError, UnicodeDecodeError) as exc:
            logger.warning("Skipping unreadable note %s: %s", path.name, exc)
            continue
        pid = meta.get("id")
        if pid is None or pid in seen_ids:
            needs_id.append((path, meta, body))
            continue
        seen_ids.add(pid)
        max_id = max(max_id, pid)
        parsed.append((path, meta, body))

    # Stamp ids into hand-created / colliding notes, rewriting their front-matter.
    for path, meta, body in needs_id:
        max_id += 1
        meta["id"] = max_id
        seen_ids.add(max_id)
        new_path = write_file(
            id=max_id,
            title=path.stem,
            content=body,
            tags=meta.get("tags") or [],
            source=meta.get("source"),
            created_at=meta.get("created_at") or utcnow_iso(),
            updated_at=meta.get("updated_at"),
            expires_at=meta.get("expires_at"),
            old_path=path,
        )
        parsed.append((new_path, meta, body))

    await db.execute("DELETE FROM posts")
    for path, meta, body in parsed:
        await index_upsert(
            db,
            id=meta["id"],
            title=path.stem,
            path=path,
            content=body,
            tags=meta.get("tags") or [],
            source=meta.get("source"),
            created_at=meta.get("created_at") or utcnow_iso(),
            updated_at=meta.get("updated_at"),
            expires_at=meta.get("expires_at"),
        )
    await _load_tag_config(db)
    await db.commit()
    logger.info("Index rebuilt from %s — %d post(s)", vault_dir(), len(parsed))
    return len(parsed)


# ── tag config (.relay/tags.yml is canonical, mirrored to the index) ──────────


async def _load_tag_config(db: aiosqlite.Connection) -> None:
    import yaml

    await db.execute("DELETE FROM tag_config")
    cfg_path = Path(settings.tags_config_path)
    if not cfg_path.exists():
        return
    try:
        data = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        logger.warning("Could not read %s: %s", cfg_path, exc)
        return
    for tag, cfg in (data.items() if isinstance(data, dict) else []):
        cfg = cfg or {}
        await db.execute(
            "INSERT OR REPLACE INTO tag_config (tag, ttl_hours, expires_at) VALUES (?, ?, ?)",
            (tag, int(cfg.get("ttl_hours") or 0), cfg.get("expires_at")),
        )


async def write_tag_config(db: aiosqlite.Connection) -> None:
    """Dump the index's tag_config back out to .relay/tags.yml (canonical)."""
    import yaml

    async with db.execute("SELECT tag, ttl_hours, expires_at FROM tag_config ORDER BY tag") as cur:
        rows = await cur.fetchall()
    data: dict = {}
    for row in rows:
        entry: dict = {}
        if row["ttl_hours"]:
            entry["ttl_hours"] = row["ttl_hours"]
        if row["expires_at"]:
            entry["expires_at"] = row["expires_at"]
        data[row["tag"]] = entry
    Path(settings.relay_dir).mkdir(parents=True, exist_ok=True)
    text = yaml.safe_dump(data, sort_keys=True, allow_unicode=True, default_flow_style=False)
    _atomic_write(Path(settings.tags_config_path), text)
