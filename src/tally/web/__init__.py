"""The Tally page: your money at a glance, Claude's advice, and purchases to categorize.

It runs as an app of its own (`tally web`), with Fava alongside it for the details: each
category and chart links to the matching page in Fava. The same page also renders inside
Fava (tally.fava_page), as a fallback.

Nothing here works anything out: the numbers come from tally.ledger.picture, the advice
from tally.advice. Picking a category changes only the account on that purchase's category
line, the edit tally itself makes when it moves an entry out of Uncategorized; amounts,
dates and the bank's side are never touched, and tally keeps it like any edit made in Fava.
"""

import datetime
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

import jinja2
from beancount import loader
from beancount.core import data
from flask import Flask, Response, redirect, request
from markupsafe import Markup

from tally import advice, plan
from tally.ledger import accounts, book, categories, entries, picture
from tally.rules import UNCATEGORIZED
from tally.web import charts

_ENV = jinja2.Environment(
    loader=jinja2.PackageLoader("tally.web"),
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


def body(
    p: picture.Picture,
    data_dir: Path,
    *,
    actions: dict[str, str],
    fava: FavaLinks,
) -> Markup:
    """The page's content, for this app's page or Fava's."""
    return Markup(  # noqa: S704 (rendered with autoescape)
        _ENV.get_template("body.html").render(
            p=p,
            m=p.this_month,
            review=advice.latest_review(data_dir),
            questions=advice.questions(data_dir),
            actions=actions,
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
    return picture.build(
        loaded, found, plan.read(loaded, found, today), today, folder / book.PENDING
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
        [
            categories.Change(
                path,
                line,
                other.account,
                found.counter(category, payee),
                payee,
            )
        ]
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


def create_app(data_dir: Path, fava_port: int | None) -> Flask:
    """The Tally app for the ledger in data_dir/ledger.

    fava_port is where Fava listens on this same host name; its links use the page's own
    scheme and host, so no address is written down anywhere.
    """
    folder = book.folder(data_dir)
    app = Flask(__name__)
    ledger = _Ledger(folder)

    @app.get("/")
    def page() -> str:
        loaded, options, errors = ledger.load()
        fava = FavaLinks(_fava_base(fava_port, options))
        return _ENV.get_template("page.html").render(
            title=options.get("title") or "Tally",
            errors=errors,
            fava=fava,
            body=body(
                picture_of(loaded, folder),
                data_dir,
                actions={"ask": "ask", "categorize": "categorize"},
                fava=fava,
            ),
        )

    @app.post("/categorize")
    def categorize_view() -> Any:
        loaded, _, _ = ledger.load()
        try:
            categorize(
                folder, loaded, request.form.get("tally_id", ""), request.form.get("category", "")
            )
        except Refused as e:
            return Response(str(e), status=e.status, mimetype="text/plain")
        return redirect("./#needs-a-category", code=303)

    @app.post("/ask")
    def ask_view() -> Any:
        try:
            ask(data_dir, request.form.get("question", ""))
        except Refused as e:
            return Response(str(e), status=e.status, mimetype="text/plain")
        return redirect("./#advice", code=303)

    return app


class _Ledger:
    """The ledger, loaded again only when one of its files has changed."""

    def __init__(self, folder: Path) -> None:
        self.folder = folder
        self._stamp: tuple[tuple[str, int], ...] | None = None
        self._loaded: tuple[list[data.Directive], dict[str, Any], int] | None = None

    def load(self) -> tuple[list[data.Directive], dict[str, Any], int]:
        """(entries, options, number of bean-check errors)."""
        stamp = tuple(
            sorted((str(p), p.stat().st_mtime_ns) for p in self.folder.rglob("*.beancount"))
        )
        if self._loaded is None or stamp != self._stamp:
            book.ensure_pending(self.folder)
            loaded, errors, options = loader.load_file(str(self.folder / book.MAIN))
            self._loaded, self._stamp = (loaded, options, len(errors)), stamp
        return self._loaded


def _fava_base(port: int | None, options: dict[str, Any]) -> str:
    from fava.util import slugify

    slug = slugify(str(options.get("title") or "beancount")) or "beancount"
    host = request.host.rsplit(":", 1)[0] if ":" in request.host else request.host
    if port is None:
        return f"/{slug}/"
    return f"//{host}:{port}/{slug}/"


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
