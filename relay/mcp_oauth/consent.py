"""Per-client consent gate for the MCP OAuth broker leg (relay post #313, Stopgap).

``RelayOAuthProvider.authorize()`` no longer forwards a client it hasn't seen a
human approve straight to PocketID — PocketID skips its own consent screen for
a client the user has already approved once, and relay registers exactly one
static client with PocketID, so *every* relay-issued authorize call looks
pre-approved to PocketID regardless of whether a human has ever seen the
specific DCR client asking. This module is the actual gate: an unapproved
client is routed here first; only an explicit Approve continues on to
PocketID.

A cookie binds the browser that *viewed* the prompt (set on ``GET``) to the
one that *approves* it (checked on ``POST``) — this is the fix CVE-2026-27124
needed and didn't have: without it, an attacker who captures the consent URL
and gets the victim to open it is functionally unchanged from today, just
with one extra click. ``__Host-`` / ``Secure`` / ``HttpOnly`` /
``SameSite=Lax`` per Obsidian Security's pitfall list (see relay post #313 §2).

**Honest limit, not papered over**: this raises the bar from *zero-interaction,
zero-signal exploitation* to *requires an explicit, out-of-context approval
click a reasonably attentive user could refuse* — it cannot prove *which*
claude.ai account is asking, since DCR's ``client_name``/``redirect_uri`` are
self-reported and platform-generic (identical for a legitimate and a
malicious claude.ai connector alike). The protective value is that a
legitimate connection is a click the user *initiated* landing on an
*expected* prompt, while an attack is an *unexpected* prompt arriving out of
context — now visible instead of invisible.
"""
from __future__ import annotations

import html
import logging

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from mcp.server.auth.provider import construct_redirect_uri
from mcp.shared.auth import OAuthClientInformationFull
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response

from ..config import settings
from . import pocketid
from .store import get_store

logger = logging.getLogger(__name__)

_SALT = "relay-mcp-consent"
# Bound to the same 600s TTL `authorize()` saves the pending row with — a
# stale binding cookie should never outlive the pending auth it was minted
# for.
_TXN_TTL_SECONDS = 600


def _cookie_name() -> str:
    # __Host- requires Secure; fall back to a plain name when SECURE_COOKIES=false
    # (plain-HTTP dev/local setups), same accommodation relay's other cookies make.
    return "__Host-relay_mcp_consent" if settings.secure_cookies else "relay_mcp_consent"


def _serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(settings.session_signing_key, salt=_SALT)


def _sign(txn_id: str) -> str:
    return _serializer().dumps(txn_id)


def _cookie_binds(cookie_value: str, txn_id: str) -> bool:
    """Whether the presented cookie was minted (by this process, unexpired)
    for this exact ``txn_id`` — the browser that saw ``GET`` is the one
    submitting ``POST``."""
    try:
        signed_txn = _serializer().loads(cookie_value, max_age=_TXN_TTL_SECONDS)
    except (BadSignature, SignatureExpired):
        return False
    return signed_txn == txn_id


async def _client_name(client_id: str) -> str:
    """Best-effort display name for the consent prompt. Self-reported by the
    client at DCR time — the page says so, never implies it's verified."""
    info_json = await get_store().get_client(client_id)
    if info_json is None:
        return client_id
    try:
        info = OAuthClientInformationFull.model_validate_json(info_json)
    except Exception:  # noqa: BLE001 — malformed stored info shouldn't 500 the prompt
        return client_id
    return info.client_name or client_id


