"""Regressions from the 2026-10-09 web UI audit (relay #198, B-12).

Each was found by driving the real UI, not by reading the code, so each is
pinned the same way: the user-visible failure, reproduced in Chromium.
"""
from __future__ import annotations

import json
import time
import urllib.request

import pytest

from .conftest import API_KEY
from .test_smoke import _api_patch, _api_post

pytestmark = pytest.mark.ui


def _api_get(base_url: str, post_id: int) -> dict:
    req = urllib.request.Request(f"{base_url}/posts/{post_id}", headers={"Authorization": f"Bearer {API_KEY}"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())


def _unique(prefix: str) -> str:
    return f"{prefix} {time.time_ns()}"


def _open_post(page, relay_server, post_id: int):
    page.goto(f"{relay_server}/?post={post_id}")
    page.locator("#postModal.open").wait_for(timeout=10_000)


def test_saving_over_an_edit_made_elsewhere_is_refused_then_deliberate(page, relay_server):
    """The form used to PATCH without `if_match`, so its stale copy of the body
    silently replaced an edit made while it was open — even when only Tags
    was touched. Now the first Save is refused with a message, and a second
    Save is an informed overwrite."""
    post = _api_post(relay_server, {"title": _unique("Conflict"), "content": "original body", "tags": ["homelab"]})
    _open_post(page, relay_server, post["id"])
    page.locator("#pmEdit").click()
    page.locator("#editModal.open .ef-content").wait_for(timeout=10_000)

    _api_patch(relay_server, post["id"], {"content": "edited in Obsidian"})
    page.locator("#emBody .ef-tags").fill("homelab, retagged")
    page.locator("#emBody .btn-save").click()

    msg = page.locator("#emBody .ef-gate-msg")
    page.wait_for_function(
        "() => document.querySelector('#emBody .ef-gate-msg').textContent.includes('changed')",
        timeout=10_000,
    )
    assert "Save again" in msg.inner_text()
    assert _api_get(relay_server, post["id"])["content"] == "edited in Obsidian"
    assert page.locator("#editModal.open").count() == 1

    page.locator("#emBody .btn-save").click()
    page.locator("#editModal.open").wait_for(state="detached", timeout=10_000)
    after = _api_get(relay_server, post["id"])
    assert after["content"] == "original body"
    assert "retagged" in after["tags"]


def test_back_closes_the_post_instead_of_leaving_the_app(page, relay_server):
    """The first post opened pushed no history entry, so Back — the Android
    gesture included — navigated away from relay with the modal still up."""
    post = _api_post(relay_server, {"title": _unique("Back target"), "content": "x", "tags": ["homelab"]})
    page.goto("about:blank")
    page.goto(relay_server)
    page.locator(f'.post[data-id="{post["id"]}"]').click()
    page.locator("#postModal.open").wait_for(timeout=10_000)

    page.go_back()
    page.locator("#postModal.open").wait_for(state="detached", timeout=10_000)
    assert page.url.startswith(relay_server)
    assert page.locator(f'.post[data-id="{post["id"]}"]').is_visible()


def test_back_unwinds_a_wikilink_chain_one_post_at_a_time(page, relay_server):
    target = _api_post(relay_server, {"title": _unique("Chain target"), "content": "end", "tags": ["homelab"]})
    source = _api_post(relay_server, {
        "title": _unique("Chain source"), "content": f"see [[{target['title']}]]", "tags": ["homelab"],
    })
    page.goto(relay_server)
    page.locator(f'.post[data-id="{source["id"]}"]').click()
    page.locator("#postModal.open").wait_for(timeout=10_000)
    page.locator("#pmBody a.wikilink").first.click()
    page.wait_for_function(f"() => document.getElementById('pmTitle').textContent === {json.dumps(target['title'])}")

    page.go_back()
    page.wait_for_function(f"() => document.getElementById('pmTitle').textContent === {json.dumps(source['title'])}")
    page.go_back()
    page.locator("#postModal.open").wait_for(state="detached", timeout=10_000)
    assert page.url.startswith(relay_server)


def test_clicking_a_wikilink_does_not_strand_its_hover_preview(page, relay_server):
    """A click inside the hover delay let the timer fire against an anchor the
    navigation had just removed: the preview painted at the top-left corner
    and stayed over every later screen until a reload."""
    target = _api_post(relay_server, {"title": _unique("Preview target"), "content": "body", "tags": ["homelab"]})
    source = _api_post(relay_server, {
        "title": _unique("Preview source"), "content": f"see [[{target['title']}|the alias]]", "tags": ["homelab"],
    })
    _open_post(page, relay_server, source["id"])
    link = page.locator("#pmBody a.wikilink").first
    link.hover()
    link.click()
    page.wait_for_timeout(800)   # well past the 350ms hover delay
    assert page.locator(".link-preview").count() == 0

    page.keyboard.press("Escape")
    page.keyboard.press("Escape")
    page.locator("#postModal.open").wait_for(state="detached", timeout=10_000)
    assert page.locator(".link-preview").count() == 0


def test_hover_preview_shows_a_wikilink_alias_not_its_raw_syntax(page, relay_server):
    leaf = _api_post(relay_server, {"title": _unique("Alias leaf"), "content": "leaf", "tags": ["homelab"]})
    middle = _api_post(relay_server, {
        "title": _unique("Alias middle"), "content": f"points at [[{leaf['title']}|the leaf]]", "tags": ["homelab"],
    })
    top = _api_post(relay_server, {
        "title": _unique("Alias top"), "content": f"[[{middle['title']}]]", "tags": ["homelab"],
    })
    _open_post(page, relay_server, top["id"])
    page.locator("#pmBody a.wikilink").first.hover()
    page.locator(".link-preview .lp-body").filter(has_text="the leaf").wait_for(timeout=10_000)
    assert "|" not in page.locator(".link-preview .lp-body").inner_text()


def test_deleting_from_the_tree_tab_keeps_the_folder_list(page, relay_server):
    """Delete (and restore) refreshed the *tag* list unconditionally, so on the
    Tree tab the sidebar swapped to tags while the Tree tab stayed lit. When the
    SSE echo won the race its debounced refresh repainted the folders a moment
    later, so the flip is caught by watching every repaint, not the end state."""
    title = _unique("Tree delete")
    post = _api_post(relay_server, {"title": title, "content": "x", "tags": ["homelab"]})
    page.on("dialog", lambda d: d.accept())
    page.locator("#tabTree").click()
    page.locator("#tagList .folder-item").first.wait_for(timeout=10_000)
    page.locator("#searchInput").fill(title)
    card = page.locator(f'.post[data-id="{post["id"]}"]')
    card.wait_for(timeout=10_000)
    card.click()
    page.locator("#postModal.open").wait_for(timeout=10_000)
    page.evaluate("""() => {
        window.__tagRowsSeen = false;
        new MutationObserver(() => {
            if (document.querySelector('#tagList .tag-item:not(.folder-item)')) window.__tagRowsSeen = true;
        }).observe(document.getElementById('tagList'), { childList: true, subtree: true });
    }""")
    page.locator("#pmDelete").click()
    page.locator("#postModal.open").wait_for(state="detached", timeout=10_000)
    page.wait_for_timeout(800)   # past the 250ms SSE-driven refresh
    assert page.evaluate("window.__tagRowsSeen") is False
    assert page.locator("#tagList .folder-item").count() > 0


def test_closing_new_post_asks_before_discarding_a_draft(page):
    """Toggling New Post shut used to wipe a draft unasked. It is a modal now,
    and every way out of it — Escape, ×, Cancel — asks first."""
    page.locator("#newPostBtn").click()
    page.locator("#composePanel.open").wait_for(timeout=5_000)
    page.locator("#cpContent").fill("an unsaved thought")
    page.once("dialog", lambda d: d.dismiss())
    page.keyboard.press("Escape")
    assert page.locator("#composePanel.open").count() == 1
    assert page.locator("#cpContent").input_value() == "an unsaved thought"

    page.once("dialog", lambda d: d.dismiss())
    page.locator("#cmClose").click()
    assert page.locator("#composePanel.open").count() == 1

    page.once("dialog", lambda d: d.accept())
    page.locator("#cpCancel").click()
    assert page.locator("#composePanel.open").count() == 0


def test_closing_an_untouched_new_post_does_not_ask(page):
    dialogs: list[str] = []
    page.on("dialog", lambda d: (dialogs.append(d.message), d.dismiss()))
    page.locator('.tag-item', has_text="homelab").first.click()   # prefills Tags
    page.locator("#newPostBtn").click()
    page.locator("#composePanel.open").wait_for(timeout=5_000)
    page.keyboard.press("Escape")
    assert page.locator("#composePanel.open").count() == 0
    assert dialogs == []


def test_publishing_without_a_body_says_why(page):
    page.locator("#newPostBtn").click()
    page.locator("#cpTitle").fill(_unique("No body"))
    page.locator("#cpPublish").click()
    assert "Write something" in page.locator("#composePanel .ef-gate-msg").inner_text()
    assert page.evaluate("document.activeElement.id") == "cpContent"


def test_signed_out_page_shows_only_the_login(relay_server, browser):
    """The sidebar's tabs stayed clickable behind the login card, and the header
    reported "offline" about a server that was up."""
    context = browser.new_context(viewport={"width": 1280, "height": 900})
    try:
        page = context.new_page()
        page.goto(relay_server)
        page.locator("#apiKeyInput").wait_for(state="visible", timeout=10_000)
        assert not page.locator("#sidebarEl").is_visible()
        assert not page.locator(".live-indicator").is_visible()

        page.locator("#apiKeyInput").fill(API_KEY)
        page.locator("#connectBtn").click()
        page.locator("#newPostBtn").wait_for(state="visible", timeout=10_000)
        assert page.locator("#sidebarEl").is_visible()
        assert page.locator(".live-indicator").is_visible()
    finally:
        context.close()


def test_app_boots_when_storage_is_blocked(relay_server, browser, seed):
    """view-prefs.js read localStorage at import time without a guard; with site
    data blocked that threw and the whole app module never ran."""
    context = browser.new_context(viewport={"width": 1280, "height": 900})
    context.add_init_script(
        "Object.defineProperty(window, 'localStorage', "
        "{ get() { throw new DOMException('blocked', 'SecurityError'); } });"
    )
    try:
        page = context.new_page()
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(relay_server)
        page.locator("#apiKeyInput").wait_for(state="visible", timeout=10_000)
        page.locator("#apiKeyInput").fill(API_KEY)
        page.locator("#connectBtn").click()
        page.locator("#feed .post").first.wait_for(timeout=10_000)
        page.locator("#vtGrid").click()   # a write, too
        assert page.locator("#feed.grid").count() == 1
        assert errors == []
    finally:
        context.close()



def test_saving_an_edit_refreshes_the_open_post_and_its_links(page, relay_server):
    """Edit opened from the post modal: after Save the modal shows the new
    body, and a rename re-resolves [[links]] to the post without a reload
    (the link index used to be fetched only at startup)."""
    target = _api_post(relay_server, {"title": _unique("Before rename"), "content": "old body", "tags": ["homelab"]})
    renamed = _unique("After rename")
    linker = _api_post(relay_server, {"title": _unique("Linker"), "content": f"see [[{renamed}]]", "tags": ["homelab"]})
    _open_post(page, relay_server, target["id"])
    page.locator("#pmEdit").click()
    page.locator("#editModal.open .ef-content").wait_for(timeout=10_000)
    page.locator("#emBody .ef-title").fill(renamed)
    page.locator("#emBody .ef-content").fill("new body")
    page.locator("#emBody .btn-save").click()
    page.locator("#editModal.open").wait_for(state="detached", timeout=10_000)
    page.wait_for_function("() => document.getElementById('pmBody').textContent.includes('new body')", timeout=5_000)

    page.keyboard.press("Escape")
    page.locator("#postModal.open").wait_for(state="detached", timeout=5_000)
    # No reload: the modal renders against whatever index the page holds now.
    page.locator(f'.post[data-id="{linker["id"]}"] .post-title').click()
    page.locator("#postModal.open").wait_for(timeout=5_000)
    page.locator("#pmBody a.wikilink").first.wait_for(timeout=10_000)
