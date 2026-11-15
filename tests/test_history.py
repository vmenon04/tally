import base64
from pathlib import Path

import pytest
import requests

from tally.ledger import history


def test_token_goes_in_an_environment_header(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TALLY_SECRETS_DIR", str(tmp_path))
    (tmp_path / "forgejo-token").write_text("made-up-token\n")
    env = history._auth_env()
    assert env["GIT_CONFIG_KEY_0"] == "http.extraHeader"
    header = env["GIT_CONFIG_VALUE_0"].removeprefix("Authorization: Basic ")
    assert base64.b64decode(header) == b"tally:made-up-token"


def test_no_token_file_pushes_without_one(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TALLY_SECRETS_DIR", str(tmp_path))
    assert history._auth_env() == {}


def git(folder: Path, *args: str) -> str:
    return history._git(folder, *args)


@pytest.fixture
def clone(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("TALLY_SECRETS_DIR", str(tmp_path / "secrets"))
    folder = tmp_path / "ledger"
    folder.mkdir()
    git(folder, "init", "--quiet", "--initial-branch", "main")
    (folder / "main.beancount").write_text('option "operating_currency" "USD"\n')
    history.commit(folder, "Start")
    return folder


def bare(tmp_path: Path, name: str) -> Path:
    remote = tmp_path / name
    git(tmp_path, "init", "--quiet", "--bare", str(remote))
    return remote


def test_no_backup_remote_is_nothing_to_do(clone: Path) -> None:
    history.push_backup(clone)  # no error


def test_backup_gets_each_commit(clone: Path, tmp_path: Path) -> None:
    remote = bare(tmp_path, "backup.git")
    git(clone, "remote", "add", "backup", str(remote))
    history.push_backup(clone)
    assert git(remote, "log", "--format=%s", "main").split("\n")[0] == "Start"


def test_backup_ssh_trusts_only_githubs_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TALLY_SECRETS_DIR", str(tmp_path))
    (tmp_path / "github-backup-key").write_text("made-up key\n")
    command = history._ssh_env(tmp_path / "known_hosts")["GIT_SSH_COMMAND"]
    assert "StrictHostKeyChecking=yes" in command and "BatchMode=yes" in command
    assert f"-i {tmp_path / 'github-backup-key'}" in command
    assert "IdentitiesOnly=yes" in command


def test_backup_still_gets_a_commit_when_forgejo_is_down(
    clone: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    import sys

    from tally.ledger import updating

    remote = bare(tmp_path, "backup.git")
    git(clone, "remote", "add", "origin", str(tmp_path / "gone.git"))
    git(clone, "remote", "add", "backup", str(remote))
    (clone / "main.beancount").write_text('option "operating_currency" "USD"\n; more\n')
    updating.save(clone, "Bring the ledger up to date", sys.stderr)
    assert "the next run tries again" in capsys.readouterr().err
    assert git(remote, "log", "--format=%s", "main").split("\n")[0] == (
        "Bring the ledger up to date"
    )


@pytest.mark.parametrize(
    ("url", "repo"),
    [
        ("git@github.com:someone/ledger-backup.git", "someone/ledger-backup"),
        ("ssh://git@ssh.github.com:443/someone/ledger-backup.git", "someone/ledger-backup"),
        ("https://github.com/someone/ledger-backup", "someone/ledger-backup"),
        ("/srv/elsewhere/backup.git", None),
        ("git@gitlab.com:someone/ledger.git", None),
    ],
)
def test_github_repo(url: str, repo: str | None) -> None:
    assert history.github_repo(url) == repo


def test_a_public_backup_repo_gets_nothing(clone: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    git(clone, "remote", "add", "backup", "git@github.com:someone/ledger-backup.git")
    asked: list[str] = []

    def get(url: str, **_: object) -> requests.Response:
        asked.append(url)
        response = requests.Response()
        response.status_code = 200  # anyone can see it
        return response

    monkeypatch.setattr(requests, "get", get)
    with pytest.raises(history.HistoryError, match="is public, so the ledger wasn't pushed"):
        history.push_backup(clone)
    assert asked == ["https://api.github.com/repos/someone/ledger-backup"]
