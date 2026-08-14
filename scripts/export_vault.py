"""Export a running relay into a fresh Obsidian-style Markdown vault.

Pulls every post over the REST API — so it works against a *remote* instance —
and writes one Markdown file per post: front-matter for
``id/tags/source/created_at/updated_at/expires_at``, the title as the filename,
placement by first domain tag. Then it builds the disposable SQLite index, so
the result is a vault relay can serve directly.

    uv run python scripts/export_vault.py --source https://your-relay.example.com --vault ./snapshot

Use it to snapshot a remote relay to local disk, or to seed a second instance.
It originally existed to migrate the pre-vault SQLite backend (done, Jul 2026);
that path is gone, but pulling a live relay into a vault still works.

Two caveats:

* **Per-tag TTL config is not exported** — the REST API has no read endpoint for
  it. Re-apply tag expiries with ``set_tag_config`` afterwards.
* **Point ``--vault`` at a new or empty directory.** Writing into a populated
  vault does not merge: same-titled posts land beside the existing ones with a
  numeric suffix (``vault.write_file``'s collision rule).

Nothing imports this module and no test covers it; it is a standalone operator
tool that talks to relay over HTTP like any other client.
"""
from __future__ import annotations

import argparse
import asyncio
import re
import sys

import requests

from relay.config import settings


def backfill_title(post: dict) -> str:
    if post.get("title"):
        return post["title"]
    body = post.get("content", "") or ""
    m = re.search(r"^\s*#{1,6}\s+(.+)$", body, re.MULTILINE)
    if m:
        return m.group(1).strip()
    for line in body.splitlines():
        if line.strip():
            return line.strip()[:80]
    return (post.get("created_at") or "untitled").replace(":", "-")


def fetch_all(base: str, key: str) -> list[dict]:
    headers = {"Authorization": f"Bearer {key}"}
    posts: dict[int, dict] = {}

    # Master document (id=0) is not returned by the list endpoint.
    r = requests.get(f"{base}/posts/0", headers=headers, timeout=15)
    if r.status_code == 200:
        posts[0] = r.json()

    offset, limit = 0, 100
    while True:
        r = requests.get(
            f"{base}/posts", headers=headers,
            params={"limit": limit, "offset": offset}, timeout=15,
        )
        r.raise_for_status()
        data = r.json()
        items = data.get("items", [])
        for p in items:
            posts[p["id"]] = p
        offset += limit
        if offset >= data.get("total", 0) or not items:
            break
    return list(posts.values())


def main() -> int:
    ap = argparse.ArgumentParser(description="Export a running relay into a fresh Markdown vault.")
    ap.add_argument("--source", default=settings.relay_base_url, help="Source relay base URL")
    ap.add_argument("--vault", default=settings.vault_path, help="Target vault directory")
    ap.add_argument("--key", default=settings.api_key, help="Bearer API key for the source")
    args = ap.parse_args()

    base = args.source.rstrip("/")
    settings.vault_path = args.vault  # point the vault layer at the target

    # Import after vault_path is set so paths resolve to the target.
    from relay import database, vault

    print(f"Pulling posts from {base} …")
    posts = fetch_all(base, args.key)
    print(f"Fetched {len(posts)} post(s). Writing vault at {args.vault} …")

    for p in posts:
        title = vault.MASTER_TITLE if p["id"] == vault.MASTER_ID else backfill_title(p)
        path = vault.write_file(
            id=p["id"],
            title=title,
            content=p.get("content", ""),
            tags=p.get("tags", []),
            source=p.get("source"),
            created_at=p.get("created_at") or vault.utcnow_iso(),
            updated_at=p.get("updated_at"),
            expires_at=p.get("expires_at"),
        )
        print(f"  #{p['id']:>4}  {path.name}")

    print("Building index …")
    asyncio.run(database.init_db())
    print(f"Done. Vault ready at {args.vault} (index at {settings.database_path}).")
    print("Reminder: per-tag TTL config was not exported — re-apply via set_tag_config if needed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
