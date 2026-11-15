# tally

[![CI](https://github.com/vmenon04/tally/actions/workflows/ci.yml/badge.svg)](https://github.com/vmenon04/tally/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
![Python 3.13](https://img.shields.io/badge/python-3.13-blue.svg)

Personal finance tracker. Imports transactions, sorts them into budget categories, and keeps a
Beancount ledger up to date. [Sure](https://github.com/we-promise/sure) is the web and phone
UI, and its chat is answered from the ledger.

It was built for one person's banks and server, but your own settings, secrets and accounts
all live outside the code, so you can run it for yours: see [Setting it up for
yourself](docs/setup.md). It's shared as is, under the MIT license: there are no releases, and
it changes when its owner needs it to.

```sh
uv sync
uv run tally --help
```

## Docs

- [Setting it up for yourself](docs/setup.md)
- [Importing bank exports](docs/importing.md)
- [Pending purchases and notifications](docs/notifications.md)
- [Weekly summary](docs/summary.md)
- [The Beancount ledger](docs/ledger.md)
- [Architecture](docs/architecture.md)
- [Running on a server](docs/server.md)
- [Development](docs/development.md)
