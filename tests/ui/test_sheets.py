"""Mobile bottom sheets: the grab handle, and the drag that follows the thumb.

Two things are pinned here, and the first one is why the file exists.

**The sheet must actually move.** The post modal shipped a swipe-to-dismiss that
looked right in the source and did nothing on screen: `.pm-inner` carried
`animation: … both`, and a forwards fill outranks inline styles, so every
`transform` the touchmove handler wrote was discarded. Only the release fired.
Asserting "the modal closed after a swipe" would have passed against that
broken version — so these tests assert the *transform mid-drag*, which is the
part that was missing.

**Every sheet behaves the same.** Three of the four modals had no handle and no
gesture at all; on a phone they could only be closed by hitting a 22px "×".
Each sheet is therefore checked by the same parametrised test rather than the
post modal getting its own special case.
"""
from __future__ import annotations

import pytest

# (id, opener, modal selector) — every modal that becomes a sheet at <=768px.
SHEETS = [
    ("post", "open_post", "#postModal"),
    ("status", "open_status", "#statusModal"),
    ("edit", "open_edit", "#editModal"),
    ("history", "open_history", "#historyModal"),
]

# Dispatches a real touch sequence on an element. Playwright's touchscreen API
# only taps, and the handlers read `touches[0].clientY` and `timeStamp`, so the
# events have to be built properly rather than faked with mouse events.
DRAG_JS = """
async ([selector, steps, holdAtEnd]) => {
  const el = document.querySelector(selector);
  const r = el.getBoundingClientRect();
  const x = r.left + r.width / 2;
  const y0 = r.top + Math.min(12, r.height / 2);
  const touch = (target, cy) => new Touch({
    identifier: 1, target, clientX: x, clientY: cy, pageX: x, pageY: cy,
  });
  const fire = (type, cy) => {
    const t = touch(el, cy);
    el.dispatchEvent(new TouchEvent(type, {
      bubbles: true, cancelable: true, composed: true,
      touches: type === 'touchend' ? [] : [t],
      targetTouches: type === 'touchend' ? [] : [t],
      changedTouches: [t],
    }));
  };
  fire('touchstart', y0);
  // One move per animation frame, like a real thumb. Firing them back-to-back
  // puts microseconds between events, and the velocity that falls out of that
  // is a flick no finger could perform — the test would then be measuring the
  // dispatch loop rather than the gesture.
  for (const dy of steps) {
    await new Promise(r => setTimeout(r, 16));
    fire('touchmove', y0 + dy);
  }
  if (!holdAtEnd) fire('touchend', y0 + (steps[steps.length - 1] ?? 0));
  return true;
}
"""


def _translate_y(page, selector: str) -> float:
    """The live vertical offset of a sheet, read off the computed transform."""
    return page.evaluate(
        """(sel) => {
          const m = new DOMMatrixReadOnly(getComputedStyle(document.querySelector(sel)).transform);
          return m.m42;
        }""",
        selector,
    )


def open_post(page):
    page.locator(".post-title").nth(2).click()
    page.wait_for_selector("#postModal.open")


def open_status(page):
    page.locator("#statusBtn").click()
    page.wait_for_selector("#statusModal.open")


def open_edit(page):
    open_post(page)
    page.locator("#pmEdit").click()
    page.wait_for_selector("#editModal.open")


def open_history(page):
    open_post(page)
    page.locator("#pmHistory").click()
    page.wait_for_selector("#historyModal.open")


def _open(page, name: str):
    globals()[name](page)
    page.wait_for_timeout(350)  # let the entry animation finish


@pytest.mark.parametrize("name,opener,modal", SHEETS, ids=[s[0] for s in SHEETS])
def test_every_sheet_shows_a_grab_handle(mobile_page, name, opener, modal):
    """The handle is the only thing telling a user the gesture exists."""
    _open(mobile_page, opener)
    handle = mobile_page.evaluate(
        """(sel) => {
          const inner = document.querySelector(sel + ' .pm-inner, ' + sel + ' .sm-inner');
          const cs = getComputedStyle(inner, '::before');
          return { content: cs.content, width: cs.width, height: cs.height, position: cs.position };
        }""",
        modal,
    )
    assert handle["content"] not in ("none", ""), f"{name} sheet has no grab handle"
    assert handle["position"] == "absolute"
    assert handle["width"] == "40px" and handle["height"] == "4px"


