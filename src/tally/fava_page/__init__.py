"""The Tally page in Fava: your money at a glance, Claude's advice, and purchases to categorize.

Fava loads it from a line in the ledger:

    2026-01-01 custom "fava-extension" "tally.fava_page"

Each category and chart links to the matching Fava page for the details. Nothing here works
anything out: the numbers come from tally.ledger.picture, the advice from tally.advice.
Picking a category changes only the account on that purchase's category line, the edit
tally itself makes when it moves an entry out of Uncategorized; amounts, dates and the
bank's side are never touched, and tally keeps it like any edit made in Fava.
"""

import datetime
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import cast

import jinja2
from beancount.core import data
from fava.ext import FavaExtensionBase, extension_endpoint
from flask import redirect, request
from markupsafe import Markup
from werkzeug.wrappers import Response

from tally import advice, config, plan
from tally.fava_page import charts
from tally.ledger import accounts, book, categories, entries, picture
from tally.rules import UNCATEGORIZED

_ENV = jinja2.Environment(
    loader=jinja2.PackageLoader("tally.fava_page"),
    autoescape=jinja2.select_autoescape(["html"]),
    undefined=jinja2.StrictUndefined,
)


class Refused(Exception):
    """A request tally won't carry out, with the HTTP status to answer it with."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


@dataclass(frozen=True, slots=True)
class FavaLinks:
    """Where Fava's pages are, from base (the ledger's root in Fava, ending in /)."""

    base: str

    def page(self, name: str) -> str:
        return f"{self.base}{name}/"

    def account(self, name: str) -> str:
        return f"{self.base}account/{name}/"


class Tally(FavaExtensionBase):
    report_title = "Tally"

    def body(self) -> Markup:
        folder = self._folder()
        return render(
            picture_of(self._entries(), folder),
            folder.parent,
            # The page is at <ledger>/extension/Tally/; its forms post next to it.
            fava=FavaLinks("../../"),
        )

    @extension_endpoint("ask", ["POST"])  # type: ignore[untyped-decorator,arg-type]
    def ask(self) -> Response:
        try:
            ask(self._folder().parent, request.form.get("question", ""))
        except Refused as e:
            return Response(str(e), status=e.status)
        return redirect(f"{request.referrer or './'}#advice", code=303)

    @extension_endpoint("categorize", ["POST"])  # type: ignore[untyped-decorator,arg-type]
    def categorize(self) -> Response:
        try:
            categorize(
                self._folder(),
                self._entries(),
                request.form.get("tally_id", ""),
                request.form.get("category", ""),
            )
        except Refused as e:
            return Response(str(e), status=e.status)
        return redirect(request.referrer or "./", code=303)

    def _entries(self) -> list[data.Directive]:
        # Fava types its entries as its own copies of Beancount's; they're Beancount's.
        return cast(list[data.Directive], self.ledger.all_entries)

    def _folder(self) -> Path:
        return Path(self.ledger.beancount_file_path).parent


def render(p: picture.Picture, data_dir: Path, *, fava: FavaLinks) -> Markup:
    """The page's content."""
    return Markup(  # noqa: S704 (rendered with autoescape)
        _ENV.get_template("body.html").render(
            p=p,
            m=p.this_month,
            review=advice.latest_review(data_dir),
            questions=advice.questions(data_dir),
            actions={"ask": "ask", "categorize": "categorize"},
            fava=fava,
            money=money,
            percent=percent,
            chart_months=lambda x: Markup(charts.months(x.months, money)),  # noqa: S704
            chart_net_worth=lambda x: Markup(charts.net_worth(x.net_worth, money)),  # noqa: S704
        )
    )


def picture_of(loaded: Sequence[data.Directive], folder: Path) -> picture.Picture:
    found = accounts.from_entries(loaded)
    today = datetime.datetime.now().astimezone().date()
    try:
        settings = config.load(folder.parent / "config.toml")
    except config.ConfigError:
        settings = config.Config()  # the page still works, without the Roth IRA limit
    return picture.build(
        loaded,
        found,
        plan.read(loaded, found, today),
        today,
        folder / book.PENDING,
        settings.roth_limit,
        settings.roth_elsewhere,
    )


def categorize(
    folder: Path, loaded: Sequence[data.Directive], tally_id: str, category: str
) -> None:
    """Give an Uncategorized purchase a category. Raises Refused."""
    found = accounts.from_entries(loaded)
    if category not in picture.choices(found):
        raise Refused(400, f"no category {category!r}")
    target = _uncategorized(loaded, found, tally_id)
    if target is None:
        raise Refused(409, "that purchase isn't waiting for a category")
    entry, other = target
    path = Path(str(entry.meta["filename"])).resolve()
    allowed = {(folder / book.PENDING).resolve()}
    allowed |= {p.resolve() for p in (folder / book.IMPORTED).glob("*.beancount")}
    if path not in allowed:
        raise Refused(403, "tally only changes entries it wrote")
    payee = entry.payee or ""
    line = int((other.meta or {})["lineno"])
    categories.apply(
        [categories.Change(path, line, other.account, found.counter(category, payee), payee)]
    )


def ask(data_dir: Path, question: str) -> None:
    """Save a question for tally to answer. Raises Refused."""
    try:
        advice.ask(data_dir, question, datetime.datetime.now(datetime.UTC))
    except advice.AdviceError as e:
        raise Refused(400, str(e)) from e


def money(amount: Decimal, cents: bool = False) -> str:
    sign = "-" if amount < 0 else ""
    return f"{sign}${abs(amount):,.2f}" if cents else f"{sign}${abs(amount):,.0f}"


def percent(share: Decimal | None) -> str:
    return "—" if share is None else f"{share * 100:.0f}%"


def _uncategorized(
    loaded: Sequence[data.Directive], found: accounts.Accounts, tally_id: str
) -> tuple[data.Transaction, data.Posting] | None:
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
