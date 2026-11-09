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
from collections.abc import Callable
from pathlib import Path
from typing import Any

from tally import (
    advice,
    alert_reading,
    categorizing,
    config,
    corrections,
    gmail,
    importing,
    mailer,
    matching,
    notify,
    pending,
    plan,
    report,
    rules,
    snaptrade,
    store,
    summary,
    summary_email,
    summary_note,
)
from tally.alerts import Alert
from tally.importers import fidelity
from tally.ledger import accounts, book, categories, picture, updating, viewing, weekly
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
        description="The past week's spending, the month so far against your plan, "
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
        help="bring the ledger's categories up to date",
        description="Keep categories you set on pending purchases, let Claude categorize new "
        "merchants, move ledger entries out of Uncategorized, and check the ledger.",
    )
    sync_parser.add_argument(
        "--dry-run", action="store_true", help="show what would change without changing it"
    )

    snaptrade_parser = commands.add_parser(
        "snaptrade",
        help="show what SnapTrade has for your brokerage accounts",
        description="List the accounts SnapTrade can see, and the kinds of positions and "
        "activities it has for each, without amounts. Read-only.",
    )
    snaptrade_parser.add_argument(
        "--activities",
        metavar="LAST4",
        help="instead, list every activity of the account ending in these digits, with amounts",
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

    web_parser = commands.add_parser(
        "web",
        help="serve the Tally app: your money at a glance, with advice",
        description="Serve the Tally app for the ledger in data/ledger until stopped. It "
        "listens only on this machine, like `tally fava`, and links to Fava for details.",
    )
    web_parser.add_argument(
        "--socket", type=Path, help="listen on this Unix socket instead of 127.0.0.1"
    )
    web_parser.add_argument("--port", type=int, default=5001, help="default: 5001")
    web_parser.add_argument(
        "--fava-port",
        type=int,
        help="the port Fava is reached on, on the same host name (default: Fava's pages are "
        "linked on this app's own address)",
    )

    args = parser.parse_args(argv)
    if args.command == "import":
        ok = importing.run(
            args.files, data_dir(), dry_run=args.dry_run, out=sys.stdout, err=sys.stderr
        )
        return 0 if ok else 1
    if args.command == "alerts":
        if not args.reread:
            code = _alerts_from_gmail(dry_run=args.dry_run)
            if not args.dry_run:
                _advise()
            return code
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
    if args.command == "snaptrade":
        try:
            client = snaptrade.Client.from_secrets(config.secrets_dir())
            if args.activities:
                print("\n".join(snaptrade.activities(client.get, args.activities)))
            else:
                print("\n".join(snaptrade.overview(client.get)))
        except snaptrade.SnapTradeError as e:
            print(f"snaptrade: {e}", file=sys.stderr)
            return 1
        return 0
    if args.command == "web":
        try:
            viewing.serve_tally(
                data_dir(), socket=args.socket, port=args.port, fava_port=args.fava_port
            )
        except (book.LedgerError, OSError) as e:
            print(f"web: {e}", file=sys.stderr)
            return 1
        return 0
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


def _advise() -> None:
    """Answer questions asked on the Tally page, and review last month once it's over.

    Quiet when there's nothing to do; never fails the alerts run.
    """
    folder = data_dir()
    if not book.exists(folder):
        return
    waiting = any(q.answer is None for q in advice.questions(folder, limit=20))
    today = datetime.datetime.now().astimezone().date()
    if not waiting and advice.reviewed(folder, _last_month(today)):
        return
    try:
        found, about = picture.from_folder(book.folder(folder), today)
    except (book.LedgerError, accounts.AccountsError, plan.PlanError) as e:
        print(f"advice: {e}", file=sys.stderr)
        return
    now = datetime.datetime.now(datetime.UTC)
    done = advice.run(folder, found, about, _ask_claude, now)
    events = [notify.Event("answer", f"answer:{i}") for i in done.answered]
    if done.reviewed:
        events.append(notify.Event("review", f"review:{done.reviewed}"))
    if events:
        _notify(events, mailbox_ok=True)


def _last_month(today: datetime.date) -> str:
    return f"{today.replace(day=1) - datetime.timedelta(days=1):%Y-%m}"


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
        month_rules, _ = _categories_and_plan(folder, alerts, stored, today)
    except rules.RulesError as e:
        print(f"rules.toml: {e}", file=sys.stderr)
        return 1
    except (book.LedgerError, accounts.AccountsError, plan.PlanError) as e:
        print(f"report: {e}", file=sys.stderr)
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
    note = None
    try:
        settings = config.load(folder / "config.toml")
        month_rules = _rules(folder / "rules.toml", alerts, posted, today)
    except (config.ConfigError, rules.RulesError) as e:
        print(f"summary: {e}", file=sys.stderr)
        return 1
    targets: plan.Targets | None = None
    try:
        month_rules, targets = _categories_and_plan(folder, alerts, posted, today)
    except (book.LedgerError, accounts.AccountsError, plan.PlanError) as e:
        note = f"(Couldn't read the ledger, so categories come from the rules alone: {e})"
    if targets is not None and not targets.monthly:
        note = "(The ledger has no plan yet, so there's nothing to compare with.)"
    sender, recipient = settings.gmail_account, settings.summary_to
    if email and (sender is None or recipient is None):
        print(
            "summary: to email it, config.toml needs gmail_account (the sender) and "
            "summary_to (the recipient)",
            file=sys.stderr,
        )
        return 1

    waiting = pending.transactions(alerts, posted)
    not_found = matching.match([a for _, a in alerts], posted).not_found
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
    ledger_problems = _ledger_problems(db_path)
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
    msg = mailer.message(
        sender,
        recipient,
        summary.title(result),
        f"{text}\n",
        summary_email.format_html(result, ledger_html),
    )
    try:
        mailer.send(sender, gmail.load_password(gmail_password_file()), msg)
    except (gmail.GmailError, mailer.MailError) as e:
        print(f"summary: not emailed: {e}", file=sys.stderr)
        return 1
    print(f"\nEmailed to {recipient}.")
    return 0


def _categories_and_plan(
    folder: Path, alerts: list[tuple[int, Alert]], stored: list[Transaction], today: datetime.date
) -> tuple[rules.Rules, plan.Targets | None]:
    """The rules with the ledger's categories over them, and the plan from the ledger.

    Raises LedgerError, AccountsError, PlanError, RulesError.
    """
    month_rules = _rules(folder / "rules.toml", alerts, stored, today)
    if not book.exists(folder):
        return month_rules, None
    ledger = book.load(book.folder(folder), month_rules)
    targets = plan.read(ledger.entries, ledger.accounts, today)
    return dataclasses.replace(month_rules, overrides=categories.in_ledger(ledger)), targets


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


def _ledger_problems(db_path: Path) -> list[str]:
    """For the weekly summary: rows the ledger lacks or doubles, and bean-check's errors."""
    conn = store.connect(db_path)
    try:
        found = updating.check(data_dir(), conn)
    except (book.LedgerError, accounts.AccountsError, rules.RulesError, OSError) as e:
        return [f"The ledger couldn't be checked: {e}"]
    finally:
        conn.close()
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
        (line for line in lines if line.startswith("sync:") and not line.startswith(CLAUDE_FAILED)),
        "it stopped with an error",
    )
    return (
        f"The ledger wasn't brought up to date before this summary: "
        f"{reason.removeprefix('sync: ').rstrip(':.')}. Run `tally sync` to see the details."
    )


