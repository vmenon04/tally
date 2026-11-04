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

from tally.importers import ParseError, bofa, discover
from tally.ledger import entries
from tally.ledger.accounts import Accounts
from tally.rules import Rules
from tally.transaction import Transaction


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
    """BofA checking: its rows, plus the balance at the start and end of the file's period."""

    tally_account = bofa.ACCOUNT
    header = bofa.SUMMARY_HEADER

    def extract_text(self, text: str, filename: str) -> data.Directives:
        statement = bofa.parse_statement(text)
        account = self.account(filename)
        meta = data.new_metadata(filename, 0)
        return [
            entries.balance(dict(meta), statement.start, account, statement.beginning),
            *self._transactions(statement.transactions, filename),
            entries.balance(
                dict(meta), statement.end + datetime.timedelta(days=1), account, statement.ending
            ),
        ]


class Discover(_CsvImporter):
    """The Discover card. Its exports carry no balance; statements give those."""

    tally_account = discover.ACCOUNT
    header = discover.HEADER

    def extract_text(self, text: str, filename: str) -> data.Directives:
        return self._transactions(discover.parse(text), filename)


def all_importers(accounts: Accounts, rules: Rules) -> list[_CsvImporter]:
    return [BofaChecking(accounts, rules), Discover(accounts, rules)]


def for_text(text: str, accounts: Accounts, rules: Rules) -> _CsvImporter:
    first = next(csv.reader(io.StringIO(text.removeprefix("﻿"))), [])
    for importer in all_importers(accounts, rules):
        if first == importer.header:
            return importer
    raise ParseError(f"not a Discover or BofA export; first line is {first}", line=1)
