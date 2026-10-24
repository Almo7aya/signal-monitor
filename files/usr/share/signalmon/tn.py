"""Minimal telnet client: login (optional) and run read-only commands.

usage: python tn.py HOST PORT [--user U --pass P] [--peek] [--cmdfile F] [--timeout T]
"""
import argparse
import re
import socket
import sys
import time

IAC, DONT, DO, WONT, WILL, SB, SE = 255, 254, 253, 252, 251, 250, 240
ECHO, SGA, TTYPE, NAWS = 1, 3, 24, 31


class Telnet:
    def __init__(self, host, port, timeout=10):
        self.s = socket.create_connection((host, port), timeout=timeout)
        self.s.settimeout(0.3)
        self.buf = b""

    def _process(self, data):
        out = bytearray()
        i = 0
        while i < len(data):
            b = data[i]
            if b == IAC and i + 1 < len(data):
                cmd = data[i + 1]
                if cmd in (DO, DONT, WILL, WONT) and i + 2 < len(data):
                    opt = data[i + 2]
                    if cmd == DO:
                        # accept only terminal type / NAWS / SGA; refuse others
                        resp = WILL if opt in (SGA, NAWS, TTYPE) else WONT
                        self.s.sendall(bytes([IAC, resp, opt]))
                        if opt == NAWS:
                            self.s.sendall(bytes([IAC, SB, NAWS, 0, 250, 0, 60, IAC, SE]))
                    elif cmd == WILL:
                        resp = DO if opt in (ECHO, SGA) else DONT
                        self.s.sendall(bytes([IAC, resp, opt]))
                    i += 3
                    continue
                if cmd == SB:
                    end = data.find(bytes([IAC, SE]), i)
                    if end == -1:
                        break
                    sub = data[i + 2:end]
                    if sub and sub[0] == TTYPE:
                        self.s.sendall(bytes([IAC, SB, TTYPE, 0]) + b"vt100" + bytes([IAC, SE]))
                    i = end + 2
                    continue
                if cmd == IAC:
                    out.append(IAC)
                i += 2
                continue
            out.append(b)
            i += 1
        return bytes(out)

    def read(self, wait=1.5, until=None, max_wait=None):
        """Read until `wait` seconds of silence or regex `until` matches."""
        deadline = time.time() + (max_wait or wait * 10)
        last = time.time()
        chunk_all = b""
        while time.time() < deadline:
            try:
                d = self.s.recv(65536)
                if not d:
                    break
                d = self._process(d)
                chunk_all += d
                last = time.time()
                if until and re.search(until, chunk_all.decode("utf-8", "replace")):
                    break
            except socket.timeout:
                if time.time() - last > wait:
                    break
        return chunk_all.decode("utf-8", "replace")

    def write(self, text):
        self.s.sendall(text.encode() + b"\r\n")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("host")
    ap.add_argument("port", type=int)
    ap.add_argument("--user")
    ap.add_argument("--password")
    ap.add_argument("--peek", action="store_true")
    ap.add_argument("--cmdfile")
    ap.add_argument("--wait", type=float, default=2.0)
    ap.add_argument("--prompt", default=r"[#$>]\s*$")
    a = ap.parse_args()

    t = Telnet(a.host, a.port)
    banner = t.read(wait=2.5, max_wait=8)
    sys.stdout.write(banner)
    if a.peek:
        return
    if a.user:
        if not re.search(r"(?i)login|user", banner):
            banner = t.read(wait=2, max_wait=6)
            sys.stdout.write(banner)
        t.write(a.user)
        sys.stdout.write(t.read(wait=2, until=r"(?i)password", max_wait=8))
        t.write(a.password or "")
        sys.stdout.write(t.read(wait=2.5, max_wait=10))
    if a.cmdfile:
        with open(a.cmdfile, encoding="utf-8") as f:
            for line in f:
                line = line.rstrip("\n")
                if not line.strip() or line.startswith("#"):
                    continue
                t.write(line)
                sys.stdout.write(t.read(wait=a.wait, max_wait=60))
                sys.stdout.flush()
    t.write("exit")


if __name__ == "__main__":
    main()
