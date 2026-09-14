"""The vault lint modal: `GET /lint` reached from the status panel.

Two-pane, list left / editor right — the same shape as the post-history
modal (`test_recovery_ui.py`'s diff tests). An earlier flat-list-of-cards
version opened the *full* post modal on click, which lost the finding's own
text the moment you looked at the post it was about, and closing that modal
dropped you back at the main feed instead of the lint list. A later version
fixed both by swapping only the right-hand pane on selection, but still made
you leave to a full post modal to actually fix anything — so the pane now
hosts the real editor (`buildEditForm`, shared with the standalone Edit
modal — see `relay/static/ui/js/edit-form.js`) directly. "Open post"
survives as a smaller, secondary action for what the inline editor doesn't
cover. See `relay/static/ui/js/lint.js` and `relay/lint.py` (relay #198,
N-5).
"""
from __future__ import annotations

import pytest

from .test_smoke import _api_post

pytestmark = pytest.mark.ui


def open_lint(page):
    """Status panel → Vault lint → the modal. The only route in."""
    page.locator("#statusBtn").click()
    page.locator("#statusModal.open").wait_for(timeout=10_000)
    browse = page.locator("#smBrowseLint")
    browse.wait_for(timeout=10_000)
    page.wait_for_function(
        "() => { const b = document.getElementById('smBrowseLint'); return b && !b.disabled; }",
        timeout=10_000,
    )
    browse.click()
    page.locator("#lintModal.open").wait_for(timeout=10_000)


def test_vault_lint_sits_in_the_status_panel(page, relay_server):
    """The Status panel's own section exists, sits under Health, and
    headlines with a count rather than nothing — the seeded posts are
    tag-incomplete on purpose (`seed` in conftest.py never sets a type tag),
    so this vault always has at least one finding."""
    page.reload()
    page.locator("#statusBtn").click()
    page.locator("#statusModal.open").wait_for(timeout=10_000)
    page.locator("#smBrowseLint").wait_for(timeout=10_000)

    titles = [t.strip().lower() for t in page.locator(".sm-section-title").all_inner_texts()]
    assert "health" in titles and "vault lint" in titles, f"sections are {titles}"
    assert titles.index("health") < titles.index("vault lint"), f"sections are {titles}"

    page.wait_for_function(
        "() => document.querySelector('.lint-summary-line')?.textContent.trim().length > 0",
        timeout=10_000,
    )
    line = page.locator(".lint-summary-line").inner_text().lower()
    assert "issue" in line or "checked" in line, f"no headline count: {line!r}"


def test_selecting_a_finding_loads_its_post_into_the_editor(page, relay_server):
    """The whole point of the two-pane layout: the finding's own text and an
    editable copy of the post it is about are visible together, and picking
    another finding never replaces the list with something else."""
    made = _api_post(relay_server, {"title": "Lint Me Untagged", "content": "unique body content", "tags": []})

    page.reload()
    open_lint(page)

    row = page.locator(f'.lm-finding[data-post-id="{made["id"]}"]')
    row.first.wait_for(timeout=10_000)
    row.first.click()

    content_field = page.locator(".lm-pane .ef-content")
    content_field.wait_for(timeout=10_000)
    assert "Zero tags" in page.locator(".lm-pane").inner_text()
    assert content_field.input_value() == "unique body content"
    # The list is still there, right where it was.
    assert page.locator(".lm-list .lm-finding").count() > 0


def test_saving_from_the_pane_fixes_the_finding_and_updates_the_feed(page, relay_server):
    """The reason the editor lives here at all: fix what the finding flags
    without ever leaving the list, and see it take effect immediately."""
    made = _api_post(relay_server, {"title": "Lint Me Fixable", "content": "x", "tags": []})

    page.reload()
    open_lint(page)

    row = page.locator(f'.lm-finding[data-post-id="{made["id"]}"]')
    row.first.wait_for(timeout=10_000)
    row.first.click()

    tags_field = page.locator(".lm-pane .ef-tags")
    tags_field.wait_for(timeout=10_000)
    tags_field.fill("homelab, reference")
    page.locator(".lm-pane .btn-save").click()

    # zero_tags is gone for this post specifically — some other rule (e.g.
    # zero_backlinks) may still legitimately list it, so check by text rather
    # than by absence of the row entirely.
    page.wait_for_function(
        """(id) => ![...document.querySelectorAll(`.lm-finding[data-post-id="${id}"]`)]
                    .some(el => el.textContent.includes('Zero tags'))""",
        arg=str(made["id"]),
        timeout=10_000,
    )
    # The feed card behind the modal picked up the fix too (main.js's onSaved).
    card = page.locator(f'[data-id="{made["id"]}"]')
    assert "homelab" in card.inner_text() and "reference" in card.inner_text()


