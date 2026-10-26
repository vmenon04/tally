"""Parsers for transaction alert emails, and the checks they share.

An alert is provisional: it's sent when a purchase is authorized, so its amount can differ
from what finally posts (a tip added, a gas station hold), and the monthly CSV export stays
the true record. An email is only believed when Gmail confirms the bank signed it.
"""

import datetime
import email.policy
import email.utils
import re
import time
from dataclasses import dataclass
from decimal import Decimal
from email.message import EmailMessage
from email.parser import BytesParser
from html.parser import HTMLParser

_AMOUNT = re.compile(r"\$((\d{1,3}(,\d{3})+|\d+)\.\d{2})")

# Tags that don't start a new line of text.
_INLINE = {"a", "b", "em", "font", "i", "img", "span", "strong", "u"}
# Tags whose text is never shown.
_HIDDEN = {"head", "script", "style", "title"}


@dataclass(frozen=True, slots=True)
class Alert:
    account: str
    date: datetime.date  # the day the alert states: of the purchase, or when money in posted
    merchant: str
    amount: Decimal  # negative = money out
    message_id: str  # unique per email, so reading the same email twice is noticed
    received: datetime.datetime  # when the bank sent it


class AlertError(ValueError):
    """An email can't be trusted, or isn't in the layout its parser expects."""


def read(raw: bytes) -> EmailMessage:
    return BytesParser(policy=email.policy.default).parsebytes(raw)


def check_sender(message: EmailMessage, domain: str) -> None:
    """Refuse the email unless Gmail checked it was signed by domain, and it's from there.

    Only the topmost Authentication-Results header counts: Gmail adds it on arrival, above
    everything the sender wrote, so a forged one further down is ignored.
    """
    results = message.get_all("Authentication-Results") or []
    first = " ".join(str(results[0]).split()) if results else ""
    if not first.startswith("mx.google.com;"):
        raise AlertError("email has no authentication result from Gmail")
    signed = rf"\bdkim=pass header\.(?:i=@|d=)(?:[\w-]+\.)*{re.escape(domain)}(?=[\s;]|$)"
    if not re.search(signed, first):
        raise AlertError(f"Gmail didn't confirm the email was signed by {domain}")
    if not is_from(message, domain):
        raise AlertError(f"email is from {sender(message)!r}, not {domain}")


def sender(message: EmailMessage) -> str:
    """The From address, lowercased."""
    return email.utils.parseaddr(str(message["From"] or ""))[1].lower()


def is_from(message: EmailMessage, domain: str) -> bool:
    """Whether the From address is at domain or one of its subdomains. Not proof on its own."""
    sender_domain = sender(message).rpartition("@")[2]
    return sender_domain == domain or sender_domain.endswith("." + domain)


def message_id(message: EmailMessage) -> str:
    value = str(message["Message-ID"] or "").strip()
    if not value:
        raise AlertError("email has no Message-ID")
    return value


def received(message: EmailMessage) -> datetime.datetime:
    try:
        sent = email.utils.parsedate_to_datetime(str(message["Date"] or ""))
    except ValueError:
        sent = None
    if sent is None or sent.tzinfo is None:
        raise AlertError(f"bad Date header {message['Date']!r}")
    return sent


def parse_date(text: str) -> datetime.date:
    """Dates like "October 17, 2026", as both Discover and BofA alerts write them."""
    try:
        parsed = time.strptime(text, "%B %d, %Y")
        return datetime.date(parsed.tm_year, parsed.tm_mon, parsed.tm_mday)
    except ValueError:
        raise AlertError(f"bad date {text!r}") from None


def parse_amount(text: str) -> Decimal:
    """Amounts like "$1,234.56", as a positive Decimal."""
    match = _AMOUNT.fullmatch(text)
    if match is None:
        raise AlertError(f"bad amount {text!r}")
    return Decimal(match.group(1).replace(",", ""))


def html_lines(html: str) -> list[str]:
    """The text of an HTML email, one line per block (paragraph, cell, <br>), spaces collapsed."""
    parser = _TextLines()
    parser.feed(html)
    parser.close()
    lines = "".join(parser.parts).split("\n")
    return [" ".join(line.split()) for line in lines if line.strip()]


class _TextLines(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._hidden = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _HIDDEN:
            self._hidden += 1
        elif tag not in _INLINE:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _HIDDEN:
            self._hidden = max(0, self._hidden - 1)
        elif tag not in _INLINE:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._hidden:
            # Line breaks in the HTML source are just spaces; only tags start new lines.
            self.parts.append(re.sub(r"\s+", " ", data))
