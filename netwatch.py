#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Kieran Cox
"""netwatch - ping + bandwidth logger for a flaky home connection (stdlib only).

Commands
  ping     10 pings to 1.1.1.1 (~3 s). Run every minute by a systemd user timer.
  bw       full bandwidth test: 25 MB from Cloudflare, capped at 8 s. A systemd
           user timer runs `bw --if-due` every minute; the interval itself
           (default 30 min, 0 = paused) is the bw_interval_min setting, which
           you change from the tray icon's "Speed test frequency" menu.
  quick    very short check (5 pings + 5 MB capped at 5 s, niced). Safe to run
           whenever; used by the tray icon's "Quick check now".
  status   print the current light colour and the latest numbers.
  report   summarise the log (outages, latency, speed distribution).

Everything lands in ~/.local/share/netwatch/ (samples.jsonl = history,
latest.json = what the tray icon reads). Optional overrides live in
~/.config/netwatch/config.json (same keys as DEFAULTS below).
"""
import argparse
import datetime as dt
import fcntl
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

DATA = Path(os.environ.get("NETWATCH_DATA") or Path.home() / ".local/share/netwatch")
CFG_FILE = Path(os.environ.get("NETWATCH_CONFIG") or Path.home() / ".config/netwatch/config.json")

DEFAULTS = dict(
    ping_host="1.1.1.1",
    ping_count=10, ping_interval=0.3, ping_wait=2,
    bw_url="https://speed.cloudflare.com/__down?bytes={bytes}",
    bw_bytes=25_000_000, bw_max_seconds=8,
    quick_bytes=5_000_000, quick_max_seconds=5,
    quick_ping_count=5, quick_ping_interval=0.25,
    # how often the scheduled full speed test runs (minutes; 0 = paused).
    # Changed from the tray menu; stored in ~/.config/netwatch/config.json.
    bw_interval_min=30,
    # light colours: speed >= green_mbit -> green, < red_mbit -> red (blinking),
    # anything in between -> yellow (this includes the 7-20 Mbit/s band).
    green_mbit=20.0, red_mbit=3.0,
    # ping can only make the light worse, never better
    ping_yellow_ms=150.0, ping_red_ms=400.0,
    loss_yellow_pct=0.0, loss_red_pct=20.0,
    # ignore data that is too old
    ping_stale_s=240, bw_stale_s=5400,
)
ORDER = {"green": 0, "yellow": 1, "red": 2}


def cfg():
    c = dict(DEFAULTS)
    try:
        c.update(json.loads(CFG_FILE.read_text()))
    except FileNotFoundError:
        pass
    except Exception as e:  # bad JSON must not kill the probes
        print(f"netwatch: ignoring bad config {CFG_FILE}: {e}", file=sys.stderr)
    return c


def set_config(key, value):
    """Persist one setting (keeps any other overrides already in the file)."""
    try:
        cur = json.loads(CFG_FILE.read_text())
    except Exception:
        cur = {}
    cur[key] = value
    CFG_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = CFG_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(cur, indent=2) + "\n")
    tmp.replace(CFG_FILE)


def bw_due(c, latest, at=None):
    """Is a scheduled full speed test due? 0 minutes means paused."""
    minutes = c["bw_interval_min"]
    if not minutes or minutes <= 0:
        return False
    last = (latest.get("bw_full") or {}).get("ts")
    at = at if at is not None else time.time()
    return last is None or at - last >= minutes * 60 - 5  # 5 s slack for timer jitter


def now():
    return time.time()


def iso(ts):
    return dt.datetime.fromtimestamp(ts).astimezone().isoformat(timespec="seconds")


# ---------------------------------------------------------------- storage
def load_latest():
    try:
        return json.loads((DATA / "latest.json").read_text())
    except Exception:
        return {}


def record(sample, key, *more_keys):
    """Append to the history and update latest.json (atomically). The sample is
    stored under `key` and any `more_keys` (e.g. a full test is both 'bw' and 'bw_full')."""
    DATA.mkdir(parents=True, exist_ok=True)
    sample = dict(sample, time=iso(sample["ts"]))
    hist = DATA / "samples.jsonl"
    try:
        if hist.stat().st_size > 30_000_000:
            hist.replace(DATA / "samples.old.jsonl")
    except FileNotFoundError:
        pass
    with open(hist, "a") as f:
        f.write(json.dumps(sample) + "\n")
    latest = load_latest()
    for k in (key, *more_keys):
        latest[k] = sample
    tmp = DATA / "latest.json.tmp"
    tmp.write_text(json.dumps(latest))
    tmp.replace(DATA / "latest.json")


