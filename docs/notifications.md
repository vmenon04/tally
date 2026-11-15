# Pending purchases and notifications

## Pending purchases

Between a purchase and the day it posts, the bank lists it as pending. For each account the
ledger links to SimpleFIN ([Importing](importing.md#from-simplefin-instead)), every `tally
sync` stores what SimpleFIN lists as pending, and those are the account's pending entries in
`pending.beancount`, in reports, in the weekly summary and in Sure. One that has posted is
matched to its posted row (same account, dated a day before to three days after, agreeing
merchant name, same amount or up to 30% more for a tip, any amount for a $1.00 gas station
check, less for a round hold of $50 or more, exactly the same for money in; the full rules
are at the top of `src/tally/matching.py`), so a category you set on it carries over. One
SimpleFIN stops listing (a hold released, a charge refunded) drops out.

Alerts read from the banks' alert emails before SimpleFIN are still in the database; its
answers cover them, so they don't count.

## Notifications

```sh
uv run tally ntfy-setup           # once: makes a random topic in secrets/ntfy-topic
uv run tally notify-test          # check your phone gets it
```

Subscribe to the printed topic in the ntfy app. After each `tally sync` (not a dry run),
tally sends headlines only, never merchants or amounts:

- new pending purchases (low priority);
- a bank that needs you to sign in again at SimpleFIN;
- an account whose balance differs from what the bank says;
- SimpleFIN failing, once it has failed for 3 hours (so one timed-out run stays quiet), then
  again every 6 hours while it lasts;
- a week with no new transactions at all, which means something upstream has stopped.

Each is sent once (a quiet week once per quiet spell); one that fails to send is retried on
the next run. `ntfy_server` in `config.toml` changes the server (default `https://ntfy.sh`).
Anyone with the topic can read the notifications, so keep it private.
