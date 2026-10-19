#!/usr/bin/env python3
"""
igmp-querier — IGMPv2 General Querier for a LAN that snoops without a querier
(RFC 2236 §7–8).

Why this exists: the router runs IGMP snooping but has no querier. A station's
group membership ages out (~260 s) unless it re-reports on its own; a smart
speaker that reports once at join then goes deaf to mDNS (224.0.0.251) until
power-cycled. A General Query every 125 s makes every host re-announce its
groups, keeping the snooping table fresh. Proven with a single query: the
speaker re-joined within 4 s. See docs/post-mortems/01-igmp-querier.md.

Election (RFC 2236 §8.5): a General Query from a LOWER IP means another querier
exists — we go passive for the Other Querier Present Interval (255 s) and resume
if it falls silent. Receive is bound to the LAN interface so a container on a
Docker bridge cannot vote us out.

Privileges: CAP_NET_RAW only. Metrics: Prometheus textfile collector, written
atomically. No log line per query — the metric is the heartbeat.
"""
import os
import select
import signal
import socket
import struct
import time

IFACE = os.environ.get("IGMP_IFACE", "eth0")
SOURCE_IP = os.environ.get("IGMP_SOURCE_IP", "192.168.1.10")
INTERVAL = int(os.environ.get("IGMP_INTERVAL", "125"))      # Query Interval, RFC default
MAX_RESP = int(os.environ.get("IGMP_MAX_RESP", "100"))      # tenths of a second → 10 s
METRICS = os.environ.get("IGMP_METRICS", "")                # empty = no metrics file
ROBUSTNESS = 2
ALL_HOSTS = "224.0.0.1"
ROUTER_ALERT = b"\x94\x04\x00\x00"                          # RFC 2113
OQPI = ROBUSTNESS * INTERVAL + MAX_RESP // 20               # Other Querier Present Interval: 255 s
STARTUP_INTERVAL = INTERVAL // 4                            # RFC 2236 §8.6


def log(msg: str) -> None:
    print(msg, flush=True)


def csum(data: bytes) -> int:
    if len(data) % 2:
        data += b"\0"
    total = sum(struct.unpack("!%dH" % (len(data) // 2), data))
    total = (total >> 16) + (total & 0xFFFF)
    total += total >> 16
    return (~total) & 0xFFFF


def build_general_query() -> bytes:
    body = struct.pack("!BBH4s", 0x11, MAX_RESP, 0, b"\0\0\0\0")
    return struct.pack("!BBH4s", 0x11, MAX_RESP, csum(body), b"\0\0\0\0")


def ip_int(addr: str) -> int:
    return struct.unpack("!I", socket.inet_aton(addr))[0]


class Querier:
    def __init__(self) -> None:
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_RAW, socket.IPPROTO_IGMP)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_BINDTODEVICE, IFACE.encode())
        self.sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(SOURCE_IP))
        self.sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 1)
        self.sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, 0)
        self.sock.setsockopt(socket.IPPROTO_IP, socket.IP_OPTIONS, ROUTER_ALERT)
        self.sock.setblocking(False)
        self.query = build_general_query()
        self.queries_sent = 0
        self.last_sent = 0.0
        self.passive_until = 0.0
        self.other_querier = ""
        self.other_seen = 0.0
        self.running = True

    # -- state ---------------------------------------------------------------
    def is_active(self, now: float) -> bool:
        return now >= self.passive_until

    def send_query(self, now: float) -> None:
        self.sock.sendto(self.query, (ALL_HOSTS, 0))
        self.queries_sent += 1
        self.last_sent = now

    def handle_incoming(self, now: float) -> None:
        while True:
            try:
                data, (src, _port) = self.sock.recvfrom(2048)
            except BlockingIOError:
                return
            ihl = (data[0] & 0x0F) * 4
            if len(data) < ihl + 8 or data[ihl] != 0x11:   # only IGMP Membership Queries matter
                continue
            if src == SOURCE_IP or ip_int(src) >= ip_int(SOURCE_IP):
                continue                                   # ours, or a higher IP: we stay querier
            was_active = self.is_active(now)
            self.passive_until = now + OQPI
            self.other_querier, self.other_seen = src, now
            if was_active:
                log(f"lower-IP querier {src} heard — going passive for {OQPI}s (RFC 2236 §8.5)")

    # -- metrics -------------------------------------------------------------
    def write_metrics(self, now: float) -> None:
        if not METRICS:
            return
        lines = [
            "# HELP arklab_igmp_querier_last_query_seconds Unix time of the last IGMP General Query sent.",
            "# TYPE arklab_igmp_querier_last_query_seconds gauge",
            f"arklab_igmp_querier_last_query_seconds {self.last_sent:.0f}",
            "# HELP arklab_igmp_querier_queries_total General Queries sent since service start.",
            "# TYPE arklab_igmp_querier_queries_total counter",
            f"arklab_igmp_querier_queries_total {self.queries_sent}",
            "# HELP arklab_igmp_querier_active 1 if this host is the elected querier, 0 while deferring to a lower-IP querier.",
            "# TYPE arklab_igmp_querier_active gauge",
            f"arklab_igmp_querier_active {1 if self.is_active(now) else 0}",
            "# HELP arklab_igmp_querier_other_querier_seen_seconds Unix time a General Query from another host was last heard (0 = never).",
            "# TYPE arklab_igmp_querier_other_querier_seen_seconds gauge",
            f"arklab_igmp_querier_other_querier_seen_seconds {self.other_seen:.0f}",
            "# HELP arklab_igmp_querier_interval_seconds Configured Query Interval.",
            "# TYPE arklab_igmp_querier_interval_seconds gauge",
            f"arklab_igmp_querier_interval_seconds {INTERVAL}",
        ]
        tmp = METRICS + ".tmp"
        with open(tmp, "w") as fh:
            fh.write("\n".join(lines) + "\n")
        os.replace(tmp, METRICS)                           # atomic: never a half-written file

    # -- main loop -----------------------------------------------------------
    def run(self) -> None:
        log(f"igmp-querier: iface={IFACE} src={SOURCE_IP} interval={INTERVAL}s "
            f"max-resp={MAX_RESP / 10:.0f}s startup={ROBUSTNESS}x{STARTUP_INTERVAL}s oqpi={OQPI}s")
        startup_left = ROBUSTNESS
        next_send = time.time()
        was_active = True
        while self.running:
            now = time.time()
            active = self.is_active(now)
            if active and not was_active:
                log(f"no query from {self.other_querier} for {OQPI}s — resuming as querier")
            was_active = active
            if now >= next_send:
                if active:
                    self.send_query(now)
                if startup_left > 0:
                    startup_left -= 1
                    next_send = now + STARTUP_INTERVAL
                else:
                    next_send = now + INTERVAL
                self.write_metrics(now)
            wait = max(0.0, min(next_send - time.time(), 5.0))   # ≤5 s so SIGTERM is honoured promptly
            readable, _, _ = select.select([self.sock], [], [], wait)
            if readable:
                self.handle_incoming(time.time())
        log("stopped")

    def stop(self, *_args) -> None:
        self.running = False


def main() -> None:
    q = Querier()
    signal.signal(signal.SIGTERM, q.stop)
    signal.signal(signal.SIGINT, q.stop)
    q.run()


if __name__ == "__main__":
    main()
