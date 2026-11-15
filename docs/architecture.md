# Architecture

tally runs on one home server. What it knows about your bank and card accounts comes from
SimpleFIN Bridge, which reads them for you: posted transactions, pending ones, and each
account's balance. Fidelity holdings come from SnapTrade. It keeps a Beancount ledger (pushed to its own repo on
Forgejo, edited with Fava), fills Sure from it, emails a weekly summary and sends
headline-only push notifications.

## The whole system

```mermaid
flowchart LR
    subgraph banks["Banks"]
        bank["Discover, Bank of America,<br/>Capital One"]
        fidelity["Fidelity"]
    end

    simplefin["SimpleFIN Bridge<br/>read-only, 24 requests a day"]
    snaptrade["SnapTrade<br/>read-only"]

    subgraph server["Home server"]
        timer["systemd timer<br/>sync every 2 hours"]
        tally["tally<br/>user: tally, sandboxed"]
        data[("/srv/tally/data<br/>tally.db, rules.toml, config.toml,<br/>raw answers, ledger/ (git clone)")]
        secrets[("/srv/tally/secrets<br/>SimpleFIN access, app password,<br/>ntfy topic, Claude token, Forgejo token")]
        forgejo["Forgejo<br/>ledger repo, localhost:3000"]
        sure["Sure<br/>web and phone UI"]
    end

    subgraph mac["Mac"]
        fava_view["Browser<br/>Fava, over an SSH tunnel, to edit"]
    end

    gmail["Gmail<br/>sends the weekly summary"]
    ntfy["ntfy.sh<br/>random 128-bit topic"]
    phone["Phone<br/>ntfy app"]

    bank --> simplefin
    fidelity --> snaptrade
    timer --> tally
    simplefin -- "HTTPS, Basic auth" --> tally
    snaptrade -- "HTTPS, signed" --> tally
    tally <--> data
    secrets --> tally
    tally -- "commit and push the ledger" --> forgejo
    tally -- "API, on this machine" --> sure
    data -. "tally-remote fava: Unix socket, SSH" .-> fava_view
    tally -- "weekly summary, SMTP" --> gmail
    tally -- "headlines only: counts, no merchants or amounts" --> ntfy
    ntfy --> phone
```

Solid lines run on their own; dotted lines are steps you do by hand.

## Trust boundaries

| Boundary | What crosses it | What protects it |
|---|---|---|
| Banks → SimpleFIN → tally | Transactions and balances | SimpleFIN holds the bank connections; tally holds only an access URL, read-only, in secrets/, never printed or logged. Its rows are checked against what's stored (same posted date and amount) and each account's balance against the ledger's. |
| tally → Forgejo | The ledger | A token for the one repo, in secrets/, given to git through its environment (never its command line or config). Forgejo is reached on localhost. |
| Fava → you | The ledger, read and edit | Runs only while `tally-remote fava` is open, on a Unix socket in a folder only your account and tally can open; SSH forwards it to your machine. |
| Sure → tally chat | Your chat messages; replies from the ledger | `tally chat` listens on a Unix socket, reached only through a relay container on Sure's own network with no port on the server, and answers only requests carrying the token Sure was given. Claude's only tools are Sure's lookups that read, passed on by `tally lookups`, which holds Sure's MCP token and refuses every other tool. A reply quoting a dollar amount that neither the ledger, the chat nor a lookup shows is refused. |
| tally → ntfy | Headlines | ntfy topics have no password, so the topic is 128 random bits and messages carry only kinds and counts. |
| Server | Everything | A `tally` user with no login, sudo or docker. `/srv/tally` is 700. The systemd sandbox allows writes to `/srv/tally/data` only. Files are 600 and folders 700. |

## Every two hours: sync

```mermaid
sequenceDiagram
    autonumber
    participant T as systemd timer
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
    U->>L: Fidelity holdings from SnapTrade
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
        simplefin["simplefin.py<br/>the bridge, its rows and pending"]
        importers_pkg["importers/: how BofA and Discover word rows,<br/>Fidelity's holdings"]
        snaptrade["snaptrade.py<br/>Fidelity holdings"]
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
        text account "discover, bofa_checking"
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
answer fixes the picture without any cleanup. Alerts from the alert emails tally used to read
are still in the table; SimpleFIN's answers cover them.

## On the server

```mermaid
flowchart LR
    subgraph systemd["systemd, root"]
        sync_timer["tally-sync.timer<br/>every 2 hours<br/>Persistent=true"]
        sync_service["tally-sync.service<br/>Type=oneshot, User=tally<br/>TZ=America/New_York<br/>stdout discarded"]
        summary_timer["tally-summary.timer<br/>Sun 18:00 America/New_York"]
        summary_service["tally-summary.service<br/>tally summary --email --sync"]
        chat_service["tally-chat.service<br/>always on"]
    end

    subgraph sandbox["Sandbox"]
        bin["/srv/tally/venv/bin/tally<br/>Python 3.13 from uv"]
    end

    subgraph fs["/srv/tally, owner tally, 700"]
        venv["venv/<br/>tally installed as a copy<br/>read-only at run time"]
        app["app/ + app.old/<br/>deployed code, REVISION"]
        datadir["data/<br/>the only writable path"]
        secretsdir["secrets/<br/>read-only at run time"]
    end

    subgraph you["Your account, over SSH"]
        remote["bin/tally-remote<br/>on your machine"]
        drop["/srv/tally-inbox<br/>Fava's socket, 2770"]
        run["sudo -u tally tally-run<br/>no password, this one program"]
        deploy["sudo -u tally tally-deploy<br/>password required"]
    end

    sync_timer --> sync_service --> bin
    summary_timer --> summary_service --> bin
    chat_service --> bin
    bin --> venv
    bin <--> datadir
    bin --> secretsdir
    sync_service -. "errors only" .-> journal["journald"]

    remote -- "tally-remote fava" --> drop
    remote -- "commands" --> run
    remote -- "git archive HEAD" --> deploy
    deploy -- "test in app.new, swap" --> app
    deploy -- "uv sync --no-editable" --> venv
```

Sandbox settings in `deploy/tally-sync.service`:
- **Filesystem:** `ProtectSystem=strict` with `ReadWritePaths=/srv/tally/data`, `ProtectHome`, `PrivateTmp`, `PrivateDevices`.
- **Privileges:** `NoNewPrivileges`, an empty `CapabilityBoundingSet`, `RestrictSUIDSGID`.
- **Network and system calls:** only internet and local sockets (`RestrictAddressFamilies`), and only the `@system-service` system calls.
- **Kernel, clock, hostname and processes:** all protected.

See [Running on a server](server.md) for installing and updating.
