# Weekly summary

```sh
uv run tally summary                      # as of today
uv run tally summary --as-of 2026-10-18   # as it would have been on that day
uv run tally summary --email              # print it and email it
uv run tally summary --email --sync       # run `tally sync` first, then email it
```

It covers the 7 days up to today (or the `--as-of` day; anything later is left out):

- **This week:** every purchase, pending ones marked, against what the plan allows per week.
- **The month so far:** each category's spending against its plan, and what's left per day.
  In a month's first days, how the month before ended.
- **Look out for:** categories over plan or ahead of pace, big purchases, uncategorized
  transactions, alerts missing from a bank export, the monthly import when it's due, and
  anything wrong with the ledger (a row missing or doubled, a bean-check error, a
  balance that differs from the bank's).
- **Coming up:** next month's paychecks, planned spending and Roth IRA transfer, and whether the
  year's planned Roth IRA contributions fit the limit.
- **From the ledger:** a bean-query report: spending per category this month so far, the same
  days last month and all of last month, and the year's Roth IRA contributions.

Categories are the ledger's, so a category you set in the ledger shows here. The plan comes from the
ledger's `plan.beancount` ([The Beancount ledger](ledger.md#your-plan)). Spending counts by the
day a purchase happened and includes pending alerts.

The advice comes from fixed rules (the thresholds are at the top of `src/tally/summary.py`).
Without `--as-of`, the finished summary then goes to Claude, which adds a short note of advice
at the top; it can only comment, a note quoting a dollar amount the summary doesn't show is
refused, and if there's no note the summary still goes out and says why. If the ledger can't
be read, the summary says so and uses `rules.toml` alone.

The first emailed summary after a month ends also has Claude's review of that month: a
headline, then what went well, what to watch and what to try next month. Claude gets the
ledger's numbers written out as sentences (each month's pay, spending and savings; this
month's categories against the plan; the merchants you spend most at; goals, investments,
Roth IRA room and net worth), plus the `custom "about"` lines in `plan.beancount`. So those
figures go to Anthropic, as merchant names already do for categorizing. It has no tools
and does no sums: a review quoting a dollar amount those numbers don't show is asked again,
then refused, and the summary says so and tries again the next week. Each review is kept
in `data/advice/`, which is how tally knows it was sent; a summary only printed, not
emailed, never writes one.

For the Roth IRA check, add to `data/config.toml`:

```toml
roth_limit = 7500        # this year's contribution limit
roth_elsewhere = 140     # contributed this year where tally can't see it (before tally, other accounts)
```

## By email

`--email` sends the same text from the alerts mailbox (`gmail_account`) to the address in
`summary_to`:

```toml
summary_to = "you@example.com"
```

It signs in to Gmail's SMTP server with the app password `tally gmail-login` saved, so there's
nothing else to set up. The summary is printed either way; if sending fails, the command says
why and exits with an error.

On the server, `tally-summary.timer` runs `tally sync` and sends it every Sunday at 6pm New York
time. If the sync fails, the summary still goes out, and its first "look out for" item says
why. See [Running on a server](server.md).
