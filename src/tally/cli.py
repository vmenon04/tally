import argparse
import os
import re
import sys
from pathlib import Path

from tally import importing, report, rules, store


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

    args = parser.parse_args(argv)
    if args.command == "import":
        ok = importing.run(
            args.files, data_dir(), dry_run=args.dry_run, out=sys.stdout, err=sys.stderr
        )
        return 0 if ok else 1
    if args.command == "report":
        return _report(*args.month)
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
    conn = store.connect(db_path)
    try:
        stored = store.transactions(conn)
    finally:
        conn.close()
    print(report.format_report(report.build(stored, month_rules, year, month)))
    return 0
