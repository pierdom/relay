"""In-process MCP server exposed over Streamable HTTP at ``/mcp``.

Unlike the stdio proxy in ``relay_mcp/server.py`` (which runs on the client
machine and talks to a relay over REST), this server runs *inside* the relay
process and calls the shared ``relay.service`` layer directly — no network
hop, no schema duplication. Any MCP client that supports the Streamable HTTP
transport can connect remotely with the relay's bearer key.
"""
from __future__ import annotations

import base64
import json
import logging
import re
from contextvars import ContextVar
from pathlib import Path

import httpx
from fastmcp import FastMCP
from fastmcp.server.auth.auth import AccessToken, AuthProvider, MultiAuth, TokenVerifier
from fastmcp.server.auth.oauth_proxy.models import UpstreamTokenSet
from fastmcp.server.auth.oidc_proxy import OIDCProxy
from fastmcp.server.dependencies import get_access_token
from fastmcp.utilities.types import Image
from joserfc import jwt as joserfc_jwt
from joserfc.errors import ExpiredTokenError
from joserfc.jwk import KeySet
from key_value.aio.stores.filetree import (
    FileTreeStore,
    FileTreeV1CollectionSanitizationStrategy,
    FileTreeV1KeySanitizationStrategy,
)
from key_value.aio.wrappers.encryption import FernetEncryptionWrapper
from mcp.server.auth.provider import RefreshToken, TokenError
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from mcp.types import Icon
from pydantic import ValidationError

from . import __version__, changes, database, lint, metrics, service, status, vault
from .config import settings
from .identity import Actor, resolve_bearer
from .models import AttachmentCreate, ChangeEntry, ChangeListResponse, PostCreate, PostUpdate, TagConfigCreate
from .routes.auth import _authorized


def _first_error(exc: ValidationError) -> str:
    """Flatten a pydantic ValidationError to its first human-readable message."""
    errors = exc.errors()
    if errors:
        msg = errors[0].get("msg", "invalid input")
        return msg.removeprefix("Value error, ")
    return "invalid input"

INSTRUCTIONS = (
    "Relay is a personal knowledge base kept as a plain-Markdown vault; posts are files "
    "a human also edits directly in Obsidian, so write them to be read by a person. "
    "Clients subscribe to changes in real time. Before writing, read the master document "
    "with get_post(id=0) — it holds the index, tag taxonomy, naming conventions, and "
    "house rules. Keep one canonical post per topic and update it in place rather than "
    "creating duplicates. list_folders shows how the vault is already organised."
)


_oidc_metadata_cache: dict | None = None
_oidc_jwks_cache: KeySet | None = None


async def _load_pocketid_jwks() -> KeySet:
    """Fetch and cache PocketID's OIDC discovery metadata + JWKS, for verifying
    `OIDCProxy`'s upstream id_tokens in `_RelayOIDCProxy` below (relay #313 Phase 2).

    A new, independent implementation rather than reusing `mcp_oauth/pocketid.py`'s
    near-identical logic — that whole module is scheduled for deletion in Phase 4,
    and importing from code about to be deleted would just create a dependency
    Phase 4 then has to unwind. Same trust model/caching tradeoff as that module:
    cached for the process lifetime, not refetched on a `kid` miss — PocketID
    rotating its signing keys is rare enough that a relay restart to pick up new
    keys is an acceptable cost on this single-user deployment, and not refetching
    avoids letting an attacker force repeated JWKS fetches with bogus `kid`s.

    A second, independent discovery+JWKS fetch from the one `OIDCProxy` itself
    already makes at construction for its own token verifier — wasteful but
    correctness-safe; reaching into `OIDCProxy`'s internal cache instead would be
    relying on an unstable private API for a minor efficiency gain."""
    global _oidc_metadata_cache, _oidc_jwks_cache
    if _oidc_jwks_cache is None:
        if _oidc_metadata_cache is None:
            url = f"{settings.oidc_issuer.rstrip('/')}/.well-known/openid-configuration"
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(url)
                resp.raise_for_status()
                _oidc_metadata_cache = resp.json()
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(_oidc_metadata_cache["jwks_uri"])
            resp.raise_for_status()
            _oidc_jwks_cache = KeySet.import_key_set(resp.json())
    return _oidc_jwks_cache


def _unverified_exp(token: str) -> float | None:
    """Read a JWT's `exp` without verifying anything. Never a security decision —
    see `_RelayOIDCProxy._get_verification_token`, its only caller, for why that
    is safe there (the value can only schedule a refresh *earlier*)."""
    try:
        payload = token.split(".")[1]
        decoded = base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4))
        return float(json.loads(decoded)["exp"])
    except Exception:
        return None


# Set only while `exchange_refresh_token` is on the stack, read by
# `_extract_upstream_claims` to tell the two grant types apart — see
# `_RelayOIDCProxy` for why that distinction is load-bearing. A ContextVar rather
# than an attribute because one proxy instance serves every concurrent request:
# an instance flag would leak one caller's grant type into another's validation.
_refreshing_upstream: ContextVar[bool] = ContextVar("relay_mcp_refreshing_upstream", default=False)


