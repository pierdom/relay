"""Recovering things from the UI: the recovery browser, and the diff.

Both close the same gap from opposite ends. Every recovery primitive already
existed — list a post's revisions, read one, restore it — but the UI could only
reach them through the post modal, so it could only recover a post that still
existed, and it had no way to show you *what* a revision would give back.

The browser lives **inside the status panel**, reached by its Recovery section.
It was first built as a fourth sidebar tab, which was wrong twice: Tags/Tree/
Files are ways to browse what exists and this is a different corpus, and the
220px strip had no room for a fourth tab anyway (`test_sidebar_tabs.py`). These
tests therefore go in through `#statusBtn`, and one of them pins the *reason*
the section is in that panel — it reports when history is off, so recovery is
sitting with the thing that decides whether recovery is possible at all.
"""
from __future__ import annotations

import pytest

from .test_smoke import _api_delete, _api_patch, _api_post

pytestmark = pytest.mark.ui


def _open_recovery(page):
    """Status panel → Recovery → the browser. The only route in."""
    page.locator("#statusBtn").click()
    page.locator("#statusModal.open").wait_for(timeout=10_000)
    browse = page.locator("#smBrowseDeleted")
    browse.wait_for(timeout=10_000)
    page.wait_for_function(
        "() => { const b = document.getElementById('smBrowseDeleted'); return b && !b.disabled; }",
        timeout=10_000,
    )
    browse.click()


def test_recovery_sits_with_the_thing_that_decides_whether_it_is_possible(page, relay_server):
    """The Recovery section is in the status panel because that panel already
    answers "does vault history work" — and when it does not, there is nothing
    to recover. This pins the pairing: both are in the same panel, and the
    section reports a count rather than offering a button that cannot help."""
    page.reload()
    page.locator("#statusBtn").click()
    page.locator("#statusModal.open").wait_for(timeout=10_000)
    page.locator("#smBrowseDeleted").wait_for(timeout=10_000)

    titles = [t.strip().lower() for t in page.locator(".sm-section-title").all_inner_texts()]
    assert "health" in titles and "recovery" in titles, f"sections are {titles}"

    line = page.locator(".sm-recovery-line").inner_text().lower()
    assert "restor" in line or "nothing" in line, f"no headline count: {line!r}"


def test_a_deleted_post_can_be_found_and_restored_without_leaving_the_ui(page, relay_server):
    """The whole feature: delete, find it knowing nothing, put it back.

    Before this the only routes back were REST, MCP or `docs/recovery.md` — and
    all three need the post's id, which is exactly what you do not have after
    deleting something by accident.
    """
    made = _api_post(relay_server, {"title": "Gone By Mistake", "tags": ["homelab"],
                                    "content": "the paragraph that must survive"})
    _api_delete(relay_server, made["id"])

    page.reload()
    _open_recovery(page)
    card = page.locator(f'.del-card[data-id="{made["id"]}"]')
    card.wait_for(timeout=10_000)
    assert "Gone By Mistake" in card.inner_text()

    # Preview reads the revision without restoring anything.
    card.get_by_text("Preview").click()
    page.wait_for_timeout(400)
    assert "must survive" in card.locator(".del-body").inner_text()

    page.once("dialog", lambda d: d.accept())
    card.get_by_text("Restore").click()
    page.wait_for_timeout(900)

    assert page.locator(f'.del-card[data-id="{made["id"]}"]').count() == 0, (
        "the restored post is still listed as deleted"
    )
    page.locator("#smClose").click()
    page.wait_for_timeout(600)
    assert page.get_by_text("Gone By Mistake").count() > 0, "restored post did not return to the feed"


