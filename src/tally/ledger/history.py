"""Committing the ledger after tally changes it, and pushing it to its Forgejo repo.

The ledger folder is a git clone. Changes you made since tally last committed (say, a
category fixed in Fava) are committed on their own first, so tally's commits hold only what
tally wrote. Commit messages give counts, never merchants or amounts.

Before an import, tally pulls from origin, so edits made elsewhere come in first.
Pushing goes to the clone's `origin`, with the Forgejo access token in
secrets/forgejo-token. The token reaches git through its environment, never its command
line (which other accounts can see) or the remote URL (which is saved in .git/config).
A failed push is reported and left for the next import to retry; the commit stays.

If the clone also has a `backup` remote (a private GitHub repo), every push goes there too,
over SSH with the deploy key in secrets/github-backup-key. GitHub's host key is pinned
here, so ssh never asks about it or trusts a different one. Before each push, tally asks
GitHub, without signing in, whether it shows that repo to anyone: if it does, the repo is
public, and nothing is pushed.
"""

import base64
import contextlib
import os
import re
import shlex
import subprocess
import tempfile
from pathlib import Path

import requests

from tally import config

TOKEN_FILE = "forgejo-token"
BACKUP = "backup"
BACKUP_KEY_FILE = "github-backup-key"
# From https://api.github.com/meta; github.com and its port-443 SSH host share one key.
GITHUB_HOST_KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOMqqnkVzrm0SdG6UOoqKLsabgH5C9okWi0dh2l9GKJl"
_IDENTITY = {
    "GIT_AUTHOR_NAME": "tally",
    "GIT_AUTHOR_EMAIL": "tally@localhost",
    "GIT_COMMITTER_NAME": "tally",
    "GIT_COMMITTER_EMAIL": "tally@localhost",
}


class HistoryError(RuntimeError):
    """git failed."""


class Unreachable(HistoryError):
    """origin couldn't be reached; the ledger can still be changed and pushed later."""


def is_repo(folder: Path) -> bool:
    return (folder / ".git").exists()


def commit(folder: Path, message: str) -> bool:
    """Commit everything changed in the ledger folder. Returns False if nothing had changed."""
    _git(folder, "add", "--all")
    if _git(folder, "status", "--porcelain").strip() == "":
        return False
    _git(folder, "commit", "--quiet", "--message", message)
    return True


def pull(folder: Path) -> None:
    """Bring in commits from origin, putting tally's unpushed ones on top of them."""
    if "origin" not in _git(folder, "remote").split():
        return
    branch = _git(folder, "branch", "--show-current").strip()
    try:
        heads = _git(folder, "ls-remote", "--heads", "origin", branch, env=_auth_env())
    except HistoryError as e:
        raise Unreachable(str(e)) from e
    if not heads.strip():
        return  # origin doesn't have this branch yet
    try:
        _git(folder, "pull", "--quiet", "--rebase", "origin", branch, env=_auth_env())
    except HistoryError:
        with contextlib.suppress(HistoryError):
            _git(folder, "rebase", "--abort")
        raise


def push(folder: Path) -> None:
    """Push the current branch to origin, if the clone has one."""
    if "origin" not in _git(folder, "remote").split():
        return
    _git(folder, "push", "--quiet", "origin", "HEAD", env=_auth_env())


def push_backup(folder: Path) -> None:
    """Push the current branch to the backup remote, if the clone has one. Raises
    HistoryError, without pushing, if that's a public GitHub repo."""
    if BACKUP not in _git(folder, "remote").split():
        return
    repo = github_repo(_git(folder, "remote", "get-url", BACKUP).strip())
    if repo is not None and _public(repo):
        raise HistoryError(
            f"the backup repo github.com/{repo} is public, so the ledger wasn't pushed to "
            "it: make it private on GitHub"
        )
    with tempfile.TemporaryDirectory() as tmp:
        known_hosts = Path(tmp) / "known_hosts"
        known_hosts.write_text(
            f"github.com {GITHUB_HOST_KEY}\n[ssh.github.com]:443 {GITHUB_HOST_KEY}\n",
            encoding="utf-8",
        )
        _git(folder, "push", "--quiet", BACKUP, "HEAD", env=_ssh_env(known_hosts))


def github_repo(url: str) -> str | None:
    """owner/name for a GitHub remote URL (SSH or HTTPS), else None."""
    found = re.fullmatch(
        r"(?:git@github\.com:|ssh://git@(?:ssh\.)?github\.com(?::\d+)?/|https://github\.com/)"
        r"([\w.-]+/[\w.-]+?)(?:\.git)?/?",
        url,
    )
    return found[1] if found else None


def _public(repo: str) -> bool:
    """Whether GitHub shows the repo to someone not signed in. Unreachable counts as not:
    the push then fails on its own."""
    try:
        answer = requests.get(f"https://api.github.com/repos/{repo}", timeout=15)
    except requests.RequestException:
        return False
    return answer.status_code == 200


def _ssh_env(known_hosts: Path) -> dict[str, str]:
    """ssh with only the deploy key and the pinned host key, never asking anything."""
    key = config.secrets_dir() / BACKUP_KEY_FILE
    options = [
        "ssh",
        "-F", "/dev/null",
        "-o", "BatchMode=yes",
        "-o", "IdentitiesOnly=yes",
        "-o", "IdentityAgent=none",
        "-o", "StrictHostKeyChecking=yes",
        "-o", "HostKeyAlgorithms=ssh-ed25519",
        "-o", f"UserKnownHostsFile={known_hosts}",
        "-o", "GlobalKnownHostsFile=/dev/null",
    ]  # fmt: skip
    if key.exists():
        options += ["-i", str(key)]
    return {"GIT_SSH_COMMAND": shlex.join(options)}


def _auth_env() -> dict[str, str]:
    path = config.secrets_dir() / TOKEN_FILE
    try:
        token = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return {}
    basic = base64.b64encode(f"tally:{token}".encode()).decode()
    return {
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "http.extraHeader",
        "GIT_CONFIG_VALUE_0": f"Authorization: Basic {basic}",
    }


def _git(folder: Path, *args: str, env: dict[str, str] | None = None) -> str:
    # Inherited GIT_* variables (a git hook sets GIT_INDEX_FILE) would point git elsewhere.
    inherited = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    full_env = {**inherited, **_IDENTITY, "GIT_TERMINAL_PROMPT": "0", **(env or {})}
    try:
        result = subprocess.run(
            ["git", "-C", str(folder), *args],
            capture_output=True,
            text=True,
            env=full_env,
            check=False,
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        raise HistoryError(f"git {args[0]}: {e}") from e
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip().splitlines()
        reason = next(
            (line for line in detail if line.startswith(("fatal:", "error:", " ! ["))),
            detail[-1] if detail else str(result.returncode),
        )
        raise HistoryError(f"git {args[0]} failed: {reason.strip()}")
    return result.stdout
