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

## Alert emails

Discover "Transaction Alert" and BofA "Your debit card was used" emails, saved as `.eml`
files in `data/alerts/`, are read with:

```sh
uv run tally alerts --dry-run   # show what would be stored, save nothing
uv run tally alerts             # store each alert once (by Message-ID); the files stay put
```

An email is only accepted when Gmail's `Authentication-Results` header shows the bank signed
it. Alerts are provisional (the amount can change before it posts) and are kept apart from
imported transactions. The command matches alerts to imported CSV rows (same account, dated
a day before to three days after, agreeing merchant name, same amount or up to 30% more for
a tip; the full rules are at the top of `src/tally/matching.py`) and lists the rest as
*waiting* (the CSVs don't reach that date yet) or *not found* (they do, but have no such row).

### Reading the alerts mailbox

Instead of saving emails by hand, tally can read a separate Gmail account that receives only
the bank alerts (a filter in your main Gmail forwards them). Put its address in
`data/config.toml` as `gmail_account = "..."`, turn on 2-Step Verification for that account,
create an app password for it at myaccount.google.com/apppasswords, then:

```sh
uv run tally gmail-login          # once: type the app password; saved to secrets/gmail-app-password
uv run tally alerts --gmail       # fetch new bank emails over IMAP, store their alerts
```

gmail-login only saves a password Gmail accepts, and only something shaped like an app
password (never your account password). The mailbox is opened read-only, so nothing in it is
marked, moved or deleted. Each email read is kept in `data/alerts/gmail/`, so it's never
fetched twice; a refused email isn't kept, so it's reported on every run.

## Reports and the Google Sheet

`data/rules.toml` sorts transactions into categories (the format is described at the top of
`src/tally/rules.py`). `uv run tally report 2026-10` shows a month's spending per category.

`uv run tally sync` fills in the Budget tab of the Sheet named in `data/config.toml`
(`sheet_id = "..."`): each month's category actuals, and actual Roth IRA contributions up to
the current month. `--dry-run` lists the cells it would change without changing them. It
never overwrites a value it didn't write itself (such as one you typed); it lists those and
writes nothing until you clear them. It also rewrites a Transactions tab (created on the
first sync) listing every transaction, newest first, with its category. Changing a category
there is how you correct one: the next sync saves it as a rule for that one transaction in a
marked section at the top of `rules.toml` (clearing the category removes it again), and
suggests a merchant-wide rule once you've corrected the same merchant the same way twice.
Rows added to that tab by hand stop the sync. It signs in as a Google service
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
