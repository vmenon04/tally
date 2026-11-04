"""Discover card "Recent Activity" CSV export.

Layout (CRLF line endings, newest first):

    Trans. Date,Post Date,Description,Amount,Category
    03/12/2026,03/12/2026,"MAPLE MARKET SPRINGFIELD ILAPPLE PAY ENDING IN 1234",12.34,"Supermarkets"

Discover writes purchases as positive and payments as negative. We flip the sign so that
negative means money spent, matching every other account.
"""

import csv
import io
import re
from decimal import Decimal

from tally.importers import ParseError, parse_amount, parse_date
from tally.transaction import Transaction

ACCOUNT = "discover"
HEADER = ["Trans. Date", "Post Date", "Description", "Amount", "Category"]

# Glued onto Apple Pay purchases with no space, sometimes followed by extra digits.
_APPLE_PAY = re.compile(r"APPLE PAY ENDING IN\s*\S*$")
# Some processors glue a reference code onto the state: "SPRINGFIELD IL00012345ABCD...".
_REFERENCE = re.compile(r" ([A-Z]{2})\d[A-Z0-9]{5,}$")


def parse(text: str) -> list[Transaction]:
    rows = csv.reader(io.StringIO(text.removeprefix("﻿"), newline=""))
    header = next(rows, None)
    if header is None:
        raise ParseError("file is empty")
    if header != HEADER:
        raise ParseError(f"expected header {HEADER}, got {header}", line=1)

    transactions = []
    for row in rows:
        line = rows.line_num
        if not any(field.strip() for field in row):
            continue
        if len(row) != len(HEADER):
            raise ParseError(f"expected {len(HEADER)} fields, got {len(row)}", line)
        trans_date, post_date, description, amount, category = row
        transactions.append(
            Transaction(
                account=ACCOUNT,
                date=parse_date(trans_date, line),
                posted=parse_date(post_date, line),
                description=description,
                merchant=clean_merchant(description),
                amount=Decimal(0) - parse_amount(amount, line),
                bank_category=category.strip() or None,
            )
        )
    return transactions


def clean_merchant(description: str) -> str:
    """Remove the parts of a description that change from row to row at the same merchant."""
    merchant = _APPLE_PAY.sub("", description.strip())
    merchant = _REFERENCE.sub(r" \1", merchant)
    return " ".join(merchant.split())
