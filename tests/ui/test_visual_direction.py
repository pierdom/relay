"""The visual direction (relay #198, B-13): two faces with two jobs, a reading
scale and line length, sentence case, previews that end on whole lines, and a
mark that follows each writer.

Measured in the rendered page — every one of these is a claim about what the
browser computes, not about what the stylesheet says.
"""
from __future__ import annotations

import pytest

from .test_bug_pass import _api, _filter_tag, _open_post, _tag, _unique
from .test_web_ui_review import _as_agent

pytestmark = pytest.mark.ui

_FONT = "(sel) => getComputedStyle(document.querySelector(sel)).fontFamily"


def test_people_read_in_the_sans_and_the_interface_stays_mono(page, relay_server):
    post = _api(relay_server, "POST", "/posts", {
        "title": _unique("Faces"), "content": "Prose with `code` in it.\n\n```\nblock\n```", "tags": ["homelab"]})
    page.reload()
    card = f'.post[data-id="{post["id"]}"]'
    page.locator(card).wait_for()
    for sel in (f"{card} .post-title", f"{card} .post-body p"):
        assert "IBM Plex Sans" in page.evaluate(_FONT, sel), sel
    for sel in (f"{card} .post-body code", f"{card} .post-id-pill", ".tag-name", "#searchInput"):
        assert "IBM Plex Mono" in page.evaluate(_FONT, sel), sel


def test_the_post_view_reads_on_a_scale_at_line_length(page, relay_server):
    body = "## Section\n\n" + "A sentence that keeps going so the paragraph wraps. " * 12
    post = _api(relay_server, "POST", "/posts", {"title": _unique("Scale"), "content": body, "tags": ["homelab"]})
    page.set_viewport_size({"width": 1440, "height": 900})
    _open_post(page, relay_server, post["id"])
    sizes = page.evaluate("""() => {
        const px = s => parseFloat(getComputedStyle(document.querySelector(s)).fontSize);
        return { title: px('#pmTitle'), h2: px('#pmBody .post-body h2'), body: px('#pmBody .post-body p') };
    }""")
    assert sizes == {"title": 22, "h2": 18, "body": 15}, sizes
    # About 75 characters of prose a line. Measured in ems of the body text,
    # not glyphs: CI's browser may not fetch the web font, and a fallback face's
    # glyph widths say nothing about the layout. Plex Sans averages ~0.47em a
    # character, so 32–38em is 68–80 characters.
    ems = page.evaluate("""() => {
        const p = document.querySelector('#pmBody .post-body p');
        return p.getBoundingClientRect().width / parseFloat(getComputedStyle(p).fontSize);
    }""")
    assert 32 <= ems <= 38, ems


def test_card_previews_end_on_whole_lines(page, relay_server):
    tag = _tag()
    para = _api(relay_server, "POST", "/posts", {
        "title": _unique("Long para"), "content": "word " * 400, "tags": [tag]})
    listy = _api(relay_server, "POST", "/posts", {
        "title": _unique("Long list"), "content": "\n".join(f"- item {i}" for i in range(8)), "tags": [tag]})
    page.reload()
    _filter_tag(page, tag)
    page.locator(f'.post[data-id="{para["id"]}"]').wait_for()
    got = page.evaluate("""([pid, lid]) => {
        const p = document.querySelector(`.post[data-id="${pid}"] .post-body p`);
        const cs = getComputedStyle(p);
        const wrap = document.querySelector(`.post[data-id="${pid}"] .post-body-wrap`);
        const items = [...document.querySelectorAll(`.post[data-id="${lid}"] .post-body li`)]
            .filter(li => li.getClientRects().length).length;
        return {
            lines: p.getBoundingClientRect().height / parseFloat(cs.lineHeight),
            fade: getComputedStyle(wrap, '::after').content,
            items,
        };
    }""", [para["id"], listy["id"]])
    assert abs(got["lines"] - 3) < 0.05, got   # three whole lines, not a pixel cut
    assert got["fade"] in ("none", "normal"), got
    assert got["items"] == 3, got


def test_list_cards_keep_a_line_length(page, relay_server):
    post = _api(relay_server, "POST", "/posts",
                {"title": _unique("Wide"), "content": "word " * 200, "tags": ["homelab"]})
    page.set_viewport_size({"width": 1600, "height": 900})
    page.reload()
    body = page.locator(f'.post[data-id="{post["id"]}"] .post-body')
    body.wait_for()
    card = page.locator(f'.post[data-id="{post["id"]}"]').bounding_box()
    assert body.bounding_box()["width"] < card["width"] * 0.7, (body.bounding_box(), card)


def test_a_writer_keeps_one_mark_and_writers_differ(page, relay_server):
    tag = _tag()
    theirs = _as_agent(relay_server, {"title": _unique("By agent"), "content": "v1", "tags": [tag]})
    mine = _api(relay_server, "POST", "/posts", {"title": _unique("By human"), "content": "x", "tags": [tag]})
    page.reload()
    _filter_tag(page, tag)
    page.locator(f'.post[data-id="{mine["id"]}"] .author-mark').wait_for()
    colour = "(sel) => getComputedStyle(document.querySelector(sel)).backgroundColor"
    agent = page.evaluate(colour, f'.post[data-id="{theirs["id"]}"] .author-mark')
    human = page.evaluate(colour, f'.post[data-id="{mine["id"]}"] .author-mark')
    assert agent != human
    _open_post(page, relay_server, theirs["id"])
    assert page.evaluate(colour, "#pmMeta .author-mark") == agent
    page.locator("#pmHistory").click()
    page.locator("#hmBody .hm-author .author-mark").first.wait_for()
    assert page.evaluate(colour, "#hmBody .hm-author .author-mark") == agent


def test_the_master_badge_is_the_cards_label_not_a_box_in_it(page):
    page.locator(".tag-item").first.click()
    badge = page.locator(".post.master-doc .master-badge")
    badge.wait_for()
    assert badge.evaluate("b => getComputedStyle(b).borderTopStyle") == "none"
    assert badge.inner_text().startswith("✦ Master document")
