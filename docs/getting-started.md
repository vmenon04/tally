# Getting started

This sets tally up on your own computer (macOS or Linux): your accounts linked, your
transactions coming in, and the ledger open in Fava. Everything else is optional and can come
later.

## 1. Install

You need git and [uv](https://docs.astral.sh/uv/getting-started/installation/). uv installs the
right Python for you.

```sh
git clone https://github.com/vmenon04/tally.git
cd tally
uv sync
uv run tally --help
```

Every command below runs from this folder. To type `tally` instead of `uv run tally`, add
`alias tally="uv run --project ~/path/to/tally tally"` to your shell's rc file.

## 2. Start from the example

```sh
cp -R example data
```

`data/` is where everything about your money lives: the ledger, your categories and tally's
database. Git never commits it. The example has a checking account and a credit card; you'll
change them to yours below.

| File | What it is |
|---|---|
| `data/rules.toml` | Your budget categories, and rules that sort merchants into them |
| `data/config.toml` | Settings, all optional |
| `data/ledger/accounts.beancount` | Your accounts, and one account per category |
| `data/ledger/opening.beancount` | What each account held when you started |
| `data/ledger/plan.beancount` | Your monthly budget |

Optionally, make the ledger a git repo (`git -C data/ledger init`): tally then commits every
change it makes, so you can see and undo them.

## 3. Connect your banks

[SimpleFIN Bridge](https://bridge.simplefin.org) reads your accounts for you, read-only (it
costs about $15 a year). Sign up, connect your banks there, and make a **setup token**. Then:

```sh
uv run tally simplefin-login
uv run tally simplefin
```

- `uv run tally simplefin-login`: Paste the setup token; it's saved in secrets/.
- `uv run tally simplefin`: Lists every account the bridge sees.

Each account is listed with the last four digits of its number, like `Everyday Checking
(1234)`. You'll use those digits next.

The bridge allows about 24 requests a day. Each `tally sync` and each `tally simplefin` uses one.

## 4. Describe your accounts

Open `data/ledger/accounts.beancount`. For each account whose transactions you want, write an
`open` line with two pieces of metadata:

```beancount
2026-01-01 open Assets:Bank:Checking USD
  tally-account: "checking"      ; any short name you like
  simplefin-account: "1234"      ; the last four digits from `tally simplefin`
```

- Bank accounts go under `Assets:`, credit cards under `Liabilities:`.
- Remove the example accounts you don't have.
- A savings account that only receives transfers from checking needs just
  `simplefin-account:`, not `tally-account:`: its deposits already come in from checking's side,
  and tally only checks its balance. Add `interest-from: "Income:Interest"` (and open
  `Income:Interest`) to bring in its interest too.

Any bank works. Bank of America checking (`tally-account: "bofa_checking"`) and Discover
(`"discover"`) get extra cleanup that makes their merchant names tidier; other banks' merchant
names are the descriptions the bank gives.

## 5. Set your opening balances

tally's first sync brings in the last 14 days. Open `data/ledger/opening.beancount` and change
the date to 15 days ago, with one line per account. Don't worry about getting the amounts
right yet: the first sync tells you what they should be.

Every account's `open` date must be on or before this date.

## 6. Pick your categories

`data/rules.toml` lists your budget categories. Change them to whatever you like, but keep
`accounts.beancount` in step: every category needs an `Expenses:` account with a matching
`category:` line. tally refuses to run, and says which one is missing, when they don't match.

Set a monthly amount per category in `data/ledger/plan.beancount`.

## 7. Your first sync

```sh
uv run tally sync --dry-run
uv run tally sync
```

- `uv run tally sync --dry-run`: Shows what would happen, changes nothing.

The sync brings in your posted and pending transactions, sorts them by your rules, and checks
the ledger. For each account whose balance doesn't match the bank's, it prints both:

```
Assets:Bank:Checking: the bank says 2,340.12, the ledger's posted rows 2,500.00
```

Change that account's opening balance by the difference (here, lower it by 159.88), and sync
again. Once nothing differs, the ledger matches your bank; from then on, a difference means a
transaction went missing or came in twice. Pending purchases don't count on either side.

## 8. Look at it

```sh
uv run tally summary
uv run tally fava
```

The summary shows this week, the month against your plan, and what to look out for. Fava then
serves the ledger at http://localhost:5000 until you stop it with Ctrl-C.

## 9. Fix categories

Merchants no rule covers land in `Expenses:Uncategorized`. Two ways to sort them:

- **One transaction:** in Fava, open it and change `Expenses:Uncategorized` to the right
  account. tally never changes a category you set.
- **Every time from now on:** add a rule to `data/rules.toml`:

  ```toml
  [[rule]]
  match = "TRADER JOE"
  category = "Groceries"
  ```

  The next sync moves everything still uncategorized that the rule covers.

If [Claude Code](https://claude.com/claude-code) is installed and logged in, each sync asks
Claude to categorize new merchants and saves its answers as rules. Without it, they stay
uncategorized until you sort them.

## 10. Keep it up to date

Run `uv run tally sync` whenever you like. To run it every two hours on its own, add a line
to `crontab -e`:

```
0 */2 * * * cd ~/path/to/tally && ~/.local/bin/uv run tally sync >/dev/null
```

(`which uv` shows where uv is.)

## Next

- [Push notifications](notifications.md) for new purchases and problems.
- [The weekly summary by email](summary.md#by-email).
- [More about the ledger](ledger.md): savings goals, statement balances, investments.
- [Running on a server](server.md), with Sure as a web and phone UI.
