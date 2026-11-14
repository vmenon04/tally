# Alert emails and notifications

tally reads a separate Gmail account that receives only the bank alerts (a filter in your
main Gmail forwards them): Discover "Transaction Alert" and "Merchant refund/credit posted to
your account" emails, and BofA "Your debit card was used" and "A direct deposit was credited
to your account" emails. Put its address in `data/config.toml` as `gmail_account = "..."`,
turn on 2-Step Verification for that account, create an app password for it at
myaccount.google.com/apppasswords, then:

```sh
uv run tally gmail-login          # once: type the app password; saved to secrets/gmail-app-password
uv run tally alerts --dry-run     # show what would be stored, save nothing
uv run tally alerts               # fetch new bank emails over IMAP, store each alert once
```

gmail-login only saves a password Gmail accepts, and only something shaped like an app
password (never your account password). The mailbox is opened read-only, so nothing in it is
marked, moved or deleted. Each email read is kept in `data/alerts/gmail/`, so it's never
fetched twice; a refused email isn't kept, so it's reported on every run.

BofA's daily "Your Available Balance" email isn't an alert, but it's kept too: `tally sync`
compares the newest one with the ledger (see [Catching dropped and duplicated
rows](ledger.md#catching-dropped-and-duplicated-rows)).

An email is only accepted when Gmail's `Authentication-Results` header shows the bank signed
it. Alerts are provisional (the amount can change before it posts) and are kept apart from
imported transactions. The command matches alerts to imported CSV rows (same account, dated
a day before to three days after, agreeing merchant name, same amount or up to 30% more for
a tip, exactly the same for a deposit or refund, whose window counts from the row's posted
date because that's the date their alerts give; the full rules are at the top of
`src/tally/matching.py`) and lists the rest as *waiting* (the CSVs don't reach that date yet)
or *not found* (they do, but have no such row).

An email that arrives before tally supports its kind of alert is kept too, as skipped, and
isn't fetched again. After deploying a parser for it, read the kept emails again (alerts
already stored are only counted):

```sh
uv run tally alerts --reread --dry-run   # show what would be stored
uv run tally alerts --reread
```

## Notifications

```sh
uv run tally ntfy-setup           # once: makes a random topic in secrets/ntfy-topic
uv run tally notify-test          # check your phone gets it
```

Subscribe to the printed topic in the ntfy app. After each `tally alerts` run (not a
dry run), tally sends headlines only, never merchants or amounts: new card alerts, refused
alert emails, purchases missing from the bank export, a mailbox it can't read, and a week
with no new transactions at all (no new alert and no export adding a row, which means
something upstream has stopped). Each is sent once (a quiet week once per quiet spell; a mailbox failure only once it has lasted 15 minutes, so a single timed-out run
stays quiet, then again every 6 hours while it lasts); one that fails to send is
retried on the next run. `ntfy_server` in `config.toml` changes the server (default
`https://ntfy.sh`). Anyone with the topic can read the notifications, so keep it private.
