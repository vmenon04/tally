# Running on a server

You don't need a server: [Getting started](getting-started.md) runs everything on your own
computer. I built tally for my banks and my home server, though, and this page is how I run it
there, so it syncs on a schedule and Sure is always up. It assumes a Linux server with systemd
that you reach over SSH.

Everything runs as a `tally` system user (no login, no sudo, no docker), sandboxed by systemd so
it can write only to its data folder. You use it from your own machine with `bin/tally-remote`.

| Path | What |
|---|---|
| `/srv/tally/app/` | The deployed code (`REVISION` names its commit); `app.old/` is the one before |
| `/srv/tally/venv/` | The environment the runs use |
| `/srv/tally/data/` | `TALLY_DATA_DIR`: your `data/` folder |
| `/srv/tally/secrets/` | `TALLY_SECRETS_DIR`: your `secrets/` folder |
| `/srv/tally/bin/uv` | uv |
| `/srv/tally-inbox/` | Where Fava's socket is, so you and `tally` can both open it |
| `/usr/local/bin/tally-run` | Runs a tally command as `tally` ([`deploy/tally-run`](../deploy/tally-run)) |
| `/usr/local/bin/tally-deploy` | Installs new code ([`deploy/tally-deploy`](../deploy/tally-deploy)) |
| `/etc/sudoers.d/tally` | Lets your account run `tally-run`, and nothing else, as `tally` without a password |

`/srv/tally` and everything in it is owner-only.

## Setting it up