def _render_page(*, txn_id: str, client_name: str, redirect_uri: str) -> str:
    esc = html.escape
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Authorize relay access</title>
<style>
  body {{ font-family: system-ui, -apple-system, sans-serif; max-width: 30rem; margin: 4rem auto;
         padding: 0 1.5rem; color: #1a1a1a; background: #fafafa; }}
  .card {{ border: 1px solid #ddd; border-radius: 12px; padding: 1.5rem; background: #fff; }}
  h1 {{ font-size: 1.15rem; margin: 0 0 1rem; }}
  .field {{ margin: 0.9rem 0; }}
  .field b {{ display: block; font-size: 0.72rem; color: #777; text-transform: uppercase;
             letter-spacing: 0.04em; margin-bottom: 0.15rem; }}
  .field span {{ word-break: break-all; }}
  .warn {{ font-size: 0.85rem; color: #7a4a00; background: #fff6e5; border: 1px solid #f0dca3;
          border-radius: 8px; padding: 0.75rem 1rem; margin-top: 1.25rem; line-height: 1.4; }}
  .actions {{ display: flex; gap: 0.75rem; margin-top: 1.5rem; }}
  button {{ font: inherit; font-size: 0.95rem; padding: 0.6rem 1.2rem; border-radius: 8px;
           border: 1px solid #ccc; background: #fff; cursor: pointer; }}
  button.approve {{ background: #1a1a1a; color: #fff; border-color: #1a1a1a; }}
</style>
</head>
<body>
  <div class="card">
    <h1>An application wants access to your relay vault</h1>
    <div class="field"><b>Application (self-reported)</b><span>{esc(client_name)}</span></div>
    <div class="field"><b>Will receive the authorization code at</b><span>{esc(redirect_uri)}</span></div>
    <div class="warn">
      This name is reported by the application itself and is not verified by relay — a
      malicious application can claim any name. Only approve this if you just started a
      connection yourself (e.g. via claude.ai's own "Connect" flow) and this prompt was
      expected. If this appeared unexpectedly — from a chat message, email, or any link you
      didn't just click to connect — choose Deny.
    </div>
    <form method="post" action="/mcp/oauth/consent" class="actions">
      <input type="hidden" name="txn_id" value="{esc(txn_id)}">
      <button type="submit" name="action" value="deny">Deny</button>
      <button type="submit" name="action" value="approve" class="approve">Approve</button>
    </form>
  </div>
</body>
</html>"""


def _plain_error(message: str, status_code: int) -> HTMLResponse:
    return HTMLResponse(f"<p>{html.escape(message)}</p>", status_code=status_code)


async def handle_consent_get(request: Request) -> Response:
    txn_id = request.query_params.get("txn_id", "")
    if not txn_id:
        return _plain_error("Missing authorization request.", 400)

    pending = await get_store().get_pending(txn_id)
    if pending is None:
        return _plain_error(
            "This authorization request has expired or was already used. Please retry from the application.",
            400,
        )

    name = await _client_name(pending.client_id)
    resp = HTMLResponse(_render_page(txn_id=txn_id, client_name=name, redirect_uri=pending.redirect_uri))
    resp.set_cookie(
        key=_cookie_name(),
        value=_sign(txn_id),
        httponly=True,
        samesite="lax",
        secure=settings.secure_cookies,
        path="/",
        max_age=_TXN_TTL_SECONDS,
    )
    return resp


async def handle_consent_post(request: Request) -> Response:
    form = await request.form()
    txn_id = str(form.get("txn_id", ""))
    action = str(form.get("action", ""))
    cookie_value = request.cookies.get(_cookie_name())

    if not txn_id or not cookie_value or not _cookie_binds(cookie_value, txn_id):
        logger.warning("MCP OAuth consent: rejected (missing/mismatched binding cookie) for txn=%s", txn_id)
        return _plain_error(
            "This approval could not be verified in this browser. Please retry the connection from the "
            "application (open the consent link in the same browser you started the connection in).",
            403,
        )

    store = get_store()
    pending = await store.get_pending(txn_id)
    if pending is None:
        return _plain_error("This authorization request has expired or was already used.", 400)

    if action != "approve":
        await store.pop_pending(txn_id)
        url = construct_redirect_uri(pending.redirect_uri, error="access_denied", state=pending.client_state)
        return RedirectResponse(url, status_code=302)

    # Approval persists (oauth.db, not memory) — this specific client skips the
    # gate on every future authorize() call, exactly like PocketID's own
    # skip-consent behaves for a client PocketID has already seen approved.
    await store.approve_client(pending.client_id)
    url = await pocketid.build_authorize_url(txn_id, pending.up_verifier, pending.up_nonce)
    return RedirectResponse(url, status_code=302)
