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