1. Install [uv](https://docs.astral.sh/uv/) on the server at `/srv/tally/bin/uv`, and clone the
   repo there.
2. **Pick your time zone.** The units in `deploy/` use `America/New_York`, which decides when a
   month starts and when the summary goes out. Change it in `tally-*.service`,
   `tally-*.timer` and `tally-run` before installing them.
3. From the clone on the server:

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

4. On your own machine, name the server: copy `.env.example` to `.env` and set `TALLY_HOST` to
   its entry in `~/.ssh/config`. Then run `bin/tally-remote deploy`.
5. Copy your `data/` and `secrets/` folders into `/srv/tally` (owned by `tally`, 700 folders
   and 600 files), and start the timers:

   ```sh
   sudo systemctl enable --now tally-summary.timer tally-sync.timer
   ```

## From your machine

```sh
alias tally=~/path/to/tally/bin/tally-remote

tally sync --dry-run
tally fava
tally deploy
```

- `alias tally=~/path/to/tally/bin/tally-remote`: In your shell's rc file.
- `tally sync --dry-run`: Any command runs on the server and prints here.
- `tally fava`: Fava on the server, at http://localhost:5000 here.
- `tally deploy`: Install the last commit (asks for your sudo password)

`deploy` first pushes the commit to `origin` and stops if that fails, so your fork always has
what the server runs. It sends only committed code (`git archive HEAD`). On the server,
`tally-deploy` unpacks it into `app.new/`, runs the tests there, and only if they pass swaps it
in. Installing code needs your password, unlike running tally, because new code can read the
secrets.

`tally fava` runs Fava on the server for as long as it stays open, on a Unix socket only your
account, `tally` and root can open, and SSH forwards it to your machine. Fava has no login, so
it never listens on a network address.

## On a schedule

| Timer | Runs | When |
|---|---|---|
| `tally-sync.timer` | `tally sync` | Every 2 hours |
| `tally-summary.timer` | `tally summary --email --sync` | Sundays at 6pm (at boot instead, if the server was off then) |

`systemctl list-timers 'tally-*'` shows the next runs; `journalctl -u tally-sync -u
tally-summary` shows errors. Their output isn't logged, since it names merchants and amounts.

## The ledger's own repo

With the ledger in a git repo that has an `origin`, every change tally makes is pushed there, and
edits made elsewhere are pulled before each sync. I use [Forgejo](https://forgejo.org) on the
same server; any git host that takes a token over HTTPS works.

1. Make an empty private repo, and an access token that can write to it. Save the token as
   `/srv/tally/secrets/forgejo-token` (owner `tally`, 600).
2. Push your `data/ledger` to it, then clone it as `tally` into `/srv/tally/data/ledger`.

If the host can't be reached, the sync carries on and pushes next time.

## Backing the ledger up to GitHub

A host on the same server is no backup. With a `backup` remote, every push also goes to a
private GitHub repo. Before each push tally checks, without signing in, that GitHub doesn't show
the repo to anyone, and pushes nothing if it does.

1. On GitHub, make an empty **private** repo (say `ledger-backup`).
2. On the server, make a key only tally can read, and print its public half:

   ```sh
   sudo -u tally ssh-keygen -q -t ed25519 -N '' -C tally-ledger-backup \
     -f /srv/tally/secrets/github-backup-key
   sudo cat /srv/tally/secrets/github-backup-key.pub
   ```

3. In the repo's Settings → Deploy keys, add that public key with **Allow write access**. A
   deploy key opens that one repo and nothing else.
4. Add the remote: `sudo -u tally git -C /srv/tally/data/ledger remote add backup
   git@github.com:<you>/ledger-backup.git`. If port 22 is blocked, use
   `ssh://git@ssh.github.com:443/<you>/ledger-backup.git`.

## Sure

[Sure](https://github.com/we-promise/sure) is a self-hosted web and phone app for your money.
tally fills it from the ledger ([Showing it in Sure](ledger.md#showing-it-in-sure)), so it's a
nicer way to look; the ledger stays the record. It runs beside tally as a Docker Compose stack
([`deploy/sure/compose.yml`](../deploy/sure/compose.yml)) in `/srv/sure`, listening on
`127.0.0.1:3200` only.

To install it, from your machine:

```sh
ssh "$TALLY_HOST" 'mkdir -p /tmp/sure' && scp -r deploy/sure/compose.yml deploy/sure/install deploy/sure/brand "$TALLY_HOST":/tmp/sure/
ssh -t "$TALLY_HOST" 'sudo sh /tmp/sure/install && rm -r /tmp/sure'
```

[`deploy/sure/install`](../deploy/sure/install) copies the compose file to `/srv/sure`, makes
`/srv/sure/.env` with a random `SECRET_KEY_BASE` and database password, and starts the
containers. If [Tailscale](https://tailscale.com) is installed, it also runs `tailscale serve`,
which puts Sure on your tailnet at `https://<server>.<tailnet>.ts.net`, reachable from your
phone.

The `.env` is made once and never replaced: its `SECRET_KEY_BASE` encrypts what Sure stores.
Open Sure, create your account, and set **Settings > Self-Hosting > Onboarding** to **Closed**
so no one else can register. Then make an API key in Sure's settings with read and write access,
and save it for tally:

```sh
tally sure-login
```

To upgrade Sure, change the image tag in `deploy/sure/compose.yml` and run the two install
commands again. It's branded as tally by the icons in `deploy/sure/brand`.

### Sure's chat

Sure's chat can be answered by tally, so it needs no AI API key: Sure's **External** assistant
posts each conversation to `tally chat`
([`deploy/tally-chat.service`](../deploy/tally-chat.service)), which asks Claude to reply from
the same facts as the monthly review. For single purchases or a merchant, Claude looks things up
in Sure through `tally lookups`, which passes on only Sure's tools that read, so Claude can't
change anything. A reply quoting a dollar amount it wasn't shown is refused. Replies take 10 to
30 seconds.

To set it up, install the service, then run Sure's install again, which adds the tokens and the
relay between them:

```sh
scp deploy/tally-chat.service "$TALLY_HOST":/tmp/
ssh -t "$TALLY_HOST" 'sudo install -m 644 /tmp/tally-chat.service /etc/systemd/system/ && rm /tmp/tally-chat.service && sudo systemctl daemon-reload && sudo systemctl enable tally-chat'
```

Then turn on AI for your user in Sure's settings. `tally deploy` restarts `tally chat` onto the
new code. [Architecture](architecture.md) has how the pieces connect.
