# MIGRATION — deploying `explore/fs-storage` to production (bespin)

Cut production over from the old SQLite-backed relay to the new **file-backed
Markdown-vault** backend, shipping the local curated vault as the production data.

## Current state

- **Prod:** `relay.geon.im` on **bespin** (Hetzner, arm64, Debian 12), via
  `docker compose` pulling `ghcr.io/pierdom/relay:latest` (built by CI from
  `main` = **old SQLite backend**). Data in the `relay_data` named volume.
- **New backend:** branch `explore/fs-storage` — file-backed vault, **14 commits
  ahead of `main`, 0 behind** (clean fast-forward). 32 tests pass.
- **Canonical data = the local curated vault** (`~/Workspace/relay/vault`, 76
  posts + attachments), *not* prod's DB. Prod is **frozen at id #142**; the local
  vault is the curated superset of the same data (adds the legacy import
  #143–#197, per-domain folders, wikilinks, attachments, the compacted #0, and
  the July cleanup/renames).

## ⚠️ Pre-flight (do these first)

1. **Freeze prod writes — pause the scheduled `financial-analyst` + digest tasks.**
   They publish to prod. *Critical:* the local vault **reused ids #143–#197** for
   the legacy import, so any *new* post the cron would create on old prod (id
   143+) collides with the local vault on cutover. After migration the allocator
   continues at **#198**, so new posts are safe — but only if nothing writes to
   old prod between now and cutover.
2. **Confirm prod max id is #142** (currently true).
3. **Per-tag TTL config:** the local vault has no `.relay/tags.yml`. If prod set
   any per-tag expiries (e.g. on `news`/`digest`), note them now and re-apply
   after cutover with `set_tag_config` — the migrate path can't read them over REST.
4. **Back up the prod data volume** (rollback safety), on bespin:
   ```
   docker run --rm -v relay_data:/data -v "$PWD":/backup alpine \
     tar czf /backup/relay_data.$(date +%F).tgz -C /data .
   ```

## 1 — Ship the code

```
git checkout main && git merge --ff-only explore/fs-storage && git push
```
CI (`.github/workflows/docker.yml`) builds + pushes `ghcr.io/pierdom/relay:latest`
(the new file-backed image). Optionally tag a release (`git tag vX.Y.Z && git push
--tags`) for a pinned, roll-back-able image.

## 2 — Stage the vault on bespin

Bundle the canonical vault (exclude the disposable index and the local Obsidian
workspace config — the index rebuilds, and `.obsidian/` is machine-specific):
```
tar czf relay-vault.tgz -C ~/Workspace/relay/vault --exclude=.relay --exclude=.obsidian .
scp relay-vault.tgz bespin:            # or over Tailscale
```
(If the prod vault will be Syncthing-synced with your Obsidian — see decisions —
keep `.obsidian/` instead so settings stay consistent.)
On bespin, load it into the volume at `/data/vault` **with the container down**:
```
docker compose down
docker run --rm -v relay_data:/data -v "$PWD":/src alpine \
  sh -c 'rm -rf /data/vault && mkdir -p /data/vault && tar xzf /src/relay-vault.tgz -C /data/vault'
```

Confirm `.env` on bespin: `RELAY_VAULT_PATH=/data/vault` (default), `API_KEY`
unchanged, `SECURE_COOKIES=true`, and `RELAY_WATCH_ENABLED` per the sync-topology
decision below.

## 3 — Cut over

```
docker compose pull && docker compose up -d
```
The new relay boots and rebuilds `/data/vault/.relay/index.db` from the files.

## 4 — Verify

- `curl -fs localhost:8000/health`
- `GET /posts` → total **76**; `GET /folders` → the 14 domains; `GET /posts/0`
  → compacted master doc; spot-check a couple of posts; `/ui` loads; `/mcp` responds.
- Re-apply any per-tag TTLs from pre-flight step 3.

## 5 — Unfreeze

Resume the scheduled `financial-analyst` + digest tasks. New posts allocate id
**#198+** and must follow the ISO filename convention (`<Title> YYYY-MM-DD`, see #0).

## Rollback

`docker compose down`; restore the previous image (pinned old tag or digest) and
the backed-up volume (`tar xzf relay_data.<date>.tgz` into a fresh `relay_data`),
then `docker compose up -d`. Old SQLite backend + data return.

## Open decisions (settle before cutover)

- **Sync topology.** Standalone (relay is the only writer via API/MCP) — or
  **Syncthing-synced** (bespin ↔ local/ananas) with `RELAY_WATCH_ENABLED=true`, so
  Obsidian/nvim edits reindex live on prod? Determines how local edits/attachments
  reach production.
- **Known UI-freeze bug** (`HANDOFF.md`): the browser UI can freeze for a few
  seconds when a `.md` is edited *externally while open in the UI*. Edge case,
  non-fatal, only under external edits. Ship-blocker or accept as a known issue?
- **bespin RAM.** It's memory-pressured (~3.8 GiB, many services). The file-backed
  relay + `watchdog` observer is light, but confirm headroom before/after.
