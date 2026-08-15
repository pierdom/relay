# Recovery

Relay commits the vault to a git repository after every write, so a post that was
overwritten, retagged, or deleted can be brought back. This page is the runbook.

There are two ways in. **In-band** (below) works from anywhere you can reach the
API and covers the common cases; **by hand with git** (the rest of this page) is
the full-power path for anything the API doesn't express.

## In the browser

The post modal has a **🕑 History** button: it lists the post's revisions, previews any of them, and restores with one click. That is the quickest route for the common case — you noticed a post looks wrong and want the previous version back.

It can only reach posts that still exist, though, since the modal is the way in. For a **deleted** post, use the API below or the git runbook further down.

## In-band: the API and MCP tools

```bash
curl -H "Authorization: Bearer $API_KEY" $RELAY/posts/54/history
curl -X POST -H "Authorization: Bearer $API_KEY" -H 'Content-Type: application/json' \
     -d '{"sha":"ab43c1e"}' $RELAY/posts/54/restore
```

Agents can drive the same thing with the `get_post_history` and `restore_post`
MCP tools — so "undo what you just did to #54" is one turn, with no shell.

Both work for a **deleted** post (`"exists": false`), and a restore keeps the
post's original id, so `[[links]]` and `#id` references resolve again. The restore
is itself committed, so it can be undone the same way. A short sha is fine.

Two things the listing deliberately does *not* include: the delete commit itself
(the file has no content at that commit — restore from the revision before it,
which is the newest one listed), and any revision whose front-matter `id` doesn't
match, since a deleted note's filename can later be taken over by a different post.

Everything below is the manual path — for reading diffs, recovering attachments,
or anything the endpoints don't cover. You need a shell on the host relay runs on.

---

## Where the history lives

| | |
|---|---|
| Repository | `<vault>/.relay/history.git` |
| Work-tree | the vault directory itself |
| Enabled by | `RELAY_HISTORY_ENABLED` (default `true`) |
| Requires | the `git` binary — present in the Docker image; without it history disables itself with a warning |

There is deliberately **no `.git` in the vault root**. The vault is usually a
Syncthing folder, and syncing a live object store between machines is a reliable
way to corrupt a repository, so the git dir hides inside `.relay/`, which is
already excluded from sync and invisible to Obsidian. Every git command therefore
has to be told where both halves are.

Set that up once per shell:

```bash
cd /path/to/vault
export GIT_DIR=.relay/history.git
export GIT_WORK_TREE=.
git log --oneline | head        # confirm it works
```

Everything below assumes those two variables are exported.

> **History is per-host.** Commits are only made where relay runs, and `.relay/`
> is not synced, so the repository exists on the server and *not* on any laptop
> the vault syncs to. It also only covers writes made **since v0.2.0 was
> deployed** — there is no history for anything before that.

---

## Reading the history

```bash
git log --oneline                                   # everything, newest first
git log --oneline --follow -- "Dev/Some Note.md"    # one note, across renames
git show <sha>                                      # full diff of one change
git show <sha>:"Dev/Some Note.md"                   # a file as it was at <sha>
git diff HEAD~1 -- "Dev/Some Note.md"               # what the last write changed
```

`--follow` matters: relay renames a note's file when its title changes, so
without it the log stops at the rename.

Commit messages tell you which path made the change:

| Message | Came from |
|---|---|
| `post <id> create\|update\|delete: <title>` | the REST API or an MCP tool |
| `external edit: <file>` / `external change: N edited, M removed` | Obsidian, nvim, anything editing files directly |
| `attachment add\|delete: <name>` | an upload or an attachment delete |
| `tag rename: a -> b (N post(s))` | a tag rename |
| `ttl expiry: N post(s)` | the cleanup loop |
| `vault: initial import` | the first run after history was enabled |

To find *when* a note was damaged, read its log and diff the suspect commit
before restoring anything:

```bash
git log --oneline --follow -- "Dev/Some Note.md"
git show <sha> -- "Dev/Some Note.md"
```

