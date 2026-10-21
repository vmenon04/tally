import builtins
import imaplib
import io
from pathlib import Path
from typing import Any

import pytest

from tally import alert_reading, gmail, store
from tally.cli import main

FIXTURES = Path(__file__).parent / "fixtures"
DISCOVER = (FIXTURES / "discover_alert.eml").read_bytes()
BOFA = (FIXTURES / "bofa_alert.eml").read_bytes()
ADDRESS = "alerts@example.com"
ALL_MAIL = b'(\\All \\HasNoChildren) "/" "[Gmail]/All Mail"'


class FakeImap:
    """Answers like Gmail's IMAP server, for the commands tally uses."""

    def __init__(
        self, messages: dict[int, tuple[int, bytes]], folders: builtins.list[bytes] | None = None
    ):
        self.messages = messages  # IMAP UID -> (X-GM-MSGID, raw email)
        self.folders = (
            folders if folders is not None else [b'(\\HasNoChildren) "/" "INBOX"', ALL_MAIL]
        )
        self.selected: tuple[str, bool] | None = None
        self.commands: builtins.list[tuple[Any, ...]] = []
        self.logged_out = False

    def list(self) -> tuple[str, builtins.list[bytes]]:
        return "OK", self.folders

    def select(self, name: bytes, readonly: bool = False) -> tuple[str, builtins.list[bytes]]:
        self.selected = (name.decode(), readonly)
        return "OK", [str(len(self.messages)).encode()]

    def uid(self, command: str, *args: Any) -> tuple[str, builtins.list[Any]]:
        self.commands.append((command, *args))
        if command == "SEARCH":
            return "OK", [b" ".join(str(u).encode() for u in sorted(self.messages))]
        uids = [int(u) for u in args[0].split(b",")]
        if args[1] == "(X-GM-MSGID)":
            return "OK", [
                f"{n} (X-GM-MSGID {self.messages[u][0]} UID {u})".encode()
                for n, u in enumerate(uids, start=1)
            ]
        assert args[1] == "(BODY.PEEK[])"
        raw = self.messages[uids[0]][1]
        return "OK", [(f"1 (UID {uids[0]} BODY[] {{{len(raw)}}}".encode(), raw), b")"]

    def logout(self) -> tuple[str, builtins.list[bytes]]:
        self.logged_out = True
        return "BYE", []


def test_opens_all_mail_read_only() -> None:
    imap = FakeImap({})
    gmail.ImapMailbox(imap)
    assert imap.selected == ('"[Gmail]/All Mail"', True)


def test_finds_all_mail_under_a_translated_name() -> None:
    imap = FakeImap({}, folders=[b'(\\All) "/" "[Google Mail]/Alle Nachrichten"'])
    gmail.ImapMailbox(imap)
    assert imap.selected == ('"[Google Mail]/Alle Nachrichten"', True)


def test_mailbox_without_all_mail_is_refused() -> None:
    with pytest.raises(gmail.GmailError, match="no All Mail folder"):
        gmail.ImapMailbox(FakeImap({}, folders=[b'(\\HasNoChildren) "/" "INBOX"']))


def test_message_ids_are_gmail_ids_newest_first() -> None:
    imap = FakeImap({4: (0x18F00000000000A1, DISCOVER), 9: (0x18F00000000000B2, BOFA)})
    mailbox = gmail.ImapMailbox(imap)
    assert mailbox.message_ids("from:(x.com)") == ["18f00000000000b2", "18f00000000000a1"]
    assert imap.commands[0] == ("SEARCH", "X-GM-RAW", '"from:(x.com)"')


def test_raw_is_read_without_marking_it() -> None:
    imap = FakeImap({4: (0xA1, DISCOVER)})
    mailbox = gmail.ImapMailbox(imap)
    [message_id] = mailbox.message_ids("from:(x.com)")
    assert mailbox.raw(message_id) == DISCOVER
    assert imap.commands[-1] == ("FETCH", b"4", "(BODY.PEEK[])")


