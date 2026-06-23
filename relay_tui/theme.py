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

# When RELAY_TRANSPARENT is set, the base canvas (Screen, header, footer,
# scrollbars) uses the terminal's default background (ANSI 49) instead of the
# palette's solid colour, so terminal transparency / background images show
# through.  HEADER_BG itself stays a real colour — it's also used as a
# foreground (e.g. dark text on accent) and for modals, which must remain opaque.
def _transparent() -> bool:
    from relay.config import settings
    return settings.relay_transparent


TRANSPARENT = _transparent()
SCREEN_BG   = "ansi_default" if TRANSPARENT else HEADER_BG

BAR_LOW  = _c("bar_low",  "bright_green")
BAR_MID  = _c("bar_mid",  "yellow")
BAR_HIGH = _c("bar_high", "bright_red")
BAR_BG   = _c("bar_bg",   "#444444")

PERF_GREAT    = _c("perf_great",    "bright_green")
PERF_GOOD     = _c("perf_good",     "green")
PERF_FLAT     = _c("perf_flat",     "cyan")
PERF_BAD      = _c("perf_bad",      "yellow")
PERF_POOR     = _c("perf_poor",     "red")
PERF_TERRIBLE = _c("perf_terrible", "bright_red")


def build_textual_theme() -> "TextualTheme":
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
        dark=True,
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
