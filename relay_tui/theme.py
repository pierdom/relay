"""The TUI's colours: every palette in ``palettes/*.toml`` becomes a Textual
theme (``relay-<name>``), and every colour on screen is a theme variable —
``$accent``, ``$background`` and relay's own ``$relay-*`` below — never a
constant baked in at import. So the theme can change while the TUI runs
(Ctrl+P → Theme), and the choice is remembered.

relay's variables, computed per theme by `relay_variable_defaults` and
checked for contrast (WCAG AA, 4.5:1) against every surface text sits on,
rather than assumed — the browser's themes have tested floors, and these
mirror them:

- ``relay-muted``: secondary text (meta lines, hints, empty states)
- ``relay-accent-text``: accent-coloured text (titles, tags, keys)
- ``relay-select`` / ``relay-select-text``: a focused list's selected row
- ``relay-select-dim``: an unfocused list's highlighted row, a faint tint
- ``relay-frame``: a panel's frame while it doesn't have focus
- ``relay-badge``: the id badge's fill (the palette's ``border``)
- ``relay-great`` / ``relay-bad`` / ``relay-terrible``: health and expiry
- ``relay-signal``: what stands out in content — tags (qobit's HI-RES/heart pink)
- ``relay-readout``: state read off the chrome, like an LED display — footer
  keys, a panel's order/count line, the live dot (qobit's amber readout)

A palette needs only ``primary`` (the accent), ``bg``, ``border`` and the three
``perf_*`` colours. It may also split the accent from Textual's own primary
(``accent``), and set ``secondary``, ``foreground``, ``surface``, ``panel``,
``boost``, ``muted``, ``signal``, ``readout`` and ``select_alpha`` — cyberdeck
does. Those text colours are still moved until they read.

ANSI themes can't blend: they get the terminal's own colours and no tints.
"""
from __future__ import annotations

import json
import os
import tomllib
from pathlib import Path

from textual.color import Color
from textual.theme import Theme

_PALETTES_DIR = Path(__file__).parent / "palettes"
THEME_PREFIX = "relay-"
DEFAULT_PALETTE = "default"
_MIN_CONTRAST = 4.5


# ── palettes ──────────────────────────────────────────────────────────────────


def palette_names() -> list[str]:
    return sorted(p.stem for p in _PALETTES_DIR.glob("*.toml"))


def load_palette(name: str) -> dict:
    p = Path(name)
    path = p if p.is_absolute() else _PALETTES_DIR / f"{name}.toml"
    try:
        with open(path, "rb") as f:
            return tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError):
        return {}


def is_dark(bg: str, explicit: object = None) -> bool:
    """Whether a palette is dark, from its background's relative luminance —
    so a light palette gets dark text. A palette may say so itself with
    ``dark = true|false``; a non-hex background counts as dark."""
    if isinstance(explicit, bool):
        return explicit
    if not (bg.startswith("#") and len(bg) == 7):
        return True
    return _luminance(Color.parse(bg)) < 0.4


# Palette hints relay_variable_defaults reads and checks for contrast; never
# CSS variables in their own right (the checked values are the relay-* ones).
_HINTS = {"muted": "palette-muted", "signal": "palette-signal", "readout": "palette-readout"}


def theme_for(name: str, palette: dict | None = None) -> Theme:
    """The Textual theme for one palette file."""
    p = load_palette(name) if palette is None else palette

    def hex_or_none(key: str) -> str | None:
        v = str(p.get(key, ""))
        return v if v.startswith("#") else None

    primary = str(p.get("primary", "#f59e0b"))
    accent = hex_or_none("accent") or primary
    bg = str(p.get("bg", "#1c1007"))
    border = str(p.get("border", "#92400e"))
    variables = {"relay-badge": border}
    variables.update({hint: v for key, hint in _HINTS.items() if (v := hex_or_none(key))})
    if (boost := hex_or_none("boost")):
        variables["boost"] = boost
    if isinstance(p.get("select_alpha"), int | float) and bg.startswith("#"):
        variables["palette-select"] = Color.parse(bg).blend(Color.parse(accent), float(p["select_alpha"])).hex
    return Theme(
        name=THEME_PREFIX + Path(name).stem,
        dark=is_dark(bg, p.get("dark")),
        primary=primary,
        secondary=hex_or_none("secondary") or accent,
        accent=accent,
        foreground=hex_or_none("foreground"),
        background=bg,
        surface=hex_or_none("surface") or border,
        panel=hex_or_none("panel") or border,
        warning=hex_or_none("perf_bad"),
        error=hex_or_none("perf_terrible"),
        success=hex_or_none("perf_great"),
        variables=variables,
    )


def all_themes() -> list[Theme]:
    return [theme_for(n) for n in palette_names()]


def display_name(theme_name: str) -> str:
    """What the header shows for a theme: the palette's own name."""
    return theme_name.removeprefix(THEME_PREFIX)


