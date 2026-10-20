import argparse
import datetime
import getpass
import os
import re
import sys
from pathlib import Path

from tally import (
    alert_reading,
    budget,
    config,
    corrections,
    gmail,
    importing,
    report,
    rules,
    sheet,
    store,
    transactions_tab,
)
from tally.transaction import Transaction


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="tally", description="Personal finance tracker.")
    commands = parser.add_subparsers(dest="command")

    import_parser = commands.add_parser(
        "import",
        help="import bank and card CSV exports",
        description="Import Discover and BofA CSV exports. With no files, imports every file "
        "in the inbox folder and removes each one once it is stored.",
    )
    import_parser.add_argument("files", nargs="*", type=Path, help="files to import")
    import_parser.add_argument(
        "--dry-run", action="store_true", help="show what would be imported without saving"
    )

    alerts_parser = commands.add_parser(
        "alerts",
        help="read saved alert emails",
        description="Read the Discover and BofA alert emails saved as .eml files in the alerts "
        "folder (or, with --gmail, in the alerts mailbox), store each alert once, and list the "
        "alerts no imported CSV row explains yet.",
    )
    alerts_parser.add_argument(
        "--gmail", action="store_true", help="read the alerts mailbox instead of the folder"
    )
    alerts_parser.add_argument(
        "--dry-run", action="store_true", help="show what would be stored without saving"
    )

    commands.add_parser(
        "gmail-login",
        help="save the alerts mailbox's app password",
        description="Save the alerts mailbox's app password, after checking Gmail accepts it.",
    )

    report_parser = commands.add_parser(
        "report",
        help="show a month's spending per category",
        description="Show a month's spending per category, sorted by the rules in rules.toml.",
    )
    report_parser.add_argument("month", type=_month, help="the month, like 2026-10")

    sync_parser = commands.add_parser(
        "sync",
        help="update the Google Sheet",
        description="Bring the Budget tab up to date with the imported transactions.",
    )
    sync_parser.add_argument(
        "--dry-run", action="store_true", help="show what would change without changing it"
    )

    args = parser.parse_args(argv)
    if args.command == "import":
        ok = importing.run(
            args.files, data_dir(), dry_run=args.dry_run, out=sys.stdout, err=sys.stderr
        )
        return 0 if ok else 1
    if args.command == "alerts":
        if args.gmail:
            return _alerts_from_gmail(dry_run=args.dry_run)
        ok = alert_reading.run(data_dir(), dry_run=args.dry_run, out=sys.stdout, err=sys.stderr)
        return 0 if ok else 1
    if args.command == "gmail-login":
        return _gmail_login()
    if args.command == "report":
        return _report(*args.month)
    if args.command == "sync":
        return _sync(dry_run=args.dry_run)
    parser.print_help()
    return 0


def data_dir() -> Path:
    """Where the database, inbox, alerts and downloads live: TALLY_DATA_DIR, else ./data."""
    return Path(os.environ.get("TALLY_DATA_DIR", "data"))


def _month(text: str) -> tuple[int, int]:
    match = re.fullmatch(r"(\d{4})-(\d{2})", text)
    if not match or not 1 <= int(match[2]) <= 12:
        raise argparse.ArgumentTypeError(f"expected a month like 2026-10, got {text!r}")
    return int(match[1]), int(match[2])


def gmail_password_file() -> Path:
    return config.secrets_dir() / "gmail-app-password"


def _gmail_account() -> str:
    account = config.load(data_dir() / "config.toml").gmail_account
    if account is None:
        raise config.ConfigError(
            "set gmail_account in config.toml to the address the alerts are forwarded to"
        )
    return account


def _gmail_login() -> int:
    """Ask for the alerts mailbox's app password, check Gmail accepts it, and save it."""
    try:
        account = _gmail_account()
        typed = getpass.getpass(f"App password for {account} (input is hidden): ")
        password = gmail.app_password(typed)
        with gmail.ImapMailbox.connect(account, password):
            pass
    except (config.ConfigError, gmail.GmailError) as e:
        print(f"gmail-login: {e}", file=sys.stderr)
        return 1
    except (EOFError, KeyboardInterrupt):
        print("\ngmail-login: cancelled, nothing saved.", file=sys.stderr)
        return 1
    gmail.save_password(gmail_password_file(), password)
    print(f"Gmail accepted it. Saved to {gmail_password_file()} (readable only by you).")
    return 0


def _alerts_from_gmail(dry_run: bool) -> int:
    try:
        account = _gmail_account()
        password = gmail.load_password(gmail_password_file())
        mailbox = gmail.ImapMailbox.connect(account, password)
    except (config.ConfigError, gmail.GmailError) as e:
        print(f"alerts: {e}", file=sys.stderr)
        return 1
    with mailbox:
        ok = alert_reading.run_gmail(
            data_dir(), mailbox, account, dry_run=dry_run, out=sys.stdout, err=sys.stderr
        )
    return 0 if ok else 1


