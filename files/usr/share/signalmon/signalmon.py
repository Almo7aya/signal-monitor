"""signal-monitor: live LTE signal monitor for two 4G routers, served by the OpenWrt gateway itself.

    /etc/init.d/signalmon start      (or the toggle in LuCI: Services > Signal Monitor)
    -> http://<listen>:<port>/       (default: the gateway's LAN address, port 8765)

Settings come from UCI (/etc/config/signalmon); command-line flags override them for testing.

Old router (Tozed / ZTE ZX297520V3, telnet without login):
    every `poll` s (default 2): SINR/RSRP/RSRQ + per-port RSRP/RSSI from nv (refreshed by the firmware every ~3 s),
    plus ONE AT command per poll when `at` is on. 4 AT commands/s, and later 1 poll/s with AT, rebooted it.
New router (SIMCom SIM7906E, telnet login):
    every `poll` s: AT+CLTEINFO? -> 4 antenna chains RSRP/RSRQ/RSSI/SINR, MIMO, rank, MCS, CA
    every `neighbour_every` s: AT$QCRSRP? -> neighbour cells
Gateway (local): per-WAN bandwidth from /sys/class/net, mwan3 state (ubus mwan3 status), speed tests through
`mwan3 use`.

Page (index.html): live values, per-antenna bars, 5-min charts, "measure spot" (15 s average of both routers),
speed tests per router, snapshots (PNG + JSON) with deltas against a chosen reference, optional tones.
Data: snapshots, speedtests.jsonl, spot/speed CSV in data_dir (persistent); per-poll CSV logs in log_dir (tmpfs).
Auto-off: the init script writes /var/run/signalmon/off_at; when it passes, this server disables the service.
"""
import argparse
import base64
import csv
import json
import os
import re
import shlex
import signal
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from tn import Telnet  # noqa: E402

PAGE_FILE = os.path.join(HERE, "index.html")
RUN_DIR = "/var/run/signalmon"
OFF_AT_FILE = os.path.join(RUN_DIR, "off_at")
STARTED = time.strftime("%Y%m%d_%H%M")

# set from UCI in main()
LISTEN, PORT = "", 8765
DATA_DIR, LOG_DIR = "/root/signalmon", "/tmp/signalmon"
OLD_POLL = 2.0          # s, old router poll interval (nv values; firmware refreshes them every ~3 s)
OLD_AT = True           # old router: one AT command per poll (alternating AT+CESQ / AT+ZRSSI) for live values
NEW_POLL = 1.0          # s, new router AT+CLTEINFO?
NEIGHBOUR_EVERY = 30.0  # s, new router AT$QCRSRP? neighbour cells
SPOT_SECONDS = 15
SPEED_URL = "https://speed.cloudflare.com/__down?bytes=50000000"
SPEED_SECONDS = 10
SPOTS_CSV = SPEED_CSV = SPEED_JSONL = SNAP_DIR = ""

# mwan3 interfaces served by each router (UCI old.wans / new.wans) and the one pinged during its speed test
OLD_WANS_L, OLD_PING = ["wan2", "wan3", "wan4"], "wan3"
NEW_WANS_L, NEW_PING = ["wan1"], "wan1"
# test group -> (WANs to download on, WANs whose device is pinged for latency under load; the first also clocks sampling)
SPEED_GROUPS = {}
OLD_WANS = set()

# Runs locally. BusyBox has no fractional sleep, so `ping -O -i 0.5` is the 0.5 s clock: every tick
# samples rx_bytes of the tested WAN devices and reports that tick's RTT (or "-" when no reply yet).
SPEED_SCRIPT = r"""DEVS="@DEVS@"
sample() { s="S $(cut -d' ' -f1 /proc/uptime) $1 $2"; for d in $DEVS; do s="$s $(cat /sys/class/net/$d/statistics/rx_bytes)"; done; echo "$s"; }
bg() { s="B $(cut -d' ' -f1 /proc/uptime)"; for d in $DEVS; do s="$s $(cat /sys/class/net/$d/statistics/rx_bytes)"; done; echo "$s"; }
bg
for p in @PINGS@; do ping -I $p -c 3 -i 0.3 -W 2 8.8.8.8 2>/dev/null | while read l; do case "$l" in *time=*) t=${l##*time=}; echo "I $p ${t%% *}";; esac; done; done
bg
sample @PING0@ -
echo GO
for w in @WANS@; do (mwan3 use $w curl -s -o /dev/null -m @T@ -w "R $w %{speed_download} %{size_download}\n" '@URL@' 2>/dev/null | grep '^R ') & done
@EXTRA_PINGS@
ping -O -I @PING0@ -c @N@ -i 0.5 -W 1 8.8.8.8 2>/dev/null | while read l; do
	case "$l" in *icmp_seq=*) ;; *) continue;; esac
	r=-; case "$l" in *time=*) r=${l##*time=}; r=${r%% *};; esac
	sample @PING0@ $r
done
wait
echo END
"""
EXTRA_PING = (r"""(ping -I @P@ -c @N@ -i 0.5 -W 1 8.8.8.8 2>/dev/null | while read l; do """
              r"""case "$l" in *time=*) t=${l##*time=}; echo "P @P@ ${t%% *}";; esac; done) &""")

