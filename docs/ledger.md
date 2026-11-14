# The Beancount ledger

tally writes everything it imports to a [Beancount](https://beancount.github.io/) ledger as
well as to its database. The ledger is its own git repo (`ledger` on Forgejo); on the server
its working copy is `data/ledger`. Nothing here happens unless `data/ledger/main.beancount`
exists, so tally works as before without one.

## What tally writes

- **`tally import`** adds each export's rows to `imported/YEAR.beancount`, after the database
  accepts the file. BofA files also add a balance assertion for the start of the period
  they cover, and for three days before its end: BofA dates some rows by the day they
  happened even when they post later, so an export's last days can still gain rows. A file the ledger can't take (an account or category with no ledger
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

## Categories

The ledger decides categories. To change one, change the account on the entry's other side,
in Fava (open the entry, then edit it) or in the file: `Expenses:Groceries` instead of
`Expenses:Uncategorized`, say. That's all; nothing overwrites it.

`rules.toml` only fills in what nobody has decided: a new entry gets the category the rules
give when it's imported, and an entry still in Uncategorized moves once the rules have an
answer. `tally sync` (Sundays, or by hand) does that:

1. a category you set on a pending purchase (in `pending.beancount`) becomes a rule for that
   one purchase, so the 5-minute refresh keeps it and the posted row gets it too (the alerts
   run does this as well);
2. Claude categorizes merchants no rule covers yet, and its answers become merchant rules;
3. imported entries still in Uncategorized move to the category the rules now give, changing
   only the account name on that line; tally commits and pushes;
4. it checks the ledger: every imported row and pending alert has exactly one entry,
   bean-check is clean, and the ledger agrees with the bank's newest balance email.

`tally sync --dry-run` shows all of it and saves nothing. The weekly summary lists any problem
under "look out for". Entries you wrote yourself (no `tally-id`) and entries you split into
more postings are left alone.

## Your plan

`plan.beancount` holds what the weekly summary compares against:

```beancount
2026-09-01 custom "budget" Expenses:Groceries "monthly" 300.00 USD   ; Fava's budget
2026-11-01 custom "paychecks" 2                                      ; paychecks that month
2026-11-01 custom "roth-plan" 600.00 USD                             ; Roth IRA transfer planned
```

A budget line holds until a later one for the same account; change the plan by adding a line
dated when the change starts. Fava shows budgets on the Income Statement and on an account's
**Changes (monthly)** page, with what's left.

## Accounts and categories

`accounts.beancount` says where things go, in each account's metadata:

```beancount
2026-09-18 open Assets:BofA:Checking USD
  tally-account: "bofa_checking"         ; this export's rows post here
2026-09-01 open Expenses:Groceries USD
  category: "Groceries"                  ; the other side of rows in this category
2026-09-01 open Assets:BofA:Savings USD
  category: "Savings transfer"
  merchant-contains: "KEEP THE CHANGE|SAV 1234"   ; only merchants containing one of these
```

Every category in `rules.toml`, the built-in ones (Income, Card payment, Savings transfer,
Investing) and Uncategorized need an account without `merchant-contains`.

A transfer between two accounts that both have exports (a Discover payment from BofA) posts
both sides to an `Assets:Transfers:...` account, so it isn't counted twice and a missing side
shows up as a non-zero balance there.

## Fidelity positions

With SnapTrade's keys in `secrets/` (`snaptrade-client-id`, `snaptrade-consumer-key`, from a
free SnapTrade Personal account connected to Fidelity read-only), `tally sync` records every
Fidelity account's holdings each time it runs, exactly as importing a positions export would,
so there's nothing to download. `tally snaptrade` shows what SnapTrade sees, without amounts.
SnapTrade gives no cost basis for a 401(k): there, new units count as bought at that day's
price, and units sold take their share of the cost. Accounts not in the ledger are skipped
while they're empty. If SnapTrade can't be reached, the sync says so and carries on.
SnapTrade also reports dividends: with `Income:Fidelity:Dividends` open in the ledger, each
snapshot books the dividends paid since the last one there, and only what's left goes to
`Income:Fidelity:Unreconciled` (flagged `!` when anything is).
It reports a 401(k)'s payroll contributions too. Mark where they come from on the
account's open directive, and open that income account:

    2026-09-01 open Assets:Fidelity:401k
      fidelity-account: "5678"
      contributions-from: "Income:Salary:401k"
    2026-09-01 open Income:Salary:401k USD

Each snapshot then books the contributions since the last one there, as pay that went to
the 401(k). When they bought more of the account's one fund, those units cost what you put
in, so the 401(k)'s cost basis is your contributions and its gain is the market's.
`tally snaptrade --activities 5678` lists an account's activities, with amounts.

The positions export still works the same way, if you'd rather download it:

Download Accounts → Positions (all accounts) from Fidelity and import it like any export:
`tally-remote import Portfolio_Positions_*.csv`. It goes to the ledger only. Each account in
it, matched by `fidelity-account:` (the account number's last four) on an open directive,
gets one transaction tagged `#fidelity-positions` that brings the ledger to the snapshot:

- each holding whose quantity or cost basis changed is closed and reopened at Fidelity's
  quantity and total cost basis, so the ledger's cost basis is exactly Fidelity's;
- the money market balance becomes the account's `:Cash` balance (Roth contributions from
  BofA already land there, so money moved into holdings balances itself);
- what's left comes from `Equity:Opening-Balances` the first time, and from
  `Income:Fidelity:Unreconciled` after that (dividends, fees, 401(k) payroll contributions:
  a snapshot can't tell them apart), flagged `!`.

It also records each holding's price, so Fava shows market value, and asserts every holding
and cash balance the next day. A snapshot older than one already imported is refused, and
the same file twice adds nothing. Fidelity's activity export would say exactly what each
change was; importing it is a possible next step.

## Catching dropped and duplicated rows

bean-check fails when an account's balance doesn't match an assertion. BofA exports assert
their own balances; type the others from statements into `statements.beancount`. A row
imported twice or missing then shows up as a failed balance.

Between exports, BofA's daily balance email does the same for checking, for the days the
ledger has only alerts. Turn it on in BofA's alert settings, for checking only: like the
alerts, it's taken to be for checking. `tally sync` compares the newest email with the ledger's balance before the day it names
(each purchase counted on the day it was made, as the bank does) and says when they
differ. Money that moves without an alert (a transfer, a card payment, Zelle) shows up there
until the next CSV import brings it in. It's a check, not an assertion: the email's
"available" balance can differ by a hold or a late-night purchase, so it isn't written to
the ledger.

## Showing it in Sure

[Sure](server.md#sure) is where you look at the money; the ledger stays the record. Once
`tally sure-login` has saved a key, every `tally sync` makes Sure's transactions match the
ledger's. An account takes part when its open directive names the Sure account it fills:

    2026-08-31 open Assets:BofA:Checking USD
      tally-account: "bofa_checking"
      sure-account: "BofA Checking"

Each posting tally imported to such an account, pending alerts included, is one Sure
transaction: on the day of the purchase, named after the merchant, in the entry's category
(categories Sure lacks are created). Both sides of a transfer between two such accounts are
sent, and Sure pairs them up itself.

Each sync adds what's missing, corrects what changed (a tip added when a purchase posts)
and removes what the ledger no longer has. tally only ever touches the transactions it
made, and leaves alone a category you set in Sure. `tally sync --dry-run` says what it
would do. If Sure can't be reached the sync still does everything else, and the next one
catches Sure up.

## Editing it

Sure shows the ledger day to day, and the weekly summary, Claude's monthly review and Sure's
chat all work from it. To edit it, run `tally-remote fava` and open http://localhost:5000
([Running on a server](server.md#fava-to-edit-the-ledger)). Fava has no login, so it never
listens on a network address: it serves on a Unix socket only your account, `tally` and root
can open, for as long as `tally-remote fava` runs.

## What tally works out

Claude's monthly review and Sure's chat go by these numbers, worked out from the ledger
(`src/tally/ledger/picture.py`). Pay counts everything booked to Income but Fidelity's market income, so 401(k) payroll
contributions count as pay. Kept is pay less spending, so it's low until payday.

**Goals** are money set aside, each in a savings account of its own, so its progress is
that account's balance. Write them in `plan.beancount`:

    2026-11-01 custom "goal" "Emergency fund" Assets:CapitalOne:Savings "months" 3
    2026-11-01 custom "goal" "Travel" Assets:BofA:Savings
    2026-11-01 custom "goal" "Laptop" Assets:BofA:Savings "target" 1200.00 USD

"months" sets the target to that many months of your usual spending. Claude's review suggests
how much to put into each one a month, from what's usually left over the last three whole
months: take-home pay (pay less 401(k) contributions), less spending, less this month's
`roth-plan`. A "months" goal still short of its target gets 70% of that and the others
split the rest; once it's full, the others split it all. Move the money yourself (a
transfer on payday in each bank's app does it); tally sees the transfers arrive.

What you tell Claude about yourself in `plan.beancount` goes into its monthly review in the
[weekly summary](summary.md):

    2026-11-01 custom "about" "First job; 10% to the 401(k), and the employer match starts next year"
    2026-11-01 custom "about" "I want to travel more"

tally doesn't import Capital One, so it sees transfers into it but not the interest. Every
month or so, copy the balance from Capital One's app into `statements.beancount` with a
pad, and the ledger books the difference as interest:

    2026-12-01 pad Assets:CapitalOne:Savings Income:Interest
    2026-12-05 balance Assets:CapitalOne:Savings  1234.56 USD

Bills that come once a month count in full against the pace instead of a little each day.
Mark their accounts in `accounts.beancount` with `fixed: TRUE`:

    2026-09-01 open Expenses:Rent
      category: "Rent + fees"
      fixed: TRUE

To try tally without real data, make a made-up ledger and open it:

    uv run python demo/make_ledger.py /tmp/tally-demo
    TALLY_DATA_DIR=/tmp/tally-demo uv run tally fava

## Setting it up on the server

1. Make an empty private `ledger` repo on Forgejo, and an access token that can write to it
   (Settings → Applications, repository read and write). Save it as
   `/srv/tally/secrets/forgejo-token` (owner `tally`, 600).
2. Push the skeleton to it, then clone it as `tally` into `/srv/tally/data/ledger`.
3. Fill in `opening.beancount`, commit, and import the archived exports to backfill:
   `tally-remote import` won't do (it imports the inbox), so run
   `tally-run import /srv/tally/data/raw/*/*.csv` on the server.


## Backing it up to GitHub

Forgejo runs on the same server as the ledger, so it's no backup. With a `backup` remote,
every push tally makes also goes to a private GitHub repo, even when Forgejo is down.

1. On GitHub, make an empty **private** repo (say `ledger-backup`), with no README.
2. On the server, make a key only tally can read, and print its public half:

   ```
   sudo -u tally ssh-keygen -q -t ed25519 -N '' -C tally-ledger-backup \
     -f /srv/tally/secrets/github-backup-key
   sudo cat /srv/tally/secrets/github-backup-key.pub
   ```
3. In the repo's Settings → Deploy keys, add that public key with **Allow write access**.
   A deploy key opens that one repo and nothing else on your account, and never expires.
4. Add the remote: `sudo -u tally git -C /srv/tally/data/ledger remote add backup
   git@github.com:<you>/ledger-backup.git`. If the network blocks port 22, use
   `ssh://git@ssh.github.com:443/<you>/ledger-backup.git` instead.

The next import or sync pushes everything. tally trusts only GitHub's published host key,
which is in `history.py`.