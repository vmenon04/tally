"""The Tally page inside Fava, kept as a fallback while the Tally app (tally.web) is new.

Fava loads it from a line in the ledger:

    2026-01-01 custom "fava-extension" "tally.fava_page"

It renders the same page as the app, from what Fava has loaded.
"""

from pathlib import Path
from typing import cast

from beancount.core import data
from fava.ext import FavaExtensionBase, extension_endpoint
from flask import redirect, request
from markupsafe import Markup
from werkzeug.wrappers import Response

from tally import web


class Tally(FavaExtensionBase):
    report_title = "Tally"

    def body(self) -> Markup:
        return web.body(
            web.picture_of(self._entries(), self._folder()),
            self._folder().parent,
            actions={"ask": "ask", "categorize": "categorize"},
            fava=web.FavaLinks("../../"),
        )

    @extension_endpoint("ask", ["POST"])  # type: ignore[untyped-decorator,arg-type]
    def ask(self) -> Response:
        try:
            web.ask(self._folder().parent, request.form.get("question", ""))
        except web.Refused as e:
            return Response(str(e), status=e.status)
        return redirect(f"{request.referrer or './'}#advice", code=303)

    @extension_endpoint("categorize", ["POST"])  # type: ignore[untyped-decorator,arg-type]
    def categorize(self) -> Response:
        try:
            web.categorize(
                self._folder(),
                self._entries(),
                request.form.get("tally_id", ""),
                request.form.get("category", ""),
            )
        except web.Refused as e:
            return Response(str(e), status=e.status)
        return redirect(request.referrer or "./", code=303)

    def _entries(self) -> list[data.Directive]:
        # Fava types its entries as its own copies of Beancount's; they're Beancount's.
        return cast(list[data.Directive], self.ledger.all_entries)

    def _folder(self) -> Path:
        return Path(self.ledger.beancount_file_path).parent
