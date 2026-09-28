# Themes

relay ships 24 themes. Pick one from the palette button in the header; the choice is saved per browser. Relay Dark is the default.

Every theme is a block of colour tokens in `relay/static/ui/app.css` plus an entry in `relay/static/ui/js/theme.js`. Reproductions of existing schemes (Catppuccin, Gruvbox, Nord…) use only their palette's own colours. Every theme clears the contrast floors in `tests/test_css_tokens.py`: body text at least 7:1, small text (meta, links, tags, buttons) at WCAG AA. Where a palette has no colour that reaches a floor for some role, the shortfall is recorded there with its measured value.

The terminal UI has matching palettes for most themes — set `RELAY_PALETTE=<name>` ([tui.md](tui.md)).

Regenerate this page and its screenshots with `scripts/theme_gallery.py` (see its docstring).


## Relay

| | |
|---|---|
| <b>Relay Dark</b> · dark · TUI: `default`<br><img src="screenshots/themes/dark.png" alt="Relay Dark" width="420"> | <b>Relay Light</b> · light · browser only<br><img src="screenshots/themes/light.png" alt="Relay Light" width="420"> |

## ANSI

| | |
|---|---|
| <b>ANSI Dark</b> · dark · browser only<br><img src="screenshots/themes/ansi-dark.png" alt="ANSI Dark" width="420"> | <b>ANSI Light</b> · light · browser only<br><img src="screenshots/themes/ansi-light.png" alt="ANSI Light" width="420"> |

## Catppuccin

| | |
|---|---|
| <b>Catppuccin Frappé</b> · dark · TUI: `catppuccin-frappe`<br><img src="screenshots/themes/catppuccin-frappe.png" alt="Catppuccin Frappé" width="420"> | <b>Catppuccin Latte</b> · light · TUI: `catppuccin-latte`<br><img src="screenshots/themes/catppuccin-latte.png" alt="Catppuccin Latte" width="420"> |
| <b>Catppuccin Macchiato</b> · dark · TUI: `catppuccin-macchiato`<br><img src="screenshots/themes/catppuccin-macchiato.png" alt="Catppuccin Macchiato" width="420"> | <b>Catppuccin Mocha</b> · dark · TUI: `catppuccin-mocha`<br><img src="screenshots/themes/catppuccin-mocha.png" alt="Catppuccin Mocha" width="420"> |

## GitHub

| | |
|---|---|
| <b>GitHub Dark</b> · dark · TUI: `github-dark`<br><img src="screenshots/themes/github-dark.png" alt="GitHub Dark" width="420"> | <b>GitHub Light</b> · light · TUI: `github-light`<br><img src="screenshots/themes/github-light.png" alt="GitHub Light" width="420"> |

## Gruvbox

| | |
|---|---|
| <b>Gruvbox</b> · dark · TUI: `gruvbox`<br><img src="screenshots/themes/gruvbox.png" alt="Gruvbox" width="420"> | <b>Gruvbox Light</b> · light · browser only<br><img src="screenshots/themes/gruvbox-light.png" alt="Gruvbox Light" width="420"> |

## Solarized

| | |
|---|---|
| <b>Solarized Dark</b> · dark · TUI: `solarized`<br><img src="screenshots/themes/solarized-dark.png" alt="Solarized Dark" width="420"> | <b>Solarized Light</b> · light · TUI: `solarized-light`<br><img src="screenshots/themes/solarized-light.png" alt="Solarized Light" width="420"> |

## More

| | |
|---|---|
| <b>Ayu Dark</b> · dark · TUI: `ayu-dark`<br><img src="screenshots/themes/ayu-dark.png" alt="Ayu Dark" width="420"> | <b>Dracula</b> · dark · TUI: `dracula`<br><img src="screenshots/themes/dracula.png" alt="Dracula" width="420"> |
| <b>Everforest Dark</b> · dark · browser only<br><img src="screenshots/themes/everforest-dark.png" alt="Everforest Dark" width="420"> | <b>Kanagawa</b> · dark · TUI: `kanagawa`<br><img src="screenshots/themes/kanagawa.png" alt="Kanagawa" width="420"> |
| <b>Molokai</b> · dark · TUI: `molokai`<br><img src="screenshots/themes/molokai.png" alt="Molokai" width="420"> | <b>Night Owl</b> · dark · TUI: `night-owl`<br><img src="screenshots/themes/night-owl.png" alt="Night Owl" width="420"> |
| <b>Nord</b> · dark · TUI: `nord`<br><img src="screenshots/themes/nord.png" alt="Nord" width="420"> | <b>One Dark</b> · dark · TUI: `one-dark`<br><img src="screenshots/themes/one-dark.png" alt="One Dark" width="420"> |
| <b>Rosé Pine</b> · dark · TUI: `rose-pine`<br><img src="screenshots/themes/rose-pine.png" alt="Rosé Pine" width="420"> | <b>Tokyo Night</b> · dark · TUI: `tokyo-night`<br><img src="screenshots/themes/tokyo-night.png" alt="Tokyo Night" width="420"> |
