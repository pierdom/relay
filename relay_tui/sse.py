from __future__ import annotations

import json
import threading
import time
from typing import Callable

import requests

from relay.config import settings


class SSESubscriber:
    """Background thread that subscribes to the relay SSE event stream.

    Calls *on_post* with the parsed JSON dict for each ``post`` event.
    Calls *on_delete* with the deleted post id for each ``delete`` event.
    Calls *on_connect* (no args) when the HTTP connection is established.
    Calls *on_disconnect* (no args) when the connection is lost or fails.

    Reconnects automatically with exponential back-off (max 30 s).
    """

    def __init__(
        self,
        on_post: Callable[[dict], None],
        on_connect: Callable[[], None] | None = None,
        on_disconnect: Callable[[], None] | None = None,
        on_delete: Callable[[int], None] | None = None,
    ) -> None:
        self._on_post = on_post
        self._on_delete = on_delete
        self._on_connect = on_connect
        self._on_disconnect = on_disconnect
        self._stop_event = threading.Event()
        self._last_id: int | None = None
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None

    # ── Public interface ──────────────────────────────────────────────────────

    def start(self) -> None:
        """Start the background daemon thread."""
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Signal the background thread to stop."""
        self._stop_event.set()

    def set_last_id(self, id: int | None) -> None:
        """Update the last received event ID for reconnect replay."""
        with self._lock:
            self._last_id = id

    # ── Internal ──────────────────────────────────────────────────────────────

    def _run(self) -> None:
        delay = 1.0
        while not self._stop_event.is_set():
            try:
                self._connect()
                delay = 1.0  # reset back-off on clean exit
            except Exception:
                if self._on_disconnect:
                    try:
                        self._on_disconnect()
                    except Exception:
                        pass
                if self._stop_event.wait(min(delay, 30.0)):
                    break
                delay = min(delay * 2, 30.0)

    def _connect(self) -> None:
        base_url = settings.relay_base_url.rstrip("/")
        url = f"{base_url}/events"
        headers: dict[str, str] = {
            "Authorization": f"Bearer {settings.api_key}",
            "Accept": "text/event-stream",
            "Cache-Control": "no-cache",
        }
        with self._lock:
            last_id = self._last_id
        if last_id is not None:
            headers["Last-Event-ID"] = str(last_id)

        with requests.get(url, headers=headers, stream=True, timeout=(10, None)) as resp:
            resp.raise_for_status()

            # Connection established
            if self._on_connect:
                try:
                    self._on_connect()
                except Exception:
                    pass

            # Parse SSE stream
            event_type: str = ""
            data_lines: list[str] = []
            event_id: str | None = None

            for raw_line in resp.iter_lines(decode_unicode=True):
                if self._stop_event.is_set():
                    return

                line: str = raw_line if raw_line is not None else ""

                if line == "":
                    # Blank line: dispatch accumulated event
                    if data_lines and event_type in ("post", "delete"):
                        raw_data = "\n".join(data_lines)
                        try:
                            parsed = json.loads(raw_data)
                            if event_type == "post":
                                self._on_post(parsed)
                            elif self._on_delete is not None:
                                self._on_delete(parsed["id"])
                        except Exception:
                            pass
                    if event_id is not None:
                        try:
                            with self._lock:
                                self._last_id = int(event_id)
                        except ValueError:
                            pass
                    # Reset accumulators
                    event_type = ""
                    data_lines = []
                    event_id = None
                elif line.startswith("event:"):
                    event_type = line[len("event:"):].strip()
                elif line.startswith("data:"):
                    data_lines.append(line[len("data:"):].strip())
                elif line.startswith("id:"):
                    event_id = line[len("id:"):].strip()
                # Ignore comment lines (starting with ':')