BW_WANS = []   # the new router's WANs, then the old router's (set in configure)
BW_KEEP = 330  # seconds of 1 s samples kept per WAN

lock = threading.Lock()
speed_proc = None  # running speed-test shell, killed on shutdown


def num(s):
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


def mean(vals):
    vals = [v for v in vals if v is not None]
    return round(sum(vals) / len(vals), 1) if vals else None


def ints(s):
    return [num(x) for x in s.split(",")] if s else []


def append_csv(path, row):
    new = not os.path.exists(path)
    with open(path, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(row.keys()))
        if new:
            w.writeheader()
        w.writerow(row)


def earfcn_band(e):
    for lo, hi, b in ((0, 599, 1), (1200, 1949, 3), (2750, 3449, 7), (3450, 3799, 8), (6150, 6449, 20),
                      (9210, 9659, 28), (37750, 38249, 38), (38650, 39649, 40), (39650, 41589, 41)):
        if e is not None and lo <= e <= hi:
            return str(b)
    return None


BW = {"0": "1.4MHz", "1": "3MHz", "2": "5MHz", "3": "10MHz", "4": "15MHz", "5": "20MHz"}


# ---- gateway helpers (all local) ----

def load_uci(pkg):
    """`uci show pkg` -> {section: {option: value | [values]}}"""
    try:
        out = subprocess.run(["uci", "-q", "show", pkg], capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return {}
    conf = {}
    for line in out.splitlines():
        key, sep, val = line.partition("=")
        parts = key.split(".")
        if not sep or len(parts) != 3:
            continue
        try:
            vals = shlex.split(val)
        except ValueError:
            continue
        conf.setdefault(parts[1], {})[parts[2]] = vals[0] if len(vals) == 1 else vals
    return conf


def ubus_json(obj, method, args=None, timeout=8):
    """-> (exit code, parsed JSON or None). ubus exits 4 when the object does not exist."""
    cmd = ["ubus", "call", obj, method] + ([json.dumps(args)] if args else [])
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return -1, None
    try:
        return r.returncode, json.loads(r.stdout) if r.returncode == 0 and r.stdout.strip() else None
    except ValueError:
        return r.returncode, None


_devs = {}


def wan_info(wan):
    """(Linux device, gateway) of a WAN interface (e.g. wan1 -> veth1, 192.168.8.1), re-resolved every minute"""
    hit = _devs.get(wan)
    if hit and time.time() - hit[2] < 60:
        return hit[0], hit[1]
    _, st = ubus_json(f"network.interface.{wan}", "status", timeout=5)
    st = st or {}
    dev = st.get("l3_device") or st.get("device")
    gw = next((r.get("nexthop") for r in st.get("route") or [] if r.get("target") == "0.0.0.0"), None)
    _devs[wan] = (dev, gw, time.time())
    return dev, gw


def wan_dev(wan):
    return wan_info(wan)[0]


def read_int(path):
    try:
        with open(path) as f:
            return int(f.read().strip())
    except (OSError, ValueError):
        return None


class Router:
    id = name = ""
    wans = []

    def __init__(self):
        self.t = None
        self.cur = {}
        self.history = []
        self.status = "connecting…"
        self.first_pci = None
        self.best = None
        self.fails = 0
        self.log_csv = os.path.join(LOG_DIR, f"signal_log_{self.id}_{STARTED}.csv")

    def push(self):
        s = dict(self.cur, t=time.time())
        self.history.append(s)
        del self.history[:-1500]
        if self.first_pci is None and s.get("pci"):
            self.first_pci = s["pci"]
        if s.get("sinr") is not None and (self.best is None or s["sinr"] > self.best):
            self.best = s["sinr"]
        self.status = "live"
        self.fails = 0
        row = {"time": time.strftime("%H:%M:%S"), "sinr": s.get("sinr"), "rsrp": s.get("rsrp"), "rsrq": s.get("rsrq"),
               "pci": s.get("pci"), "band": s.get("band")}
        for i, c in enumerate(s.get("chains", []), 1):
            row[f"ch{i}_rsrp"], row[f"ch{i}_rssi"], row[f"ch{i}_sinr"] = c.get("rsrp"), c.get("rssi"), c.get("sinr")
        try:
            append_csv(self.log_csv, row)
        except OSError:
            pass

    def nodata(self):
        self.status = "router answered but no values"
        self.fails += 1
        if self.fails > 5:
            self.fails = 0
            self.t = None

    def run(self):
        while True:
            try:
                if self.t is None:
                    self.connect()
                self.poll()
            except Exception as e:  # keep polling whatever happens; reconnect next round
                with lock:
                    self.status = f"error: {e}"
                self.t = None
                time.sleep(2)

    def snapshot(self, now):
        recent = [h for h in self.history if now - h["t"] <= 10]
        latest = self.history[-1] if self.history else None
        return {
            "id": self.id, "name": self.name, "wans": self.wans, "status": self.status, "latest": latest,
            "history": [[round(h["t"], 2), h.get("sinr"), h.get("rsrp")] for h in self.history[-700:]],
            "avg10": {k: mean(h.get(k) for h in recent) for k in ("sinr", "rsrp", "rsrq")} if recent else None,
            "best": self.best,
            "cell_changed": bool(latest and self.first_pci and latest.get("pci") != self.first_pci),
            "first_pci": self.first_pci,
        }

    def window_avg(self, start, end):
        win = [h for h in self.history if start <= h["t"] <= end]
        out = {"n": len(win), "sinr": mean(h.get("sinr") for h in win), "rsrp": mean(h.get("rsrp") for h in win),
               "rsrq": mean(h.get("rsrq") for h in win), "pci": win[-1].get("pci") if win else None,
               "band": win[-1].get("band") if win else None}
        nch = max((len(h.get("chains", [])) for h in win), default=0)
        out["chains"] = [mean(h["chains"][i].get("rsrp") if i < len(h.get("chains", [])) else None for h in win)
                         for i in range(nch)]
        out["chains_rssi"] = [mean(h["chains"][i].get("rssi") if i < len(h.get("chains", [])) else None for h in win)
                              for i in range(nch)]
        return out


class OldRouter(Router):
    """Reads the values the firmware keeps in nv (it refreshes them every ~3 s), plus, when OLD_AT is on,
    ONE AT command per poll (alternating AT+CESQ / AT+ZRSSI) for live RSRP/RSRQ and per-port RSSI.

    4 AT commands per second made this weak router reboot, and so did 1 poll/s with AT: keep it at >= 2 s.
    """
    id, name, wans = "old", "Old router", OLD_WANS_L
    HOST = ("192.168.1.1", 23)
    KEYS = ["lte_sinr", "lte_rsrp", "lte_rsrq", "lte_rssi1", "lte_rssi2", "main_antenna_rsrp", "sub_antenna_rsrp",
            "lte_pci", "lte_band", "cell_id", "BandWidth"]
    CMD = 'echo "S""|' + "|".join(f"$(nv get {k})" for k in KEYS) + '|END"'
    AT = ["echo \"A\"\"|$(at_cmd AT+CESQ | grep -o 'CESQ: [-0-9,]*')|END\"",
          "echo \"A\"\"|$(at_cmd AT+ZRSSI | grep -o 'ZRSSI: [-0-9,]*')|END\""]

    def connect(self):
        self.t = Telnet(*self.HOST)
        self.t.read(wait=1.5, max_wait=5)
        self.sinr_t = None
        self.n = 0
        self.live = {}  # AT-derived values: key -> (value, time)

    def live_or(self, key, fallback):
        v = self.live.get(key)
        return v[0] if v and time.time() - v[1] < max(3.0, OLD_POLL * 3) else fallback

    def cmd(self, c, tag):
        self.t.write(c)
        out = self.t.read(wait=0.4, until=tag + r"\|[^\r\n]*\|END", max_wait=5)
        return next((ln.strip()[len(tag) + 1:-4] for ln in out.splitlines()
                     if ln.strip().startswith(tag + "|") and ln.strip().endswith("|END")), None)

    def poll(self):
        t0 = time.time()
        line = self.cmd(self.CMD, "S")
        at = self.cmd(self.AT[self.n % 2], "A") if OLD_AT else None
        self.n += 1
        with lock:
            c = self.cur
            now = time.time()
            if at and at.startswith("CESQ: "):
                ce = ints(at[6:])
                if len(ce) == 6:
                    if ce[4] not in (None, 255):
                        self.live["rsrq"] = (-20 + ce[4] * 0.5, now)
                    if ce[5] not in (None, 255):
                        self.live["rsrp"] = (-141 + ce[5], now)
            elif at and at.startswith("ZRSSI: "):
                z = ints(at[7:])
                if len(z) == 4 and None not in z:
                    # "-52,3,-55,8" = -52.3 / -55.8 dBm (integer, tenth)
                    self.live["rssi1"] = (z[0] - z[1] / 10 if z[0] < 0 else z[0] + z[1] / 10, now)
                    self.live["rssi2"] = (z[2] - z[3] / 10 if z[2] < 0 else z[2] + z[3] / 10, now)
            if line:
                v = dict(zip(self.KEYS, line.split("|")))
                sinr = num(v.get("lte_sinr"))
                if sinr != c.get("sinr"):
                    self.sinr_t = time.time()
                c["sinr"] = sinr
                c["rsrp"] = self.live_or("rsrp", num(v.get("lte_rsrp")))
                c["rsrq"] = self.live_or("rsrq", num(v.get("lte_rsrq")))
                c["rssi1"] = self.live_or("rssi1", num(v.get("lte_rssi1")))
                c["rssi2"] = self.live_or("rssi2", num(v.get("lte_rssi2")))
                main, sub = num(v.get("main_antenna_rsrp")), num(v.get("sub_antenna_rsrp"))
                # per-port RSRP is stored as value+140 (59 -> -81 dBm)
                c["p1_rsrp"], c["p2_rsrp"] = (main - 140 if main else None), (sub - 140 if sub else None)
                c["pci"], c["band"] = v.get("lte_pci") or None, v.get("lte_band") or None
                c["cell"], c["bw"] = v.get("cell_id") or None, v.get("BandWidth") or None
                c["sinr_age"] = round(time.time() - self.sinr_t, 1) if self.sinr_t else None
                c["chains"] = [{"name": "Port 1 · main", "rsrp": c.get("p1_rsrp"), "rssi": c.get("rssi1")},
                               {"name": "Port 2 · aux", "rsrp": c.get("p2_rsrp"), "rssi": c.get("rssi2")}]
                self.push()
            else:
                self.nodata()
        time.sleep(max(0.05, OLD_POLL - (time.time() - t0)))


class NewRouter(Router):
    id, name, wans = "new", "New router", NEW_WANS_L
    HOST, USER, PASS = ("192.168.0.1", 4719), "superadmin", ""

    def connect(self):
        self.t = Telnet(*self.HOST)
        self.t.read(wait=2, until=r"Login:", max_wait=8)
        self.t.write(self.USER)
        self.t.read(wait=2, until=r"Password:", max_wait=8)
        self.t.write(self.PASS)
        self.t.read(wait=2, until=r"# ?$", max_wait=10)
        self.n = 0

    def at(self, c):
        self.t.write(f"atcmd '{c}'")
        out = self.t.read(wait=0.4, until=r"\n(OK|ERROR)\s", max_wait=5)
        return out.replace("\r", "").replace("\n", "")

    def poll(self):
        t0 = time.time()
        txt = self.at("AT+CLTEINFO?")
        nb = self.at("AT$QCRSRP?") if self.n % max(1, round(NEIGHBOUR_EVERY / NEW_POLL)) == 0 else None
        self.n += 1
        with lock:
            c = self.cur
            m = re.search(r"\+PCC: ([-\d]+),([-\d]+),([-\d]+),([-\d]+),([-\d]+),([-\d]+),([-\d]+),([-\d]+),([-\d]+)", txt)
            if m:
                g = m.groups()
                c["earfcn"] = num(g[2])
                c["band"] = earfcn_band(c["earfcn"])
                c["bw"], c["cell"], c["pci"] = BW.get(g[5], g[5]), g[7], g[8]
            m = re.search(r"PCC RSRP: ([-\d,]+?),RSRQ: ([-\d,]+?),RSSI: ([-\d,]+?),SINR: ([-\d,]+?),"
                          r"CINR: ([-\d,]+?),DL_RANK: (\d+)", txt)
            if m:
                rsrp, rsrq, rssi, sinr = (ints(x) for x in m.groups()[:4])
                c["chains"] = [{"name": f"Ant {i + 1}", "rsrp": rsrp[i] if i < len(rsrp) else None,
                                "rssi": rssi[i] if i < len(rssi) else None, "sinr": sinr[i] if i < len(sinr) else None,
                                "rsrq": rsrq[i] if i < len(rsrq) else None} for i in range(max(len(rsrp), 1))]
                c["rsrp"], c["rsrq"], c["sinr"] = mean(rsrp), mean(rsrq), mean(sinr)
                c["dl_rank"] = m.group(6)
            for key, pat in (("mimo", r"MIMO: (\d+)"), ("dl_mcs", r"DL_MCS: (\d+)"), ("cqi", r"CQI: (\d+)"),
                             ("rrc", r"RRC STATE: (\d+)")):
                mm = re.search(pat, txt)
                c[key] = mm.group(1) if mm else None
            scc = re.findall(r"\+SCC\d*: ([^+]*)", txt)
            c["ca"] = [s.strip().rstrip(",") for s in scc if s.strip()] or None
            if nb:
                mm = re.search(r"\$QCRSRP: (.*?)OK", nb)
                if mm:
                    parts = [p.strip().strip('"') for p in mm.group(1).split(",")]
                    c["neighbours"] = [{"pci": str(int(parts[i])), "earfcn": parts[i + 1],
                                        "band": earfcn_band(num(parts[i + 1])), "rsrp": num(parts[i + 2])}
                                       for i in range(0, len(parts) - 2, 3) if parts[i].strip().isdigit()]
            if m:
                self.push()
            else:
                self.nodata()
        time.sleep(max(0.05, NEW_POLL - (time.time() - t0)))


def load_snapshots():
    os.makedirs(SNAP_DIR, exist_ok=True)
    snaps = []
    for f in sorted(os.listdir(SNAP_DIR)):
        if f.endswith(".json"):
            try:
                with open(os.path.join(SNAP_DIR, f), encoding="utf-8") as fh:
                    d = json.load(fh)
                snaps.append({k: d.get(k) for k in ("id", "label", "time", "png", "old", "new")})
            except (OSError, ValueError):
                pass
    return snaps


def load_speedtests():
    out = []
    try:
        with open(SPEED_JSONL, encoding="utf-8") as f:
            for line in f:
                try:
                    out.append(json.loads(line))
                except ValueError:
                    pass
    except OSError:
        pass
    return out


class Bandwidth:
    """Per-WAN download/upload from the gateway's own byte counters (download = rx on the WAN device), 1 s
    samples, plus mwan3's per-WAN state every 10 s."""

    def __init__(self):
        self.series = {}  # wan -> [[t, down Mbit/s, up Mbit/s], ...]
        self.mwan = {}
        self.state = "starting"

    def stop(self):
        pass  # nothing to clean up: counters are read locally

    def run(self):
        with lock:
            self.series = {w: [] for w in BW_WANS}
        prev, n = None, 0
        while True:
            t0 = time.monotonic()
            try:
                vals = []
                for w in BW_WANS:
                    d = wan_dev(w)
                    vals += [read_int(f"/sys/class/net/{d}/statistics/rx_bytes") or 0,
                             read_int(f"/sys/class/net/{d}/statistics/tx_bytes") or 0] if d else [0, 0]
                up = time.monotonic()
                if prev and up - prev[0] > 0.2:
                    dt, now = up - prev[0], round(time.time(), 1)
                    with lock:
                        for i, w in enumerate(BW_WANS):
                            down = max(0.0, vals[2 * i] - prev[1][2 * i]) * 8 / dt / 1e6
                            upl = max(0.0, vals[2 * i + 1] - prev[1][2 * i + 1]) * 8 / dt / 1e6
                            s = self.series[w]
                            s.append([now, round(down, 3), round(upl, 3)])
                            del s[:-BW_KEEP]
                        self.state = "live"
                prev = (up, vals)
                if n % 10 == 0:
                    _, st = ubus_json("mwan3", "status", timeout=8)
                    ifs = (st or {}).get("interfaces") or {}
                    with lock:
                        self.mwan = {w: v.get("status") for w, v in ifs.items() if isinstance(v, dict)}
            except Exception as e:  # keep sampling whatever happened
                with lock:
                    self.state = f"error: {e}"
            n += 1
            time.sleep(max(0.05, 1.0 - (time.monotonic() - t0)))


ROUTERS = {}
BWMON = Bandwidth()
shared = {"spots": [], "speed": [], "speed_busy": None, "speed_live": None, "measuring": None, "snapshots": []}


def save_snapshot(label, data_url):
    """Save the page's PNG plus our own 10 s averages and 5-min history for both routers."""
    now = time.time()
    stamp = time.strftime("%Y%m%d_%H%M%S")
    base = f"{stamp}_{re.sub(r'[^A-Za-z0-9_-]+', '-', label).strip('-')[:30] or 'snapshot'}"
    prefix = "data:image/png;base64,"
    png = None
    if data_url.startswith(prefix):
        png = base + ".png"
        with open(os.path.join(SNAP_DIR, png), "wb") as f:
            f.write(base64.b64decode(data_url[len(prefix):]))
    with lock:
        meta = {"id": f"{stamp}{int(now * 1000) % 1000:03d}", "label": label,
                "time": time.strftime("%Y-%m-%d %H:%M:%S"), "png": png}
        full = dict(meta)
        for rid, r in ROUTERS.items():
            meta[rid] = r.window_avg(now - 10, now)
            full[rid] = dict(meta[rid], latest=r.history[-1] if r.history else None,
                             history=[[round(h["t"], 2), h.get("sinr"), h.get("rsrp")]
                                      for h in r.history if now - h["t"] <= 300])
        shared["snapshots"].append(meta)
    with open(os.path.join(SNAP_DIR, base + ".json"), "w", encoding="utf-8") as f:
        json.dump(full, f, indent=1)
    print(f"snapshot '{label}' saved: {SNAP_DIR}/{base}.png/.json  "
          f"(old SINR {meta['old']['sinr']}, new SINR {meta['new']['sinr']})", flush=True)
    return meta


def finish_spot_if_due(now):
    m = shared["measuring"]
    if not m or now < m["end"]:
        return
    spot = {"t": now, "time": time.strftime("%H:%M:%S"), "label": m["label"], "speed_old": None, "speed_new": None}
    row = {"time": spot["time"], "label": m["label"]}
    for rid, r in ROUTERS.items():
        a = r.window_avg(m["start"], m["end"])
        spot[rid] = a
        row.update({f"{rid}_sinr": a["sinr"], f"{rid}_rsrp": a["rsrp"], f"{rid}_rsrq": a["rsrq"], f"{rid}_pci": a["pci"],
                    f"{rid}_n": a["n"]})
        for i, v in enumerate(a["chains"], 1):
            row[f"{rid}_ch{i}_rsrp"] = v
    shared["spots"].append(spot)
    shared["measuring"] = None
    append_csv(SPOTS_CSV, row)
    print(f"spot '{m['label']}': old SINR {spot['old']['sinr']} RSRP {spot['old']['rsrp']} | "
          f"new SINR {spot['new']['sinr']} RSRP {spot['new']['rsrp']}", flush=True)


def spot_timer():
    while True:
        with lock:
            finish_spot_if_due(time.time())
        time.sleep(0.25)


def ping_stats(points):
    v = [p[1] for p in points]
    ok = [x for x in v if x is not None]
    return {"avg": mean(ok), "max": max(ok) if ok else None,
            "loss": round(100 * (len(v) - len(ok)) / len(v)) if v else None}


def speedtest(rid, label):
    """Download on the group's WANs for SPEED_SECONDS, streaming per-WAN throughput and RTT as it runs."""
    global speed_proc
    wans, ping_wans = SPEED_GROUPS[rid]
    devs = [wan_dev(w) or w for w in wans]
    pings = [wan_dev(w) or w for w in ping_wans]
    T, N = SPEED_SECONDS, SPEED_SECONDS * 2 + 2
    extra = "\n".join(EXTRA_PING.replace("@P@", p).replace("@N@", str(N)) for p in pings[1:])
    script = (SPEED_SCRIPT.replace("@DEVS@", " ".join(devs)).replace("@PINGS@", " ".join(pings))
              .replace("@PING0@", pings[0]).replace("@WANS@", " ".join(wans)).replace("@T@", str(T))
              .replace("@N@", str(N)).replace("@URL@", SPEED_URL).replace("@EXTRA_PINGS@", extra))
    live = {"router": rid, "label": label or "-", "T": T, "wans": wans, "devs": devs, "phase": "measuring idle ping",
            "series": [], "ping": {p: [] for p in pings}, "idle": {p: [] for p in pings}, "per": {}, "bg": {},
            "started": time.time()}
    with lock:
        shared["speed_live"] = live
    base = first = go_t = bg0 = None
    mb = 0.0
    try:
        speed_proc = proc = subprocess.Popen(["/bin/sh", "-c", script], stdout=subprocess.PIPE,
                                             stderr=subprocess.DEVNULL, text=True, bufsize=1, start_new_session=True)
        killer = threading.Timer(T + 40, proc.kill)
        killer.start()
        for line in proc.stdout:
            p = line.split()
            if not p:
                continue
            with lock:
                if p[0] == "I" and len(p) == 3:
                    live["idle"].setdefault(p[1], []).append(num(p[2]))
                elif p[0] == "B" and len(p) == 2 + len(devs):
                    # other traffic already on these WANs during the ~1 s idle-ping phase before the download
                    up, b = num(p[1]), [num(x) or 0 for x in p[2:]]
                    if bg0 is None:
                        bg0 = (up, b)
                    elif up - bg0[0] > 0.2:
                        dt = up - bg0[0]
                        live["bg"] = {w: round(max(0.0, bi - pi) * 8 / dt / 1e6, 2) for w, bi, pi in zip(wans, b, bg0[1])}
                elif p[0] == "GO":
                    live["phase"] = "running"
                    go_t = time.time()
                elif p[0] == "S" and len(p) == 4 + len(devs):
                    up, b = num(p[1]), [num(x) or 0 for x in p[4:]]
                    if base is None:
                        base, first = (up, b), up
                    elif up - base[0] > 0.05:
                        dt = up - base[0]
                        rates = [round(max(0.0, bi - pi) * 8 / dt / 1e6, 2) for bi, pi in zip(b, base[1])]
                        el = round(up - first, 2)
                        live["series"].append([el, round(sum(rates), 2)] + rates)
                        live["ping"][p[2]].append([el, num(p[3])])  # None = no reply yet (counts as loss)
                        base = (up, b)
                elif p[0] == "P" and len(p) == 3:
                    live["ping"].setdefault(p[1], []).append([round(time.time() - (go_t or time.time()), 2), num(p[2])])
                elif p[0] == "R" and len(p) == 4:
                    live["per"][p[1]] = round(float(p[2]) * 8 / 1e6, 1)
                    mb += float(p[3]) / 1e6
                elif p[0] == "END":
                    break
        proc.wait(timeout=10)
        killer.cancel()
    except (subprocess.SubprocessError, OSError, ValueError):
        pass
    speed_proc = None

    series, per = live["series"], live["per"]
    totals = [s[1] for s in series]
    smooth = [(a + b) / 2 for a, b in zip(totals, totals[1:])] or totals
    res = {"t": time.time(), "date": time.strftime("%Y-%m-%d"), "time": time.strftime("%H:%M:%S"), "router": rid,
           "label": live["label"], "T": T, "wans": wans, "devs": devs, "per": per,
           "mbit": round(sum(per.values()), 1) if per else None,
           "peak": round(max(smooth), 1) if smooth else None, "mb": round(mb, 1),
           "ping_idle": {k: mean(v) for k, v in live["idle"].items()},
           "ping_load": {k: ping_stats(v) for k, v in live["ping"].items()},
           "series": series, "ping": live["ping"], "bg": live["bg"]}
    if per:
        res["split"] = {"old": round(sum(v for w, v in per.items() if w in OLD_WANS), 1) if rid != "new" else None,
                        "new": round(sum(v for w, v in per.items() if w not in OLD_WANS), 1) if rid != "old" else None}
    with lock:
        shared["speed"].append(res)
        shared["speed_busy"] = None
        shared["speed_live"] = None
        for spot in reversed(shared["spots"]):
            if label and spot["label"] == label:
                if rid in ("old", "both") and per:
                    spot["speed_old"] = res["split"]["old"]
                if rid in ("new", "both") and per:
                    spot["speed_new"] = res["split"]["new"]
                break
    with open(SPEED_JSONL, "a", encoding="utf-8") as f:
        f.write(json.dumps(res) + "\n")
    load = res["ping_load"].get(pings[0], {})
    append_csv(SPEED_CSV, {"time": res["time"], "router": rid, "label": res["label"], "mbit": res["mbit"],
                           "peak": res["peak"], "per": json.dumps(per), "mb": res["mb"],
                           "ping_idle": res["ping_idle"].get(pings[0]), "ping_load_avg": load.get("avg"),
                           "ping_load_max": load.get("max"), "ping_loss": load.get("loss")})
    print(f"speed {rid} '{res['label']}': {res['mbit']} Mbit/s (peak {res['peak']}) {per} "
          f"ping {res['ping_idle'].get(pings[0])}->{load.get('avg')} ms ({res['mb']} MB)", flush=True)


def off_at():
    v = read_int(OFF_AT_FILE)
    return v if v and v > 0 else None


def autooff_watch():
    """When /var/run/signalmon/off_at passes: disable the service and stop it (LuCI shows it as off)."""
    while True:
        t = off_at()
        if t and time.time() >= t:
            print("auto-off: time is up, turning the signal monitor off", flush=True)
            subprocess.Popen(["/bin/sh", "-c", "uci set signalmon.main.enabled=0 && uci commit signalmon && "
                                               "/etc/init.d/signalmon stop"], start_new_session=True)
            return
        time.sleep(10)


def snapshot():
    now = time.time()
    with lock:
        m = shared["measuring"]
        oa = off_at()
        return {
            "routers": [r.snapshot(now) for r in ROUTERS.values()],
            "spots": shared["spots"],
            "speed_rev": len(shared["speed"]),
            "speed_busy": shared["speed_busy"],
            "speed_live": shared["speed_live"],
            "measuring": {"label": m["label"], "remaining": max(0.0, m["end"] - now)} if m else None,
            "snapshots": shared["snapshots"],
            "poll": {"old": OLD_POLL, "old_at": OLD_AT, "new": NEW_POLL},
            "bw": {w: s[-300:] for w, s in BWMON.series.items()},
            "bw_mwan": BWMON.mwan,
            "wan_gw": {w: (_devs.get(w) or (None, None))[1] for w in BW_WANS},
            "bw_state": BWMON.state,
            "auto_off": {"off_at": oa, "remaining": max(0, round(oa - now)) if oa else None},
            "data_dir": DATA_DIR,
        }


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _send(self, code, body, ctype):
        b = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            with open(PAGE_FILE, encoding="utf-8") as f:  # re-read so page edits apply on refresh
                self._send(200, f.read(), "text/html; charset=utf-8")
        elif path == "/data":
            self._send(200, json.dumps(snapshot()), "application/json")
        elif path == "/speeds":
            with lock:
                body = json.dumps(shared["speed"][-40:])
            self._send(200, body, "application/json")
        elif path.startswith("/snapshots/"):
            name = os.path.basename(path)
            fp = os.path.join(SNAP_DIR, name)
            if re.fullmatch(r"[A-Za-z0-9_.-]+\.png", name) and os.path.isfile(fp):
                with open(fp, "rb") as f:
                    self._send(200, f.read(), "image/png")
            else:
                self._send(404, "not found", "text/plain")
        else:
            self._send(404, "not found", "text/plain")

    def do_POST(self):
        u = urlparse(self.path)
        if u.path == "/snapshot":
            n = int(self.headers.get("Content-Length") or 0)
            if n > 30_000_000:
                self._send(413, "too large", "text/plain")
                return
            try:
                body = json.loads(self.rfile.read(n) or b"{}")
            except ValueError:
                body = {}
            label = str(body.get("label") or "").strip()[:40] or f"snapshot {len(shared['snapshots']) + 1}"
            meta = save_snapshot(label, str(body.get("png") or ""))
            self._send(200, json.dumps({"ok": True, "snapshot": meta}), "application/json")
            return
        qs = parse_qs(u.query)
        label = (qs.get("label", [""])[0] or "").strip()[:40]
        rid = qs.get("router", [""])[0]
        now = time.time()
        ok = False
        with lock:
            if u.path == "/spot" and not shared["measuring"]:
                label = label or f"spot {len(shared['spots']) + 1}"
                shared["measuring"] = {"label": label, "start": now, "end": now + SPOT_SECONDS}
                ok = True
            elif u.path == "/speedtest" and rid in SPEED_GROUPS and not shared["speed_busy"]:
                shared["speed_busy"] = rid  # one test at a time, so tests never skew each other
                threading.Thread(target=speedtest, args=(rid, label), daemon=True).start()
                ok = True
        self._send(200, json.dumps({"ok": ok}), "application/json")


def lan_ip():
    """the gateway's LAN address (network.lan.ipaddr, with or without /prefix), used when main.listen is empty"""
    v = load_uci("network").get("lan", {}).get("ipaddr") or "192.168.1.1"
    return (v[0] if isinstance(v, list) else v).split("/")[0]


def configure(argv=None):
    """UCI /etc/config/signalmon, then command-line overrides."""
    global LISTEN, PORT, DATA_DIR, LOG_DIR, OLD_POLL, OLD_AT, NEW_POLL, NEIGHBOUR_EVERY, SPEED_URL, SPEED_SECONDS
    global SPOTS_CSV, SPEED_CSV, SPEED_JSONL, SNAP_DIR
    c = load_uci("signalmon")
    main_, old, new = c.get("main", {}), c.get("old", {}), c.get("new", {})
    ap = argparse.ArgumentParser(description="Live LTE signal monitor for both routers (settings: /etc/config/signalmon)")
    ap.add_argument("--listen", default=main_.get("listen", ""), help="address to serve on (default: the LAN address)")
    ap.add_argument("--port", type=int, default=int(main_.get("port", PORT)))
    ap.add_argument("--old-poll", type=float, default=float(old.get("poll", OLD_POLL)), help="old router poll interval, s")
    ap.add_argument("--no-old-at", action="store_true", default=old.get("at", "1") == "0",
                    help="old router: nv values only, no AT commands (safest)")
    ap.add_argument("--new-poll", type=float, default=float(new.get("poll", NEW_POLL)), help="new router poll interval, s")
    a = ap.parse_args(argv)

    LISTEN, PORT = a.listen or lan_ip(), a.port
    OLD_AT = not a.no_old_at
    # 1 poll/s with an AT command rebooted the old router: never faster than 2 s with AT, 1 s without
    OLD_POLL = max(2.0 if OLD_AT else 1.0, a.old_poll)
    NEW_POLL = max(0.5, a.new_poll)
    NEIGHBOUR_EVERY = max(5.0, float(new.get("neighbour_every", NEIGHBOUR_EVERY)))
    SPEED_URL = main_.get("speed_url", SPEED_URL)
    SPEED_SECONDS = max(3, int(main_.get("speed_seconds", SPEED_SECONDS)))
    DATA_DIR = main_.get("data_dir", DATA_DIR)
    LOG_DIR = main_.get("log_dir", LOG_DIR)
    SNAP_DIR = os.path.join(DATA_DIR, "snapshots")
    SPEED_JSONL = os.path.join(DATA_DIR, "speedtests.jsonl")
    SPOTS_CSV = os.path.join(DATA_DIR, f"signal_spots_{STARTED}.csv")
    SPEED_CSV = os.path.join(DATA_DIR, f"signal_speed_{STARTED}.csv")
    for d in (DATA_DIR, LOG_DIR, SNAP_DIR):
        os.makedirs(d, exist_ok=True)

    OldRouter.HOST = (old.get("host", OldRouter.HOST[0]), int(old.get("port", OldRouter.HOST[1])))
    NewRouter.HOST = (new.get("host", NewRouter.HOST[0]), int(new.get("port", NewRouter.HOST[1])))
    NewRouter.USER = new.get("user", NewRouter.USER)
    NewRouter.PASS = new.get("password", NewRouter.PASS)
    OldRouter.name = old.get("name", OldRouter.name)
    NewRouter.name = new.get("name", NewRouter.name)

    # which mwan3 interfaces each router serves (bandwidth cards, speed tests)
    def wans(sec, dflt):
        v = sec.get("wans", dflt)
        return [v] if isinstance(v, str) else list(v)
    OldRouter.wans, NewRouter.wans = wans(old, OLD_WANS_L), wans(new, NEW_WANS_L)
    old_ping = old.get("ping_wan", OLD_PING if OLD_PING in OldRouter.wans else OldRouter.wans[0])
    new_ping = new.get("ping_wan", NEW_PING if NEW_PING in NewRouter.wans else NewRouter.wans[0])
    SPEED_GROUPS.clear()
    SPEED_GROUPS.update(old=(OldRouter.wans, [old_ping]), new=(NewRouter.wans, [new_ping]),
                        both=(NewRouter.wans + OldRouter.wans, [new_ping, old_ping]))
    OLD_WANS.clear()
    OLD_WANS.update(OldRouter.wans)
    BW_WANS[:] = NewRouter.wans + [w for w in OldRouter.wans if w not in NewRouter.wans]


def main():
    configure()
    ROUTERS.update(old=OldRouter(), new=NewRouter())
    shared["speed"] = load_speedtests()
    shared["snapshots"] = load_snapshots()

    def stop(*_):
        if speed_proc and speed_proc.poll() is None:
            try:
                os.killpg(speed_proc.pid, signal.SIGTERM)
            except OSError:
                pass
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, stop)
    server = ThreadingHTTPServer((LISTEN, PORT), Handler)
    server.daemon_threads = True
    print(f"signal monitor on http://{LISTEN}:{PORT}/  old router {OldRouter.HOST[0]} every {OLD_POLL:g} s "
          f"({'nv + 1 AT cmd' if OLD_AT else 'nv only'}), new router {NewRouter.HOST[0]} every {NEW_POLL:g} s; "
          f"data {DATA_DIR}, logs {LOG_DIR}", flush=True)
    for r in ROUTERS.values():
        threading.Thread(target=r.run, daemon=True).start()
    for target in (spot_timer, BWMON.run, autooff_watch):
        threading.Thread(target=target, daemon=True).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
