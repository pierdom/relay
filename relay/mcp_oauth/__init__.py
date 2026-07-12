"""Remote MCP OAuth — relay as its own OAuth 2.1 Authorization Server.

When ``MCP_OAUTH_ENABLED`` is set, relay advertises itself as the Authorization
Server for the ``/mcp`` Resource Server (RFC 9728), supports Dynamic Client
Registration (RFC 7591) + PKCE, and brokers the human login upstream to PocketID
(reusing the Phase-1 OIDC client). Tokens are audience-bound to ``/mcp`` (RFC
8707) and stored server-side, hashed, in ``.relay/oauth.db`` — separate from the
disposable index. The static ``API_KEY`` remains a full-power bearer via the
verifier, so enabling the flag is backward-compatible.

Design: relay post #201.
"""
