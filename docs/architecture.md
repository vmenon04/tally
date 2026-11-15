# Architecture

tally runs on one home server. Everything it knows comes from two sources: the CSV exports
you download from each bank, which are the record of what posted, and the alert emails the
banks send at purchase time, which are provisional and give early warning. It keeps a
Beancount ledger (pushed to its own repo on Forgejo, edited with Fava), emails a weekly
summary and sends headline-only push notifications. There are no bank logins and no
aggregators.

## The whole system

```mermaid
flowchart LR
    subgraph banks["Banks"]
        discover_bank["Discover"]
        bofa_bank["Bank of America"]
    end

    subgraph google["Google"]
        main_gmail["Main Gmail<br/>filter forwards bank alerts"]
        alerts_gmail["Alerts Gmail<br/>holds only the bank alerts<br/>2-Step Verification + app password"]
    end

    subgraph mac["Mac"]
        browser["Browser<br/>download CSV exports"]
        fava_view["Browser<br/>Fava, over an SSH tunnel, to edit"]
    end

    subgraph server["Home server"]
        timer["systemd timer<br/>every 5 min"]
        tally["tally<br/>user: tally, sandboxed"]
        data[("/srv/tally/data<br/>tally.db, rules.toml, config.toml,<br/>emails, CSVs, ledger/ (git clone)")]
        secrets[("/srv/tally/secrets<br/>app password, ntfy topic,<br/>Claude token, Forgejo token")]
        forgejo["Forgejo<br/>ledger repo, localhost:3000"]
    end

    ntfy["ntfy.sh<br/>random 128-bit topic"]
    phone["Phone<br/>ntfy app"]

    discover_bank -- "alert email, DKIM-signed" --> main_gmail
    bofa_bank -- "alert email, DKIM-signed" --> main_gmail
    main_gmail -- "forward" --> alerts_gmail
    discover_bank -. "CSV export" .-> browser
    bofa_bank -. "CSV export" .-> browser
    browser -. "tally-remote import, over SSH" .-> data

    timer --> tally
    alerts_gmail -- "IMAP, read-only" --> tally
    tally <--> data
    secrets --> tally
    tally -- "commit and push the ledger" --> forgejo
    data -. "tally-remote fava: Unix socket, SSH" .-> fava_view
    tally -- "weekly summary, SMTP" --> main_gmail
    tally -- "headlines only: counts, no merchants or amounts" --> ntfy
    ntfy --> phone
```

Solid lines run on their own; dotted lines are steps you do by hand.

## Trust boundaries

| Boundary | What crosses it | What protects it |
|---|---|---|
| Bank → alerts mailbox | Alert emails | An email counts only if Gmail's topmost `Authentication-Results` header shows `dkim=pass` for the bank's domain and the From address is the bank's. A forged email is refused and reported. |
| Alerts mailbox → tally | Raw emails | An app password for an account that holds nothing but bank alerts. The mailbox is opened with `EXAMINE` and read with `BODY.PEEK`, so tally can't change it. |
| CSV → database | Posted transactions | The bank is recognized by file contents. BofA running balances must add up, and a gap in its history stops the import. Rows are deduplicated by identity. |
| tally → Forgejo | The ledger | A token for the one repo, in secrets/, given to git through its environment (never its command line or config). Forgejo is reached on localhost. |
| Fava → you | The ledger, read and edit | Runs only while `tally-remote fava` is open, on a Unix socket in a folder only your account and tally can open; SSH forwards it to your machine. |
| Sure → tally chat | Your chat messages; replies from the ledger | `tally chat` listens on a Unix socket, reached only through a relay container on Sure's own network with no port on the server, and answers only requests carrying the token Sure was given. Claude's only tools are Sure's lookups that read, passed on by `tally lookups`, which holds Sure's MCP token and refuses every other tool. A reply quoting a dollar amount that neither the ledger, the chat nor a lookup shows is refused. |
| tally → ntfy | Headlines | ntfy topics have no password, so the topic is 128 random bits and messages carry only kinds and counts. |
| Server | Everything | A `tally` user with no login, sudo or docker. `/srv/tally` is 700. The systemd sandbox allows writes to `/srv/tally/data` only. Files are 600 and folders 700. |

## Every 5 minutes: alerts

