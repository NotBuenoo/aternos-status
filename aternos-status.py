#!/usr/bin/env python3
"""
aternos-status - check whether Aternos (Minecraft Java) servers are online.

Shows: online/offline/starting state, player count, and ping.
Watch mode sends a desktop notification (notify-send) when a server comes online.

Usage:
  aternos-status.py myserver.aternos.me                 # one-shot check
  aternos-status.py -w myserver.aternos.me              # live view + notifications
  aternos-status.py -w --until-online myserver.aternos.me   # notify once, then exit
  aternos-status.py -w a.aternos.me b.aternos.me:12345  # several servers

Player joins/leaves while a server is online trigger *silent* notifications
(use --no-player-alerts to turn them off).

Phone notifications (via ntfy - install the ntfy app and subscribe to your topic):
  aternos-status.py -w --ntfy my-secret-topic myserver.aternos.me
  aternos-status.py --ntfy my-secret-topic --test-notify   # send a test and exit

With no server arguments, addresses are read (one per line) from
~/.config/aternos-status/servers.txt

Only the Python standard library is required. Optional: dnspython, to follow
SRV records (needed for Aternos addresses that don't use port 25565).
Checking status never starts an Aternos server - only a join attempt does.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import socket
import struct
import subprocess
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_PORT = 25565
PROTOCOLS = (-1, 767)  # handshake protocol numbers to try, in order
CONFIG_FILE = (
    Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    / "aternos-status"
    / "servers.txt"
)

ONLINE, OFFLINE, STARTING, STOPPING, UNREACHABLE = (
    "online", "offline", "starting", "stopping", "unreachable",
)

# Aternos answers pings itself while the real server is down, with a
# placeholder status (e.g. version name "● Offline" and 0 max players).
STARTING_WORDS = ("starting", "loading", "preparing", "queue", "waiting", "connecting")
STOPPING_WORDS = ("stopping", "saving")
OFFLINE_WORDS = ("offline", "stopped")


@dataclass
class Status:
    address: str
    state: str
    players_online: int = 0
    players_max: int = 0
    sample: list = field(default_factory=list)
    ping_ms: float | None = None
    version: str = ""
    motd: str = ""
    error: str = ""
    raw: dict | None = None


# --------------------------------------------------------------------------
# Minecraft Server List Ping protocol
# --------------------------------------------------------------------------
def varint(n: int) -> bytes:
    n &= 0xFFFFFFFF
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def decode_varint(buf: bytes, off: int = 0) -> tuple[int, int]:
    result = shift = 0
    while True:
        b = buf[off]
        off += 1
        result |= (b & 0x7F) << shift
        if not b & 0x80:
            return result, off
        shift += 7
        if shift > 35:
            raise ValueError("VarInt too long")


def recv_exact(sock: socket.socket, n: int) -> bytes:
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("connection closed by server")
        buf += chunk
    return bytes(buf)


def read_varint(sock: socket.socket) -> int:
    result = 0
    for i in range(5):
        b = recv_exact(sock, 1)[0]
        result |= (b & 0x7F) << (7 * i)
        if not b & 0x80:
            return result
    raise ValueError("VarInt too long")


class PingError(Exception):
    def __init__(self, msg: str, retry: bool = False):
        super().__init__(msg)
        self.retry = retry  # worth retrying with a different handshake?


def query(host: str, conn_host: str, conn_port: int, timeout: float, protocol: int = -1):
    """Return (status_json, ping_ms)."""
    t0 = time.perf_counter()
    try:
        s = socket.create_connection((conn_host, conn_port), timeout=timeout)
    except socket.timeout:
        raise PingError(f"connect timed out after {timeout:g}s") from None
    with s:
        connected_ms = (time.perf_counter() - t0) * 1000
        s.settimeout(timeout)
        host_b = host.encode("utf-8")
        handshake = (
            b"\x00" + varint(protocol) + varint(len(host_b)) + host_b
            + struct.pack(">H", conn_port) + varint(1)
        )
        s.sendall(varint(len(handshake)) + handshake)
        s.sendall(b"\x01\x00")  # status request

        t_req = time.perf_counter()
        try:
            length = read_varint(s)
            payload = recv_exact(s, length)
        except socket.timeout:
            raise PingError(
                f"connected in {connected_ms:.0f} ms, but no status reply within {timeout:g}s",
                retry=True) from None
        except ConnectionError:
            raise PingError("connected, but the server closed the connection without a status reply",
                            retry=True) from None
        fallback_ms = (time.perf_counter() - t_req) * 1000

        _pid, off = decode_varint(payload, 0)
        slen, off = decode_varint(payload, off)
        data = json.loads(payload[off:off + slen].decode("utf-8", "replace"))

        # Real ping/pong round trip; fall back to status round trip if refused.
        try:
            token = int(time.time() * 1000) & 0x7FFFFFFFFFFFFFFF
            pkt = b"\x01" + struct.pack(">q", token)
            t1 = time.perf_counter()
            s.sendall(varint(len(pkt)) + pkt)
            plen = read_varint(s)
            recv_exact(s, plen)
            ping_ms = (time.perf_counter() - t1) * 1000
        except (OSError, ConnectionError, ValueError):
            ping_ms = fallback_ms
    return data, ping_ms


def resolve_srv(host: str):
    try:
        import dns.resolver  # type: ignore

        answers = dns.resolver.resolve(f"_minecraft._tcp.{host}", "SRV", lifetime=3)
        best = sorted(answers, key=lambda r: (r.priority, -r.weight))[0]
        return str(best.target).rstrip("."), int(best.port)
    except Exception:
        return None


def flatten_motd(d) -> str:
    if d is None:
        return ""
    if isinstance(d, str):
        return d
    if isinstance(d, list):
        return "".join(flatten_motd(x) for x in d)
    if isinstance(d, dict):
        s = d.get("text") or d.get("translate") or ""
        return s + "".join(flatten_motd(x) for x in d.get("extra", []))
    return str(d)


def classify(version: str, motd: str, p_max: int) -> str:
    v, m = re.sub(r"§.", "", version).lower(), motd.lower()
    placeholder = "●" in v or p_max == 0
    if not placeholder:
        return ONLINE
    for text in (v, m):
        if any(w in text for w in STOPPING_WORDS):
            return STOPPING
        if any(w in text for w in STARTING_WORDS):
            return STARTING
        if any(w in text for w in OFFLINE_WORDS):
            return OFFLINE
    return OFFLINE


def split_address(addr: str):
    addr = addr.strip()
    if addr.count(":") == 1:
        host, port = addr.rsplit(":", 1)
        if port.isdigit():
            return host, int(port)
    return addr, None


def check(addr: str, timeout: float) -> Status:
    host, port = split_address(addr)
    conn_host, conn_port = host, port or DEFAULT_PORT
    if port is None:
        srv = resolve_srv(host)
        if srv:
            conn_host, conn_port = srv
    data, err, tried = None, "", 0
    for proto in PROTOCOLS:
        tried += 1
        try:
            data, ping_ms = query(host, conn_host, conn_port, timeout, proto)
            break
        except PingError as e:
            err = str(e)
            if e.retry:
                continue
            break
        except (OSError, ValueError, IndexError) as e:
            err = str(e) or type(e).__name__
            break
    if data is None:
        if tried > 1:
            err += " (retried with another protocol number)"
        return Status(addr, UNREACHABLE, error=err)

    players = data.get("players") or {}
    version = (data.get("version") or {}).get("name", "")
    motd = re.sub(r"§.", "", flatten_motd(data.get("description"))).strip()
    p_max = int(players.get("max", 0) or 0)
    return Status(
        address=addr,
        state=classify(version, motd, p_max),
        players_online=int(players.get("online", 0) or 0),
        players_max=p_max,
        sample=[p.get("name", "?") for p in (players.get("sample") or [])],
        ping_ms=ping_ms,
        version=re.sub(r"§.", "", version),
        motd=" ".join(motd.split()),
        raw=data,
    )


# --------------------------------------------------------------------------
# Output + notifications
# --------------------------------------------------------------------------
USE_COLOR = sys.stdout.isatty() and "NO_COLOR" not in os.environ
COLORS = {ONLINE: "32", STARTING: "33", STOPPING: "33", OFFLINE: "31", UNREACHABLE: "90"}


def c(text: str, code: str) -> str:
    return f"\033[{code}m{text}\033[0m" if USE_COLOR else text


def render(st: Status, name_w: int) -> str:
    dot = c("●", COLORS[st.state])
    state = c(st.state.upper().ljust(11), COLORS[st.state])
    line = f"{dot} {st.address.ljust(name_w)}  {state}"
    if st.state == ONLINE:
        line += f" {st.players_online}/{st.players_max} players"
        line += f"   {st.ping_ms:.0f} ms" if st.ping_ms is not None else ""
        if st.version:
            line += c(f"   {st.version}", "90")
        if st.sample:
            line += "\n    " + c("players: " + ", ".join(st.sample), "36")
    elif st.state == UNREACHABLE and st.error:
        line += c(f" {st.error}", "90")
    return line


_warned = False
PUSH = {"server": "https://ntfy.sh", "topic": None, "token": None, "desktop": True}


def push_phone(title: str, body: str, urgency: str) -> None:
    if not PUSH["topic"]:
        return
    req = urllib.request.Request(
        f'{PUSH["server"].rstrip("/")}/{PUSH["topic"]}',
        data=body.encode("utf-8"),
        headers={
            "Title": title,
            "Priority": {"critical": "high", "low": "low"}.get(urgency, "default"),
            "Tags": "video_game",
        },
        method="POST",
    )
    if PUSH["token"]:
        req.add_header("Authorization", f"Bearer {PUSH['token']}")
    try:
        urllib.request.urlopen(req, timeout=10).close()
    except Exception as e:  # never let a failed push kill the watch loop
        print(f"phone push failed: {e}")


def notify(title: str, body: str, urgency: str = "normal") -> None:
    global _warned
    if PUSH["desktop"]:
        exe = shutil.which("notify-send")
        if exe:
            cmd = [exe, "-a", "aternos-status", "-u", urgency, "-i", "applications-games"]
            if urgency == "low":  # silent: ask the daemon not to play a sound
                cmd += ["-h", "boolean:suppress-sound:true"]
            subprocess.run(cmd + [title, body], check=False, timeout=5)
        elif not _warned:
            print("note: notify-send not found (install libnotify) - using terminal bell only")
            _warned = True
        if urgency != "low":
            sys.stdout.write("\a")
            sys.stdout.flush()
    push_phone(title, body, urgency)


def player_changes(old: Status, new: Status):
    """Joins/leaves between two ONLINE polls, as [(verb, who)]."""
    def known(st):  # server gave us the full name list
        return len(st.sample) == st.players_online

    events = []
    if known(old) and known(new):
        joined = [n for n in new.sample if n not in old.sample]
        left = [n for n in old.sample if n not in new.sample]
        if joined:
            events.append(("joined", ", ".join(joined)))
        if left:
            events.append(("left", ", ".join(left)))
    else:  # names hidden/truncated: fall back to the player count
        d = new.players_online - old.players_online
        if d:
            n = abs(d)
            events.append(("joined" if d > 0 else "left", "A player" if n == 1 else f"{n} players"))
    return events


def check_all(servers, timeout):
    with ThreadPoolExecutor(max_workers=min(8, len(servers))) as pool:
        return list(pool.map(lambda a: check(a, timeout), servers))


def load_servers(cli_servers):
    if cli_servers:
        return cli_servers
    if CONFIG_FILE.exists():
        lines = CONFIG_FILE.read_text().splitlines()
        servers = [l.strip() for l in lines if l.strip() and not l.lstrip().startswith("#")]
        if servers:
            return servers
    sys.exit(f"No servers given. Pass addresses, or list them in {CONFIG_FILE}")


def main() -> int:
    ap = argparse.ArgumentParser(description="Check Aternos Minecraft server status.")
    ap.add_argument("servers", nargs="*", help="e.g. myserver.aternos.me or host:port")
    ap.add_argument("-w", "--watch", action="store_true", help="keep checking and notify on changes")
    ap.add_argument("-i", "--interval", type=int, default=15, help="seconds between checks (default 15)")
    ap.add_argument("-t", "--timeout", type=float, default=8.0, help="connect/read timeout in seconds (default 8)")
    ap.add_argument("--until-online", action="store_true",
                    help="with --watch: notify when online, then exit")
    ap.add_argument("--no-notify", action="store_true", help="disable desktop notifications")
    ap.add_argument("--ntfy", metavar="TOPIC", default=os.environ.get("NTFY_TOPIC"),
                    help="also push to your phone via this ntfy topic (or set $NTFY_TOPIC)")
    ap.add_argument("--ntfy-server", default=os.environ.get("NTFY_SERVER", "https://ntfy.sh"),
                    help="ntfy server URL (default https://ntfy.sh; use your own if self-hosted)")
    ap.add_argument("--no-player-alerts", action="store_true",
                    help="don't send (silent) notifications when players join or leave")
    ap.add_argument("--test-notify", action="store_true", help="send a test notification and exit")
    ap.add_argument("--debug", action="store_true", help="print the raw status JSON")
    args = ap.parse_args()

    PUSH.update(server=args.ntfy_server, topic=args.ntfy, token=os.environ.get("NTFY_TOKEN"),
                desktop=not args.no_notify)
    if args.test_notify:
        notify("aternos-status test", "Notifications are working.", "critical")
        return 0

    servers = load_servers(args.servers)
    name_w = max(len(s) for s in servers)

    if not args.watch:
        results = check_all(servers, args.timeout)
        for r in results:
            print(render(r, name_w))
            if args.debug and r.raw:
                print(json.dumps(r.raw, indent=2, ensure_ascii=False))
        return 0 if any(r.state == ONLINE for r in results) else 1

    prev: dict[str, str] = {}
    prev_status: dict[str, Status] = {}
    seen_online: set[str] = set()
    tty = sys.stdout.isatty()
    try:
        while True:
            results = check_all(servers, args.timeout)
            if tty:
                sys.stdout.write("\033[H\033[2J")
            for r in results:
                print(render(r, name_w))
                if args.debug and r.raw:
                    print(c(json.dumps(r.raw, ensure_ascii=False), "90"))
            print(c(f"\nchecked {time.strftime('%H:%M:%S')} · every {args.interval}s · Ctrl-C to quit", "90"))
            sys.stdout.flush()

            for r in results:
                old = prev.get(r.address)
                came_online = r.state == ONLINE and old != ONLINE
                if came_online:
                    seen_online.add(r.address)
                if PUSH["desktop"] or PUSH["topic"]:
                    if came_online and (old is not None or args.until_online):
                        notify(f"{r.address} is online!",
                               f"{r.players_online}/{r.players_max} players · {r.ping_ms:.0f} ms",
                               "critical")
                    elif old == ONLINE and r.state in (OFFLINE, STOPPING):
                        notify(f"{r.address} went offline", "The server has stopped.")
                    elif (not args.no_player_alerts and old == ONLINE and r.state == ONLINE
                          and r.address in prev_status):
                        for verb, who in player_changes(prev_status[r.address], r):
                            notify(f"{who} {verb}",
                                   f"{r.address} · {r.players_online}/{r.players_max} players",
                                   "low")
                prev[r.address] = r.state
                prev_status[r.address] = r

            if args.until_online and seen_online >= set(servers):
                return 0
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print()
        return 0


if __name__ == "__main__":
    sys.exit(main())
