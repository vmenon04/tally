# Running tally on a server

tally runs as its own `tally` system user (no login, no sudo, no docker), laid out as:

| Path | What |
|---|---|
| `/srv/tally/app/` | The code and its `.venv` (replaceable) |
| `/srv/tally/data/` | `TALLY_DATA_DIR`: database, `config.toml`, `rules.toml`, alert emails |
| `/srv/tally/secrets/` | `TALLY_SECRETS_DIR` |
| `/srv/tally/bin/uv` | uv, pinned |

`/srv/tally` and everything in it is owner-only. `tally-alerts.timer` runs `tally alerts --gmail`
every 5 minutes through `tally-alerts.service`, which can write only to `/srv/tally/data`.
Install or update them, from a copy of the repo on the server, with:

```sh
sudo install -m 644 deploy/tally-alerts.service deploy/tally-alerts.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now tally-alerts.timer
```

`systemctl list-timers tally-alerts` shows the next run; `journalctl -u tally-alerts` shows
errors (the run's report itself isn't logged, since it names merchants and amounts).
