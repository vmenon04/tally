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
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

from tally import (
    advice,
    alert_reading,
    categorizing,
    chat,
    config,
    corrections,
    gmail,
    importing,
    lookups,
    mailer,
    matching,
    notify,
    pending,
    plan,
    report,
    rules,
    simplefin,
    snaptrade,
    store,
    summary,
    summary_email,
    summary_note,
    sure,
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

    commands.add_parser(
        "sure-login",
        help="save Sure's API key",
        description="Save an API key made in Sure's settings, after checking Sure accepts it.",
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

    commands.add_parser(
        "simplefin-login",
        help="claim a SimpleFIN setup token and save its access",
        description="Ask for a setup token from bridge.simplefin.org (hidden input), claim it "
        "for an access URL, and save that in secrets/. A token can be claimed only once.",
    )
    simplefin_parser = commands.add_parser(
        "simplefin",
        help="show what SimpleFIN sees for your accounts",
        description="List the accounts SimpleFIN Bridge sees, their balances, and how many "
        "transactions it has from the last 30 days. Read-only.",
    )
    simplefin_parser.add_argument(
        "--transactions",
        metavar="LAST4",
        help="instead, list the last 30 days' transactions of the account ending in these digits",
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

    chat_parser = commands.add_parser(
        "chat",
        help="answer Sure's chat from the ledger",
        description="Answer Sure's chat (its External assistant) on a Unix socket until "
        "stopped, with Claude working from the ledger. Requests need the token in "
        "secrets/chat-token. See docs/server.md.",
    )
    chat_parser.add_argument("--socket", type=Path, required=True, help="listen on this socket")

    lookups_parser = commands.add_parser(
        "lookups",
        help="pass Sure's read-only tools to Claude (tally chat starts it)",
        description="An MCP server on stdin and stdout offering Sure's tools that only read, "
        "adding each result to the record file. tally chat starts it for Claude.",
    )
    lookups_parser.add_argument("--sure", required=True, help="where Sure listens")
    lookups_parser.add_argument("--token-file", type=Path, required=True)
    lookups_parser.add_argument("--record", type=Path, required=True)

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
    if args.command == "sure-login":
        return _sure_login()
    if args.command == "report":
        return _report(*args.month)
    if args.command == "summary":
        return _summary(args.as_of, email=args.email, sync=args.sync)
    if args.command == "sync":
        return _sync(dry_run=args.dry_run)
    if args.command == "simplefin-login":
        return _simplefin_login()
    if args.command == "simplefin":
        return _simplefin(transactions_of=args.transactions)
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
    if args.command == "fava":
        try:
            viewing.serve(data_dir(), socket=args.socket, port=args.port)
        except (book.LedgerError, OSError) as e:
            print(f"fava: {e}", file=sys.stderr)
            return 1
        return 0
    if args.command == "chat":
        return _chat(args.socket)
    if args.command == "lookups":
        token = categorizing.load_token(args.token_file)
        if token is None:
            print(f"lookups: no token in {args.token_file}", file=sys.stderr)
            return 1
        lookups.serve(lookups.sure(args.sure, token), args.record, sys.stdin, sys.stdout)
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


def _sure_login() -> int:
    """Ask for a Sure API key, check Sure accepts it, and save it."""
    path = config.secrets_dir() / sure.API_KEY_FILE
    try:
        url = config.load(data_dir() / "config.toml").sure_url
        key = getpass.getpass(f"API key for Sure at {url} (input is hidden): ").strip()
        names = sure.account_names(sure.Client(url, key))
    except (config.ConfigError, sure.SureError) as e:
        print(f"sure-login: {e}", file=sys.stderr)
        return 1
    except (EOFError, KeyboardInterrupt):
        print("\nsure-login: cancelled, nothing saved.", file=sys.stderr)
        return 1
    config.write_secret(path, key + "\n")
    print(f"Sure accepted it. Saved to {path} (readable only by you).")
    print(f"Sure has {len(names)} account(s): {', '.join(names) or 'none yet'}")
    return 0


def _simplefin_login() -> int:
    """Claim a SimpleFIN setup token, save the access URL, and say what it can see."""
    path = config.secrets_dir() / simplefin.ACCESS_FILE
    try:
        token = getpass.getpass("SimpleFIN setup token (input is hidden): ").strip()
        access = simplefin.claim(token)
    except simplefin.SimpleFINError as e:
        print(f"simplefin-login: {e}", file=sys.stderr)
        return 1
    except (EOFError, KeyboardInterrupt):
        print("\nsimplefin-login: cancelled, nothing saved.", file=sys.stderr)
        return 1
    config.write_secret(path, access + "\n")
    print(f"Claimed. Saved the access to {path} (readable only by you).")
    return _simplefin(balances_only=True)


def _simplefin(*, balances_only: bool = False, transactions_of: str | None = None) -> int:
    path = config.secrets_dir() / simplefin.ACCESS_FILE
    access = categorizing.load_token(path)
    if access is None:
        print(f"simplefin: no access saved at {path}; run `tally simplefin-login`", file=sys.stderr)
        return 1
    today = datetime.datetime.now().astimezone().date()
    try:
        found = simplefin.fetch(
            access, today - datetime.timedelta(days=30), balances_only=balances_only
        )
    except simplefin.SimpleFINError as e:
        print(f"simplefin: {e}", file=sys.stderr)
        return 1
    if transactions_of is None:
        print("\n".join(simplefin.overview(found, today)))
        return 0
    lines = simplefin.transactions(found, transactions_of)
    if lines is None:
        print(f"simplefin: no account ending in {transactions_of}", file=sys.stderr)
        return 1
    print("\n".join(lines))
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
    if as_of is None and email:
        # Only when emailing: the review is written once, so a summary only printed would
        # use it up.
        result = _with_review(result, today, settings)
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
# Days of SimpleFIN rows each sync asks for: enough to catch up after a missed week.
BANK_DAYS = 14
# Seconds for the summary's note: much less than the summary service's time limit.
NOTE_TIMEOUT = 60


def _quiet_sync() -> str | None:
    """Run a sync without printing it. None if it worked, else why it didn't, in a sentence."""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = _sync(dry_run=False)
    lines = [line.strip() for line in [*err.getvalue().splitlines(), *out.getvalue().splitlines()]]
    if code == 0:
        # It worked, but Sure may not match: that's worth hearing about too.
        sure_lines = [
            line.removeprefix("sync: ")
            for line in lines
            if line.startswith(("sync: Sure", "sync: SimpleFIN"))
        ]
        return "; ".join(sure_lines) + "." if sure_lines else None
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
            folder,
            conn,
            _ask_claude,
            dry_run=dry_run,
            err=sys.stderr,
            brokerage=_brokerage(),
            sure=_sure(),
            bank=_bank(),
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
    for problem in done.bank_problems:
        print(f"sync: SimpleFIN: {problem}", file=sys.stderr)
    for account, new in done.banked.items():
        verb = "would add" if dry_run else "added"
        print(f"\nFrom SimpleFIN, {account} ({verb} {len(new)}):")
        print("\n".join(f"  {t.date}  {t.amount:>10}  {t.merchant}" for t in new))
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
    if done.sure_failed:
        print(f"sync: Sure not updated: {done.sure_failed}", file=sys.stderr)
    elif done.sure is not None:
        print(f"\nSure: {done.sure.summary()}.")
        for name, theirs, mine in done.sure.differ:
            print(f"  {name}: Sure says {theirs:,.2f}, the ledger {mine:,.2f}")
            # Without amounts: the scheduled runs log errors, and nothing that names money.
            print(
                f"sync: Sure's balance for {name} differs from the ledger's "
                "(`tally sync --dry-run` says by how much)",
                file=sys.stderr,
            )
    return 0


def _bank() -> Callable[[], simplefin.Answer] | None:
    """SimpleFIN, the last BANK_DAYS of it, once its access is saved."""
    access = categorizing.load_token(config.secrets_dir() / simplefin.ACCESS_FILE)
    if access is None:
        return None
    since = datetime.datetime.now().astimezone().date() - datetime.timedelta(days=BANK_DAYS)
    return lambda: simplefin.fetch(access, since)


def _sure() -> sure.Client | None:
    """Sure's API, once `tally sure-login` has saved a key."""
    folder = config.secrets_dir()
    if not (folder / sure.API_KEY_FILE).exists():
        return None
    return sure.Client.from_secrets(config.load(data_dir() / "config.toml").sure_url, folder)


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


def _with_review(
    result: summary.Summary, today: datetime.date, settings: config.Config
) -> summary.Summary:
    """The summary with Claude's review of last month, if it's due; why not, if it fails."""
    folder = data_dir()
    if not book.exists(folder):
        return result
    try:
        found, about = picture.from_folder(
            book.folder(folder), today, settings.roth_limit, settings.roth_elsewhere
        )
        written = advice.review(
            folder, found, about, _ask_claude, datetime.datetime.now(datetime.UTC)
        )
    except (book.LedgerError, accounts.AccountsError, plan.PlanError) as e:
        problem = f"No review of last month from Claude: {e}"
    except categorizing.ClaudeError as e:
        problem = f"No review of last month from Claude (tried again next week): {e}"
    else:
        return dataclasses.replace(result, review=written)
    return dataclasses.replace(result, look_out=[*result.look_out, problem])


def _chat(socket: Path) -> int:
    token = categorizing.load_token(config.secrets_dir() / "chat-token")
    if token is None:
        print(f"chat: no token in {config.secrets_dir() / 'chat-token'}", file=sys.stderr)
        return 1
    folder = data_dir()
    # Sure's MCP token: without it, Claude answers from the facts alone.
    sure_token = config.secrets_dir() / "sure-mcp-token"
    program = str(Path(sys.executable).with_name("tally"))

    def answer(said: list[chat.Message]) -> str:
        today = datetime.datetime.now().astimezone().date()
        settings = config.load(folder / "config.toml")
        found, about = picture.from_folder(
            book.folder(folder), today, settings.roth_limit, settings.roth_elsewhere
        )
        known = advice.facts(found, about)
        if not sure_token.exists():
            return chat.reply(said, known, _ask_claude)
        with tempfile.TemporaryDirectory() as temporary:
            record = Path(temporary) / "looked-up"
            record.touch()
            mcp = chat.lookups_config(program, settings.sure_url, sure_token.resolve(), record)
            claude_token = categorizing.load_token(config.secrets_dir() / "claude-token")

            def ask(question: str, schema: dict[str, Any]) -> Any:
                return categorizing.ask_claude(
                    question, schema, claude_token, mcp=mcp, allowed=chat.LOOKUPS
                )

            return chat.reply(said, known, ask, record.read_text)

    try:
        server = chat.server(socket, token, answer)
    except OSError as e:
        print(f"chat: {e}", file=sys.stderr)
        return 1
    with server:
        server.serve_forever()
    return 0


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