# ── contrast ──────────────────────────────────────────────────────────────────


def _luminance(c: Color) -> float:
    def channel(v: int) -> float:
        x = v / 255
        return x / 12.92 if x <= 0.03928 else ((x + 0.055) / 1.055) ** 2.4

    return 0.2126 * channel(c.r) + 0.7152 * channel(c.g) + 0.0722 * channel(c.b)


def contrast(a: Color, b: Color) -> float:
    """WCAG contrast ratio of two opaque colours (1 to 21)."""
    hi, lo = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def readable(colour: Color, bg: Color) -> Color:
    """``colour``, moved toward black or white just enough to read on ``bg``."""
    target = bg.get_contrast_text(1.0)
    for step in range(11):
        candidate = colour.blend(target, step / 10)
        if contrast(candidate, bg) >= _MIN_CONTRAST:
            return candidate
    return target


def relay_variable_defaults(theme: Theme) -> dict[str, str]:
    """relay's variables for ``theme`` (Textual's built-ins included), unless
    it sets them itself. Measured, not assumed: each text colour is moved until
    it reads on every surface it can sit on."""
    accent_name = theme.accent or theme.primary
    if theme.ansi:
        return {
            "relay-muted": "ansi_bright_black" if theme.dark else "ansi_default",
            "relay-accent-text": accent_name,
            "relay-select": theme.variables.get("block-cursor-background", "ansi_white"),
            "relay-select-text": theme.variables.get("block-cursor-foreground", "ansi_black"),
            "relay-select-dim": "transparent",
            "relay-frame": accent_name,
            "relay-badge": "ansi_bright_black",
            "relay-great": theme.success or "ansi_green",
            "relay-bad": theme.warning or "ansi_yellow",
            "relay-terrible": theme.error or "ansi_red",
            "relay-signal": accent_name,
            "relay-readout": accent_name,
        }
    system = theme.to_color_system().generate()
    bg = Color.parse(system["background"])
    accent = Color.parse(system["accent"])
    fg = Color.parse(system["foreground"])
    hints = theme.variables
    select = Color.parse(hints["palette-select"]) if "palette-select" in hints else bg.blend(accent, 0.30)
    select_dim = bg.blend(accent, 0.12)
    # Text on a selected row: the first of accent, foreground, black/white
    # that reads on it.
    candidates = (accent, fg, select.get_contrast_text(1.0))
    select_text = next(
        (c for c in candidates if contrast(c, select) >= _MIN_CONTRAST),
        max(candidates, key=lambda c: contrast(c, select)),
    )
    # Every surface ordinary text sits on: the page and an unfocused highlight.
    surfaces = (bg, select_dim)
    accent_text = accent
    muted = Color.parse(hints["palette-muted"]) if "palette-muted" in hints else bg.blend(fg, 0.62)
    signal = Color.parse(hints["palette-signal"]) if "palette-signal" in hints else accent
    readout = Color.parse(hints["palette-readout"]) if "palette-readout" in hints else accent
    great, bad, terrible = (Color.parse(system[k]) for k in ("success", "warning", "error"))
    for under in surfaces:
        accent_text, muted = readable(accent_text, under), readable(muted, under)
        signal, readout = readable(signal, under), readable(readout, under)
        great, bad, terrible = readable(great, under), readable(bad, under), readable(terrible, under)
    return {
        "relay-muted": muted.hex,
        "relay-accent-text": accent_text.hex,
        "relay-select": select.hex,
        "relay-select-text": select_text.hex,
        "relay-select-dim": select_dim.hex,
        "relay-frame": bg.blend(accent, 0.45).hex,
        "relay-badge": system.get("panel", bg.blend(fg, 0.15).hex),
        "relay-great": great.hex,
        "relay-bad": bad.hex,
        "relay-terrible": terrible.hex,
        "relay-signal": signal.hex,
        "relay-readout": readout.hex,
    }


# ── the saved choice ──────────────────────────────────────────────────────────


def _state_file() -> Path:
    base = os.environ.get("RELAY_TUI_STATE_DIR") or os.path.join(
        os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state"), "relay-tui"
    )
    return Path(base) / "settings.json"


def load_settings() -> dict:
    try:
        data = json.loads(_state_file().read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save_setting(key: str, value: object) -> None:
    data = load_settings()
    data[key] = value
    try:
        path = _state_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data))
    except OSError:
        pass  # a remembered theme is a convenience; never fail over it


def initial_theme(configured: str, available: set[str]) -> str:
    """The theme to start with: an explicit ``RELAY_PALETTE`` wins, then the
    one picked last time (Ctrl+P → Theme), then relay's default."""
    if configured and configured != DEFAULT_PALETTE:
        name = THEME_PREFIX + Path(configured).stem
        if name in available:
            return name
    saved = load_settings().get("theme")
    if isinstance(saved, str) and saved in available:
        return saved
    return THEME_PREFIX + DEFAULT_PALETTE