class _RelayOIDCProxy(OIDCProxy):
    """Enforces relay's own `OIDC_ALLOWED_SUBS`/`OIDC_ALLOWED_EMAILS` allowlist on
    top of `OIDCProxy` — not a stock feature (relay #313 Phase 2 plan flagged this
    explicitly). `_extract_upstream_claims` is fastmcp's own documented override
    point for inspecting the upstream token response; raising here aborts
    `exchange_authorization_code`/`exchange_refresh_token` before any FastMCP
    access token is issued (confirmed by reading both call sites: the raise lands
    before `self.jwt_issuer.issue_access_token(...)` in each), so a real PocketID
    login by a non-allowlisted human still can't obtain a working `/mcp`
    credential. Runs on every refresh too, not just initial login — a stronger
    guarantee than the current hand-rolled `mcp_oauth/broker.py`, which only
    checks once at login and never revisits the allowlist on refresh.

    Independently re-verifies the id_token's signature against PocketID's own
    JWKS rather than trusting `idp_tokens`'s contents unverified — the same rigor
    `mcp_oauth/pocketid.py` already applies today, reimplemented here rather than
    imported from it since that whole module is deleted in Phase 4. Deliberately
    does not check `nonce` (unlike `pocketid.py`'s hand-rolled upstream leg) —
    `OIDCProxy` owns the upstream `/authorize` request end to end and doesn't
    expose a way to thread a per-transaction nonce through to this hook;
    `iss`/`aud`/`exp` are the essential claims for authenticity of a token
    obtained via a fresh, PKCE-bound, server-to-server code exchange. Flagged
    here for Phase 5 to scrutinize explicitly, not silently asserted as
    equivalent."""

    def _get_verification_token(self, upstream_token_set: UpstreamTokenSet) -> str | None:
        """Align the upstream token set's expiry with the id_token's own `exp`.

        `OAuthProxy.load_access_token` runs on *every* `/mcp` request and has two
        separate notions of upstream expiry that `verify_id_token=True` pulls
        apart:

        * what it **validates** — `_get_verification_token`, which for us is the
          **id_token**, rejected by the verifier once its own `exp` passes;
        * when it decides to **refresh** — `upstream_token_set.expires_at`, which
          comes from the token response's `expires_in`, i.e. the **access
          token's** lifetime.

        Nothing keeps those two in agreement. Whenever PocketID's id_token is
        shorter-lived than its access token, every request in the gap between the
        two fails validation while `needs_refresh` is still False, so no
        transparent refresh is attempted and `/mcp` answers 401 with a perfectly
        healthy session underneath. Reproduced end to end against a mock IdP
        (3s id_token, 3600s access token): `/mcp` returned 200, then 401 six
        seconds later, with an hour left on relay's own access token.

        Clamping `expires_at` down to the id_token's `exp` makes the refresh
        trigger track the token actually being validated, so the refresh fires
        *before* validation can fail. Combined with the non-zero
        `token_expiry_threshold_seconds` in `_build_auth`, the rotation happens
        ahead of expiry rather than during an outage.

        The `exp` is read **without signature verification**, deliberately. It is
        not trusted for any authorization decision — the same id_token is fully
        verified moments later by the token verifier, and again by
        `_extract_upstream_claims` on the refresh. This value only ever moves the
        refresh *earlier* (it is applied solely when it is lower than the
        recorded expiry), so a forged or corrupt `exp` can at worst cause a
        redundant refresh; it can never extend a session.
        """
        token = super()._get_verification_token(upstream_token_set)
        if token is not None:
            exp = _unverified_exp(token)
            if exp is not None and exp < upstream_token_set.expires_at:
                upstream_token_set.expires_at = exp
        return token

    async def exchange_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: RefreshToken,
        scopes: list[str],
    ) -> OAuthToken:
        """Mark the refresh grant so `_extract_upstream_claims` can relax `exp`.

        Overridden purely to set the flag; the exchange itself is entirely
        `OAuthProxy`'s. See `_extract_upstream_claims` for the reasoning.
        """
        marker = _refreshing_upstream.set(True)
        try:
            return await super().exchange_refresh_token(client, refresh_token, scopes)
        finally:
            _refreshing_upstream.reset(marker)

    def _prepare_scopes_for_token_exchange(self, scopes: list[str]) -> list[str]:
        """Never forward the downstream `scope` to PocketID's own `/token`.

        Found live, not read for (relay #313, first production connector test):
        a real claude.ai connection requests `scope=relay` from `/authorize`
        (relay's own MCP scope, since that's what `settings.mcp_scopes` reports
        via `/.well-known/oauth-authorization-server`) — fine for the FastMCP
        token this proxy issues *to* claude.ai, meaningless to PocketID, which
        only ever advertised `openid profile email groups offline_access`
        (confirmed against its live discovery document). The base
        implementation echoes `transaction["scopes"]` straight through as the
        exchange's own `scope` param; empty means "omit it", which is the
        correct default per RFC 6749 §4.1.3 since the code already carries
        whatever PocketID granted at `/authorize`.
        """
        return []

    def _prepare_scopes_for_upstream_refresh(self, scopes: list[str]) -> list[str]:
        """Same reasoning as `_prepare_scopes_for_token_exchange`, for the
        refresh grant: the stored `RefreshToken.scopes` is relay's own
        `["relay"]`, never PocketID's. Omitting `scope` on refresh is RFC 6749
        §6-legal (treated as identical to the scope originally granted)."""
        return []

    def _translate_scopes_from_idp(self, scopes: list[str]) -> list[str]:
        """Substitute relay's own scope for whatever PocketID echoed back.

        Found live, immediately after the scope/resource fix above: fixing the
        *upstream* leg surfaced a second-order bug on the *downstream* one.
        `OAuthProxy.exchange_authorization_code` (and the refresh path) reads
        `idp_tokens["scope"]` — per RFC 6749 §5.1, the IdP MUST echo the scope
        it actually granted — and, unless translated here, embeds *that
        upstream string* as the FastMCP JWT's own scope claim. PocketID
        dutifully echoes back `openid profile email offline_access` (the
        vocabulary `extra_authorize_params` above asks it for); with no
        translation that becomes the token `claude.ai` receives, which then
        fails relay's own `required_scopes=["relay"]` check on every `/mcp`
        call with `insufficient_scope` (confirmed live: `POST /token` 200,
        immediately followed by `POST /mcp` 403). PocketID's scope vocabulary
        and relay's MCP scope are disjoint by design — relay's `required_scopes`
        is the only value that was ever meaningful here, matching what
        `_get_verification_token`'s docstring already documents about
        `verify_id_token=True` decoupling upstream identity from relay's own
        authorization.
        """
        _ = scopes
        return list(settings.mcp_scopes)

    async def _extract_upstream_claims(self, idp_tokens: dict) -> dict | None:
        id_token = idp_tokens.get("id_token")
        if not id_token:
            raise TokenError("invalid_grant", "PocketID token response had no id_token")

        # Everything from here to the allowlist check is converted into a clean
        # OAuth error rather than allowed to escape (relay #313 Phase 5). It
        # already failed *closed* — an escaping exception means no token — but it
        # surfaced as a bare 500 from `/token`, an unauthenticated endpoint: a
        # stale JWKS after an IdP key rotation, or PocketID being briefly
        # unreachable, would turn every login attempt into an unhandled-exception
        # log entry instead of a diagnosable `invalid_grant`. `TokenError` is
        # re-raised untouched so the allowlist denial below keeps its own
        # `unauthorized_client` code, and the unexpected case is logged with a
        # traceback so converting it here doesn't cost us the diagnosis.
        try:
            jwks = await _load_pocketid_jwks()
            decoded = joserfc_jwt.decode(id_token, jwks)
            assert _oidc_metadata_cache is not None  # _load_pocketid_jwks populates this first
            # `exp` is marked essential deliberately. joserfc validates a claim
            # only when it is *present*, so without this an id_token carrying no
            # `exp` at all would satisfy the registry and be accepted forever.
            claim_options = {
                "iss": {"essential": True, "value": _oidc_metadata_cache["issuer"]},
                "aud": {"essential": True, "value": settings.oidc_client_id},
                "exp": {"essential": True},
            }
            try:
                # enforces exp/nbf/iat + iss/aud
                joserfc_jwt.JWTClaimsRegistry(**claim_options).validate(decoded.claims)
            except ExpiredTokenError:
                # On the *login* path an expired id_token is simply invalid.
                if not _refreshing_upstream.get():
                    raise
                # On the *refresh* path it usually isn't the same token at all.
                # OIDC Core §12.2 makes `id_token` OPTIONAL in a refresh response,
                # and `OAuthProxy.exchange_refresh_token` merges that response over
                # the stored login response rather than replacing it — so when the
                # IdP omits one, what arrives here is the *original login*
                # id_token, re-presented. It is genuinely old by design, and once
                # past its (typically minutes-long) lifetime every refresh would
                # fail `invalid_grant` forever: a connector that worked at login
                # would break an hour later and need a fresh manual login, over and
                # over. Reproduced end to end against a mock IdP before this fix.
                #
                # Relaxing `exp` *here specifically* costs nothing: on this path the
                # id_token is not what authenticates the request — the caller already
                # presented a valid FastMCP refresh token, and the upstream refresh
                # that just succeeded is PocketID's own live attestation that the
                # session is still good (a disabled or revoked user fails that
                # exchange and never reaches this line). The id_token is read only to
                # learn *whose* session it is, for the allowlist check below, and
                # signature/`iss`/`aud`/`sub` — which is all that question depends on
                # — stay fully verified. `exp` is dropped from both the options and
                # the claims so "essential" doesn't then trip on its absence.
                # Logged at WARNING, not DEBUG, because it is the one observable
                # signal for an upstream that never reissues `id_token`. In that
                # case this refresh succeeds and mints a working-looking relay
                # token that every `/mcp` request then rejects with 401, because
                # `load_access_token` re-verifies this same stale id_token per
                # request and nothing can ever replace it. Without this line that
                # presents as an unexplained 401 loop; with it, the cause is named.
                logging.getLogger(__name__).warning(
                    "MCP OAuth: the IdP did not reissue an id_token on the refresh grant, so the "
                    "original login id_token is being re-presented and has expired. Identity is "
                    "still verified (signature/iss/aud/sub), but /mcp requests will 401 until the "
                    "next interactive login — see _get_verification_token in this module."
                )
                del claim_options["exp"]
                joserfc_jwt.JWTClaimsRegistry(**claim_options).validate(
                    {k: v for k, v in decoded.claims.items() if k != "exp"}
                )
        except TokenError:
            raise
        except Exception:
            logging.getLogger(__name__).exception(
                "MCP OAuth: could not verify the upstream id_token (JWKS unreachable, "
                "signing key rotated since startup, or claims invalid)"
            )
            raise TokenError("invalid_grant", "Upstream id_token could not be verified") from None

        claims = decoded.claims
        sub = claims.get("sub") or ""
        email = (claims.get("email") or "").lower()
        email_verified = claims.get("email_verified") is True
        if not sub or not _authorized(sub, email, email_verified):
            logging.getLogger(__name__).warning(
                "MCP OAuth: login denied for sub=%s email=%s (not in allowlist)", sub, email
            )
            raise TokenError("unauthorized_client", "This identity is not authorized for this relay")
        return {"sub": sub, "email": email}


