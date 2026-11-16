# Development

## Setup

```sh
uv sync
git config core.hooksPath .githooks
```

- `uv sync`: Install Python and dev tools.
- `git config core.hooksPath .githooks`: Enable the pre-commit checks (once per clone)

## Layout

| Path | What | In git |
|---|---|---|
| `src/tally/` | Application code | yes |
| `tests/` | Tests. Fixtures use made-up data only | yes |
| `example/` | The starter data folder new users copy to `data/` | yes |
| `demo/make_demo.py` | Makes a data folder of made-up money, run through tally's own sync | yes |
| `bin/tally-remote` | Runs tally on the server from your machine | yes |
| `bin/check` | The checks every commit must pass | yes |
| `deploy/` | Server scripts, sudoers rule, systemd units and Sure's compose file ([Running on a server](server.md)) | yes |
| `Dockerfile` | The image CI publishes, below | yes |
| `docs/` | These docs | yes |
| `secrets/` | API keys and tokens (`chmod 700`) | **never** |
| `data/` | Database, ledger, config, raw SimpleFIN answers | **never** |

## Checks

The pre-commit hook and CI both run [`bin/check`](../bin/check): ruff (lint and format), mypy
and the tests.

```sh
bin/check
```

## The image

CI also builds a Docker image of tally, with Claude Code, and checks it runs. On `main`, once
the checks pass, it publishes it as `ghcr.io/vmenon04/tally:<commit>`. The image holds no data
or secrets ([`.dockerignore`](../.dockerignore) lets in only the code); mount them at `/data`
and `/secrets`:

```sh
docker build --tag tally .
docker run --rm -v "$PWD/data:/data" -v "$PWD/secrets:/secrets" tally summary
```
