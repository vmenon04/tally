"""Keeping the ledger up to date as exports and alerts come in, and on each sync.

The ledger is the record. tally.db keeps what tally needs to read files and alerts
(deduplicating rows, matching alerts to posted rows), and every row it takes in goes to the
ledger too. Categories are the ledger's: rules.toml (yours, and Claude's) only fills in the
ones nothing has decided yet. Nothing here runs unless data/ledger/main.beancount exists.
"""

import datetime
import hashlib
import json
import sqlite3
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any, TextIO

from beancount.core import data

from tally import categorizing, corrections, matching, pending, rules, simplefin, store
from tally.alerts import Alert
from tally.importers import ParseError
from tally.importers.fidelity import Snapshot
from tally.ledger import (
    accounts,
    book,
    categories,
    entries,
    history,
    importers,
    mirror,
    parity,
    positions,
)
from tally.snaptrade import SnapTradeError
from tally.sure import Client as SureClient
from tally.sure import SureError
from tally.transaction import Transaction, identity

RULES = "rules.toml"

type Ask = Callable[[str, dict[str, Any]], Any]


@dataclass(slots=True)
class Session:
    data_dir: Path
    ledger: book.Ledger
    rules: rules.Rules
    added: int = 0


def open_session(data_dir: Path, conn: sqlite3.Connection | None = None) -> Session | None:
    """The ledger and the rules, or None if there's no ledger.

    With conn, the rules carry categories set on pending alerts over to the rows that
    posted for them. Raises LedgerError, RulesError.
    """
    if not book.exists(data_dir):
        return None
    month_rules = rules_for(data_dir, conn) if conn is not None else rules.load(data_dir / RULES)
    return Session(data_dir, book.load(book.folder(data_dir), month_rules), month_rules)


def rules_for(data_dir: Path, conn: sqlite3.Connection) -> rules.Rules:
    """rules.toml, with corrections made on pending alerts moved to the rows that posted."""
    text = _rules_text(data_dir)
    stored = store.transactions(conn)
    alerts = store.alerts_with_ids(conn)
    return rules.parse(corrections.carry_over(text, pending.posted(alerts, stored), _today()).text)


def extract(
    session: Session, tally_account: str, text: str, file_name: str
) -> list[data.Directive]:
    """The entries a file would add to the ledger, without adding them."""
    if tally_account == "fidelity_positions":
        positions = importers.FidelityPositions(session.ledger.accounts)
        extracted = positions.extract_text(text, file_name, session.ledger.entries)
        return book.new_entries(extracted, session.ledger)
    importer = importers.for_account(tally_account, session.ledger.accounts, session.rules)
    return book.new_entries(importer.extract_text(text, file_name), session.ledger)


def add(session: Session, new: list[data.Directive]) -> None:
    book.add(session.ledger, new)
    session.ledger.entries.extend(new)
    session.added += len(new)


def refresh_pending(session: Session, conn: sqlite3.Connection) -> list[str]:
    """Keep categories you changed on pending entries, then rewrite pending.beancount.

    A change becomes a one-transaction rule in rules.toml, so it survives the rewrite and
    carries over to the posted row. Returns the messages for those changes.
    """
    messages = _keep_pending_fixes(session, conn)
    found = matching.match(store.alerts(conn), store.transactions(conn), store.fetched(conn))
    book.write_pending(session.ledger, found.waiting, session.rules)
    return messages


def refresh_pending_in(data_dir: Path, conn: sqlite3.Connection, err: TextIO) -> None:
    """refresh_pending for the alerts run, reporting problems instead of stopping it."""
    try:
        session = open_session(data_dir, conn)
        if session is not None:
            refresh_pending(session, conn)
    except (book.LedgerError, rules.RulesError, accounts.AccountsError) as e:
        print(f"ledger: pending alerts not written: {e}", file=err)


class NotUpToDate(Exception):
    """The ledger has commits from elsewhere that can't be brought in."""


def bring_up_to_date(data_dir: Path, err: TextIO) -> None:
    """Before tally changes the ledger: commit edits made here, then pull ones made elsewhere.

    Raises NotUpToDate if pulling conflicts; an unreachable Forgejo is only reported.
    """
    folder = book.folder(data_dir)
    if not (book.exists(data_dir) and history.is_repo(folder)):
        return
    try:
        history.commit(folder, "Save changes made outside tally")
        history.pull(folder)
    except history.Unreachable as e:
        print(f"ledger: {e}; going on without the latest from Forgejo", file=err)
    except history.HistoryError as e:
        raise NotUpToDate(str(e)) from e


