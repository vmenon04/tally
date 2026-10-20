"""Parsers for bank and card exports, and the field formats they share."""

import datetime
import re
from decimal import Decimal

_DATE = re.compile(r"(\d{2})/(\d{2})/(\d{4})")
_AMOUNT = re.compile(r"-?(\d{1,3}(,\d{3})+|\d+)\.\d{2}")


class ParseError(ValueError):
    """An export file isn't in the layout its parser expects."""

    def __init__(self, message: str, line: int | None = None) -> None:
        self.line = line
        super().__init__(f"line {line}: {message}" if line is not None else message)


def parse_date(text: str, line: int) -> datetime.date:
    """MM/DD/YYYY, as both Discover and BofA write it."""
    match = _DATE.fullmatch(text.strip())
    if match:
        month, day, year = (int(part) for part in match.groups())
        try:
            return datetime.date(year, month, day)
        except ValueError:
            pass
    raise ParseError(f"bad date {text!r}", line)


def parse_amount(text: str, line: int) -> Decimal:
    """Two decimal places with optional sign and thousands separators: "-1,234.56"."""
    text = text.strip()
    if not _AMOUNT.fullmatch(text):
        raise ParseError(f"bad amount {text!r}", line)
    return Decimal(text.replace(",", ""))
