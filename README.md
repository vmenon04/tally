# tally

[![CI](https://github.com/vmenon04/tally/actions/workflows/ci.yml/badge.svg)](https://github.com/vmenon04/tally/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
![Python 3.13](https://img.shields.io/badge/python-3.13-blue.svg)

Personal finance tracker. Imports transactions, sorts them into budget categories, and keeps a
Beancount ledger up to date. [Sure](https://github.com/we-promise/sure) is the web and phone
UI, and its chat is answered from the ledger.

```sh
uv sync
uv run tally --help
```

## Docs

- [Importing bank exports](docs/importing.md)
- [Alert emails and notifications](docs/alerts.md)
- [Weekly summary](docs/summary.md)
- [The Beancount ledger](docs/ledger.md)
- [Architecture](docs/architecture.md)
- [Running on a server](docs/server.md)
- [Development](docs/development.md)
