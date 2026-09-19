from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

_ENV_FILE = Path(__file__).parent.parent / ".env"

logger = logging.getLogger(__name__)

# Mirrors models._clean_tag_list's shape (a key name ends up in git author
# fields and `?author=` query strings) and identity.APIKEY_NAME, duplicated
# rather than imported: relay.identity imports settings from this module, so
# importing back would be a cycle.
_KEY_NAME_RE = re.compile(r"^[a-z0-9_-]+$")
_RESERVED_KEY_NAME = "apikey"


@dataclass(frozen=True)
class ApiKeyScope:
    """What a bearer key may do (relay #198, B-8) — parsed from
    ``RELAY_API_KEY_SCOPES``, orthogonal to ``RELAY_API_KEYS`` (which key
    material resolves to which identity). ``mode`` is ``"full"``, ``"read"``
    or ``"write"``; ``tags`` is only meaningful for ``"write"`` — the set of
    vault tags a write may touch, checked ALL-of (every tag on the post, not
    just one) by ``identity.Actor.can_write_tags``.

    A name absent from ``Settings.key_scopes`` means ``FULL_SCOPE`` wherever
    that's looked up — this type only ever represents a *restriction*, so a
    missing entry can never grant more than a key already had, which is what
    keeps this additive to every already-deployed ``RELAY_API_KEYS`` key.
    """

    mode: str
    tags: frozenset[str] = field(default_factory=frozenset)


