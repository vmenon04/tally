# Development

## Setup

```sh
uv sync                                # install Python and dev tools
git config core.hooksPath .githooks    # enable the pre-commit checks (once per clone)
```

## Layout

| Path | What | In git |
|---|---|---|
| `src/tally/` | Application code | yes |
| `tests/` | Tests. Fixtures use made-up data only | yes |
| `bin/tally-remote` | Runs tally on the server from your machine | yes |
| `deploy/` | Server scripts, sudoers rule and systemd units ([Running on a server](server.md)) | yes |
| `docs/` | These docs | yes |
| `secrets/` | API keys and tokens (`chmod 700`) | **never** |
| `data/` | Database, bank CSVs, config | **never** |

## Checks

The pre-commit hook and CI run the same checks:

```sh
uv run ruff check && uv run ruff format --check && uv run mypy && uv run pytest
```
