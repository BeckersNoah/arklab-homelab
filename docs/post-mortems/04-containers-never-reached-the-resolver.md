# Post-mortem 4 — Containers had never reached the resolver

| | |
|---|---|
| **Impact** | For ~6 months every container resolved DNS through Docker's public fallback instead of the lab's Pi-hole: unfiltered, unlogged, DNSSEC-unvalidated, and invisible to every dashboard that said "DNS is fine" |
| **Duration** | Undetected since the lab was built; surfaced by a single container entering a restart loop after a routine security reboot |
| **Class** | Container networking — a fallback that worked hid a path that never did |

## Symptom

After a kernel update and reboot, one Python service (a Telegram bot) went into a restart loop: it could not resolve `api.telegram.org`. Every other container looked healthy. House DNS was working for every laptop and phone.

## Investigation

1. `docker exec <container> getent hosts api.telegram.org` — no answer. `docker exec pihole …` — Pi-hole itself was fine.
2. `/etc/docker/daemon.json` pointed container DNS at the host's **LAN address**, the same address every laptop uses. Reasonable, and wrong:
   - Docker's NAT rules for a published port **exclude bridge-originated traffic to the host's own IP** (the `! -i br-…` match on the DNAT rule). A container sending to `<host LAN IP>:53` is not redirected into the Pi-hole container.
   - The packet then reaches the host's `INPUT` chain, where a LAN-scoped UFW rule for `:53` drops it (it arrives on a bridge interface, not the LAN interface). `[UFW BLOCK] IN=br-… DST=<host> DPT=53` lines had been in the log the whole time.
3. So why had it ever worked? Docker's embedded resolver falls back to a public server when the configured one fails. **Every container had been resolving via `1.1.1.1` since day one.** The restart loop appeared only because that one image's resolver gave up faster after the update.
4. Confirming the scale: Pi-hole's query log showed almost no container clients, which had been explained away as "containers don't do much DNS".

## Root cause

Container DNS was configured to a path that cannot exist (bridge → host LAN IP → published port), and a silent fallback made the misconfiguration look like success for six months.

## Fix

- Pi-hole gets a **static address on the shared Docker bridge** (`172.20.0.253`), with the network's `ip_range` confining dynamic assignment to `172.20.1.0/24` so the static block can never collide.
- `daemon.json` → `"dns": ["172.20.0.253"]`, **no fallback entry**. This is deliberate: a Pi-hole outage must fail loudly for containers rather than be masked.
- A static address is reachable only from its own bridge (Docker isolates bridges), so any container that needs external names must be a member of the shared network. Services that only talk to container names or LAN addresses stay off it. Several monitoring-only containers had no resolver at all; the ones that needed one were joined.
- Convention, now enforced in review: **never set `dns:` to the host's LAN IP in a compose file**, and no per-service `dns:` override should be needed.

## Detection added

Three blackbox **assertion** probes against `172.20.0.253:53` — the exact path every container uses — alerted at 3 m, fail-closed:

| Probe | Asserts | Catches |
|---|---|---|
| `dns_sinkhole` | `doubleclick.net → 0.0.0.0` | Empty gravity, dead blocklist, blocking switched off |
| `dns_local_record` | a local `*.arklab.app` record → host address | `dns.hosts` wiped, FTL not bound to `:53` |
| `dns_recursive` | a DNSSEC-signed public name → its known answer, and *not* `0.0.0.0` | Upstream path broken, or a public name sinkholed by mistake |

Plus a population alert: `pihole_clients_active_1h < 6` for 2 h (baseline ~13, most of them containers) — a client count collapsing is exactly what this fault looks like from Pi-hole's side.

## Lessons

- **A fallback that works is a fault that hides.** The system was "working" by every external measure and doing none of what it was built to do.
- **"Working" is not "working as designed."** Assert the behaviour, not the liveness.
- **Read the firewall log.** The evidence had been there for six months, filed under noise.
