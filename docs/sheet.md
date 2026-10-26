# Reports and the Google Sheet

`data/rules.toml` sorts transactions into categories (the format is described at the top of
`src/tally/rules.py`). `uv run tally report 2026-10` shows a month's spending per category.

`uv run tally sync` fills in the Budget tab of the Sheet named in `data/config.toml`
(`sheet_id = "..."`): each month's category actuals, and actual Roth IRA contributions up to
the current month (for the current month, only once a contribution has gone in, so the
month's planned amount stays until then). `--dry-run` lists the cells it would change without
changing them. It never overwrites a value it didn't write itself (such as one you typed); it lists those and
writes nothing until you clear them. It also rewrites a Transactions tab (created on the
first sync) listing every transaction, newest first, with its category. Changing a category
there is how you correct one: the next sync saves it as a rule for that one transaction in a
marked section at the top of `rules.toml` (clearing the category removes it again).
Rows added to that tab by hand stop the sync.

Before writing, sync asks Claude to categorize the merchants no rule covers. It runs Claude
Code with every tool turned off (`claude -p`, logged in with the token from `claude
setup-token` saved in `secrets/claude-token`), sends only the merchant names, amounts and
accounts, with your categories and merchant rules as examples, and saves each answer as a
rule for that merchant in a marked section at the bottom of `rules.toml`, so your own rules
and corrections always win. Claude can answer only with one of your categories, or leave a
merchant uncategorized. Correcting a row Claude categorized also corrects its rule for that merchant.
If Claude can't be reached, sync says so and goes on without it.

Card alerts that no imported CSV reaches yet count as *pending*, in the report and on both
tabs, so the Sheet stays current between imports. They're shown in grey italics with
"(pending)" after the account, and the next import replaces each with its posted row (whose
amount may differ, for example with a tip). Their categories come from the same rules, and you
correct them like any other row. When the purchase posts, sync moves your correction to the
posted row, even if the bank names, dates or prices it differently. Alerts a CSV should show
but doesn't (declined or reversed) aren't counted; `tally alerts` lists them.

Sync signs in as a Google service account whose key is `secrets/google-service-account.json`;
share only the Sheet with that account. `TALLY_SECRETS_DIR` sets the secrets folder (default: `secrets`).
