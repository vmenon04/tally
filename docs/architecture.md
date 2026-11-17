# Architecture

This is how the pieces fit together when tally runs on a home server, the way I run it; on
your own computer it's the same minus Docker, Sure and the ledger's repo, all optional. What
tally knows about your accounts comes from SimpleFIN Bridge: posted transactions, pending ones,
each account's balance, and brokerage holdings. It keeps a Beancount ledger (edited with Fava,
optionally pushed to its own git repo), fills Sure from it, emails a weekly summary and sends
headline-only push notifications.

## The whole system

```mermaid
flowchart LR
    subgraph banks["Banks"]
        bank["Your banks and cards"]
        fidelity["Brokerage"]
    end

    simplefin["SimpleFIN Bridge<br/>read-only, 24 requests a day"]

    subgraph server["Home server"]
        timer["supercronic<br/>sync every 2 hours"]
        tally["tally<br/>container, user: tally"]
        data[("/srv/tally/data<br/>tally.db, rules.toml, config.toml,<br/>raw answers, ledger/ (git clone)")]
        secrets[("/srv/tally/secrets<br/>SimpleFIN access, app password,<br/>ntfy topic, Claude token, git token")]
        forgejo["Git host, e.g. Forgejo<br/>ledger repo"]
        sure["Sure<br/>web and phone UI"]
    end

    subgraph mac["Your computer"]
        fava_view["Browser<br/>Fava, over an SSH tunnel, to edit"]
    end

    gmail["Gmail<br/>sends the weekly summary"]
    ntfy["ntfy.sh<br/>random 128-bit topic"]
    phone["Phone<br/>ntfy app"]

    bank --> simplefin
    fidelity --> simplefin
    timer --> tally
    simplefin -- "HTTPS, Basic auth" --> tally
    tally <--> data
    secrets --> tally
    tally -- "commit and push the ledger" --> forgejo
    tally -- "API, on this machine" --> sure
    data -. "tally fava: SSH tunnel" .-> fava_view
    tally -- "weekly summary, SMTP" --> gmail
    tally -- "headlines only: counts, no merchants or amounts" --> ntfy
    ntfy --> phone
```

Solid lines run on their own; dotted lines are steps you do by hand.

## Trust boundaries

