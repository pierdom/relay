"""Vault history: a git commit after every write.

The vault is the source of truth, and until now every write to it was
destructive — ``update_post`` replaces the whole body, ``delete_post`` unlinks,
and there is no version field, so a bad reconstruction by one agent silently
overwrote a canonical post with no way back. This module makes each write leave
a recoverable trace, using git rather than a bespoke revisions table so that
``git log`` / ``diff`` / ``revert`` work with no new API surface, and so the
history covers *everything* in the vault — attachments included — not just the
rows relay models as posts.

**Layout.** The repository lives at ``<vault>/.relay/history.git`` with the vault
itself as the work-tree, and every command passes ``--git-dir``/``--work-tree``
explicitly. Deliberately there is **no ``.git`` entry in the vault root**: the
vault is typically a Syncthing folder, and syncing a live object store between
machines is a well-known way to corrupt a repository. ``.relay/`` is already
excluded from Syncthing and invisible to Obsidian, so the history rides along
with the vault's own backups while staying out of every sync path. The ignore
rule for ``.relay/`` lives in the repo's ``info/exclude`` rather than a
``.gitignore``, so no relay bookkeeping file appears in the vault either.

**Failure policy.** History is a safety net, never a gate: every operation here
swallows its errors and logs. A broken or missing git must never fail a write.
If the ``git`` binary is absent the module disables itself after one warning.
"""
from __future__ import annotations

import asyncio
import logging
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from . import frontmatter
from .config import settings

logger = logging.getLogger(__name__)

# Serialises commits: every commit stages the whole tree (`git add -A`), so two
# concurrent ones would race over the index file and could attribute one change
# to the other's message.
_lock = asyncio.Lock()

# None = not yet probed. Set False once we know git is unusable, so a vault on a
# host without git logs the reason once instead of on every write.
_available: bool | None = None

# Pinned identity so commits never depend on the host's global git config, which
# a container almost certainly lacks.
_IDENTITY = ("-c", "user.name=relay", "-c", "user.email=relay@localhost")

_TIMEOUT_SECONDS = 30


def _git_dir() -> Path:
    return Path(settings.history_dir)


def _run(*args: str, timeout: int = _TIMEOUT_SECONDS) -> subprocess.CompletedProcess:
    """Run a git command against the vault work-tree. Never raises on a non-zero
    exit — callers decide what a failure means (an empty commit is rc=1)."""
    cmd = [
        "git",
        f"--git-dir={_git_dir()}",
        f"--work-tree={settings.vault_path}",
        *_IDENTITY,
        *args,
    ]
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def enabled() -> bool:
    return bool(settings.history_enabled) and _available is not False


def _probe() -> bool:
    """Whether history can run at all: turned on, and a git binary on PATH."""
    global _available
    if not settings.history_enabled:
        return False
    if _available is None:
        _available = shutil.which("git") is not None
        if not _available:
            logger.warning(
                "RELAY_HISTORY_ENABLED is on but no `git` binary is available — "
                "vault history is disabled. Writes are unaffected."
            )
    return _available


def _init_sync() -> bool:
    """Create the history repo if absent and stamp the ignore rule. Idempotent."""
    git_dir = _git_dir()
    if not (git_dir / "HEAD").exists():
        git_dir.parent.mkdir(parents=True, exist_ok=True)
        created = subprocess.run(
            ["git", "init", "--quiet", "--bare", str(git_dir)],
            capture_output=True, text=True, timeout=_TIMEOUT_SECONDS,
        )
        if created.returncode:
            logger.warning("Could not create the vault history repo: %s", created.stderr.strip())
            return False
        # A bare repo refuses a work-tree; this is the one setting that makes the
        # detached git-dir + explicit work-tree layout legal.
        subprocess.run(
            ["git", f"--git-dir={git_dir}", "config", "core.bare", "false"],
            capture_output=True, text=True, timeout=_TIMEOUT_SECONDS,
        )
    # In info/exclude, not a .gitignore: keeps relay's bookkeeping out of the vault.
    info = git_dir / "info"
    info.mkdir(parents=True, exist_ok=True)
    (info / "exclude").write_text(".relay/\n", encoding="utf-8")
    return True