def test_cancel_in_the_pane_closes_without_a_prompt_when_unchanged(page, relay_server):
    made = _api_post(relay_server, {"title": "Lint Me Untouched", "content": "x", "tags": []})

    page.reload()
    open_lint(page)

    row = page.locator(f'.lm-finding[data-post-id="{made["id"]}"]')
    row.first.wait_for(timeout=10_000)
    row.first.click()
    page.locator(".lm-pane .ef-content").wait_for(timeout=10_000)

    page.locator(".lm-pane .btn-cancel").click()
    # No dialog fired (an unhandled one would otherwise auto-dismiss and,
    # were the guard backwards, leave the pane on a stale placeholder) — the
    # same finding's fresh editor is what should be showing.
    page.wait_for_timeout(300)
    assert page.locator(".lm-pane .ef-content").input_value() == "x"


def test_switching_rows_with_unsaved_changes_asks_to_discard(page, relay_server):
    """The guard this pane needs that a standalone modal does not: leaving an
    in-progress edit is one click away (another row), not a deliberate close."""
    a = _api_post(relay_server, {"title": "Lint Dirty A", "content": "original a", "tags": []})
    b = _api_post(relay_server, {"title": "Lint Dirty B", "content": "original b", "tags": []})

    page.reload()
    open_lint(page)

    row_a = page.locator(f'.lm-finding[data-post-id="{a["id"]}"]').first
    row_a.wait_for(timeout=10_000)
    row_a.click()
    content_field = page.locator(".lm-pane .ef-content")
    content_field.wait_for(timeout=10_000)
    content_field.fill("changed but not saved")

    row_b = page.locator(f'.lm-finding[data-post-id="{b["id"]}"]').first
    page.once("dialog", lambda d: d.dismiss())   # "keep editing"
    row_b.click()
    page.wait_for_timeout(300)
    assert page.locator(".lm-pane .ef-content").input_value() == "changed but not saved", (
        "declining the discard prompt should leave the in-progress edit in place"
    )

    page.once("dialog", lambda d: d.accept())    # "discard"
    row_b.click()
    page.wait_for_function(
        '() => document.querySelector(".lm-pane .ef-content")?.value === "original b"',
        timeout=10_000,
    )


def test_a_tags_only_edit_is_not_silently_discarded(page, relay_server):
    """The exact edit this pane exists for — fixing a missing/zero-tags
    finding by touching only the Tags field, never Content — must be caught
    by the same discard guard as any other change. A guard that only looked
    at the content field would treat this as nothing to lose."""
    a = _api_post(relay_server, {"title": "Lint Tags Only A", "content": "x", "tags": []})
    b = _api_post(relay_server, {"title": "Lint Tags Only B", "content": "y", "tags": []})

    page.reload()
    open_lint(page)

    row_a = page.locator(f'.lm-finding[data-post-id="{a["id"]}"]').first
    row_a.wait_for(timeout=10_000)
    row_a.click()
    tags_field = page.locator(".lm-pane .ef-tags")
    tags_field.wait_for(timeout=10_000)
    tags_field.fill("homelab, reference")

    row_b = page.locator(f'.lm-finding[data-post-id="{b["id"]}"]').first
    page.once("dialog", lambda d: d.dismiss())   # "keep editing"
    row_b.click()
    page.wait_for_timeout(300)
    assert page.locator(".lm-pane .ef-tags").input_value() == "homelab, reference", (
        "a tags-only change was discarded without asking"
    )


def test_open_post_leaves_lint_and_status_and_opens_the_real_post(page, relay_server):
    """Opening the post is a deliberate, separate action — not a side effect
    of merely looking at a finding."""
    made = _api_post(relay_server, {"title": "Lint Me Untagged Too", "content": "x", "tags": []})

    page.reload()
    open_lint(page)

    row = page.locator(f'.lm-finding[data-post-id="{made["id"]}"]')
    row.first.wait_for(timeout=10_000)
    row.first.click()

    open_btn = page.locator(".lm-open")
    open_btn.wait_for(timeout=10_000)
    open_btn.click()

    page.locator("#postModal.open").wait_for(timeout=10_000)
    assert page.locator("#lintModal.open").count() == 0, "lint modal stayed open behind the post"
    assert page.locator("#statusModal.open").count() == 0, "status modal stayed open behind the post"
    assert "Lint Me Untagged Too" in page.locator("#postModal").inner_text()


