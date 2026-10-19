#!/usr/bin/env python3
"""mDNS multicast-path probe.

Sends PTR queries to the mDNS group (224.0.0.251:5353) from an EPHEMERAL source
port, so responders reply legacy-unicast (RFC 6762 §6.7). This exercises the
wired -> wireless multicast delivery path that a unicast probe cannot see — the
path that silently breaks when a snooping switch has no querier.

Replies are unicast from an unsolicited source, so a default-deny firewall drops
them silently (visible in tcpdump, invisible to the socket). Required rule:
    allow in on <lan-iface> udp from <lan-cidr> port 5353

Environment:
    MDNS_TARGET_IP   address the speaker must answer from (required)
    MDNS_SERVICES    comma-separated service names (default: Sonos/AirPlay/Spotify)

Exit 0 if the target answers every service, 1 otherwise. The cron wrapper turns
that into `arklab_mdns_speaker_discoverable` for the textfile collector.
Run it by hand DURING a live fault — one red run mid-fault outweighs a week of green.
"""
import os
import socket
import struct
import sys

TARGET_IP = os.environ.get("MDNS_TARGET_IP", "")
SERVICES = [s for s in os.environ.get(
    "MDNS_SERVICES", "_sonos._tcp.local,_airplay._tcp.local,_spotify-connect._tcp.local"
).split(",") if s]


def query(name: str) -> list[str]:
    parts = b"".join(struct.pack("B", len(p)) + p.encode() for p in name.split("."))
    pkt = struct.pack(">HHHHHH", 0x1234, 0, 1, 0, 0, 0) + parts + b"\x00" + struct.pack(">HH", 12, 1)
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
    s.settimeout(4)
    s.sendto(pkt, ("224.0.0.251", 5353))
    hits = []
    try:
        while True:
            _, addr = s.recvfrom(65535)
            hits.append(addr[0])
    except socket.timeout:
        pass
    s.close()
    return sorted(set(hits))


def main() -> int:
    if not TARGET_IP:
        print("MDNS_TARGET_IP is not set", file=sys.stderr)
        return 2
    ok = True
    for svc in SERVICES:
        r = query(svc)
        status = "OK" if TARGET_IP in r else "MISSING"
        if TARGET_IP not in r:
            ok = False
        print(f"{svc}: {r if r else 'NO REPLY'} [{status}]")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