class _StaticBearerAuth(TokenVerifier):
    """Static pre-shared bearer key, wired through fastmcp's own `auth=` slot rather
    than a hand-rolled ASGI wrapper. With MCP OAuth active this is one of
    `MultiAuth`'s `verifiers` (see `_build_auth` below) — a pure fallback with no
    routes of its own, tried after `_RelayOIDCProxy`. Subclasses `TokenVerifier`
    specifically (not the bare `AuthProvider` etf-scout-mcp's own reference
    `_StaticBearerAuth` uses) —
    fastmcp's docstring for it is explicit that token verifiers "typically don't
    provide authentication routes by default", which is exactly the property this
    needs and is more precise than relying on an unstated default. Deliberately not
    fastmcp's own `DebugTokenVerifier` (`fastmcp.server.auth.providers.debug`) — same
    shape, but its name and docstring ("bypasses standard security checks... only use
    in controlled environments") is the wrong signal to leave sitting in relay's
    actual production auth path. Delegates comparison to `identity.resolve_bearer`
    (relay #198, B-7) — the same constant-time, multi-key-aware resolver
    `auth.require_api_key` uses — rather than a bare `==`, and its return
    identifies *which* configured key matched, not just whether one did."""

    async def verify_token(self, token: str) -> AccessToken | None:
        actor = resolve_bearer(token)
        if actor is not None:
            # Caught live (relay #313 Phase 2): with MCP OAuth active, MultiAuth
            # enforces `required_scopes` against every verifier's result, not just
            # the OAuth server's — `scopes=[]` here made the static-bearer path
            # 403 "insufficient_scope" on every single call, since it could never
            # satisfy `settings.mcp_scopes`. The static key represents full,
            # unscoped relay access (same as it always has); reporting relay's
            # actual configured scopes is what lets it clear that check.
            #
            # `subject=actor.name` (relay #198, B-7): which configured key
            # matched — "apikey" for the primary key, or a RELAY_API_KEYS
            # name — read back by `_current_actor()` for git/changes/
            # updated_by provenance. `client_id` stays the fixed "relay" in
            # case anything else depends on its current constant value.
            return AccessToken(token=token, client_id="relay", scopes=settings.mcp_scopes, subject=actor.name)
        return None


def _brand_icons() -> list[Icon]:
    """Advertise relay's logo in the initialize serverInfo (MCP SEP-973).

    Clients that read `serverInfo.icons` (spec 2025-11-25) show these instead of
    the generic globe. `/assets` is public (no auth), so the src URLs resolve for
    an unauthenticated fetch. Claude's remote connectors don't render this yet
    (anthropics/claude-ai-mcp#152) but other clients already do, and it's the
    spec-correct place for it — so it lights up automatically when Claude ships.
    """
    base = settings.relay_base_url.rstrip("/")
    return [
        Icon(src=f"{base}/assets/relay-mark.svg", mimeType="image/svg+xml"),
        Icon(src=f"{base}/assets/relay-mark-512.png", mimeType="image/png", sizes=["512x512"]),
    ]


def _oauth_client_storage() -> FernetEncryptionWrapper:
    """Encrypted, JSON-file-backed storage for OAuth state, rooted in the vault.

    Both halves of this were audit findings (relay #313 Phase 5), and both come
    from the same root cause: **passing `client_storage` at all opts out of what
    `OIDCProxy` does for you.** Left `None`, fastmcp builds a
    `FernetEncryptionWrapper(FileTreeStore(...))` under `~/.fastmcp/`; Phase 2
    passed a bare store to move it onto the vault volume, and silently dropped
    the encryption with it. `proxy.py` still logs "Stored encrypted upstream
    tokens" either way, which is how it stayed unnoticed. This function moves the
    storage *and* keeps everything the default gave us.

    **Encryption.** The proxy persists the *upstream* PocketID access and refresh
    tokens (it re-validates them on every request and refreshes them
    transparently). Without the wrapper those sit in cleartext on disk —
    confirmed by reading a stored record during the audit. To be precise about
    the blast radius, since an earlier draft of this comment overstated it:
    `.relay/` is *excluded* from Syncthing (see `settings.history_dir`), so these
    files do **not** replicate to every synced device. What remains is real
    enough on its own — cleartext, long-lived IdP credentials sitting in the
    same directory tree as the private notes, inside whatever backs that volume
    up, readable by anything that can read a file in the vault. The
    implementation this migration replaced never had this exposure at all: it
    stored only relay's *own* tokens, hashed, and never persisted an upstream
    token.
    Key derivation mirrors fastmcp's own (PBKDF2 over a high-entropy secret with
    a fixed salt), and `raise_on_decryption_error=False` matches its default
    too: rotating `OIDC_CLIENT_SECRET` then reads as a cache miss and clients
    re-authenticate, instead of every request hard-failing on undecryptable state.

    **FileTreeStore, not DiskStore.** `DiskStore` wraps `diskcache`, which
    `pip-audit` flags as PYSEC-2026-2447 — it serializes with **pickle** through
    5.6.3, with no fixed release, so write access to that directory is arbitrary
    code execution in this process on the next read. For a directory inside a
    hand-edited vault that precondition is not far-fetched. Switching the extra
    to `py-key-value-aio[filetree]` drops `diskcache` from the dependency tree
    rather than merely not calling it. It also stops putting a SQLite database
    inside the vault — the corruption footgun this repo already designed
    `history.git` around — in favour of per-key JSON.
    """
    # Both sanitization strategies `os.pathconf()` the directory to size their
    # name/path limits, so it has to exist before they are constructed — the
    # store's own `auto_create` runs too late for that.
    directory = Path(settings.mcp_oauth_storage_dir)
    directory.mkdir(parents=True, exist_ok=True)
    store = FileTreeStore(
        data_directory=directory,
        # Passed explicitly, exactly as fastmcp does for its own default store:
        # these bound key/collection names to the filesystem's real limits. That
        # matters more here than it looks, because a CIMD client_id is a *URL*
        # and becomes a storage key — this is what keeps such a key from
        # escaping the directory or blowing past NAME_MAX.
        key_sanitization_strategy=FileTreeV1KeySanitizationStrategy(directory),
        collection_sanitization_strategy=FileTreeV1CollectionSanitizationStrategy(directory),
    )
    return FernetEncryptionWrapper(
        key_value=store,
        source_material=settings.oidc_client_secret,
        salt="relay-mcp-oauth-storage",
        raise_on_decryption_error=False,
    )


