"""Regenerate docs/themes.md and its screenshots from the theme registry.

Run against a relay serving a *copy* of sample_vault/ — never your real vault:

    cp -r sample_vault /tmp/demo-vault
    API_KEY=demo RELAY_VAULT_PATH=/tmp/demo-vault SECURE_COOKIES=false \\
        uv run uvicorn relay.main:app --port 8791 &
    uv run python scripts/theme_gallery.py --base http://127.0.0.1:8791 --key demo

Each theme is selected through the real picker (so the brand mark and anything
else theme.js paints is right), then the feed is captured at 1200x720 into
docs/screenshots/themes/<id>.png. The page groups themes exactly as the picker
does: Relay first, then the named families, then the rest alphabetically.
"""
from __future__ import annotations

import argparse
import re
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
THEME_JS = ROOT / "relay" / "static" / "ui" / "js" / "theme.js"
SHOTS = ROOT / "docs" / "screenshots" / "themes"
PAGE = ROOT / "docs" / "themes.md"
PALETTES = ROOT / "relay_tui" / "palettes"
TUI_NAMES = {"dark": "default", "solarized-dark": "solarized"}   # web id -> TUI palette where they differ


def catalogue() -> tuple[list[dict], list[str], dict[str, str]]:
    js = THEME_JS.read_text(encoding="utf-8")
    themes = []
    for line in js.splitlines():
        m = re.match(r"\s*\{ id: '([^']+)',\s*label: '([^']+)',\s*dark: (true|false)(.*)\},", line)
        if m:
            group = re.search(r"group: '([^']+)'", m[4])
            themes.append({"id": m[1], "label": m[2], "dark": m[3] == "true",
                           "group": group[1] if group else None, "signature": "signature: true" in m[4]})
    order = [g.strip(" '") for g in re.search(r"const GROUP_ORDER = \[([^\]]+)\]", js)[1].split(",")]
    labels = dict(re.findall(r"(\w+): '([^']+)'", re.search(r"const GROUP_LABELS = \{([^}]+)\}", js)[1]))
    return themes, order, labels


def sections(themes: list[dict], order: list[str], labels: dict[str, str]) -> list[tuple[str, list[dict]]]:
    by_label = lambda ts: sorted(ts, key=lambda t: t["label"])   # noqa: E731
    out = [("Relay", [t for t in themes if t["signature"]])]
    out += [(labels[g], by_label([t for t in themes if t["group"] == g])) for g in order[1:]]
    out.append(("More", by_label([t for t in themes if not t["group"] and not t["signature"]])))
    return out


def capture(base: str, key: str, themes: list[dict]) -> None:
    SHOTS.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1200, "height": 720})
        page.goto(base)
        page.fill("#apiKeyInput", key)
        page.click("#connectBtn")
        page.wait_for_selector("#newPostBtn")
        for theme in themes:
            page.goto(base + "/")
            page.wait_for_selector("#newPostBtn")
            page.click("#themeBtn")
            page.click(f'.theme-opt[data-theme-id="{theme["id"]}"]')
            page.mouse.move(5, 700)   # no hover state in the shot
            page.wait_for_timeout(500)
            page.screenshot(path=str(SHOTS / f"{theme['id']}.png"))
        browser.close()


def cell(theme: dict) -> str:
    tui = TUI_NAMES.get(theme["id"], theme["id"])
    where = f"TUI: `{tui}`" if (PALETTES / f"{tui}.toml").exists() else "browser only"
    kind = "dark" if theme["dark"] else "light"
    return (f'<b>{theme["label"]}</b> · {kind} · {where}<br>'
            f'<img src="screenshots/themes/{theme["id"]}.png" alt="{theme["label"]}" width="420">')


INTRO = (
    "Pick one from the palette button in the header; the choice is saved per browser. "
    "Relay Dark is the default.\n\n"
    "Every theme is a block of colour tokens in `relay/static/ui/app.css` plus an entry in "
    "`relay/static/ui/js/theme.js`. Reproductions of existing schemes (Catppuccin, Gruvbox, Nord…) "
    "use only their palette's own colours. Every theme clears the contrast floors in "
    "`tests/test_css_tokens.py`: body text at least 7:1, small text (meta, links, tags, buttons) at "
    "WCAG AA. Where a palette has no colour that reaches a floor for some role, the shortfall is "
    "recorded there with its measured value.\n\n"
    "The terminal UI has matching palettes for most themes — set `RELAY_PALETTE=<name>` ([tui.md](tui.md)).\n\n"
    "Regenerate this page and its screenshots with `scripts/theme_gallery.py` (see its docstring).\n"
)


def write_page(themes: list[dict], grouped: list[tuple[str, list[dict]]]) -> None:
    lines = [f"# Themes\n\nrelay ships {len(themes)} themes. " + INTRO]
    for name, members in grouped:
        lines += [f"\n## {name}\n", "| | |", "|---|---|"]
        for i in range(0, len(members), 2):
            pair = members[i:i + 2]
            lines.append("| " + " | ".join(cell(t) for t in pair) + (" | |" if len(pair) == 1 else " |"))
    PAGE.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base", required=True, help="URL of a relay serving a copy of sample_vault/")
    parser.add_argument("--key", required=True, help="that relay's API_KEY")
    parser.add_argument("--no-screenshots", action="store_true", help="only rewrite docs/themes.md")
    args = parser.parse_args()
    themes, order, labels = catalogue()
    if not args.no_screenshots:
        capture(args.base.rstrip("/"), args.key, themes)
    write_page(themes, sections(themes, order, labels))
    print(f"{len(themes)} themes -> {PAGE.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
