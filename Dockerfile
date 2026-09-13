# Both bases are pinned by digest (dependabot's docker ecosystem refreshes them),
# so a rebuild is reproducible and a compromised or yanked tag can't change what
# ships. uv is its own stage rather than a bare `COPY --from=` so dependabot can
# see it too.
FROM ghcr.io/astral-sh/uv:0.12.12@sha256:73d2665b478d8fa2de1cf105c6841f8e9cb6b09e568fc7700440c09f8fcd7ac4 AS uv
FROM python:3.14-slim@sha256:cad9a2c871761c413caa6fdd6441c783451e740a48aaeba60ae62a8b53525ef6

# git backs the vault history (a commit per write, see relay/history.py). Without
# it relay still runs — history disables itself with a warning — but every write
# would be unrecoverable, so it ships in the image.
RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

COPY --from=uv /uv /usr/local/bin/uv

WORKDIR /app

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PATH="/app/.venv/bin:$PATH"

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project

COPY relay/ ./relay/
RUN uv sync --frozen

# Never root at runtime, even under a plain `docker run` (compose additionally
# sets `user:` to the host uid so vault files stay user-owned). Nothing under
# /app is written after build; every runtime write goes to the vault volume.
USER 1000:1000

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')"

CMD ["uvicorn", "relay.main:app", "--host", "0.0.0.0", "--port", "8000"]
