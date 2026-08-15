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


def _api_patch(base_url: str, post_id: int, payload: dict) -> dict:
    req = urllib.request.Request(
        f"{base_url}/posts/{post_id}",
        data=json.dumps(payload).encode(),
        headers={"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"},
        method="PATCH",
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())


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
    # The suite runs with RELAY_HISTORY_ENABLED=true (the post-history panel needs
    # real revisions) and CI installs git, so all three health dots should be green.
    # A `bad` dot here means the server genuinely lost a capability.
    assert body.locator(".sm-dot").count() == 3
    assert body.locator(".sm-dot.bad").count() == 0, "a health check regressed"
    assert body.locator(".sm-dot.ok").count() == 3
    page.locator("#smClose").click()
    page.locator("#statusModal.open").wait_for(state="detached", timeout=5_000)


def test_status_panel_closes_on_backdrop_and_escape(page):
    """Its close paths live in a different module from the key handler that calls
    them, so they are worth pinning explicitly rather than assuming."""
    page.locator("#statusBtn").click()
    page.locator("#statusModal.open").wait_for(timeout=10_000)
    # Near the corner: the backdrop spans the viewport but its centre sits behind
    # the panel, so a default (centre) click is intercepted by .sm-inner.
    page.locator("#smBackdrop").click(position={"x": 5, "y": 5})
    page.locator("#statusModal.open").wait_for(state="detached", timeout=5_000)

    page.locator("#statusBtn").click()
    page.locator("#statusModal.open").wait_for(timeout=10_000)
    page.keyboard.press("Escape")
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


def test_stylesheet_is_served_and_applied(page, relay_server):
    """The stylesheet now lives in /static/app.css instead of a <style> block.

    Added with that extraction, because the other smokes are geometry- and
    behaviour-based and all nine of them passed with the stylesheet entirely
    missing — an unstyled page still renders posts, filters tags and opens modals.
    This is the one that notices.
    """
    with urllib.request.urlopen(f"{relay_server}/static/app.css", timeout=10) as r:
        assert r.status == 200
        css = r.read().decode()
    assert ".feed" in css and "--accent" in css, "served file does not look like the app stylesheet"

    # Computed values that only exist if the sheet actually applied.
    applied = page.evaluate(
        """() => {
            const body = getComputedStyle(document.body);
            const card = document.querySelector('.feed .post');
            return {
                bg: body.backgroundColor,
                font: body.fontFamily,
                radius: card ? getComputedStyle(card).borderRadius : null,
            };
        }"""
    )
    assert applied["bg"] not in ("rgba(0, 0, 0, 0)", "rgb(255, 255, 255)"), (
        f"body has no themed background ({applied['bg']}) — stylesheet did not apply"
    )
    assert "Plex" in applied["font"] or "mono" in applied["font"].lower(), applied["font"]
    assert applied["radius"] and applied["radius"] != "0px", "cards lost their border radius"


# ── post history panel ───────────────────────────────────────────────────────


def test_history_panel_lists_revisions_and_previews_one(page, relay_server):
    post = _api_post(relay_server, {"title": "Revised Note", "content": "first draft", "tags": ["homelab"]})
    page.reload()
    page.get_by_text("Revised Note").first.wait_for(timeout=10_000)
    page.get_by_text("Revised Note").first.click()
    page.locator("#postModal.open").wait_for(timeout=10_000)
    page.locator("#pmHistory").click()

    page.locator("#historyModal.open").wait_for(timeout=10_000)
    rows = page.locator("#hmBody .hm-rev")
    rows.first.wait_for(timeout=10_000)
    assert rows.count() >= 1
    assert f"#{post['id']}" in page.locator("#hmTitle").inner_text()

    rows.first.click()
    page.locator("#hmBody .hm-body-text").wait_for(timeout=10_000)
    assert "first draft" in page.locator("#hmBody .hm-body-text").inner_text()
    assert page.locator(".hm-restore").is_visible()


