"""beangulp importers for the bank exports, built on tally's own parsers.

They work with beangulp's tools as well as with `tally import`. Recognizing a file, checking
it (BofA running balances) and reading its rows is the parsers' job; these add the ledger's
accounts and categories, the balance assertions a file proves, and duplicate marking.
"""

import csv
import datetime
import io
from pathlib import Path

import beangulp
from beancount.core import data

from tally.importers import ParseError, bofa, discover, fidelity
from tally.ledger import entries, positions
from tally.ledger.accounts import Accounts
from tally.rules import Rules
from tally.transaction import Transaction

# Days before an export's end after which BofA no longer adds rows dated earlier.
SETTLE_DAYS = 3


class _CsvImporter(beangulp.Importer):  # type: ignore[misc]
    tally_account: str
    header: list[str]

    def __init__(self, accounts: Accounts, rules: Rules) -> None:
        self.accounts = accounts
        self.rules = rules

    @property
    def name(self) -> str:
        return f"tally.{self.tally_account}"

    def identify(self, filepath: str) -> bool:
        try:
            text = Path(filepath).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return False
        first = next(csv.reader(io.StringIO(text.removeprefix("﻿"))), [])
        return first == self.header

    def account(self, filepath: str) -> str:
        return self.accounts.source(self.tally_account)

    def extract(self, filepath: str, existing: data.Directives) -> data.Directives:
        return self.extract_text(Path(filepath).read_text(encoding="utf-8"), filepath)

    def extract_text(self, text: str, filename: str) -> data.Directives:
        raise NotImplementedError

    def deduplicate(self, extracted: data.Directives, existing: data.Directives) -> None:
        entries.mark_duplicates(extracted, existing)

    def _transactions(self, rows: list[Transaction], filename: str) -> list[data.Directive]:
        return [
            entries.transaction(t, self.accounts, self.rules, meta=data.new_metadata(filename, 0))
            for t in rows
        ]


class BofaChecking(_CsvImporter):
    """BofA checking: its rows, plus the balance at the start of the file's period and as it
    stood SETTLE_DAYS before its end.

    BofA dates some rows by the day they happened even when they post later, so the last
    days of an export can still gain rows after you download it (an export "as of 10/19"
    taken on 10/20 lacked two rows later dated 10/19). Its ending balance would then fail
    once a later export brings them in.
    """

    tally_account = bofa.ACCOUNT
    header = bofa.SUMMARY_HEADER

    def extract_text(self, text: str, filename: str) -> data.Directives:
        statement = bofa.parse_statement(text)
        account = self.account(filename)
        meta = data.new_metadata(filename, 0)
        result: list[data.Directive] = [
            entries.balance(dict(meta), statement.start, account, statement.beginning),
            *self._transactions(statement.transactions, filename),
        ]
        settled = statement.end - datetime.timedelta(days=SETTLE_DAYS)
        if settled >= statement.start:
            value = statement.beginning
            for t in statement.transactions:
                if (t.posted or t.date) <= settled and t.balance is not None:
                    value = t.balance
            day_after = settled + datetime.timedelta(days=1)
            result.append(entries.balance(dict(meta), day_after, account, value))
        return result


class Discover(_CsvImporter):
    """The Discover card. Its exports carry no balance; statements give those."""

    tally_account = discover.ACCOUNT
    header = discover.HEADER

    def extract_text(self, text: str, filename: str) -> data.Directives:
        return self._transactions(discover.parse(text), filename)


class FidelityPositions(beangulp.Importer):  # type: ignore[misc]
    """Fidelity's positions export: holdings at cost, prices and balances (see positions.py)."""

    def __init__(self, accounts: Accounts) -> None:
        self.accounts = accounts

    @property
    def name(self) -> str:
        return "tally.fidelity_positions"

    def identify(self, filepath: str) -> bool:
        try:
            text = Path(filepath).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return False
        return fidelity.is_positions(next(csv.reader(io.StringIO(text.removeprefix("\ufeff"))), []))

    def account(self, filepath: str) -> str:
        return "Assets:Fidelity"

    def extract(self, filepath: str, existing: data.Directives) -> data.Directives:
        text = Path(filepath).read_text(encoding="utf-8")
        return self.extract_text(text, filepath, existing)

    def extract_text(self, text: str, filename: str, existing: data.Directives) -> data.Directives:
        return positions.entries(fidelity.parse(text), existing, self.accounts, filename)

    def deduplicate(self, extracted: data.Directives, existing: data.Directives) -> None:
        entries.mark_duplicates(extracted, existing)


def all_importers(accounts: Accounts, rules: Rules) -> list[_CsvImporter]:
    return [BofaChecking(accounts, rules), Discover(accounts, rules)]


def for_account(tally_account: str, accounts: Accounts, rules: Rules) -> _CsvImporter:
    """The importer for the export importing.read recognized as this account."""
    for importer in all_importers(accounts, rules):
        if importer.tally_account == tally_account:
            return importer
    raise ParseError(f"no ledger importer for {tally_account}")