def test_no_matching_messages() -> None:
    imap = FakeImap({})
    assert gmail.ImapMailbox(imap).message_ids("from:(x.com)") == []
    assert [c[0] for c in imap.commands] == ["SEARCH"]


def test_ids_are_fetched_in_batches() -> None:
    imap = FakeImap({u: (u, b"") for u in range(1, 1201)})
    assert len(gmail.ImapMailbox(imap).message_ids("from:(x.com)")) == 1200
    assert [c[0] for c in imap.commands] == ["SEARCH", "FETCH", "FETCH", "FETCH"]


def test_unknown_message_is_refused() -> None:
    with pytest.raises(gmail.GmailError, match="wasn't in the last search"):
        gmail.ImapMailbox(FakeImap({})).raw("a1")


def test_imap_failure_is_a_gmail_error() -> None:
    class Broken(FakeImap):
        def uid(self, command: str, *args: Any) -> tuple[str, list[Any]]:
            raise imaplib.IMAP4.abort("socket error: EOF")

    with pytest.raises(gmail.GmailError, match="searching the mailbox"):
        gmail.ImapMailbox(Broken({})).message_ids("from:(x.com)")


def test_closing_logs_out() -> None:
    imap = FakeImap({})
    with gmail.ImapMailbox(imap):
        pass
    assert imap.logged_out


@pytest.mark.parametrize(
    "typed", ["abcdefghijklmnop", "abcd efgh ijkl mnop", " abcd efgh ijkl mnop\n"]
)
def test_app_password_as_google_shows_it(typed: str) -> None:
    assert gmail.app_password(typed) == "abcdefghijklmnop"


@pytest.mark.parametrize(
    "typed", ["", "hunter2", "abcdefghijklmno", "ABCDEFGHIJKLMNOP", "abcdefghijklmnop1"]
)
def test_anything_else_is_not_an_app_password(typed: str) -> None:
    with pytest.raises(gmail.GmailError, match="isn't an app password"):
        gmail.app_password(typed)


def test_password_is_saved_owner_only(tmp_path: Path) -> None:
    path = tmp_path / "secrets" / "gmail-app-password"
    gmail.save_password(path, "abcdefghijklmnop")
    assert gmail.load_password(path) == "abcdefghijklmnop"
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.parent.stat().st_mode & 0o777 == 0o700
    assert [p.name for p in path.parent.iterdir()] == ["gmail-app-password"]


def test_missing_password_file(tmp_path: Path) -> None:
    with pytest.raises(gmail.GmailError, match="run `tally gmail-login`"):
        gmail.load_password(tmp_path / "gmail-app-password")


class FakeMailbox:
    def __init__(self, messages: dict[str, bytes]) -> None:
        self.messages = messages  # oldest first; Gmail lists newest first
        self.fetched: list[str] = []
        self.queries: list[str] = []

    def message_ids(self, query: str) -> list[str]:
        self.queries.append(query)
        return list(reversed(self.messages))

    def raw(self, message_id: str) -> bytes:
        self.fetched.append(message_id)
        return self.messages[message_id]


def run(data: Path, mailbox: FakeMailbox, dry_run: bool = False) -> tuple[bool, str, str]:
    out, err = io.StringIO(), io.StringIO()
    result = alert_reading.run_gmail(data, mailbox, ADDRESS, dry_run=dry_run, out=out, err=err)
    return result.ok, out.getvalue(), err.getvalue()


def stored_count(data: Path) -> int:
    conn = store.connect(data / "tally.db")
    try:
        return len(store.alerts(conn))
    finally:
        conn.close()


def test_reads_new_emails_oldest_first_and_keeps_them(tmp_path: Path) -> None:
    mailbox = FakeMailbox({"m1": DISCOVER, "m2": BOFA})
    ok, out, _ = run(tmp_path, mailbox)
    assert ok
    assert mailbox.queries == ["from:(discover.com OR bankofamerica.com)"]
    assert mailbox.fetched == ["m1", "m2"]
    assert f"Gmail {ADDRESS}: 2 bank emails, 2 not read before." in out
    assert "gmail m1: added  discover" in out
    assert "2 added, 0 already stored, 0 skipped, 0 refused." in out
    assert stored_count(tmp_path) == 2
    kept = tmp_path / "alerts" / "gmail" / "m1.eml"
    assert kept.read_bytes() == DISCOVER
    assert kept.stat().st_mode & 0o777 == 0o600
    assert kept.parent.stat().st_mode & 0o777 == 0o700