class Lock:
    """Non-blocking advisory lock so two tests never overlap."""

    def __init__(self, name):
        DATA.mkdir(parents=True, exist_ok=True)
        self.f = open(DATA / name, "w")
        try:
            fcntl.flock(self.f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.ok = True
        except BlockingIOError:
            self.ok = False

    def release(self):
        self.f.close()


# ----------------------------------------------------------------- probes
def do_ping(host, count, interval, wait):
    t0 = now()
    try:
        p = subprocess.run(
            ["ping", "-4", "-q", "-c", str(count), "-i", str(interval), "-W", str(wait), host],
            capture_output=True, text=True, timeout=count * (interval + wait) + 5)
        out = p.stdout
    except Exception:
        out = ""
    m = re.search(r"(\d+) packets transmitted, (\d+) (?:packets )?received", out)
    sent, recv = (int(m.group(1)), int(m.group(2))) if m else (count, 0)
    s = dict(type="ping", ts=t0, host=host, sent=sent, recv=recv,
             loss=round(100.0 * (sent - recv) / max(sent, 1), 1))
    r = re.search(r"= ([\d.]+)/([\d.]+)/([\d.]+)/([\d.]+) ms", out)
    if r:
        s.update(min=float(r[1]), avg=float(r[2]), max=float(r[3]), jitter=float(r[4]))
    return s


def do_download(c, nbytes, max_s, label):
    """mbit uses curl's speed_download (whole transfer incl. TLS + first byte),
    the same basis as the earlier manual tests; mbit_xfer excludes TTFB."""
    url = c["bw_url"].format(bytes=nbytes)
    fmt = "%{http_code} %{size_download} %{time_total} %{time_starttransfer} %{speed_download}"
    t0 = now()
    try:
        p = subprocess.run(["curl", "-4", "-s", "-o", "/dev/null", "--max-time", str(max_s), "-w", fmt, url],
                           capture_output=True, text=True, timeout=max_s + 5)
        v = p.stdout.split()
        code, size, ttot, tfirst, spd = int(v[0]), int(float(v[1])), float(v[2]), float(v[3]), float(v[4])
    except Exception:
        code, size, ttot, tfirst, spd = 0, 0, float(max_s), 0.0, 0.0
    xfer = max(ttot - tfirst, 0.001)
    return dict(type=label, ts=t0, bytes=size, seconds=round(ttot, 2), ttfb=round(tfirst, 2),
                http=code, complete=size >= nbytes, mbit=round(spd * 8 / 1e6, 2),
                mbit_xfer=round(size * 8 / xfer / 1e6, 2) if size else 0.0)


def default_iface():
    try:
        out = subprocess.run(["ip", "-4", "route", "show", "default"], capture_output=True,
                             text=True, timeout=3).stdout
        m = re.search(r"dev (\S+)", out)
        return m.group(1) if m else None
    except Exception:
        return None


def local_mbit(iface, seconds=1.0):
    """This machine's own traffic, so a slow test can be explained by a busy line."""
    def read():
        try:
            for line in open("/proc/net/dev"):
                name, _, rest = line.partition(":")
                if name.strip() == iface:
                    f = rest.split()
                    return int(f[0]) + int(f[8])
        except Exception:
            pass
        return None
    a = read()
    time.sleep(seconds)
    b = read()
    return round((b - a) * 8 / seconds / 1e6, 2) if a is not None and b is not None else None


# ----------------------------------------------------------- classification
def speed_level(c, mbit):
    if mbit < c["red_mbit"]:
        return "red"
    return "green" if mbit >= c["green_mbit"] else "yellow"


def ping_level(c, p):
    if p["recv"] == 0:
        return "red"
    avg = p.get("avg", 0.0)
    if p["loss"] >= c["loss_red_pct"] or avg >= c["ping_red_ms"]:
        return "red"
    if p["loss"] > c["loss_yellow_pct"] or avg >= c["ping_yellow_ms"]:
        return "yellow"
    return "green"


def classify(c, latest, at=None):
    """-> (state, detail). state in green/yellow/red/unknown. The light is the
    worse of the speed level and the ping level; 'unknown' when the ping probe
    has stopped reporting (so a dead logger never looks like a good line)."""
    at = at if at is not None else now()
    ping, bw = latest.get("ping"), latest.get("bw")
    detail = dict(ping_level=None, speed_level=None, notes=[])
    if not ping or at - ping["ts"] > c["ping_stale_s"]:
        detail["notes"].append("ping probe is not reporting")
        return "unknown", detail
    levels = [ping_level(c, ping)]
    detail["ping_level"] = levels[0]
    # a speed reading stays valid for 2.5 test intervals (never less than bw_stale_s)
    bw_stale = max(c["bw_stale_s"], 2.5 * 60 * (c.get("bw_interval_min") or 0))
    if bw and at - bw["ts"] <= bw_stale:
        detail["speed_level"] = speed_level(c, bw["mbit"])
        levels.append(detail["speed_level"])
    else:
        detail["notes"].append("no recent speed test")
    return max(levels, key=ORDER.get), detail


# ------------------------------------------------------------------ commands
def run_quick(c, notify=False, why="quick"):
    lk = Lock("bw.lock")
    if not lk.ok:
        print("another speed test is already running")
        return None
    try:
        p = do_ping(c["ping_host"], c["quick_ping_count"], c["quick_ping_interval"], c["ping_wait"])
        record(p, "ping")
        d = do_download(c, c["quick_bytes"], c["quick_max_seconds"], "quick")
        d["why"] = why
        record(d, "bw")
    finally:
        lk.release()
    state, detail = classify(c, load_latest())
    ms = f"{p['avg']:.0f} ms" if "avg" in p else "no reply"
    line = f"{d['mbit']:.1f} Mbit/s, ping {ms}, {p['loss']:.0f}% loss -> {state.upper()}"
    print(line)
    if notify:
        subprocess.run(["notify-send", "-u", "low", "-t", "6000", "-i", "network-wired", "Netwatch quick check", line],
                       capture_output=True)
    return state


def cmd_ping(args):
    os.nice(10)
    c = cfg()
    lk = Lock("ping.lock")
    if not lk.ok:
        return 0
    try:
        before, _ = classify(c, load_latest())
        p = do_ping(c["ping_host"], c["ping_count"], c["ping_interval"], c["ping_wait"])
        record(p, "ping")
    finally:
        lk.release()
    # Coming back from an outage: the last speed sample is probably a red one
    # taken while the line was dead, so refresh it instead of waiting up to 30 min.
    bw = load_latest().get("bw")
    if (before == "red" and ping_level(c, p) != "red"
            and (not bw or (speed_level(c, bw["mbit"]) == "red" and now() - bw["ts"] > 60))):
        run_quick(c, why="recovered")
    return 0


def cmd_bw(args):
    os.nice(10)
    c = cfg()
    if args.if_due and not bw_due(c, load_latest()):
        return 0  # the timer fires every minute; the interval setting decides if we run
    lk = Lock("bw.lock")
    if not lk.ok:
        print("another speed test is already running")
        return 0
    try:
        iface = default_iface()
        busy = local_mbit(iface) if iface else None
        d = do_download(c, c["bw_bytes"], c["bw_max_seconds"], "bw")
        d["local_mbit_before"] = busy
        record(d, "bw", "bw_full")
    finally:
        lk.release()
    state, _ = classify(c, load_latest())
    line = f"{d['mbit']:.1f} Mbit/s ({d['bytes'] / 1e6:.1f} MB in {d['seconds']} s) -> {state.upper()}"
    print(line)
    if args.notify:
        subprocess.run(["notify-send", "-u", "low", "-t", "6000", "-i", "network-wired", "Netwatch speed test", line],
                       capture_output=True)
    return 0


def cmd_quick(args):
    os.nice(10)
    run_quick(cfg(), notify=args.notify)
    return 0


def cmd_status(args):
    c = cfg()
    latest = load_latest()
    state, detail = classify(c, latest)
    if args.json:
        print(json.dumps(dict(state=state, detail=detail, latest=latest)))
        return 0
    print(f"state: {state.upper()}  (ping {detail['ping_level']}, speed {detail['speed_level']})")
    for note in detail["notes"]:
        print(f"  note: {note}")
    p, b = latest.get("ping"), latest.get("bw")
    if p:
        ms = f"{p['avg']:.0f} ms avg / {p['max']:.0f} max" if "avg" in p else "no replies"
        print(f"ping  {p['time']}  {ms}, {p['loss']:.0f}% loss")
    if b:
        print(f"speed {b['time']}  {b['mbit']:.1f} Mbit/s ({b['type']}, {b['bytes'] / 1e6:.1f} MB in {b['seconds']} s)")
    return 0


def _pct(vals, q):
    vals = sorted(vals)
    return vals[min(int(q * len(vals)), len(vals) - 1)] if vals else None


def cmd_report(args):
    c = cfg()
    since = now() - args.hours * 3600
    pings, bws = [], []
    try:
        for line in open(DATA / "samples.jsonl"):
            try:
                s = json.loads(line)
            except ValueError:
                continue
            if s.get("ts", 0) < since:
                continue
            if s["type"] == "ping":
                pings.append(s)
            elif s["type"] in ("bw", "quick"):
                bws.append(s)
    except FileNotFoundError:
        print("no samples yet")
        return 0
    print(f"netwatch report: last {args.hours:g} h (since {iso(since)})")
    if pings:
        lat = [p["avg"] for p in pings if "avg" in p]
        dead = [p for p in pings if p["recv"] == 0]
        lossy = [p for p in pings if p["loss"] > 0]
        expected = args.hours * 60
        print(f"\nPing to {c['ping_host']}: {len(pings)} probes ({100 * len(pings) / expected:.0f}% of the expected {expected:.0f})")
        if lat:
            print(f"  latency median {_pct(lat, .5):.0f} ms, p95 {_pct(lat, .95):.0f} ms, worst probe avg {max(lat):.0f} ms")
        print(f"  probes with any loss: {len(lossy)} ({100 * len(lossy) / len(pings):.1f}%); no replies at all: {len(dead)}")
        # outage windows = runs of fully dead probes
        runs, cur = [], []
        for p in sorted(pings, key=lambda x: x["ts"]):
            if p["recv"] == 0 and (not cur or p["ts"] - cur[-1]["ts"] <= 150):
                cur.append(p)
            else:
                if cur:
                    runs.append(cur)
                cur = [p] if p["recv"] == 0 else []
        if cur:
            runs.append(cur)
        if runs:
            print("  outages (no ping replies):")
            for r in runs:
                a, b = r[0]["ts"], r[-1]["ts"] + 60
                print(f"    {dt.datetime.fromtimestamp(a):%a %H:%M:%S} - {dt.datetime.fromtimestamp(b):%H:%M:%S}  (~{(b - a) / 60:.0f} min)")
        else:
            print("  no full outages recorded")
    else:
        print("\nno ping samples in this window")
    full = [b for b in bws if b["type"] == "bw"]
    if bws:
        sp = [b["mbit"] for b in (full or bws)]
        red = sum(1 for v in sp if v < c["red_mbit"])
        grn = sum(1 for v in sp if v >= c["green_mbit"])
        print(f"\nSpeed to Cloudflare ({'full' if full else 'quick'} tests): {len(sp)} samples")
        print(f"  min {min(sp):.1f} / median {_pct(sp, .5):.1f} / max {max(sp):.1f} Mbit/s")
        print(f"  red (<{c['red_mbit']:g}): {red}  yellow: {len(sp) - red - grn}  green (>={c['green_mbit']:g}): {grn}")
        worst = sorted(full or bws, key=lambda b: b["mbit"])[:5]
        print("  slowest:  " + "; ".join(f"{dt.datetime.fromtimestamp(b['ts']):%a %H:%M} {b['mbit']:.1f}" for b in worst))
    else:
        print("\nno speed samples in this window")
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("ping").set_defaults(fn=cmd_ping)
    p = sub.add_parser("bw")
    p.add_argument("--notify", action="store_true")
    p.add_argument("--if-due", action="store_true",
                   help="only run if bw_interval_min has elapsed since the last full test (used by the timer)")
    p.set_defaults(fn=cmd_bw)
    p = sub.add_parser("quick")
    p.add_argument("--notify", action="store_true")
    p.set_defaults(fn=cmd_quick)
    p = sub.add_parser("status")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_status)
    p = sub.add_parser("report")
    p.add_argument("--hours", type=float, default=24)
    p.set_defaults(fn=cmd_report)
    args = ap.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
