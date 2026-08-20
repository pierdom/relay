"""Unit tests for relay/frontmatter.py — the module that reads and writes
every note on disk. A bug here silently corrupts data, so it needs direct
coverage rather than relying on the integration tests to catch it indirectly.
"""
from __future__ import annotations

from relay.frontmatter import (
    parse,
    sanitize_attachment_name,
    sanitize_title,
    serialize,
    unique_path,
)

# ── parse ─────────────────────────────────────────────────────────────────────


def test_parse_returns_empty_dict_and_full_text_when_no_front_matter():
    text = "# Hello\n\nJust a body."
    meta, body = parse(text)
    assert meta == {}
    assert body == text


def test_parse_extracts_id_tags_source_and_body():
    text = "---\nid: 7\ntags:\n  - homelab\n  - dev\nsource: https://example.com\n---\n\nBody here.\n"
    meta, body = parse(text)
    assert meta["id"] == 7
    assert meta["tags"] == ["homelab", "dev"]
    assert meta["source"] == "https://example.com"
    assert body.strip() == "Body here."


def test_parse_tags_as_comma_string():
    text = "---\nid: 1\ntags: homelab,dev\n---\n\nbody"
    meta, _ = parse(text)
    assert meta["tags"] == ["homelab", "dev"]


def test_parse_coerces_yaml_datetime_to_iso_string():
    # YAML auto-parses bare ISO timestamps into datetime objects; parse() must
    # normalise them back to strings for downstream code that expects strings.
    text = "---\nid: 1\ncreated_at: 2024-01-15T12:00:00Z\n---\n\nbody"
    meta, _ = parse(text)
    assert isinstance(meta["created_at"], str)
    assert "2024-01-15" in meta["created_at"]


def test_parse_coerces_yaml_date_to_iso_string():
    text = "---\nid: 1\ncreated_at: 2024-01-15\n---\n\nbody"
    meta, _ = parse(text)
    assert isinstance(meta["created_at"], str)
    assert meta["created_at"].startswith("2024-01-15")


def test_parse_invalid_id_is_dropped():
    text = "---\nid: not-a-number\n---\n\nbody"
    meta, _ = parse(text)
    assert "id" not in meta


def test_parse_non_dict_front_matter_falls_back_gracefully():
    text = "---\n- just a list\n---\n\nbody"
    meta, body = parse(text)
    assert meta == {}
    assert body == text


def test_parse_missing_optional_fields_are_none():
    text = "---\nid: 3\n---\n\nbody"
    meta, _ = parse(text)
    assert meta.get("source") is None
    assert meta.get("updated_at") is None
    assert meta.get("expires_at") is None


def test_parse_empty_tags_field_becomes_empty_list():
    text = "---\nid: 1\ntags: []\n---\n\nbody"
    meta, _ = parse(text)
    assert meta["tags"] == []


# ── serialize ─────────────────────────────────────────────────────────────────


def test_serialize_round_trips_cleanly():
    meta = {
        "id": 5,
        "tags": ["homelab"],
        "source": None,
        "created_at": "2024-01-01T00:00:00Z",
        "updated_at": None,
        "expires_at": None,
    }
    body = "Some content."
    text = serialize(meta, body)
    meta2, body2 = parse(text)
    assert meta2["id"] == 5
    assert meta2["tags"] == ["homelab"]
    assert body2.strip() == body


def test_serialize_id_is_first_field():
    meta = {"id": 1, "tags": [], "source": None, "created_at": "2024-01-01T00:00:00Z",
            "updated_at": None, "expires_at": None}
    text = serialize(meta, "body")
    lines = text.splitlines()
    assert lines[1].startswith("id:")


def test_serialize_none_fields_are_omitted():
    meta = {"id": 1, "tags": [], "source": None, "created_at": "2024-01-01T00:00:00Z",
            "updated_at": None, "expires_at": None}
    text = serialize(meta, "body")
    assert "source:" not in text
    assert "updated_at:" not in text
    assert "expires_at:" not in text


def test_serialize_adds_trailing_newline_to_body():
    meta = {"id": 1, "tags": [], "source": None, "created_at": "2024-01-01T00:00:00Z",
            "updated_at": None, "expires_at": None}
    text = serialize(meta, "no trailing newline")
    assert text.endswith("\n")


def test_serialize_empty_tags_emits_empty_list():
    meta = {"id": 1, "tags": [], "source": None, "created_at": "2024-01-01T00:00:00Z",
            "updated_at": None, "expires_at": None}
    text = serialize(meta, "body")
    assert "tags: []" in text


# ── sanitize_title ────────────────────────────────────────────────────────────


def test_sanitize_title_strips_illegal_chars():
    assert "/" not in sanitize_title("a/b")
    assert ":" not in sanitize_title("title: sub")
    assert "#" not in sanitize_title("tag #1")


def test_sanitize_title_collapses_whitespace():
    result = sanitize_title("lots  of   spaces")
    assert "  " not in result
    assert result == "lots of spaces"


def test_sanitize_title_strips_trailing_dot_and_space():
    result = sanitize_title("trailing. ")
    assert not result.endswith(".")
    assert not result.endswith(" ")


def test_sanitize_title_fallback_for_empty_or_all_illegal():
    assert sanitize_title("") == "Untitled"
    assert sanitize_title("///") == "Untitled"


def test_sanitize_title_truncates_at_max():
    long_title = "a" * 300
    result = sanitize_title(long_title)
    assert len(result) <= 180


def test_sanitize_title_preserves_unicode():
    result = sanitize_title("日本語タイトル")
    assert result == "日本語タイトル"


# ── sanitize_attachment_name ──────────────────────────────────────────────────


def test_sanitize_attachment_name_strips_directory_component():
    result = sanitize_attachment_name("../../etc/passwd")
    assert "/" not in result
    assert result == "passwd"


def test_sanitize_attachment_name_replaces_illegal_chars():
    result = sanitize_attachment_name('my:file"name.png')
    assert ":" not in result
    assert '"' not in result
    assert result.endswith(".png")


def test_sanitize_attachment_name_fallback_for_empty():
    assert sanitize_attachment_name("") == "attachment"
    assert sanitize_attachment_name("///") == "attachment"


def test_sanitize_attachment_name_truncates_preserving_extension():
    name = "a" * 200 + ".png"
    result = sanitize_attachment_name(name)
    assert result.endswith(".png")
    assert len(result) <= 180


# ── unique_path ───────────────────────────────────────────────────────────────


def test_unique_path_returns_free_name_as_is(tmp_path):
    result = unique_path(tmp_path, "My Note")
    assert result == tmp_path / "My Note.md"
    assert not result.exists()


def test_unique_path_suffixes_on_collision(tmp_path):
    (tmp_path / "My Note.md").touch()
    result = unique_path(tmp_path, "My Note")
    assert result == tmp_path / "My Note 2.md"


def test_unique_path_increments_past_existing_suffixes(tmp_path):
    (tmp_path / "My Note.md").touch()
    (tmp_path / "My Note 2.md").touch()
    result = unique_path(tmp_path, "My Note")
    assert result == tmp_path / "My Note 3.md"


def test_unique_path_exclude_treats_path_as_free(tmp_path):
    existing = tmp_path / "My Note.md"
    existing.touch()
    result = unique_path(tmp_path, "My Note", exclude=existing)
    assert result == existing  # renaming onto itself is a no-op
