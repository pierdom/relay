"""Regressions from the v1.16 web-UI audit (screenshots of every screen, desktop
and phone, against a seeded vault).

Each test pins one thing the audit found broken:

- the sidebar's "all" row summed tag (or folder) counts — a multi-tag post
  counted once per tag, an untagged one not at all, the root master document
  never — instead of counting posts;
- a slow feed response could land after a newer one and repaint stale results;
- the Files tab left the post feed's search/sort/view controls live;
- a search with no hits said "No posts yet";
- an attachment image in the post modal sat at the panel's left edge instead of
  in the reading column the text uses.
"""
from __future__ import annotations

import json
import time
import urllib.request

import pytest

from .conftest import API_KEY

pytestmark = pytest.mark.ui


def _api(base_url: str, path: str, payload: dict | None = None) -> dict:
    req = urllib.request.Request(
        f"{base_url}{path}",
        data=json.dumps(payload).encode() if payload is not None else None,
        headers={"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"},
        method="POST" if payload is not None else "GET",
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())


def _post_count(base_url: str) -> int:
    d = _api(base_url, "/posts?limit=1&summary=true")
    return d["total"] + (1 if d["pinned"] else 0)


def _all_count(page) -> int:
    row = page.locator("#tagList .tag-item").first
    assert row.locator(".tag-name").inner_text().strip().lower() == "all"
    return int(row.locator(".tag-count").inner_text())


def test_the_all_row_counts_posts_in_both_tags_and_tree(page, relay_server):
    # A multi-tag post and an untagged one: exactly the two cases a tag-count sum gets wrong.
    _api(relay_server, "/posts", {"title": f"Multi {time.time()}", "content": "x", "tags": ["homelab", "radio", "dev"]})
    _api(relay_server, "/posts", {"title": f"Untagged {time.time()}", "content": "x"})
    page.reload()
    page.locator("#newPostBtn").wait_for(state="visible")
    page.wait_for_timeout(600)
    expected = _post_count(relay_server)
    assert _all_count(page) == expected
    page.locator("#tabTree").click()
    page.wait_for_timeout(600)
    assert _all_count(page) == expected


def test_a_stale_feed_response_never_repaints_a_newer_one(page):
    """Delay the first search's response past the second request: the feed must
    end on the second query's results, not the late first one's."""
    # Delay inside the page, not in a Playwright route handler: the sync API
    # dispatches handlers one at a time, so sleeping there serialises the two
    # requests and the race never happens.
    page.evaluate("""() => {
        const real = window.fetch; let delayed = false;
        window.fetch = async (url, opts) => {
            const res = await real(url, opts);
            if (String(url).includes('search=Smoke') && !delayed) {
                delayed = true;
                await new Promise(r => setTimeout(r, 1200));
            }
            return res;
        };
    }""")
    search = page.locator("#searchInput")
    search.fill("Smoke")
    page.wait_for_timeout(400)          # debounce fires; the slow request is in flight
    search.fill("Radio")
    page.wait_for_timeout(2500)         # the slow response has now landed too
    titles = page.locator("#feed .post .post-title").all_inner_texts()
    assert titles and all("Smoke" not in t for t in titles), titles


def test_the_files_tab_hides_the_post_feed_controls(page):
    assert page.locator("#searchBar").is_visible()
    page.locator("#tabFiles").click()
    page.wait_for_timeout(400)
    assert not page.locator("#searchBar").is_visible()
    page.locator("#tabTags").click()
    page.wait_for_timeout(400)
    assert page.locator("#searchBar").is_visible()


def test_an_empty_search_says_what_it_searched_for(page):
    page.locator("#searchInput").fill("zzqqxx-nothing")
    page.wait_for_timeout(900)
    empty = page.locator("#feed .empty").text_content()
    assert "zzqqxx-nothing" in empty and "No posts yet" not in empty


def test_an_attachment_image_sits_in_the_reading_column(page, relay_server):
    import base64
    png = base64.b64encode(bytes.fromhex(
        "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
        "890000000a49444154789c6300010000050001" "0d0a2db4" "0000000049454e44ae426082"
    )).decode()
    post = _api(relay_server, "/posts", {
        "title": f"Pictured {time.time()}", "content": "Text first.", "tags": ["homelab"],
    })
    _api(relay_server, "/attachments", {"filename": f"pic-{post['id']}.png", "data": png, "post_id": post["id"]})
    page.goto(f"{relay_server}/id/{post['id']}")
    page.locator("#postModal").wait_for(state="visible")
    page.wait_for_timeout(600)
    lefts = page.evaluate("""() => ({
        text: Math.round(document.querySelector('#pmBody .post-body > p').getBoundingClientRect().left),
        img: Math.round(document.querySelector('#pmBody img.attachment').getBoundingClientRect().left),
    })""")
    assert lefts["img"] == lefts["text"], lefts


