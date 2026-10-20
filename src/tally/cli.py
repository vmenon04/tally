import argparse
import datetime
import os
import re
import sys
from pathlib import Path

from tally import budget, config, importing, report, rules, sheet, store
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
    if args.command == "report":
        return _report(*args.month)
    if args.command == "sync":
        return _sync(dry_run=args.dry_run)
    parser.print_help()
    return 0


def data_dir() -> Path:
    """Where the database, inbox and archived downloads live: TALLY_DATA_DIR, else ./data."""
    return Path(os.environ.get("TALLY_DATA_DIR", "data"))


def _month(text: str) -> tuple[int, int]:
    match = re.fullmatch(r"(\d{4})-(\d{2})", text)
    if not match or not 1 <= int(match[2]) <= 12:
        raise argparse.ArgumentTypeError(f"expected a month like 2026-10, got {text!r}")
    return int(match[1]), int(match[2])


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
    if not dry_run:
        print("tally sync only supports --dry-run so far.", file=sys.stderr)
        return 1
    folder = data_dir()
    db_path = folder / "tally.db"
    if not db_path.exists():
        print(f"Nothing imported yet: no database at {db_path}.", file=sys.stderr)
        return 1
    try:
        settings = config.load(folder / "config.toml")
        month_rules = rules.load(folder / "rules.toml")
        target = sheet.GoogleSheet(
            settings.sheet_id, config.secrets_dir() / "google-service-account.json"
        )
        grid = target.values(budget.RANGE)
    except (config.ConfigError, rules.RulesError, sheet.SheetError) as e:
        print(f"sync: {e}", file=sys.stderr)
        return 1

    stored = _stored_transactions(db_path)
    months = sorted({(t.date.year, t.date.month) for t in stored})
    reports = [report.build(stored, month_rules, year, month) for year, month in months]
    today = datetime.datetime.now().astimezone().date()
    try:
        changes = budget.plan(grid, reports, month_rules.categories, (today.year, today.month))
    except budget.BudgetError as e:
        print(f"sync: Budget tab: {e}", file=sys.stderr)
        return 1
    print(f"Dry run: nothing will be changed. Sheet: {settings.sheet_id}")
    print()
    print(budget.format_plan(changes))
    return 0


def _stored_transactions(db_path: Path) -> list[Transaction]:
    conn = store.connect(db_path)
    try:
        return store.transactions(conn)
    finally:
        conn.close()