| Boundary | What crosses it | What protects it |
|---|---|---|
| Banks → SimpleFIN → tally | Transactions and balances | SimpleFIN holds the bank connections; tally holds only an access URL, read-only, in secrets/, never printed or logged. Its rows are checked against what's stored (same posted date and amount) and each account's balance against the ledger's. |
| tally → the ledger's repo | The ledger | A token for the one repo, in secrets/, given to git through its environment (never its command line or config). |
| Fava → you | The ledger, read and edit | Runs only while you have it open, on the server's loopback; an SSH tunnel brings it to your machine. |
| Sure → tally chat | Your chat messages; replies from the ledger | `tally chat` listens on a Unix socket, reached only through a relay container on Sure's own network with no port on the server, and answers only requests carrying the token Sure was given. Claude's only tools are Sure's lookups that read, passed on by `tally lookups`, which holds Sure's MCP token and refuses every other tool. A reply quoting a dollar amount that neither the ledger, the chat nor a lookup shows is refused. |
| tally → ntfy | Headlines | ntfy topics have no password, so the topic is 128 random bits and messages carry only kinds and counts. |
| Server | Everything | Containers running as a `tally` user with no login, sudo or docker, with read-only files of their own and no capabilities. They can write only to `/srv/tally/data` (and the chat to its socket's folder). `/srv/tally` is 700, its files 600. |

## Every two hours: sync

```mermaid
sequenceDiagram
    autonumber
    participant T as supercronic
    participant C as cli.py
    participant B as simplefin.py
    participant U as ledger/updating.py
    participant S as store.py: tally.db
    participant L as data/ledger
    participant N as notify.py

    T->>C: tally sync
    C->>B: fetch the last 14 days
    alt can't be asked
        C->>N: SimpleFIN failed: announced once it has lasted 3 h, then every 6 h
    else answered
        U->>S: posted rows not stored yet (same posted date and amount)
        U->>L: their entries, categorized by the rules
        U->>S: each pending transaction, as an alert; when it was asked
    end
    U->>U: categories set on pending entries become rules; Claude for new merchants
    U->>L: pending.beancount: what SimpleFIN last listed
    U->>L: brokerage holdings from SimpleFIN's answer
    U->>U: every row once, bean-check, each balance against the bank's
    U->>U: Sure made to match the ledger
    C->>N: new pending purchases, banks to sign in to again, balances that differ
    N->>S: which keys were already announced?
    N->>N: one POST per kind, most urgent first; keys recorded once ntfy accepts
```

## Reports and the summary

```mermaid
flowchart TD
    ledger[("data/ledger")] -- "categories you set" --> rulesx["rules.py<br/>ledger categories first,<br/>then data/rules.toml"]
    db[("tally.db: transactions, alerts")] --> rep["report.py<br/>a month's spending per category"]
    rulesx --> rep
    rep --> weekly["summary.py<br/>week, month vs plan,<br/>look out for, coming up"]
    ledger -- "plan.beancount" --> weekly
    ledger -- "bean-query" --> lreport["ledger/weekly.py<br/>month vs last month, Roth"]
    ledger --> picture["ledger/picture.py<br/>the facts Claude goes by"]
    picture --> advice["advice.py<br/>monthly review"]
    picture --> chat["chat.py<br/>Sure's chat"]
    weekly --> email["tally summary --email"]
    lreport --> email
    advice --> email
```

## Modules

An arrow means "imports". `cli.py` is the only entry point. The parsers know nothing about
storage, and the ledger package knows nothing about notifications.

```mermaid
flowchart TD
    cli["cli.py<br/>commands"]

    subgraph sources["Where transactions come from"]
        simplefin["simplefin.py<br/>the bridge, its rows, pending and holdings"]
        importers_pkg["importers/: merchant cleanup for some banks,<br/>brokerage holdings"]
    end

    subgraph pendingpkg["Pending"]
        alerts_pkg["alerts/__init__.py<br/>Alert"]
        matching["matching.py<br/>pending to posted row"]
        pending["pending.py<br/>waiting ones as rows"]
        notify["notify.py<br/>ntfy, once per event"]
    end

    subgraph budgeting["Categories and summary"]
        rules["rules.py<br/>categories"]
        report["report.py<br/>month totals"]
        plan["plan.py<br/>plan.beancount"]
        corrections["corrections.py<br/>pending fixes to rules"]
        categorizing["categorizing.py<br/>Claude's merchant rules"]
        summary["summary.py, mailer.py<br/>weekly summary"]
        advice["advice.py, chat.py, lookups.py<br/>Claude's review and Sure's chat"]
    end

    subgraph ledgerpkg["Ledger"]
        ledger_mods["ledger/: accounts, entries, importers,<br/>positions, book, categories, updating,<br/>history, parity, weekly, viewing, mirror"]
        sure["sure.py<br/>Sure's API"]
    end

    store["store.py<br/>SQLite, migrations"]
    config["config.py<br/>config.toml, secrets"]
    transaction["transaction.py<br/>Transaction, identity"]

    cli --> simplefin & notify & config & store & pending
    cli --> rules & report & plan & summary & advice & ledger_mods

    simplefin --> importers_pkg & alerts_pkg & transaction
    notify --> config & store
    matching --> alerts_pkg & transaction
    pending --> matching & alerts_pkg & transaction

    rules --> importers_pkg & transaction
    report --> rules & transaction
    corrections --> categorizing & rules & transaction
    categorizing --> rules & transaction
    summary --> plan & report & pending & rules
    ledger_mods --> rules & corrections & categorizing & store & pending & matching & simplefin & sure

    store --> alerts_pkg & transaction
```

## Database

`data/tally.db`, SQLite. Money is stored as integer cents, and dates as ISO text. The schema
version is stored in `PRAGMA user_version`, and `store.connect` runs any missing migrations.

```mermaid
erDiagram
    imports ||--o{ transactions : "added"

    imports {
        int id PK
        text account
        text file_name "simplefin (a CSV's, before)"
        text file_sha256
        text imported_at "for simplefin: when it was asked"
        int rows_in_file
        int rows_new
    }
    transactions {
        int id PK
        text account "a tally-account"
        text date
        text posted
        text description
        text merchant
        int amount_cents "negative = money out"
        text bank_category
        int balance_cents "no longer filled"
        int import_id FK
    }
    alerts {
        int id PK
        text message_id UK "simplefin:account:transaction"
        text account
        text date
        text merchant
        int amount_cents
        text received_at "when SimpleFIN last listed it"
        text read_at
    }
    notifications {
        text key PK "new:, signin:, balance:, stale:, bank-failure"
        text sent_at
    }
```

Pending transactions are never joined to posted ones in the database. Matching is recomputed
on every run, and pending.beancount is worked out afresh each time, so a late or corrected
answer fixes the picture without any cleanup.

## On the server

```mermaid
flowchart LR
    subgraph github["GitHub"]
        ci["tally's CI<br/>checks, builds the image;<br/>a v* tag publishes it"]
        image["ghcr.io/vmenon04/tally:VERSION<br/>code, Claude Code, supercronic;<br/>no data or secrets"]
    end

    subgraph server["Server, Docker"]
        compose["/opt/tally<br/>compose.yml names the version,<br/>crontab, .env: the tally user"]
        schedule["tally-schedule-1<br/>supercronic: sync, summary"]
        chat["tally-chat-1<br/>tally chat, always on"]
        datadir[("/srv/tally/data<br/>read and write")]
        secretsdir[("/srv/tally/secrets<br/>read-only")]
        sock["/srv/tally-chat<br/>chat.sock"]
    end

    you["You, over SSH<br/>sudo docker exec ... tally"]

    ci --> image
    image -- "docker compose up -d" --> schedule
    image -- "docker compose up -d" --> chat
    compose --> schedule
    compose --> chat
    schedule <--> datadir
    chat <--> datadir
    secretsdir --> schedule
    secretsdir --> chat
    chat --> sock
    you -. "commands, Fava" .-> schedule
```

Settings every container gets in `deploy/compose.yml`:
- **User:** the server's `tally` user, which owns `/srv/tally`; never root.
- **Filesystem:** the image read-only (`read_only`), a fresh `/tmp`, and only the data folder (and the chat's socket folder) writable. Secrets are mounted read-only.
- **Privileges:** no Linux capabilities (`cap_drop: ALL`), `no-new-privileges`, and Docker's default system call filter.
- **Network:** the server's own, to reach Sure on `127.0.0.1` and the internet.

See [Running on a server](server.md) for installing and upgrading.
