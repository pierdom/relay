"""The web UI review (relay #198, B-13): provenance, activity, related posts,
the quick switcher, writing aids, the outline, the phone header, the one-line
master document and content-sized grid tiles.

Each test drives the feature in Chromium against the real server.
"""
from __future__ import annotations

import json
import urllib.request

import pytest

from .conftest import AGENT_KEY, API_KEY
from .test_bug_pass import _api, _filter_tag, _open_post, _tag, _unique

pytestmark = pytest.mark.ui


def _as_agent(base_url: str, payload: dict) -> dict:
    req = urllib.request.Request(
        f"{base_url}/posts", method="POST", data=json.dumps(payload).encode(),
        headers={"Authorization": f"Bearer {AGENT_KEY}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())


def _new(base_url: str, prefix: str, content: str = "x", tag: str = "homelab") -> dict:
    return _api(base_url, "POST", "/posts", {"title": _unique(prefix), "content": content, "tags": [tag]})


def _blur(page):
    page.evaluate("document.activeElement && document.activeElement.blur()")


# ── 1. Who wrote it ──────────────────────────────────────────────────────────


def test_cards_and_the_post_view_name_the_writer(page, relay_server):
    tag = _tag()
    post = _as_agent(relay_server, {"title": _unique("Agent note"), "content": "x", "tags": [tag]})
    page.reload()
    _filter_tag(page, tag)
    card = page.locator(f'.post[data-id="{post["id"]}"]')
    assert card.locator(".post-author").inner_text() == "by agent"
    _open_post(page, relay_server, post["id"])
    assert page.locator("#pmMeta .post-author").inner_text() == "by agent"


def test_the_author_chip_filters_the_feed_by_writer(page, relay_server):
    tag = _tag()
    mine = _new(relay_server, "Human note", "x", tag)
    theirs = _as_agent(relay_server, {"title": _unique("Agent note"), "content": "x", "tags": [tag]})
    page.reload()
    _filter_tag(page, tag)
    page.locator(f'.post[data-id="{mine["id"]}"]').wait_for()
    with page.expect_response(lambda r: "author=agent" in r.url):
        page.locator(f'.post[data-id="{theirs["id"]}"] .post-author').click()
    assert page.locator("#searchScope").inner_text() == "by agent ×"
    authors = page.locator("#feed .post .post-author").all_inner_texts()
    assert authors and set(authors) == {"by agent"}, authors
    assert page.locator(f'.post[data-id="{mine["id"]}"]').count() == 0
    # The chip clears it, like any other filter.
    page.locator("#searchScope").click()
    page.locator("#searchScope").wait_for(state="hidden")


def test_history_names_who_made_each_revision(page, relay_server):
    post = _as_agent(relay_server, {"title": _unique("Shared note"), "content": "v1", "tags": ["homelab"]})
    _api(relay_server, "PATCH", f"/posts/{post['id']}", {"content": "v2"})
    _open_post(page, relay_server, post["id"])
    page.locator("#pmHistory").click()
    page.locator("#hmBody .hm-rev").nth(1).wait_for()
    assert page.locator("#hmBody .hm-author").all_inner_texts() == ["by apikey", "by agent"]


def test_a_filter_does_not_outlive_the_session(page, relay_server):
    theirs = _as_agent(relay_server, {"title": _unique("Agent post"), "content": "x", "tags": ["homelab"]})
    page.reload()
    page.locator(f'.post[data-id="{theirs["id"]}"] .post-author').click()
    page.locator("#searchScope").wait_for(state="visible")
    page.evaluate("document.getElementById('disconnectBtn').click()")
    page.locator("#apiKeyInput").fill(API_KEY)
    with page.expect_response(lambda r: "/posts?" in r.url and "limit=20" in r.url) as resp:
        page.locator("#connectBtn").click()
    assert "author=" not in resp.value.url
    assert page.locator("#searchScope").is_hidden()


# ── 2. Recent activity ───────────────────────────────────────────────────────


def test_status_lists_recent_changes_and_opens_them(page, relay_server):
    title = _unique("Activity note")
    _as_agent(relay_server, {"title": title, "content": "x", "tags": ["homelab"]})
    page.reload()   # the link index decides which rows are links
    page.locator("#newPostBtn").wait_for()
    page.locator("#statusBtn").click()
    row = page.locator(".sm-act", has_text=title).first
    row.wait_for(timeout=10_000)
    assert "created by agent" in row.locator(".sm-act-meta").inner_text()
    row.locator("button.sm-act-title").click()
    page.locator("#postModal.open").wait_for()
    assert page.locator("#pmTitle").inner_text() == title
    assert page.locator("#pmBack").inner_text() == "← Status"
    page.locator("#pmBack").click()
    page.locator("#statusModal.open").wait_for()


def test_a_deleted_post_in_activity_is_named_but_not_a_link(page, relay_server):
    title = _unique("Gone note")
    post = _api(relay_server, "POST", "/posts", {"title": title, "content": "x", "tags": ["homelab"]})
    _api(relay_server, "DELETE", f"/posts/{post['id']}")
    page.reload()
    page.locator("#newPostBtn").wait_for()
    page.locator("#statusBtn").click()
    deleted = page.locator(".sm-act", has_text="deleted by apikey").filter(has_text=title)
    deleted.wait_for(timeout=10_000)
    assert deleted.locator("button").count() == 0


# ── 4. Related, not linked ───────────────────────────────────────────────────


def test_related_posts_show_under_the_post_when_semantic_search_is_on(page, relay_server):
    """The suite runs without embeddings, so /status and /related are answered
    in the page — what is under test is that the UI asks and renders."""
    other = _new(relay_server, "Kindred note")
    post = _new(relay_server, "Seed note")

    def status(route):
        body = route.fetch().json()
        body["features"]["search"]["embeddings"] = True
        route.fulfill(json=body)

    page.route("**/status", status)
    page.route(f"**/posts/{post['id']}/related", lambda r: r.fulfill(
        json={"items": [{"id": other["id"], "title": other["title"], "score": 0.9}]}))
    _open_post(page, relay_server, post["id"])
    related = page.locator("#pmRelated")
    related.locator("a.wikilink").wait_for(timeout=10_000)
    assert related.locator("h4").text_content() == "Related, not linked (1)"
    related.locator("a.wikilink").click()
    page.wait_for_function("t => document.getElementById('pmTitle').textContent === t", arg=other["title"])


def test_related_posts_are_not_asked_for_without_semantic_search(page, relay_server):
    post = _new(relay_server, "Plain note")
    asked = []
    page.on("request", lambda r: asked.append(r.url) if "/related" in r.url else None)
    _open_post(page, relay_server, post["id"])
    page.wait_for_timeout(300)
    assert not asked


# ── 5. Quick switcher and shortcuts ──────────────────────────────────────────


def test_ctrl_k_jumps_to_a_post_by_title(page, relay_server):
    title = _unique("Switcher target")
    _api(relay_server, "POST", "/posts", {"title": title, "content": "x", "tags": ["homelab"]})
    page.reload()
    page.locator("#newPostBtn").wait_for()
    page.keyboard.press("Control+k")
    page.locator("#switcherModal.open").wait_for()
    assert page.evaluate("document.activeElement.id") == "swInput"
    page.keyboard.type(title.split()[-1])   # the unique number alone
    page.locator(".sw-item", has_text=title).wait_for()
    page.keyboard.press("Enter")
    page.locator("#postModal.open").wait_for()
    assert page.locator("#pmTitle").inner_text() == title
    assert not page.locator("#switcherModal.open").count()


def test_the_switcher_finds_a_post_by_id_and_says_when_nothing_matches(page, relay_server):
    post = _new(relay_server, "Numbered")
    page.reload()
    page.locator("#newPostBtn").wait_for()
    page.keyboard.press("Control+k")
    page.locator("#swInput").fill(f"#{post['id']}")
    first = page.locator(".sw-item").first
    assert first.get_attribute("aria-selected") == "true"
    assert first.locator(".ac-hint").inner_text() == f"#{post['id']}"
    page.locator("#swInput").fill("zz-no-such-title-zz")
    page.locator(".sw-empty").wait_for()


def test_ctrl_k_does_not_open_over_an_editor(page, relay_server):
    post = _new(relay_server, "Busy")
    _open_post(page, relay_server, post["id"])
    page.locator("#pmEdit").click()
    page.locator("#editModal.open .ef-content").click()
    page.keyboard.press("Control+k")
    page.wait_for_timeout(200)
    assert not page.locator("#switcherModal.open").count()


def test_slash_focuses_search_and_n_starts_a_post(page):
    page.locator("#feed .post").first.wait_for()
    _blur(page)
    page.keyboard.press("/")
    assert page.evaluate("document.activeElement.id") == "searchInput"
    assert page.locator("#searchInput").input_value() == ""   # the key itself is not typed
    _blur(page)
    page.keyboard.press("n")
    page.locator("#composePanel.open").wait_for()
    assert page.locator("#cpTitle").input_value() == ""


# ── 6. Writing aids ──────────────────────────────────────────────────────────


def _compose(page):
    page.locator("#newPostBtn").click()
    page.locator("#composePanel.open").wait_for()


def test_double_bracket_completes_a_title(page, relay_server):
    title = _unique("Linkable")
    _api(relay_server, "POST", "/posts", {"title": title, "content": "x", "tags": ["homelab"]})
    page.reload()
    page.locator("#newPostBtn").wait_for()
    _compose(page)
    page.locator("#cpContent").click()
    page.keyboard.type("See [[" + title.split()[-1])
    item = page.locator("#composePanel .ac-menu:not([hidden]) .ac-item", has_text=title)
    item.wait_for()
    page.keyboard.press("Enter")
    assert page.locator("#cpContent").input_value() == f"See [[{title}]]"
    # The accepted link reaches the saved draft, not only the next keystroke.
    assert f"[[{title}]]" in page.evaluate("localStorage.getItem('relay-draft')")


def test_hash_completes_to_an_id_and_escape_closes_only_the_list(page, relay_server):
    title = _unique("Idable")
    post = _api(relay_server, "POST", "/posts", {"title": title, "content": "x", "tags": ["homelab"]})
    page.reload()
    page.locator("#newPostBtn").wait_for()
    _compose(page)
    page.locator("#cpContent").click()
    page.keyboard.type("Ref #" + title.split()[-1])
    menu = page.locator("#composePanel .ac-menu").first
    menu.locator(".ac-item", has_text=title).wait_for()
    asked = []
    page.on("dialog", lambda d: (asked.append(d.message), d.dismiss()))
    page.keyboard.press("Escape")
    assert menu.is_hidden()
    assert not asked, asked   # the list closed; the dialog was never asked to
    assert page.locator("#composePanel.open").count() == 1
    page.keyboard.type(" ")
    page.keyboard.press("Backspace")   # typing again reopens it
    menu.locator(".ac-item", has_text=title).wait_for()
    page.keyboard.press("Tab")
    assert page.locator("#cpContent").input_value() == f"Ref #{post['id']}"
    assert menu.is_hidden()   # the inserted #id is not a new query
    # Nothing is preselected for `#`: a "#word" may be prose, so Enter stays a new line.
    page.keyboard.type(" #" + title.split()[-1])
    menu.locator(".ac-item", has_text=title).wait_for()
    page.keyboard.press("Enter")
    assert page.locator("#cpContent").input_value() == f"Ref #{post['id']} #{title.split()[-1]}\n"
    assert menu.is_hidden()


def test_tags_complete_from_the_vault(page, relay_server):
    tag = _tag()
    _new(relay_server, "Tagged", "x", tag)
    page.reload()
    page.locator("#newPostBtn").wait_for()
    _compose(page)
    page.locator("#cpTags").fill("")
    page.locator("#cpTags").click()
    page.keyboard.type(f"homelab, {tag[:-2]}")
    page.locator("#composePanel .ac-item", has_text=tag).wait_for()
    page.keyboard.press("Enter")
    assert page.locator("#cpTags").input_value() == f"homelab, {tag}"


def test_preview_renders_the_body_and_write_returns(page):
    _compose(page)
    page.locator("#cpContent").fill("## Heading\n\n- one\n- two\n\nSee #0.")
    page.locator("#cpPreview").click()
    preview = page.locator("#composePanel .ef-preview")
    assert preview.locator("h2").inner_text() == "Heading"
    assert preview.locator("li").count() == 2
    # A link in the preview would open its post *behind* the editor sheet.
    preview.locator("a.wikilink").click()
    page.wait_for_timeout(300)
    assert not page.locator("#postModal.open").count()
    assert page.locator("#cpContent").is_hidden()
    page.locator("#cpPreview").click()
    assert page.locator("#cpContent").is_visible()
    assert preview.is_hidden()


def test_the_edit_form_has_the_same_aids(page, relay_server):
    post = _new(relay_server, "Editable", "**bold**")
    _open_post(page, relay_server, post["id"])
    page.locator("#pmEdit").click()
    page.locator("#emBody .ef-preview-btn").click()
    assert page.locator("#emBody .ef-preview strong").inner_text() == "bold"
    assert page.locator("#emBody .ef-content-highlight").is_hidden()


def test_an_unpublished_draft_survives_a_reload(page):
    title = _unique("Draft")
    _compose(page)
    page.locator("#cpTitle").fill(title)
    page.locator("#cpContent").fill("half a thought")
    page.reload()
    page.locator("#newPostBtn").wait_for()
    _compose(page)
    assert page.locator("#cpTitle").input_value() == title
    assert page.locator("#cpContent").input_value() == "half a thought"
    # Discarding it, deliberately, is what drops it.
    page.once("dialog", lambda d: d.accept())
    page.locator("#cpCancel").click()
    page.locator("#composePanel.open").wait_for(state="detached")
    page.reload()
    page.locator("#newPostBtn").wait_for()
    _compose(page)
    assert page.locator("#cpTitle").input_value() == ""


def test_publishing_and_disconnecting_drop_the_draft(page):
    _compose(page)
    page.locator("#cpTitle").fill(_unique("Published draft"))
    page.locator("#cpContent").fill("done")
    page.locator("#cpPublish").click()
    page.locator("#composePanel.open").wait_for(state="detached")
    assert page.evaluate("localStorage.getItem('relay-draft')") is None

    _compose(page)
    page.locator("#cpContent").fill("private words")
    assert page.evaluate("localStorage.getItem('relay-draft')") is not None
    page.evaluate("document.getElementById('disconnectBtn').click()")
    page.locator("#apiKeyInput").wait_for(state="visible")
    assert page.evaluate("localStorage.getItem('relay-draft')") is None


# ── 7. Outline ───────────────────────────────────────────────────────────────

_LONG = "\n\n".join(f"## Part {i}\n\n" + ("A line of prose. " * 60) for i in range(1, 5))


def test_a_long_post_gets_an_outline_on_a_wide_screen(page, relay_server):
    post = _new(relay_server, "Long read", _LONG)
    page.set_viewport_size({"width": 1440, "height": 900})
    _open_post(page, relay_server, post["id"])
    outline = page.locator("#pmOutline")
    assert outline.is_visible()
    assert outline.locator(".pm-outline-item").all_inner_texts() == [f"Part {i}" for i in range(1, 5)]
    # It sits in the gutter, clear of the reading column.
    gap = page.evaluate("""() => document.querySelector('#pmBody .post-body > h2').getBoundingClientRect().left
        - document.getElementById('pmOutline').getBoundingClientRect().right""")
    assert gap >= 16, gap
    outline.locator(".pm-outline-item", has_text="Part 4").click()
    page.wait_for_function("() => document.getElementById('pmBody').scrollTop > 200")


def test_no_outline_on_a_narrow_screen_or_a_short_post(page, relay_server):
    long = _new(relay_server, "Long", _LONG)
    short = _new(relay_server, "Short", "## Only\n\nx")
    page.set_viewport_size({"width": 1100, "height": 900})
    _open_post(page, relay_server, long["id"])
    assert page.locator("#pmOutline").is_hidden()
    page.set_viewport_size({"width": 1440, "height": 900})
    _open_post(page, relay_server, short["id"])
    assert page.locator("#pmOutline").is_hidden()


def test_a_heading_that_restates_the_title_is_dropped_even_with_punctuation(page, relay_server):
    """Titles are filenames, so "Runbook: restore" is stored as "Runbook restore";
    its "# Runbook: restore" heading still restates it."""
    n = _unique("Colon")
    title = f"{n}: notes"
    post = _api(relay_server, "POST", "/posts", {"title": title, "content": f"# {title}\n\nbody", "tags": ["homelab"]})
    page.reload()
    card = page.locator(f'.post[data-id="{post["id"]}"]')
    card.wait_for()
    assert post["title"] != title   # the colon really was dropped
    assert card.locator(".post-body h1").count() == 0
    _open_post(page, relay_server, post["id"])
    assert page.locator("#pmBody .post-body h1").count() == 0


# ── 8. Phone header ──────────────────────────────────────────────────────────


def test_the_phone_header_leads_with_the_drawer_and_a_square_plus(mobile_page):
    boxes = mobile_page.evaluate("""() => ['menuBtn', 'newPostBtn', 'themeBtn'].map(id => {
        const r = document.getElementById(id).getBoundingClientRect();
        return { id, left: r.left, w: r.width, h: r.height };
    })""")
    menu, plus, theme = boxes
    header_items = mobile_page.evaluate("""() => [...document.querySelectorAll('header > *')]
        .filter(e => e.getBoundingClientRect().width).map(e => e.getBoundingClientRect().left)""")
    assert menu["left"] == min(header_items), boxes
    assert menu["w"] >= 40 and menu["h"] >= 40, menu
    assert (plus["w"], plus["h"]) == (theme["w"], theme["h"]), boxes
    label = mobile_page.locator("#newPostBtn .np-label").bounding_box()
    assert label["width"] <= 1 and label["height"] <= 1, label   # visually "+" alone
    assert mobile_page.get_by_role("button", name="+ New Post").count() == 1   # still named in full


# ── 9. Master document ───────────────────────────────────────────────────────


def test_the_master_document_is_one_line_until_opened(page):
    page.locator(".tag-item").first.click()
    master = page.locator(".post.master-doc")
    master.wait_for()
    assert master.bounding_box()["height"] < 50, master.bounding_box()
    assert master.locator(".post-body-wrap").is_hidden()
    master.locator(".master-badge").click()
    assert master.locator(".post-body-wrap").is_visible()
    assert master.locator(".btn-edit").count() == 1
    assert master.bounding_box()["height"] > 80


# ── 10. Grid tiles ───────────────────────────────────────────────────────────


def test_grid_tiles_size_to_content_but_line_up_in_a_row(page, relay_server):
    # Newest first: four one-line tiles fill the first row of four, the tall one
    # starts the next.
    tag = _tag()
    long = _new(relay_server, "Tall tile", _LONG, tag)
    for i in range(4):
        _new(relay_server, f"Tile {i}", "one line", tag)
    page.set_viewport_size({"width": 1400, "height": 900})
    page.reload()
    page.locator("#newPostBtn").wait_for()
    _filter_tag(page, tag)
    page.locator("#vtGrid").click()
    page.locator(f'.post[data-id="{long["id"]}"]').wait_for()
    page.wait_for_timeout(400)
    tiles = page.evaluate("""() => [...document.querySelectorAll('#feed.grid > .post:not(.pinned)')].map(t => {
        const r = t.getBoundingClientRect();
        return { top: Math.round(r.top), h: Math.round(r.height), tall: t.textContent.includes('Tall tile') };
    })""")
    tall = next(t for t in tiles if t["tall"])
    assert tall["h"] <= 340, tiles
    rows = {}
    for t in tiles:
        rows.setdefault(t["top"], set()).add(t["h"])
    assert all(len(h) == 1 for h in rows.values()), f"a row's tiles differ in height: {tiles}"
    short_rows = [h.pop() for top, h in rows.items() if top != tall["top"]]
    assert short_rows and all(h < 300 for h in short_rows), f"short tiles still fill 340px: {tiles}"
    page.locator("#vtList").click()
