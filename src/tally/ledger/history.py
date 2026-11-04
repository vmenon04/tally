"""Committing the ledger after tally changes it, and pushing it to its Forgejo repo.

The ledger folder is a git clone. Changes you made since tally last committed (say, a
category fixed in Fava) are committed on their own first, so tally's commits hold only what
tally wrote. Commit messages give counts, never merchants or amounts.

Before an import, tally pulls from origin, so edits made elsewhere come in first.
Pushing goes to the clone's `origin`, with the Forgejo access token in
secrets/forgejo-token. The token reaches git through its environment, never its command
line (which other accounts can see) or the remote URL (which is saved in .git/config).
A failed push is reported and left for the next import to retry; the commit stays.
"""

import base64
import contextlib
import os
import subprocess
from pathlib import Path

from tally import config

TOKEN_FILE = "forgejo-token"
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
