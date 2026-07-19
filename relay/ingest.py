"""Byte-ingestion transports for attachments beyond inline base64.

Inline base64 (`add_attachment(data=…)`) is unusable from an MCP client for any
real file: tool-call arguments are model-generated tokens, so the agent would
have to *emit the whole base64 blob itself* — a ~550 KB image is ~750 K chars,
far past what's safely emittable. These two transports let real files reach the
vault without the bytes passing through the model context:

- ``fetch_url()`` — the server GETs a caller-supplied URL (SSRF-guarded on every
  hop including redirects, size-capped, streamed). Cheapest; works whenever the
  file already lives somewhere the server can reach.
- an upload-slot registry (``create_slot`` / ``stage_path`` / ``claim_slot``) —
  the caller POSTs for a short-lived slot, PUTs raw bytes out-of-band, then
  finalizes ``add_attachment(upload_id=…)``. Bytes never touch the tool call.

Both enforce the same ``attachment_max_bytes`` cap the inline path does.
"""
from __future__ import annotations

import ipaddress
import os
import secrets
import shutil
import socket
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import unquote, urlparse

import httpx

from .config import settings


class FetchError(Exception):
    """A source_url fetch failed (bad scheme, blocked host, network, or too large)."""


# ── source_url fetch ──────────────────────────────────────────────────────────


def _guard_host(host: str) -> None:
    """Reject URLs whose host resolves to a non-routable / metadata address.

    Callers are already authenticated (bearer / OAuth), so this isn't a full
    SSRF boundary — it's a guard against the classic pivots: the cloud metadata
    endpoint (169.254.169.254), loopback, and other reserved space. Private LAN
    ranges stay allowed on purpose so a homelab file server is fetchable. Runs on
    every hop (redirects included) via the httpx request hook.
    """
    if not host:
        raise FetchError("source_url has no host")
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as exc:
        raise FetchError(f"could not resolve source_url host: {host}") from exc
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_reserved or ip.is_unspecified:
            raise FetchError(f"source_url host {host} resolves to a blocked address ({ip})")


async def _guard_request(request: httpx.Request) -> None:
    _guard_host(request.url.host)


def _make_client(timeout: float) -> httpx.AsyncClient:
    """httpx client that SSRF-guards every request it makes, including redirects."""
    return httpx.AsyncClient(
        timeout=timeout,
        follow_redirects=True,
        event_hooks={"request": [_guard_request]},
    )


def _filename_from_response(url: str, resp: httpx.Response) -> str | None:
    """Best-effort attachment name: Content-Disposition filename, else URL basename."""
    cd = resp.headers.get("content-disposition", "")
    for part in cd.split(";"):
        part = part.strip()
        if part.lower().startswith("filename="):
            name = part[len("filename="):].strip().strip('"')
            if name:
                return unquote(name)
    tail = urlparse(url).path.rsplit("/", 1)[-1]
    return unquote(tail) or None


async def fetch_url(
    url: str, *, max_bytes: int, timeout: float | None = None
) -> tuple[bytes, str | None]:
    """GET ``url`` and return ``(bytes, suggested_filename)``.

    Streams and enforces ``max_bytes`` (both the declared Content-Length and the
    actual byte count). Raises ``FetchError`` on a bad scheme, a blocked host, a
    network/HTTP error, or an over-cap body.
    """
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise FetchError("source_url must be an http(s) URL")
    to = settings.attachment_fetch_timeout_seconds if timeout is None else timeout
    try:
        async with _make_client(to) as client:
            async with client.stream("GET", url) as resp:
                resp.raise_for_status()
                declared = resp.headers.get("content-length")
                if declared and declared.isdigit() and int(declared) > max_bytes:
                    raise FetchError(
                        f"source_url body exceeds the {settings.attachment_max_mb} MB limit"
                    )
                chunks: list[bytes] = []
                total = 0
                async for chunk in resp.aiter_bytes():
                    total += len(chunk)
                    if total > max_bytes:
                        raise FetchError(
                            f"source_url body exceeds the {settings.attachment_max_mb} MB limit"
                        )
                    chunks.append(chunk)
                name = _filename_from_response(url, resp)
    except FetchError:
        raise
    except httpx.HTTPStatusError as exc:
        raise FetchError(f"source_url returned {exc.response.status_code}") from exc
    except httpx.HTTPError as exc:
        raise FetchError(f"could not fetch source_url: {exc}") from exc
    data = b"".join(chunks)
    if not data:
        raise FetchError("source_url returned an empty body")
    return data, name


