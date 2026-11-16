from __future__ import annotations

import tomllib
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from textual.theme import Theme as TextualTheme

# ── palette loader ────────────────────────────────────────────────────────────
# Looks for  palettes/<name>.toml  next to this file.
# Override the active palette with:  RELAY_PALETTE=<stem>  in the environment.

_PALETTES_DIR = Path(__file__).parent / "palettes"


def palette_name() -> str:
    """Return the name of the active palette (from settings / env / .env)."""
    from relay.config import settings
    return settings.relay_palette


def _load() -> dict[str, str]:
    name = palette_name()
    p = Path(name)
    path = p if p.is_absolute() else _PALETTES_DIR / f"{name}.toml"
    if not path.exists():
        return {}
    try:
        with open(path, "rb") as f:
            return tomllib.load(f)
    except Exception:
        return {}


_p = _load()


def _c(key: str, default: str) -> str:
    return str(_p.get(key, default))


# ── Exported constants ────────────────────────────────────────────────────────

ACCENT    = _c("primary", "#f59e0b")
BORDER    = _c("border",  "#92400e")
HEADER_BG = _c("bg",      "#1c1007")

PERF_GREAT    = _c("perf_great",    "bright_green")
PERF_BAD      = _c("perf_bad",      "yellow")
PERF_TERRIBLE = _c("perf_terrible", "bright_red")


def is_dark(bg: str) -> bool:
    """Whether a palette is dark, from its background's relative luminance —
    so a light palette gets dark text. A palette may say so itself with
    ``dark = true|false``; a non-hex background counts as dark."""
    explicit = _p.get("dark")
    if isinstance(explicit, bool):
        return explicit
    if not (bg.startswith("#") and len(bg) == 7):
        return True

    def channel(hex2: str) -> float:
        c = int(hex2, 16) / 255
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4

    r, g, b = (channel(bg[i:i + 2]) for i in (1, 3, 5))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b < 0.4


def build_textual_theme() -> TextualTheme:
    """Return a Textual Theme derived from the active palette.

    Maps palette constants to Textual's CSS variable layer so that built-in
    widgets (notifications, OptionList, Button, etc.) automatically use the
    same colours as the custom widgets.
    """
    from textual.theme import Theme

    def _hex(val: str) -> str | None:
        return val if val.startswith("#") else None

    return Theme(
        name="relay",
        dark=is_dark(HEADER_BG),
        primary=ACCENT,
        secondary=ACCENT,
        accent=ACCENT,
        background=HEADER_BG,
        surface=BORDER,
        panel=BORDER,
        warning=_hex(PERF_BAD),
        error=_hex(PERF_TERRIBLE),
        success=_hex(PERF_GREAT),
    )