def _report(year: int, month: int) -> int:
    folder = data_dir()
    db_path = folder / "tally.db"
    if not db_path.exists():
        print(f"Nothing imported yet: no database at {db_path}.", file=sys.stderr)
        return 1
    try:
        month_rules = rules.load(folder / "rules.toml")
    except rules.RulesError as e:
        print(f"rules.toml: {e}", file=sys.stderr)
        return 1
    stored = _stored_transactions(db_path)
    print(report.format_report(report.build(stored, month_rules, year, month)))
    return 0


def _sync(dry_run: bool) -> int:
    folder = data_dir()
    db_path = folder / "tally.db"
    if not db_path.exists():
        print(f"Nothing imported yet: no database at {db_path}.", file=sys.stderr)
        return 1
    try:
        settings = config.load(folder / "config.toml")
        rules_path = folder / "rules.toml"
        month_rules = rules.load(rules_path)
        target = sheet.GoogleSheet(
            settings.sheet_id,
            config.secrets_dir() / "google-service-account.json",
            write=not dry_run,
        )
        tabs = target.tabs()
        if budget.TAB not in tabs:
            print(f"sync: the Sheet has no {budget.TAB!r} tab.", file=sys.stderr)
            return 1
        grid = target.values(budget.RANGE)
        has_tab = transactions_tab.TAB in tabs
        tab_grid = target.values(transactions_tab.RANGE) if has_tab else []
    except (config.ConfigError, rules.RulesError, sheet.SheetError) as e:
        print(f"sync: {e}", file=sys.stderr)
        return 1

    conn = store.connect(db_path)
    try:
        pairs = store.transactions_with_ids(conn)
        by_id = dict(pairs)
        today = datetime.datetime.now().astimezone().date()
        try:
            edits = transactions_tab.find_edits(
                tab_grid, store.sheet_categories(conn, settings.sheet_id)
            )
        except transactions_tab.TransactionsTabError as e:
            print(f"sync: {e}", file=sys.stderr)
            return 1
        added = [e for e in edits if not (isinstance(e.id, int) and e.id in by_id and e.was)]
        changed = [
            (by_id[e.id], str(e.now)) for e in edits if e not in added and isinstance(e.id, int)
        ]
        fixes = None
        if changed and not added:
            try:
                fixes = corrections.apply(rules_path.read_text(encoding="utf-8"), changed, today)
                month_rules = rules.parse(fixes.text)
            except rules.RulesError as e:
                print(f"sync: correcting rules.toml: {e}", file=sys.stderr)
                return 1

        stored = [t for _, t in pairs]
        months = sorted({(t.date.year, t.date.month) for t in stored})
        reports = [report.build(stored, month_rules, year, month) for year, month in months]
        try:
            result = budget.plan(grid, reports, month_rules.categories, (today.year, today.month))
        except budget.BudgetError as e:
            print(f"sync: {e}", file=sys.stderr)
            return 1
        conflicts = budget.find_conflicts(result, store.sheet_writes(conn, settings.sheet_id))
        tab_rows = transactions_tab.rows(pairs, month_rules)

        heading = "Dry run: nothing will be changed." if dry_run else "Syncing."
        print(f"{heading} Sheet: {settings.sheet_id}")
        if fixes:
            saved = "not saved, dry run" if dry_run else "saved to rules.toml"
            print(f"\nCorrections from the Transactions tab ({saved}):")
            print("\n".join(f"  {message}" for message in fixes.messages))
            for suggestion in fixes.suggestions:
                print(f"\n{suggestion}")
        print()
        print(budget.format_plan(result, conflicts))
        uncategorized = sum(r.category == rules.UNCATEGORIZED for r in tab_rows)
        print(
            f"\nTransactions tab: {'rewrite' if has_tab else 'create'} with {len(tab_rows)} "
            f"rows ({uncategorized} uncategorized)"
        )
        if added:
            print()
            print(transactions_tab.format_edits(added))
        if conflicts or added:
            return 1
        if dry_run:
            return 0
        if result.missing_rows or result.missing_months:
            print("\nNothing written: the Budget tab is missing rows or columns.", file=sys.stderr)
            return 1

        if fixes:
            corrections.save(rules_path, fixes.text)
        requests = [sheet.number_cell(tabs[budget.TAB], c.cell, c.new) for c in result.changes]
        requests += transactions_tab.requests(
            tabs.get(transactions_tab.TAB), _unused_tab_id(tabs), tab_rows, month_rules
        )
        try:
            target.batch_update(requests)
        except sheet.SheetError as e:
            print(f"sync: {e}. Nothing was written.", file=sys.stderr)
            return 1
        store.record_sheet_writes(
            conn,
            settings.sheet_id,
            [(c.row, c.month, c.new) for c in [*result.changes, *result.unchanged]],
        )
        store.replace_sheet_categories(
            conn, settings.sheet_id, {r.id: r.category for r in tab_rows}
        )
        print(
            f"\nUpdated {len(result.changes)} Budget cells and wrote {len(tab_rows)} rows "
            "to the Transactions tab."
        )
        return 0
    finally:
        conn.close()


def _unused_tab_id(tabs: dict[str, int]) -> int:
    tab_id = 1
    while tab_id in tabs.values():
        tab_id += 1
    return tab_id


def _stored_transactions(db_path: Path) -> list[Transaction]:
    conn = store.connect(db_path)
    try:
        return store.transactions(conn)
    finally:
        conn.close()
