"""No emoji or rare-block glyphs as UI icons — they render as blank boxes.

CLAUDE.md already says "inline SVG, not glyphs or emoji", and the browser UI
still shipped ✏️/🗑️ card buttons, 🕑/📎 labels and a ⤺ Restore: on a host
without an emoji font (the headless browser the smoke suite runs in, a lean
Linux desktop) every one rendered as a notdef box. Checked statically because
"does something render" is a question about the font stack, not the markup.
Comments are stripped first — prose may name the glyph it replaced.
"""
from __future__ import annotations

import re
from pathlib import Path

UI = Path(__file__).parent.parent / "relay" / "static"

# Emoji/pictographs, and the supplemental-arrows block ⤺ came from — both
# outside what the UI's monospace font covers. Plain arrows (← → ↑ ↓), ✦, ▾
# and ✓ are in it and stay allowed.
FORBIDDEN = re.compile("[\U0001F000-\U0001FFFF⤀-⥿️]")

COMMENT = {
    ".js": re.compile(r"/\*.*?\*/|(?<![:'\"`])//[^\n]*", re.S),
    ".css": re.compile(r"/\*.*?\*/", re.S),
    ".html": re.compile(r"<!--.*?-->|/\*.*?\*/|(?<![:'\"])//[^\n]*", re.S),
}


def test_no_emoji_icons_in_the_ui_sources():
    offenders = []
    for path in sorted([*UI.glob("index.html"), *UI.glob("ui/*.css"), *UI.glob("ui/js/*.js")]):
        text = COMMENT[path.suffix].sub("", path.read_text(encoding="utf-8"))
        for line in text.splitlines():
            if FORBIDDEN.search(line):
                offenders.append(f"{path.relative_to(UI)}: {line.strip()[:80]}")
    assert not offenders, "emoji/rare glyph used as UI text — draw it in icons.js:\n" + "\n".join(offenders)