def save(folder: Path, message: str, err: TextIO) -> None:
    """Commit what tally changed, and push it to Forgejo and the backup.

    A failure is reported and the next run retries. Each push is tried on its own, so the
    backup still gets the commit when Forgejo is down.
    """
    if not history.is_repo(folder):
        return
    try:
        history.commit(folder, message)
    except history.HistoryError as e:
        print(f"ledger: {e}; the next run tries again", file=err)
        return
    for push in (history.push, history.push_backup):
        try:
            push(folder)
        except history.HistoryError as e:
            print(f"ledger: {e}; the next run tries again", file=err)


@dataclass(slots=True)
class SyncResult:
    kept: list[str] = field(default_factory=list)  # pending categories you set, now rules
    carried: list[str] = field(default_factory=list)  # moved to the rows that posted
    guessed: list[str] = field(default_factory=list)  # Claude's new merchant rules
    claude_failed: str | None = None
    changes: list[categories.Change] = field(default_factory=list)
    holdings: list[str] = field(default_factory=list)  # per account: cost and market value
    holdings_added: int = 0  # ledger entries from the brokerage snapshot
    holdings_failed: str | None = None
    check: parity.Parity | None = None
    sure: mirror.Plan | None = None  # what Sure needed to match the ledger
    sure_failed: str | None = None
    banked: dict[str, list[Transaction]] = field(default_factory=dict)  # new from SimpleFIN
    bank_failed: str | None = None  # why SimpleFIN couldn't be asked, if it couldn't
    bank_problems: list[str] = field(default_factory=list)  # what it said went wrong
    pending: list[Alert] = field(default_factory=list)  # what it lists as pending
    # Ledger account, what the bank says it holds, and what the ledger's posted rows add to.
    bank_differ: list[tuple[str, Decimal, Decimal]] = field(default_factory=list)
    _bank: simplefin.Answer | None = None


def sync(
    data_dir: Path,
    conn: sqlite3.Connection,
    ask: Ask,
    *,
    dry_run: bool,
    err: TextIO,
    brokerage: Callable[[], Snapshot] | None = None,
    sure: SureClient | None = None,
    bank: Callable[[], simplefin.Answer] | None = None,
) -> SyncResult | None:
    """`tally sync`: bring the ledger's categories up to date, then check it.

    0. with bank (SimpleFIN), rows posted since the last import are stored and added;
    1. categories you set on pending entries become rules (they'd be lost otherwise);
    2. rules made on pending alerts move to the rows that have posted since;
    3. Claude categorizes merchants nothing covers yet;
    4. imported entries still in Uncategorized move to the category the rules now give;
    5. pending.beancount is rewritten;
    6. with brokerage (SnapTrade), today's holdings are recorded, as a positions export would;
    7. the ledger is committed and pushed, and checked;
    8. with sure, Sure's transactions are made to match the ledger's.

    A dry run works it all out and saves nothing. None if there's no ledger.
    Raises LedgerError, AccountsError, RulesError or NotUpToDate.
    """
    if not book.exists(data_dir):
        return None
    if not dry_run:
        bring_up_to_date(data_dir, err)
    today = _today()
    text = _rules_text(data_dir)
    result = SyncResult()
    if bank is not None:
        _import_from_bank(data_dir, conn, bank, result, text, dry_run=dry_run, today=today)

    stored = store.transactions(conn)
    alerts = store.alerts_with_ids(conn)
    fixes = _pending_fixes(book.load(book.folder(data_dir), rules.parse(text)), alerts)
    if fixes:
        kept = corrections.apply(text, fixes, today)
        text, result.kept = kept.text, kept.messages
    carried = corrections.carry_over(text, pending.posted(alerts, stored), today)
    text, result.carried = carried.text, carried.messages
    waiting = matching.match([a for _, a in alerts], stored, store.fetched(conn)).waiting
    counted = [*stored, *(pending.as_transaction(a) for a in waiting)]
    try:
        guessed = categorizing.add(text, counted, ask, today)
        text, result.guessed = guessed.text, guessed.messages
    except categorizing.ClaudeError as e:
        result.claude_failed = str(e)
    month_rules = rules.parse(text)

    ledger = book.load(book.folder(data_dir), month_rules)
    result.changes = categories.plan(ledger, counted, month_rules)
    if dry_run:
        if brokerage is not None:
            _record_holdings(ledger, brokerage, result, dry_run=True)
        _check_bank(ledger, result, today, dry_run=True)
        ledger = book.with_pending(ledger, book.pending_entries(ledger, waiting, month_rules))
        result.check = parity.compare(ledger, counted)
        if sure is not None:
            _mirror(ledger, sure, result, dry_run=True)
        return result

    if text != _rules_text(data_dir):
        corrections.save(data_dir / RULES, text)
    categories.apply(result.changes)
    book.write_pending(ledger, waiting, month_rules)
    if brokerage is not None:
        _record_holdings(book.load(ledger.folder, month_rules), brokerage, result, dry_run=False)
    if result.changes or result.holdings_added or any(result.banked.values()):
        save(ledger.folder, "Bring the ledger up to date", err)
    ledger = book.load(ledger.folder, month_rules)
    _check_bank(ledger, result, today, dry_run=False)
    result.check = parity.compare(ledger, counted)
    if sure is not None:
        _mirror(ledger, sure, result, dry_run=False)
    return result