# ── presigned upload slots ────────────────────────────────────────────────────


@dataclass
class UploadSlot:
    id: str
    path: Path
    expires_at: float
    received_bytes: int = 0
    ready: bool = False


class UploadRegistry:
    """In-memory registry of presigned upload slots with disk-staged bytes.

    Single-process by design (relay runs one uvicorn worker); the staging dir is
    wiped at startup so an unclaimed slot never lingers. Slots and their files are
    single-use — ``claim_slot`` reads then deletes.
    """

    def __init__(self) -> None:
        self._slots: dict[str, UploadSlot] = {}

    def _dir(self) -> Path:
        d = Path(settings.uploads_dir)
        d.mkdir(parents=True, exist_ok=True)
        return d

    def reset(self) -> None:
        """Drop all slots and wipe staged files (called at startup)."""
        self._slots.clear()
        shutil.rmtree(settings.uploads_dir, ignore_errors=True)

    def purge_expired(self) -> int:
        """Discard expired slots (and their staged files). Returns the count.

        Called opportunistically on ``create_slot`` and periodically from the
        cleanup loop, so an unclaimed slot's bytes don't linger until restart.
        """
        now = time.time()
        stale = [s for s, slot in self._slots.items() if slot.expires_at <= now]
        for sid in stale:
            self._discard(sid)
        return len(stale)

    def _discard(self, upload_id: str) -> None:
        slot = self._slots.pop(upload_id, None)
        if slot is not None:
            try:
                slot.path.unlink()
            except OSError:
                pass

    def create_slot(self) -> UploadSlot:
        self.purge_expired()
        upload_id = secrets.token_urlsafe(24)
        slot = UploadSlot(
            id=upload_id,
            path=self._dir() / upload_id,
            expires_at=time.time() + settings.attachment_upload_ttl_seconds,
        )
        self._slots[upload_id] = slot
        return slot

    def _live_slot(self, upload_id: str) -> UploadSlot | None:
        slot = self._slots.get(upload_id)
        if slot is None:
            return None
        if slot.expires_at <= time.time():
            self._discard(upload_id)
            return None
        return slot

    def stage_path(self, upload_id: str) -> UploadSlot | None:
        """The live slot for a PUT, or ``None`` if unknown/expired."""
        return self._live_slot(upload_id)

    def mark_received(self, upload_id: str, size: int) -> None:
        slot = self._slots.get(upload_id)
        if slot is not None:
            slot.received_bytes = size
            slot.ready = True

    def claim_slot(self, upload_id: str) -> bytes | None:
        """Read a ready slot's staged bytes and consume it (single-use).

        Returns ``None`` when the id is unknown/expired or nothing was PUT yet.
        """
        slot = self._live_slot(upload_id)
        if slot is None or not slot.ready:
            return None
        try:
            data = slot.path.read_bytes()
        except OSError:
            self._discard(upload_id)
            return None
        self._discard(upload_id)
        return data

    def discard(self, upload_id: str) -> None:
        self._discard(upload_id)


registry = UploadRegistry()


async def stage_upload(upload_id: str, stream, *, max_bytes: int) -> int:
    """Stream a PUT body to the slot's staging file, enforcing ``max_bytes``.

    ``stream`` is an async byte iterator (Starlette ``request.stream()``). Returns
    the byte count. Raises ``FetchError`` on overflow (staging file cleaned up) or
    ``KeyError`` when the slot is unknown/expired.
    """
    slot = registry.stage_path(upload_id)
    if slot is None:
        raise KeyError(upload_id)
    total = 0
    tmp = slot.path.with_suffix(".part")
    try:
        with open(tmp, "wb") as fh:
            async for chunk in stream:
                total += len(chunk)
                if total > max_bytes:
                    raise FetchError(
                        f"upload exceeds the {settings.attachment_max_mb} MB limit"
                    )
                fh.write(chunk)
        os.replace(tmp, slot.path)
    except BaseException:
        # Only drop the partial temp file. os.replace is atomic, so slot.path is
        # never left half-written; leaving it intact protects a prior good upload
        # from being destroyed by a failed re-PUT to the same slot.
        try:
            tmp.unlink()
        except OSError:
            pass
        raise
    registry.mark_received(upload_id, total)
    return total