def _build_auth() -> AuthProvider:
    """Static bearer only when MCP OAuth isn't configured (unchanged fallback);
    `MultiAuth(server=_RelayOIDCProxy(...), verifiers=[_StaticBearerAuth()])` when
    it is active (relay #313 Phase 2). `server` owns the OAuth routes/metadata and
    is tried first for token verification; `verifiers` are pure fallbacks with no
    routes of their own. Mirrors etf-scout-mcp's `_make_auth()` precedence (OIDC >
    static bearer), composed rather than either/or since relay needs both at
    once — Claude's remote connector uses OAuth, the stdio bridge and other
    machine clients use the static key.

    Constructing `_RelayOIDCProxy` makes a real (bounded, ~10s-timeout) network
    call to PocketID's OIDC discovery endpoint — fastmcp's own documented
    tradeoff so a slow/unreachable issuer can't hang startup indefinitely, but a
    behavior change from the old `mcp_oauth/pocketid.py`, which fetched lazily on
    first request rather than at startup. Worth Phase 5/6 attention, not silently
    carried over.

    `client_storage` is pinned under `<vault>/.relay/mcp_oauth/` rather than
    fastmcp's own default (`~/.fastmcp/oauth-proxy/`, outside the vault volume
    entirely) so OAuth client registrations and encrypted tokens ride the same
    Docker volume and durability guarantee `oauth.db` has today — the Phase 2
    checklist item this closes. `redirect_path` is pinned to the exact path
    already registered on PocketID's own client config
    (`<RELAY_BASE_URL>/mcp/oauth/callback`, documented in CLAUDE.md) — fastmcp's
    own default (`/auth/callback`) would collide with relay's *existing* web-UI
    OIDC callback route at that same path.

    `resource_base_url` is deliberately the bare origin (`settings.relay_base_url`),
    not `settings.mcp_resource_url` (`<base>/mcp`) — caught live: fastmcp's
    `set_mcp_path("/mcp")` (called automatically from `http_app(path="/mcp")`'s own
    wiring) *appends* the mount path to `resource_base_url` itself to derive the
    real resource URL, so passing an already-`/mcp`-suffixed value produced
    `.../mcp/mcp` in the served OAuth metadata and `WWW-Authenticate` header.
    """
    if not settings.mcp_oauth_active:
        return _StaticBearerAuth()

    oidc = _RelayOIDCProxy(
        config_url=f"{settings.oidc_issuer.rstrip('/')}/.well-known/openid-configuration",
        client_id=settings.oidc_client_id,
        client_secret=settings.oidc_client_secret,
        base_url=settings.relay_base_url,
        redirect_path="/mcp/oauth/callback",
        required_scopes=settings.mcp_scopes,
        allowed_client_redirect_uris=settings.mcp_allowed_client_redirect_uri_patterns,
        client_storage=_oauth_client_storage(),
        # Load-bearing, and the single most important line in this call — without
        # it, a *completed* OAuth login mints a token that 401s on every
        # subsequent request (relay #313 Phase 5, caught by running the real flow;
        # no amount of reading would have shown it).
        #
        # `load_access_token` implements a token *swap*: it verifies relay's own
        # JWT, then re-validates the stored upstream token on every request via
        # `OIDCProxy`'s `token_verifier`. Which upstream token that is, and what
        # it's checked against, is decided at construction (`oidc_proxy.py`):
        #     verifier_audience = client_id if verify_id_token else audience
        #     verifier_scopes   = None      if verify_id_token else required_scopes
        # Left False (the default), relay passes no `audience` — so the verifier
        # audience is None — and hands it `required_scopes=["relay"]`, which means
        # it demands PocketID's *own access token* be a JWT carrying a `relay`
        # scope. `relay` is this server's MCP scope; PocketID has never heard of
        # it and will never mint it, and PocketID's access token isn't an RP's to
        # validate in the first place.
        #
        # True verifies the **id_token** instead: always a JWT, always verifiable
        # against the IdP's JWKS, `aud` == our client_id per OIDC Core §2. fastmcp
        # then restores relay's own `required_scopes` at the FastMCP-token level
        # (see `OIDCProxy.__init__`), so `relay` is still enforced — on relay's
        # token, which is the only place it was ever meaningful. Verified both
        # ways against a mock IdP: opaque *and* JWT upstream access tokens now
        # both authenticate end to end, so PocketID's token format stops mattering.
        verify_id_token=True,
        # Rotate the upstream token *before* it expires rather than after. With
        # the default 0, `load_access_token` only refreshes once the recorded
        # expiry has already passed — so the request that discovers the expiry is
        # the one that pays for it, and any request racing the rotation 401s.
        # Since `_get_verification_token` above clamps that expiry to the
        # id_token's own `exp`, this threshold is measured against the token
        # actually being validated: at 120s, PocketID is asked for a fresh
        # id_token two minutes before the current one stops verifying. Small
        # enough to stay well inside any plausible IdP token lifetime, large
        # enough to cover a slow upstream round trip.
        token_expiry_threshold_seconds=120,
        # Both found live against real PocketID, not the mock IdP Phase 5 used —
        # the mock tolerated a scope/resource it had never heard of; PocketID
        # rejects `/authorize` outright with `invalid_request` naming exactly
        # these two params. `_prepare_scopes_for_token_exchange`/
        # `_prepare_scopes_for_upstream_refresh` above cover the token-exchange
        # and refresh legs; this pair covers the one upstream leg with no
        # override hook at all — `_build_upstream_authorize_url` builds
        # `scope` from `transaction["scopes"]` (relay's own `["relay"]`)
        # unconditionally.
        #
        # `forward_resource=False`: PocketID's discovery document advertises no
        # RFC 8707 support, and the default (`True`) forwards claude.ai's
        # `resource=https://relay.herrdr.net/mcp` straight through.
        #
        # `extra_authorize_params={"scope": ...}`: applied last via a plain
        # dict.update() over the already-built query params (confirmed by
        # reading `_build_upstream_authorize_url`), so this is the one
        # supported way to override — not add to — the `scope` PocketID
        # actually receives. `offline_access` (in PocketID's advertised scope
        # list, absent from the web-UI login's own `openid email profile` in
        # `routes/auth.py`) is required here and not there: the web UI re-authenticates
        # via its own session cookie, but this proxy must be able to silently
        # refresh PocketID's token to keep an MCP session alive, which needs a
        # PocketID refresh_token in the first place.
        forward_resource=False,
        extra_authorize_params={"scope": "openid profile email offline_access"},
    )
    return MultiAuth(server=oidc, verifiers=[_StaticBearerAuth()])


