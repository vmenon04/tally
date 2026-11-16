# tally, with Claude Code for categorizing and the chat. Published to ghcr.io/vmenon04/tally by
# .github/workflows/ci.yml. It holds no data or secrets: mount them at /data and /secrets.
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


FROM base AS claude
# Claude Code's own build for Linux, checked against its published checksum.
ARG CLAUDE_VERSION=2.1.325
ARG TARGETARCH
ADD --checksum=sha256:0dc4f4a9a165fc60746d7ae8489712e5fc352f1e7ad4eb22e7141bd0812eb0cf \
    https://downloads.claude.ai/claude-code-releases/${CLAUDE_VERSION}/linux-x64/claude /claude-amd64
ADD --checksum=sha256:7b6e484b1989a5a14a4b354709ccc8672518a3ec5b9ccd7981fc6254327fde67 \
    https://downloads.claude.ai/claude-code-releases/${CLAUDE_VERSION}/linux-arm64/claude /claude-arm64
RUN install -m 755 "/claude-${TARGETARCH}" /usr/local/bin/claude


FROM base
# Links the published image to this repo on GitHub.
LABEL org.opencontainers.image.source=https://github.com/vmenon04/tally
# git keeps the ledger's history; ssh pushes its backup to GitHub.
RUN apt-get update \
    && apt-get install -y --no-install-recommends git openssh-client \
    && rm -rf /var/lib/apt/lists/*
COPY --from=build /app/.venv /app/.venv
COPY --from=claude /usr/local/bin/claude /usr/local/bin/claude
RUN useradd --uid 10001 --user-group --create-home --home-dir /home/tally tally
USER tally
ENV PATH=/app/.venv/bin:$PATH \
    TALLY_DATA_DIR=/data \
    TALLY_SECRETS_DIR=/secrets \
    DISABLE_AUTOUPDATER=1
WORKDIR /data
ENTRYPOINT ["tally"]
CMD ["--help"]
