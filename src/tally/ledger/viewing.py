"""Serving Fava, Beancount's web UI, for as long as you're looking at it.

Fava has no login, so it never listens on a network address other than this machine's
loopback. On the server it listens on a Unix socket instead, in a folder only your account
and tally can open (/srv/tally-inbox), so other accounts on the server can't reach it either;
`tally-remote fava` forwards a port on your machine to that socket over SSH.
"""

import signal
from pathlib import Path
from types import FrameType

from werkzeug.serving import make_server

from tally.ledger import book


def serve(data_dir: Path, *, socket: Path | None, port: int) -> None:
    """Serve Fava on the socket, or on 127.0.0.1:port, until interrupted or hung up."""
    from fava.application import create_app  # slow to import; only this command needs it

    main = book.folder(data_dir) / book.MAIN
    if not main.is_file():
        raise book.LedgerError(f"no ledger at {main}")
    book.ensure_pending(main.parent)
    app = create_app([str(main)])
    ours = None
    if socket is None:
        server = make_server("127.0.0.1", port, app, threaded=True)
        print(f"Fava: http://127.0.0.1:{port}  (Ctrl-C to stop)", flush=True)
    else:
        socket.unlink(missing_ok=True)  # left behind by a run that was killed
        server = make_server(f"unix://{socket}", 0, app, threaded=True)
        # The folder decides who can connect; the socket itself must let your account in.
        socket.chmod(0o666)
        ours = socket.stat().st_ino
        print(f"Fava: serving on {socket}  (Ctrl-C to stop)", flush=True)

    def stop(signum: int, frame: FrameType | None) -> None:
        raise KeyboardInterrupt

    signal.signal(signal.SIGHUP, stop)  # the SSH session ended
    signal.signal(signal.SIGTERM, stop)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        # Only if it's still ours: another Fava may have replaced it since (a second
        # `tally-remote fava`), and removing that would cut it off.
        if socket is not None and _inode(socket) == ours:
            socket.unlink(missing_ok=True)


def _inode(path: Path) -> int | None:
    try:
        return path.stat().st_ino
    except FileNotFoundError:
        return None
