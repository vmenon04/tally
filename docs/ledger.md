# The Beancount ledger

The ledger in `data/ledger` is the record of your money: plain-text
[Beancount](https://beancount.github.io/) files you can read, edit and keep in git.
[Getting started](getting-started.md) makes one from `example/`.

| File | Who writes it |
|---|---|
| `main.beancount` | You: options, and includes for the rest |
| `accounts.beancount` | You: your accounts and categories |
| `opening.beancount` | You: what each account held when you started |
| `plan.beancount` | You: budget, paychecks, goals |
| `statements.beancount` | You: balances copied from statements |
| `imported/YEAR.beancount` | tally: every transaction it brings in |
| `pending.beancount` | tally, rewritten each sync: purchases not posted yet, flagged `!`. Not in git |

If the ledger is a git repo, tally commits each change it makes, and pushes when there's a
remote ([Running on a server](server.md#the-ledgers-own-repo)).

## What tally writes

Each `tally sync` adds the transactions SimpleFIN has posted ([Where transactions come
from](transactions.md)) to `imported/`, rewrites `pending.beancount`, then runs bean-check.
Each pending purchase disappears once its posted row arrives.

Entries are dated by **posting date**, which is what statements and running balances count;
`purchased:` holds the day it happened when that differs. Each entry carries `tally-id`, a hash
of the row, so nothing is added twice (two equal coffees on one day are both kept). Runs of five
or more digits in descriptions are cut to their last four, so account and reference numbers
stay out.

## Categories

The ledger decides categories. To change one, change the account on the entry's other side, in
Fava or in the file: `Expenses:Groceries` instead of `Expenses:Uncategorized`, say. Nothing
overwrites it.

`rules.toml` only fills in what nobody has decided. Each `tally sync`:

1. turns a category you set on a pending purchase into a rule for that one purchase, so the
   posted row gets it too;
2. asks Claude (if [Claude Code](https://claude.com/claude-code) is installed) to categorize
   merchants no rule covers yet, and saves its answers as rules;
3. moves imported entries still in Uncategorized to the category the rules now give;
4. checks the ledger: every transaction has exactly one entry, bean-check is clean, and each
   account agrees with what the bank says it holds.

`tally sync --dry-run` shows all of it and saves nothing. Entries you wrote yourself (no
`tally-id`) and entries you split into more postings are left alone.

## Accounts

`accounts.beancount` says where things go, in each account's metadata (all the keys are in the
[reference](reference.md#ledger-metadata)):

```beancount
2026-01-01 open Assets:Bank:Checking USD
  tally-account: "checking"              ; its transactions post here
  simplefin-account: "1234"              ; and come from SimpleFIN's account ending 1234
2026-01-01 open Expenses:Groceries USD
  category: "Groceries"                  ; the other side of rows in this category
2026-01-01 open Assets:Bank:Savings USD
  category: "Savings transfer"
  merchant-contains: "TRANSFER TO SAV"   ; only rows whose merchant or description has one (| between)
```

Every category in `rules.toml`, the built-in ones (Income, Card payment, Savings transfer,
Investing) and Uncategorized need an account without `merchant-contains`.

A transfer between two accounts tally brings in (paying the card from checking) posts both
sides to an `Assets:Transfers:...` account, so it isn't counted twice, and a missing side shows
up as a non-zero balance there.

## Your plan

`plan.beancount` holds what the weekly summary compares against:

```beancount
2026-01-01 custom "budget" Expenses:Groceries "monthly" 300.00 USD
2026-01-01 custom "paychecks" 2                ; paychecks in a month
2026-01-01 custom "roth-plan" 600.00 USD       ; optional: a Roth IRA transfer planned
```

A line holds until a later one for the same thing; change the plan by adding a line dated when
the change starts. Fava shows budgets on the Income Statement and on an account's **Changes
(monthly)** page.

Bills that come once a month count in full against the month's pace instead of a little each
day. Mark their accounts with `fixed: TRUE`.

### Goals and the monthly review

**Goals** are money set aside, each in a savings account of its own, so a goal's progress is
that account's balance:

```beancount
2026-01-01 custom "goal" "Emergency fund" Assets:Bank:Savings "months" 3
2026-01-01 custom "goal" "Laptop" Assets:Bank:Laptop "target" 1200.00 USD
```

"months" sets the target to that many months of your usual spending. Claude's monthly review
(in the [weekly summary](summary.md)) suggests how much to put into each, from what's usually
left over. You move the money yourself; tally sees the transfers arrive.

Tell the review about yourself in the same file:

```beancount
2026-01-01 custom "about" "Saving for a trip in the spring"
```

The numbers the review and Sure's chat go by are worked out in `src/tally/ledger/picture.py`.
Pay counts everything booked to Income except investment income; kept is pay less spending.

## Catching dropped and duplicated rows

Each sync compares every linked account's balance at the bank with the ledger's posted entries
(pending ones left out, as the bank leaves them out). A missed or doubled row shows up there,
and in the weekly summary. It's a check, not an assertion, so a difference never stops
bean-check.

For a stricter check, copy balances from statements into `statements.beancount`; bean-check then
fails when the ledger disagrees:

```beancount
2026-02-01 balance Assets:Bank:Checking  2345.67 USD
```

For an account whose transactions tally doesn't bring in, a `pad` before a balance books the
difference (interest, say) for you:

```beancount
2026-02-01 pad Assets:Bank:Savings Income:Interest
2026-02-05 balance Assets:Bank:Savings  1234.56 USD
```

## Investment holdings

This was built and tested with Fidelity. With the brokerage connected at SimpleFIN Bridge,
`tally sync` records each linked account's holdings once a day, from the same answer that
brings in the bank rows. Link an account by the last four digits of its number:

```beancount
2026-01-01 open Assets:Fidelity:Roth
  fidelity-account: "1234"
```

Each account gets one transaction a day, tagged `#fidelity-positions`, that brings the ledger
to what the brokerage reports:

- each holding whose quantity or cost changed is closed and reopened at the brokerage's
  quantity and total cost basis;
- the money market fund becomes the account's `:Cash` balance;
- what's left comes from `Equity:Opening-Balances` the first time, and from
  `Income:Fidelity:Unreconciled` after that, flagged `!`.

It also records each holding's price, so Fava shows market value, and asserts every holding the
next day. `tally simplefin --extra 1234` shows what the bridge sends for an account.

**Dividends:** with `Income:Fidelity:Dividends` open, rows described "dividend received" are
booked there, and only what's left goes to Unreconciled.

**401(k) contributions:** rows described "contribution" are payroll contributions. Say where
they come from, and open that income account:

```beancount
2026-01-01 open Assets:Fidelity:401k
  fidelity-account: "5678"
  contributions-from: "Income:Salary:401k"
2026-01-01 open Income:Salary:401k USD
```

A 401(k)'s fund comes with no purchase price, so new units count as bought at that day's price,
and its cost basis is your contributions.

**Money from your bank:** Fidelity's holdings can show a transfer days after the bank says it
left, and in between the cash balance asserted from them would fail. Post the bank's side to a
clearing account instead, one per Fidelity account, told apart by the account number in the
bank's description, and name it on the Fidelity account:

```beancount
2026-01-01 open Assets:Transfers:Fidelity:Roth USD
  category: "Investing"
  merchant-contains: "1234"
2026-01-01 open Assets:Fidelity:Roth
  fidelity-account: "1234"
  transfers-from: "Assets:Transfers:Fidelity:Roth"
2026-01-01 open Assets:Transfers:Fidelity USD     ; any other: nothing clears it
  category: "Investing"
```

Once a snapshot shows the money in the account, it comes out of the clearing account (up to
what's waiting there), so the clearing account is back to zero; money back to the bank works
the same way. A balance left in one means a transfer the holdings never showed. The weekly Roth
IRA total counts what went into the Roth's cash or its clearing account.

## Showing it in Sure

[Sure](server.md#sure) can show the ledger as a web and phone app; the ledger stays the record.
Once `tally sure-login` has saved a key, every sync makes Sure match the ledger, for each
account whose open directive names the Sure account it fills:

```beancount
2026-01-01 open Assets:Bank:Checking USD
  tally-account: "checking"
  sure-account: "Checking"
```

Each posting to such an account, pending purchases included, is one Sure transaction, in the
entry's category (categories Sure lacks are made). Each sync adds what's missing, corrects what
changed and removes what the ledger no longer has. tally only touches the transactions it made,
and leaves alone a category you set in Sure. If Sure can't be reached, the next sync catches it
up.

The sync also names any account whose balance in Sure differs from the ledger's. They can
differ even when the transactions agree, since Sure counts from an opening balance of its own.

An investment account fills a manual **Investment** account you make in Sure the same way. Sure
gets its value each day, not its transactions.
