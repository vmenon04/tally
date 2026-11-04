# The Beancount ledger

tally writes everything it imports to a [Beancount](https://beancount.github.io/) ledger as
well as to its database. The ledger is its own git repo (`ledger` on Forgejo); on the server
its working copy is `data/ledger`. Nothing here happens unless `data/ledger/main.beancount`
exists, so tally works as before without one.

## What tally writes

- **`tally import`** adds each export's rows to `imported/YEAR.beancount`, after the database
  accepts the file. BofA files also add a balance assertion for the start and end of the
  period they cover. A file the ledger can't take (an account or category with no ledger
  account) isn't imported anywhere. Afterwards it runs bean-check and reports any problem,
  commits the ledger and pushes it.
- **`tally alerts`** (and `import`) rewrites `pending.beancount`: alert emails no export has
  reached yet, flagged `!`. Each disappears once its posted row arrives. It isn't in git.

Entries are dated by **posting date**, which is what statements and running balances count;
`purchased:` holds the day it happened when that differs. A pending alert is dated no earlier
than its account's latest balance assertion, since it can't have posted inside a period an
export already covers.

Each entry carries `tally-id`, a hash of the row's account, dates, description and amount.
Importing an overlapping file adds only rows the ledger doesn't have, counting identical rows
the way the database does (two equal coffees on one day are both kept). Runs of five or more
digits in descriptions are cut to their last four, so account and reference numbers stay out.

## Accounts and categories

`accounts.beancount` says where things go, in each account's metadata:

```beancount
2026-09-18 open Assets:BofA:Checking USD
  tally-account: "bofa_checking"         ; this export's rows post here
2026-09-01 open Expenses:Groceries USD
  category: "Groceries"                  ; the other side of rows in this category
2026-09-01 open Assets:BofA:Savings USD
  category: "Savings transfer"
  merchant-contains: "KEEP THE CHANGE"   ; only for these merchants
```

Every category in `rules.toml`, the built-in ones (Income, Card payment, Savings transfer,
Investing) and Uncategorized need an account without `merchant-contains`.

A transfer between two accounts that both have exports (a Discover payment from BofA) posts
both sides to an `Assets:Transfers:...` account, so it isn't counted twice and a missing side
shows up as a non-zero balance there.

## Catching dropped and duplicated rows

bean-check fails when an account's balance doesn't match an assertion. BofA exports assert
their own balances; type the others from statements into `statements.beancount`. A row
imported twice or missing then shows up as a failed balance.

## Browsing it

```sh
tally-remote fava        # then open http://localhost:5000; Ctrl-C stops it
```

Fava runs on the server only while that's open. It has no login, so it listens on a Unix
socket in `/srv/tally-inbox`, which only your account and `tally` can open, and SSH forwards a
port on your machine to it. Nothing is exposed to the network.

## Setting it up on the server

1. Make an empty private `ledger` repo on Forgejo, and an access token that can write to it
   (Settings → Applications, repository read and write). Save it as
   `/srv/tally/secrets/forgejo-token` (owner `tally`, 600).
2. Push the skeleton to it, then clone it as `tally` into `/srv/tally/data/ledger`.
3. Fill in `opening.beancount`, commit, and import the archived exports to backfill:
   `tally-remote import` won't do (it imports the inbox), so run
   `tally-run import /srv/tally/data/raw/*/*.csv` on the server.
