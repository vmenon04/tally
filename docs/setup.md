# Setting it up for yourself

tally was built for one person's accounts, but nothing personal is in the code: your
settings live in `data/config.toml`, your secrets in `secrets/`, and your accounts and
categories in your ledger. Neither folder is ever committed.

## What it can read

- **Bank and card accounts** through [SimpleFIN Bridge](https://bridge.simplefin.org) (paid):
  any bank it connects to. Rows are read the way Bank of America checking and Discover
  word them; another bank needs its own small row reader in `src/tally/simplefin.py` (and,
  for CSVs, `src/tally/importers/`).
- **CSV exports** from Bank of America checking and Discover, for accounts SimpleFIN doesn't
  fill.
- **Fidelity holdings** through [SnapTrade](https://snaptrade.com) (free personal key), or
  Fidelity's positions CSV.

## Settings: `data/config.toml`

| Key | What | Needed for |
|---|---|---|
| `gmail_account` | The Gmail address the weekly summary is sent from | `summary --email` |
| `summary_to` | Where the weekly summary goes | `summary --email` |
| `roth_limit`, `roth_elsewhere` | This year's Roth IRA limit, and what you put in where tally can't see | The Roth IRA check |
| `ntfy_server` | Your own ntfy server (default `https://ntfy.sh`) | Notifications, optional |
| `sure_url` | Where Sure listens (default `http://127.0.0.1:3200`) | Sure, optional |

`TALLY_DATA_DIR` and `TALLY_SECRETS_DIR` move the two folders (default `data/` and
`secrets/` in the current directory). `TALLY_HOST`, in your environment or the repo's `.env`
(copy `.env.example`), names your server for `bin/tally-remote`.

## Secrets: `secrets/`

Each is a file only you can read (600, in a 700 folder), never an environment variable, and
made by a command that checks it works first, so you never edit one by hand:

| File | Made by | For |
|---|---|---|
| `simplefin-access-url` | `tally simplefin-login` | Bank and card accounts |
| `snaptrade-client-id`, `snaptrade-consumer-key` | You, from SnapTrade's dashboard | Fidelity holdings |
| `gmail-app-password` | `tally gmail-login` | Sending the weekly summary |
| `ntfy-topic` | `tally ntfy-setup` | Notifications |
| `claude-token` | You, from `claude setup-token` | Claude's categories, review and chat |
| `sure-api-key` | `tally sure-login` | Filling Sure |
| `chat-token`, `sure-mcp-token` | `deploy/sure/install` | Sure's chat, and its lookups |
| `forgejo-token` | You, from Forgejo | Pushing the ledger to its repo |
| `github-backup-key` | You, with `ssh-keygen` ([The ledger](ledger.md#backing-it-up-to-github)) | A backup of the ledger in a **private** GitHub repo; tally refuses to push to a public one |

Every one is optional: tally does what it can with the ones it has.

## Your accounts and categories: the ledger

Accounts, categories, your budget and goals are in the ledger's open directives and
`plan.beancount` ([The Beancount ledger](ledger.md)): `tally-account:` and
`simplefin-account:` say where rows come from, `category:` where they go, `sure-account:`
which Sure account shows them. Rules for naming merchants' categories are in
`data/rules.toml`.

## On a server

[Running on a server](server.md) sets it up under a `tally` user, with systemd timers. The
units in `deploy/` use the `America/New_York` time zone and paths under `/srv/tally`; change
them for yours.
