"""Unit tests for relay/vault.py pure helpers.

The integration paths (create/update/delete via the API) are covered elsewhere.
These target the pieces that are testable in isolation and where a bug would be
non-obvious: the attachment name dedup logic, the path-traversal guard in
resolve_attachment, and the single-use semantics of was_self_delete.
"""
from __future__ import annotations

from relay import vault
from relay.config import settings

# ── was_self_delete: consumed on first call ───────────────────────────────────


def test_was_self_delete_returns_true_once_then_false(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "vault_path", str(tmp_path / "vault"))
    p = tmp_path / "vault" / "note.md"
    vault.note_delete(p)
    assert vault.was_self_delete(p) is True
    assert vault.was_self_delete(p) is False   # suppression consumed


def test_was_self_delete_unknown_path_is_false(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "vault_path", str(tmp_path / "vault"))
    p = tmp_path / "vault" / "ghost.md"
    assert vault.was_self_delete(p) is False


# ── _unique_attachment_name ───────────────────────────────────────────────────


def test_unique_attachment_name_no_collision(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "vault_path", str(tmp_path / "vault"))
    # Empty vault → name comes back unchanged.
    result = vault._unique_attachment_name("chart.png")
    assert result == "chart.png"


def test_unique_attachment_name_collision_adds_suffix(tmp_path, monkeypatch):
    vp = tmp_path / "vault"
    monkeypatch.setattr(settings, "vault_path", str(vp))
    assets = vp / "Homelab" / "assets"
    assets.mkdir(parents=True)
    (assets / "chart.png").write_bytes(b"x")

    result = vault._unique_attachment_name("chart.png")
    assert result == "chart 1.png"


def test_unique_attachment_name_collision_multiple_suffixes(tmp_path, monkeypatch):
    vp = tmp_path / "vault"
    monkeypatch.setattr(settings, "vault_path", str(vp))
    assets = vp / "Homelab" / "assets"
    assets.mkdir(parents=True)
    (assets / "chart.png").write_bytes(b"x")
    (assets / "chart 1.png").write_bytes(b"x")

    result = vault._unique_attachment_name("chart.png")
    assert result == "chart 2.png"


def test_unique_attachment_name_no_extension(tmp_path, monkeypatch):
    vp = tmp_path / "vault"
    monkeypatch.setattr(settings, "vault_path", str(vp))
    assets = vp / "Homelab" / "assets"
    assets.mkdir(parents=True)
    (assets / "notes").write_bytes(b"x")

    result = vault._unique_attachment_name("notes")
    assert result == "notes 1"


def test_unique_attachment_name_case_insensitive(tmp_path, monkeypatch):
    vp = tmp_path / "vault"
    monkeypatch.setattr(settings, "vault_path", str(vp))
    assets = vp / "Homelab" / "assets"
    assets.mkdir(parents=True)
    (assets / "Chart.PNG").write_bytes(b"x")

    # "chart.png" (lowercase) should be treated as taken because the vault is
    # case-insensitive on names.
    result = vault._unique_attachment_name("chart.png")
    assert result == "chart 1.png"


# ── resolve_attachment: security boundary ────────────────────────────────────


def test_resolve_attachment_finds_file_in_assets(tmp_path, monkeypatch):
    vp = tmp_path / "vault"
    monkeypatch.setattr(settings, "vault_path", str(vp))
    assets = vp / "Homelab" / "assets"
    assets.mkdir(parents=True)
    f = assets / "photo.png"
    f.write_bytes(b"png")

    result = vault.resolve_attachment("photo.png")
    assert result is not None
    assert result.name == "photo.png"


def test_resolve_attachment_returns_none_for_missing_file(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "vault_path", str(tmp_path / "vault"))
    (tmp_path / "vault").mkdir()
    assert vault.resolve_attachment("ghost.png") is None


def test_resolve_attachment_blocks_relay_dir(tmp_path, monkeypatch):
    """Files inside .relay/ must never be served — it holds the sqlite db and git."""
    vp = tmp_path / "vault"
    monkeypatch.setattr(settings, "vault_path", str(vp))
    relay_dir = vp / ".relay"
    relay_dir.mkdir(parents=True)
    secret = relay_dir / "index.db"
    secret.write_bytes(b"secret")

    # Direct path form — must be blocked
    result = vault.resolve_attachment(".relay/index.db")
    assert result is None


def test_resolve_attachment_blocks_path_traversal(tmp_path, monkeypatch):
    """A path that traverses out of the vault must be rejected."""
    vp = tmp_path / "vault"
    monkeypatch.setattr(settings, "vault_path", str(vp))
    vp.mkdir(parents=True)
    outside = tmp_path / "secret.txt"
    outside.write_text("secret", encoding="utf-8")

    result = vault.resolve_attachment("../secret.txt")
    assert result is None


def test_resolve_attachment_bare_name_skips_relay_subdir(tmp_path, monkeypatch):
    """A bare filename must not resolve to a file inside .relay/ even if it
    exists there — the assets-dir scan must not descend into .relay."""
    vp = tmp_path / "vault"
    monkeypatch.setattr(settings, "vault_path", str(vp))
    relay_assets = vp / ".relay" / "assets"
    relay_assets.mkdir(parents=True)
    (relay_assets / "hidden.png").write_bytes(b"hidden")

    result = vault.resolve_attachment("hidden.png")
    assert result is None