@pytest.mark.parametrize("name,opener,modal", SHEETS, ids=[s[0] for s in SHEETS])
def test_sheet_follows_the_thumb_while_dragging(mobile_page, name, opener, modal):
    """The regression that started this: the transform used to be swallowed."""
    _open(mobile_page, opener)
    inner_sel = f"{modal} .pm-inner" if name == "post" else f"{modal} .sm-inner"
    header_sel = f"{modal} .pm-header" if name == "post" else f"{modal} .sm-head"

    assert _translate_y(mobile_page, inner_sel) == 0

    # Hold the drag open (no touchend) so the mid-gesture state can be read.
    mobile_page.evaluate(DRAG_JS, [header_sel, [20, 40, 60], True])
    moved = _translate_y(mobile_page, inner_sel)
    assert moved == pytest.approx(60, abs=2), f"{name} sheet did not track the thumb (got {moved})"

    # Past the dismiss threshold the handle changes colour, which is the only
    # signal that letting go now will close it.
    mobile_page.evaluate(DRAG_JS, [header_sel, [120], True])
    assert mobile_page.locator(f"{inner_sel}.sheet-armed").count() == 1


@pytest.mark.parametrize("name,opener,modal", SHEETS, ids=[s[0] for s in SHEETS])
def test_a_long_drag_dismisses_the_sheet(mobile_page, name, opener, modal):
    _open(mobile_page, opener)
    header_sel = f"{modal} .pm-header" if name == "post" else f"{modal} .sm-head"
    mobile_page.evaluate(DRAG_JS, [header_sel, [40, 90, 140, 190], False])
    # `:not(.open)` is display:none, so wait on the state rather than visibility.
    mobile_page.wait_for_function(
        "sel => !document.querySelector(sel).classList.contains('open')", arg=modal, timeout=3000
    )


@pytest.mark.parametrize("name,opener,modal", SHEETS, ids=[s[0] for s in SHEETS])
def test_a_short_drag_springs_back(mobile_page, name, opener, modal):
    """A sheet nudged a few pixels must return to rest, not sit askew."""
    _open(mobile_page, opener)
    inner_sel = f"{modal} .pm-inner" if name == "post" else f"{modal} .sm-inner"
    header_sel = f"{modal} .pm-header" if name == "post" else f"{modal} .sm-head"

    mobile_page.evaluate(DRAG_JS, [header_sel, [10, 24, 30], False])
    mobile_page.wait_for_timeout(500)

    assert mobile_page.locator(f"{modal}.open").count() == 1, f"{name} sheet closed on a short drag"
    assert _translate_y(mobile_page, inner_sel) == pytest.approx(0, abs=1)
    assert mobile_page.locator(f"{inner_sel}.sheet-dragging").count() == 0


def test_the_gesture_is_mobile_only(page):
    """On desktop the dialog is centred and the drag has nothing to grab —
    binding it there would let a stray trackpad gesture throw the modal away."""
    open_post(page)
    page.wait_for_timeout(300)
    page.evaluate(DRAG_JS, [".pm-header", [40, 90, 140, 190], False])
    page.wait_for_timeout(400)
    assert page.locator("#postModal.open").count() == 1
    assert _translate_y(page, ".pm-inner") == pytest.approx(0, abs=1)


def test_the_edit_sheet_asks_before_a_swipe_throws_work_away(mobile_page):
    """A swipe is far easier to trigger by accident than the "×" it replaces, so
    the dirty check has to run *before* the sheet animates away — and a declined
    confirm has to leave the sheet open with the text still in it."""
    open_edit(mobile_page)
    mobile_page.wait_for_timeout(350)
    field = mobile_page.locator("#editModal .ef-content")
    field.fill("a body the user is not done with")

    mobile_page.once("dialog", lambda d: d.dismiss())        # "keep editing"
    mobile_page.evaluate(DRAG_JS, ["#editModal .sm-head", [40, 90, 140, 190], False])
    mobile_page.wait_for_timeout(500)
    assert mobile_page.locator("#editModal.open").count() == 1, "swipe discarded unsaved changes"
    assert field.input_value() == "a body the user is not done with"
    assert _translate_y(mobile_page, "#editModal .sm-inner") == pytest.approx(0, abs=1)

    mobile_page.once("dialog", lambda d: d.accept())         # "discard"
    mobile_page.evaluate(DRAG_JS, ["#editModal .sm-head", [40, 90, 140, 190], False])
    mobile_page.wait_for_function(
        "() => !document.querySelector('#editModal').classList.contains('open')", timeout=3000
    )