mcp = FastMCP(
    "relay",
    # serverInfo.version, which a client shows next to the server's name. Left
    # unset it is the empty string; under mcp 1.x it was the *SDK's* version
    # (a 1.6.1 relay announced itself as "1.29.0"), which was never what a user
    # reading it wanted. relay's own version is.
    version=__version__,
    instructions=INSTRUCTIONS,
    website_url=settings.relay_base_url.rstrip("/"),
    icons=_brand_icons(),
    auth=_build_auth(),
)


_db = database.connect


def _current_actor() -> Actor | None:
    """The identity behind the in-flight tool call (relay #198, B-7), or
    ``None`` for a caller this server can't yet attribute (shouldn't happen —
    every tool call is already authenticated by ``auth=_build_auth()`` — but
    provenance is advisory enrichment, never a reason to fail a write).

    Two paths, confirmed by reading fastmcp's ``AccessToken`` construction
    directly (not assumed — same standard this file already holds every
    other `OIDCProxy` subtlety to):

    - **Static bearer**: ``subject`` is set by ``_StaticBearerAuth`` below to
      the matched key's configured name (``apikey`` or a `RELAY_API_KEYS`
      name) — used as-is.
    - **MCP OAuth** (`verify_id_token=True`): the token verifier
      (`fastmcp.server.auth.providers.jwt.JWTVerifier.load_access_token`)
      sets ``subject = claims["sub"]`` from PocketID's **id_token** and
      ``claims`` to that token's full decoded payload — so `email` (relay
      requests the `email` OIDC scope) is already a top-level claim,
      independently of `_RelayOIDCProxy._extract_upstream_claims`'s own copy
      nested under `claims["upstream_claims"]` (a separate fastmcp
      mechanism, `OAuthProxy.load_access_token`'s token-swap merge). `sub` is
      an opaque, IdP-assigned identifier — unreadable in a git author line or
      an `?author=` filter — so `email` is preferred here even though
      `subject` is the more reliably-present field, mirroring
      `identity.actor_from_session`'s own email-first priority for the web
      UI's OIDC session cookie.
    """
    token = get_access_token()
    if token is None:
        return None
    claims = token.claims or {}
    name = claims.get("email") or token.subject or claims.get("sub")
    if not name:
        return None
    return Actor(name=name, email=f"{name}@relay.local" if "@" not in name else name)


@mcp.resource(
    "relay://master-document",
    name="Master Document",
    description=(
        "The relay master document (post id=0): index, tag taxonomy, naming "
        "conventions, and house rules. Read before publishing."
    ),
    mime_type="text/markdown",
)
async def master_document() -> str:
    async with _db() as db:
        post = await service.get_post(db, 0)
    return post.content if post is not None else "Master document not found."


@mcp.tool(
    description=(
        "Publish a post to the relay feed. Subscribers receive it in real time. The response's "
        "'similar' field (relay #198, N-7) is an advisory duplicate-guard: other posts already in "
        "the vault whose embedding is close enough to be worth a glance before you duplicate work "
        "— always empty if this relay hasn't got embeddings enabled, and never blocks or slows down "
        "the publish itself."
    )
)
async def publish_post(
    title: str,
    content: str,
    tags: list[str] | None = None,
    source: str | None = None,
    expires_at: str | None = None,
) -> dict:
    """`title` becomes the Markdown filename. `expires_at`: optional ISO 8601 datetime; overrides tag/global TTL."""
    metrics.record_tool_call("publish_post")
    body = PostCreate(
        content=content,
        title=title,
        tags=tags or [],
        source=source,
        expires_at=expires_at,
    )
    async with _db() as db:
        post = await service.create_post(db, body, actor=_current_actor())
    return post.model_dump()


@mcp.tool(
    description=(
        "List posts from the relay feed, optionally filtered by tag, folder or search term. "
        "Returns metadata-only summaries (id, title, tags, folder, and a short excerpt) "
        "by default — call get_post(id) for a full body. Pass summary=false to get full "
        "content inline (heavier). sort is 'updated' (default, last modified — includes "
        "edits made directly in Obsidian) or 'created'; order is 'desc' (default) or 'asc'. "
        "Sort by created + asc to read a topic's posts in the order they were written. "
        "mode ranks 'search' (relay #253, proof of concept): 'keyword' (default, FTS5/bm25), "
        "'semantic' (embedding similarity), or 'hybrid' (fusion of both), and can be combined "
        "with tag/folder — semantic/hybrid return an error if this relay hasn't got embeddings "
        "enabled. A bare id or '#id' as search (e.g. '42' or '#42') is a lookup, not a ranked "
        "search: it answers with just that post as the response's 'pinned' field (items empty) "
        "and ignores mode/tag/folder entirely — a plain id lookup works the same whether or not "
        "this relay has embeddings enabled. author (relay #198, B-7) filters to posts whose most "
        "recent write is attributed to that identity (a named API key or an OIDC user's email/sub)."
    )
)
async def list_posts(
    tag: str | None = None,
    folder: str | None = None,
    search: str | None = None,
    limit: int = 20,
    offset: int = 0,
    summary: bool = True,
    sort: str = "updated",
    order: str = "desc",
    mode: str = "keyword",
    author: str | None = None,
) -> dict:
    metrics.record_tool_call("list_posts")
    async with _db() as db:
        try:
            result = await service.list_posts(
                db, tag=tag, folder=folder, search=search, limit=limit, offset=offset,
                summary=summary, sort=sort, order=order, mode=mode, author=author,
            )
        except service.SemanticSearchUnavailable:
            return {"error": "Semantic search is not enabled on this relay."}
        except service.InvalidSearchMode:
            return {"error": "mode must be 'keyword', 'semantic', or 'hybrid'."}
    return result.model_dump()


@mcp.tool(description="Get a single post by its ID. Use id=0 for the master document.")
async def get_post(id: int) -> dict:
    metrics.record_tool_call("get_post")
    async with _db() as db:
        post = await service.get_post(db, id)
    if post is None:
        return {"error": f"Post #{id} not found."}
    return post.model_dump()


@mcp.tool(
    description=(
        "Update an existing post. Only provided fields change; omitted fields are left "
        "untouched. Providing tags replaces the list wholesale; an empty array clears them. "
        "Pass an empty string for expires_at (or source) to clear it. Pass if_match (a "
        "post's etag from a prior response) to reject the write with a conflict if the "
        "post has changed since — otherwise this silently overwrites like today."
    )
)
async def update_post(
    id: int,
    title: str | None = None,
    content: str | None = None,
    tags: list[str] | None = None,
    source: str | None = None,
    expires_at: str | None = None,
    if_match: str | None = None,
) -> dict:
    metrics.record_tool_call("update_post")
    # An omitted argument and an explicit null both arrive as None here, so a
    # None is "leave alone". PostUpdate turns "" into a clear for expires_at and
    # source — the documented way to unset either from MCP (AUDIT.md B-05).
    fields = {
        "title": title,
        "content": content,
        "tags": tags,
        "source": source,
        "expires_at": expires_at,
        "if_match": if_match,
    }
    body = PostUpdate(**{k: v for k, v in fields.items() if v is not None})
    async with _db() as db:
        try:
            post = await service.update_post(db, id, body, actor=_current_actor())
        except service.PostNotFound:
            return {"error": f"Post #{id} not found."}
        except service.ConcurrentModification:
            current = await service.get_post(db, id)
            return {
                "error": f"post #{id} has changed since if_match was captured",
                "current": current.model_dump() if current is not None else None,
            }
    return post.model_dump()


