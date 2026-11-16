# Pending purchases and notifications

## Pending purchases

Between a purchase and the day it posts, the bank lists it as pending. Each `tally sync` keeps
what SimpleFIN lists as pending for your linked accounts, and shows it in `pending.beancount`,
the weekly summary and Sure.

Once one posts, it's matched to its posted row (same account, a day before to three days after,
a merchant name that agrees, and the same amount, allowing for tips, gas station holds and the
like; the full rules are at the top of `src/tally/matching.py`), so a category you set on it
carries over. One SimpleFIN stops listing (a hold released, a charge refunded) drops out.

## Notifications

tally can send headlines to your phone through [ntfy](https://ntfy.sh), free and with no
account:

```sh
tally ntfy-setup
tally notify-test
```

- `tally ntfy-setup`: Once: makes a random topic in secrets/ntfy-topic.
- `tally notify-test`: Check your phone gets it.

Subscribe to the printed topic in the ntfy app. After each `tally sync` (not a dry run), tally
sends headlines only, never merchants or amounts:

- new pending purchases (low priority);
- a bank that needs you to sign in again at SimpleFIN;
- an account whose balance differs from what the bank says;
- SimpleFIN failing for 3 hours, then again every 6 hours while it lasts;
- a week with no new transactions at all, which means something upstream has stopped.

Each is sent once; one that fails to send is retried on the next run. Anyone with the topic can
read the notifications, so keep it private. `ntfy_server` in `config.toml` changes the server.
