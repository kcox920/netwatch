# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Kieran Cox
"""Read-only client for the AT&T Internet Air gateway's built-in web API.

Used by netwatch to log the cellular signal (RSRP / RSRQ / SINR, serving cell,
neighbours) so dropouts and slow periods can be lined up with radio conditions.

Unofficial: it speaks the same REST API the gateway's own web page uses
(tested against the Nov 2026 firmware of the AT&T Internet Air gateway). It only
ever reads status. Credentials live in ~/.config/netwatch/gateway.json (mode
0600), never in this repository:

    {"host": "192.168.1.254", "login": "admin", "code": "<Device Access Code>"}
"""
import hashlib
import html
import http.cookiejar
import json
import os
import random
import ssl
import stat
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

CRED_FILE = Path(os.environ.get("NETWATCH_GATEWAY") or Path.home() / ".config/netwatch/gateway.json")

_B64 = "./0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
_ORDER = [(0, 21, 42), (22, 43, 1), (44, 2, 23), (3, 24, 45), (25, 46, 4), (47, 5, 26), (6, 27, 48),
          (28, 49, 7), (50, 8, 29), (9, 30, 51), (31, 52, 10), (53, 11, 32), (12, 33, 54), (34, 55, 13),
          (56, 14, 35), (15, 36, 57), (37, 58, 16), (59, 17, 38), (18, 39, 60), (40, 61, 19), (62, 20, 41)]


def _b64(b2, b1, b0, n):
    w = (b2 << 16) | (b1 << 8) | b0
    out = ""
    for _ in range(n):
        out += _B64[w & 0x3F]
        w >>= 6
    return out