@mcp.tool(
    description=(
        "Partial edit: old_str must match exactly once in the post's current content "
        "(like a code-agent str_replace) and is replaced with new_str — for changing a "
        "line or paragraph without resending the whole body. Add more surrounding "
        "context to old_str if it isn't unique. Pass if_match (a post's etag from a "
        "prior response) to also reject the edit if the post changed since you read it."
    )
)
async def edit_post(id: int, old_str: str, new_str: str, if_match: str | None = None) -> dict:
    metrics.record_tool_call("edit_post")
    async with _db() as db:
        try:
            post = await service.edit_post(db, id, old_str, new_str, if_match=if_match, actor=_current_actor())
        except service.PostNotFound:
            return {"error": f"Post #{id} not found."}
        except service.EditNoChange:
            return {"error": "new_str must be different from old_str."}
        except service.EditTextNotFound:
            return {"error": f"old_str not found in post #{id}'s content."}
        except service.EditTextNotUnique as exc:
            return {"error": f"old_str matches {exc.count} times in post #{id}; must match exactly once."}
        except service.ConcurrentModification:
            current = await service.get_post(db, id)
            return {
                "error": f"post #{id} has changed since if_match was captured",
                "current": current.model_dump() if current is not None else None,
            }
    return post.model_dump()


@mcp.tool(
    description=(
        "Append content to the end of a post instead of resending the whole body. A "
        "blank line is inserted before it unless the post is currently empty. Pass "
        "if_match (a post's etag from a prior response) to reject the append if the "
        "post changed since you read it."
    )
)
async def append_post(id: int, content: str, if_match: str | None = None) -> dict:
    metrics.record_tool_call("append_post")
    async with _db() as db:
        try:
            post = await service.append_post(db, id, content, if_match=if_match, actor=_current_actor())
        except service.PostNotFound:
            return {"error": f"Post #{id} not found."}
        except service.ConcurrentModification:
            current = await service.get_post(db, id)
            return {
                "error": f"post #{id} has changed since if_match was captured",
                "current": current.model_dump() if current is not None else None,
            }
    return post.model_dump()


@mcp.tool(
    description=(
        "List a post's revision history from the vault's git history, newest first. "
        "Works for a deleted post too (exists=false), which is the case most worth "
        "recovering. Each item has sha, short_sha, when, message, and path — pass a sha "
        "to restore_post. Returns an error if vault history is disabled."
    )
)
async def get_post_history(id: int, limit: int = 20) -> dict:
    metrics.record_tool_call("get_post_history")
    async with _db() as db:
        try:
            result = await service.get_post_history(db, id, limit=limit)
        except service.HistoryUnavailable:
            return {"error": "Vault history is disabled or git is unavailable."}
    return result.model_dump()


@mcp.tool(
    description=(
        "List posts that no longer exist but can still be restored, newest first. This is "
        "the discovery half of recovery: restore_post can put back any post whose id you "
        "know, and after a delete you do not know it. Each item carries id, title, sha, "
        "when, reason (deleted, external or expiry) and path — pass the id and sha "
        "straight to restore_post. TTL expiries are excluded unless include_expiry is "
        "true, since those are routine and would bury the deletion you are looking for. "
        "Nothing is moved on delete and there is nothing to purge: this reads the vault's "
        "git history. Returns an error if vault history is disabled."
    )
)
async def list_deleted_posts(limit: int = 50, include_expiry: bool = False) -> dict:
    metrics.record_tool_call("list_deleted_posts")
    async with _db() as db:
        try:
            result = await service.list_deleted_posts(db, limit=limit, include_expiry=include_expiry)
        except service.HistoryUnavailable:
            return {"error": "Vault history is disabled or git is unavailable."}
    return result.model_dump()


@mcp.tool(
    description=(
        "The vault changelog (relay #198, N-4): every post-affecting write, newest first — "
        "create, update, edit, append, delete, restore, tag rename, an external Obsidian "
        "edit/delete, or a TTL expiry. Each item has seq, id, title, action, when, sha and "
        "author (relay #198, B-7: null for a write that predates it, or one with no "
        "authenticated identity — the TTL sweep, an external edit). Pass since as a seq "
        "from a prior response to page forward, or an ISO 8601 timestamp to see what moved "
        "after a given time — e.g. 'what did the schedulers publish overnight'. Omit since "
        "for the most recent `limit`. author filters to one identity's writes. This is a "
        "flat feed over git history, not a second store — reading it costs nothing extra."
    )
)
async def list_changes(since: str | None = None, limit: int = 50, author: str | None = None) -> dict:
    metrics.record_tool_call("list_changes")
    async with _db() as db:
        try:
            rows = await changes.list_changes(db, since=since, limit=limit, author=author)
        except changes.HistoryUnavailable:
            return {"error": "Vault history is disabled or git is unavailable."}
    return ChangeListResponse(items=[ChangeEntry.from_row(r) for r in rows]).model_dump()


@mcp.tool(
    description=(
        "Read a post exactly as it was at one revision — title, content and tags. Use this "
        "to see what a restore would give back before calling restore_post: the history "
        "listing carries only metadata, and picking a sha out of it blind is a poor way to "
        "undo something. Works for a deleted post too, and accepts a short sha. Read-only; "
        "it changes nothing. Returns an error if vault history is disabled."
    )
)
async def get_post_revision(id: int, sha: str) -> dict:
    metrics.record_tool_call("get_post_revision")
    async with _db() as db:
        try:
            result = await service.get_post_revision(db, id, sha)
        except service.HistoryUnavailable:
            return {"error": "Vault history is disabled or git is unavailable."}
        except service.RevisionNotFound:
            return {"error": f"No revision '{sha}' in the history of post #{id}."}
    return result.model_dump()


@mcp.tool(
    description=(
        "List a post's backlinks — the other posts that link to it via [[Title]] or #id. "
        "Check this before rewriting or deleting a post: relay keeps one canonical post per "
        "topic and cross-links by id, so the posts listed here are the ones that break if it "
        "goes away or is renamed. Returns an error if the post does not exist."
    )
)
async def get_backlinks(id: int) -> dict:
    metrics.record_tool_call("get_backlinks")
    async with _db() as db:
        try:
            result = await service.get_backlinks(db, id)
        except service.PostNotFound:
            return {"error": f"Post #{id} not found."}
    return result.model_dump()


@mcp.tool(
    description=(
        "List posts related to this one by embedding similarity that it does NOT already "
        "cross-link via [[Title]] or #id, in either direction (relay #198, N-7) — an automatic "
        "to-do list of missing wikilinks, not a general 'more like this'. A post already linked "
        "has already had its relationship made explicit and won't show up here. Returns an error "
        "if the post doesn't exist or this relay hasn't got embeddings enabled."
    )
)
async def get_related(id: int) -> dict:
    metrics.record_tool_call("get_related")
    async with _db() as db:
        try:
            result = await service.get_related(db, id)
        except service.PostNotFound:
            return {"error": f"Post #{id} not found."}
        except service.SemanticSearchUnavailable:
            return {"error": "Semantic search is not enabled on this relay."}
    return result.model_dump()


