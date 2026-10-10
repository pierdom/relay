"""Keyboard and assistive-technology access (relay #198, B-12).

The audit found the UI usable only with a pointer in three places: Tab never
reached a tag row or a feed card (both were clickable <div>s), modals neither
took nor returned focus and let Tab walk out behind them, and none of them
said it was a dialog. These drive the real keyboard path.
"""
from __future__ import annotations

import time

import pytest

from .test_smoke import _api_post

pytestmark = pytest.mark.ui

_ACTIVE_IN = "(id) => document.getElementById(id).contains(document.activeElement)"


def _unique(prefix: str) -> str:
    return f"{prefix} {time.time_ns()}"


def test_a_card_opens_from_the_keyboard_and_focus_comes_back(page, relay_server):
    post = _api_post(relay_server, {"title": _unique("Keyboard card"), "content": "body", "tags": ["homelab"]})
    page.reload()
    link = page.locator(f'.post[data-id="{post["id"]}"] a.post-title')
    link.wait_for(timeout=10_000)
    link.focus()
    page.keyboard.press("Enter")
    page.locator("#postModal.open").wait_for(timeout=10_000)

    panel = page.locator("#postModal .pm-inner")
    assert panel.get_attribute("role") == "dialog"
    assert panel.get_attribute("aria-modal") == "true"
    assert panel.get_attribute("aria-labelledby") == "pmTitle"
    page.wait_for_function(_ACTIVE_IN, arg="postModal", timeout=5_000)
    # Exactly one modal for one Enter: the feed's own Enter shortcut must not
    # open the j/k-selected card on top of what the link just opened.
    page.wait_for_timeout(500)   # let a second, async open land if there is one
    assert not page.locator("#pmBack").is_visible()   # a stacked post would show "← <title>"

    page.keyboard.press("Escape")
    page.locator("#postModal.open").wait_for(state="detached", timeout=10_000)
    page.wait_for_function(
        "(id) => document.activeElement.closest('.post')?.dataset.id === id", arg=str(post["id"]), timeout=5_000,
    )


def test_tab_stays_inside_an_open_modal(page):
    page.locator("#feed .post:not(.pinned)").first.click()
    page.locator("#postModal.open").wait_for(timeout=10_000)
    for _ in range(20):
        page.keyboard.press("Tab")
        assert page.evaluate(_ACTIVE_IN, "postModal")
    for _ in range(5):
        page.keyboard.press("Shift+Tab")
        assert page.evaluate(_ACTIVE_IN, "postModal")


def test_every_modal_is_announced_as_a_dialog(page):
    labelled = page.evaluate("""() => [...document.querySelectorAll('.pm-inner, .sm-inner')].map(p => ({
        role: p.getAttribute('role'), modal: p.getAttribute('aria-modal'),
        // The post modal's label is its title, filled when a post opens (checked
        // in the test above) — here it only has to exist.
        label: !!document.getElementById(p.getAttribute('aria-labelledby') || ''),
    }))""")
    assert len(labelled) >= 6
    for d in labelled:
        assert d["role"] == "dialog" and d["modal"] == "true" and d["label"], d


def test_a_tag_filters_the_feed_from_the_keyboard(page):
    row = page.locator(".tag-item", has_text="radio").first
    row.locator("button.tag-name").focus()
    page.keyboard.press("Enter")
    page.locator(".tag-item.active", has_text="radio").wait_for(timeout=10_000)
    assert row.locator(".tag-name").get_attribute("aria-current") == "true"
    page.wait_for_function(
        "() => [...document.querySelectorAll('#feed .post:not(.pinned)')]"
        ".every(p => [...p.querySelectorAll('.tag-pill')].some(t => t.dataset.tag === 'radio'))",
        timeout=10_000,
    )


def test_the_edit_shortcut_does_not_type_into_the_title(page, relay_server):
    """`e` moved focus into the Title field before its character was inserted,
    so the editor opened with an "e" prefixed to the title — saving renamed the
    post, and Escape asked to discard a change nobody made."""
    title = _unique("Shortcut")
    post = _api_post(relay_server, {"title": title, "content": "x", "tags": ["homelab"]})
    page.goto(f"{relay_server}/?post={post['id']}")
    page.locator("#postModal.open").wait_for(timeout=10_000)
    page.keyboard.press("e")
    page.locator("#editModal.open .ef-title").wait_for(timeout=10_000)
    assert page.locator("#emBody .ef-title").input_value() == title

    page.keyboard.press("Escape")   # clean form: closes without a discard prompt
    page.locator("#editModal.open").wait_for(state="detached", timeout=5_000)
    page.wait_for_function(_ACTIVE_IN, arg="postModal", timeout=5_000)


def test_the_master_document_toggles_from_the_keyboard(page):
    page.locator(".tag-item").first.click()   # "all" — the pinned master doc leads it
    badge = page.locator(".post.master-doc button.master-badge")
    badge.wait_for(timeout=10_000)
    assert badge.get_attribute("aria-expanded") == "false"
    badge.focus()
    page.keyboard.press("Enter")
    page.wait_for_function(
        "() => !document.querySelector('.post.master-doc').classList.contains('collapsed')", timeout=5_000,
    )
    assert badge.get_attribute("aria-expanded") == "true"


def test_reduced_motion_stills_the_feed(page):
    page.emulate_media(reduced_motion="reduce")
    page.reload()
    page.locator("#feed .post").nth(3).wait_for(timeout=10_000)
    timing = page.evaluate("""() => {
        const s = getComputedStyle(document.querySelectorAll('#feed .post')[3]);
        return { duration: s.animationDuration, delay: s.animationDelay };
    }""")
    assert timing["duration"] in ("1e-05s", "0.00001s"), timing
    assert timing["delay"] == "0s", timing
