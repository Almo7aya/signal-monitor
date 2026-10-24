# signal-monitor

A live LTE signal monitor for two 4G routers, served by the OpenWrt gateway that sits in front of them. Use it
while you place antennas, choose bands or compare locations. You switch it on and off from LuCI, and it turns
itself off after a while, so the routers' modems are only polled while you are actually looking.

- **Signal:** both routers side by side.
  - Per-antenna RSRP, RSSI and SINR, with 5-minute charts.
  - On the SIMCom router: neighbour cells, MIMO, rank, MCS and carrier aggregation.
- **Antenna work:**
  - "Measure spot" takes a 15 s average of both routers.
  - Snapshots save PNG + JSON references, with deltas against a chosen one.
  - Optional tones: one voice per router, and the pitch follows the signal, so you can move antennas without
    looking at the screen.
- **Bandwidth:** live download and upload per WAN, from the gateway's own interface counters, with mwan3 state.
- **Speed tests** per router: a 10 s download through that router's WANs (`mwan3 use`), with ping under load.

## Supported hardware

It talks to the routers' modems directly over telnet. It is written for these two:

| | "Old" router | "New" router |
|---|---|---|
| Example | Tozed ZLT S25 | Tozed CPE with a SIMCom module |
| Chipset / modem | ZTE ZX297520V3 | SIMCom SIM7906E (Qualcomm) |
| Access | telnet (port 23), no login | telnet (port 4719), login |
| Values | firmware nv values, plus 1 AT command per poll (`AT+CESQ` / `AT+ZRSSI`) | `AT+CLTEINFO?`, plus `AT$QCRSRP?` every 30 s |

Other routers need their own poller class in `signalmon.py`.

The gateway: OpenWrt (tested on 24.10, Raspberry Pi 4) with `python3`, `rpcd` and LuCI. Bandwidth cards and
speed tests use `mwan3`.

## Safety

- **The ZTE router reboots when it is polled too hard.** 4 AT commands per second rebooted it, and so did 1 poll
  per second with an AT command. The monitor never polls it faster than every 2 s while AT is on. Turning AT off
  (firmware values only, refreshed every ~3 s) is the safest setting.
- **It only polls the routers while it is on.** It is off by default and turns itself off after `auto_off`
  minutes (default 60).
- **Speed tests download through your WANs** (~5–25 MB of mobile data each). They only run when you press a
  speed-test button.
- The page has no login. It listens on the LAN address only; don't expose the port to the internet.

## Install

From Windows (ssh key access to the gateway as root):

```powershell
.\deploy.ps1 -Router root@192.168.1.1
```

From Linux or macOS:

```sh
./deploy.sh root@192.168.1.1
```

Both copy the project to the gateway and run `install.sh` there. You can also set `SIGNALMON_ROUTER` once
instead of passing the address. By hand: copy `files/`, `install.sh` and `uninstall.sh` to the gateway and run
`sh install.sh`.

The first install creates `/etc/config/signalmon`. Set your routers' addresses, which mwan3 interfaces go through
each router, and the SIMCom router's telnet password, in LuCI (Services → Signal Monitor) or with `uci`:

```sh
uci set signalmon.new.host='192.168.0.1'
uci set signalmon.new.password='…'
uci commit signalmon
```

Updates keep your config. If the monitor is on, an update restarts it.

Remove it with `sh uninstall.sh` (keeps the settings and the data directory) or `sh uninstall.sh --purge` (also
deletes the settings).

## Use

**LuCI → Services → Signal Monitor:**

- the on/off switch;
- how long until it turns itself off, and "Restart the timer";
- "Open monitor ↗" (`http://<LAN address>:8765/`), or "Show the monitor on this page";
- all settings.

**Shell:** `ubus call signalmon start`, `stop`, `status`, `extend '{"minutes":60}'`.

### How on/off works

- `signalmon.main.enabled` is the switch. `start` sets it to 1 and starts the procd service; `stop` sets it to 0
  and stops it. At boot the service starts only if it was left on.
- On start, the init script writes `/var/run/signalmon/off_at` (now + `auto_off` minutes). When that time passes,
  the server sets `enabled=0` and stops the service. "Restart the timer" rewrites `off_at`.
- Saving settings in LuCI restarts the monitor if it is on.

## Settings (`/etc/config/signalmon`)

| Option | Default | |
|---|---|---|
| `main.enabled` | `0` | on/off |
| `main.auto_off` | `60` | minutes until it turns itself off; `0` = never |
| `main.listen`, `main.port` | LAN address, `8765` | where the page is served |
| `main.data_dir` | `/root/signalmon` | snapshots, `speedtests.jsonl`, spot/speed CSV (kept) |
| `main.log_dir` | `/tmp/signalmon` | per-poll CSV logs, in RAM to spare the SD card |
| `main.speed_url`, `main.speed_seconds` | Cloudflare 50 MB, `10` | speed tests |
| `old.name`, `old.host`, `old.port`, `old.poll`, `old.at` | `Old router`, `192.168.1.1`, `23`, `2`, `1` | ZTE router |
| `new.name`, `new.host`, `new.port`, `new.user`, `new.password` | `New router`, `192.168.0.1`, `4719`, `superadmin`, — | SIMCom router |
| `new.poll`, `new.neighbour_every` | `1`, `30` | SIMCom polling (s) |
| `<router>.wans` (list), `<router>.ping_wan` | old: `wan2 wan3 wan4`, `wan3`; new: `wan1`, `wan1` | the mwan3 interfaces that go through each router, and the one pinged during its speed test |

Command-line flags override the settings when testing by hand:

```sh
python3 /usr/share/signalmon/signalmon.py --port 8766 --old-poll 3 --no-old-at
```

## HTTP API (used by the page)

| Request | |
|---|---|
| `GET /data` | live state of both routers, bandwidth, spots, snapshots, auto-off |
| `GET /speeds` | the last 40 speed tests |
| `POST /spot?label=…` | 15 s spot measurement |
| `POST /speedtest?router=old\|new\|both&label=…` | speed test (uses mobile data) |
| `POST /snapshot` `{label, png}` | save a snapshot |

## Layout

```
files/                                  installed as-is under / on the gateway
  etc/config/signalmon                  settings (kept on update)
  etc/init.d/signalmon                  procd service; runs only while enabled=1
  usr/share/signalmon/signalmon.py      HTTP server + pollers (Python 3, standard library only)
  usr/share/signalmon/index.html        the monitor page
  usr/share/signalmon/tn.py             minimal telnet client
  usr/libexec/rpcd/signalmon            ubus API for LuCI: status / start / stop / extend
  usr/share/rpcd/acl.d/luci-app-signalmon.json
  usr/share/luci/menu.d/luci-app-signalmon.json
  www/luci-static/resources/view/signalmon/index.js   LuCI page
install.sh, uninstall.sh                run on the gateway
deploy.ps1, deploy.sh                   copy + install from Windows / Linux / macOS
```

## License

MIT — see [LICENSE](LICENSE).