@mcp.tool(
    description=(
        "Rename a tag across every post that carries it, in one atomic pass. Use this to fix "
        "taxonomy rather than retagging posts one at a time — that is slower and leaves the "
        "vault half-migrated if it stops partway. The new name is normalised the same way "
        "tags always are (lowercased; only letters, digits, hyphen and underscore survive). "
        "Renaming to a tag that already exists merges the two. Returns the full tag list."
    )
)
async def rename_tag(tag: str, new_name: str) -> dict:
    metrics.record_tool_call("rename_tag")
    cleaned = re.sub(r"[^a-z0-9_-]", "", new_name.strip().lower())
    if not cleaned:
        return {"error": "new_name must contain at least one letter, digit, hyphen or underscore."}
    async with _db() as db:
        try:
            result = await service.rename_tag(db, tag, cleaned, actor=_current_actor())
        except service.InvalidTag:
            # `InvalidTag` carries no message (REST supplies its own static
            # detail too — see routes/tags.py) — `str(exc)` here would be "".
            return {"error": "tag must contain at least one letter, digit, hyphen or underscore."}
    return result.model_dump()


@mcp.tool(
    description=(
        "Restore a post to an earlier revision, recreating it if it was deleted. Pass a "
        "sha from get_post_history. The restore is itself recorded in history, so it can "
        "be undone the same way. Use this to undo a bad overwrite rather than "
        "reconstructing the body by hand."
    )
)
async def restore_post(id: int, sha: str) -> dict:
    metrics.record_tool_call("restore_post")
    async with _db() as db:
        try:
            post = await service.restore_post(db, id, sha, actor=_current_actor())
        except service.HistoryUnavailable:
            return {"error": "Vault history is disabled or git is unavailable."}
        except service.RevisionNotFound:
            return {"error": f"No revision '{sha}' in the history of post #{id}."}
    return post.model_dump()


@mcp.tool(
    description=(
        "Report this relay's runtime status: version, uptime, which vault it is serving, counts of "
        "posts/tags/folders/attachments, semantic-search embedding coverage and backend state, and which "
        "features are actually working. Use it to confirm you are talking to the vault you think you are, "
        "and to check features that degrade silently — vault history is off when git is missing (writes "
        "would be unrecoverable), search falls back to substring matching without FTS5, and external "
        "edits are not picked up when the watcher is off."   )
)
async def get_status() -> dict:
    metrics.record_tool_call("get_status")
    async with _db() as db:
        result = await status.build(db)
    return result.model_dump()


@mcp.tool(
    description=(
        "Lint the vault: check it against the rules already written down in #0 instead of "
        "relying on someone reading every post (relay #198, N-5): posts missing a domain and/or type tag, "
        "a post with zero tags, notes stuck in Inbox despite carrying a domain tag, broken #NNN "
        "refs and dangling [[wikilinks]] (and specifically links pointing at a deleted post), a "
        "dangling attachment embed (![[file]]) or a plain [[wikilink]] whose target is a filename "
        "(should be an embed or attachment instead) — each its own rule, since the fix differs from "
        "an ordinary broken post link — an H1 missing entirely, or one that drifted from the title "
        "(the classic case: relay's filename sanitizer strips a character like ':' that the body's "
        "H1 still has), a hub/plan post not updated in a while, a post with zero backlinks, a "
        "tags.yml entry with zero posts, a post that embedded to zero chunks, and #0's own stated "
        "post count going stale. Link/embed/heading scanning ignores fenced code and inline code "
        "spans (a post documenting relay's own syntax doesn't get flagged for its own examples), "
        "a bare #N below 10 or above the vault's id high-water mark is never treated as a post "
        "reference (footnote markers, procedure steps and GitHub issue/PR numbers collide with real "
        "ids far more often than a genuine cross-link does at either extreme), and a link to a "
        "deleted post that carried a rotation tag like digest at deletion time is suppressed rather "
        "than re-reported every run. The four link rules (broken_link, link_to_deleted_post, "
        "broken_attachment_embed, wikilink_to_filename) report one finding per distinct broken "
        "target per post, not one per mention, with the real count in occurrences, and carry a "
        "match field — the exact broken text — for jumping straight to it in an editor. The master "
        "document (id=0) is exempt from the tag/folder/H1/staleness/embedding-coverage rules — a "
        "root index reasonably breaks conventions an ordinary post follows — but not from the link "
        "checks: a broken cross-link in #0 is a factual defect, not a convention, and matters more "
        "there than anywhere else. Findings are ordered by post id. Read-only. A rule that needs a "
        "disabled feature (history, embeddings) is skipped and named in skipped_rules rather than "
        "erroring."
    )
)
async def lint_vault() -> dict:
    metrics.record_tool_call("lint_vault")
    async with _db() as db:
        result = await lint.run(db)
    return result.model_dump()


@mcp.tool(
    description=(
        "Re-run the embedding backfill without restarting relay — resumes from the content-addressed "
        "cache by default, or pass force=true to wipe every embedded chunk/vector/cache row first and "
        "re-embed the whole vault from scratch. Returns the same object as get_status's 'embeddings' "
        "field. Errors if embeddings aren't enabled on this relay, or if a backfill is already running."
    )
)
async def trigger_embedding_backfill(force: bool = False) -> dict:
    metrics.record_tool_call("trigger_embedding_backfill")
    async with _db() as db:
        try:
            result = await status.trigger_backfill(db, force=force)
        except status.EmbeddingsUnavailable:
            return {"error": "Semantic search is not enabled on this relay."}
        except status.BackfillAlreadyRunning:
            return {"error": "A backfill is already running."}
    return result.model_dump()


@mcp.tool(
    description=(
        "Turn semantic/hybrid search on or off at runtime, without a restart. Enabling only resumes "
        "against whatever model and vector schema are already on disk and kicks off a backfill for "
        "any posts written while it was off; changing EMBEDDING_MODEL to a different dimension still "
        "needs a restart. Disabling frees the embedding model's memory immediately rather than waiting "
        "for the idle timeout. In-memory only — a restart reverts to whatever .env says. Returns the "
        "same object as get_status's 'embeddings' field."
    )
)
async def set_embeddings_enabled(enabled: bool) -> dict:
    metrics.record_tool_call("set_embeddings_enabled")
    async with _db() as db:
        try:
            result = await status.set_embeddings_enabled(db, enabled)
        except status.EmbeddingsUnavailable:
            return {
                "error": "sqlite-vec is not available on this relay, or EMBEDDING_MODEL is not a known "
                "fastembed model."
            }
        except status.EmbeddingDimensionMismatch:
            return {
                "error": "EMBEDDING_MODEL's dimension doesn't match the vector schema already on disk. "
                "Restart relay to rebuild it before enabling."
            }
    return result.model_dump()


@mcp.tool(description="Delete a post from the relay feed by its ID. The master document (id=0) cannot be deleted.")
async def delete_post(id: int) -> dict:
    metrics.record_tool_call("delete_post")
    async with _db() as db:
        try:
            await service.delete_post(db, id, actor=_current_actor())
        except service.ProtectedPost:
            return {"error": "Master document (id=0) cannot be deleted."}
        except service.PostNotFound:
            return {"error": f"Post #{id} not found."}
    return {"ok": True, "deleted": id}


