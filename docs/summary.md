# Weekly summary

```sh
uv run tally summary                      # as of today
uv run tally summary --as-of 2026-10-18   # as it would have been on that day
uv run tally summary --email              # print it and email it
uv run tally summary --email --sync       # run `tally sync` first, then email it
```

It covers the 7 days up to today (or the `--as-of` day; anything later is left out):

- **This week:** every purchase, pending ones marked, against what the plan allows per week.
- **The month so far:** each category's spending against its "Plan / mo" on the Budget tab, and
  what's left per day. In a month's first days, how the month before ended.
- **Look out for:** categories over plan or ahead of pace, big purchases, uncategorized
  transactions, alerts missing from a bank export, and the monthly CSV import when it's due.
- **Coming up:** next month's paychecks, planned spending and Roth IRA transfer, and whether the
  year's planned Roth IRA contributions fit the limit.

The advice comes from fixed rules (the thresholds are at the top of `src/tally/summary.py`),
worked out from the database, the pending alerts and the numbers typed on the Budget tab, which
is read but never changed. The numbers are worked out here. Without `--as-of`, the finished summary then goes to Claude,
which adds a short note of advice at the top; it can only comment, a note quoting a
dollar amount the summary doesn't show is refused, and if there's no note the summary still goes
out and says why. If the Sheet can't be read,
the summary says so and leaves out the plan comparisons.

For the Roth IRA check, add to `data/config.toml`:

```toml
roth_limit = 7500        # this year's contribution limit
roth_elsewhere = 2240    # contributed this year where tally can't see it (before tally, other accounts)
```

The amounts typed in the Budget tab's Roth IRA row for the coming months count as planned.
`tally sync` keeps the current month's planned amount there until a contribution goes in, then
replaces it with the actual one.

## By email

`--email` sends the same text, with a link to the Sheet, from the alerts mailbox
(`gmail_account`) to the address in `summary_to`:

```toml
summary_to = "you@example.com"
```

It signs in to Gmail's SMTP server with the app password `tally gmail-login` saved, so there's
nothing else to set up. The summary is printed either way; if sending fails, the command says
why and exits with an error.

On the server, `tally-summary.timer` syncs the Sheet and sends it every Sunday at 6pm New York
time. If the sync is blocked (say, by a value typed into a cell tally fills), the summary
still goes out, and its first "look out for" item says why. See
[Running on a server](server.md).
