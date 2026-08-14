"""Zero-dependency in-process metrics for the Prometheus/OpenMetrics text format.

relay already avoids heavyweight deps (FTS5 over a vector store, hand-rolled
OAuth over a framework), so this stays in the same spirit: a tiny counter
registry + a text renderer, no ``prometheus_client``. Counters live for the
process lifetime (reset on restart, like the disposable index); gauges that
reflect current state (post/tag totals, live SSE clients) are sampled at scrape
time by ``relay.routes.metrics`` and merged in, so they're always exact rather
than drifting event counters.

Increments happen from the asyncio event loop (HTTP handlers, MCP tools, the
cleanup loop) and potentially the watcher thread, so mutation is guarded by a
lock — cheap, and correct regardless of which thread touches a counter.
"""
from __future__ import annotations

import threading

# A metric "family" as passed to the renderer:
#   (name, help_text, type, [(labels_dict, value), ...])
Family = tuple[str, str, str, list[tuple[dict[str, str], float]]]


class Counter:
    """A monotonically increasing counter, optionally partitioned by labels."""

    def __init__(self, name: str, documentation: str, labelnames: tuple[str, ...] = ()) -> None:
        self.name = name
        self.documentation = documentation
        self.labelnames = labelnames
        self._values: dict[tuple[str, ...], float] = {}
        self._lock = threading.Lock()

    def inc(self, amount: float = 1.0, **labels: str) -> None:
        key = tuple(str(labels.get(name, "")) for name in self.labelnames)
        with self._lock:
            self._values[key] = self._values.get(key, 0.0) + amount

    def family(self) -> Family:
        with self._lock:
            samples = [
                (dict(zip(self.labelnames, key, strict=True)), value)
                for key, value in self._values.items()
            ]
        return (self.name, self.documentation, "counter", samples)


# ── Counter instances (process-lifetime) ──────────────────────────────────────

# `path` is the matched route template (e.g. /posts/{post_id}), not the raw URL,
# so cardinality stays bounded by the number of routes. See routes/metrics helper.
http_requests = Counter(
    "relay_http_requests_total",
    "HTTP requests served, by route template, method, and status code.",
    ("method", "path", "status"),
)
mcp_tool_calls = Counter(
    "relay_mcp_tool_calls_total",
    "In-process MCP (Streamable HTTP /mcp) tool invocations, by tool name.",
    ("tool",),
)
search_queries = Counter(
    "relay_search_queries_total",
    "list_posts calls that ran a full-text (or LIKE fallback) search.",
)
cleanup_deletions = Counter(
    "relay_cleanup_deletions_total",
    "Posts deleted by the TTL cleanup loop.",
)
upload_slots_purged = Counter(
    "relay_upload_slots_purged_total",
    "Expired presigned upload slots swept by the cleanup loop.",
)


def record_tool_call(tool: str) -> None:
    """Count one in-process MCP tool invocation. Called from ``relay.mcp_server``."""
    mcp_tool_calls.inc(tool=tool)


# ── Rendering ──────────────────────────────────────────────────────────────────


def _fmt_value(value: float) -> str:
    # Emit whole numbers without a trailing .0 for readable output; Prometheus
    # accepts either, but counts are integers in practice.
    return str(int(value)) if value == int(value) else repr(value)


def _fmt_labels(labels: dict[str, str]) -> str:
    if not labels:
        return ""
    parts = []
    for key, raw in labels.items():
        escaped = raw.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
        parts.append(f'{key}="{escaped}"')
    return "{" + ",".join(parts) + "}"


def render(families: list[Family]) -> str:
    """Render metric families to the Prometheus 0.0.4 text exposition format.

    Widely compatible: both Prometheus and Telegraf's ``inputs.prometheus`` parse
    it, and it degrades cleanly for OpenMetrics consumers.
    """
    lines: list[str] = []
    for name, documentation, typ, samples in families:
        lines.append(f"# HELP {name} {documentation}")
        lines.append(f"# TYPE {name} {typ}")
        for labels, value in samples:
            lines.append(f"{name}{_fmt_labels(labels)} {_fmt_value(value)}")
    return "\n".join(lines) + "\n"


def build_info_family() -> Family:
    """A constant ``relay_build_info{version="…"} 1`` gauge (Prometheus idiom for
    surfacing the running version as a scrapeable label)."""
    from . import __version__

    return (
        "relay_build_info",
        "Relay build information; the value is always 1.",
        "gauge",
        [({"version": __version__}, 1.0)],
    )


def gauge(name: str, documentation: str, value: float, labels: dict[str, str] | None = None) -> Family:
    """Build a single-sample gauge family for a scrape-time value."""
    return (name, documentation, "gauge", [(labels or {}, value)])
