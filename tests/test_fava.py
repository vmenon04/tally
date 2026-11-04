import http.client
import io
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest
from test_ledger_import import make_data_dir

from tally import cli


class _UnixConnection(http.client.HTTPConnection):
    def __init__(self, path: Path) -> None:
        super().__init__("localhost")
        self.path = path

    def connect(self) -> None:
        self.sock = socket.socket(socket.AF_UNIX)
        self.sock.connect(str(self.path))


def test_serves_on_a_socket_until_hung_up(tmp_path: Path) -> None:
    data_dir = make_data_dir(tmp_path)
    # Socket paths are limited to about 100 characters, and tmp_path can be longer.
    short = Path(tempfile.mkdtemp(prefix="fava", dir="/tmp"))
    sock = short / "fava.sock"
    try:
        proc = subprocess.Popen(
            [
                sys.executable,
                "-c",
                "import sys; from tally.cli import main; sys.exit(main())",
                "fava",
                "--socket",
                str(sock),
            ],
            env={"TALLY_DATA_DIR": str(data_dir), "PATH": "/usr/bin:/bin"},
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        deadline = time.monotonic() + 30
        while not sock.exists():
            assert proc.poll() is None, proc.stderr.read() if proc.stderr else ""
            assert time.monotonic() < deadline
            time.sleep(0.1)
        assert sock.stat().st_mode & 0o777 == 0o666
        connection = _UnixConnection(sock)
        connection.request("GET", "/")
        assert connection.getresponse().status == 302
        proc.send_signal(signal.SIGHUP)
        assert proc.wait(timeout=10) == 0
        assert not sock.exists()
    finally:
        if proc.poll() is None:
            proc.kill()
        shutil.rmtree(short)


def test_no_ledger_is_an_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TALLY_DATA_DIR", str(tmp_path))
    err = io.StringIO()
    monkeypatch.setattr(sys, "stderr", err)
    assert cli.main(["fava"]) == 1
    assert "no ledger" in err.getvalue()
