"""The Tally page in Fava: your money at a glance, and the purchases that need a category.

Fava loads it from a line in the ledger:

    2026-01-01 custom "fava-extension" "tally.fava_page"

It reads what Fava has loaded, so it shows exactly what the rest of Fava does. Picking a
category for a purchase changes the account on that purchase's other posting line, the way
tally itself moves entries out of Uncategorized; amounts, dates and the bank's side are
never touched. tally keeps the change like any other edit made in Fava.
"""

import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

from beancount.core import data
from fava.ext import FavaExtensionBase, extension_endpoint
from flask import redirect, request
from markupsafe import Markup
from werkzeug.wrappers import Response

from tally import advice, plan
from tally.fava_page import charts
from tally.ledger import accounts, book, categories, entries, picture
from tally.rules import UNCATEGORIZED


class Tally(FavaExtensionBase):
    report_title = "Tally"

    def view(self) -> picture.Picture:
        loaded = self._entries()
        found = accounts.from_entries(loaded)
        today = datetime.datetime.now().astimezone().date()
        return picture.build(
            loaded, found, plan.read(loaded, found, today), today, self._folder() / book.PENDING
        )

    def money(self, amount: Decimal, cents: bool = False) -> str:
        sign = "-" if amount < 0 else ""
        return f"{sign}${abs(amount):,.2f}" if cents else f"{sign}${abs(amount):,.0f}"

    def percent(self, share: Decimal | None) -> str:
        return "—" if share is None else f"{share * 100:.0f}%"

    def month_name(self, day: datetime.date) -> str:
        return f"{day:%B}"

    def chart_months(self, p: picture.Picture) -> Markup:
        return Markup(charts.months(p.months, self.money))  # noqa: S704 (our own SVG)

    def chart_net_worth(self, p: picture.Picture) -> Markup:
        return Markup(charts.net_worth(p.net_worth, self.money))  # noqa: S704

    def review(self) -> advice.Review | None:
        return advice.latest_review(self._folder().parent)

    def questions(self) -> list[advice.Question]:
        return advice.questions(self._folder().parent)

    @extension_endpoint("ask", ["POST"])  # type: ignore[untyped-decorator,arg-type]
    def ask(self) -> Response:
        """Save a question for tally to answer on its next run."""
        try:
            advice.ask(
                self._folder().parent,
                request.form.get("question", ""),
                datetime.datetime.now(datetime.UTC),
            )
        except advice.AdviceError as e:
            return Response(str(e), status=400)
        return redirect(f"{request.referrer or './'}#advice", code=303)

    @extension_endpoint("categorize", ["POST"])  # type: ignore[untyped-decorator,arg-type]
    def categorize(self) -> Response:
        """Give an Uncategorized purchase the category picked for it."""
        back = request.referrer or "./"
        tally_id = request.form.get("tally_id", "")
        category = request.form.get("category", "")
        loaded = self._entries()
        found = accounts.from_entries(loaded)
        if category not in picture._choices(found):
            return Response(f"no category {category!r}", status=400)
        target = _uncategorized(loaded, found, tally_id)
        if target is None:
            return Response("that purchase isn't waiting for a category", status=409)
        entry, other = target
        path = Path(str(entry.meta["filename"]))
        allowed = {(self._folder() / book.PENDING).resolve(), *self._imported()}
        if path.resolve() not in allowed:
            return Response("tally only changes entries it wrote", status=403)
        change = categories.Change(
            path,
            int(other.meta["lineno"]),
            other.account,
            found.counter(category, entry.payee or ""),
            entry.payee or "",
        )
        categories.apply([change])
        return redirect(back, code=303)

    def _entries(self) -> list[data.Directive]:
        # Fava types its entries as its own copies of Beancount's; they're Beancount's.
        return cast(list[data.Directive], self.ledger.all_entries)

    def _folder(self) -> Path:
        return Path(self.ledger.beancount_file_path).parent

    def _imported(self) -> set[Path]:
        return {p.resolve() for p in (self._folder() / book.IMPORTED).glob("*.beancount")}


def _uncategorized(
    loaded: list[data.Directive], found: accounts.Accounts, tally_id: str
) -> tuple[data.Transaction, Any] | None:
    sources = set(found.sources.values())
    for e in loaded:
        if not isinstance(e, data.Transaction) or e.meta.get(entries.ID_KEY) != tally_id:
            continue
        others = [p for p in e.postings if p.account not in sources]
        if (
            len(e.postings) == 2
            and len(others) == 1
            and found.category_of(others[0].account) == UNCATEGORIZED
            and others[0].meta
        ):
            return e, others[0]
    return None
