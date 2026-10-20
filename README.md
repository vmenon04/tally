# tally

Personal finance tracker. Imports transactions, sorts them into budget categories, and keeps a Google Sheet budget up to date.

## Setup

```sh
uv sync                                # install Python and dev tools
git config core.hooksPath .githooks    # enable the pre-commit checks (once per clone)
```

## Importing bank exports

Download the CSVs (Discover: Recent Activity or a statement; BofA checking: any date range),
put them in `data/inbox/`, then:

```sh
uv run tally import --dry-run   # show what would be added, save nothing
uv run tally import             # store new rows, archive each file to data/raw/, empty the inbox
```

The bank is recognized from the file's contents, so file names don't matter. Overlapping
downloads are fine: rows already stored are skipped. A file that fails (unknown layout, running
balances that don't add up, a gap in BofA history) is not stored and stays in the inbox.
`uv run tally import FILE...` imports specific files and leaves them where they are.

`TALLY_DATA_DIR` sets the data folder (default: `data` in the current directory).

## Reports and the Google Sheet

`data/rules.toml` sorts transactions into categories (the format is described at the top of
`src/tally/rules.py`). `uv run tally report 2026-10` shows a month's spending per category.

`uv run tally sync --dry-run` reads the Budget tab of the Sheet named in `data/config.toml`
(`sheet_id = "..."`) and lists the cells it would fill in. It signs in as a Google service
account whose key is `secrets/google-service-account.json`; share only the Sheet with that
account. `TALLY_SECRETS_DIR` sets the secrets folder (default: `secrets`).

## Layout

| Path | What | In git |
|---|---|---|
| `src/tally/` | Application code | yes |
| `tests/` | Tests. Fixtures use made-up data only | yes |
| `secrets/` | API keys and tokens (`chmod 700`) | **never** |
| `data/` | Database, bank CSVs, config | **never** |

## Checks

The pre-commit hook and CI run the same checks:

```sh
uv run ruff check && uv run ruff format --check && uv run mypy && uv run pytest
```
