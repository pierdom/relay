"""The stylesheet's colour literals all live in the :root token blocks.

Themes are a token problem, not a file-layout one: a theme can only be a small
block of variable overrides if no component hardcodes a colour behind its back.
Before this was enforced, 37 literals in app.css bypassed the token layer — the
accent written out as `rgba(245,158,11,…)` in nine places (the *dark* accent,
painted in light mode too), a third red matching neither theme, black drop
shadows worn by the cream light surface, and `#2d3348` belonging to no palette
at all.

So the invariant is mechanical: outside `:root` / `:root[data-theme=…]`, a
declaration may reference `var(--token)` or derive from one with `color-mix()`,
but may not name a colour. `rgba(var(--scrim-rgb), .6)` is fine — the channels
come from a token, only the alpha is at the call site.
"""
from __future__ import annotations

import re
from pathlib import Path

CSS = Path(__file__).resolve().parent.parent / "relay" / "static" / "ui" / "app.css"

# A hex colour, or an rgb()/rgba() whose first channel is a number rather than a
# var() reference. `rgba(var(--scrim-rgb), 0.6)` is deliberately not matched.
LITERAL = re.compile(r"#[0-9a-fA-F]{3,8}\b|\brgba?\(\s*\d")

# The blocks that may hold colour values. A theme is written as
# `:root[data-theme="x"], [data-theme="x"] {` — the second selector lets a theme
# paint a subtree, which is what makes the picker's swatches live samples rather
# than hex values copied into a second place.
#
# Deliberately narrow: only `:root`, optionally with an attribute, optionally
# followed by attribute-only selectors. `:root .thing {` must NOT match, or a
# rule full of literals could hide behind a `:root` prefix.
ROOT_BLOCK = re.compile(r"^:root(?:\[[^\]]*\])?(?:\s*,\s*\[[^\]]*\])*\s*\{")


def _strip_comments(css: str) -> str:
    """Blank out /* … */ while preserving line structure.

    A comment may legitimately name a colour — the gruvbox block cites the
    palette's own hex values to say where they came from, and the light block
    records a contrast ratio against `#b96809`. Scanning raw lines flagged those
    as violations, which would have made the rule "do not explain your colours".
    Newlines are kept so reported line numbers still point at the real line.
    """
    out, i, n = [], 0, len(css)
    while i < n:
        start = css.find("/*", i)
        if start == -1:
            out.append(css[i:])
            break
        out.append(css[i:start])
        end = css.find("*/", start + 2)
        if end == -1:
            out.append("\n" * css.count("\n", start))
            break
        out.append("\n" * css.count("\n", start, end + 2))
        i = end + 2
    return "".join(out)


def _lines_outside_root_blocks(css: str) -> list[tuple[int, str]]:
    out, depth, in_root = [], 0, False
    for n, line in enumerate(css.splitlines(), 1):
        opening = ROOT_BLOCK.match(line)
        if opening and depth == 0:
            in_root = True
        if not in_root:
            out.append((n, line))
        depth += line.count("{") - line.count("}")
        if in_root and depth == 0:
            in_root = False
    return out


def test_no_colour_literal_outside_the_token_blocks():
    css = _strip_comments(CSS.read_text(encoding="utf-8"))
    offenders = [
        f"app.css:{n}: {line.strip()}"
        for n, line in _lines_outside_root_blocks(css)
        if LITERAL.search(line)
    ]
    assert not offenders, "colour literals bypass the token layer:\n" + "\n".join(offenders)


def test_both_themes_declare_the_same_token_set():
    """A token defined in one theme and missing from the other falls back to the
    other theme's value — the failure is silent and looks like a design choice."""
    css = CSS.read_text(encoding="utf-8")
    blocks = {}
    pattern = r"^(:root(?:\[[^\]]*\])?)(?:\s*,\s*\[[^\]]*\])*\s*\{(.*?)^\}"
    for match in re.finditer(pattern, css, re.S | re.M):
        # Union, never overwrite: a second block under the same selector used to
        # clobber the first, and an empty one becoming the baseline made every
        # real theme look like it had 23 extra tokens. Properties belong on
        # `html`; `:root` blocks are for tokens.
        found = set(re.findall(r"^\s*(--[\w-]+):", match.group(2), re.M))
        blocks.setdefault(match.group(1), set()).update(found)

    assert len(blocks) >= 3, f"expected dark, light and gruvbox token blocks, found {list(blocks)}"
    baseline = blocks[":root"]
    for selector, tokens in blocks.items():
        if selector == ":root":
            continue
        assert tokens == baseline, (
            f"{selector} token set differs from :root — "
            f"missing {sorted(baseline - tokens)}, extra {sorted(tokens - baseline)}"
        )
