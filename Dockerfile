# Both bases are pinned by digest (dependabot's docker ecosystem refreshes them),
# so a rebuild is reproducible and a compromised or yanked tag can't change what
# ships. uv is its own stage rather than a bare `COPY --from=` so dependabot can
# see it too.
FROM ghcr.io/astral-sh/uv:0.8.17@sha256:e4644cb5bd56fdc2c5ea3ee0525d9d21eed1603bccd6a21f887a938be7e85be1 AS uv
FROM python:3.13-slim@sha256:9d2e5553305c7c7b0097999bb17187c69b921ccd6bc9d40e4bb5ebe652c00285

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