FULL_SCOPE = ApiKeyScope(mode="full")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=_ENV_FILE, env_file_encoding="utf-8", extra="ignore")

    api_key: str
    # Named keys for per-agent identity/provenance (relay #198, B-7):
    # "name:key,name:key,...". Each resolves to its own git commit author,
    # `changes.author` and `updated_by` — see relay/identity.py. The plain
    # `api_key` above always remains valid too, under the reserved identity
    # `identity.APIKEY_NAME` ("apikey").
    api_keys: str = Field(
        default="",
        validation_alias=AliasChoices("RELAY_API_KEYS", "API_KEYS"),
    )
    # Per-key read/write-to-tags restrictions (relay #198, B-8): "name:mode[:tags]",
    # comma-separated, keyed by the same names as `api_keys` but parsed independently
    # (see `key_scopes`'s own docstring for why this isn't a third field on api_keys).
    # A name absent here is full access — every key deployed before this feature keeps
    # behaving exactly as it does today.
    api_key_scopes: str = Field(
        default="",
        validation_alias=AliasChoices("RELAY_API_KEY_SCOPES", "API_KEY_SCOPES"),
    )
    relay_base_url: str = "http://localhost:8000"
    default_ttl_hours: int = 0  # 0 = never expire
    cleanup_interval_minutes: int = 60
    vault_path: str = Field(
        default="/data/vault",
        validation_alias=AliasChoices("RELAY_VAULT_PATH", "VAULT_PATH"),
    )
    watch_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("RELAY_WATCH_ENABLED", "WATCH_ENABLED"),
    )
    # Commit the vault to a git repo after every write, so a clobbered post is
    # recoverable (`git log`/`revert`). Degrades to a no-op with one warning if
    # the git binary is missing — history never gates a write.
    history_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("RELAY_HISTORY_ENABLED", "HISTORY_ENABLED"),
    )
    # Proof-of-concept semantic search (relay post #253). Off by default
    # everywhere — including production — until phase 4's eval numbers say
    # it's worth surfacing; only opt-in tests and the eval harness turn it on.
    embedding_enabled: bool = Field(
        default=False,
        validation_alias=AliasChoices("RELAY_EMBEDDING_ENABLED", "EMBEDDING_ENABLED"),
    )
    # Part of the cache key (relay.vectors._hash's model_id) — changing this
    # alone forces a full re-embed on next sync, no migration needed. Post
    # #253's pick, `intfloat/multilingual-e5-small`, isn't in fastembed 0.8.0's
    # model registry (only the 1024-dim `-large` variant is) — verified in this
    # environment. This is the same 384-dim, MIT-licensed, multilingual family
    # the post wanted (EN/IT/ES/CA coverage) without a schema change; exactly
    # the "cheap to change your mind about" swap the post's design allows for.
    embedding_model: str = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
    # onnxruntime's own default (unset intra_op_num_threads) picks a thread count
    # from the host's CPU count, and each thread carries its own tensor-buffer
    # overhead — real memory cost measured against a 2-CPU production VPS (relay
    # #253's post-v1.1.1 "reduce memory footprint" priority). Embedding here is
    # inherently sequential (one post's chunks at a time, never concurrent
    # batches — see vault.backfill_embeddings/sync_post_chunks), so there is no
    # cross-request parallelism to lose by pinning this low. 1 is the
    # conservative starting point; raise it (env var, no code change) if a
    # deployment has CPU to spare and wants faster embedding instead.
    embedding_threads: int = 1
    # The real memory cost, measured locally: constructing FastEmbedBackend
    # jumps RSS ~67MB -> ~637MB (onnxruntime session + model weights), *before*
    # a single embed call — and stays flat after, across both a single query
    # and a 20-doc batch. So it's not a leak and not per-call growth; it's the
    # one-time cost of having the model loaded at all, which embedding_threads
    # (a thread-pool cap) never touched — confirmed on the production VPS: no
    # observed reduction. This is the model actually being unloaded between
    # uses to give that ~570MB back to the OS during idle stretches, at the
    # cost of a several-second reload on the next search/write. 0 disables
    # (never unload, previous always-resident behavior).
    embedding_idle_unload_seconds: int = 300
    relay_palette: str = "default"
    relay_transparent: bool = False
    secure_cookies: bool = True
    attachment_max_mb: int = 25  # reject uploads larger than this (base64-decoded)
    # How long a presigned upload slot (POST /attachments/uploads) stays open for
    # its out-of-band PUT before it's purged. Short-lived like an OAuth code.
    attachment_upload_ttl_seconds: int = 3600  # 1h
    # Timeout (seconds) for a server-side source_url fetch on add_attachment.
    attachment_fetch_timeout_seconds: int = 20

    # --- Web UI OIDC (PocketID). All optional; absent => OIDC login disabled,
    # the API-key paste + bearer paths keep working unchanged. ---
    oidc_issuer: str = ""  # PocketID base URL (OIDC discovery at /.well-known/openid-configuration)
    oidc_client_id: str = ""
    oidc_client_secret: str = ""
    # Signs the relay_session cookie. Falls back to api_key if unset, so sessions
    # still carry identity/expiry even without a dedicated secret.
    session_secret: str = ""
    session_max_age_hours: int = 24 * 30  # signed-cookie lifetime
    # Allowlists of who may log in via OIDC (comma-separated). Prefer subs: the
    # OIDC `sub` is the IdP's immutable user id, so it can't be spoofed by a user
    # editing their own profile. Email matching additionally requires a verified
    # email. Both empty => any user PocketID authenticates is allowed.
    oidc_allowed_subs: str = ""
    oidc_allowed_emails: str = ""

    # --- Phase 2: remote MCP OAuth (relay as its own Authorization Server,
    # brokering the human login upstream to PocketID). When enabled, /mcp is an
    # OAuth 2.1 Resource Server; the SDK mounts /authorize /token /register
    # /revoke + metadata, and relay mints audience-bound tokens for Claude.
    # The upstream login reuses the Phase-1 OIDC client (oidc_*), so no separate
    # MCP client credentials — just add the /mcp/oauth/callback redirect URI to
    # that PocketID client. Absent/false keeps the static-bearer path unchanged. ---
    mcp_oauth_enabled: bool = False
    mcp_required_scopes: str = "relay"  # comma-separated; single scope = full tool access
    # DCR redirect-URI host allowlist (comma-separated) for https redirects. Blocks
    # an attacker from registering a client that points an auth code at their own
    # https endpoint. Defaults to Claude's known connector callback hosts; empty =
    # allow any https (opt-out). http redirects stay loopback-only regardless. An
    # entry may be an exact host (`claude.ai`) or a `*.`-prefixed suffix
    # (`*.mistral.ai`) matching that domain's subdomains only — not the bare apex,
    # and never a naive substring (see `mcp_redirect_host_wildcards`).
    mcp_allowed_redirect_hosts: str = "claude.ai,claude.com,chatgpt.com,*.mistral.ai"

    @property
    def attachment_max_bytes(self) -> int:
        return self.attachment_max_mb * 1024 * 1024

    @property
    def uploads_dir(self) -> str:
        """Staging dir for presigned uploads (bytes land here before finalize).

        Under ``.relay/`` so it rides the vault dir, but wiped at startup — an
        unclaimed slot is disposable, like an OAuth auth code."""
        return str(Path(self.relay_dir) / "uploads")

    @property
    def oidc_enabled(self) -> bool:
        return bool(self.oidc_issuer and self.oidc_client_id and self.oidc_client_secret)

    @property
    def mcp_oauth_active(self) -> bool:
        """Whether remote MCP OAuth is *actually* running. It needs the upstream
        OIDC client too — the flag alone can't broker a login. Single source of
        truth so wiring, store init, and cleanup never disagree."""
        return self.mcp_oauth_enabled and self.oidc_enabled

    @property
    def session_signing_key(self) -> str:
        """Key for signing the session cookie; falls back to the API key."""
        return self.session_secret or self.api_key

    @property
    def allowed_emails(self) -> set[str]:
        return {e.strip().lower() for e in self.oidc_allowed_emails.split(",") if e.strip()}

    @property
    def allowed_subs(self) -> set[str]:
        return {s.strip() for s in self.oidc_allowed_subs.split(",") if s.strip()}

    @property
    def named_api_keys(self) -> dict[str, str]:
        """Parsed ``RELAY_API_KEYS`` ("name:key,name:key,..."). A malformed
        entry, a name colliding with the reserved primary-key identity
        (``apikey``), or a duplicate name is skipped with a warning rather
        than failing startup — a typo'd extra key shouldn't take the relay
        down, it just won't authenticate anything until fixed."""
        keys: dict[str, str] = {}
        for entry in self.api_keys.split(","):
            entry = entry.strip()
            if not entry:
                continue
            name, _, key = entry.partition(":")
            name, key = name.strip().lower(), key.strip()
            if not key or not _KEY_NAME_RE.match(name):
                logger.warning("RELAY_API_KEYS: skipping malformed entry %r", entry)
                continue
            if name == _RESERVED_KEY_NAME:
                logger.warning("RELAY_API_KEYS: %r is reserved for the primary API_KEY — skipping", name)
                continue
            if name in keys:
                logger.warning("RELAY_API_KEYS: duplicate name %r — keeping the first", name)
                continue
            keys[name] = key
        return keys

    @property
    def all_api_keys(self) -> dict[str, str]:
        """Every valid bearer key, by identity name — the primary ``API_KEY``
        (under the reserved name ``apikey``) plus every named key. Single
        source ``identity.resolve_bearer``/``auth.bearer_matches`` both read."""
        return {_RESERVED_KEY_NAME: self.api_key, **self.named_api_keys}

    @property
    def key_scopes(self) -> dict[str, ApiKeyScope]:
        """Parsed ``RELAY_API_KEY_SCOPES`` ("name:mode[:tags]", comma-separated;
        relay #198, B-8). A separate variable from ``RELAY_API_KEYS`` rather than
        a third ``:``-delimited field on it: ``named_api_keys`` already partitions
        each entry on its *first* colon only, so key material may itself legally
        contain colons — appending a scope suffix to that same grammar would be
        genuinely ambiguous to parse and risks corrupting already-deployed key
        secrets. Keeping the two independent also means scopes can be layered
        onto an existing key by name, with no redeploy of the key itself.

        Same skip-with-warning philosophy as ``named_api_keys``: a malformed
        entry, an unknown mode, a ``write`` entry with no (or an invalid) tag,
        a duplicate name, or the reserved ``apikey`` name is skipped with a
        warning, never a startup failure. The reserved-name exclusion mirrors
        ``named_api_keys``'s own guard — the break-glass primary key is not
        scopable, consistent with its exemption from the OIDC allowlist
        (``auth.still_authorized``'s ``sub == APIKEY_SUB`` carve-out). Does
        not otherwise cross-validate against ``all_api_keys`` — an entry for
        a name that isn't (or isn't yet) a configured key is simply inert,
        not an error, so scope config can be prepared ahead of the key it
        will apply to.

        ``tags`` are lowercased before validation, same as the ``name``
        itself and as every vault tag already is (``tags.py``'s own
        cleaning) — a `write:News` entry means the same thing as
        `write:news`, rather than silently failing to parse (and falling
        back to :data:`FULL_SCOPE`, the opposite of what a typo like that
        should do) just because vault tags are conventionally lowercase but
        this config value wasn't normalised to match.
        """
        scopes: dict[str, ApiKeyScope] = {}
        for entry in self.api_key_scopes.split(","):
            entry = entry.strip()
            if not entry:
                continue
            parts = entry.split(":")
            name = parts[0].strip().lower()
            if not _KEY_NAME_RE.match(name):
                logger.warning("RELAY_API_KEY_SCOPES: skipping malformed entry %r", entry)
                continue
            if name == _RESERVED_KEY_NAME:
                logger.warning("RELAY_API_KEY_SCOPES: %r is reserved for the primary API_KEY — skipping", name)
                continue
            if name in scopes:
                logger.warning("RELAY_API_KEY_SCOPES: duplicate name %r — keeping the first", name)
                continue
            mode = parts[1].strip().lower() if len(parts) > 1 else ""
            if mode in ("full", "read") and len(parts) == 2:
                scopes[name] = ApiKeyScope(mode=mode)
            elif mode == "write" and len(parts) == 3:
                tags = frozenset(t.strip().lower() for t in parts[2].split("+") if t.strip())
                if tags and all(_KEY_NAME_RE.match(t) for t in tags):
                    scopes[name] = ApiKeyScope(mode="write", tags=tags)
                else:
                    logger.warning("RELAY_API_KEY_SCOPES: skipping malformed entry %r", entry)
            else:
                logger.warning("RELAY_API_KEY_SCOPES: skipping malformed entry %r", entry)
        return scopes

    @property
    def mcp_scopes(self) -> list[str]:
        return [s.strip() for s in self.mcp_required_scopes.split(",") if s.strip()]

    @property
    def mcp_redirect_hosts(self) -> set[str]:
        """Allowlisted https redirect hosts for DCR (lowercased), exact-match
        entries only — `*.`-prefixed entries live in `mcp_redirect_host_wildcards`
        instead. Both empty = any (opt-out)."""
        return {
            h.strip().lower()
            for h in self.mcp_allowed_redirect_hosts.split(",")
            if h.strip() and not h.strip().startswith("*.")
        }

    @property
    def mcp_redirect_host_wildcards(self) -> set[str]:
        """Base domains (lowercased, no leading `*.`) from `*.`-prefixed entries in
        `MCP_ALLOWED_REDIRECT_HOSTS`. A base domain here matches only its
        subdomains (`sub.mistral.ai`), not the bare apex (`mistral.ai` needs its
        own separate exact entry) — mirrors how a wildcard TLS cert doesn't cover
        its own apex either, and keeps the two cases from being silently conflated.
        Matching is dot-boundary suffix matching (`provider._redirect_uri_allowed`),
        never a bare `str.endswith` on the raw domain — that would also accept
        `evilmistral.ai` for a `mistral.ai` entry (relay #313 Phase 2 checklist
        already flagged this exact class of bug for the future fastmcp migration;
        it applies here too)."""
        return {
            h.strip().lower().removeprefix("*.")
            for h in self.mcp_allowed_redirect_hosts.split(",")
            if h.strip().startswith("*.")
        }

    @property
    def mcp_allowed_client_redirect_uri_patterns(self) -> list[str] | None:
        """`mcp_redirect_hosts`/`mcp_redirect_host_wildcards` translated into
        fastmcp `OIDCProxy`'s `allowed_client_redirect_uris` URI-pattern shape
        (relay #313 Phase 2). Only a format translation, not a re-implementation
        of the matching itself: fastmcp's own wildcard host matching
        (`fastmcp.server.auth.redirect_validation._match_host`) uses the identical
        dot-boundary suffix semantics as this file's own docstrings describe —
        confirmed by reading it, not assumed — so `*.mistral.ai` here is exactly
        as safe as it always was.

        `None` (both sets empty) is the right translation of relay's own "empty =
        allow any https" default, not `[]` (which means "allow none" to fastmcp) —
        with no explicit allowlist, fastmcp falls back to trusting each DCR
        client's self-declared, self-registered redirect URI, which is the same
        practical openness relay's own "empty" already meant.

        Always includes `http://localhost:*`/`http://127.0.0.1:*` when returning
        an explicit list — caught while writing Phase 4's docs, not by a test:
        fastmcp's `validate_redirect_uri` gives loopback URIs **no automatic
        exemption** once `allowed_patterns` is a real list (confirmed by reading
        it) — unlike the old hand-rolled `provider.py`, which explicitly allowed
        loopback http regardless of the https host allowlist (`_pending()`'s own
        test cases: `http://localhost:41000/cb`, `http://127.0.0.1:8080/cb`).
        Without these two entries, a native/local MCP client could no longer
        register against a relay with the default (non-empty) redirect-host
        allowlist — a real regression, not a hypothetical one. Same two patterns
        as fastmcp's own `redirect_validation.DEFAULT_LOCALHOST_PATTERNS`,
        hand-copied rather than imported so this settings module doesn't need to
        know about fastmcp's types.
        """
        hosts = self.mcp_redirect_hosts
        wildcards = self.mcp_redirect_host_wildcards
        if not hosts and not wildcards:
            return None
        return (
            ["http://localhost:*", "http://127.0.0.1:*"]
            + [f"https://{h}/*" for h in sorted(hosts)]
            + [f"https://*.{w}/*" for w in sorted(wildcards)]
        )

    @property
    def relay_dir(self) -> str:
        """Hidden control folder inside the vault (index DB + tag config)."""
        return str(Path(self.vault_path) / ".relay")

    @property
    def history_dir(self) -> str:
        """Git dir for the vault history.

        Inside ``.relay/`` on purpose: that path is already excluded from
        Syncthing and hidden from Obsidian, so the object store never syncs
        between machines (a reliable way to corrupt a repo) and never shows up as
        vault content. The work-tree is the vault itself, passed explicitly, so
        no ``.git`` entry is created in the vault root. Durable, unlike the index
        beside it — the startup rebuild must never touch this.
        """
        return str(Path(self.relay_dir) / "history.git")

    @property
    def database_path(self) -> str:
        """Derived index DB path. The index is disposable — files are canonical."""
        return str(Path(self.relay_dir) / "index.db")

    @property
    def tags_config_path(self) -> str:
        return str(Path(self.relay_dir) / "tags.yml")

    @property
    def embedding_cache_dir(self) -> str:
        """FastEmbed's downloaded ONNX model cache.

        Under ``.relay/`` so it rides the vault dir — guaranteed writable by the
        runtime UID the same way ``uploads_dir``/``database_path`` already are
        (relay's container runs as an arbitrary host UID with no matching
        ``/etc/passwd`` entry, so huggingface_hub's HOME-based default resolves
        to an unwritable path otherwise) — and persists across container
        restarts instead of re-downloading a few hundred MB every time.
        """
        return str(Path(self.relay_dir) / "models")

    @property
    def mcp_oauth_storage_dir(self) -> str:
        """Persistent OAuth store — DCR client registrations and encrypted upstream
        tokens (relay #313 Phase 2/4: fastmcp's `DiskStore`/`diskcache`, replacing
        the old hand-rolled `mcp_oauth/store.py`'s single-file SQLite `oauth.db`
        with a directory of its own).

        Separate from the disposable ``index.db`` — the startup index rebuild must
        never touch it. Lives in ``.relay/`` so it rides the vault backup.
        """
        return str(Path(self.relay_dir) / "mcp_oauth")

    @property
    def mcp_resource_url(self) -> str:
        """RFC 8707 resource identifier for the MCP endpoint (token audience)."""
        return f"{self.relay_base_url.rstrip('/')}/mcp"


settings = Settings()