def _commit_sync(message: str) -> bool:
    """Stage the whole vault and commit. True if a commit was actually created."""
    staged = _run("add", "-A")
    if staged.returncode:
        logger.warning("Vault history: staging failed — %s", staged.stderr.strip())
        return False
    done = _run("commit", "-m", message)
    if done.returncode == 0:
        return True
    # rc=1 with a clean tree is the normal "nothing changed" case, not an error:
    # a no-op update, or a write the previous commit already captured.
    if "nothing to commit" in done.stdout or "nothing added to commit" in done.stdout:
        return False
    logger.warning("Vault history: commit failed — %s", (done.stderr or done.stdout).strip())
    return False


async def init() -> None:
    """Prepare the history repo and capture the vault's current state.

    Called once from the app lifespan. The first run on an existing vault makes a
    single ``vault: initial import`` commit — the baseline every later diff is
    taken against.
    """
    if not _probe():
        return
    async with _lock:
        try:
            if not await asyncio.to_thread(_init_sync):
                return
            if await asyncio.to_thread(_commit_sync, "vault: initial import"):
                logger.info("Vault history initialised at %s", _git_dir())
        except (OSError, subprocess.SubprocessError) as exc:
            logger.warning("Vault history unavailable: %s", exc)


async def commit(message: str) -> bool:
    """Commit the vault's current state. Returns whether anything was recorded.

    Safe to call from any write path: it never raises and never blocks the event
    loop (git runs in a worker thread).
    """
    if not _probe():
        return False
    async with _lock:
        try:
            return await asyncio.to_thread(_commit_sync, message)
        except (OSError, subprocess.SubprocessError) as exc:
            logger.warning("Vault history: commit skipped — %s", exc)
            return False


def reset_state_for_tests() -> None:
    """Forget the cached git probe (tests toggle the setting between cases)."""
    global _available
    _available = None


# ── reading history back out ─────────────────────────────────────────────────
#
# Git keys history by *path*; relay keys posts by *id*, and a post's path changes
# when its title does and disappears when it's deleted. Everything below exists to
# bridge that gap safely.

# Record/field separators: a subject line can contain anything printable, so the
# parser keys on control characters rather than a punctuation convention.
_RS, _FS = "\x1e", "\x1f"
_LOG_FORMAT = f"{_RS}%H{_FS}%aI{_FS}%s"


@dataclass(frozen=True)
class Revision:
    """One commit that touched a post's file, with the path *as of that commit*."""

    sha: str
    when: str
    message: str
    path: str

    @property
    def short_sha(self) -> str:
        return self.sha[:7]


def _parse_log(out: str) -> list[Revision]:
    """Parse `git log --name-only` output into (commit, path) pairs."""
    revisions: list[Revision] = []
    sha = when = message = ""
    for line in out.split("\n"):
        if line.startswith(_RS):
            parts = line[1:].split(_FS)
            if len(parts) == 3:
                sha, when, message = parts
            continue
        path = line.strip()
        # --name-only prints one line per path touched; the pathspec filter means
        # only the post's own file appears, so the first is the one we want.
        if path and sha and not any(r.sha == sha for r in revisions):
            revisions.append(Revision(sha=sha, when=when, message=message, path=path))
    return revisions


