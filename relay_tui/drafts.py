"""The Compose draft: kept on disk while you write, so an Esc, a crash or a
quit doesn't lose a half-written post. Dropped on publish or a deliberate
discard — the browser's `relay-draft` does the same in localStorage."""
from __future__ import annotations

import json
import os
from pathlib import Path


def _path() -> Path:
    base = os.environ.get("RELAY_TUI_STATE_DIR") or os.path.join(
        os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state"), "relay-tui"
    )
    return Path(base) / "draft.json"


def load() -> dict | None:
    try:
        data = json.loads(_path().read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and any(str(v).strip() for v in data.values()) else None


def save(fields: dict) -> None:
    if not any(str(v or "").strip() for v in fields.values()):
        clear()
        return
    try:
        path = _path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(fields))
    except OSError:
        pass  # a draft is a convenience; never fail the editor over it


def clear() -> None:
    try:
        _path().unlink()
    except OSError:
        pass