def test_every_desktop_modal_shares_the_same_chrome(page):
    """Four modals, one shell. They are two separate implementations (`.pm-*`
    for the post viewer, `.sm-*` for the other three) that drifted apart in the details
    nobody looks at directly but everybody feels: header padding was 20px on one
    and 18px on the other, only the post body had styled scrollbars, only the
    post modal clipped its corners, and the edit modal's buttons floated at the
    end of the form while the post modal had a proper footer rail.

    Compared as a set rather than against hardcoded values — the point is that
    they agree, not what they agree on.
    """
    page.set_viewport_size({"width": 1500, "height": 950})
    page.wait_for_timeout(200)

    seen = {}
    for name, opener, modal in SHEETS:
        globals()[opener](page)
        page.wait_for_timeout(350)
        sel = f"{modal} .pm-inner" if name == "post" else f"{modal} .sm-inner"
        seen[name] = page.evaluate(
            """(sel) => {
              const inner = document.querySelector(sel);
              const head = inner.querySelector('.pm-header, .sm-head');
              const body = inner.querySelector('.pm-body, .sm-body');
              const cs = getComputedStyle(inner);
              return [
                cs.borderRadius, cs.overflow, cs.boxShadow, cs.backgroundColor,
                cs.borderTopWidth + ' ' + cs.borderTopColor,
                cs.animationName + '/' + cs.animationFillMode + '/' + cs.animationDuration,
                // Vertical only. The post modal's header is a centred article
                // column with its close button pinned to the panel corner, so its
                // horizontal padding is content layout rather than chrome. The
                // shell — radius, clipping, shadow, background, border, entry
                // animation, body inset, scrollbars — must still agree.
                getComputedStyle(head).paddingTop,
                getComputedStyle(body).paddingTop,
                getComputedStyle(body).scrollbarWidth,
              ].join(' | ');
            }""",
            sel,
        )
        page.keyboard.press("Escape")
        page.wait_for_timeout(200)
        if name in ("edit", "history"):
            page.keyboard.press("Escape")  # the post modal underneath
            page.wait_for_timeout(200)

    distinct = set(seen.values())
    assert len(distinct) == 1, "modal chrome has drifted:\n" + "\n".join(
        f"  {k}: {v}" for k, v in seen.items()
    )


def test_modal_footers_share_one_rail(page):
    """The post and edit modals are the two with a footer; it must be the same
    footer. Status and history deliberately have none — the rule is that a
    footer, where one exists, looks identical, not that every panel grows one."""
    page.set_viewport_size({"width": 1500, "height": 950})
    open_post(page)
    page.wait_for_timeout(350)
    post_footer = page.evaluate(
        """() => { const cs = getComputedStyle(document.querySelector('.pm-footer'));
                   return [cs.padding, cs.borderTopWidth, cs.borderTopColor,
                           cs.backgroundColor, cs.justifyContent].join(' | '); }"""
    )
    page.locator("#pmEdit").click()
    page.wait_for_selector("#editModal.open")
    page.wait_for_timeout(350)
    edit_footer = page.evaluate(
        """() => { const cs = getComputedStyle(document.querySelector('#editModal .edit-actions'));
                   return [cs.padding, cs.borderTopWidth, cs.borderTopColor,
                           cs.backgroundColor, cs.justifyContent].join(' | '); }"""
    )
    assert post_footer == edit_footer, f"post: {post_footer}\nedit: {edit_footer}"


@pytest.mark.parametrize("name,opener,modal", SHEETS, ids=[s[0] for s in SHEETS])
def test_sheets_are_full_width_and_bottom_anchored(mobile_page, name, opener, modal):
    """All four fill the width and sit on the bottom edge.

    The edit and history sheets used to be 23px narrower than the other two —
    not a design choice, a **source-order accident**: `.em-inner`/`.hm-inner`
    declare `width: 94%` further down the file than the mobile block's
    `width: 100%`, at equal specificity, so the later rule won. Nothing about
    the CSS looked wrong; only measuring the rendered panels showed it.
    """
    _open(mobile_page, opener)
    inner_sel = f"{modal} .pm-inner" if name == "post" else f"{modal} .sm-inner"
    box = mobile_page.evaluate(
        """(sel) => { const r = document.querySelector(sel).getBoundingClientRect();
                      return { w: Math.round(r.width), bottom: Math.round(r.bottom),
                               vw: window.innerWidth, vh: window.innerHeight }; }""",
        inner_sel,
    )
    assert box["w"] == box["vw"], f"{name} sheet is {box['w']}px in a {box['vw']}px viewport"
    assert abs(box["bottom"] - box["vh"]) <= 1, f"{name} sheet is not on the bottom edge"


