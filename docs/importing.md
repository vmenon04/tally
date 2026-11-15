# Importing bank exports

Download the CSVs (Discover: Recent Activity or a statement; BofA checking: any date range),
put them in `data/inbox/`, then:

```sh
uv run tally import --dry-run   # show what would be added, save nothing
uv run tally import             # store new rows, archive each file to data/raw/, empty the inbox
```

The bank is recognized from the file's contents, so file names don't matter. Overlapping
downloads are fine: rows already stored are skipped. A file that fails (unknown layout, running
balances that don't add up, a gap in BofA history) is not stored and stays in the inbox.
`uv run tally import FILE...` imports specific files and leaves them where they are.

With tally on a server, `tally-remote import FILE...` sends the files and imports them there
([Running on a server](server.md)).

`TALLY_DATA_DIR` sets the data folder (default: `data` in the current directory).

## From SimpleFIN instead

[SimpleFIN Bridge](https://bridge.simplefin.org) (paid, about $15 a year) reads your accounts
for you, so the downloads aren't needed. Connect your banks there, make a setup token, then:

```sh
tally simplefin-login                  # claims the token (hidden input); saves secrets/simplefin-access-url
tally simplefin                        # each account it sees, its balance, recent transactions
tally simplefin --transactions 1234    # one account's transactions, as the bridge gives them
```

An account takes its rows from SimpleFIN once the ledger links it, by its last four digits,
on the account with its `tally-account:`:

    2026-08-31 open Assets:BofA:Checking USD
      tally-account: "bofa_checking"
      simplefin-account: "1234"

Each `tally sync` then asks for the last 14 days and adds the rows posted since: a row counts
as stored when a stored row of its account has the same posted date and amount, which covers
the CSV rows from before (the bank words them differently in each). BofA's SimpleFIN rows
("CHECKCARD 1018 SHOP …", masked reference numbers) are read into the same purchase dates and
merchant names as its CSV's, so rules keep working. What came in is kept in
`data/raw/simplefin/`. A CSV for a linked account is refused, since its rows would be counted
twice; remove the `simplefin-account:` line to import CSVs again. Problems the bridge reports
(a bank that needs you to sign in again) are in the sync's output and the weekly summary.

Its pending transactions are the account's pending entries: see [Pending
purchases](notifications.md#pending-purchases).

Every account linked with `simplefin-account:` also has its balance checked on each sync
against the bank's; an account can be linked for that alone, without `tally-account:`
(Capital One savings, say, whose interest tally doesn't import):

    2026-09-01 open Assets:CapitalOne:Savings USD
      simplefin-account: "5678"

The bridge allows about 24 requests a day: the nightly sync makes one, and so does each
`tally simplefin`.
