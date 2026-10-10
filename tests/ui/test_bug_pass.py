"""Second bug-fixing pass over the web UI (relay #198, B-12).

Each test drives the failure the way it was found: in Chromium, against the
real server, with races forced by delaying one request inside the page.
"""
from __future__ import annotations

import json
import time
import urllib.request

import pytest

from .conftest import API_KEY
from .test_smoke import _api_post

pytestmark = pytest.mark.ui


def _unique(prefix: str) -> str:
    return f"{prefix} {time.time_ns()}"


def _tag() -> str:
    return f"t{time.time_ns() % 10**10}"


def _api(base_url: str, method: str, path: str, payload: dict | None = None):
    req = urllib.request.Request(
        f"{base_url}{path}", method=method,
        data=json.dumps(payload).encode() if payload is not None else None,
        headers={"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        body = r.read()
        return json.loads(body) if body else None


def _open_post(page, relay_server, post_id: int):
    page.goto(f"{relay_server}/?post={post_id}")
    page.locator("#postModal.open").wait_for(timeout=10_000)


def _filter_tag(page, tag: str):
    page.locator(".tag-item", has=page.locator("button.tag-name", has_text=tag)).first.click()
    page.locator(".search-scope").wait_for(state="visible", timeout=5_000)


def test_forward_onto_the_master_document_reopens_it(page, relay_server):
    """popstate tested `e.state?.postId` — falsy for #0, so Forward onto the
    master document closed the modal instead of showing it."""
    post = _api_post(relay_server, {"title": _unique("Points at zero"), "content": "see #0", "tags": ["homelab"]})
    _open_post(page, relay_server, post["id"])
    page.locator("#pmBody a.wikilink", has_text="#0").click()
    page.wait_for_function("() => document.getElementById('pmTitle').textContent === 'Master Document'")
    page.go_back()
    page.wait_for_function(f"() => document.getElementById('pmTitle').textContent === {json.dumps(post['title'])}")
    page.go_forward()
    page.wait_for_function("() => document.getElementById('pmTitle').textContent === 'Master Document'")
    assert page.locator("#postModal.open").count() == 1


def test_a_link_inside_a_card_opens_only_its_target(page, relay_server):
    """The card's own click handler fired too, so a wikilink opened the card's
    post with the target stacked on top."""
    target = _api_post(relay_server, {"title": _unique("Card target"), "content": "x", "tags": ["homelab"]})
    linker = _api_post(relay_server, {
        "title": _unique("Card linker"), "tags": ["homelab"],
        "content": f"see [[{target['title']}]] and [out](https://example.com/page)",
    })
    page.reload()
    card = page.locator(f'.post[data-id="{linker["id"]}"]')
    card.wait_for(timeout=10_000)
    assert card.locator('a[href="https://example.com/page"]').get_attribute("target") == "_blank"
    card.locator("a.wikilink").click()
    page.wait_for_function(f"() => document.getElementById('pmTitle').textContent === {json.dumps(target['title'])}")
    assert not page.locator("#pmBack").is_visible(), "the card's own post was opened underneath"


def test_saving_the_master_document_keeps_it_pinned(page):
    """Edit's save replaced the card with a fresh render that lost `pinned`."""
    master = page.locator('.post.pinned[data-id="0"]')
    master.wait_for(timeout=10_000)
    master.hover()
    master.locator(".btn-edit").click()
    page.locator("#editModal.open .ef-content").wait_for(timeout=10_000)
    page.locator("#emBody .btn-save").click()   # unchanged: still saves and re-renders
    page.locator("#editModal.open").wait_for(state="detached", timeout=10_000)
    assert "pinned" in (page.locator('.post[data-id="0"]').get_attribute("class") or "")


def test_the_master_document_offers_no_delete(page, relay_server):
    assert page.locator('.post[data-id="0"] .btn-delete').count() == 0
    _open_post(page, relay_server, 0)
    assert not page.locator("#pmDelete").is_visible()


def test_a_folder_after_a_tag_still_hears_new_posts(page, relay_server):
    """Only selectTag reconnected the live stream, so after a tag a folder
    view stayed subscribed to that tag and never heard anything else."""
    tag = _tag()
    _api_post(relay_server, {"title": _unique("Tag seed"), "content": "x", "tags": [tag]})
    page.reload()
    _filter_tag(page, tag)
    page.locator("#tabTree").click()
    page.locator(".folder-item", has_text="Homelab").click()
    page.wait_for_timeout(500)
    _api_post(relay_server, {"title": _unique("Folder live"), "content": "x", "tags": ["homelab"]})
    page.locator("#newPostsPill").wait_for(state="visible", timeout=10_000)


def test_renaming_the_active_tag_keeps_the_feed_live(page, relay_server):
    """The stream stayed subscribed to the old name after a rename."""
    old, new = _tag(), _tag()
    _api_post(relay_server, {"title": _unique("Rename seed"), "content": "x", "tags": [old]})
    page.reload()
    _filter_tag(page, old)
    row = page.locator(".tag-item", has=page.locator("button.tag-name", has_text=old)).first
    row.hover()
    row.locator(".tag-rename").click()
    page.locator(".tag-rename-input").fill(new)
    page.locator(".tag-rename-input").press("Enter")
    page.locator(".search-scope", has_text=new).wait_for(timeout=10_000)
    page.wait_for_timeout(500)
    title = _unique("After rename live")
    _api_post(relay_server, {"title": title, "content": "x", "tags": [new]})
    page.locator(".post", has_text=title).wait_for(timeout=10_000)


def test_load_more_after_a_live_post_shows_no_duplicate(page, relay_server):
    """A live post shifts the server's list by one, so the next offset page
    repeated the last card shown."""
    tag = _tag()
    for i in range(22):
        _api_post(relay_server, {"title": f"Paging {tag} {i:02d}", "content": "x", "tags": [tag]})
    page.reload()
    _filter_tag(page, tag)
    page.locator("#feed .post").nth(19).wait_for(timeout=10_000)
    title = _unique("Live paging")
    _api_post(relay_server, {"title": title, "content": "x", "tags": [tag]})
    page.locator(".post", has_text=title).wait_for(timeout=10_000)
    page.locator("#loadMoreBtn").click()
    page.wait_for_function("() => document.querySelectorAll('#feed .post').length >= 23", timeout=10_000)
    ids = page.eval_on_selector_all("#feed .post", "els => els.map(e => e.dataset.id)")
    assert len(ids) == len(set(ids)) == 23


def test_a_tag_called_all_is_not_the_all_row(page, relay_server):
    _api_post(relay_server, {"title": _unique("Literally all"), "content": "x", "tags": ["all"]})
    page.reload()
    page.locator(".tag-item", has=page.locator("button.tag-name", has_text="all")).nth(1).click()
    page.wait_for_timeout(500)
    assert page.locator(".tag-item.active").count() == 1


def test_copy_works_without_the_clipboard_api(page, relay_server):
    """Plain HTTP has no navigator.clipboard; the button threw and copied nothing."""
    post = _api_post(relay_server, {"title": _unique("Code"), "content": "```\nuptime\n```", "tags": ["homelab"]})
    page.add_init_script("Object.defineProperty(navigator, 'clipboard', { value: undefined, configurable: true })")
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    _open_post(page, relay_server, post["id"])
    page.locator(".code-copy").first.click()
    page.locator(".code-copy.copied").wait_for(timeout=3_000)
    assert errors == []


def test_an_expired_session_returns_to_the_login_card(page, relay_server):
    """Every request failed in place ("Could not load posts. Invalid API key")
    with no way back to the login."""
    page.reload()                     # drop the in-memory key; the cookie carries the session
    page.locator("#newPostBtn").wait_for(state="visible", timeout=10_000)
    page.context.clear_cookies()      # the session expires
    page.locator("#searchInput").fill("anything")
    page.locator("#apiKeyInput").wait_for(state="visible", timeout=10_000)
    assert "session has ended" in page.locator("#loginError").inner_text()
    assert not page.locator("#sidebarEl").is_visible()


def test_a_slow_revision_does_not_overwrite_the_one_picked_after_it(page, relay_server):
    post = _api_post(relay_server, {"title": _unique("Revisions"), "content": "v0", "tags": ["homelab"]})
    for i in range(1, 3):
        _api(relay_server, "PATCH", f"/posts/{post['id']}", {"content": f"v{i}"})
    revs = _api(relay_server, "GET", f"/posts/{post['id']}/history")["items"]
    _open_post(page, relay_server, post["id"])
    page.evaluate("""slow => {
        const f = window.fetch;
        window.fetch = async (u, o) => {
            if (String(u).includes(slow)) await new Promise(r => setTimeout(r, 1200));
            return f(u, o);
        };
    }""", revs[1]["sha"])
    page.locator("#pmHistory").click()
    rows = page.locator("#hmBody .hm-rev")
    rows.nth(2).wait_for(timeout=10_000)
    rows.nth(1).click()
    rows.nth(2).click()
    page.wait_for_timeout(1800)   # past the slow answer
    assert revs[2]["short_sha"] in page.locator(".hm-pane .hm-pane-head").inner_text().lower()