def test_restoring_from_the_panel_undoes_a_clobber(page, relay_server):
    """The whole point, end to end and through the browser."""
    post = _api_post(relay_server, {"title": "Clobbered Note", "content": "THE GOOD VERSION", "tags": ["homelab"]})
    pid = post["id"]
    req = urllib.request.Request(
        f"{relay_server}/posts/{pid}",
        data=json.dumps({"content": "ruined by a bad rewrite"}).encode(),
        headers={"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"},
        method="PATCH",
    )
    urllib.request.urlopen(req, timeout=10)

    page.reload()
    page.get_by_text("Clobbered Note").first.wait_for(timeout=10_000)
    page.get_by_text("Clobbered Note").first.click()
    page.locator("#postModal.open").wait_for(timeout=10_000)
    page.locator("#pmHistory").click()
    page.locator("#historyModal.open").wait_for(timeout=10_000)

    # oldest revision = the create, before the clobber
    rows = page.locator("#hmBody .hm-rev")
    rows.first.wait_for(timeout=10_000)
    rows.last.click()
    page.locator("#hmBody .hm-body-text").wait_for(timeout=10_000)
    assert "THE GOOD VERSION" in page.locator("#hmBody .hm-body-text").inner_text()

    page.on("dialog", lambda d: d.accept())
    page.locator(".hm-restore").click()
    page.locator("#historyModal.open").wait_for(state="detached", timeout=10_000)

    # the feed reloads, and the good body is back
    page.wait_for_function(
        """(id) => {
            const card = document.querySelector(`[data-id="${id}"]`);
            return card && card.textContent.includes('THE GOOD VERSION');
        }""",
        arg=pid,
        timeout=15_000,
    )

    # The post modal stays open behind the history panel, and the restore streams
    # back over SSE — so it refreshes in place rather than showing the version we
    # just undid. Worth pinning: it depends on the SSE handler continuing to treat
    # a `post` event for the open modal as an in-place update.
    assert page.locator("#postModal.open").count() == 1
    assert "THE GOOD VERSION" in page.locator("#pmBody").inner_text()


def test_history_panel_does_not_resize_when_switching_revisions(page, relay_server):
    """The panel used to be sized by its contents, so selecting a revision
    collapsed it to the height of the loading line and re-inflated when the body
    arrived — a visible jump on every click. Panes now exist up front and scroll
    internally, so the shell must not move at all."""
    post = _api_post(relay_server, {"title": "Steady Panel", "content": "SHORT", "tags": ["homelab"]})
    _api_patch(relay_server, post["id"], {"content": "A MUCH LONGER BODY\n" * 60})
    _api_patch(relay_server, post["id"], {"content": "tiny again"})

    page.reload()
    page.get_by_text("Steady Panel").first.wait_for(timeout=10_000)
    page.get_by_text("Steady Panel").first.click()
    page.locator("#postModal.open").wait_for(timeout=10_000)
    page.locator("#pmHistory").click()
    page.locator("#historyModal.open").wait_for(timeout=10_000)
    page.locator("#hmBody .hm-rev").first.wait_for(timeout=10_000)
    page.wait_for_timeout(400)  # let the 0.18s modalIn entry animation settle

    def size():
        box = page.locator(".hm-inner").bounding_box()
        return (round(box["width"]), round(box["height"]))

    seen = {size()}
    rows = page.locator("#hmBody .hm-rev")
    assert rows.count() >= 3, "need several revisions of differing length"
    for i in range(rows.count()):
        rows.nth(i).click()
        page.wait_for_timeout(120)   # catch it mid-load, where the collapse used to happen
        seen.add(size())
        page.locator("#hmBody .hm-body-text").wait_for(timeout=10_000)
        seen.add(size())
    assert len(seen) == 1, f"panel changed size while switching revisions: {sorted(seen)}"


def test_history_panel_keeps_the_revision_list_visible_while_previewing(page):
    """Two panes, not a stacked list-then-preview: the list has to stay on screen
    so versions can actually be compared."""
    page.locator(".feed .post").first.wait_for(timeout=10_000)
    page.get_by_text("Smoke Post 0").first.click()
    page.locator("#postModal.open").wait_for(timeout=10_000)
    page.locator("#pmHistory").click()
    page.locator("#historyModal.open").wait_for(timeout=10_000)
    rows = page.locator("#hmBody .hm-rev")
    rows.first.wait_for(timeout=10_000)
    rows.first.click()
    page.locator("#hmBody .hm-body-text").wait_for(timeout=10_000)
    assert rows.first.is_visible(), "the revision list was pushed out of view by the preview"


def test_history_panel_closes_on_escape(page):
    page.locator(".feed .post").first.wait_for(timeout=10_000)
    page.get_by_text("Smoke Post 0").first.click()
    page.locator("#postModal.open").wait_for(timeout=10_000)
    page.locator("#pmHistory").click()
    page.locator("#historyModal.open").wait_for(timeout=10_000)
    page.keyboard.press("Escape")
    page.locator("#historyModal.open").wait_for(state="detached", timeout=5_000)
    # the post modal it opened over is still there
    assert page.locator("#postModal.open").count() == 1


