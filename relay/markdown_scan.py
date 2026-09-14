"""Blank out the parts of a post's raw Markdown that are code, not content.

Three scanners each need this and, until now, none of them had it:
``links.extract_links`` (wikilink/id-ref resolution — relay #198, N-5
follow-up, L-2/L-3), ``lint.py``'s H1 scanner (same follow-up, L-6), and
``main.js``'s ``extractMedia`` (L-7, JS-side — this module has no bearing on
it, but the fix there mirrors the same idea: skip code, don't scan it).
``chunking.py`` has its own, separate, fence-only stripper (``_strip_code_fences``)
predating this one — left alone; embedding-relevance and link-scanning have
different enough needs (chunking never had a false-positive problem to begin
with) that merging them isn't worth the churn this fix didn't ask for.
"""
from __future__ import annotations

import re

# A fenced block or a single-line inline code span — the same two forms
# main.js's own ``preprocessLinks`` already excludes from link rendering.
# Combining both in one pattern is safe under DOTALL: the inline alternative's
# ``[^`\n]*`` still excludes newlines on its own regardless of that flag,
# since DOTALL only changes what ``.`` matches, not an explicit character class.
_CODE_RE = re.compile(r"```.*?```|`[^`\n]*`", re.DOTALL)


def strip_code(content: str) -> str:
    """Replace every fenced block and inline code span with spaces.

    Every non-newline character in a match becomes a space; newlines are left
    alone. This keeps the string's length *and* line/column structure intact
    — a multiline ``^...$`` regex (the H1 scanner) still sees the same line
    boundaries it would against the original text, and a match found in the
    surviving text is guaranteed to be real content, never a syntax example.
    """
    def _blank(m: re.Match[str]) -> str:
        return "".join(c if c == "\n" else " " for c in m.group(0))

    return _CODE_RE.sub(_blank, content)
