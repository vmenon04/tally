# Running on a server

The server holds the only copy of the data. Everything runs there as a `tally` system user
(no login, no sudo, no docker); you use it from your own machine with `bin/tally-remote`.

| Path | What |
|---|---|
| `/srv/tally/app/` | The deployed code (`REVISION` names its commit); `app.old/` is the one before |
| `/srv/tally/venv/` | The environment the runs use, with tally installed as a copy |
| `/srv/tally/data/` | `TALLY_DATA_DIR`: database, `config.toml`, `rules.toml`, alert emails |
| `/srv/tally/secrets/` | `TALLY_SECRETS_DIR` |
| `/srv/tally/bin/uv` | uv, pinned |
| `/srv/tally-inbox/` | Drop folder: you can write, `tally` can read (`2770`, group `tally`) |
| `/usr/local/bin/tally-run` | Runs a tally command as `tally` ([`deploy/tally-run`](../deploy/tally-run)) |
| `/usr/local/bin/tally-deploy` | Installs new code ([`deploy/tally-deploy`](../deploy/tally-deploy)) |
| `/etc/sudoers.d/tally` | Lets your account run `tally-run`, and nothing else, as `tally` without a password |

`/srv/tally` and everything in it is owner-only.

## From your machine

```sh
alias tally=~/path/to/tally/bin/tally-remote    # in your shell's rc file

tally import ~/Downloads/*.csv    # send CSVs to the drop folder, then import them on the server
tally report 2026-10              # any other command runs on the server and prints here
tally sync --dry-run
tally deploy                      # install the last commit (asks for your sudo password)
```

`TALLY_HOST` names the server (default `myserver`, an entry in `~/.ssh/config`). `import`
moves the dropped files into `data/inbox` first, so the usual inbox rules apply: a file that
fails stays there, and a dry run leaves them there.

`deploy` first pushes the commit to `origin`, and stops if that fails, so GitHub always has what
the server runs. It then sends `git archive HEAD` (so only committed code, and it refuses to run with
uncommitted changes). On the server, `tally-deploy` unpacks it into `app.new/`, runs the tests
there, and only if they pass swaps it in and updates `venv/`. Installing code needs your
password, unlike running tally, because new code can read the secrets.

## On a schedule

| Timer | Runs | When |
|---|---|---|
| `tally-alerts.timer` | `tally alerts` | every 5 minutes |
| `tally-summary.timer` | `tally summary --email --sync` | Sundays at 6pm New York time (at boot instead, if the server was off then) |
| `tally-sync.timer` | `tally sync` | every night at 3:30am New York time, so Claude categorizes new merchants within a day and Fidelity holdings stay current |

Each runs through its `.service`, sandboxed so it can write only to `/srv/tally/data`.
`systemctl list-timers 'tally-*'` shows the next runs; `journalctl -u tally-alerts -u
tally-summary -u tally-sync` shows errors (the output itself isn't logged, since it names merchants and
amounts). Commands run through `tally-run` aren't sandboxed, but run as the same `tally` user.

## Fava, all the time

`tally-fava.service` runs Fava for the ledger, sandboxed with no network at all: it serves
on the Unix socket `/srv/tally-inbox/fava.sock`, in a folder only your account, `tally` and
root can open. Other accounts on the server can't reach it. Two ways in:

- **Tailscale** (phone, laptop): `tailscale serve` on the server forwards
  `https://<server>.<tailnet>.ts.net` to the socket, for your tailnet's devices only.
- **SSH** (any machine with your key): `tally-remote fava`, then http://localhost:5000.

To install it:

```sh
scp deploy/tally-fava.service myserver:/tmp/
ssh -t myserver 'sudo install -m 644 /tmp/tally-fava.service /etc/systemd/system/ && rm /tmp/tally-fava.service && sudo systemctl daemon-reload && sudo systemctl enable --now tally-fava'
```

Fava reloads the ledger when its files change, so imports show up without a restart.
Anything you change in Fava's editor is committed by tally on its next import or sync.

## Sure

[Sure](https://github.com/we-promise/sure) is the web and phone UI. It runs beside tally as
a Docker Compose stack ([`deploy/sure/compose.yml`](../deploy/sure/compose.yml)) in
`/srv/sure`, owned by root, and listens on `127.0.0.1:3200` only. `tailscale serve` puts it
on the tailnet at `https://<server>.<tailnet>.ts.net:8443`.

The ledger stays the record. Sure's database holds a copy that tally can push again, so it
isn't backed up.

To install it:

```sh
ssh myserver 'mkdir -p /tmp/sure' && scp deploy/sure/compose.yml deploy/sure/install myserver:/tmp/sure/
ssh -t myserver 'sudo sh /tmp/sure/install && rm -r /tmp/sure'
```

[`deploy/sure/install`](../deploy/sure/install) copies the compose file to `/srv/sure`,
makes `/srv/sure/.env` with a random `SECRET_KEY_BASE` and database password, starts the
containers and sets up `tailscale serve`.

The `.env` is made once and never replaced: its `SECRET_KEY_BASE` encrypts what Sure
stores, so a new one would make the database unreadable. Then open the address above,
create your account, and set **Settings > Self-Hosting > Onboarding** to **Closed** so no
one else can register.

To upgrade, change the image tag in `deploy/sure/compose.yml`, commit, and run the same two
commands again.

## Setting up a server

From a clone of the repo on the server, with uv installed at `/srv/tally/bin/uv`:

```sh
sudo useradd --system --user-group --home-dir /srv/tally --create-home --shell /usr/sbin/nologin tally
sudo chmod 700 /srv/tally
sudo install -d -o "$USER" -g tally -m 2770 /srv/tally-inbox
sudo install -m 755 deploy/tally-run deploy/tally-deploy /usr/local/bin/
sudo visudo -c -f deploy/sudoers && sudo install -m 440 deploy/sudoers /etc/sudoers.d/tally
sudo install -m 644 deploy/tally-*.service deploy/tally-*.timer /etc/systemd/system/
sudo systemctl daemon-reload
```

Then `bin/tally-remote deploy` from your machine, copy `data/` and `secrets/` into
`/srv/tally` (owned by `tally`, 700 folders and 600 files), and
`sudo systemctl enable --now tally-alerts.timer tally-summary.timer tally-sync.timer`.
