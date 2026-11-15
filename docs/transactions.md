# Where transactions come from

## Bank and card accounts: SimpleFIN

[SimpleFIN Bridge](https://bridge.simplefin.org) (paid, about $15 a year) reads your accounts
for you. Connect your banks there, make a setup token, then:

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

Each `tally sync` (every two hours on the server) asks for the last 14 days and adds the
rows posted since: a row counts as stored when a stored row of its account has the same
posted date and amount. Bank of America's rows ("CHECKCARD 1018 SHOP …", masked reference
numbers) are read into the purchase date and a merchant name that stays the same from row
to row, so rules keep working. What came in is kept in `data/raw/simplefin/`, so the
database can be rebuilt from it. Problems the bridge reports (a bank that needs you to sign
in again) are in the sync's output, the weekly summary and a notification.

Its pending transactions are the account's pending entries: see [Pending
purchases](notifications.md#pending-purchases).

Every account linked with `simplefin-account:` also has its balance checked on each sync
against the bank's; an account can be linked for that alone, without `tally-account:`
(Capital One savings, say, whose interest tally doesn't import):

    2026-09-01 open Assets:CapitalOne:Savings USD
      simplefin-account: "5678"

The bridge allows about 24 requests a day: each sync makes one (12 a day), and so does each
`tally simplefin`.

Reading another bank's rows takes a small function in `src/tally/simplefin.py` that turns
them into Transactions, with that bank's merchant cleanup in `src/tally/importers/`.

## Fidelity holdings: SnapTrade

Each sync also records what each Fidelity account holds, from SnapTrade: see [Fidelity
holdings](ledger.md#fidelity-holdings).

`TALLY_DATA_DIR` sets the data folder (default: `data` in the current directory).