def test_the_post_modal_shows_a_lint_breadcrumb_that_returns_to_the_same_finding(page, relay_server):
    """The complaint this breadcrumb exists to fix: opening a post from lint
    must not strand you with no way back, and going back must land on the
    same finding you left, not a reset full list."""
    made = _api_post(relay_server, {"title": "Lint Me Breadcrumb", "content": "x", "tags": []})

    page.reload()
    open_lint(page)

    # Filter down first, so the round trip has real state to lose if it isn't
    # actually preserved — a fresh openLintModal() would reset this to "all".
    zero_tags_chip = page.locator(".lm-filter", has_text="Zero tags").first
    zero_tags_chip.wait_for(timeout=10_000)
    zero_tags_chip.click()

    row = page.locator(f'.lm-finding[data-post-id="{made["id"]}"]')
    row.first.wait_for(timeout=10_000)
    row.first.click()
    page.locator(".lm-open").click()

    page.locator("#postModal.open").wait_for(timeout=10_000)
    back = page.locator("#pmBack")
    assert back.is_visible(), "no breadcrumb shown for a post opened from lint"
    assert "vault lint" in back.inner_text().strip().lower()

    back.click()
    page.locator("#lintModal.open").wait_for(timeout=10_000)
    assert page.locator("#postModal.open").count() == 0

    # Still filtered to zero tags, not reset to "all".
    active = page.locator(".lm-filter.active")
    assert active.inner_text().lower().startswith("zero tags"), (
        f"filter was not preserved across the round trip: {active.inner_text()!r}"
    )
    # And the same finding is still the one shown in the pane.
    assert page.locator(".lm-pane .ef-title").input_value() == "Lint Me Breadcrumb"


def test_closing_the_lint_modal_returns_to_status_not_the_feed(page, relay_server):
    """The complaint this modal exists to fix: closing it must not dump you
    back at the main feed, only at whatever was open underneath — the status
    panel, same as history closes back onto the post modal."""
    page.reload()
    open_lint(page)

    page.locator("#lmClose").click()
    assert page.locator("#lintModal.open").count() == 0
    assert page.locator("#statusModal.open").count() == 1, "closing lint should reveal status, not the feed"


def test_escape_closes_lint_before_status(page, relay_server):
    page.reload()
    open_lint(page)

    page.keyboard.press("Escape")
    assert page.locator("#lintModal.open").count() == 0
    assert page.locator("#statusModal.open").count() == 1, "first Escape should only close lint"

    page.keyboard.press("Escape")
    assert page.locator("#statusModal.open").count() == 0


def test_filter_chips_narrow_the_list_to_one_rule(page, relay_server):
    """Self-contained rather than picking "whatever chip is at index 1": the
    vault (`relay_server`) is session-scoped and shared across every UI test
    file, so which rule that index lands on — and how many rows it has —
    depends on what every other test has created and fixed by the time this
    one runs. A post with exactly one, known rule removes that dependency."""
    made = _api_post(relay_server, {"title": "Lint Filter Target", "content": "x", "tags": ["homelab"]})

    page.reload()
    open_lint(page)
    page.locator(".lm-finding").first.wait_for(timeout=10_000)

    label = "Missing type tag"
    chip = page.locator(".lm-filter", has_text=label).first
    chip.wait_for(timeout=10_000)
    chip.click()

    row = page.locator(f'.lm-finding[data-post-id="{made["id"]}"]')
    row.wait_for(timeout=10_000)
    page.wait_for_function(
        """(label) => [...document.querySelectorAll('.lm-finding .hm-msg')]
                       .every(el => el.textContent.trim() === label)""",
        arg=label,
        timeout=10_000,
    )
    rows_text = page.locator(".lm-finding .hm-msg").all_inner_texts()
    assert rows_text, "filtering left no rows"
    assert all(t.strip() == label for t in rows_text), f"filter did not narrow to one rule: {rows_text}"


def test_selecting_a_broken_link_finding_selects_and_highlights_the_broken_text(page, relay_server):
    """The whole point of `LintFinding.match`: land on the exact spot that's
    wrong in a long post, not just the post it's in."""
    filler = "Some unrelated prose to push the link well past the fold.\n\n" * 20
    made = _api_post(relay_server, {
        "title": "Lint Me Locate",
        "content": f"{filler}See [[Nonexistent Post]] for details.\n",
        "tags": ["homelab", "reference"],
    })

    page.reload()
    open_lint(page)

    row = page.locator(f'.lm-finding[data-post-id="{made["id"]}"]').first
    row.wait_for(timeout=10_000)
    row.click()

    content_field = page.locator(".lm-pane .ef-content")
    content_field.wait_for(timeout=10_000)
    page.wait_for_function(
        "() => document.activeElement?.classList?.contains('ef-content')", timeout=10_000,
    )
    selected_text = content_field.evaluate(
        "el => el.value.slice(el.selectionStart, el.selectionEnd)"
    )
    assert selected_text == "[[Nonexistent Post]]", f"selection was {selected_text!r}"

    # The highlight backdrop paints the same span red once /links resolves.
    page.wait_for_function(
        """() => {
          const mark = document.querySelector('.lm-pane .ef-broken-link');
          return mark && mark.textContent === '[[Nonexistent Post]]';
        }""",
        timeout=10_000,
    )