def sha512_crypt(password, salt, rounds=5000):
    """glibc-compatible '$6$' password hash (pure Python; the stdlib `crypt`
    module is gone in Python 3.13+). Returns '$6$<salt>$<hash>'."""
    pw = password.encode() if isinstance(password, str) else password
    sb = salt.encode()[:16]
    sha = hashlib.sha512
    b = sha(pw + sb + pw).digest()
    ctx = sha(pw + sb)
    n = len(pw)
    while n > 64:
        ctx.update(b)
        n -= 64
    ctx.update(b[:n])
    n = len(pw)
    while n > 0:
        ctx.update(b if n & 1 else pw)
        n >>= 1
    a = ctx.digest()
    dp = sha(pw * len(pw)).digest()
    p = (dp * (len(pw) // 64 + 1))[:len(pw)]
    ds = sha(sb * (16 + a[0])).digest()
    s = (ds * (len(sb) // 64 + 1))[:len(sb)]
    c = a
    for i in range(rounds):
        h = sha()
        h.update(p if i & 1 else c)
        if i % 3:
            h.update(s)
        if i % 7:
            h.update(p)
        h.update(c if i & 1 else p)
        c = h.digest()
    out = "".join(_b64(c[x], c[y], c[z], 4) for x, y, z in _ORDER) + _b64(0, 0, c[63], 2)
    return f"$6${sb.decode()}${out}"


class GatewayError(Exception):
    pass


def load_credentials():
    try:
        st = CRED_FILE.stat()
    except FileNotFoundError:
        return None
    if st.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
        print(f"netwatch: warning: {CRED_FILE} is readable by other users; run: chmod 600 {CRED_FILE}", file=sys.stderr)
    c = json.loads(CRED_FILE.read_text())
    c.setdefault("host", "192.168.1.254")
    c.setdefault("login", "admin")
    if not c.get("code"):
        raise GatewayError(f"{CRED_FILE} has no 'code'")
    return c


class Gateway:
    def __init__(self, host, login, code, timeout=10):
        self.base, self.user, self.code, self.timeout = f"https://{host}", login, code, timeout
        ctx = ssl.create_default_context()
        ctx.check_hostname = False          # the gateway uses a self-signed certificate
        ctx.verify_mode = ssl.CERT_NONE
        self.jar = http.cookiejar.CookieJar()
        self.op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar),
                                              urllib.request.HTTPSHandler(context=ctx))
        self.logged_in = False

    def _cookie(self, name):
        return next((c.value for c in self.jar if c.name == name), None)

    def _req(self, path, data=None):
        d = urllib.parse.urlencode(data).encode() if data is not None else None
        r = urllib.request.Request(self.base + path, data=d)
        if d is not None:
            r.add_header("Content-Type", "application/x-www-form-urlencoded")
        csrf = self._cookie("__Host-csrf_token")
        if csrf:
            r.add_header("X-CSRF-Token", csrf)   # required on every call after login
        r.add_header("Referer", self.base + "/")
        try:
            with self.op.open(r, timeout=self.timeout) as resp:
                return resp.status, resp.read().decode("utf8", "replace")
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf8", "replace")
        except (urllib.error.URLError, OSError) as e:
            raise GatewayError(f"cannot reach gateway: {e}") from e

    def login(self):
        sha = lambda s: hashlib.sha512(s.encode()).hexdigest()
        st, _ = self._req("/api/v1/login-params", {"login": self.user})
        salt, nonce = self._cookie("salt"), self._cookie("nonce")
        if st >= 300 or not salt or not nonce:
            raise GatewayError(f"login-params failed (HTTP {st})")
        kn = sha512_crypt(self.code, salt)
        nn = sha(f"{self.user}:{nonce}:{kn[3:]}")
        cnonce = str(random.randrange(10 ** 18)).zfill(19)
        st, body = self._req("/api/v1/login", {"login": self.user, "auth_key": sha(f"{nn}:0:{cnonce}"),
                                               "cnonce": cnonce})
        if st >= 300:
            raise GatewayError(f"login rejected (HTTP {st}) - wrong access code?")
        self.logged_in = True

    def get(self, path):
        st, body = self._req(path)
        if st == 401:
            self.logged_in = False
            raise GatewayError("session expired")
        if st >= 300:
            raise GatewayError(f"GET {path}: HTTP {st}")
        return json.loads(body)

    def logout(self):
        if self.logged_in:
            try:
                self._req("/api/v1/logout", {})
            except GatewayError:
                pass
            self.logged_in = False


# ------------------------------------------------------------------ parsing
def _num(v, scale=1.0):
    try:
        return float(v) / scale
    except (TypeError, ValueError):
        return None


def _first_iface(doc):
    try:
        return doc[0]["cellular"]["interfaces"][0]
    except (KeyError, IndexError, TypeError):
        return {}


def read_signal(gw):
    """One reading of the radio state. Needs gw.login() first."""
    main = _first_iface(gw.get("/api/v2/cellular/interface"))
    nr5 = _first_iface(gw.get("/api/v1/cellular/interface_5g"))
    try:
        nh = gw.get("/api/v1/cellular/nhcells")[0].get("lte_cells", [])
    except (GatewayError, IndexError, KeyError, TypeError):
        nh = []
    lte, nrc = main.get("lte_cell", {}), main.get("5g_cell", {})
    ci = nr5.get("connect_info", {})
    cellid = int(lte["cellid"]) if str(lte.get("cellid", "")).isdigit() else None
    return dict(
        type="signal",
        status=main.get("status"), tech=main.get("current_access_technology"),
        network=html.unescape(main.get("network_in_use", "")), plmn=main.get("plmn"),
        attach=main.get("attach_status"),
        lte=dict(rsrp=_num(lte.get("rsrp")), rsrq=_num(lte.get("rsrq")), sinr=_num(lte.get("sinr")),
                 rssi=_num(lte.get("rssi")), band=lte.get("bandinfo"),
                 bw_mhz=_num(lte.get("di_bandwidth"), 1e6), pci=_num(lte.get("pci")),
                 cellid=cellid, enb=(cellid >> 8) if cellid else None, sector=(cellid & 255) if cellid else None,
                 ca_cells=len(main.get("lte_ca_cells") or [])),
        nr=dict(rsrp=_num(nr5.get("rsrp", nrc.get("rsrp"))), rsrq=_num(nr5.get("rsrq", nrc.get("rsrq"))),
                sinr=_num(ci.get("sinr", nrc.get("sinr"))), band=nr5.get("bandinfo") or nrc.get("bandinfo"),
                bw_mhz=_num(ci.get("di_bandwidth", nrc.get("di_bandwidth")), 1e6),
                pci=_num(ci.get("pci", nrc.get("pci")))),
        neighbours=[dict(pci=c.get("phycellid"), rsrp=c.get("rsrp"), rsrq=c.get("rsrq")) for c in nh],
    )


# ------------------------------------------------------------ interpretation
def _label(v, steps):
    """steps: [(threshold, label), ...] best first; below the last -> 'bad'."""
    if v is None:
        return "n/a"
    for t, name in steps:
        if v >= t:
            return name
    return "bad"


RSRP_STEPS = [(-80, "excellent"), (-90, "good"), (-100, "fair"), (-110, "poor")]
RSRQ_STEPS = [(-9, "excellent"), (-12, "good"), (-15, "fair"), (-20, "poor")]
SINR_STEPS = [(20, "excellent"), (13, "good"), (6, "fair"), (0, "poor")]


def labels(sig):
    return dict(rsrp=_label(sig["lte"]["rsrp"], RSRP_STEPS), rsrq=_label(sig["lte"]["rsrq"], RSRQ_STEPS),
                sinr=_label(sig["lte"]["sinr"], SINR_STEPS), nr_sinr=_label(sig["nr"]["sinr"], SINR_STEPS))


def _norm(v, lo, hi):
    return None if v is None else max(0.0, min(1.0, (v - lo) / (hi - lo)))


def score(sig):
    """0-100 placement score: mean of the available normalised readings.
    Only meant for comparing positions of the same gateway, not as an absolute grade."""
    parts = [_norm(sig["lte"]["rsrp"], -125, -75), _norm(sig["lte"]["rsrq"], -20, -6),
             _norm(sig["lte"]["sinr"], -5, 25), _norm(sig["nr"]["sinr"], -5, 25)]
    parts = [p for p in parts if p is not None]
    return round(100 * sum(parts) / len(parts)) if parts else None


def summary_line(sig):
    l, n = sig["lte"], sig["nr"]
    f = lambda v, u="": "n/a" if v is None else f"{v:g}{u}"
    return (f"LTE rsrp {f(l['rsrp'])} rsrq {f(l['rsrq'])} sinr {f(l['sinr'])} | "
            f"NR sinr {f(n['sinr'])} rsrp {f(n['rsrp'])} | PCI {f(l['pci'])} | score {score(sig)}")
