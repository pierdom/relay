from __future__ import annotations

from relay.chunking import chunk_post


def test_no_headings_is_one_chunk():
    chunks = chunk_post("Title", "just a short paragraph, no headings here")
    assert len(chunks) == 1
    assert chunks[0].heading_path == ""
    assert chunks[0].embed_text == "Title\n\njust a short paragraph, no headings here"


def test_empty_content_yields_no_chunks():
    assert chunk_post("Title", "") == []
    assert chunk_post("Title", "   \n  ") == []


def test_splits_on_h2():
    content = "## First\n" + "first-word " * 60 + "\n\n## Second\n" + "second-word " * 60
    chunks = chunk_post("Title", content)
    assert [c.heading_path for c in chunks] == ["First", "Second"]
    assert "first-word" not in chunks[1].body


def test_h3_nests_under_preceding_h2():
    content = (
        "## Parent\n" + "parent-word " * 60 +
        "\n\n### Child\n" + "child-word " * 60
    )
    chunks = chunk_post("Title", content)
    assert chunks[0].heading_path == "Parent"
    assert chunks[1].heading_path == "Parent > Child"


def test_h3_without_preceding_h2_stands_alone():
    content = "### Orphan\n" + "orphan-word " * 60
    chunks = chunk_post("Title", content)
    assert chunks[0].heading_path == "Orphan"


def test_intro_before_first_heading_gets_empty_heading_path():
    content = "intro-word " * 60 + "\n\n## Section\n" + "section-word " * 60
    chunks = chunk_post("Title", content)
    assert chunks[0].heading_path == ""
    assert chunks[1].heading_path == "Section"


def test_code_fences_stripped_before_chunking():
    content = "## Section\nsome prose here.\n\n```python\ndef leaked_secret(): pass\n```\n\nmore prose."
    chunks = chunk_post("Title", content)
    joined = " ".join(c.body for c in chunks)
    assert "leaked_secret" not in joined
    assert "some prose" in joined
    assert "more prose" in joined


def test_runt_merges_forward():
    content = "## Tiny\nshort.\n\n## Real section\n" + "real content word " * 60
    chunks = chunk_post("Title", content)
    # "Tiny" (well under 50 words) must not survive as its own chunk
    assert "Tiny" not in [c.heading_path for c in chunks]
    assert any("short." in c.body for c in chunks)


def test_trailing_runt_merges_backward():
    content = "## Real section\n" + "real content word " * 60 + "\n\n## Tiny\nshort."
    chunks = chunk_post("Title", content)
    assert len(chunks) == 1
    assert "short." in chunks[0].body


def test_giant_section_splits_with_overlap():
    body_words = [f"word{i}" for i in range(900)]
    content = "## Big\n" + " ".join(body_words)
    chunks = chunk_post("Title", content)
    assert len(chunks) > 1
    assert all(c.heading_path == "Big" for c in chunks)
    # overlap: some word present in both consecutive pieces
    first_words = set(chunks[0].body.split())
    second_words = set(chunks[1].body.split())
    assert first_words & second_words


def test_embed_text_carries_title_body_does_not():
    content = "## Section\n" + "content word " * 20
    chunks = chunk_post("My Post", content)
    assert chunks[0].embed_text.startswith("My Post > Section\n\n")
    assert "My Post" not in chunks[0].body
