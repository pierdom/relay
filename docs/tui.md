# Terminal UI

```bash
uv run relay-tui
```

Requires a running relay instance. Set `RELAY_BASE_URL` and `API_KEY` (or put them in `.env`).

## Keybindings

| Key | Action |
|-----|--------|
| `n` / `e` / `d` | New / edit / delete post |
| `Enter` | Open post detail |
| `h` | Post history (inside detail view) |
| `v` | Recovery — browse and restore deleted posts |
| `/` | Search (title / content / source) |
| `a` | Attachment browser (open externally / delete) |
| `t` | Toggle TOPICS: Tags ⇄ Tree (folder) |
| `c` / `R` | Tag expiry / rename (from TOPICS panel) |
| `s` / `o` | Sort field (updated ⇄ created) / order (desc ⇄ asc) |
| `r` / `Tab` | Refresh feed / switch panel |
| `q` | Quit |

SSE runs in a background thread (`● live` / `○ offline`). On reconnect the client replays missed posts via `Last-Event-ID`.

## Palettes

Set `RELAY_PALETTE=<name>` to pick a colour scheme. The default (`relay`) matches the browser UI's Relay Dark theme.

| Name | Description |
|------|-------------|
| `default` | Relay Dark (amber accent, near-black background) |
| `dracula` | Dracula — purple accent |
| `gruvbox` | Gruvbox Dark — yellow accent |
| `molokai` | Molokai — cyan accent |
| `nord` | Nord — frost cyan accent |
| `tokyo-night` | Tokyo Night — blue accent |
| `catppuccin-latte` | Catppuccin Latte (light) — mauve accent |
| `catppuccin-frappe` | Catppuccin Frappé — peach accent |
| `catppuccin-macchiato` | Catppuccin Macchiato — blue accent |
| `catppuccin-mocha` | Catppuccin Mocha — mauve accent |

Palette files live in `relay_tui/palettes/`. Each is a small TOML file — copy one to create your own.

## Background transparency

By default the TUI draws its own background. If your terminal has a custom background image or blur effect, set:

```bash
RELAY_TRANSPARENT=1 uv run relay-tui
```

You can also toggle it at runtime from the command palette (`Ctrl+P` → *Draw theme background*) without restarting.

When transparency is on, editing modals remain opaque so text stays readable against the terminal background.
