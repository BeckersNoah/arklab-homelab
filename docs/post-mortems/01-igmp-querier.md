# Post-mortem 1 — The LAN had no IGMP querier

| | |
|---|---|
| **Impact** | A WiFi smart speaker became undiscoverable (mDNS) a few hours after joining the network, repeatedly, for weeks. Cloud control kept working, so the symptom looked like an app bug |
| **Duration** | Intermittent for ~6 weeks; root cause proven and fixed in one session |
| **Class** | Network — multicast delivery |

## Symptom

Ping to the speaker: fine. A unicast mDNS query to its address: answered. A multicast mDNS query to `224.0.0.251`: **no reply**. The speaker's own app, which talks through the vendor cloud, worked throughout — which is why "it's the speaker" survived as a hypothesis far too long. Power-cycling the speaker fixed it for a few hours.

## Investigation

1. Ruled out the obvious: the speaker had been moved from 2.4 GHz to 5 GHz, the channel pinned, the router's "smart" features (EasyMesh, band steering) off. No change.
2. `tcpdump -i <lan-iface> igmp` on the wired host showed the router joining `224.0.0.106` — the *all-snoopers* group (RFC 4286). So the router **snoops IGMP** and only forwards a multicast group to ports where it has seen a membership report.
3. The same capture over 10 minutes showed **zero IGMP General Queries** from anyone. The router snoops but has no querier of its own, and a consumer LAN has no multicast router to provide one.
4. Without periodic queries, a station's group membership in the snooping table ages out (~260 s) unless the station re-reports on its own. The speaker reports **once, at join**, and then never again — a perfectly legal implementation that goes deaf behind a querier-less snooper.
5. Proof: a single hand-crafted IGMPv2 General Query sent from the host. The speaker re-announced its groups within 4 seconds and discovery was restored.

## Root cause

IGMP snooping with no querier on the segment. Group state expired, multicast to the speaker was silently dropped, unicast kept working.

## Fix

[`host/igmp-querier.py`](../../host/igmp-querier.py) — an RFC 2236 General Querier running on the wired host:

- General Query to `224.0.0.1` every 125 s (the RFC default), two startup queries 31 s apart, Router Alert IP option set.
- **Querier election (§8.5):** a query from a lower IP makes it passive for the Other Querier Present Interval (255 s); it resumes if that querier falls silent.
- **Receive is bound to the LAN interface**, so a container on a Docker bridge (which also sees multicast) can never win the election and silence the real querier.
- Runs as an unprivileged user with exactly `CAP_NET_RAW`, under `ProtectSystem=strict`, `SystemCallFilter=@system-service`, `RestrictAddressFamilies=AF_INET` — see [`host/igmp-querier.service`](../../host/igmp-querier.service).
- Logs nothing per query. The metric is the heartbeat.

## Detection added

Two sensors, because the fault has a cause and an effect and either can regress alone:

| Sensor | Mechanism | Alert |
|---|---|---|
| Cause: querier alive | Textfile metric `igmp_querier_last_query_seconds`, written atomically by the daemon | `time() − last_query > 600 s` for 5 m, **NoData → Alerting** |
| Effect: speaker discoverable | [`monitoring/scripts/mdns-probe.py`](../../monitoring/scripts/mdns-probe.py) every 5 min: PTR queries to the multicast group from an *ephemeral* port, so responders reply legacy-unicast (RFC 6762 §6.7). This exercises the wired → wireless multicast delivery path a unicast probe cannot see | `discoverable < 1` for 15 m; probe-cron-stale > 15 m; both fail-closed |

The probe's replies are unicast from an unsolicited source, so a default-deny firewall drops them silently (visible in `tcpdump`, invisible to the socket). Two narrowly scoped rules allow `udp sport 5353` and `udp sport 1900` from the LAN /24 on the LAN interface only.

## Lessons

- **A symptom that the vendor's cloud masks is still a network fault.** "The app works" told us nothing about the LAN.
- **Capture during the fault.** One red probe run mid-incident outweighs a week of green. The runbook now says: run the probe by hand *while* it is broken.
- **Instrument the cause and the effect.** A healthy querier with a dead delivery path, or a dead querier with a lucky speaker, are both states we want to see.
