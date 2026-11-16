# netwatch

A small connection monitor for flaky home internet: it logs ping and download
speed over time and shows the result as a coloured light in the GNOME top bar.

- **Green** - Cloudflare download >= 20 Mbit/s and ping healthy
- **Yellow** - 3-20 Mbit/s, or ping degraded
- **Red, blinking** - under 3 Mbit/s, or ping dead / very slow
- **Grey** - the logger has stopped reporting (so a dead logger never looks like a good line)

The light is the worse of the speed level and the ping level. Ping can only
make it worse, never better.

It was built to keep timestamped evidence for an ISP support call: every probe
is appended to a JSON-lines history, and `netwatch report` lists outages and
the speed distribution.

## How it checks

| Check | What | How often |
|---|---|---|
| Ping | 10 pings to 1.1.1.1 (about 3 s) | every minute |
| Full speed test | 25 MB from `speed.cloudflare.com`, capped at 8 s | every 30 min (change it from the tray menu) |
| Quick check | 5 pings + 5 MB capped at 5 s, low priority | on demand, about 2 s |

Ping runs often because it is nearly free and catches dropouts and latency
spikes. The speed test catches caps and slow periods that ping cannot see.
When ping recovers from an outage, a quick speed sample is taken automatically
so the light does not stay red on a stale reading.

The only network traffic is those pings and downloads. Nothing is uploaded and
no data leaves your machine.

## Requirements

- Linux with GNOME and a tray (AppIndicator) provider. Ubuntu ships the
  `ubuntu-appindicators` extension; elsewhere install the
  "AppIndicator and KStatusNotifierItem Support" extension.
- Python 3.8+ with PyGObject and the Ayatana (or classic) AppIndicator typelib:
  `sudo apt install python3-gi gir1.2-ayatanaappindicator3-0.1`
- `curl`, `ping`, and systemd (user units) for scheduling. `notify-send` is optional.

No Python packages are needed beyond the standard library.

## Install

```sh
git clone https://github.com/kcox920/netwatch.git ~/dev/netwatch
cd ~/dev/netwatch
./install.sh
setsid python3 netwatch_indicator.py >/dev/null 2>&1 &   # start the light now; it autostarts at login
ln -s "$PWD/netwatch.py" ~/.local/bin/netwatch            # optional: a `netwatch` command
```

`./install.sh --uninstall` removes the timers and autostart entry (the history in
`~/.local/share/netwatch` is kept).

## Using it

Click the light for the latest numbers and a menu:

- **Quick check now** and **Full speed test now**
- **Speed test frequency** - every 5/10/15/30 min, hourly, 2 h, 6 h, **Custom...** or **Paused**.
  Takes effect immediately and is remembered.
- **24 h report...** and **Open data folder**

From a terminal:

```sh
netwatch status              # current colour and latest numbers
netwatch quick               # 2-second check
netwatch report --hours 24   # outages, latency percentiles, speed distribution
netwatch bw                  # run a full speed test now
netwatch signal --watch      # live radio signal from the AT&T gateway (see below)
```

## Configuration

Optional overrides go in `~/.config/netwatch/config.json`; any key from
`DEFAULTS` in `netwatch.py` works, for example:

```json
{ "bw_interval_min": 15, "green_mbit": 50, "red_mbit": 5, "ping_host": "9.9.9.9" }
```

`bw_interval_min` is the one the tray menu edits (`0` = paused). Colour
thresholds and the ping rules are also there. Speeds between `red_mbit` and
`green_mbit` are yellow.

## Gateway signal logging (AT&T Internet Air, optional)

With the gateway's Device Access Code, netwatch can also log the cellular radio
signal (LTE and 5G RSRP / RSRQ / SINR, serving cell, neighbour cells) once a
minute, so dropouts and slow periods can be lined up with radio conditions.
It only reads status from the gateway's own web API (the one its web page uses;
unofficial, tested on the Nov 2026 firmware) and never changes anything.

Create `~/.config/netwatch/gateway.json` **outside this repo** and lock it down:

```sh
umask 077
cat > ~/.config/netwatch/gateway.json <<'EOF'
{ "host": "192.168.1.254", "login": "admin", "code": "YOUR-DEVICE-ACCESS-CODE" }
EOF
```

Then:

```sh
netwatch signal            # one reading, with a quality label for each number
netwatch signal --watch    # live feed + a 0-100 placement score, for finding the best spot for the gateway
netwatch report            # now also summarises signal and shows it just before each outage
```

The score is only for comparing positions of the same gateway (it averages the
normalised RSRP, RSRQ and SINR readings); it is not an absolute grade. Logging is
skipped silently if the file is missing, and a gateway error never interrupts the
ping probe. Set `"signal_log": false` in `config.json` to turn it off.

## Notes and limitations

- The quick check reads lower than the full test on a fast line (a 5 MB download
  never reaches full speed), so compare like with like. Near the green
  threshold the quick check can show yellow when the full test would say green.
- Results are for this machine to Cloudflare and 1.1.1.1. They say how your
  connection is doing, not why.
- Tests are for the colour rules: `python3 -m unittest -v test_netwatch`.

## License

GPL-3.0-or-later. See [LICENSE](LICENSE).

## Files

- `netwatch.py` - probes, logging, `status`/`report`/`signal`, colour rules
- `attgw.py` - read-only client for the AT&T Internet Air gateway (signal logging)
- `netwatch_indicator.py` - the tray light and menu
- `systemd/` - user timers (ping every minute; speed test checked every minute and run when due)
- `icons/` - the five light icons
- `install.sh` - installs the timers and the autostart entry
