# Running on a server

You don't need a server: [Getting started](getting-started.md) runs everything on your own
computer. I built tally for my banks and my home server, though, and this page is how I run it
there, so it syncs on a schedule and Sure is always up. It assumes a Linux server with Docker
that you reach over SSH.

tally runs from the image CI publishes, `ghcr.io/vmenon04/tally:<version>`
([The image](development.md#the-image)), as two containers from
[`deploy/compose.yml`](../deploy/compose.yml):

| Container | What it does |
|---|---|
| `tally-schedule-1` | Runs [`deploy/crontab`](../deploy/crontab) with supercronic: `tally sync` every 2 hours, and the weekly summary on Sunday evenings |
| `tally-chat-1` | Answers [Sure's chat](#sures-chat) |

Both run as a `tally` system user (no login, no sudo, not in the docker group) that owns the
data. Each container's own files are read-only, it has no Linux capabilities, and it can write
only to the data folder (and the chat to its socket's folder). The image holds no data or
secrets; they stay on the server:

| Path | What |
|---|---|
| `/srv/tally/data/` | `TALLY_DATA_DIR`: your `data/` folder |
| `/srv/tally/secrets/` | `TALLY_SECRETS_DIR`: your `secrets/` folder, read-only to the containers |
| `/srv/tally-chat/` | The chat's socket |
| `/opt/tally/` | `compose.yml`, `crontab` and a `.env` naming the tally user |

`/srv/tally` and everything in it is owner-only. The image is built for x86-64 servers.

## Setting it up

1. Make the `tally` user and its folders, then copy your `data/` and `secrets/` folders into
   `/srv/tally` (owned by `tally`, 700 folders and 600 files):

   ```sh
   sudo useradd --system --user-group --home-dir /srv/tally --create-home --shell /usr/sbin/nologin tally
   sudo chmod 700 /srv/tally
   sudo install -d -o tally -g tally -m 755 /srv/tally-chat
   ```

2. Copy `deploy/compose.yml` and `deploy/crontab` into `/opt/tally`, and give it a `.env`
   naming the tally user and, if it isn't `America/New_York`, your time zone, which decides
   when a month starts and when the summary goes out:

   ```sh
   sudo install -d /opt/tally
   sudo install -m 644 deploy/compose.yml deploy/crontab /opt/tally/
   printf 'TALLY_USER=%s:%s\nTALLY_TZ=America/New_York\n' "$(id -u tally)" "$(id -g tally)" | sudo tee /opt/tally/.env
   ```

3. Start the schedule. The chat starts once [Sure](#sure) is set up, since it needs Sure's
   token:

   ```sh
   sudo docker compose --project-directory /opt/tally up -d schedule
   ```

To upgrade, change the image's version in `/opt/tally/compose.yml` and run
`sudo docker compose --project-directory /opt/tally up -d`. I keep my compose file in a private
repo instead, and a GitHub Actions runner on the server runs that `up` whenever I push; any way
of getting the file there works.

## Running a command

Any tally command runs in the schedule's container, as `tally`, with the same version and
folders:

```sh
ssh -t server sudo docker exec -it tally-schedule-1 tally sync --dry-run
```

The login commands (`simplefin-login`, `gmail-login`, `sure-login`, `ntfy-setup`) save to
`secrets/`, which the containers can only read, so they run in a container of their own with it
writable:

```sh
sudo docker compose --project-directory /opt/tally run --rm --entrypoint tally \
  --volume /srv/tally/secrets:/run/secrets-rw --env TALLY_SECRETS_DIR=/run/secrets-rw \
  schedule simplefin-login
```

To edit the ledger in Fava, start it on the server and forward its port to your machine; it
stops when you press Ctrl-C:

```sh
ssh -t -L 5000:127.0.0.1:5000 server sudo docker exec -it tally-schedule-1 tally fava
```

Then open http://localhost:5000. Fava has no login, so it listens only on the server's own
loopback, and only while that's open.

## On a schedule

| When | Runs |
|---|---|
| Every 2 hours, at half past | `tally sync` |
| Sundays at 6pm | `tally summary --email --sync` |

A run the server was off for is skipped, and the next one catches up. `sudo docker logs
tally-schedule-1` shows each run and its errors. Their output isn't logged, since it names
merchants and amounts.

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
ssh server 'mkdir -p /tmp/sure' && scp -r deploy/sure/compose.yml deploy/sure/install deploy/sure/brand server:/tmp/sure/
ssh -t server 'sudo sh /tmp/sure/install && rm -r /tmp/sure'
```

[`deploy/sure/install`](../deploy/sure/install) copies the compose file to `/srv/sure`, makes
`/srv/sure/.env` with a random `SECRET_KEY_BASE` and database password, and starts the
containers. If [Tailscale](https://tailscale.com) is installed, it also runs `tailscale serve`,
which puts Sure on your tailnet at `https://<server>.<tailnet>.ts.net`, reachable from your
phone.

The `.env` is made once and never replaced: its `SECRET_KEY_BASE` encrypts what Sure stores.
Open Sure, create your account, and set **Settings > Self-Hosting > Onboarding** to **Closed**
so no one else can register. Then make an API key in Sure's settings with read and write access,
and save it for tally with `tally sure-login`, run as in [Running a command](#running-a-command).

To upgrade Sure, change the image tag in `deploy/sure/compose.yml` and run the two install
commands again. It's branded as tally by the icons in `deploy/sure/brand`.

### Sure's chat

Sure's chat can be answered by tally, so it needs no AI API key: Sure's **External** assistant
posts each conversation to `tally chat` (the chat container), which asks Claude to reply from
the same facts as the monthly review. For single purchases or a merchant, Claude looks things up
in Sure through `tally lookups`, which passes on only Sure's tools that read, so Claude can't
change anything. A reply quoting a dollar amount it wasn't shown is refused. Replies take 10 to
30 seconds.

Sure's install makes the tokens and the relay between them. Once it has run, start the chat:

```sh
sudo docker compose --project-directory /opt/tally up -d
```

Then turn on AI for your user in Sure's settings. [Architecture](architecture.md) has how the
pieces connect.
