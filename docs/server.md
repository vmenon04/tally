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

`deploy` sends `git archive HEAD` (so only committed code, and it refuses to run with
uncommitted changes). On the server, `tally-deploy` unpacks it into `app.new/`, runs the tests
there, and only if they pass swaps it in and updates `venv/`. Installing code needs your
password, unlike running tally, because new code can read the secrets.

## Every 5 minutes

`tally-alerts.timer` runs `tally alerts --gmail` through `tally-alerts.service`, which is
sandboxed so it can write only to `/srv/tally/data`. `systemctl list-timers tally-alerts`
shows the next run; `journalctl -u tally-alerts` shows errors (the run's report itself isn't
logged, since it names merchants and amounts). Commands run through `tally-run` aren't
sandboxed, but run as the same `tally` user.

## Setting up a server

From a clone of the repo on the server, with uv installed at `/srv/tally/bin/uv`:

```sh
sudo useradd --system --user-group --home-dir /srv/tally --create-home --shell /usr/sbin/nologin tally
sudo chmod 700 /srv/tally
sudo install -d -o "$USER" -g tally -m 2770 /srv/tally-inbox
sudo install -m 755 deploy/tally-run deploy/tally-deploy /usr/local/bin/
sudo visudo -c -f deploy/sudoers && sudo install -m 440 deploy/sudoers /etc/sudoers.d/tally
sudo install -m 644 deploy/tally-alerts.service deploy/tally-alerts.timer /etc/systemd/system/
sudo systemctl daemon-reload
```

Then `bin/tally-remote deploy` from your machine, copy `data/` and `secrets/` into
`/srv/tally` (owned by `tally`, 700 folders and 600 files), and
`sudo systemctl enable --now tally-alerts.timer`.