# ── tag config form (sidebar) ────────────────────────────────────────────────


def _open_tag_config(page, tag: str):
    row = page.locator(".tag-item", has_text=tag).first
    row.hover()
    row.locator(".tag-config-btn").click()
    page.wait_for_timeout(150)


def test_tag_config_form_stays_inside_the_sidebar(page):
    """It used to spill out of the sidebar, taking its controls off-screen with it
    — the row could then only be closed by reloading the page.

    `datetime-local` has a wide min-content width, and a flex child's automatic
    minimum is min-content unless `min-width: 0` says otherwise. How wide that
    widget renders depends on browser, locale and zoom, so the sidebar is forced
    narrow here rather than trusting this browser to reproduce it: without the
    fix the form measures ~196px inside a 149px sidebar, with it ~123px. A test
    that only ran at the default width passed either way and proved nothing.
    """
    page.locator(".tag-item").first.wait_for(timeout=10_000)
    page.add_style_tag(
        content="#sidebarEl, .sidebar { width: 150px !important; min-width: 150px !important; }"
    )
    _open_tag_config(page, "homelab")
    page.locator(".tag-config-form").wait_for(timeout=5_000)

    fits = page.evaluate(
        """() => {
            const bar = document.getElementById('sidebarEl');
            const form = document.querySelector('.tag-config-form');
            return { available: bar.clientWidth, needed: form.scrollWidth };
        }"""
    )
    assert fits["needed"] <= fits["available"], (
        f"form needs {fits['needed']}px in a {fits['available']}px sidebar — it will spill out"
    )

    overflowing = page.evaluate(
        """() => {
            const bar = document.getElementById('sidebarEl').getBoundingClientRect();
            const bad = [];
            for (const el of document.querySelectorAll('.tag-config-form, .tag-config-form *')) {
                const r = el.getBoundingClientRect();
                if (r.width && (r.right > bar.right + 1 || r.left < bar.left - 1)) {
                    bad.push(`${el.className || el.tagName}: ${Math.round(r.left)}..${Math.round(r.right)}`);
                }
            }
            return bad;
        }"""
    )
    assert not overflowing, "tag config form spilled outside the sidebar:\n  " + "\n  ".join(overflowing)
    assert page.locator(".tc-save").is_visible()
    assert page.locator(".tc-cancel").is_visible()


def test_only_one_tag_config_form_can_be_open(page):
    """Opening a second used to stack another form over the tag list."""
    page.locator(".tag-item").first.wait_for(timeout=10_000)
    _open_tag_config(page, "homelab")
    assert page.locator(".tag-config-form").count() == 1
    _open_tag_config(page, "radio")
    assert page.locator(".tag-config-form").count() == 1, "a second form stayed open"


def test_tag_config_form_can_be_dismissed(page):
    page.locator(".tag-item").first.wait_for(timeout=10_000)

    _open_tag_config(page, "homelab")
    page.locator(".tc-cancel").click()
    page.wait_for_timeout(150)
    assert page.locator(".tag-config-form").count() == 0, "Cancel did not close it"

    _open_tag_config(page, "homelab")
    page.keyboard.press("Escape")
    page.wait_for_timeout(150)
    assert page.locator(".tag-config-form").count() == 0, "Escape did not close it"

    _open_tag_config(page, "homelab")
    page.locator("#feed").click(position={"x": 5, "y": 5})
    page.wait_for_timeout(200)
    assert page.locator(".tag-config-form").count() == 0, "clicking away did not close it"


def test_the_tag_row_is_restored_after_closing_its_config(page):
    """The form replaces the row's contents, so the gear is gone while it is open
    — that is why closing is done with Cancel/Escape/click-away rather than by
    clicking the gear again. The row must come back intact afterwards, or the tag
    becomes unusable until a reload."""
    page.locator(".tag-item").first.wait_for(timeout=10_000)
    row = page.locator(".tag-item", has_text="homelab").first

    _open_tag_config(page, "homelab")
    assert row.locator(".tag-config-btn").count() == 0, "the gear survived inside the form"

    page.locator(".tc-cancel").click()
    page.wait_for_timeout(150)
    assert row.locator(".tag-name").inner_text() == "homelab"
    assert row.locator(".tag-config-btn").count() == 1, "the gear did not come back"
    assert row.locator(".tag-rename").count() == 1, "the rename control did not come back"

    # and it still works a second time
    _open_tag_config(page, "homelab")
    assert page.locator(".tag-config-form").count() == 1