def test_emails_read_before_are_not_fetched_again(tmp_path: Path) -> None:
    run(tmp_path, FakeMailbox({"m1": DISCOVER}))
    mailbox = FakeMailbox({"m1": DISCOVER, "m2": BOFA})
    ok, out, _ = run(tmp_path, mailbox)
    assert ok
    assert mailbox.fetched == ["m2"]
    assert "2 bank emails, 1 not read before." in out


def test_skipped_emails_are_kept_so_they_are_read_once(tmp_path: Path) -> None:
    statement = DISCOVER.replace(b"Subject: Transaction Alert", b"Subject: Statement ready")
    run(tmp_path, FakeMailbox({"m1": statement}))
    mailbox = FakeMailbox({"m1": statement})
    run(tmp_path, mailbox)
    assert mailbox.fetched == []


def test_refused_email_is_reported_on_every_run(tmp_path: Path) -> None:
    forged = DISCOVER.replace(b"dkim=pass", b"dkim=fail")
    for _ in range(2):
        mailbox = FakeMailbox({"m1": forged, "m2": BOFA})
        ok, _, err = run(tmp_path, mailbox)
        assert not ok
        assert "gmail m1: refused: Gmail didn't confirm" in err
        assert "m1" in mailbox.fetched
    assert not (tmp_path / "alerts" / "gmail" / "m1.eml").exists()
    assert stored_count(tmp_path) == 1


def test_dry_run_keeps_and_stores_nothing(tmp_path: Path) -> None:
    ok, out, _ = run(tmp_path, FakeMailbox({"m1": DISCOVER}), dry_run=True)
    assert ok
    assert "Dry run: nothing will be saved." in out
    assert "1 added" in out
    assert not (tmp_path / "tally.db").exists()
    assert not (tmp_path / "alerts").exists()


def test_gmail_failure_midway_keeps_what_was_read(tmp_path: Path) -> None:
    class Flaky(FakeMailbox):
        def raw(self, message_id: str) -> bytes:
            if message_id == "m2":
                raise gmail.GmailError("reading m2: HTTP 500")
            return super().raw(message_id)

    ok, _, err = run(tmp_path, Flaky({"m1": DISCOVER, "m2": BOFA}))
    assert not ok
    assert "reading m2: HTTP 500" in err
    assert stored_count(tmp_path) == 1
    assert (tmp_path / "alerts" / "gmail" / "m1.eml").exists()


