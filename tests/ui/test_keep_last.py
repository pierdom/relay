"""Keep-last retention in the sidebar's tag form (relay #198, B-2)."""
from __future__ import annotations

import pytest

from .test_bug_pass import _api, _tag, _unique

pytestmark = pytest.mark.ui


def _row(page, tag):
    return page.locator(".tag-item", has=page.locator("button.tag-name", has_text=tag)).first


def test_the_tag_form_sets_keep_last_and_says_what_it_will_expire(page, relay_server):
    tag = _tag()
    for _ in range(5):
        _api(relay_server, "POST", "/posts", {"title": _unique("Digest"), "content": "x", "tags": [tag]})
    page.reload()
    row = _row(page, tag)
    row.wait_for()
    row.hover()
    row.locator(".tag-config-btn").click()
    form = page.locator(".tag-config-form")
    keep = form.locator(".tc-keep")
    keep.fill("3")
    assert form.locator(".tc-note").inner_text().startswith("The oldest 2 of its 5 posts will expire")
    keep.fill("9")
    assert form.locator(".tc-note").is_hidden()   # nothing over the limit, nothing to warn about
    keep.fill("3")
    form.locator(".tc-save").click()
    page.wait_for_function(
        "t => [...document.querySelectorAll('.tag-item')].some(r => r.dataset.tag === t"
        " && r.querySelector('.tag-config-btn.has-expiry'))", arg=tag)
    cfg = next(t for t in _api(relay_server, "GET", "/tags")["tags"] if t["tag"] == tag)
    assert cfg["keep_last"] == 3
    assert _row(page, tag).locator(".tag-config-btn").get_attribute("title") == "Only the newest 3 are kept"


def test_editing_one_rule_keeps_the_others(page, relay_server):
    """The config is replaced whole, so the form must send back what it shows."""
    tag = _tag()
    _api(relay_server, "POST", "/posts", {"title": _unique("Ruled"), "content": "x", "tags": [tag]})
    _api(relay_server, "POST", f"/tags/{tag}/config", {"ttl_hours": 48, "keep_last": 7})
    page.reload()
    row = _row(page, tag)
    row.wait_for()
    assert row.locator(".tag-config-btn").get_attribute("title") == (
        "Posts expire 48h after posting; only the newest 7 are kept")
    row.hover()
    row.locator(".tag-config-btn").click()
    form = page.locator(".tag-config-form")
    assert form.locator(".tc-keep").input_value() == "7"
    form.locator(".tc-ttl").fill("24")
    form.locator(".tc-save").click()
    page.wait_for_function(
        "t => document.querySelector(`.tag-item[data-tag='${t}'] .tag-config-btn`)"
        "?.title.startsWith('Posts expire 24h')", arg=tag)
    cfg = next(t for t in _api(relay_server, "GET", "/tags")["tags"] if t["tag"] == tag)
    assert (cfg["ttl_hours"], cfg["keep_last"]) == (24, 7)
