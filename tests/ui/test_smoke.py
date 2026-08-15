"""Browser smokes for the single-page UI.

`relay/static/index.html` is the biggest single file in the repo and the only one
with no automated coverage — 18 of its commits are `fix(ui)`. These are the safety
net that has to exist *before* it gets broken into modules: they assert the flows
a restructuring would plausibly break, not implementation details.

Deliberately shallow and few. They should stay fast enough that nobody is tempted
to skip them.
"""
from __future__ import annotations

import json
import urllib.request

import pytest

from .conftest import API_KEY

pytestmark = pytest.mark.ui


def _api_post(base_url: str, payload: dict) -> dict:
    req = urllib.request.Request(
        f"{base_url}/posts",
        data=json.dumps(payload).encode(),
        headers={"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())


def test_feed_renders_seeded_posts(page):
    page.locator(".feed .post").first.wait_for(timeout=10_000)
    assert page.locator(".feed .post").count() >= 4
    assert page.get_by_text("Smoke Post 0").first.is_visible()


def test_tag_filter_narrows_the_feed(page):
    page.locator(".feed .post").first.wait_for(timeout=10_000)
    page.locator(".tag-item", has_text="radio").first.click()
    page.wait_for_function(
        "() => document.querySelectorAll('.feed .post').length === 1", timeout=10_000
    )
    assert page.get_by_text("Radio Log").first.is_visible()


def test_search_filters_the_feed(page):
    page.locator(".feed .post").first.wait_for(timeout=10_000)
    page.locator("#searchInput").fill("Radio")
    page.wait_for_function(
        "() => [...document.querySelectorAll('.feed .post')]"
        ".every(p => p.textContent.includes('Radio'))",
        timeout=10_000,
    )
    assert page.locator(".feed .post").count() >= 1


def test_post_modal_opens_with_the_body(page):
    page.locator(".feed .post").first.wait_for(timeout=10_000)
    page.get_by_text("Smoke Post 1").first.click()
    page.locator("#postModal.open").wait_for(timeout=10_000)
    assert "Smoke Post 1" in page.locator("#pmTitle").inner_text()
    assert "body number 1" in page.locator("#pmBody").inner_text()
    page.locator("#pmClose").click()
    page.locator("#postModal.open").wait_for(state="detached", timeout=5_000)


def test_grid_tiles_do_not_paint_outside_their_card(page):
    """Pins the constraint CLAUDE.md calls the tight one.

    A grid tile has a `1fr` inner track whose automatic minimum is its items'
    min-content width, so any child that cannot shrink widens the track past the
    card border and everything inside then paints outside the frame. Every card
    grid area carries `min-width: 0` to prevent that — this is what notices if a
    future element forgets.
    """
    page.locator(".feed .post").first.wait_for(timeout=10_000)
    page.locator("#vtGrid").click()
    page.wait_for_function("() => document.querySelector('.feed').classList.contains('grid')")
    page.wait_for_timeout(200)  # let the grid settle before measuring

    overflowing = page.evaluate(
        """() => {
            const bad = [];
            for (const card of document.querySelectorAll('.feed.grid .post')) {
                const cb = card.getBoundingClientRect();
                for (const el of card.querySelectorAll('*')) {
                    const r = el.getBoundingClientRect();
                    if (r.width === 0 && r.height === 0) continue;
                    if (r.right > cb.right + 1 || r.left < cb.left - 1) {
                        bad.push(`${el.className || el.tagName}: ${Math.round(r.left)}..${Math.round(r.right)}`
                                 + ` vs card ${Math.round(cb.left)}..${Math.round(cb.right)}`);
                    }
                }
            }
            return bad;
        }"""
    )
    assert not overflowing, "content painted outside its grid tile:\n  " + "\n  ".join(overflowing)


def test_view_toggle_persists_across_a_reload(page):
    page.locator(".feed .post").first.wait_for(timeout=10_000)
    page.locator("#vtGrid").click()
    page.wait_for_function("() => document.querySelector('.feed').classList.contains('grid')")
    page.reload()
    page.locator(".feed .post").first.wait_for(timeout=10_000)
    assert page.locator(".feed.grid").count() == 1, "grid view was not restored from localStorage"


def test_sse_prepends_a_post_created_elsewhere(page, relay_server):
    """The live feed is the piece most likely to break silently in a refactor."""
    page.locator(".feed .post").first.wait_for(timeout=10_000)
    page.wait_for_function("() => document.getElementById('liveLabel').textContent === 'live'",
                           timeout=15_000)
    _api_post(relay_server, {"title": "Pushed Live", "content": "arrived over SSE", "tags": ["homelab"]})
    page.get_by_text("Pushed Live").first.wait_for(timeout=15_000)


def test_status_panel_reports_version_and_health(page):
    page.locator("#statusBtn").click()
    page.locator("#statusModal.open").wait_for(timeout=10_000)
    body = page.locator("#smBody")
    body.locator(".sm-feat").first.wait_for(timeout=10_000)

    assert page.locator("#smVersion").inner_text().strip(), "no version rendered"
    text = body.inner_text()
    for label in ("Vault history", "Full-text search", "External edits", "Posts", "Uptime"):
        assert label in text, f"status panel missing {label!r}"
    # history is off in this deployment, so its dot must read as a fault
    assert body.locator(".sm-dot.bad").count() == 1
    page.locator("#smClose").click()
    page.locator("#statusModal.open").wait_for(state="detached", timeout=5_000)


def test_sidebar_tabs_switch_views(page):
    page.locator(".feed .post").first.wait_for(timeout=10_000)
    page.locator("#tabTree").click()
    page.wait_for_function("() => document.getElementById('tabTree').classList.contains('active')")
    page.locator("#tabFiles").click()
    page.wait_for_function("() => document.getElementById('tabFiles').classList.contains('active')")
    page.locator("#tabTags").click()
    page.wait_for_function("() => document.getElementById('tabTags').classList.contains('active')")
    assert page.locator(".feed .post").count() >= 4