---

## Restoring

### A post that was overwritten

The common case: an agent replaced a good body with a bad reconstruction.

```bash
git checkout <sha> -- "Dev/Some Note.md"      # <sha> = the last good commit
```

That is all. Relay's watcher notices the file change, re-indexes it, and pushes
the update over SSE — the API and UI serve the restored content within a second
or two. The restore is itself committed as `external edit: …`, so it appears in
the log and can be undone in turn.

### A post that was deleted

Identical, but you need a commit from *before* the delete. `git log` on a deleted
path needs `--` to disambiguate:

```bash
git log --oneline -- "Dev/Some Note.md"       # the delete is the newest entry
git checkout <sha> -- "Dev/Some Note.md"      # <sha> = the commit before it
```

The note keeps its **original id**, because the id lives in the file's
front-matter and the watcher indexes by it. Links and `#id` references to it
resolve again.

> **Fixed after v0.2.0** (PR #45). On v0.2.0 exactly, restoring a *deleted* note
> whose bytes are unchanged is silently ignored by the watcher: the file returns
> to disk but never re-enters the index. Overwrites are unaffected. If you are on
> v0.2.0 and see a restored file that the API still 404s, either touch it
> (`printf '\n' >> "<file>"`) or restart relay to force a rebuild from files.

### A post and the attachments deleted with it

`delete_post` removes the attachments that post embedded, and both land in the
**same commit**, so one checkout brings back the whole thing:

```bash
git checkout <sha> -- .        # everything as it was at <sha>
```

Scope it to a folder (`git checkout <sha> -- Homelab/`) if you would rather not
touch the rest of the vault.

### A bad tag rename

A rename rewrites front-matter across every post carrying the tag, in one commit:

```bash
git show <sha>                 # inspect what it did first
git revert --no-commit <sha>   # apply the inverse to the working tree
git reset                      # unstage; relay commits it as an external edit
```

`--no-commit` is the point: it changes the files without making a git commit of
its own, leaving relay's watcher to notice and record it like any other external
edit. Or restore just the affected files with `git checkout <sha>~1 -- <paths>`.

### Posts removed by TTL expiry

Look for `ttl expiry: N post(s)` and restore from the commit before it. Then fix
the TTL that caused it (`expires_at` on the post, or the tag's config) or it will
expire again on the next cleanup pass.

---

## After restoring

Relay picks up restored files automatically **if the watcher is running**
(`RELAY_WATCH_ENABLED=true`, the default). If it is off, or if a restore does not
show up:

```bash
docker compose restart relay
```

The index is rebuilt from the files on every startup, so a restart always
reconciles it with whatever is on disk.

---

## Dangerous commands

The git dir lives *inside* the work-tree, which makes two ordinary commands
destructive here:

| Command | What it does to you |
|---|---|
| `git clean -fdx` | Deletes ignored files — including `.relay/`, which is **the history repo itself** plus the index. Never run it in the vault. |
| `git reset --hard` | Rewrites **every** file in the vault to a past state, not just the one you meant. |
| `git checkout <sha>` *(no `-- <path>`)* | Detaches HEAD and rewrites the whole work-tree. Always pass `-- <path>`. |

Restoring with `git checkout <sha> -- <path>` is safe: it touches only the paths
you name, and the current state is already committed, so nothing is lost.

---

## Backing up the history

The repository is a normal git repo and can be cloned or mirrored:

```bash
git clone --mirror /path/to/vault/.relay/history.git relay-history.git
```

Worth including in the host's backups. It is not covered by vault-file sync,
because `.relay/` is excluded from Syncthing by design.

**Repository growth:** attachments are committed too, so binary uploads
accumulate (each bounded by `ATTACHMENT_MAX_MB`). Tracking them is deliberate —
attachment deletion was a real data-loss path — but `git gc` is the lever if the
repo ever gets uncomfortable:

```bash
git gc --aggressive --prune=now
```
