# Post-mortem 2 — Full recursion on a lossy cellular link

| | |
|---|---|
| **Impact** | Intermittent `ERR_NAME_NOT_RESOLVED` across every device in the house, concentrated in evening and night hours |
| **Duration** | ~3 weeks of intermittent symptoms; one evening to root-cause once the right counters were looked at |
| **Class** | DNS — resolver architecture vs. link characteristics |

## Context

The lab's DNS path is `client → Pi-hole (sinkhole, local records) → Unbound → upstream`. Unbound was configured as a **full recursive resolver**: root → TLD → authoritative, DNSSEC-validated, with QNAME minimisation. That is the textbook privacy-maximising setup, and it had been fine on fibre.

The WAN is 5G behind CGNAT. Blackbox ICMP probes (every 15 s, to the gateway *and* to two public resolvers) showed something the gateway probe did not: **5-second loss windows and 1–2 s RTT bursts** to the public targets in some evening hours while the LAN hop stayed flat. Carrier-side, not self-inflicted — the only heavy downloader on the network was idle during the events.

## Investigation

1. `unbound-control stats_noreset` showed `requestlist.exceeded` at **~940/day** — queries dropped because the pending-request list was full.
2. Container logs at `verbosity 1` / `log-servfail yes` were full of `ratelimit exceeded com. 1000`: the **per-zone rate limiter** was tripping on `.com`. That limiter exists to stop recursive floods; here it was being tripped by Unbound's own retries.
3. The mechanics: a cold recursive lookup is 3–5 independent UDP exchanges (root, TLD, authoritative, plus DNSKEY/DS for validation), averaging **335 ms** on this link when it was healthy. During a 5 s loss window every in-flight exchange times out, every client retries, and Unbound fans out again — a retry storm against the TLD servers that the rate limiter correctly refused, answering SERVFAIL.
4. Measured in the bad hours: **61–68 % of forwarded queries went unanswered**. Serve-expired masked some of it; new names had no cached answer to serve.

## Root cause

Resolver design mismatched to the link. Full recursion multiplies the number of independent UDP round trips per lookup, and a bursty-loss link punishes exactly that.

## Fix

- `forward-first: yes` to Quad9 over **DNS-over-TLS** (`forward-tls-upstream: yes`, `tls-cert-bundle` set). One persistent TCP/TLS session, one round trip per lookup, and TCP retransmission absorbs the loss windows that UDP turned into timeouts.
- **Recursion is kept as the fallback** (`forward-first`), so an upstream outage degrades to the old behaviour rather than to nothing.
- **DNSSEC is still validated locally** — verified by `dnssec-failed.org` returning SERVFAIL through the full path. Forwarding changes who performs the lookup, not who checks the signatures.
- `module-config: "validator iterator"` (the image default adds `subnetcache`/ECS, which is unwanted on a household resolver and degrades `serve-expired` and `prefetch`).
- Result: `requestlist.exceeded` from ~940/day to ~25/day. Still watched weekly.

Two older decisions in the same stack are worth recording because they are what kept the house *partially* online during this and during a 15-minute Unbound crash loop:

- **Pi-hole's upstream is the literal IP of Unbound, not its hostname**, and `depends_on` is deliberately absent. dnsmasq resolves its upstream once at config-parse time; with a hostname, an Unbound failure used to escalate into FTL never binding `:53` at all. With the literal IP, Pi-hole stays up and serves cache + local records while Unbound is down.
- **Unbound is reachable only from Pi-hole's private network**, via two independent layers (not attached to the shared bridge, *and* `access-control` limited to that subnet). Nothing can resolve past Pi-hole's filtering and logging.

## Detection added

- `probe_success{job="blackbox-dns-recursive"}` — a DNSSEC-signed public name resolved through the full path, alerted at 3 m fail-closed.
- **Known gap, on the roadmap:** that probe's name is kept warm by `prefetch`/`serve-expired`, so it stayed green through this very outage. The replacement is a cron'd *cold* lookup (`r$RANDOM.<signed-zone>` → NXDOMAIN with an AUTHORITY section means the upstream path works) written to the textfile collector, fail-closed.

## Lessons

- **Measure the link before choosing the resolver.** The right DNS architecture is a function of loss and latency distribution, not of ideology.
- **A resolver's own retries can be its denial-of-service.** Rate limiters do their job; make sure the load they are refusing isn't yours.
- **A probe answered from cache measures the cache.** Every "is the upstream path alive" check must bypass it.
