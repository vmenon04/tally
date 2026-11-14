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

`TALLY_HOST` names the server, an entry in `~/.ssh/config`. Set it in your environment or in
the repo's `.env` (copy [`.env.example`](../.env.example); `.env` is never committed). `import`
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
| `tally-sync.timer` | `tally sync` | every night at 3:30am New York time, so Claude categorizes new merchants within a day, Fidelity holdings stay current and Sure matches the ledger |

Each runs through its `.service`, sandboxed so it can write only to `/srv/tally/data`.
`systemctl list-timers 'tally-*'` shows the next runs; `journalctl -u tally-alerts -u
tally-summary -u tally-sync` shows errors (the output itself isn't logged, since it names merchants and
amounts). Commands run through `tally-run` aren't sandboxed, but run as the same `tally` user.

## Fava, to edit the ledger

Sure shows the ledger day to day. To edit it (your plan, a category, a line that fails
bean-check), run `tally-remote fava` and open http://localhost:5000. It starts Fava on the
server for as long as it stays open, on the Unix socket `/srv/tally-inbox/fava.sock` in a
folder only your account, `tally` and root can open, and SSH forwards your port to it.
Anything you change in Fava's editor is committed by tally on its next import or sync.

## Sure

[Sure](https://github.com/we-promise/sure) is the web and phone UI. It runs beside tally as
a Docker Compose stack ([`deploy/sure/compose.yml`](../deploy/sure/compose.yml)) in
`/srv/sure`, owned by root, and listens on `127.0.0.1:3200` only. `tailscale serve` puts it
on the tailnet at `https://<server>.<tailnet>.ts.net`.

The ledger stays the record. Sure's database holds a copy that tally can push again, so it
isn't backed up.

To install it:

```sh
ssh "$TALLY_HOST" 'mkdir -p /tmp/sure' && scp -r deploy/sure/compose.yml deploy/sure/install deploy/sure/brand "$TALLY_HOST":/tmp/sure/
ssh -t "$TALLY_HOST" 'sudo sh /tmp/sure/install && rm -r /tmp/sure'
```

[`deploy/sure/install`](../deploy/sure/install) copies the compose file to `/srv/sure`,
makes `/srv/sure/.env` with a random `SECRET_KEY_BASE` and database password, starts the
containers and sets up `tailscale serve`.

The `.env` is made once and never replaced: its `SECRET_KEY_BASE` encrypts what Sure
stores, so a new one would make the database unreadable. Then open the address above,
create your account, and set **Settings > Self-Hosting > Onboarding** to **Closed** so no
one else can register.

tally reaches Sure through its API. Make an API key in Sure's settings with read and write
access, then save it on the server; the command asks for it with hidden input and checks
Sure accepts it:

```sh
tally sure-login
```

The key is kept in `secrets/sure-api-key`. `sure_url` in `config.toml` says where Sure
listens, if it isn't `http://127.0.0.1:3200`.

It's named tally (`PRODUCT_NAME` in the compose file) and wears tally's logo: when the web
container starts, [`deploy/sure/brand/apply`](../deploy/sure/brand/apply) copies the icons in
`deploy/sure/brand` over Sure's, by name, so it keeps working across upgrades. Browsers keep
old icons for a while; a phone's home screen icon changes once it's added again.

To upgrade, change the image tag in `deploy/sure/compose.yml`, commit, and run the same two
commands again.

### Sure's chat

Sure's chat is answered by tally, so it needs no AI API key. Sure's **External** assistant
posts each conversation to `tally chat` ([`deploy/tally-chat.service`](../deploy/tally-chat.service)),
which asks Claude, the way categorizing does, to reply from the same facts as the monthly
review ([summary.md](summary.md)). For more, such as single purchases or a merchant, it looks
things up in Sure through `tally lookups`, an MCP server that passes on only Sure's tools that
read (see `src/tally/lookups.py`), so Claude never gets Sure's token and can't change anything.
A reply quoting a dollar amount that neither the facts, the conversation nor a lookup shows is
asked again once, then refused. A reply takes 10 to 30 seconds and shows up whole.

`tally chat` serves on the Unix socket `/srv/tally-chat/chat.sock`. A relay in Sure's compose
file (`tally-chat`, nginx) passes Sure's requests to it; it has no port on the server, so only
Sure's containers reach it. Requests need the token in `secrets/chat-token`, which `install`
makes once as `CHAT_TOKEN` in `/srv/sure/.env` and copies for tally. Likewise `MCP_TOKEN`
(tally's copy is `secrets/sure-mcp-token`) lets the lookups into Sure's `/mcp`, as the user
`MCP_USER_EMAIL`, which `install` asks for the first time. Without `secrets/sure-mcp-token`,
the chat answers from the facts alone.

To set it up, install the service, then run Sure's install again (above), which adds the token,
the socket's folder and the relay, and starts the service:

```sh
scp deploy/tally-chat.service "$TALLY_HOST":/tmp/
ssh -t "$TALLY_HOST" 'sudo install -m 644 /tmp/tally-chat.service /etc/systemd/system/ && rm /tmp/tally-chat.service && sudo systemctl daemon-reload && sudo systemctl enable tally-chat'
```

Then turn on AI for your user in Sure's settings. `tally-remote deploy` restarts `tally chat`
onto the new code.

## Setting up a server

From a clone of the repo on the server, with uv installed at `/srv/tally/bin/uv`:

```sh
sudo useradd --system --user-group --home-dir /srv/tally --create-home --shell /usr/sbin/nologin tally
sudo chmod 700 /srv/tally
sudo install -d -o "$USER" -g tally -m 2770 /srv/tally-inbox
sudo install -m 755 deploy/tally-run deploy/tally-deploy /usr/local/bin/
sed "s/^YOUR_ACCOUNT/$USER/" deploy/sudoers >/tmp/tally-sudoers
sudo visudo -c -f /tmp/tally-sudoers && sudo install -m 440 /tmp/tally-sudoers /etc/sudoers.d/tally && rm /tmp/tally-sudoers
sudo install -m 644 deploy/tally-*.service deploy/tally-*.timer /etc/systemd/system/
sudo systemctl daemon-reload
```

Then `bin/tally-remote deploy` from your machine, copy `data/` and `secrets/` into
`/srv/tally` (owned by `tally`, 700 folders and 600 files), and
`sudo systemctl enable --now tally-alerts.timer tally-summary.timer tally-sync.timer`.
Sure and its chat are set up as above.
