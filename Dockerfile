# tally, with Claude Code for categorizing and the chat, and supercronic for scheduled runs.
# Published to ghcr.io/vmenon04/tally by .github/workflows/ci.yml. It holds no data or
# secrets: mount them at /data and /secrets.
#
#     docker run --rm -v "$PWD/data:/data" -v "$PWD/secrets:/secrets" ghcr.io/vmenon04/tally sync

FROM python:3.13.18-slim-bookworm AS base


FROM base AS build
COPY --from=ghcr.io/astral-sh/uv:0.8.1 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /app
# The dependencies first, so a change to tally's code alone reuses this layer.
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project
COPY README.md LICENSE ./
COPY src ./src
RUN uv sync --locked --no-dev --no-editable


FROM base AS tools
# Claude Code's own build for Linux, checked against its published checksum.
ARG CLAUDE_VERSION=2.1.325
ARG TARGETARCH
ADD --checksum=sha256:0dc4f4a9a165fc60746d7ae8489712e5fc352f1e7ad4eb22e7141bd0812eb0cf \
    https://downloads.claude.ai/claude-code-releases/${CLAUDE_VERSION}/linux-x64/claude /claude-amd64
ADD --checksum=sha256:7b6e484b1989a5a14a4b354709ccc8672518a3ec5b9ccd7981fc6254327fde67 \
    https://downloads.claude.ai/claude-code-releases/${CLAUDE_VERSION}/linux-arm64/claude /claude-arm64
RUN install -m 755 "/claude-${TARGETARCH}" /usr/local/bin/claude
# supercronic runs the scheduled commands in a container: `supercronic <crontab>`.
ARG SUPERCRONIC_VERSION=0.2.51
ADD --checksum=sha256:cc232108374e8e2b537045f2d5eaa1229f619db314c3ba6a14d38b35dc0aa123 \
    https://github.com/aptible/supercronic/releases/download/v${SUPERCRONIC_VERSION}/supercronic-linux-amd64 /supercronic-amd64
ADD --checksum=sha256:4ae22576a78223d8ccdf5f3b3660a5cbb5288416bf0ca96afc4a36d195932b94 \
    https://github.com/aptible/supercronic/releases/download/v${SUPERCRONIC_VERSION}/supercronic-linux-arm64 /supercronic-arm64
RUN install -m 755 "/supercronic-${TARGETARCH}" /usr/local/bin/supercronic


FROM base
# Links the published image to this repo on GitHub.
LABEL org.opencontainers.image.source=https://github.com/vmenon04/tally
# git keeps the ledger's history; ssh pushes its backup to GitHub.
RUN apt-get update \
    && apt-get install -y --no-install-recommends git openssh-client \
    && rm -rf /var/lib/apt/lists/*
COPY --from=build /app/.venv /app/.venv
COPY --from=tools /usr/local/bin/claude /usr/local/bin/supercronic /usr/local/bin/
RUN useradd --uid 10001 --user-group --create-home --home-dir /home/tally tally
USER tally
ENV PATH=/app/.venv/bin:$PATH \
    TALLY_DATA_DIR=/data \
    TALLY_SECRETS_DIR=/secrets \
    DISABLE_AUTOUPDATER=1
WORKDIR /data
ENTRYPOINT ["tally"]
CMD ["--help"]
