"""Migrate a running relay (SQLite-backed) into an Obsidian-style Markdown vault.

Pulls every post over the REST API (works against a remote instance such as
relay.geon.im) and writes one Markdown file per post — front-matter for
``id/tags/source/created_at/updated_at/expires_at``, the title as the filename —
then builds the disposable SQLite index.

    uv run python scripts/migrate_to_vault.py --source https://relay.geon.im --vault ./vault

Note: per-tag TTL config is NOT migrated — the REST API has no read endpoint for
it. Re-apply tag expiries via set_tag_config after migrating.
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
    ap = argparse.ArgumentParser(description=__doc__)
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
    print("Reminder: per-tag TTL config was not migrated — re-apply via set_tag_config if needed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
