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
from pathlib import Path

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
