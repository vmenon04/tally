# Weekly summary

```sh
tally summary
tally summary --as-of 2026-10-18
tally summary --email
tally summary --email --sync
```

- `tally summary`: As of today.
- `tally summary --as-of 2026-10-18`: As it would have been on that day.
- `tally summary --email`: Print it and email it.
- `tally summary --email --sync`: Run `tally sync` first, then email it.

It covers the 7 days up to today (or the `--as-of` day):

- **This week:** every purchase, pending ones marked, against what your plan allows per week.
- **The month so far:** each category's spending against its plan, and what's left per day.
  In a month's first days, how the month before ended.
- **Look out for:** categories over plan or ahead of pace, big purchases, uncategorized
  transactions, an account with nothing posted since last month, and anything wrong with the
  ledger (a row missing or doubled, a bean-check error, a balance that differs from the bank's).
- **Coming up:** next month's paychecks and planned spending.
- **From the ledger:** spending per category this month, the same days last month, and all of
  last month.

Categories and the plan come from the ledger ([Your plan](ledger.md#your-plan)). Spending counts
by the day a purchase happened and includes pending ones. The thresholds for "look out for" are
at the top of `src/tally/summary.py`.

## Claude's note and monthly review

If [Claude Code](https://claude.com/claude-code) is installed and logged in (or
`secrets/claude-token` holds a token from `claude setup-token`), Claude adds a short note of
advice at the top. The first emailed summary after a month ends also has Claude's review of that
month: what went well, what to watch and what to try next month.

Claude gets the summary, or for the review, the ledger's numbers written out as sentences
(pay, spending, savings, categories against the plan, top merchants, goals, investments, net
worth), plus your `custom "about"` lines. Those figures go to Anthropic. A note or review quoting
a dollar amount those numbers don't show is refused, and the summary goes out without it. Each
review is kept in `data/advice/`.

## Roth IRA check (optional)

If you contribute to a Roth IRA, add to `data/config.toml`:

```toml
roth_limit = 7500        # this year's contribution limit
roth_elsewhere = 0       # contributed this year where tally can't see it
```

and plan transfers in `plan.beancount` (`custom "roth-plan" 600.00 USD`). "Coming up" then says
whether the year's planned contributions fit the limit.

## By email

`--email` sends the same text from a Gmail account to any address:

```toml
gmail_account = "you@gmail.com"
summary_to = "you@example.com"
```

Then make a Gmail [app password](https://myaccount.google.com/apppasswords) and save it:

```sh
tally gmail-login
```

The summary is printed either way; if sending fails, the command says why and exits with an
error. To send it every week, run `tally summary --email --sync` from cron, or on a
[server](server.md#on-a-schedule), where the schedule does it on Sunday evenings.