def _import_from_bank(
    data_dir: Path,
    conn: sqlite3.Connection,
    bank: Callable[[], simplefin.Answer],
    result: SyncResult,
    text: str,
    *,
    dry_run: bool,
    today: datetime.date,
) -> None:
    """Store and add to the ledger the rows SimpleFIN has posted that tally hasn't got.

    A row is already stored when a stored row of its account has the same posted date and
    amount (one stored row each): that covers the CSV rows from before SimpleFIN, which the
    bank describes differently there, and the rows earlier syncs took. A failure is noted,
    never raised: the next run catches up.
    """
    ledger = book.load(book.folder(data_dir), rules.parse(text))
    if not ledger.accounts.simplefin:
        return
    try:
        answer = bank()
        found = simplefin.rows(answer, ledger.accounts.simplefin)
    except simplefin.SimpleFINError as e:
        result.bank_failed = str(e)
        return
    result._bank = answer
    result.bank_problems += answer.problems
    month_rules = rules.parse(text)
    # One moment for the whole answer: it's when each account was last asked about, and when
    # each pending transaction was last listed (see matching).
    now = datetime.datetime.now(datetime.UTC)
    kept: list[dict[str, str]] = []
    for account, rows in found.items():
        new = _not_stored(store.transactions(conn, account), rows)
        result.banked[account] = new
        kept += [_raw(t) for t in new]
        # Stored in a dry run too, as is each pending one: its database is a copy, and the
        # steps after count them. Stored with no rows too, to note when it was asked.
        store.save(
            conn,
            new,
            account=account,
            file_name=store.BANK_IMPORT,
            file_sha256=hashlib.sha256(json.dumps([_raw(t) for t in new]).encode()).hexdigest(),
            now=now,
        )
        if dry_run or not new:
            continue
        made = [
            entries.transaction(
                t, ledger.accounts, month_rules, meta=data.new_metadata("simplefin", 0)
            )
            for t in new
        ]
        book.add(ledger, book.new_entries(made, ledger))
    result.pending = simplefin.pending(answer, ledger.accounts.simplefin, now)
    for alert in result.pending:
        store.see_alert(conn, alert)
    if kept and not dry_run:
        _archive(data_dir, today, kept)


def _check_bank(
    ledger: book.Ledger, result: SyncResult, today: datetime.date, *, dry_run: bool
) -> None:
    """Compare each linked account's posted balance in the ledger with the bank's. In a dry
    run, an account that got new rows is left out: the ledger doesn't have them."""
    if result._bank is None:
        return
    for last4, account in sorted(ledger.accounts.bank_accounts.items()):
        theirs = simplefin.account_ending(result._bank, last4)
        new = result.banked.get(ledger.accounts.simplefin.get(last4, ""))
        if theirs is None or (dry_run and new):
            continue
        mine = sum(
            (
                p.units.number or Decimal(0)
                for e in ledger.entries
                if isinstance(e, data.Transaction)
                and e.date <= today
                and not str(e.meta.get("filename", "")).endswith(book.PENDING)
                for p in e.postings
                if p.account == account and p.units is not None
            ),
            Decimal(0),
        )
        if mine != theirs.balance:
            result.bank_differ.append((account, theirs.balance, mine))


def _not_stored(stored: Sequence[Transaction], rows: Sequence[Transaction]) -> list[Transaction]:
    have = Counter((t.posted or t.date, t.amount) for t in stored)
    new = []
    for t in rows:
        key = (t.posted or t.date, t.amount)
        if have[key]:
            have[key] -= 1
        else:
            new.append(t)
    return new