```mermaid
sequenceDiagram
    autonumber
    participant T as systemd timer
    participant C as cli.py
    participant G as gmail.py
    participant M as Alerts Gmail
    participant R as alert_reading.py
    participant P as alerts/discover.py, alerts/bofa.py
    participant S as store.py: tally.db
    participant X as matching.py
    participant N as notify.py
    participant F as ntfy.sh

    T->>C: tally alerts
    C->>G: ImapMailbox.connect(address, app password)
    G->>M: LOGIN, LIST, find \All, EXAMINE All Mail
    alt can't connect or log in
        C->>N: mailbox failed: announced once it has lasted 15 min, then every 6 h
    else connected
        R->>G: message_ids(from:discover.com OR bankofamerica.com)
        G->>M: UID SEARCH X-GM-RAW, FETCH X-GM-MSGID
        Note over R: skip IDs already kept in data/alerts/gmail/
        loop each new email, oldest first
            R->>G: raw(id)
            G->>M: UID FETCH BODY.PEEK[]
            R->>P: choose the parser by sender domain
            P->>P: DKIM + From check, then read merchant, date, amount
            alt refused
                R-->>C: refused, email not kept, reported every run
            else parsed
                R->>S: save_alert, unique by Message-ID
                R->>R: keep the raw email in data/alerts/gmail/id.eml (600)
            end
        end
        R->>S: alerts(), transactions()
        R->>X: match(alerts, transactions)
        X-->>R: matched / waiting / not found
    end
    C->>N: events: new, refused, not_found, mailbox
    N->>S: which keys were already announced?
    N->>F: one POST per kind, most urgent first
    N->>S: record keys only after ntfy accepts
```

## By hand: import, report, sync

```mermaid
flowchart TD
    csv["CSV in data/inbox/"] --> recognize{"importing.py<br/>recognize by first line"}
    recognize -- "Discover" --> dparse["importers/discover.py"]
    recognize -- "BofA" --> bparse["importers/bofa.py<br/>checks running balances"]
    recognize -- "Fidelity positions" --> fparse["importers/fidelity.py<br/>snapshot of holdings"]
    recognize -- "unknown" --> stay["left in inbox, error shown"]
    dparse --> txn["Transaction<br/>account, dates, description, amount"]
    bparse --> txn
    txn --> save["store.py<br/>skip rows already stored,<br/>refuse a BofA gap"]
    save --> db[("tally.db: transactions, imports")]
    save --> archive["original copied to<br/>data/raw/account/date-hash.csv"]

    txn --> extract["ledger/importers.py<br/>entries, categorized by rules.toml,<br/>balance assertions"]
    fparse --> positions["ledger/positions.py<br/>holdings at cost, prices, balances"]
    extract --> ledger[("data/ledger<br/>imported/YEAR.beancount")]
    positions --> ledger
    alerts_db[("tally.db: alerts")] --> waiting["pending.py<br/>alerts no CSV reaches yet"]
    waiting --> pend["ledger/book.py<br/>pending.beancount, rewritten each run"]
    pend --> ledger
    ledger --> history["ledger/history.py<br/>commit, push to Forgejo"]

    ledger -- "categories you set" --> rulesx["rules.py<br/>ledger categories first,<br/>then data/rules.toml"]
    rulesx --> rep["report.py<br/>a month's spending per category"]
    rep --> cli_report["tally report YYYY-MM"]
    rep --> weekly["summary.py<br/>week, month vs plan,<br/>look out for, coming up"]
    ledger -- "plan.beancount" --> weekly
    ledger -- "bean-query" --> lreport["ledger/weekly.py<br/>month vs last month, Roth"]
    weekly --> cli_summary["tally summary"]
    lreport --> cli_summary

    sync["tally sync<br/>ledger/updating.py"] --> corr["corrections.py<br/>a category set on a pending entry<br/>becomes a one-purchase rule"]
    sync --> claude["categorizing.py<br/>claude -p, no tools;<br/>answers become merchant rules"]
    sync --> moves["ledger/categories.py<br/>entries still Uncategorized<br/>move to the rules' answer"]
    sync --> parity["ledger/parity.py<br/>every row once, bean-check"]
```

## Modules

An arrow means "imports". `cli.py` is the only entry point. The parsers know nothing about
storage, and the ledger package knows nothing about email or notifications.

