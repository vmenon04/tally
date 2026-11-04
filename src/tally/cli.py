import argparse
import contextlib
import dataclasses
import datetime
import getpass
import html
import io
import os
import re
import sys
from pathlib import Path
from typing import Any

from tally import (
    alert_reading,
    budget,
    categorizing,
    config,
    corrections,
    gmail,
    importing,
    mailer,
    matching,
    notify,
    pending,
    report,
    rules,
    sheet,
    store,
    summary,
    summary_email,
    summary_note,
    transactions_tab,
)
from tally.alerts import Alert
from tally.ledger import accounts, book, categories, updating, viewing, weekly
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
        help="read the alerts mailbox",
        description="Fetch the Discover and BofA alert emails tally hasn't read yet from the "
        "alerts mailbox, store each alert once, and list the alerts no imported CSV row "
        "explains yet.",
    )
    alerts_parser.add_argument(
        "--reread",
        action="store_true",
        help="read the emails already fetched from the mailbox again, after a new alert "
        "type is supported",
    )
    alerts_parser.add_argument(
        "--dry-run", action="store_true", help="show what would be stored without saving"
    )

    commands.add_parser(
        "gmail-login",
        help="save the alerts mailbox's app password",
        description="Save the alerts mailbox's app password, after checking Gmail accepts it.",
    )

    ntfy_parser = commands.add_parser(
        "ntfy-setup",
        help="turn on push notifications",
        description="Make a random ntfy topic, save it to secrets/, and show how to subscribe.",
    )
    ntfy_parser.add_argument(
        "--new-topic", action="store_true", help="replace the topic with a new one"
    )
    commands.add_parser(
        "notify-test",
        help="send a test notification",
        description="Send a test notification to the ntfy topic.",
    )

    report_parser = commands.add_parser(
        "report",
        help="show a month's spending per category",
        description="Show a month's spending per category, sorted by the rules in rules.toml.",
    )
    report_parser.add_argument("month", type=_month, help="the month, like 2026-10")

    summary_parser = commands.add_parser(
        "summary",
        help="show the weekly summary",
        description="The past week's spending, the month so far against the Budget tab's plan, "
        "what to look out for, and what's coming up.",
    )
    summary_parser.add_argument(
        "--as-of",
        type=_date,
        help="the summary as it would have been on this day, like 2026-10-18 (default: today)",
    )
    summary_parser.add_argument(
        "--email",
        action="store_true",
        help="also email it, from the alerts mailbox to summary_to in config.toml",
    )
    summary_parser.add_argument(
        "--sync",
        action="store_true",
        help="run `tally sync` first; if it fails, the summary says why",
    )

    sync_parser = commands.add_parser(
        "sync",
        help="update the Google Sheet",
        description="Bring the Budget tab up to date with the imported transactions.",
    )
    sync_parser.add_argument(
        "--dry-run", action="store_true", help="show what would change without changing it"
    )

    fava_parser = commands.add_parser(
        "fava",
        help="browse the ledger in Fava",
        description="Serve Fava for the ledger in data/ledger until stopped. It listens only "
        "on this machine; from your own machine, use `tally-remote fava`.",
    )
    fava_parser.add_argument(
        "--socket", type=Path, help="listen on this Unix socket instead of 127.0.0.1"
    )
    fava_parser.add_argument("--port", type=int, default=5000, help="default: 5000")

    args = parser.parse_args(argv)
    if args.command == "import":
        ok = importing.run(
            args.files, data_dir(), dry_run=args.dry_run, out=sys.stdout, err=sys.stderr
        )
        return 0 if ok else 1
    if args.command == "alerts":
        if not args.reread:
            return _alerts_from_gmail(dry_run=args.dry_run)
        ok = alert_reading.reread(data_dir(), dry_run=args.dry_run, out=sys.stdout, err=sys.stderr)
        return 0 if ok else 1
    if args.command == "gmail-login":
        return _gmail_login()
    if args.command == "ntfy-setup":
        return _ntfy_setup(args.new_topic)
    if args.command == "notify-test":
        return _notify_test()
    if args.command == "report":
        return _report(*args.month)
    if args.command == "summary":
        return _summary(args.as_of, email=args.email, sync=args.sync)
    if args.command == "sync":
        return _sync(dry_run=args.dry_run)
    if args.command == "fava":
        try:
            viewing.serve(data_dir(), socket=args.socket, port=args.port)
        except (book.LedgerError, OSError) as e:
            print(f"fava: {e}", file=sys.stderr)
            return 1
        return 0
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