def _raw(t: Transaction) -> dict[str, str]:
    return {
        "account": t.account,
        "date": t.date.isoformat(),
        "posted": t.posted.isoformat() if t.posted else "",
        "description": t.description,
        "amount": str(t.amount),
    }


def _archive(data_dir: Path, today: datetime.date, kept: list[dict[str, str]]) -> None:
    """Keep what SimpleFIN gave in data/raw/simplefin/, as an export is kept in raw/, so the
    database can be rebuilt from it."""
    from tally.importing import private_dir

    folder = private_dir(data_dir, "raw", "simplefin")
    content = json.dumps(kept, indent=1) + "\n"
    name = hashlib.sha256(content.encode()).hexdigest()[:16]
    path = folder / f"{today.isoformat()}-{name}.json"
    path.write_text(content, encoding="utf-8")
    path.chmod(0o600)


def _mirror(ledger: book.Ledger, sure: SureClient, result: SyncResult, *, dry_run: bool) -> None:
    """Make Sure match the ledger. A failure is noted, never raised: the next run finishes."""
    try:
        today = datetime.datetime.now().astimezone().date()
        result.sure = mirror.plan(
            mirror.rows(ledger), sure, mirror.balances(ledger), mirror.worth(ledger, today), today
        )
        if not dry_run:
            mirror.apply(result.sure, sure)
    except SureError as e:
        result.sure_failed = str(e)


def _record_holdings(
    ledger: book.Ledger, brokerage: Callable[[], Snapshot], result: SyncResult, *, dry_run: bool
) -> None:
    """Add today's brokerage holdings to the ledger. A failure is noted, never raised."""
    try:
        snap = brokerage()
        extracted = positions.entries(snap, ledger.entries, ledger.accounts, "snaptrade")
    except (SnapTradeError, ParseError, accounts.AccountsError) as e:
        result.holdings_failed = str(e)
        return
    for account in snap.accounts:
        if not account.holdings and not account.cash:
            continue
        cost = sum((h.cost or 0 for h in account.holdings), Decimal(0))
        market = sum((h.quantity * h.price for h in account.holdings), Decimal(0))
        market += account.cash or 0
        known = (
            "" if all(h.cost is not None for h in account.holdings) else " (no cost basis given)"
        )
        result.holdings.append(
            f"{account.name} …{account.label}: cost {cost:,.2f}{known}, market {market:,.2f}"
        )
    new = book.new_entries(extracted, ledger)
    result.holdings_added = len(new)
    if not dry_run:
        book.add(ledger, new)


def check(data_dir: Path, conn: sqlite3.Connection) -> parity.Parity | None:
    """parity.compare without changing anything, with pending entries rebuilt in memory."""
    session = open_session(data_dir, conn)
    if session is None:
        return None
    stored = store.transactions(conn)
    waiting = matching.match(store.alerts(conn), stored, store.fetched(conn)).waiting
    ledger = book.with_pending(
        session.ledger, book.pending_entries(session.ledger, waiting, session.rules)
    )
    return parity.compare(ledger, [*stored, *(pending.as_transaction(a) for a in waiting)])


def _keep_pending_fixes(session: Session, conn: sqlite3.Connection) -> list[str]:
    fixes = _pending_fixes(session.ledger, store.alerts_with_ids(conn))
    if not fixes:
        return []
    text = _rules_text(session.data_dir)
    kept = corrections.apply(text, fixes, _today())
    corrections.save(session.data_dir / RULES, kept.text)
    session.rules = rules_for(session.data_dir, conn)
    return kept.messages


def _pending_fixes(
    ledger: book.Ledger, alerts: Sequence[tuple[int, Alert]]
) -> list[tuple[Transaction, str]]:
    """(pending transaction, category) for each category you changed on a pending entry."""
    by_id = {identity(t): t for t in (pending.as_transaction(a) for _, a in alerts)}
    return [
        (by_id[key], category)
        for key, category in book.pending_fixes(ledger).items()
        if key in by_id
    ]


def _rules_text(data_dir: Path) -> str:
    try:
        return (data_dir / RULES).read_text(encoding="utf-8")
    except FileNotFoundError:
        raise rules.RulesError(f"no rules file at {data_dir / RULES}") from None


def _today() -> datetime.date:
    return datetime.datetime.now().astimezone().date()
