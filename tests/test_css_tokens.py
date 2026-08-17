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


# ── Contrast: two tiers ───────────────────────────────────────────────────────
# The floor started as one number reverse-engineered from Relay Dark, and adding
# Catppuccin showed why that was wrong: three of its four flavours fell below it,
# and Latte's prose tops out at 7.06:1 because the palette has no darker text
# than #4c4f69. Catppuccin is not badly designed — relay's own themes are simply
# unusually contrasty (13.9:1 and 16.7:1), and a number taken from them is a
# house style, not a standard.
#
# So: an **accessibility floor** every theme must clear, no exceptions, and a
# **house target** that relay's own themes are designed against. A faithful
# reproduction of someone else's scheme is held to the first only, and every
# such theme is named below with its measured value so the gap stays visible
# rather than becoming a silent allowance.
FLOOR_BODY = 7.0          # WCAG AAA, normal text
FLOOR_ON_ACCENT = 4.5     # WCAG AA — this sits on every primary button
FLOOR_CHIP = 4.5          # WCAG AA — chips render at 9-10px

HOUSE_TEXT = 10.0
HOUSE_BODY = 9.5

# Reproductions, with the measured prose contrast that keeps them out of the
# house target. Each is at its palette's ceiling — none can be raised without
# mixing a colour the original scheme does not contain.
REPRODUCTIONS = {
    "catppuccin-latte": 7.06,
    "catppuccin-frappe": 8.06,
    "catppuccin-macchiato": 9.92,
}


def _luminance(hex_colour: str) -> float:
    raw = hex_colour.lstrip("#")
    if len(raw) == 3:   # `--on-accent: #000` is shorthand, and it is the token
        raw = "".join(c * 2 for c in raw)   # this check most needs to read
    channels = [int(raw[i:i + 2], 16) / 255 for i in (0, 2, 4)]
    linear = [c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def _contrast(a: str, b: str) -> float:
    la, lb = _luminance(a), _luminance(b)
    return (max(la, lb) + 0.05) / (min(la, lb) + 0.05)


def _hex_tokens() -> dict[str, dict[str, str]]:
    """Plain hex tokens per theme, 3- and 6-digit. Alpha forms are skipped: they
    sit over an unknown backdrop, so a static contrast number for them would be
    fiction. Missing `#000` shorthand is how this check first failed — silently
    dropping the one token that decides every primary button's legibility."""
    css = _strip_comments(CSS.read_text(encoding="utf-8"))
    out: dict[str, dict[str, str]] = {}
    pattern = r"^(:root(?:\[[^\]]*\])?)(?:\s*,\s*\[[^\]]*\])*\s*\{(.*?)^\}"
    for match in re.finditer(pattern, css, re.S | re.M):
        name = match.group(1)
        key = "dark" if name == ":root" else re.search(r'"([^"]+)"', name).group(1)
        found = dict(re.findall(r"^\s*(--[\w-]+):\s*(#[0-9a-fA-F]{3}|#[0-9a-fA-F]{6})\s*;", match.group(2), re.M))
        if found:
            out.setdefault(key, set() if False else {}).update(found)
    return out


def test_every_theme_clears_the_accessibility_floor():
    """No exceptions. A theme that cannot reach these does not ship."""
    themes = _hex_tokens()
    assert len(themes) >= 6, f"expected every theme block to parse, got {sorted(themes)}"

    failures = []
    for theme, tokens in sorted(themes.items()):
        body = _contrast(tokens["--body"], tokens["--surface"])
        on_accent = _contrast(tokens["--on-accent"], tokens["--accent"])
        if body < FLOOR_BODY:
            failures.append(f"{theme}: --body on --surface is {body:.2f}:1 (floor {FLOOR_BODY})")
        if on_accent < FLOOR_ON_ACCENT:
            failures.append(f"{theme}: --on-accent on --accent is {on_accent:.2f}:1 (floor {FLOOR_ON_ACCENT})")
    assert not failures, "themes below the accessibility floor:\n  " + "\n  ".join(failures)


def test_house_themes_clear_the_house_target():
    """relay's own themes are designed against a higher bar than the standard.

    A reproduction is exempt, but only by being named in `REPRODUCTIONS` with
    the number that keeps it out — and if one improves past the target, this
    fails too, so the list cannot rot into a blanket allowance.
    """
    failures = []
    for theme, tokens in sorted(_hex_tokens().items()):
        text = _contrast(tokens["--text"], tokens["--surface"])
        body = _contrast(tokens["--body"], tokens["--surface"])
        if theme in REPRODUCTIONS:
            recorded = REPRODUCTIONS[theme]
            if abs(body - recorded) > 0.05:
                failures.append(f"{theme}: recorded at {recorded}:1, now measures {body:.2f}:1 — update or promote it")
            elif text >= HOUSE_TEXT and body >= HOUSE_BODY:
                failures.append(f"{theme}: now clears the house target; drop it from REPRODUCTIONS")
            continue
        if text < HOUSE_TEXT:
            failures.append(f"{theme}: --text on --surface is {text:.2f}:1 (house target {HOUSE_TEXT})")
        if body < HOUSE_BODY:
            failures.append(f"{theme}: --body on --surface is {body:.2f}:1 (house target {HOUSE_BODY})")
    assert not failures, "themes below the house target:\n  " + "\n  ".join(failures)


# A decorative chip — the post-id pill, the tag count — is its accent's colour on
# a 10% tint of that same accent. Same hue on both sides caps the contrast at the
# accent's own contrast against the surface, and it *approaches* that cap as the
# tint lightens, so the tint is deliberately faint rather than a comfortable
# chip. 18% put three themes under 4:1.
CHIP_TINT = 0.10


def _composite(fg: str, bg: str, alpha: float) -> str:
    def channels(h):
        raw = h.lstrip("#")
        if len(raw) == 3:
            raw = "".join(c * 2 for c in raw)
        return [int(raw[i:i + 2], 16) for i in (0, 2, 4)]
    f, b = channels(fg), channels(bg)
    blend = (round(f[i] * alpha + b[i] * (1 - alpha)) for i in range(3))
    return "#" + "".join(f"{c:02x}" for c in blend)


def test_decorative_chips_stay_legible():
    """`--accent-2` / `--accent-3` text on their own tint, in every theme.

    No exemption. Catppuccin Latte is the case that proves the rule bites: every
    one of its fourteen accent members lands under AA as chip text on its own
    base — mauve, the best, reaches 4.18 — so it spends mauve on the id pill and
    `text` on the tag count rather than shipping two illegible colourful ones.
    """
    failures = []
    for theme, tokens in sorted(_hex_tokens().items()):
        for token in ("--accent-2", "--accent-3"):
            chip = _composite(tokens[token], tokens["--surface"], CHIP_TINT)
            got = _contrast(tokens[token], chip)
            if got < FLOOR_CHIP:
                failures.append(f"{theme}: {token} on its chip is {got:.2f}:1 (floor {FLOOR_CHIP})")
    assert not failures, "chips below the legibility floor:\n  " + "\n  ".join(failures)
