# Terminal UI

```bash
uv run relay-tui
```

Requires a running relay instance. Set `RELAY_BASE_URL` and `API_KEY` (or put them in `.env`).

## Layout

On a terminal at least 110 columns wide the TUI shows three columns: **topics** (tags, or the folder tree) · **the feed** · **the open post**. Moving through the feed shows each post in the right-hand column as you go; the first post opens on its own, and again when you pick another tag or folder. Following a link moves that column, and `[` / `]` walk back and forward along the links you followed. Editing happens in the column too.

`l` switches between three columns and the plain list, where `Enter` opens a post in a full-screen view. A narrower terminal always gets the list.

## Keybindings

| Key | Action |
|-----|--------|
| `n` / `e` / `d` | New / edit / delete post (in three columns, `e` and `d` act on the open post) |
| `j` / `k` (or ↓ / ↑) | Next / previous post |
| `Enter` | Three columns: move into the post to scroll it (`Esc` returns to the feed). List: open the post |
| `Ctrl+K` | Go to any post by title or `#id` |
| `f` | Follow a link from the open post (outbound and backlinks) |
| `[` / `]` | Back / forward along followed links (three columns) |
| `h` | Post history |
| `l` | Three columns ⇄ list |
| `v` | Recovery — browse and restore deleted posts |
| `/` | Search (title / content / source); keyword, semantic or hybrid when the server has semantic search on |
| `b` | Only posts by the open post's writer (again clears) |
| `u` | Undo the last delete (history on) |
| `a` | Attachment browser (open externally / delete) |
| `t` | Toggle TOPICS: Tags ⇄ Tree (folder) |
| `c` / `R` | Tag expiry and retention (TTL, date, keep-newest-N) / rename (from TOPICS panel) |
| `s` / `o` | Sort field (updated ⇄ created) / order (desc ⇄ asc) |
| `r` / `Tab` | Refresh feed / move between columns |
| `Ctrl+S` / `Esc` | Save / leave the editor (an edit asks first when there are unsaved changes; a new post keeps its draft) |
| `Ctrl+T` | Preview the text in the editor |
| `q` | Quit |

## Deleting

With vault history on, `d` deletes straight away and the notice offers `u` to undo it. With history off a delete can't be undone, so it asks first, naming the post. Older deletes are in Recovery (`v`).

## Writing

The editor is the same for a new post and an edit:

- `[[` lists posts by title; `#` lists them by title or id and inserts `#id`. ↑/↓ choose, Tab or Enter accepts, Esc closes the list. After a bare `#` nothing is chosen until you press ↓ or Tab, so Enter is still a new line.
- The Tags field suggests the vault's tags as you type; → accepts.
- `Ctrl+T` swaps the text for a preview of how it will read, and back.
- A new post is kept as a draft while you write it (under `$XDG_STATE_HOME/relay-tui/`, or `RELAY_TUI_STATE_DIR`), so Esc or quitting loses nothing; `n` opens it again. Publishing or Discard drops it.

## Edit conflicts

A save carries the version of the post the editor opened on. If the post changed in the meantime (an agent appended to it, or you saved it in Obsidian), the save is refused instead of overwriting that change. The editor says who changed it and keeps your text; save again to overwrite on purpose.

SSE runs in a background thread (`● live` / `○ offline`). On reconnect the client replays missed posts via `Last-Event-ID`.

## Per-key scopes

If the key in `API_KEY`/`.env` is scoped (`RELAY_API_KEY_SCOPES` on the server — see [docs/auth.md](auth.md)), the header sub-title shows it next to the live/offline dot: `read-only` for a read-only key, `write:tag1,tag2` for a key restricted to those vault tags. No badge at all means full access — the common case isn't called out every session.

A read-only key blocks `n`/`e`/`d` locally with a toast (`This key is read-only`) instead of sending a request the server would reject anyway. A tag-restricted key is **not** checked client-side — a write outside its allowed tags still reaches the server and comes back as a toast with the server's own error (`This API key's scope does not permit this write`).

## Palettes

Set `RELAY_PALETTE=<name>` to pick a colour scheme. The default (`default`) matches the browser UI's Relay Dark theme.

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
| `solarized` | Solarized Dark — blue accent |
| `solarized-light` | Solarized Light — blue accent |
| `rose-pine` | Rosé Pine — rose accent |
| `one-dark` | One Dark — blue accent |
| `kanagawa` | Kanagawa — crystal-blue accent |
| `github-dark` | GitHub Dark — blue accent |
| `github-light` | GitHub Light (light) — blue accent |
| `ayu-dark` | Ayu Dark — gold accent |
| `ayu-darker` | Ayu Darker — darker canvas, brighter gold |
| `night-owl` | Night Owl — periwinkle accent |
| `candy` | Candy — hot-pink accent (TUI only) |
| `earthy` | Earthy (TUI only) |
| `pastel` | Pastel (TUI only) |
| `tango` | Tango — sky-blue accent (TUI only) |

Palette files live in `relay_tui/palettes/`. Each is a small TOML file — copy one to create your own.

## Background transparency

By default the TUI draws its own background. If your terminal has a custom background image or blur effect, set:

```bash
RELAY_TRANSPARENT=1 uv run relay-tui
```

You can also toggle it at runtime from the command palette (`Ctrl+P` → *Draw theme background*) without restarting.

When transparency is on, editing modals remain opaque so text stays readable against the terminal background.
