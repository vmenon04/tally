# tally

[![CI](https://github.com/vmenon04/tally/actions/workflows/ci.yml/badge.svg)](https://github.com/vmenon04/tally/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
![Python 3.13](https://img.shields.io/badge/python-3.13-blue.svg)

A personal finance tracker that keeps your money in a plain-text
[Beancount](https://beancount.github.io/) ledger you own.

- Brings in your bank and card transactions through
  [SimpleFIN Bridge](https://bridge.simplefin.org), including pending ones.
- Sorts them into your budget categories with simple rules, or with Claude if you have
  [Claude Code](https://claude.com/claude-code).
- Checks itself: every transaction once, and each account's balance against the bank's.
- Shows the ledger in [Fava](https://beancount.github.io/fava/), and writes a weekly summary of
  your spending against your plan.

Optional extras: an emailed weekly summary, push notifications to your phone, a web and phone UI
through [Sure](https://github.com/we-promise/sure), and running it all on a home server.

## Get started

You need git, [uv](https://docs.astral.sh/uv/) and a SimpleFIN Bridge account.

```sh
git clone https://github.com/vmenon04/tally.git
cd tally
uv sync
cp -R example data
```

Then follow [Getting started](docs/getting-started.md) to link your accounts and run your first
sync. It takes about half an hour.

## Docs

- [Getting started](docs/getting-started.md): set it up on your own computer
- [Reference](docs/reference.md): commands, settings, secrets, ledger metadata, rules
- [Where transactions come from](docs/transactions.md)
- [The Beancount ledger](docs/ledger.md): categories, your plan, goals, investments, Sure
- [Weekly summary](docs/summary.md)
- [Pending purchases and notifications](docs/notifications.md)
- [Running on a server](docs/server.md): scheduled syncs, Sure, backups
- [Architecture](docs/architecture.md) and [Development](docs/development.md)

I built tally for my banks and my server, but nothing about them is in the code: your settings,
secrets and accounts all live in your own `data/` and `secrets/` folders. It's shared as is,
under the MIT license. There are no releases; `main` is what's current.
