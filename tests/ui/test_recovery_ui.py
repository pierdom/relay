"""Recovering things from the UI: the Deleted view, and the diff.

Both close the same gap from opposite ends. Every recovery primitive already
existed — list a post's revisions, read one, restore it — but the UI could only
reach them through the post modal, so it could only recover a post that still
existed, and it had no way to show you *what* a revision would give back.
"""
from __future__ import annotations

import pytest

from .test_smoke import _api_delete, _api_patch, _api_post

pytestmark = pytest.mark.ui


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
    page.locator("#tabDeleted").click()
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
    page.locator("#tabTags").click()
    page.wait_for_timeout(600)
    assert page.get_by_text("Gone By Mistake").count() > 0, "restored post did not return to the feed"


def test_the_deleted_view_separates_the_routine_from_the_alarming(page, relay_server):
    """A TTL sweep and an accident both remove a post; only one is interesting.

    The sidebar filters on the reason the API reports, so fourteen expired
    digests a week cannot bury the one delete that mattered.
    """
    made = _api_post(relay_server, {"title": "Reason Shown", "tags": ["homelab"], "content": "x"})
    _api_delete(relay_server, made["id"])

    page.reload()
    page.locator("#tabDeleted").click()
    card = page.locator(f'.del-card[data-id="{made["id"]}"]')
    card.wait_for(timeout=10_000)
    assert card.locator(".del-reason").inner_text().strip().lower() == "deleted"
    # The sidebar becomes reason filters rather than tags.
    assert page.locator("#tagList .tag-item").count() >= 2


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
