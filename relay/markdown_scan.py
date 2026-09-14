"""Blank out the parts of a post's raw Markdown that are code, not content.

Two scanners need this: ``links.extract_links`` (wikilink/id-ref resolution,
relay #198 N-5 follow-up) and ``lint.py``'s H1 locator. ``main.js``'s
``extractMedia`` needed the equivalent fix but is JS, so it mirrors the idea
rather than importing this module. ``chunking.py`` has its own, separate,
fence-only stripper (``_strip_code_fences``) predating this one — left
alone; embedding-relevance and link-scanning have different enough needs
(chunking never had a false-positive problem to begin with) that merging
them isn't worth the churn this fix didn't ask for.

Both functions return a string the *same length* as the input, with
newlines untouched — a multiline ``^...$`` regex sees the same line
boundaries it would against the original.
"""
from __future__ import annotations

import re

_FENCE_RE = re.compile(r"```.*?```", re.DOTALL)
# A fenced block or a single-line inline code span — the same two forms
# main.js's own ``preprocessLinks`` already excludes from link rendering.
# Combining both in one pattern is safe under DOTALL: the inline alternative's
# ``[^`\n]*`` still excludes newlines on its own regardless of that flag,
# since DOTALL only changes what ``.`` matches, not an explicit character class.
_CODE_RE = re.compile(r"```.*?```|`[^`\n]*`", re.DOTALL)


def _blank(m: re.Match[str]) -> str:
    return "".join(c if c == "\n" else " " for c in m.group(0))


def strip_fences(content: str) -> str:
    """Replace every ``` fenced block with spaces; inline code is untouched.

    For *locating* something that must not be mistaken for a fenced code
    example — lint.py's H1 scanner uses this to find the real H1 line
    without picking up a heading-shaped line inside a fenced snippet.
    Leaving inline spans alone (unlike strip_code below) matters: a real H1
    can legitimately contain one, and it's still that line's real content,
    not a syntax example — a post's actual H1 was comparing unequal to its
    title for exactly this reason before this function existed (relay #198
    N-5 follow-up).
    """
    return _FENCE_RE.sub(_blank, content)


def strip_code(content: str) -> str:
    """Replace every fenced block and inline code span with spaces.

    For *scanning content for links/refs that must not be mistaken for a
    syntax example* — relay.links.extract_links's whole job (relay #198
    N-5 follow-up: a post documenting relay's own `[[...]]` syntax, or a
    fenced shell/TOML snippet that happens to contain it, used to have every
    example treated as a live link).
    """
    return _CODE_RE.sub(_blank, content)