BOM_TABLE = """Spec.

| # | Component | Exact product | Price | Status |
|---|---|---|---|---|
| 1 | Case | Define 7 Black TG Dark Tint (FD-C-DEF7A-03), speced | €166.99 | ordered — ETA Aug, checklist due |
"""

WIDE_TABLE = "| " + " | ".join(f"Column{i}" for i in range(10)) + " |\n|" + "---|" * 10 + "\n| " + " | ".join(
    "value" for _ in range(10)) + " |\n"

MEASURE_TABLE = """() => {
    const t = document.querySelector('#pmBody table'); const w = t.closest('.table-scroll');
    const cells = [...t.rows[1].cells]; const heads = [...t.rows[0].cells];
    const lineCount = el => { const r = document.createRange(); r.selectNodeContents(el);
        return new Set([...r.getClientRects()].map(x => Math.round(x.top))).size; };
    return {
        widths: cells.map(c => Math.round(c.getBoundingClientRect().width)),
        valign: getComputedStyle(cells[0]).verticalAlign,
        headLines: heads.map(lineCount),
        scrolls: w.scrollWidth > w.clientWidth,
    };
}"""


def _open(page, base, title, content):
    post = _api(base, "/posts", {"title": f"{title} {time.time()}", "content": content, "tags": ["homelab"]})
    page.goto(f"{base}/id/{post['id']}")
    page.locator("#postModal").wait_for(state="visible")
    page.wait_for_timeout(500)
    return page.evaluate(MEASURE_TABLE)


def test_table_columns_size_to_their_content(page, relay_server):
    """Was `table-layout: fixed`: five equal columns, a 166px "#" beside a
    crushed product description, and middle-aligned cells."""
    m = _open(page, relay_server, "BOM", BOM_TABLE)
    assert m["widths"][0] * 4 < m["widths"][2], m        # "#" narrow, the description wide
    assert m["valign"] == "top", m
    assert set(m["headLines"]) == {1}, f"a header wrapped: {m}"


def test_a_wide_table_scrolls_on_a_phone_instead_of_crushing(mobile_page, relay_server):
    """On a 390px screen the BOM table broke "Component" mid-word into equal
    80px columns; it now keeps its headers whole, gives the prose column room,
    and scrolls inside its box instead."""
    m = _open(mobile_page, relay_server, "BOM phone", BOM_TABLE)
    assert m["scrolls"], m
    assert set(m["headLines"]) == {1}, f"a header broke across lines: {m}"
    assert m["widths"][2] >= 120, m                        # the prose column keeps its floor
    wide = _open(mobile_page, relay_server, "Wide", WIDE_TABLE)
    assert wide["scrolls"] and set(wide["headLines"]) == {1}, wide


def test_a_wrong_key_is_reported_inline_not_in_an_alert(relay_server, browser):
    page = browser.new_page()
    dialogs = []
    page.on("dialog", lambda d: (dialogs.append(d.message), d.dismiss()))
    page.goto(relay_server)
    page.locator("#apiKeyInput").fill("definitely-wrong")
    page.locator("#connectBtn").click()
    page.locator("#loginError").wait_for(state="visible")
    assert "Invalid API key" in page.locator("#loginError").inner_text()
    assert not dialogs, dialogs
    page.close()


def test_phone_grid_tiles_size_to_their_content(mobile_page, relay_server):
    """A single-column grid kept the 340px uniform tile height: a one-line post
    became a screen-tall card of empty space."""
    _api(relay_server, "/posts", {"title": f"Tiny {time.time()}", "content": "x", "tags": ["homelab"]})
    mobile_page.reload()
    mobile_page.locator("#newPostBtn").wait_for(state="visible")
    mobile_page.locator("#vtGrid").click()
    mobile_page.wait_for_timeout(500)
    tiny = mobile_page.locator("#feed .post", has_text="Tiny").first
    assert tiny.bounding_box()["height"] < 250, tiny.bounding_box()


def test_history_restore_is_styled_as_an_action(page, relay_server):
    """It was a full-width grey `.btn-edit` that read as a text field."""
    post = _api(relay_server, "/posts", {"title": f"Versioned {time.time()}", "content": "v1", "tags": ["homelab"]})
    req = urllib.request.Request(f"{relay_server}/posts/{post['id']}", data=json.dumps({"content": "v2"}).encode(),
                                 headers={"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"},
                                 method="PATCH")
    urllib.request.urlopen(req, timeout=10).close()
    page.goto(f"{relay_server}/id/{post['id']}")
    page.locator("#postModal").wait_for(state="visible")
    page.locator("#pmHistory").click()
    page.locator("#hmBody .hm-rev").nth(1).click()
    restore = page.locator(".hm-restore")
    restore.wait_for(state="visible")
    assert "btn-restore" in restore.get_attribute("class")
