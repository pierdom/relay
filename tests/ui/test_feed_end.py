"""The end of the feed (relay #198): paging without a "Load more" bar.

The bar sat under the scrolling list, visible the whole time — 51px of a
phone screen — while scrolling already loaded the next page. It was also the
only way past a first page too short to scroll, and the only retry after a
page failed. These pin that the replacement covers all three.
"""
from __future__ import annotations

import pytest

from .test_bug_pass import _api, _filter_tag, _tag, _unique

pytestmark = pytest.mark.ui

PAGES = 45   # more than two pages of 20


@pytest.fixture(scope="module")
def long_tag(relay_server):
    tag = _tag()
    for i in range(PAGES):
        _api(relay_server, "POST", "/posts", {"title": f"Paged {tag} {i:02d}", "content": "one line", "tags": [tag]})
    return tag


def _cards(page) -> int:
    return page.evaluate("document.querySelectorAll('#feed .post').length")


def _end(page) -> str:
    return page.evaluate("document.querySelector('#feed .feed-end')?.textContent ?? null")


def _scroll_to_the_end(page, want: int):
    for _ in range(20):
        if _cards(page) >= want:
            return
        page.evaluate("document.getElementById('feed').scrollTop = 1e9")
        page.wait_for_timeout(250)


def test_nothing_is_pinned_under_the_feed(mobile_page, long_tag):
    mobile_page.locator("#menuBtn").click()   # on a phone the tags are in the drawer
    _filter_tag(mobile_page, long_tag)
    mobile_page.locator("#feed .post").nth(19).wait_for()
    gap = mobile_page.evaluate("""() => document.querySelector('main').getBoundingClientRect().bottom
        - document.getElementById('feed').getBoundingClientRect().bottom""")
    assert gap <= 1, f"{gap}px of the screen sits under the feed"
    assert mobile_page.locator("#loadMoreBtn").count() == 0


def test_scrolling_loads_every_page_then_says_so(page, long_tag):
    _filter_tag(page, long_tag)
    page.locator("#feed .post").nth(19).wait_for()
    assert _cards(page) == 20 and _end(page) == ""   # more to come: nothing to say yet
    _scroll_to_the_end(page, PAGES)
    page.wait_for_function("n => document.querySelector('#feed .feed-end').textContent === `All ${n} posts`",
                           arg=PAGES)
    assert _cards(page) == PAGES
    # The line is inside the list, after the last card.
    assert page.evaluate("document.getElementById('feed').lastElementChild.className") == "feed-end"


def test_a_screen_the_first_page_cannot_fill_fills_itself(page, long_tag):
    page.set_viewport_size({"width": 2560, "height": 2400})
    page.locator("#vtGrid").click()
    _filter_tag(page, long_tag)
    page.wait_for_function("n => document.querySelectorAll('#feed .post').length === n", arg=PAGES, timeout=10_000)
    assert _end(page) == f"All {PAGES} posts"
    page.locator("#vtList").click()


def test_a_failed_page_says_so_and_retries(page, long_tag):
    failing = {"on": True, "asked": 0}

    def maybe_fail(route):
        if failing["on"] and "offset=20" in route.request.url:
            failing["asked"] += 1
            return route.fulfill(status=500, json={"detail": "boom"})
        return route.fallback()

    page.route("**/posts?*", maybe_fail)
    _filter_tag(page, long_tag)
    page.locator("#feed .post").nth(19).wait_for()
    page.evaluate("document.getElementById('feed').scrollTop = 1e9")
    retry = page.locator("#feed .feed-end-retry")
    retry.wait_for()
    assert _end(page).startswith("Couldn’t load more posts.")
    # It waits for Retry: scrolling away and back does not ask a failing server again.
    page.evaluate("document.getElementById('feed').scrollTop = 0")
    page.wait_for_timeout(300)
    page.evaluate("document.getElementById('feed').scrollTop = 1e9")
    page.wait_for_timeout(500)
    assert failing["asked"] == 1 and _cards(page) == 20, failing
    failing["on"] = False
    retry.click()
    _scroll_to_the_end(page, PAGES)
    page.wait_for_function("n => document.querySelectorAll('#feed .post').length === n", arg=PAGES)


def test_a_page_overtaken_by_a_new_filter_is_not_a_failure(page, long_tag, relay_server):
    """A next-page request still in flight when the filter changed came back
    stale and read as a failed page: the new feed stopped at 20 with Retry (it
    flaked the tall-screen test above whenever a tag was clicked mid-fill)."""
    other = _tag()
    for i in range(PAGES):
        _api(relay_server, "POST", "/posts", {"title": f"Other {other} {i:02d}", "content": "x", "tags": [other]})
    page.reload()
    held = []

    def hold(route):
        if f"tag={long_tag}" in route.request.url and "offset=20" in route.request.url:
            held.append(route)
            return None
        return route.fallback()

    page.route("**/posts?*", hold)
    _filter_tag(page, long_tag)
    page.locator("#feed .post").nth(19).wait_for()
    page.evaluate("document.getElementById('feed').scrollTop = 1e9")
    page.wait_for_function("() => document.querySelector('#feed .feed-end').textContent === 'Loading…'")
    _filter_tag(page, other)
    page.wait_for_function(f"() => document.querySelector('#feed .post-title')?.textContent.includes('{other}')")
    held[0].continue_()   # the stale page lands after the new feed painted
    page.wait_for_timeout(300)
    assert page.locator("#feed .feed-end-retry").count() == 0
    _scroll_to_the_end(page, PAGES)
    page.wait_for_function("n => document.querySelectorAll('#feed .post').length === n", arg=PAGES)


def test_the_end_line_follows_live_changes_and_single_posts(page, relay_server):
    tag = _tag()
    one = _api(relay_server, "POST", "/posts", {"title": _unique("Lonely"), "content": "x", "tags": [tag]})
    page.reload()
    _filter_tag(page, tag)
    page.locator(f'.post[data-id="{one["id"]}"]').wait_for()
    assert _end(page) == "The only post"
    _api(relay_server, "POST", "/posts", {"title": _unique("Company"), "content": "x", "tags": [tag]})
    page.wait_for_function("() => document.querySelector('#feed .feed-end').textContent === 'All 2 posts'")
    _api(relay_server, "DELETE", f"/posts/{one['id']}")
    page.wait_for_function("() => document.querySelector('#feed .feed-end').textContent === 'The only post'")


def test_a_search_counts_matching_posts(page, relay_server):
    word = f"zq{_tag()}"
    for _ in range(2):
        _api(relay_server, "POST", "/posts", {"title": _unique("Findable"), "content": word, "tags": ["homelab"]})
    page.locator("#searchInput").fill(word)
    page.wait_for_function("() => document.querySelector('#feed .feed-end')?.textContent === 'All 2 matching posts'")
