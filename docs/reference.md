# Reference

Nothing personal is in the code. Your settings live in `data/config.toml`, your secrets in
`secrets/`, and your accounts, categories and plan in your ledger. Git never commits `data/`
or `secrets/`.

## Commands

| Command | What it does |
|---|---|
| `tally sync` | Brings in transactions, sorts them, checks the ledger, sends notifications. `--dry-run` changes nothing |
| `tally summary` | The weekly summary. `--email` sends it, `--sync` syncs first, `--as-of DATE` shows an earlier week |
| `tally fava` | Opens the ledger in Fava at http://localhost:5000 |
| `tally simplefin` | What SimpleFIN sees: accounts, balances, recent transactions. `--transactions 1234` lists one account's, `--extra 1234` shows what tally doesn't read |
| `tally simplefin-login` | Saves your SimpleFIN access from a setup token |
| `tally ntfy-setup`, `tally notify-test` | Turn on [push notifications](notifications.md), and test them |
| `tally gmail-login` | Saves the Gmail app password the [weekly summary](summary.md#by-email) is sent with |
| `tally sure-login` | Saves [Sure](server.md#sure)'s API key |
| `tally chat`, `tally lookups` | Answer Sure's chat ([Running on a server](server.md#sures-chat)) |

Run them as `uv run tally ...` from the repo, or in the container on a
[server](server.md).

## Settings: `data/config.toml`

Every key is optional.

| Key | What | Used by |
|---|---|---|
| `gmail_account` | The Gmail address the weekly summary is sent from | `summary --email` |
| `summary_to` | Where the weekly summary goes | `summary --email` |
| `roth_limit`, `roth_elsewhere` | This year's Roth IRA limit, and what you put in where tally can't see | The summary's Roth IRA check |
| `ntfy_server` | Your own ntfy server (default `https://ntfy.sh`) | Notifications |
| `sure_url` | Where Sure listens (default `http://127.0.0.1:3200`) | Sure |

## Environment

| Variable | What |
|---|---|
| `TALLY_DATA_DIR` | The data folder (default `data/` in the current directory) |
| `TALLY_SECRETS_DIR` | The secrets folder (default `secrets/` in the current directory) |

## Secrets: `secrets/`

Each is a file only you can read (600, in a 700 folder), made by a command that checks it works
first, so you never edit one by hand. Every one is optional: tally does what it can with the
ones it has.

| File | Made by | For |
|---|---|---|
| `simplefin-access-url` | `tally simplefin-login` | Bank and card accounts |
| `ntfy-topic` | `tally ntfy-setup` | Notifications |
| `gmail-app-password` | `tally gmail-login` | Emailing the weekly summary |
| `claude-token` | You, from `claude setup-token` | Claude, on a machine where Claude Code isn't logged in |
| `sure-api-key` | `tally sure-login` | Filling Sure |
| `chat-token`, `sure-mcp-token` | `deploy/sure/install` | Sure's chat, and its lookups |
| `forgejo-token` | You, from your git host | Pushing the ledger to its own repo ([server](server.md#the-ledgers-own-repo)) |
| `github-backup-key` | You, with `ssh-keygen` | A backup of the ledger in a private GitHub repo ([server](server.md#backing-the-ledger-up-to-github)) |

## Ledger metadata

These go on `open` lines in `accounts.beancount` ([The ledger](ledger.md)).

| Key | Means |
|---|---|
| `tally-account: "checking"` | This account's transactions post here. Any name; `bofa_checking` and `discover` get extra merchant cleanup |
| `simplefin-account: "1234"` | It's SimpleFIN's account ending 1234: its rows come in (with `tally-account`) and its balance is checked |
| `interest-from: "Income:Interest"` | For an account with `simplefin-account` and no `tally-account`: bring in its interest only |
| `category: "Groceries"` | Transactions in this category post their other side here |
| `merchant-contains: "A\|B"` | With `category`: only merchants containing one of these |
| `fixed: TRUE` | A bill that comes once a month: the summary counts it in full, not a little each day |
| `fidelity-account: "1234"` | Record this Fidelity account's holdings ([investments](ledger.md#investment-holdings)) |
| `contributions-from: "Income:Salary:401k"` | Where a 401(k)'s payroll contributions come from |
| `sure-account: "Checking"` | The Sure account this one fills |

And in `plan.beancount`:

| Line | Means |
|---|---|
| `custom "budget" Expenses:Groceries "monthly" 300.00 USD` | A monthly budget, until a later line for the same account |
| `custom "paychecks" 2` | Paychecks that month |
| `custom "roth-plan" 600.00 USD` | A Roth IRA transfer planned that month |
| `custom "goal" "Trip" Assets:Bank:Savings "target" 1200.00 USD` | A savings goal (or `"months" 3`, of usual spending) |
| `custom "about" "Saving for a trip"` | Something Claude's monthly review should know about you |

## Rules: `data/rules.toml`

```toml
categories = ["Groceries", "Eating out"]   # your budget categories

[[rule]]
match = "MAPLE MARKET"     # the merchant contains this (any case); or merchant = the whole name
category = "Groceries"
# optional: account = "checking", date = 2026-09-29, amount = -50.00
```

The first rule that matches wins, so put one-offs first. Besides your categories, rules can use
Income, Card payment, Savings transfer and Investing. Every category, those four and
Uncategorized need an account with that `category:` in the ledger.
