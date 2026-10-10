"""Look-and-feel fixes from the 2026-10-09 web UI audit (relay #198, B-12).

Each pins what a person sees, measured in the rendered page — layout claims
(the title keeps its row) are checked on geometry, not on the CSS that is
supposed to produce it.
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


def _api(base_url: str, method: str, path: str, payload: dict | None = None):
    req = urllib.request.Request(
        f"{base_url}{path}", method=method,
        data=json.dumps(payload).encode() if payload is not None else None,
        headers={"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        body = r.read()
        return json.loads(body) if body else None


def test_old_dates_read_as_dates_not_day_counts(page):
    out = page.evaluate("""async () => {
        const src = document.querySelector('script[type=module]').src.replace(/main\\.js$/, 'util.js');
        const { relativeTime } = await import(src);
        const day = 86400e3, now = Date.now();
        return {
          recent: relativeTime(new Date(now - 3 * day).toISOString()),
          old: relativeTime(new Date(now - 400 * day).toISOString()),
          soon: relativeTime(new Date(now + 2 * day).toISOString()),
          later: relativeTime(new Date(now + 90 * day).toISOString()),
        };
    }""")
    assert out["recent"] == "3d ago"
    assert "ago" not in out["old"] and any(ch.isdigit() for ch in out["old"]), out
    assert out["soon"] == "in 2d"
    assert out["later"].startswith("on "), out


def test_a_source_url_shows_its_host_and_links_in_the_modal(page, relay_server):
    url = "https://www.example.org/very/long/path/that/used/to/fill/the/header?q=1"
    post = _api_post(relay_server, {"title": _unique("Sourced"), "content": "x", "tags": ["homelab"], "source": url})
    page.reload()
    src = page.locator(f'.post[data-id="{post["id"]}"] .post-source')
    src.wait_for(timeout=10_000)
    assert src.inner_text() == "via example.org"
    assert src.get_attribute("title") == url

    page.locator(f'.post[data-id="{post["id"]}"] .post-title').click()
    link = page.locator("#pmMeta a.post-source")
    link.wait_for(timeout=10_000)
    assert link.get_attribute("href") == url
    assert link.get_attribute("rel") == "noopener noreferrer"


def test_a_non_http_source_is_never_a_link(page, relay_server):
    post = _api_post(relay_server, {
        "title": _unique("Scripted source"), "content": "x", "tags": ["homelab"], "source": "javascript:alert(1)",
    })
    page.goto(f"{relay_server}/?post={post['id']}")
    page.locator("#postModal.open").wait_for(timeout=10_000)
    assert page.locator("#pmMeta a.post-source").count() == 0
    assert "javascript:alert(1)" in page.locator("#pmMeta .post-source").inner_text()


def test_a_long_title_keeps_its_row_and_the_tags_wrap_under_it(page, relay_server):
    title = _unique("A long title that should be readable in full on a desktop-width card")
    # Its own tags: shared ones (radio, homelab…) would change counts other
    # tests in the session-scoped vault rely on.
    tags = ["wrap-one", "wrap-two", "wrap-three", "wrap-four", "wrap_tag_with_a_long_name"]
    post = _api_post(relay_server, {"title": title, "content": "x", "tags": tags})
    page.reload()
    card = page.locator(f'.post[data-id="{post["id"]}"]')
    card.wait_for(timeout=10_000)
    geo = card.evaluate("""c => {
        const t = c.querySelector('.post-title'), g = c.querySelector('.post-tags');
        return { clipped: t.scrollWidth > t.clientWidth + 1,
                 titleBottom: t.getBoundingClientRect().bottom, tagsTop: g.getBoundingClientRect().top };
    }""")
    assert not geo["clipped"], "title was ellipsised to make room for tags"
    assert geo["tagsTop"] >= geo["titleBottom"] - 1, "tags should wrap below a title that needs the row"


def test_search_names_the_filter_it_runs_inside_and_offers_to_widen(page):
    page.locator(".tag-item", has_text="radio").first.click()
    chip = page.locator("#searchScope")
    chip.wait_for(state="visible", timeout=5_000)
    assert chip.inner_text().startswith("#radio")

    page.locator("#searchInput").fill("zzqqxxnothing")
    empty = page.locator("#feed .empty")
    empty.wait_for(timeout=10_000)
    assert "tagged #radio" in empty.inner_text()

    empty.locator("[data-action=clear-scope]").click()
    chip.wait_for(state="hidden", timeout=5_000)
    page.wait_for_function("() => !document.querySelector('.tag-item.active')?.textContent.includes('radio')")
    assert page.locator("#searchInput").input_value() == "zzqqxxnothing"   # the search survives


def test_delete_offers_undo_instead_of_asking_first(page, relay_server):
    dialogs: list[str] = []
    page.on("dialog", lambda d: (dialogs.append(d.message), d.dismiss()))
    title = _unique("Undo me")
    post = _api_post(relay_server, {"title": title, "content": "x", "tags": ["homelab"]})
    page.reload()
    page.wait_for_timeout(800)   # /status (history on) has to land before the delete decides
    card = page.locator(f'.post[data-id="{post["id"]}"]')
    card.hover()
    card.locator(".btn-delete").click()
    card.wait_for(state="detached", timeout=10_000)
    assert dialogs == []

    toast = page.locator(".toast")
    toast.wait_for(state="visible", timeout=5_000)
    assert title in toast.inner_text()
    toast.locator(".toast-action").click()
    page.locator(f'.post[data-id="{post["id"]}"]').wait_for(timeout=10_000)
    assert _api(relay_server, "GET", f"/posts/{post['id']}")["title"] == title


def test_tag_expiry_form_shows_and_removes_the_current_setting(page, relay_server):
    tag = f"ttl{time.time_ns() % 10**8}"
    _api_post(relay_server, {"title": _unique("Expiring"), "content": "x", "tags": [tag]})
    _api(relay_server, "POST", f"/tags/{tag}/config", {"ttl_hours": 48})
    page.reload()
    row = page.locator(".tag-item", has=page.locator("button.tag-name", has_text=tag)).first
    row.wait_for(timeout=10_000)
    btn = row.locator(".tag-config-btn")
    assert "has-expiry" in (btn.get_attribute("class") or "")
    assert "48h" in (btn.get_attribute("title") or "")

    btn.click()
    form = page.locator(".tag-config-form")
    form.wait_for(timeout=5_000)
    assert form.locator(".tc-ttl").input_value() == "48"
    form.locator(".tc-remove").click()
    page.wait_for_function(
        "t => [...document.querySelectorAll('.tag-item')].some(r => r.querySelector('.tag-name')?.textContent === t"
        " && !r.querySelector('.tag-config-btn.has-expiry'))",
        arg=tag, timeout=10_000,
    )
    assert all(t["ttl_hours"] is None for t in _api(relay_server, "GET", "/tags")["tags"] if t["tag"] == tag)


def test_new_tag_starts_a_post_carrying_it(page):
    """It used to POST an empty tag config, which the server reads as "remove",
    so the new tag silently never appeared."""
    page.locator("#newTagBtn").click()
    page.locator("#tagNewInput").fill("Fresh-Tag")
    page.locator("#tagNewInput").press("Enter")
    page.locator("#composePanel.open").wait_for(timeout=5_000)
    assert "fresh-tag" in page.locator("#cpTags").input_value()
    assert page.evaluate("document.activeElement.id") == "cpTitle"


def test_status_panel_says_semantic_search_is_off_in_one_line(page):
    page.locator("#statusBtn").click()
    page.locator("#statusModal.open").wait_for(timeout=10_000)
    page.get_by_text("Off — search matches words only").wait_for(timeout=10_000)
    rows = page.evaluate("""() => {
        const title = [...document.querySelectorAll('.sm-section-title')]
            .find(t => t.textContent === 'Semantic search');
        return title.parentElement.querySelectorAll('dt').length;
    }""")
    assert rows <= 2
