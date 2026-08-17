"""The theme button must not stay lit after a tap — but must stay usable by keyboard.

Selecting a theme on a phone left the trigger highlighted. Nothing was stuck in
CSS: `.icon-btn:focus-visible` paints accent text and an accent border, and the
picker called `themeBtn.focus()` after every selection, so the button was
genuinely focused and correctly drawing its focus state. **The focus was the
highlight.**

Returning focus to the trigger is right for a keyboard user — they are mid-flow
and need somewhere sane to tab on from — and meaningless after a tap, where there
is no one to hand it to. So both focus moves are now conditioned on the
activation being a keyboard one (`detail === 0` on the synthesised click).

⚠️ "Is focus currently inside the menu?" looks like the same test and is not:
tapping a <button> focuses it, so that is true after a tap as well. The first fix
used it and changed nothing — the probe still showed the trigger focused.

⚠️ Headless Chromium does not paint `:focus-visible` for a programmatic focus, so
asserting on colour here would pass against the bug. What is asserted is the
state that *causes* the highlight: whether the trigger holds focus at all.
"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.ui

FOCUSED = "() => document.activeElement && document.activeElement.id"


def test_a_tap_leaves_the_theme_button_unfocused(mobile_page):
    page = mobile_page
    page.locator("#themeBtn").tap()
    page.wait_for_timeout(250)
    page.locator('.theme-opt[data-theme-id="nord"]').tap()
    page.wait_for_timeout(400)

    assert page.evaluate(FOCUSED) != "themeBtn", (
        "the theme button kept focus after a tap, so it keeps drawing :focus-visible"
    )
    assert page.evaluate("() => document.documentElement.getAttribute('data-theme')") == "nord", (
        "the theme did not actually change — the fix must not cost the feature"
    )


def test_a_tap_does_not_focus_the_menu_either(mobile_page):
    """Opening by tap should move focus nowhere; the focus grab exists to make
    the arrow keys land somewhere, which a tapping user is not using."""
    page = mobile_page
    page.locator("#themeBtn").tap()
    page.wait_for_timeout(250)
    assert not page.evaluate("() => !!document.activeElement.closest('#themeMenu')"), (
        "tapping the trigger focused a menu option"
    )


def test_the_keyboard_flow_still_moves_focus_where_it_should(page):
    """The half that must survive: open with Enter, arrow down, select, and land
    back on the trigger rather than on `body`."""
    page.locator("#themeBtn").focus()
    page.keyboard.press("Enter")
    page.wait_for_timeout(250)
    assert page.evaluate("() => !!document.activeElement.closest('#themeMenu')"), (
        "keyboard open did not move focus into the menu, so the arrow keys have no anchor"
    )

    page.keyboard.press("ArrowDown")
    page.wait_for_timeout(150)
    chosen = page.evaluate("() => document.activeElement.dataset.themeId")
    assert chosen, "arrow keys did not land on a theme option"

    page.keyboard.press("Enter")
    page.wait_for_timeout(400)
    assert page.evaluate(FOCUSED) == "themeBtn", "focus was dropped instead of returned to the trigger"
    assert page.evaluate("() => localStorage.getItem('relay-theme')") == chosen