@pytest.mark.parametrize("name,opener,modal", SHEETS, ids=[s[0] for s in SHEETS])
def test_no_sheet_control_is_below_the_touch_target_floor(mobile_page, name, opener, modal):
    """44px is the floor for anything you tap.

    Measured before this pass: the close button was **13x22** on three of the
    four sheets, the post footer's History/Edit/Delete were 22px tall, and
    Save/Cancel were 29px. Delete sitting a few pixels from Edit at that size is
    the part that actually costs something.

    Scoped to chrome controls — links inside rendered post bodies are inline
    prose and cannot be 44px without wrecking the text.
    """
    _open(mobile_page, opener)
    inner_sel = f"{modal} .pm-inner" if name == "post" else f"{modal} .sm-inner"
    small = mobile_page.evaluate(
        """(sel) => {
          const inner = document.querySelector(sel);
          const out = [];
          inner.querySelectorAll('button, input:not([type=hidden]), select').forEach(el => {
            if (el.closest('.post-body')) return;           // inline prose links
            const r = el.getBoundingClientRect();
            if (!r.width || !r.height) return;              // hidden
            if (r.width < 44 || r.height < 44)
              out.push((el.id || el.className || el.tagName) +
                       ' ' + Math.round(r.width) + 'x' + Math.round(r.height));
          });
          return out;
        }""",
        inner_sel,
    )
    assert not small, f"{name} sheet has targets below 44px: {small}"


def test_the_post_sheet_leaves_a_backdrop_to_tap(mobile_page, relay_server):
    """It used to be full-bleed at 100vh, which cost three things at once: the
    rounded top and grab handle were pressed against the screen edge (under the
    notch on a real device), and there was no backdrop left to tap, so the sheet
    read as a page rather than something layered over the feed.

    The post has to be long enough to fill the sheet. A short one is sized by
    its content and clears the top edge whatever the max-height says — the first
    version of this test seeded a two-line note and passed against the full-bleed
    layout it was written to reject."""
    from tests.ui.test_smoke import _api_post

    _api_post(relay_server, {
        "title": "Tall Enough To Fill",
        "content": "A body long enough that the sheet reaches its ceiling.\n\n" * 40,
        "tags": ["homelab"],
    })
    mobile_page.reload()
    mobile_page.get_by_text("Tall Enough To Fill").first.wait_for(timeout=10_000)
    mobile_page.get_by_text("Tall Enough To Fill").first.click()
    mobile_page.wait_for_selector("#postModal.open")
    mobile_page.wait_for_timeout(350)
    gap = mobile_page.evaluate(
        "() => Math.round(document.querySelector('.pm-inner').getBoundingClientRect().top)"
    )
    assert gap >= 24, f"post sheet is full-bleed (top={gap})"


def test_edit_sheet_inputs_do_not_trigger_ios_zoom(mobile_page):
    """iOS Safari zooms the page whenever a focused input renders below 16px,
    and the sheet layout does not survive that zoom — the user is left pinching
    back out mid-edit. This is a hard threshold, not a preference.

    Held by `@media (hover: none) { input, textarea, select { font-size: 16px } }`,
    which predates this suite and was easy to miss: the edit form's own rule says
    12px, so the guarantee is invisible unless you measure the computed value.
    Pinned here so a future tidy-up of that `!important` cannot quietly drop it."""
    open_edit(mobile_page)
    mobile_page.wait_for_timeout(350)
    sizes = mobile_page.evaluate(
        """() => [...document.querySelectorAll('#editModal input, #editModal textarea, #editModal select')]
                 .map(el => [el.className || el.type, parseFloat(getComputedStyle(el).fontSize)])"""
    )
    too_small = [s for s in sizes if s[1] < 16]
    assert sizes and not too_small, f"inputs below the 16px iOS zoom threshold: {too_small}"
