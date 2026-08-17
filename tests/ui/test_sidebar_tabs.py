"""The sidebar tab strip fits its sidebar, on desktop and in the mobile drawer.

This exists because a fourth tab was added (Deleted) and did not fit. At 8px tab
padding and 0.14em tracking the four labels measured 198px inside a 188px content
box, so DELETED's active chip sat on the sidebar border and the 22px `+` add
button — visible in exactly the Tags mode where the strip is widest — was pushed
off the edge entirely. Nothing in the CSS looked wrong; the strip simply
overflowed, and `overflow: hidden` on the aside clipped the evidence rather than
showing it.

The fourth tab is gone (recovery moved into the status panel, which is where it
belonged anyway), so the strip is back to three and has room. **The test is the
part worth keeping**: it measures the rendered geometry — the tabs *plus* the
add button, against the header's content box, in both sidebar widths — so the
next attempt to add a tab fails loudly here instead of silently clipping.

⚠️ The mobile drawer is the tight case, and tight for a reason that must not be
"fixed" by relaxing it: `@media (hover: none)` puts a 44px floor on every tab, so
they cannot shrink however the type is set. Headroom there has to come from the
drawer's own padding, never from the touch target.
"""
from __future__ import annotations

# Roughly one character at the strip's 9px uppercase type. Sized to say "the
# longest label could grow by one and this would still hold", not to taste.
MIN_SLACK = 6

MEASURE = """() => {
  const head = document.querySelector('.sidebar-header');
  const strip = document.querySelector('.sidebar-tabs');
  const add = document.getElementById('newTagBtn');
  const cs = getComputedStyle(head);
  const inner = head.getBoundingClientRect().width
    - parseFloat(cs.paddingLeft) - parseFloat(cs.paddingRight);
  const tabs = [...strip.querySelectorAll('.sb-tab')].map(t => {
    const r = t.getBoundingClientRect();
    return { label: t.textContent, width: r.width, height: r.height, top: Math.round(r.top) };
  });
  return {
    inner,
    strip: strip.getBoundingClientRect().width,
    add: getComputedStyle(add).display === 'none' ? 0 : add.getBoundingClientRect().width,
    clipped: strip.scrollWidth > strip.clientWidth + 1,
    tabs,
  };
}"""


def _assert_strip_fits(page, *, floor: float | None = None):
    m = page.evaluate(MEASURE)

    assert len(m["tabs"]) >= 3, f"expected the full tab set, got {[t['label'] for t in m['tabs']]}"
    assert not m["clipped"], "the tab strip is scrolling inside itself — a label is cut off"

    # The add button belongs to Tags mode, which is also the mode the strip is
    # measured in, so both are competing for the same row.
    demand = m["strip"] + m["add"]
    slack = m["inner"] - demand
    # Not merely `<= inner`. A label is content: it gets renamed, and the font
    # can fall back. Landing inside the box by less than one character's width
    # is not a fit, it is a coincidence — the mobile drawer originally cleared
    # by exactly 1px, which this floor is what catches.
    assert slack >= MIN_SLACK, (
        f"tabs ({m['strip']:.0f}px) + add button ({m['add']:.0f}px) = {demand:.0f}px "
        f"in a {m['inner']:.0f}px header — {slack:.0f}px of headroom, floor is {MIN_SLACK}px"
    )

    # One row: a wrapped tab is the other way this fails, and it does not clip.
    assert len({t["top"] for t in m["tabs"]}) == 1, (
        f"tabs are not on one line: {[(t['label'], t['top']) for t in m['tabs']]}"
    )

    if floor is not None:
        small = [(t["label"], round(t["width"]), round(t["height"])) for t in m["tabs"]
                 if t["width"] < floor or t["height"] < floor]
        assert not small, f"tabs below the {floor}px touch floor: {small}"

    return m


def test_the_tab_strip_fits_the_desktop_sidebar(page):
    _assert_strip_fits(page)


def test_the_tab_strip_fits_the_mobile_drawer(mobile_page):
    """Same invariant at 260px, where every tab is also pinned to 44px."""
    mobile_page.locator("#menuBtn").click()
    mobile_page.wait_for_timeout(400)
    _assert_strip_fits(mobile_page, floor=44)
