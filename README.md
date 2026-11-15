# tally

[![CI](https://github.com/vmenon04/tally/actions/workflows/ci.yml/badge.svg)](https://github.com/vmenon04/tally/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
![Python 3.13](https://img.shields.io/badge/python-3.13-blue.svg)

Personal finance tracker. Imports transactions, sorts them into budget categories, and keeps a
Beancount ledger up to date. [Sure](https://github.com/we-promise/sure) is the web and phone
UI, and its chat is answered from the ledger.

This is a personal tool, built for one person's banks, accounts and server, and shared as is.
It isn't meant for general use: there are no releases, no support, and it changes whenever its
owner needs it to. You're welcome to read it or borrow from it under the MIT license.

```sh
uv sync
uv run tally --help
```

## Docs

- [Importing bank exports](docs/importing.md)
- [Pending purchases and notifications](docs/notifications.md)
- [Weekly summary](docs/summary.md)
- [The Beancount ledger](docs/ledger.md)
- [Architecture](docs/architecture.md)
- [Running on a server](docs/server.md)
- [Development](docs/development.md)