@mcp.tool(
    description=(
        "Attach a file (image, PDF, …) to the vault. Provide the bytes exactly one way: "
        "`data` (base64 — only viable for tiny files, since you must emit the whole blob), "
        "`source_url` (an http(s) URL the server fetches — preferred for real files), or "
        "`upload_id` from create_upload (bytes PUT out-of-band). With `post_id`, the file is "
        "filed under that post's folder and its ![[file]] embed is appended to the post body; "
        "otherwise it goes to `folder`, or to the folder `tags` would file a post under, or Inbox — and you place "
        "the returned `ref` yourself. Pass embed=false to file a post's attachment without touching its body. "
        "`filename` is required with `data`; with `source_url`/`upload_id` it's derived when omitted."
    )
)
async def add_attachment(
    filename: str | None = None,
    data: str | None = None,
    source_url: str | None = None,
    upload_id: str | None = None,
    post_id: int | None = None,
    folder: str | None = None,
    tags: list[str] | None = None,
    embed: bool = True,
) -> dict:
    """Returns {filename, ref, folder, post_id}. `ref` is the ![[…]] embed to drop into a post."""
    metrics.record_tool_call("add_attachment")
    try:
        body = AttachmentCreate(
            filename=filename, data=data, source_url=source_url,
            upload_id=upload_id, post_id=post_id, folder=folder, tags=tags or [], embed=embed,
        )
    except ValidationError as exc:
        return {"error": _first_error(exc)}
    async with _db() as db:
        try:
            result = await service.ingest_attachment(
                db, filename=body.filename, data=body.data, source_url=body.source_url,
                upload_id=body.upload_id, post_id=body.post_id, folder=body.folder,
                tags=body.tags, embed=body.embed, actor=_current_actor(),
            )
        except ValueError:
            return {"error": "data is not valid base64"}
        except service.AttachmentSourceError as exc:
            return {"error": str(exc)}
        except service.PostNotFound:
            return {"error": f"Post #{post_id} not found."}
        except service.AttachmentError as exc:
            return {"error": str(exc)}
    return result.model_dump()


@mcp.tool(
    description=(
        "Mint a presigned upload slot for a file too large to pass as base64. Returns "
        "{upload_id, upload_url, method, max_bytes, expires_at}: PUT the raw bytes to "
        "`upload_url` (out-of-band — not through this tool call), then call add_attachment "
        "with the `upload_id` to file it. Use when you can reach the relay host to PUT."
    )
)
async def create_upload() -> dict:
    metrics.record_tool_call("create_upload")
    return service.create_upload_slot().model_dump()


@mcp.tool(
    description=(
        "Retrieve an attachment from the vault by its filename (as used in ![[file]]). "
        "Images are returned so they can be viewed inline; other files return a note with "
        "the vault path."
    )
)
async def get_attachment(name: str):
    """Returns image content for images, else a dict describing the file."""
    metrics.record_tool_call("get_attachment")
    try:
        result = vault.read_attachment(name, max_bytes=settings.attachment_max_bytes)
    except ValueError:
        return {"error": f"Attachment '{name}' is too large to return inline "
                         f"(over {settings.attachment_max_mb} MB)."}
    if result is None:
        return {"error": f"Attachment '{name}' not found."}
    path, raw, mime = result
    if mime.startswith("image/"):
        # Derive format from the mime (image/jpeg → 'jpeg') so Image doesn't emit
        # a non-standard type like image/jpg from the '.jpg' suffix.
        return Image(data=raw, format=mime.split("/", 1)[1])
    return {"filename": path.name, "mime": mime, "bytes": len(raw),
            "note": "Non-image attachment; not shown inline."}


@mcp.tool(
    description=(
        "Delete an attachment from the vault by its filename. Returns the removed name and "
        "any post ids that still embed/link it (now dangling) so you can fix them."
    )
)
async def delete_attachment(name: str) -> dict:
    metrics.record_tool_call("delete_attachment")
    async with _db() as db:
        result = await service.delete_attachment(db, name, actor=_current_actor())
    if result is None:
        return {"error": f"Attachment '{name}' not found."}
    return result.model_dump()


@mcp.tool(
    description=(
        "List attachments stored in the vault (filename, folder, size, and the ![[…]] "
        "embed ref). Scope with `post_id` (that post's folder) or `folder`; omit both to "
        "list every attachment. Use the returned filename with get_attachment."
    )
)
async def list_attachments(post_id: int | None = None, folder: str | None = None) -> dict:
    metrics.record_tool_call("list_attachments")
    async with _db() as db:
        try:
            result = await service.list_attachments(db, post_id=post_id, folder=folder)
        except service.PostNotFound:
            return {"error": f"Post #{post_id} not found."}
        except service.InvalidFolder as exc:
            return {"error": str(exc)}
    return result.model_dump()


@mcp.tool(
    description=(
        "List the vault's first-level folders with their post counts. These are the names list_posts and l"
        "ist_attachments accept as `folder`; a post is filed by its first domain tag at creation, so t"
        "his is the map of what the vault already has before you choose one."
    )
)
async def list_folders() -> dict:
    metrics.record_tool_call("list_folders")
    async with _db() as db:
        result = await service.list_folders(db)
    return result.model_dump()


@mcp.tool(description="List all tags in the relay feed with their post counts.")
async def list_tags() -> dict:
    metrics.record_tool_call("list_tags")
    async with _db() as db:
        result = await service.list_tags(db)
    return result.model_dump()


@mcp.tool(
    description=(
        "Set expiry configuration for a tag. Provide ttl_hours (relative to each post's "
        "creation), expires_at (absolute cutoff), or both. Only affects posts without their "
        "own expires_at. Provide neither to remove the tag's expiry configuration."
    )
)
async def set_tag_config(
    tag: str,
    ttl_hours: int | None = None,
    expires_at: str | None = None,
) -> dict:
    metrics.record_tool_call("set_tag_config")
    body = TagConfigCreate(ttl_hours=ttl_hours, expires_at=expires_at)
    async with _db() as db:
        result = await service.set_tag_config(db, tag, body)
    return result.model_dump()


# Built once at import time (relay #313 Phase 1 spike) rather than per-call: fastmcp's
# http_app() constructs the Starlette app and its (not-yet-running) session manager
# together, and main.py's own lifespan needs this *exact* instance's `.lifespan` context
# manager — mounted sub-apps don't get their lifespan run automatically, so relay drives
# it from its own lifespan (see main.py). Building a second instance there would give
# FastAPI a session manager different from the one actually mounted.
#
# host_origin_protection=False matches today's explicit disable of the old SDK's
# DNS-rebinding protection: relay is mounted into FastAPI behind a public reverse proxy,
# not run standalone, so a real Host header (e.g. relay.geon.im) must not be rejected.
# fastmcp's own default ("auto") is smarter than the old SDK's — it only enforces on a
# loopback bind — but False pins relay to the same explicit, audited behavior it has
# always had rather than an implicit heuristic. Revisit in Phase 5.
#
# The old SDK's 4 MiB max_request_body_size cap is still very much active — verified
# live (a ~6 MB /mcp POST body 413s with "Request body too large"). fastmcp's http_app()
# has no parameter to configure it, but internally still builds the old SDK's
# TransportSecuritySettings when constructing the session manager and only overrides
# enable_dns_rebinding_protection on it (see host_origin_protection above), leaving
# max_request_body_size at the SDK's own DEFAULT_MAX_REQUEST_BODY_SIZE. So this is now
# an unexposed fastmcp-internal default rather than a relay-configurable setting — worth
# reconfirming across future fastmcp versions (Phase 5·E), not a Phase 1 gap.
#
# `auth=_build_auth()` above (not a wrapper here) is what actually gates every
# request — fastmcp's own `http_app()` forwards `self.auth` into the app it builds and
# wraps it in its own RequireAuthMiddleware, so there is nothing left for this module to
# do beyond building the app and handing it to main.py to mount directly.
mcp_http_app = mcp.http_app(
    path="/mcp",
    transport="streamable-http",
    stateless_http=True,
    host_origin_protection=False,
)
