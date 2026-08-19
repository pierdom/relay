# Security Policy

## Reporting a Vulnerability

Please **do not** open a public GitHub issue for security vulnerabilities.

Report privately by emailing **security+relay@fiadino.org**. Include:
- A description of the vulnerability and its impact
- Steps to reproduce or a proof-of-concept (if safe to share)
- Any suggested mitigations you've identified

You'll receive an acknowledgement within 48 hours and a resolution timeline once the issue is assessed. If a CVE is warranted, one will be requested after a fix is ready.

## Scope

The following are in scope:

- **REST API** (`/posts`, `/tags`, `/attachments`, `/events`, `/status`, `/metrics`)
- **MCP endpoint** (`/mcp`) and the stdio proxy (`relay-mcp`)
- **OAuth 2.1 authorization server** (`/mcp/oauth/*`) — DCR, PKCE, token handling
- **OIDC login** (`/auth/*`) and session management
- **Attachment transports** — `source_url` SSRF, upload-slot handling
- **Authentication bypass** — bearer token, session cookie, allowlist enforcement

The following are **out of scope**:

- Issues requiring physical access to the server
- Social engineering or phishing
- Browser UI cosmetic issues
- Vulnerabilities in third-party OIDC providers (e.g. PocketID)

## Supported Versions

Only the latest release receives security fixes. Patch releases are cut promptly for confirmed vulnerabilities.
