"""Vault history — a git commit per write (`relay/history.py`).

The point of the feature is recovery, so most of these assert that a *prior*
state can still be read back out of git after a destructive write, rather than
just that a commit exists.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

os.environ.setdefault("API_KEY", "test-key")

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from relay import database, history
from relay.auth import require_api_key
from relay.config import settings
from relay.main import app

AUTH = {"Authorization": "Bearer test-key"}
PNG = "iVBORw0KGgo="

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="needs a git binary")


@pytest.fixture(autouse=True)
def _reset_probe():
    """`history` caches whether git exists; tests toggle that, so clear it."""
    history.reset_state_for_tests()
    yield
    history.reset_state_for_tests()


@pytest_asyncio.fixture
async def client(monkeypatch):
    monkeypatch.setattr(settings, "history_enabled", True)  # conftest turns it off
    await database.init_db()
    await history.init()

    app.dependency_overrides[require_api_key] = lambda: None
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


def git(*args: str) -> str:
    r = subprocess.run(
        ["git", f"--git-dir={settings.history_dir}", f"--work-tree={settings.vault_path}", *args],
        capture_output=True, text=True,
    )
    assert r.returncode == 0, r.stderr
    return r.stdout.strip()


def log() -> list[str]:
    return git("log", "--format=%s").splitlines()


async def _create(client, title, content="body", tags=("homelab",)) -> dict:
    r = await client.post(
        "/posts", json={"title": title, "content": content, "tags": list(tags)}, headers=AUTH
    )
    assert r.status_code == 201, r.text
    return r.json()


# ── layout ───────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_repo_lives_in_relay_and_leaves_no_dot_git_in_the_vault(client):
    """No `.git` in the vault root: it would sync between machines (corruption)
    and show up in Obsidian. The git dir hides in the already-ignored .relay/."""
    assert Path(settings.history_dir).is_dir()
    assert not (Path(settings.vault_path) / ".git").exists()


@pytest.mark.asyncio
async def test_relay_control_dir_is_never_committed(client):
    await _create(client, "Anything")
    tracked = git("ls-files").splitlines()
    assert tracked, "nothing tracked at all"
    assert not [p for p in tracked if p.startswith(".relay")], tracked
    assert not (Path(settings.vault_path) / ".gitignore").exists()


# ── a commit per write ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_create_update_delete_each_commit(client):
    post = await _create(client, "Lifecycle")
    pid = post["id"]
    await client.patch(f"/posts/{pid}", json={"content": "v2"}, headers=AUTH)
    await client.delete(f"/posts/{pid}", headers=AUTH)

    messages = log()
    assert f"post {pid} create: Lifecycle" in messages
    assert f"post {pid} update: Lifecycle" in messages
    assert f"post {pid} delete: Lifecycle" in messages


@pytest.mark.asyncio
async def test_prior_body_is_recoverable_after_an_overwrite(client):
    """The scenario the feature exists for: a full-body replace clobbers a post."""
    post = await _create(client, "Canonical", content="the good version")
    await client.patch(
        f"/posts/{post['id']}", json={"content": "oops, wrong reconstruction"}, headers=AUTH
    )
    assert "the good version" not in (await client.get(f"/posts/{post['id']}", headers=AUTH)).json()["content"]

    path = git("ls-files", "*Canonical.md")
    assert "the good version" in git("show", f"HEAD~1:{path}")


@pytest.mark.asyncio
async def test_deleted_post_and_its_attachment_are_both_recoverable(client):
    """#39's failure mode, now with a way back — and both in the same commit, so
    one revert restores the note and the image it embedded."""
    post = await _create(client, "With Asset")
    await client.post(
        "/attachments", json={"filename": "chart.png", "data": PNG, "post_id": post["id"]}, headers=AUTH
    )
    path = git("ls-files", "*With Asset.md")
    await client.delete(f"/posts/{post['id']}", headers=AUTH)

    assert not (Path(settings.vault_path) / path).exists()
    restored = git("show", f"HEAD~1:{path}")
    assert "![[chart.png]]" in restored
    # `cat-file -e` rather than `show`: the asset is binary and must not be decoded
    assert git("cat-file", "-e", "HEAD~1:Homelab/assets/chart.png") == ""


@pytest.mark.asyncio
async def test_attachment_upload_commits_before_the_embed(client):
    post = await _create(client, "Embedder")
    await client.post(
        "/attachments", json={"filename": "pic.png", "data": PNG, "post_id": post["id"]}, headers=AUTH
    )
    messages = log()
    assert messages[0] == f"post {post['id']} update: Embedder"   # the embed
    assert messages[1] == "attachment add: pic.png"               # the upload before it


@pytest.mark.asyncio
async def test_tag_rename_is_one_commit(client):
    await _create(client, "Tagged", tags=("homelab",))
    await client.patch("/tags/homelab", json={"new_name": "lab"}, headers=AUTH)
    assert "tag rename: homelab -> lab (1 post(s))" in log()


@pytest.mark.asyncio
async def test_no_empty_commit_when_nothing_changed(client):
    await _create(client, "Static")
    before = len(log())
    assert await history.commit("nothing happened") is False
    assert len(log()) == before


# ── degradation: history must never gate a write ─────────────────────────────


@pytest.mark.asyncio
async def test_writes_succeed_when_git_is_missing(client, monkeypatch):
    monkeypatch.setattr(history.shutil, "which", lambda _name: None)
    history.reset_state_for_tests()
    post = await _create(client, "No Git Here")
    assert (await client.get(f"/posts/{post['id']}", headers=AUTH)).status_code == 200
    assert history.enabled() is False


@pytest.mark.asyncio
async def test_disabled_history_creates_no_repo(monkeypatch):
    monkeypatch.setattr(settings, "history_enabled", False)
    await database.init_db()
    await history.init()
    app.dependency_overrides[require_api_key] = lambda: None
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            await _create(c, "Unrecorded")
    finally:
        app.dependency_overrides.clear()
    assert not Path(settings.history_dir).exists()


# ── external edits: the changes relay never sees through its own API ──────────


@pytest.mark.asyncio
async def test_external_edit_is_committed_and_the_prior_body_recoverable(client):
    """An Obsidian/nvim edit goes straight to the file. This is the coverage a
    revisions table keyed on relay's own write paths would not give."""
    from relay import watcher

    await _create(client, "Human Edited", content="written by relay")
    path = Path(settings.vault_path) / git("ls-files", "*Human Edited.md")

    text = path.read_text(encoding="utf-8")
    path.write_text(text.replace("written by relay", "mangled by a human"), encoding="utf-8")
    await watcher._reconcile([str(path)])

    assert log()[0] == "external edit: Human Edited.md"
    rel = git("ls-files", "*Human Edited.md")
    assert "mangled by a human" in git("show", f"HEAD:{rel}")
    assert "written by relay" in git("show", f"HEAD~1:{rel}")


@pytest.mark.asyncio
async def test_bulk_external_change_is_one_commit(client):
    """A debounced batch commits once, not once per file."""
    from relay import watcher

    await _create(client, "Bulk A")
    await _create(client, "Bulk B")
    paths = []
    for title in ("Bulk A", "Bulk B"):
        p = Path(settings.vault_path) / git("ls-files", f"*{title}.md")
        p.write_text(p.read_text(encoding="utf-8") + "\nedited\n", encoding="utf-8")
        paths.append(str(p))

    before = len(log())
    await watcher._reconcile(paths)
    assert len(log()) == before + 1
    assert log()[0] == "external change: 2 edited, 0 removed"