@pytest.fixture
def dirs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("TALLY_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("TALLY_SECRETS_DIR", str(tmp_path / "secrets"))
    (tmp_path / "data").mkdir()
    return tmp_path


def test_command_needs_gmail_account(dirs: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (dirs / "data" / "config.toml").write_text('sheet_id = "s"\n')
    assert main(["alerts", "--gmail"]) == 1
    assert "set gmail_account in config.toml" in capsys.readouterr().err


def config_with_account(dirs: Path) -> None:
    (dirs / "data" / "config.toml").write_text(f'sheet_id = "s"\ngmail_account = "{ADDRESS}"\n')


def test_command_needs_the_app_password(dirs: Path, capsys: pytest.CaptureFixture[str]) -> None:
    config_with_account(dirs)
    assert main(["alerts", "--gmail"]) == 1
    assert "no app password" in capsys.readouterr().err


def test_login_refuses_what_isnt_an_app_password(
    dirs: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    config_with_account(dirs)
    monkeypatch.setattr("getpass.getpass", lambda prompt: "my-real-password")

    def no_connection(address: str, password: str) -> None:
        raise AssertionError("must not try the password")

    monkeypatch.setattr(gmail.ImapMailbox, "connect", no_connection)
    assert main(["gmail-login"]) == 1
    assert "isn't an app password" in capsys.readouterr().err
    assert not (dirs / "secrets").exists()


def test_login_saves_a_password_gmail_accepts(
    dirs: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    config_with_account(dirs)
    monkeypatch.setattr("getpass.getpass", lambda prompt: "abcd efgh ijkl mnop")
    tried: list[tuple[str, str]] = []

    def connect(address: str, password: str) -> gmail.ImapMailbox:
        tried.append((address, password))
        return gmail.ImapMailbox(FakeImap({}))

    monkeypatch.setattr(gmail.ImapMailbox, "connect", connect)
    assert main(["gmail-login"]) == 0
    assert tried == [(ADDRESS, "abcdefghijklmnop")]
    saved = dirs / "secrets" / "gmail-app-password"
    assert saved.read_text() == "abcdefghijklmnop\n"
    assert saved.stat().st_mode & 0o777 == 0o600


def test_login_refused_by_gmail_saves_nothing(
    dirs: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    config_with_account(dirs)
    monkeypatch.setattr("getpass.getpass", lambda prompt: "abcdefghijklmnop")

    def refuse(address: str, password: str) -> gmail.ImapMailbox:
        raise gmail.GmailError("Gmail refused the app password")

    monkeypatch.setattr(gmail.ImapMailbox, "connect", refuse)
    assert main(["gmail-login"]) == 1
    assert "Gmail refused the app password" in capsys.readouterr().err
    assert not (dirs / "secrets" / "gmail-app-password").exists()


def test_run_reports_what_happened(tmp_path: Path) -> None:
    forged = BOFA.replace(b"dkim=pass", b"dkim=fail")
    out, err = io.StringIO(), io.StringIO()
    mailbox = FakeMailbox({"m1": DISCOVER, "m2": forged})
    result = alert_reading.run_gmail(tmp_path, mailbox, ADDRESS, dry_run=False, out=out, err=err)
    assert [a.merchant for a in result.added] == ["MAPLE MARKET #0412"]
    assert result.refused == ["m2"]
    assert result.not_found == []
    assert result.failure is None
    assert not result.ok


def test_search_failure_is_reported(tmp_path: Path) -> None:
    class Down(FakeMailbox):
        def message_ids(self, query: str) -> list[str]:
            raise gmail.GmailError("searching the mailbox: socket error")

    out, err = io.StringIO(), io.StringIO()
    result = alert_reading.run_gmail(tmp_path, Down({}), ADDRESS, dry_run=False, out=out, err=err)
    assert result.failure == "searching the mailbox: socket error"
    assert not result.ok


class FakeConnection:
    """Stands in for imaplib.IMAP4_SSL up to login."""

    def __init__(self, error: Exception) -> None:
        self.error = error
        self.closed = False

    def __call__(self, host: str, **kwargs: object) -> "FakeConnection":
        return self

    def login(self, address: str, password: str) -> None:
        raise self.error

    def shutdown(self) -> None:
        self.closed = True


def test_refused_password_and_timeout_are_told_apart(monkeypatch: pytest.MonkeyPatch) -> None:
    refused = FakeConnection(imaplib.IMAP4.error("[AUTHENTICATIONFAILED] Invalid credentials"))
    monkeypatch.setattr(imaplib, "IMAP4_SSL", refused)
    with pytest.raises(gmail.GmailError, match="refused the app password"):
        gmail.ImapMailbox.connect("alerts@example.com", "abcdefghijklmnop")
    assert refused.closed

    timed_out = FakeConnection(TimeoutError("The read operation timed out"))
    monkeypatch.setattr(imaplib, "IMAP4_SSL", timed_out)
    with pytest.raises(gmail.GmailError, match="didn't respond while signing in") as error:
        gmail.ImapMailbox.connect("alerts@example.com", "abcdefghijklmnop")
    assert "refused" not in str(error.value)
    assert timed_out.closed
