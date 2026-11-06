"""The three-column view: sidebar · a narrow note list · the open note, in place.

The note column is the post modal's own panel docked into `#readerPane`
(`js/reader.js`), so these check that it reads, navigates and edits there
without a modal — and that a phone still gets the list and the modal.
"""
from __future__ import annotations

import json
import urllib.request
import uuid

from .conftest import API_KEY


def _api(base_url: str, method: str, path: str, payload: dict | None = None):
    req = urllib.request.Request(
        f"{base_url}{path}", method=method,
        data=json.dumps(payload).encode() if payload is not None else None,
        headers={"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        body = r.read()
        return json.loads(body) if body else None


def _reader(page):
    page.locator("#vtReader").click()
    page.locator("#readerPane:not(.empty) #pmTitle").wait_for(timeout=5_000)


def _title(page) -> str:
    return page.locator("#pmTitle").text_content()


def _modal_open(page) -> bool:
    return page.evaluate("document.getElementById('postModal').classList.contains('open')")


def test_the_first_note_opens_in_place_not_in_a_modal(page):
    _reader(page)
    first = page.locator(".feed .post:not(.master-doc) .post-title").first.text_content()
    assert _title(page) == first
    assert not _modal_open(page)
    assert page.locator(".feed .post.selected").count() == 1
    # The list column is narrower than the note column beside it.
    feed_w = page.locator("main").bounding_box()["width"]
    pane_w = page.locator("#readerPane").bounding_box()["width"]
    assert feed_w <= 380 < pane_w


def test_a_click_shows_a_note_and_j_steps_to_the_next(page):
    _reader(page)
    cards = page.locator(".feed .post:not(.master-doc)")
    second, third = cards.nth(1), cards.nth(2)
    second.click()
    assert _title(page) == second.locator(".post-title").text_content()
    page.keyboard.press("j")
    page.wait_for_function("(t) => document.getElementById('pmTitle').textContent === t",
                           arg=third.locator(".post-title").text_content())
    assert not _modal_open(page)


def test_a_tag_shows_its_own_first_note(page):
    _reader(page)
    page.locator(".tag-item", has_text="radio").click()
    page.wait_for_function("() => document.getElementById('pmTitle').textContent === 'Radio Log'")


def test_a_wikilink_navigates_the_pane_and_back_returns(page, relay_server):
    tag = f"rd{uuid.uuid4().hex[:6]}"
    _api(relay_server, "POST", "/posts", {"title": f"Reader Target {tag}", "content": "the target", "tags": [tag]})
    _api(relay_server, "POST", "/posts", {"title": f"Reader Source {tag}",
                                          "content": f"see [[Reader Target {tag}]]", "tags": [tag]})
    page.reload()
    page.locator(".feed .post").first.wait_for()
    _reader(page)
    page.locator(".feed .post", has_text=f"Reader Source {tag}").click()
    page.locator("#pmBody a.wikilink[data-post-id]").click()
    page.wait_for_function("(t) => document.getElementById('pmTitle').textContent === t", arg=f"Reader Target {tag}")
    assert page.locator("#pmBack").text_content() == f"← Reader Source {tag}"
    page.go_back()
    page.wait_for_function("(t) => document.getElementById('pmTitle').textContent === t", arg=f"Reader Source {tag}")
    assert not _modal_open(page)


def test_editing_happens_in_the_pane(page, relay_server):
    tag = f"rd{uuid.uuid4().hex[:6]}"
    _api(relay_server, "POST", "/posts", {"title": f"Reader Edit {tag}", "content": "before", "tags": [tag]})
    page.reload()
    page.locator(".feed .post").first.wait_for()
    _reader(page)
    page.locator(".feed .post", has_text=f"Reader Edit {tag}").click()
    page.keyboard.press("e")
    page.locator("#readerPane.editing .ef-content").wait_for()
    assert not page.evaluate("document.getElementById('editModal').classList.contains('open')")
    page.locator("#readerEdit .ef-content").fill("after the edit")
    page.locator("#readerEdit .btn-save").click()
    page.locator("#readerPane:not(.editing) #pmBody", has_text="after the edit").wait_for()


def test_a_mid_width_moves_the_sidebar_into_the_drawer(page):
    _reader(page)
    page.set_viewport_size({"width": 1000, "height": 800})
    page.wait_for_timeout(350)   # the drawer's transform transition
    box = page.locator("aside").bounding_box()
    assert box["x"] + box["width"] <= 0, f"sidebar still on screen: {box}"
    assert page.locator("#menuBtn").is_visible()
    assert page.locator("#readerPane").is_visible()
    page.locator("#menuBtn").click()
    page.wait_for_timeout(350)
    assert page.locator("aside").bounding_box()["x"] >= 0


def test_a_phone_falls_back_to_the_list_and_the_modal(mobile_page):
    page = mobile_page
    page.locator("#vtReader").click()
    assert not page.locator("#readerPane").is_visible()
    page.locator(".feed .post:not(.master-doc)").first.click()
    page.wait_for_function("() => document.getElementById('postModal').classList.contains('open')")