def test_the_recovery_browser_separates_the_routine_from_the_alarming(page, relay_server):
    """A TTL sweep and an accident both remove a post; only one is interesting.

    The browser filters on the reason the API reports, so fourteen expired
    digests a week cannot bury the one delete that mattered.

    ⚠️ Scope: this covers the badge and the filter row. The *headline* count in
    the status panel also excludes expiries (`recoverableCount`), and that half
    is **not** covered here — forcing a real TTL sweep needs the cleanup loop,
    which is far too slow for a smoke. The API-side default is pinned by
    `tests/test_deleted_posts.py::test_ttl_expiries_are_excluded_by_default`;
    the JS filter on top of it is currently unpinned.
    """
    made = _api_post(relay_server, {"title": "Reason Shown", "tags": ["homelab"], "content": "x"})
    _api_delete(relay_server, made["id"])

    page.reload()
    _open_recovery(page)
    card = page.locator(f'.del-card[data-id="{made["id"]}"]')
    card.wait_for(timeout=10_000)
    assert card.locator(".del-reason").inner_text().strip().lower() == "deleted"
    assert page.locator(".del-filter").count() >= 2, "no reason filters were offered"


def test_the_history_panel_diffs_a_revision_against_the_post_as_it_stands(page, relay_server):
    """"Nothing surfaces a clobber" — this is the half that shows you what changed.

    Diffing against *current* rather than against the previous revision is
    deliberate: the question you have when you suspect an overwrite is "what
    would restoring give me back", not "what did this commit do".
    """
    made = _api_post(relay_server, {"title": "Overwritten Note", "tags": ["homelab"],
                                    "content": "keep me\nlose me\ntrailing\n"})
    _api_patch(relay_server, made["id"], {"content": "keep me\nbrand new\ntrailing\n"})

    page.reload()
    page.get_by_text("Overwritten Note").first.wait_for(timeout=10_000)
    page.get_by_text("Overwritten Note").first.click()
    page.locator("#postModal.open").wait_for(timeout=10_000)
    page.locator("#pmHistory").click()
    page.locator("#historyModal.open").wait_for(timeout=10_000)

    page.locator("#hmBody .hm-rev").last.wait_for(timeout=10_000)
    page.locator("#hmBody .hm-rev").last.click()          # the create revision
    page.wait_for_timeout(600)

    page.get_by_text("Diff vs current").click()
    page.wait_for_timeout(400)

    removed = page.locator(".hm-diff .hm-d-del").all_inner_texts()
    added = page.locator(".hm-diff .hm-d-add").all_inner_texts()
    assert any("lose me" in t for t in removed), f"the removed line is not marked: {removed}"
    assert any("brand new" in t for t in added), f"the added line is not marked: {added}"
    # Unchanged lines outside the edit are trimmed, so the diff stays readable.
    assert not any("keep me" in t for t in removed + added), "context was reported as a change"


def test_switching_between_body_and_diff_does_not_resize_the_panel(page, relay_server):
    """The panel's fixed height is load-bearing — it exists so selecting a
    revision cannot make it jump. A second pane that measures differently would
    reintroduce exactly that, one toggle removed."""
    made = _api_post(relay_server, {"title": "Steady Diff", "tags": ["homelab"], "content": "a\nb\n"})
    _api_patch(relay_server, made["id"], {"content": "a\nc\n" + "filler\n" * 40})

    page.reload()
    page.get_by_text("Steady Diff").first.wait_for(timeout=10_000)
    page.get_by_text("Steady Diff").first.click()
    page.locator("#postModal.open").wait_for(timeout=10_000)
    page.locator("#pmHistory").click()
    page.locator("#historyModal.open").wait_for(timeout=10_000)
    page.locator("#hmBody .hm-rev").last.wait_for(timeout=10_000)
    page.locator("#hmBody .hm-rev").last.click()
    page.wait_for_timeout(600)

    def shell():
        box = page.locator(".hm-inner").bounding_box()
        return (round(box["width"]), round(box["height"]))

    seen = {shell()}
    page.get_by_text("Diff vs current").click()
    page.wait_for_timeout(300)
    seen.add(shell())
    page.get_by_text("Body", exact=True).click()
    page.wait_for_timeout(300)
    seen.add(shell())
    assert len(seen) == 1, f"the panel resized when switching panes: {sorted(seen)}"