```mermaid
flowchart TD
    cli["cli.py<br/>commands"]

    subgraph email["Alert emails"]
        alert_reading["alert_reading.py<br/>fetch, store, summarize"]
        gmail["gmail.py<br/>IMAP mailbox"]
        alerts_pkg["alerts/__init__.py<br/>Alert, DKIM check, HTML to lines"]
        alerts_discover["alerts/discover.py"]
        alerts_bofa["alerts/bofa.py"]
        matching["matching.py<br/>alert to posted row"]
        notify["notify.py<br/>ntfy, once per event"]
        pending["pending.py<br/>waiting alerts as rows"]
    end

    subgraph csvs["Bank exports"]
        importing["importing.py<br/>recognize, store, archive"]
        importers_pkg["importers/__init__.py<br/>shared field parsing"]
        importers_discover["importers/discover.py"]
        importers_bofa["importers/bofa.py"]
    end

    subgraph budgeting["Categories and summary"]
        rules["rules.py<br/>categories"]
        report["report.py<br/>month totals"]
        plan["plan.py<br/>plan.beancount"]
        corrections["corrections.py<br/>pending fixes to rules"]
        categorizing["categorizing.py<br/>Claude's merchant rules"]
        summary["summary.py<br/>weekly summary"]
        advice["advice.py, chat.py<br/>Claude's review and Sure's chat"]
    end

    subgraph ledgerpkg["Ledger"]
        ledger_mods["ledger/: accounts, entries, importers,<br/>positions, book, categories, updating,<br/>history, parity, weekly, viewing, mirror"]
        sure["sure.py<br/>Sure's API"]
    end

    store["store.py<br/>SQLite, migrations"]
    config["config.py<br/>config.toml, secrets"]
    transaction["transaction.py<br/>Transaction, identity"]

    cli --> alert_reading & importing & notify & gmail & config & store & pending
    cli --> rules & report & plan & summary & advice & ledger_mods

    alert_reading --> gmail & matching & store & alerts_pkg & alerts_discover & alerts_bofa
    alert_reading --> importing & ledger_mods
    alerts_discover --> alerts_pkg
    alerts_bofa --> alerts_pkg
    gmail --> config
    notify --> config & store
    matching --> alerts_pkg & transaction
    pending --> matching & alerts_pkg & transaction

    importing --> store & importers_pkg & importers_discover & importers_bofa & transaction
    importing --> ledger_mods
    importers_discover --> importers_pkg & transaction
    importers_bofa --> importers_pkg & transaction

    rules --> importers_discover & importers_bofa & transaction
    report --> rules & transaction
    corrections --> categorizing & rules & transaction
    categorizing --> rules & transaction
    summary --> plan & report & pending & rules
    ledger_mods --> rules & corrections & categorizing & store & pending & matching

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
        text file_name
        text file_sha256
        text imported_at
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
        int balance_cents "BofA running balance"
        int import_id FK
    }
    alerts {
        int id PK
        text message_id UK "deduplicates"
        text account
        text date
        text merchant
        int amount_cents
        text received_at
        text read_at
    }
    notifications {
        text key PK "new:, refused:, not-found:, mailbox-failure"
        text sent_at
    }
```

Alerts are never joined to transactions in the database. Matching is recomputed on every
run, so a late or corrected CSV fixes the picture without any cleanup. The same goes for
pending alerts in the ledger: pending.beancount is worked out afresh on every run.

Two tables from before the ledger, `sheet_writes` and `sheet_categories`, are still in the
database but nothing uses them.

## On the server

```mermaid
flowchart LR
    subgraph systemd["systemd, root"]
        timer["tally-alerts.timer<br/>OnBootSec=2min<br/>OnUnitActiveSec=5min"]
        service["tally-alerts.service<br/>Type=oneshot, User=tally<br/>TZ=America/New_York<br/>stdout discarded"]
        summary_timer["tally-summary.timer<br/>Sun 18:00 America/New_York<br/>Persistent=true"]
        summary_service["tally-summary.service<br/>tally summary --email --sync<br/>same sandbox"]
    end

    subgraph sandbox["Sandbox: exposure 1.3"]
        bin["/srv/tally/venv/bin/tally<br/>Python 3.13 from uv"]
    end

    subgraph fs["/srv/tally, owner tally, 700"]
        venv["venv/<br/>tally installed as a copy<br/>read-only at run time"]
        app["app/ + app.old/<br/>deployed code, REVISION"]
        datadir["data/<br/>the only writable path"]
        secretsdir["secrets/<br/>read-only at run time"]
        uv["bin/uv 0.8.1<br/>used only to install"]
    end

    subgraph you["Your account, over SSH"]
        remote["bin/tally-remote<br/>on your machine"]
        drop["/srv/tally-inbox<br/>2770, group tally"]
        run["sudo -u tally tally-run<br/>no password, this one program"]
        deploy["sudo -u tally tally-deploy<br/>password required"]
    end

    timer --> service --> bin
    summary_timer --> summary_service --> bin
    bin --> venv
    bin <--> datadir
    bin --> secretsdir
    service -. "errors only" .-> journal["journald"]

    remote -- "CSVs" --> drop
    remote -- "commands" --> run
    remote -- "git archive HEAD" --> deploy
    run -- "moves drop files to data/inbox" --> datadir
    run --> venv
    deploy -- "test in app.new, swap" --> app
    deploy -- "uv sync --no-editable" --> venv
```

Sandbox settings in `deploy/tally-alerts.service`:
- **Filesystem:** `ProtectSystem=strict` with `ReadWritePaths=/srv/tally/data`, `ProtectHome`, `PrivateTmp`, `PrivateDevices`.
- **Privileges:** `NoNewPrivileges`, an empty `CapabilityBoundingSet`, `RestrictSUIDSGID`.
- **Network and system calls:** only internet and local sockets (`RestrictAddressFamilies`), and only the `@system-service` system calls.
- **Kernel, clock, hostname and processes:** all protected.

See [Running on a server](server.md) for installing and updating.