def _historical_paths_sync(post_id: int) -> list[str]:
    """Paths a post's file has occupied, found by content rather than by name.

    The pickaxe reports the commits where that front-matter line appeared or
    vanished — i.e. where the file was created and where it was deleted — which
    gives a path for a post that no longer exists without depending on relay's
    commit-message convention holding.

    `-G` with an anchored regex, not `-S` with a literal: `-S"id: 2"` also matches
    `id: 21` (substring), which would drag an unrelated post's paths in. The id
    check in `_revisions_sync` would still filter them, but only after walking
    their history for nothing. Note the flag and its value must be **one** argv
    element — `["-G", regex]` and `"-G regex"` both silently match nothing.
    """
    got = _run("log", f"-G^id: {post_id}$", "--format=" + _LOG_FORMAT, "--name-only")
    if got.returncode:
        return []
    seen: list[str] = []
    for rev in _parse_log(got.stdout):
        if rev.path not in seen:
            seen.append(rev.path)
    return seen


def _blob_sync(sha: str, path: str) -> str | None:
    got = _run("show", f"{sha}:{path}")
    return got.stdout if got.returncode == 0 else None


def _post_id_of(sha: str, path: str) -> int | None:
    text = _blob_sync(sha, path)
    if text is None:
        return None
    try:
        meta, _ = frontmatter.parse(text)
    except Exception:  # a malformed or half-written revision is simply not a match
        return None
    pid = meta.get("id")
    return pid if isinstance(pid, int) else None


def _revisions_sync(post_id: int, current_path: str | None, limit: int) -> list[Revision]:
    if current_path:
        # The post still exists, so --follow walks the rename chain correctly.
        candidates = _parse_log(
            _run("log", "--follow", "--format=" + _LOG_FORMAT, "--name-only", "--", current_path).stdout
        )
    else:
        # Deleted. --follow must NOT be used here: with no path at HEAD, git's
        # rename detection latches onto an unrelated file and walks *its* history,
        # which would list another post's revisions under this id. Plain log over
        # every path this post is known to have occupied stays clean.
        paths = _historical_paths_sync(post_id)
        if not paths:
            return []
        candidates = _parse_log(
            _run("log", "--format=" + _LOG_FORMAT, "--name-only", "--", *paths).stdout
        )

    # Verify each revision really belongs to this post. Titles are filenames, so a
    # deleted note's path can later be reused by a different post; without this a
    # restore could write another post's body under this id.
    out: list[Revision] = []
    for rev in candidates:
        if len(out) >= limit:
            break
        if _post_id_of(rev.sha, rev.path) == post_id:
            out.append(rev)
    return _truncate_at_creation(out, post_id)


def _truncate_at_creation(revs: list[Revision], post_id: int) -> list[Revision]:
    """Cut the history at this post's own creation.

    ``allocate_id`` is ``MAX(id)+1``, so deleting the newest post hands its id
    straight to the next one created. If that successor also takes the same title
    it takes the same *path* too, and the walk runs back through the delete into
    the previous occupant's revisions — which carry the same front-matter id, so
    the check above cannot tell them apart. Restoring one would then overwrite the
    live post with a stranger's body.

    A post's history starts at its `post <id> create:` commit; anything older
    belongs to a previous holder of that id. Posts that predate history, or that
    were created externally and indexed by the watcher, have no such commit — the
    list is then returned unchanged, which is the best that can be said about them.

    The underlying id reuse is a separate defect; this keeps it from being
    destructive here.
    """
    marker = f"post {post_id} create:"
    for i, rev in enumerate(revs):
        if rev.message.startswith(marker):
            return revs[: i + 1]
    return revs


async def revisions(post_id: int, *, current_path: str | None, limit: int = 20) -> list[Revision]:
    """Commits that touched this post's file, newest first."""
    if not _probe():
        return []
    try:
        return await asyncio.to_thread(_revisions_sync, post_id, current_path, limit)
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning("Vault history: could not read revisions — %s", exc)
        return []


async def blob(sha: str, path: str) -> str | None:
    """The full file text at a revision, or None if it isn't there."""
    if not _probe():
        return None
    try:
        return await asyncio.to_thread(_blob_sync, sha, path)
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning("Vault history: could not read %s:%s — %s", sha, path, exc)
        return None