def _sync(dry_run: bool) -> int:
    folder = data_dir()
    db_path = folder / "tally.db"
    if not db_path.exists():
        print(f"Nothing imported yet: no database at {db_path}.", file=sys.stderr)
        return 1
    if not book.exists(folder):
        print(f"sync: there's no ledger at {book.folder(folder)}.", file=sys.stderr)
        return 1
    conn = store.connect_copy(db_path) if dry_run else store.connect(db_path)
    try:
        done = updating.sync(
            folder, conn, _ask_claude, dry_run=dry_run, err=sys.stderr, brokerage=_brokerage()
        )
    except (
        book.LedgerError,
        accounts.AccountsError,
        rules.RulesError,
        updating.NotUpToDate,
    ) as e:
        print(f"sync: {e}", file=sys.stderr)
        return 1
    finally:
        conn.close()
    if done is None:
        return 1

    print("Dry run: nothing will be changed." if dry_run else "Bringing the ledger up to date.")
    saved = "not saved, dry run" if dry_run else "saved to rules.toml"
    for title, messages in (
        ("Categories you set on pending purchases", done.kept),
        ("Moved to the purchases that have posted", done.carried),
        ("Categorized by Claude", done.guessed),
    ):
        if messages:
            print(f"\n{title} ({saved}):")
            print("\n".join(f"  {message}" for message in messages))
    if done.claude_failed:
        print(f"{CLAUDE_FAILED}: {done.claude_failed}", file=sys.stderr)
    if done.holdings_failed:
        print(f"sync: Fidelity holdings not updated: {done.holdings_failed}", file=sys.stderr)
    elif done.holdings:
        verb = "would add" if dry_run else "added"
        print(f"\nFidelity holdings from SnapTrade ({verb} {done.holdings_added} ledger entries):")
        print("\n".join(f"  {line}" for line in done.holdings))
    if done.changes:
        verb = "Would move" if dry_run else "Moved"
        print(f"\n{verb} {len(done.changes)} entries out of Uncategorized:")
        print("\n".join(f"  {categories.describe(c)}" for c in done.changes))
    if done.check is not None:
        if done.check.ok:
            print(
                f"\nLedger: all {done.check.rows} imported rows are there once; "
                "bean-check is clean."
            )
        else:
            print("\nLedger: " + " ".join(done.check.problems()))
            print("\n".join(done.check.details()[:20]))
    return 0


def _brokerage() -> Callable[[], fidelity.Snapshot] | None:
    """Today's holdings from SnapTrade, if its keys are in secrets/; else None."""
    folder = config.secrets_dir()
    if not (folder / snaptrade.CLIENT_ID_FILE).exists():
        return None

    def fetch() -> fidelity.Snapshot:
        client = snaptrade.Client.from_secrets(folder)
        return snaptrade.snapshot(client.get, datetime.datetime.now().astimezone().date())

    return fetch


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

    Only sync and import save that move; report and summary apply it as they read, so they
    agree with the ledger in between.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise rules.RulesError(f"no rules file at {path}") from None
    return rules.parse(corrections.carry_over(text, pending.posted(alerts, stored), today).text)
