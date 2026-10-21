# Architecture

tally runs on one home server. Everything it knows comes from two sources: the CSV exports
you download from each bank, which are the record of what posted, and the alert emails the
banks send at purchase time, which are provisional and give early warning. It writes to a
Google Sheet and sends headline-only push notifications. There are no bank logins and no
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
        sheet["Google Sheet<br/>Budget tab + Transactions tab"]
    end

    subgraph mac["Mac"]
        browser["Browser<br/>download CSV exports"]
    end

    subgraph server["Home server: myserver"]
        timer["systemd timer<br/>every 5 min"]
        tally["tally<br/>user: tally, sandboxed"]
        data[("/srv/tally/data<br/>tally.db, rules.toml,<br/>config.toml, emails, CSVs")]
        secrets[("/srv/tally/secrets<br/>app password, ntfy topic,<br/>service account key")]
    end

    ntfy["ntfy.sh<br/>random 128-bit topic"]
    phone["Phone<br/>ntfy app"]

    discover_bank -- "alert email, DKIM-signed" --> main_gmail
    bofa_bank -- "alert email, DKIM-signed" --> main_gmail
    main_gmail -- "forward" --> alerts_gmail
    discover_bank -. "CSV export" .-> browser
    bofa_bank -. "CSV export" .-> browser
    browser -. "copy to data/inbox over SSH" .-> data

    timer --> tally
    alerts_gmail -- "IMAP, read-only" --> tally
    tally <--> data
    secrets --> tally
    tally -- "Sheets API, service account" --> sheet
    sheet -- "category corrections" --> tally
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
| tally → Sheet | Category totals, transaction list | A service account that can see only the one Sheet shared with it. Dry runs use a read-only client. tally never overwrites a cell it didn't write. |
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

    T->>C: tally alerts --gmail
    C->>G: ImapMailbox.connect(address, app password)
    G->>M: LOGIN, LIST, find \All, EXAMINE All Mail
    alt can't connect or log in
        C->>N: Event "mailbox", repeated every 6 h at most
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
    recognize -- "unknown" --> stay["left in inbox, error shown"]
    dparse --> txn["Transaction<br/>account, dates, description, amount"]
    bparse --> txn
    txn --> save["store.py<br/>skip rows already stored,<br/>refuse a BofA gap"]
    save --> db[("tally.db: transactions, imports")]
    save --> archive["original moved to<br/>data/raw/account/date-hash.csv"]

    db --> categorize["rules.py + data/rules.toml<br/>first matching rule wins"]
    categorize --> rep["report.py<br/>a month's spending per category"]
    rep --> cli_report["tally report YYYY-MM"]

    rep --> plan["budget.py<br/>cells to change on the Budget tab"]
    plan --> conflicts{"cell changed by<br/>someone other than tally?"}
    conflicts -- "yes" --> stop["list conflicts, write nothing"]
    conflicts -- "no" --> write["sheet.py<br/>batchUpdate as service account"]
    categorize --> tab["transactions_tab.py<br/>rewrite Transactions tab"]
    tab --> write
    write --> gsheet["Google Sheet"]
    write --> record[("tally.db: sheet_writes,<br/>sheet_categories")]

    gsheet -- "you change a Category cell" --> edits["transactions_tab.find_edits"]
    edits --> corr["corrections.py<br/>one-transaction rule in rules.toml,<br/>suggest merchant rule after 2"]
    corr --> categorize
```

## Modules

An arrow means "imports". `cli.py` is the only entry point. The parsers know nothing about
storage, and the storage knows nothing about the Sheet.

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
    end

    subgraph csvs["Bank exports"]
        importing["importing.py<br/>recognize, store, archive"]
        importers_pkg["importers/__init__.py<br/>shared field parsing"]
        importers_discover["importers/discover.py"]
        importers_bofa["importers/bofa.py"]
    end

    subgraph budgeting["Budget"]
        rules["rules.py<br/>categories"]
        report["report.py<br/>month totals"]
        budget["budget.py<br/>Budget tab plan"]
        transactions_tab["transactions_tab.py<br/>Transactions tab"]
        corrections["corrections.py<br/>Sheet edits to rules"]
        sheet["sheet.py<br/>Sheets API"]
    end

    store["store.py<br/>SQLite, migrations"]
    config["config.py<br/>config.toml, secrets"]
    transaction["transaction.py<br/>Transaction"]

    cli --> alert_reading & importing & notify & gmail & config & store
    cli --> rules & report & budget & transactions_tab & corrections & sheet

    alert_reading --> gmail & matching & store & alerts_pkg & alerts_discover & alerts_bofa
    alert_reading --> importing
    alerts_discover --> alerts_pkg
    alerts_bofa --> alerts_pkg
    gmail --> config
    notify --> config & store
    matching --> alerts_pkg & transaction

    importing --> store & importers_pkg & importers_discover & importers_bofa & transaction
    importers_discover --> importers_pkg & transaction
    importers_bofa --> importers_pkg & transaction

    rules --> importers_discover & importers_bofa & transaction
    report --> rules & transaction
    budget --> report & sheet
    transactions_tab --> rules & sheet & transaction
    corrections --> rules & transaction

    store --> alerts_pkg & transaction
```

## Database

`data/tally.db`, SQLite. Money is stored as integer cents, and dates as ISO text. The schema
version is stored in `PRAGMA user_version`, and `store.connect` runs any missing migrations.

```mermaid
erDiagram
    imports ||--o{ transactions : "added"
    transactions ||--o| sheet_categories : "corrected on the Sheet"

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
    sheet_writes {
        text sheet_id PK
        text row_label PK
        text month PK
        int value_cents "what tally last wrote"
    }
    sheet_categories {
        text sheet_id PK
        int transaction_id PK
        text category
    }
```

Alerts are never joined to transactions in the database. Matching is recomputed on every
run, so a late or corrected CSV fixes the picture without any cleanup.

## On the server

```mermaid
flowchart LR
    subgraph systemd["systemd, root"]
        timer["tally-alerts.timer<br/>OnBootSec=2min<br/>OnUnitActiveSec=5min"]
        service["tally-alerts.service<br/>Type=oneshot, User=tally<br/>TZ=America/New_York<br/>stdout discarded"]
    end

    subgraph sandbox["Sandbox: exposure 1.3"]
        bin["/srv/tally/app/.venv/bin/tally<br/>Python 3.13 from uv"]
    end

    subgraph fs["/srv/tally, owner tally, 700"]
        app["app/<br/>code + .venv<br/>read-only at run time"]
        datadir["data/<br/>the only writable path"]
        secretsdir["secrets/<br/>read-only at run time"]
        uv["bin/uv 0.8.1<br/>used only to install"]
    end

    timer --> service --> bin
    bin --> app
    bin <--> datadir
    bin --> secretsdir
    service -. "errors only" .-> journal["journald"]
```

Sandbox settings in `deploy/tally-alerts.service`:
- **Filesystem:** `ProtectSystem=strict` with `ReadWritePaths=/srv/tally/data`, `ProtectHome`, `PrivateTmp`, `PrivateDevices`.
- **Privileges:** `NoNewPrivileges`, an empty `CapabilityBoundingSet`, `RestrictSUIDSGID`.
- **Network and system calls:** only internet and local sockets (`RestrictAddressFamilies`), and only the `@system-service` system calls.
- **Kernel, clock, hostname and processes:** all protected.

See [Running on a server](server.md) for installing and updating.
