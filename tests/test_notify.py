import datetime
import io
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import requests

from tally import gmail, notify, store
from tally.cli import main
from tally.notify import Event

FIXTURES = Path(__file__).parent / "fixtures"
DISCOVER = (FIXTURES / "discover_alert.eml").read_bytes()
NOW = datetime.datetime(2026, 3, 20, 12, 0, tzinfo=datetime.UTC)


class FakeSender:
    def __init__(self, fail: bool = False) -> None:
        self.sent: list[tuple[str, str]] = []
        self.fail = fail

    def send(self, message: str, priority: str) -> None:
        if self.fail:
            raise notify.NotifyError("couldn't reach ntfy: ConnectionError")
        self.sent.append((message, priority))


@pytest.fixture
def db(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    conn = store.connect(tmp_path / "tally.db")
    yield conn
    conn.close()


def send(
    db: sqlite3.Connection, sender: FakeSender, events: list[Event], now: datetime.datetime = NOW
) -> tuple[list[str], str]:
    err = io.StringIO()
    return notify.send_new(db, sender, events, now, err), err.getvalue()


def test_one_message_per_kind_most_urgent_first(db: sqlite3.Connection) -> None:
    sender = FakeSender()
    events = [
        Event("new", "new:a"),
        Event("new", "new:b"),
        Event("not_found", "not-found:a"),
        Event("refused", "refused:m1"),
    ]
    send(db, sender, events)
    assert sender.sent == [
        ("1 alert email refused, check tally", "high"),
        ("1 purchase missing from the bank export", "default"),
        ("2 new card alerts", "low"),
    ]


def test_messages_name_no_merchant_or_amount(db: sqlite3.Connection) -> None:
    sender = FakeSender()
    send(db, sender, [Event(kind, f"{kind}:x") for kind in notify.KINDS])
    for message, _ in sender.sent:
        assert "$" not in message
        assert not any(ch.isdigit() for ch in message.replace("1 ", ""))


def test_each_event_is_announced_once(db: sqlite3.Connection) -> None:
    sender = FakeSender()
    send(db, sender, [Event("not_found", "not-found:a")])
    send(db, sender, [Event("not_found", "not-found:a"), Event("not_found", "not-found:b")])
    assert [m for m, _ in sender.sent] == [
        "1 purchase missing from the bank export",
        "1 purchase missing from the bank export",
    ]
    send(db, sender, [Event("not_found", "not-found:a"), Event("not_found", "not-found:b")])
    assert len(sender.sent) == 2


def test_mailbox_failure_repeats_after_six_hours(db: sqlite3.Connection) -> None:
    sender = FakeSender()
    failure = [Event("mailbox", notify.MAILBOX_FAILURE)]
    send(db, sender, failure)
    send(db, sender, failure, NOW + datetime.timedelta(hours=5, minutes=59))
    assert len(sender.sent) == 1
    send(db, sender, failure, NOW + datetime.timedelta(hours=6))
    assert sender.sent == [("can't read the alerts mailbox", "high")] * 2


def test_mailbox_failure_waits_out_the_grace_period(db: sqlite3.Connection) -> None:
    assert notify.mailbox_failed(db, NOW) == []
    assert notify.mailbox_failed(db, NOW + datetime.timedelta(minutes=14, seconds=59)) == []
    assert notify.mailbox_failed(db, NOW + notify.MAILBOX_GRACE) == [
        Event("mailbox", notify.MAILBOX_FAILURE)
    ]


def test_reading_the_mailbox_starts_a_new_grace_period(db: sqlite3.Connection) -> None:
    notify.mailbox_failed(db, NOW)
    notify.mailbox_read(db)
    later = NOW + datetime.timedelta(hours=1)
    assert notify.mailbox_failed(db, later) == []
    assert notify.mailbox_failed(db, later + notify.MAILBOX_GRACE) != []


def test_other_events_never_repeat(db: sqlite3.Connection) -> None:
    sender = FakeSender()
    send(db, sender, [Event("refused", "refused:m1")])
    send(db, sender, [Event("refused", "refused:m1")], NOW + datetime.timedelta(days=30))
    assert len(sender.sent) == 1


def test_unsent_message_is_tried_next_run(db: sqlite3.Connection) -> None:
    events = [Event("new", "new:a")]
    messages, err = send(db, FakeSender(fail=True), events)
    assert messages == []
    assert "will try again on the next run" in err
    sender = FakeSender()
    assert send(db, sender, events)[0] == ["1 new card alert"]


def test_topic_is_random_and_usable(tmp_path: Path) -> None:
    topic = notify.new_topic()
    assert topic.startswith("tally-")
    assert len(topic) == len("tally-") + 22  # 128 bits, base64
    assert notify.new_topic() != topic
    path = tmp_path / "secrets" / "ntfy-topic"
    notify.save_topic(path, topic)
    assert notify.load_topic(path) == topic
    assert path.stat().st_mode & 0o777 == 0o600


def test_missing_or_short_topic(tmp_path: Path) -> None:
    path = tmp_path / "ntfy-topic"
    with pytest.raises(notify.NotifyError, match="run `tally ntfy-setup`"):
        notify.load_topic(path)
    path.write_text("tally\n")
    with pytest.raises(notify.NotifyError, match="doesn't hold a usable topic"):
        notify.load_topic(path)


class FakePost:
    def __init__(self, status: int = 200, error: Exception | None = None) -> None:
        self.status = status
        self.error = error
        self.calls: list[dict[str, Any]] = []

    def __call__(self, url: str, **kwargs: Any) -> Any:
        self.calls.append({"url": url, **kwargs})
        if self.error:
            raise self.error
        response = requests.Response()
        response.status_code = self.status
        return response


def test_ntfy_sender_posts_the_headline(monkeypatch: pytest.MonkeyPatch) -> None:
    post = FakePost()
    monkeypatch.setattr(requests, "post", post)
    notify.NtfySender("https://ntfy.example.com", "tally-topic").send("1 new card alert", "low")
    [call] = post.calls
    assert call["url"] == "https://ntfy.example.com/tally-topic"
    assert call["data"] == b"1 new card alert"
    assert call["headers"] == {"Title": "tally", "Priority": "low"}


def test_ntfy_errors_never_show_the_topic(monkeypatch: pytest.MonkeyPatch) -> None:
    sender = notify.NtfySender("https://ntfy.example.com", "tally-secret-topic")
    monkeypatch.setattr(requests, "post", FakePost(status=429))
    with pytest.raises(notify.NotifyError, match="HTTP 429") as refused:
        sender.send("x", "low")
    error = requests.ConnectionError("https://ntfy.example.com/tally-secret-topic unreachable")
    monkeypatch.setattr(requests, "post", FakePost(error=error))
    with pytest.raises(notify.NotifyError, match="ConnectionError") as unreachable:
        sender.send("x", "low")
    assert "secret" not in str(refused.value) + str(unreachable.value)


ADDRESS = "alerts@example.com"


@pytest.fixture
def dirs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("TALLY_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("TALLY_SECRETS_DIR", str(tmp_path / "secrets"))
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "config.toml").write_text(f'sheet_id = "s"\ngmail_account = "{ADDRESS}"\n')
    return tmp_path


def test_ntfy_setup_makes_a_topic_once(dirs: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["ntfy-setup"]) == 0
    path = dirs / "secrets" / "ntfy-topic"
    topic = notify.load_topic(path)
    out = capsys.readouterr().out
    assert f"topic:  {topic}" in out
    assert "server: https://ntfy.sh" in out
    assert path.stat().st_mode & 0o777 == 0o600

    assert main(["ntfy-setup"]) == 0
    assert notify.load_topic(path) == topic
    assert "Already set up" in capsys.readouterr().out

    assert main(["ntfy-setup", "--new-topic"]) == 0
    assert notify.load_topic(path) != topic


def test_notify_test(
    dirs: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["notify-test"]) == 1
    assert "run `tally ntfy-setup`" in capsys.readouterr().err
    main(["ntfy-setup"])
    post = FakePost()
    monkeypatch.setattr(requests, "post", post)
    assert main(["notify-test"]) == 0
    assert [c["data"] for c in post.calls] == [b"test notification"]


class FakeImapMailbox:
    def __init__(self, messages: dict[str, bytes]) -> None:
        self.messages = messages

    def __enter__(self) -> "FakeImapMailbox":
        return self

    def __exit__(self, *exc: object) -> None:
        pass

    def message_ids(self, query: str) -> list[str]:
        return list(reversed(self.messages))

    def raw(self, message_id: str) -> bytes:
        return self.messages[message_id]


@pytest.fixture
def mailbox(dirs: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, bytes]:
    """The alerts mailbox's messages; tests add to it between runs."""
    gmail.save_password(dirs / "secrets" / "gmail-app-password", "abcdefghijklmnop")
    messages: dict[str, bytes] = {}
    monkeypatch.setattr(
        gmail.ImapMailbox, "connect", lambda address, password: FakeImapMailbox(messages)
    )
    return messages


def test_gmail_run_notifies_new_alerts_once(
    dirs: Path,
    mailbox: dict[str, bytes],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    main(["ntfy-setup"])
    post = FakePost()
    monkeypatch.setattr(requests, "post", post)
    mailbox["m1"] = DISCOVER
    assert main(["alerts"]) == 0
    assert "Notified: 1 new card alert" in capsys.readouterr().out
    assert main(["alerts"]) == 0
    assert [c["data"] for c in post.calls] == [b"1 new card alert"]


def test_gmail_dry_run_sends_nothing(
    dirs: Path, mailbox: dict[str, bytes], monkeypatch: pytest.MonkeyPatch
) -> None:
    main(["ntfy-setup"])
    post = FakePost()
    monkeypatch.setattr(requests, "post", post)
    mailbox["m1"] = DISCOVER
    assert main(["alerts", "--dry-run"]) == 0
    assert post.calls == []


def test_without_a_topic_the_run_still_works(
    dirs: Path, mailbox: dict[str, bytes], capsys: pytest.CaptureFixture[str]
) -> None:
    mailbox["m1"] = DISCOVER
    assert main(["alerts"]) == 0
    out = capsys.readouterr().out
    assert "1 added" in out
    assert "Notifications are off: run `tally ntfy-setup`" in out


def test_unreachable_mailbox_is_announced_until_it_recovers(
    dirs: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(notify, "MAILBOX_GRACE", datetime.timedelta(0))
    main(["ntfy-setup"])
    post = FakePost()
    monkeypatch.setattr(requests, "post", post)
    gmail.save_password(dirs / "secrets" / "gmail-app-password", "abcdefghijklmnop")

    def refuse(address: str, password: str) -> FakeImapMailbox:
        raise gmail.GmailError("Gmail refused the app password")

    monkeypatch.setattr(gmail.ImapMailbox, "connect", refuse)
    assert main(["alerts"]) == 1
    assert main(["alerts"]) == 1
    assert [c["data"] for c in post.calls] == [b"can't read the alerts mailbox"]

    monkeypatch.setattr(gmail.ImapMailbox, "connect", lambda a, p: FakeImapMailbox({}))
    assert main(["alerts"]) == 0
    monkeypatch.setattr(gmail.ImapMailbox, "connect", refuse)
    assert main(["alerts"]) == 1
    assert len(post.calls) == 2  # announced again: it had recovered in between


def test_one_failed_run_is_not_announced(
    dirs: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    main(["ntfy-setup"])
    post = FakePost()
    monkeypatch.setattr(requests, "post", post)
    gmail.save_password(dirs / "secrets" / "gmail-app-password", "abcdefghijklmnop")

    def time_out(address: str, password: str) -> FakeImapMailbox:
        raise gmail.GmailError("Gmail didn't respond while signing in (timed out)")

    monkeypatch.setattr(gmail.ImapMailbox, "connect", time_out)
    assert main(["alerts"]) == 1
    assert main(["alerts"]) == 1
    assert post.calls == []  # still within the grace period

    conn = store.connect(dirs / "data" / "tally.db")
    try:  # pretend the failures began 20 minutes ago
        old = datetime.datetime.now(datetime.UTC) - datetime.timedelta(minutes=20)
        store.record_notifications(conn, [notify.FAILING_SINCE], old)
    finally:
        conn.close()
    assert main(["alerts"]) == 1
    assert [c["data"] for c in post.calls] == [b"can't read the alerts mailbox"]


def _import_at(db: sqlite3.Connection, when: datetime.datetime) -> None:
    from tally.importers import bofa

    rows = bofa.parse((FIXTURES / "bofa_checking.csv").read_text(newline=""))
    store.save(db, rows, account="bofa_checking", file_name="a.csv", file_sha256="x", now=when)


def test_nothing_imported_yet_is_not_stale(db: sqlite3.Connection) -> None:
    assert notify.stale(db, NOW) == []


def test_stale_after_seven_quiet_days_announced_once(db: sqlite3.Connection) -> None:
    _import_at(db, NOW - datetime.timedelta(days=6, hours=23))
    assert notify.stale(db, NOW) == []
    later = NOW + datetime.timedelta(hours=2)
    events = notify.stale(db, later)
    assert [e.kind for e in events] == ["stale"]
    sender = FakeSender()
    send(db, sender, events, later)
    send(db, sender, notify.stale(db, later + datetime.timedelta(hours=1)), later)
    assert sender.sent == [
        ("no new transactions for a week, check the alerts and exports", "default")
    ]


def test_a_new_alert_ends_the_quiet_spell(db: sqlite3.Connection) -> None:
    from tally.alerts import discover

    _import_at(db, NOW - datetime.timedelta(days=10))
    first = notify.stale(db, NOW)
    send(db, FakeSender(), first)
    alert = discover.parse(DISCOVER)
    assert alert is not None
    store.save_alert(db, alert, now=NOW)
    assert notify.stale(db, NOW + datetime.timedelta(days=1)) == []
    # Another quiet week later, it's a new spell with its own announcement.
    again = notify.stale(db, NOW + datetime.timedelta(days=8))
    assert again and again[0].key != first[0].key


def test_an_import_with_no_new_rows_does_not_count(db: sqlite3.Connection) -> None:
    _import_at(db, NOW - datetime.timedelta(days=10))
    _import_at(db, NOW - datetime.timedelta(days=1))  # same file again: nothing new
    assert [e.kind for e in notify.stale(db, NOW)] == ["stale"]
