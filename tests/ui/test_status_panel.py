"""The status panel reads as one panel, not three stacked ones.

It had drifted into running **three** alignment systems down a single 560px
column, which is what made it feel unbalanced without any one thing looking
wrong:

  * Health was a flex row with its value pushed by `margin-left: auto`, so
    "git 2.55.0" sat hard against the right edge, ~400px from its own label;
  * Vault and Server were `auto 1fr` grids that each sized their own label
    column, so their values started at two *different* x positions;
  * Recovery inherited `.sm-feat-note` and came out right-aligned — an override
    meant to stop that existed but was declared earlier in the file at equal
    specificity, so it lost on source order and never applied. (The same trap as
    `.em-inner`/`.hm-inner`; the fix is that no element needs the override now.)

So this measures the rendered geometry: every label shares one left edge, every
value shares one left edge, and the recovery block sits on the label edge like
everything else. Asserting they *agree* rather than what they agree on, so a
deliberate change to the layout only has to be made in one place.
"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.ui

MEASURE = """() => {
  const left = el => Math.round(el.getBoundingClientRect().left);
  return {
    labels: [...new Set([...document.querySelectorAll('.sm-rows dt, .sm-feat-name')].map(left))],
    values: [...new Set([...document.querySelectorAll('.sm-rows dd, .sm-feat-note')].map(left))],
    recoveryLine: left(document.querySelector('.sm-recovery-line')),
    recoveryBtn: left(document.getElementById('smBrowseDeleted')),
    // The widest value must still fit; a long vault path wraps rather than
    // widening the panel (the same min-width:0 lesson as the grid tiles).
    overflows: document.querySelector('.sm-body').scrollWidth
             > document.querySelector('.sm-body').clientWidth + 1,
  };
}"""


def _open(page):
    page.locator("#statusBtn").click()
    page.locator("#statusModal.open").wait_for(timeout=10_000)
    page.locator("#smBrowseDeleted").wait_for(timeout=10_000)
    page.wait_for_timeout(300)


def test_every_section_shares_one_label_and_value_column(page):
    _open(page)
    m = page.evaluate(MEASURE)

    assert len(m["labels"]) == 1, (
        f"labels start at {len(m['labels'])} different x positions: {sorted(m['labels'])}"
    )
    assert len(m["values"]) == 1, (
        f"values start at {len(m['values'])} different x positions: {sorted(m['values'])}"
    )
    assert not m["overflows"], "the panel scrolls sideways — a value widened it"


def test_recovery_sits_on_the_same_left_edge_as_everything_else(page):
    """Recovery is prose plus a button, not a label/value pair, so it hangs off
    the label edge. It was right-aligned by an inherited class."""
    _open(page)
    m = page.evaluate(MEASURE)
    edge = m["labels"][0]
    assert m["recoveryLine"] == edge, f"recovery text at {m['recoveryLine']}, labels at {edge}"
    assert m["recoveryBtn"] == edge, f"recovery button at {m['recoveryBtn']}, labels at {edge}"


def test_the_label_column_narrows_on_a_phone(mobile_page):
    """A 150px label column of a ~350px sheet leaves the vault path wrapping
    every few characters, so the token shrinks rather than the layout breaking."""
    _open(mobile_page)
    m = mobile_page.evaluate(MEASURE)
    assert len(m["labels"]) == 1 and len(m["values"]) == 1, "the sheet lost the shared columns"
    gutter = m["values"][0] - m["labels"][0]
    assert gutter < 150, f"label column is still {gutter}px wide in a phone sheet"
    assert not m["overflows"], "the sheet scrolls sideways"