def _date(text: str) -> datetime.date:
    try:
        return datetime.date.fromisoformat(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected a date like 2026-10-18, got {text!r}") from None


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
    except config.ConfigError as e:
        print(f"alerts: {e}", file=sys.stderr)
        return 1
    try:
        password = gmail.load_password(gmail_password_file())
        mailbox = gmail.ImapMailbox.connect(account, password)
    except gmail.GmailError as e:
        print(f"alerts: {e}", file=sys.stderr)
        if not dry_run:
            _notify([], mailbox_ok=False)
        return 1
    with mailbox:
        run = alert_reading.run_gmail(
            data_dir(), mailbox, account, dry_run=dry_run, out=sys.stdout, err=sys.stderr
        )
    if not dry_run:
        events = [notify.Event("new", f"new:{a.message_id}") for a in run.added]
        events += [notify.Event("refused", f"refused:{i}") for i in run.refused]
        events += [notify.Event("not_found", f"not-found:{a.message_id}") for a in run.not_found]
        _notify(events, mailbox_ok=run.failure is None)
    return 0 if run.ok else 1


def ntfy_topic_file() -> Path:
    return config.secrets_dir() / "ntfy-topic"


def _sender() -> notify.NtfySender:
    """The ntfy sender from config.toml and the saved topic. Raises NotifyError or ConfigError."""
    settings = config.load(data_dir() / "config.toml")
    return notify.NtfySender(settings.ntfy_server, notify.load_topic(ntfy_topic_file()))


def _notify(events: list[notify.Event], *, mailbox_ok: bool) -> None:
    """Announce the events not announced before. Never fails the command.

    mailbox_ok says whether this run read the mailbox; a failure is announced only once it
    has lasted a while (see notify.MAILBOX_GRACE).
    """
    try:
        sender = _sender()
    except notify.NotifyError:
        print("\nNotifications are off: run `tally ntfy-setup` to turn them on.")
        return
    except config.ConfigError as e:
        print(f"notify: {e}", file=sys.stderr)
        return
    importing.private_dir(data_dir())
    conn = store.connect(data_dir() / "tally.db")
    try:
        now = datetime.datetime.now(datetime.UTC)
        if mailbox_ok:
            notify.mailbox_read(conn)
        else:
            events = [*events, *notify.mailbox_failed(conn, now)]
        events = [*events, *notify.stale(conn, now)]
        for message in notify.send_new(conn, sender, events, now, sys.stderr):
            print(f"Notified: {message}")
    finally:
        conn.close()


def _ntfy_setup(new_topic: bool) -> int:
    path = ntfy_topic_file()
    try:
        server = config.load(data_dir() / "config.toml").ntfy_server
        topic = notify.load_topic(path) if path.exists() and not new_topic else None
    except (config.ConfigError, notify.NotifyError) as e:
        print(f"ntfy-setup: {e}", file=sys.stderr)
        return 1
    if topic is None:
        topic = notify.new_topic()
        notify.save_topic(path, topic)
        print(f"Saved a new topic to {path} (readable only by you).")
    else:
        print(f"Already set up ({path}); `tally ntfy-setup --new-topic` replaces the topic.")
    print(
        "\nIn the ntfy app: + (subscribe to topic), then\n"
        f"  topic:  {topic}\n"
        f"  server: {server}\n"
        "Treat the topic like a password: anyone who has it can read your notifications.\n"
        "Then run `tally notify-test`."
    )
    return 0


def _notify_test() -> int:
    try:
        _sender().send("test notification", "default")
    except (config.ConfigError, notify.NotifyError) as e:
        print(f"notify-test: {e}", file=sys.stderr)
        return 1
    print("Sent. It should be on your phone within a few seconds.")
    return 0


def _report(year: int, month: int) -> int:
    folder = data_dir()
    db_path = folder / "tally.db"
    if not db_path.exists():
        print(f"Nothing imported yet: no database at {db_path}.", file=sys.stderr)
        return 1
    pairs, alerts = _stored(db_path)
    stored = [t for _, t in pairs]
    waiting = [t for _, t in pending.transactions(alerts, stored)]
    today = datetime.datetime.now().astimezone().date()
    try:
        month_rules = _rules(folder / "rules.toml", alerts, stored, today)
    except rules.RulesError as e:
        print(f"rules.toml: {e}", file=sys.stderr)
        return 1
    print(report.format_report(report.build([*stored, *waiting], month_rules, year, month)))
    in_month = sum(t.date.year == year and t.date.month == month for t in waiting)
    if in_month:
        print(
            f"\nIncludes {in_month} pending card alert(s) not yet in a bank export; "
            "their amounts can change when they post."
        )
    return 0


def _summary(as_of: datetime.date | None, *, email: bool, sync: bool) -> int:
    folder = data_dir()
    db_path = folder / "tally.db"
    if not db_path.exists():
        print(f"Nothing imported yet: no database at {db_path}.", file=sys.stderr)
        return 1
    sync_problem = _quiet_sync() if sync else None
    pairs, alerts = _stored(db_path)
    posted = [t for _, t in pairs]
    today = as_of or datetime.datetime.now().astimezone().date()
    try:
        settings = config.load(folder / "config.toml")
        month_rules = _rules(folder / "rules.toml", alerts, posted, today)
    except (config.ConfigError, rules.RulesError) as e:
        print(f"summary: {e}", file=sys.stderr)
        return 1
    sender, recipient = settings.gmail_account, settings.summary_to
    if email and (sender is None or recipient is None):
        print(
            "summary: to email it, config.toml needs gmail_account (the sender) and "
            "summary_to (the recipient)",
            file=sys.stderr,
        )
        return 1

    targets, note = None, None
    try:
        target = sheet.GoogleSheet(
            settings.sheet_id, config.secrets_dir() / "google-service-account.json"
        )
        targets = budget.read_targets(target.values(budget.RANGE))
    except (sheet.SheetError, budget.BudgetError) as e:
        note = f"(Couldn't read the Budget tab, so there's no plan to compare with: {e})"

    waiting = pending.transactions(alerts, posted)
    not_found = matching.match([a for _, a in alerts], posted).not_found
    waiting_alerts = matching.match([a for _, a in alerts], posted).waiting
    ledger_problems = _ledger_problems(
        [t for _, t in [*pairs, *waiting]], waiting_alerts, month_rules
    )
    result = summary.build(
        today,
        [*pairs, *waiting],
        month_rules,
        targets,
        not_found,
        summary.Roth(settings.roth_limit, settings.roth_elsewhere),
        note,
    )
    if sync_problem:
        result = dataclasses.replace(result, look_out=[sync_problem, *result.look_out])
    if ledger_problems:
        result = dataclasses.replace(result, look_out=[*result.look_out, *ledger_problems])
    if as_of is None:
        result = _with_claude_note(result)
    text = summary.format_summary(result)
    ledger_text, ledger_html = _ledger_report(today, settings)
    if ledger_text:
        text = f"{text}\n\n{ledger_text}"
    print(text)
    if not email or sender is None or recipient is None:
        return 0
    link = f"https://docs.google.com/spreadsheets/d/{settings.sheet_id}"
    msg = mailer.message(
        sender,
        recipient,
        summary.title(result),
        f"{text}\n\nBudget: {link}\n",
        summary_email.format_html(result, link, ledger_html),
    )
    try:
        mailer.send(sender, gmail.load_password(gmail_password_file()), msg)
    except (gmail.GmailError, mailer.MailError) as e:
        print(f"summary: not emailed: {e}", file=sys.stderr)
        return 1
    print(f"\nEmailed to {recipient}.")
    return 0


def _ledger_report(today: datetime.date, settings: config.Config) -> tuple[str, str]:
    """The ledger's report as text and HTML, or a line saying why there isn't one."""
    if not book.exists(data_dir()):
        return "", ""
    try:
        found = weekly.build(
            book.folder(data_dir()), today, settings.roth_limit, settings.roth_elsewhere
        )
    except (weekly.ReportError, OSError) as e:
        line = f"(No report from the ledger this week: {e})"
        return line, f"<p>{html.escape(line)}</p>"
    return weekly.format_text(found), weekly.format_html(found)


def _ledger_problems(
    counted: list[Transaction], waiting: list[Alert], month_rules: rules.Rules
) -> list[str]:
    """For the weekly summary: how the ledger and tally disagree, if they do."""
    try:
        found = updating.check(data_dir(), counted, waiting, month_rules)
    except (book.LedgerError, accounts.AccountsError, OSError) as e:
        return [f"The ledger couldn't be checked: {e}"]
    return found.problems() if found is not None else []


# Said when Claude can't be asked; the sync goes on without it, so it's never why one stopped.
CLAUDE_FAILED = "sync: Claude didn't categorize"
# Seconds for the summary's note: much less than the summary service's time limit.
NOTE_TIMEOUT = 60


def _quiet_sync() -> str | None:
    """Run a sync without printing it. None if it worked, else why it didn't, in a sentence."""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = _sync(dry_run=False)
    if code == 0:
        return None
    lines = [line.strip() for line in [*err.getvalue().splitlines(), *out.getvalue().splitlines()]]
    reason = next(
        (
            line
            for line in lines
            if (
                line.startswith(("sync:", "Nothing written")) and not line.startswith(CLAUDE_FAILED)
            )
            or "tally didn't write" in line
            or "hold a value tally didn't write" in line
        ),
        "it stopped with an error",
    )
    return (
        f"The Sheet wasn't updated before this summary: "
        f"{reason.removeprefix('sync: ').rstrip(':.')}. Run `tally sync` to see the details."
    )


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
        alerts = store.alerts_with_ids(conn)
        stored = [t for _, t in pairs]
        waiting = pending.transactions(alerts, stored)
        waiting_alerts = matching.match([a for _, a in alerts], stored).waiting
        # Every alert can be corrected under its pending row's ID, even one that has posted
        # since the tab was written: carry_over then moves the correction to the posted row.
        by_id = dict(pairs) | {-i: pending.as_transaction(a) for i, a in alerts}
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
        text = rules_path.read_text(encoding="utf-8")
        try:
            fixes = corrections.Result(text, [])
            if changed and not added:
                fixes = corrections.apply(text, changed, today)
            carried = corrections.carry_over(fixes.text, pending.posted(alerts, stored), today)
            counted = [t for _, t in [*pairs, *waiting]]
            try:
                guessed = categorizing.add(carried.text, counted, _ask_claude, today)
            except categorizing.ClaudeError as e:
                print(f"{CLAUDE_FAILED}: {e}", file=sys.stderr)
                guessed = categorizing.Result(carried.text, [])
            fixes = corrections.Result(guessed.text, [*fixes.messages, *carried.messages])
            month_rules = rules.parse(fixes.text)
        except rules.RulesError as e:
            print(f"sync: correcting rules.toml: {e}", file=sys.stderr)
            return 1

        months = sorted({(t.date.year, t.date.month) for t in counted})
        reports = [report.build(counted, month_rules, year, month) for year, month in months]
        try:
            result = budget.plan(grid, reports, month_rules.categories, (today.year, today.month))
        except budget.BudgetError as e:
            print(f"sync: {e}", file=sys.stderr)
            return 1
        conflicts = budget.find_conflicts(result, store.sheet_writes(conn, settings.sheet_id))
        tab_rows = transactions_tab.rows([*pairs, *waiting], month_rules)

        heading = "Dry run: nothing will be changed." if dry_run else "Syncing."
        print(f"{heading} Sheet: {settings.sheet_id}")
        if fixes.messages:
            saved = "not saved, dry run" if dry_run else "saved to rules.toml"
            print(f"\nCorrections ({saved}):")
            print("\n".join(f"  {message}" for message in fixes.messages))
        if guessed.messages:
            saved = "not saved, dry run" if dry_run else "saved to rules.toml"
            print(f"\nCategorized by Claude ({saved}):")
            print("\n".join(f"  {message}" for message in guessed.messages))
        print()
        print(budget.format_plan(result, conflicts))
        uncategorized = sum(r.category == rules.UNCATEGORIZED for r in tab_rows)
        counts = f"{uncategorized} uncategorized"
        if waiting:
            counts = f"{len(waiting)} pending, {counts}"
        print(
            f"\nTransactions tab: {'rewrite' if has_tab else 'create'} with {len(tab_rows)} "
            f"rows ({counts})"
        )
        if added:
            print()
            print(transactions_tab.format_edits(added))
        if conflicts or added:
            return 1
        if dry_run:
            _sync_ledger(counted, waiting_alerts, month_rules, dry_run=True)
            return 0
        if result.missing_rows or result.missing_months:
            print("\nNothing written: the Budget tab is missing rows or columns.", file=sys.stderr)
            return 1

        if fixes.text != text:
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
        _sync_ledger(counted, waiting_alerts, month_rules, dry_run=False)
        return 0
    finally:
        conn.close()


def _sync_ledger(
    counted: list[Transaction], waiting: list[Alert], month_rules: rules.Rules, *, dry_run: bool
) -> None:
    """The ledger's part of a sync. Its problems are reported, never a reason the sync failed."""
    try:
        done = updating.sync(
            data_dir(), counted, waiting, month_rules, dry_run=dry_run, err=sys.stderr
        )
    except (book.LedgerError, accounts.AccountsError, updating.NotUpToDate, OSError) as e:
        print(f"\nLedger: not updated: {e}", file=sys.stderr)
        return
    if done is None:
        return
    if done.changes:
        verb = "would move" if dry_run else "moved"
        print(f"\nLedger: {verb} {len(done.changes)} entries to their current category:")
        print("\n".join(f"  {categories.describe(c)}" for c in done.changes))
    if done.parity.ok:
        print(
            f"\nLedger: all {done.parity.rows} rows and every month's totals match; "
            "bean-check is clean."
        )
    else:
        print("\nLedger: " + " ".join(done.parity.problems()))
        print("\n".join(done.parity.details()[:20]))


def _with_claude_note(result: summary.Summary) -> summary.Summary:
    """The summary with Claude's note, or saying why there isn't one; it's sent either way."""
    try:
        note = summary_note.write(summary.format_summary(result), _ask_claude_quickly)
    except categorizing.ClaudeError as e:
        return dataclasses.replace(result, look_out=[*result.look_out, f"No note from Claude: {e}"])
    return dataclasses.replace(result, claude_note=note)


def _ask_claude_quickly(question: str, schema: dict[str, Any]) -> Any:
    token = categorizing.load_token(config.secrets_dir() / "claude-token")
    return categorizing.ask_claude(question, schema, token, timeout=NOTE_TIMEOUT)


def _ask_claude(question: str, schema: dict[str, Any]) -> Any:
    token = categorizing.load_token(config.secrets_dir() / "claude-token")
    return categorizing.ask_claude(question, schema, token)


def _unused_tab_id(tabs: dict[str, int]) -> int:
    tab_id = 1
    while tab_id in tabs.values():
        tab_id += 1
    return tab_id


def _stored(db_path: Path) -> tuple[list[tuple[int, Transaction]], list[tuple[int, Alert]]]:
    """The stored transactions and alerts, with their IDs."""
    conn = store.connect(db_path)
    try:
        return store.transactions_with_ids(conn), store.alerts_with_ids(conn)
    finally:
        conn.close()


def _rules(
    path: Path, alerts: list[tuple[int, Alert]], stored: list[Transaction], today: datetime.date
) -> rules.Rules:
    """The rules, with corrections made on pending rows moved to the rows that posted since.

    Only sync saves that move; report and summary apply it as they read, so they agree with
    the Sheet in between.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise rules.RulesError(f"no rules file at {path}") from None
    return rules.parse(corrections.carry_over(text, pending.posted(alerts, stored), today).text)
