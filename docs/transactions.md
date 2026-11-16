# Where transactions come from

[SimpleFIN Bridge](https://bridge.simplefin.org) reads your bank and card accounts for you,
read-only, for about $15 a year. Connect your banks there, make a setup token, then:

```sh
tally simplefin-login                  # claims the token (hidden input); saves secrets/simplefin-access-url
tally simplefin                        # each account it sees, its balance, recent transactions
tally simplefin --transactions 1234    # one account's transactions, as the bridge gives them
```

## Linking an account

An account's transactions come in once the ledger links it by its last four digits:

```beancount
2026-01-01 open Assets:Bank:Checking USD
  tally-account: "checking"
  simplefin-account: "1234"
```

Each `tally sync` asks for the last 14 days and adds the rows posted since. A row counts as
already stored when a stored row of its account has the same posted date and amount. What came
in is kept in `data/raw/simplefin/`, so the database can be rebuilt from it. Problems the bridge
reports (a bank that needs you to sign in again) show up in the sync's output, the weekly
summary and a notification.

Pending transactions become the account's [pending purchases](notifications.md#pending-purchases).

The bridge allows about 24 requests a day. Each sync makes one, and so does each
`tally simplefin`, so syncing every two hours leaves room for checking by hand.

## Merchant names

Rules match on the merchant name, so it should read the same each time you shop somewhere. For
most banks it's the description the bank gives, with spaces tidied. Two banks get extra cleanup,
picked by their `tally-account` name:

- `bofa_checking`: Bank of America checking. Its "CHECKCARD 1018 SHOP ..." rows give the
  purchase date, and masked reference numbers are removed.
- `discover`: Discover cards. Apple Pay suffixes and glued-on reference codes are removed.

If your bank adds something that changes from row to row, use `match =` rules (the merchant
contains this text) rather than `merchant =`, or add a cleanup for it: a small function in
`src/tally/simplefin.py`, listed in `_ROWS`, with the cleanup in `src/tally/importers/`.

## Accounts checked, not imported

Every account linked with `simplefin-account:` has its balance checked against the bank's on
each sync. An account can be linked for that alone, without `tally-account:`: a savings account
whose deposits already come in as transfers out of checking, say.

Such an account still earns interest the ledger wouldn't otherwise see. Add `interest-from:` and
each sync brings in that account's interest alone (its money in described with "interest"):

```beancount
2026-01-01 open Assets:Bank:Savings USD
  simplefin-account: "5678"
  interest-from: "Income:Interest"
2026-01-01 open Income:Interest USD
```

## Investments

Each sync can also record what a brokerage account holds: see [Investment
holdings](ledger.md#investment-holdings).
