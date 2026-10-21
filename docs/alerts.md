# Alert emails and notifications

Discover "Transaction Alert" and BofA "Your debit card was used" emails, saved as `.eml`
files in `data/alerts/`, are read with:

```sh
uv run tally alerts --dry-run   # show what would be stored, save nothing
uv run tally alerts             # store each alert once (by Message-ID); the files stay put
```

An email is only accepted when Gmail's `Authentication-Results` header shows the bank signed
it. Alerts are provisional (the amount can change before it posts) and are kept apart from
imported transactions. The command matches alerts to imported CSV rows (same account, dated
a day before to three days after, agreeing merchant name, same amount or up to 30% more for
a tip; the full rules are at the top of `src/tally/matching.py`) and lists the rest as
*waiting* (the CSVs don't reach that date yet) or *not found* (they do, but have no such row).

## Reading the alerts mailbox

Instead of saving emails by hand, tally can read a separate Gmail account that receives only
the bank alerts (a filter in your main Gmail forwards them). Put its address in
`data/config.toml` as `gmail_account = "..."`, turn on 2-Step Verification for that account,
create an app password for it at myaccount.google.com/apppasswords, then:

```sh
uv run tally gmail-login          # once: type the app password; saved to secrets/gmail-app-password
uv run tally alerts --gmail       # fetch new bank emails over IMAP, store their alerts
```

gmail-login only saves a password Gmail accepts, and only something shaped like an app
password (never your account password). The mailbox is opened read-only, so nothing in it is
marked, moved or deleted. Each email read is kept in `data/alerts/gmail/`, so it's never
fetched twice; a refused email isn't kept, so it's reported on every run.

## Notifications

```sh
uv run tally ntfy-setup           # once: makes a random topic in secrets/ntfy-topic
uv run tally notify-test          # check your phone gets it
```

Subscribe to the printed topic in the ntfy app. After each `tally alerts --gmail` run (not a
dry run), tally sends headlines only, never merchants or amounts: new card alerts, refused
alert emails, purchases missing from the bank export, and a mailbox it can't read. Each is
sent once (a mailbox failure again every 6 hours while it lasts); one that fails to send is
retried on the next run. `ntfy_server` in `config.toml` changes the server (default
`https://ntfy.sh`). Anyone with the topic can read the notifications, so keep it private.
