# Design principles and conventions

These are the rules the lab is run by. Most of them were paid for.

## Compose files

- **Volume sources are absolute paths. Full stop.** No `~` (Compose creates a literal `~/` directory), no `${HOME}` (empty when the post-boot service runs `up -d` as root). Both have caused house-wide DNS outages. CI refuses them.
- **Image tags are pinned.** No `:latest` except a digest pin where the publisher publishes no version tags. Diun reports newer tags; changing a pin is a deliberate commit, followed by `pull && up -d` for that stack and an edit to the watch list.
- **Validate before deploying:** `docker compose config -q && echo VALID`, and `config | grep -iE 'source:|target:'` to eyeball mounts.
- **A running container is not evidence its compose file is valid.** Mounts and environment resolve at creation; divergence surfaces at the next recreate — usually a reboot.
- **Commit or revert compose edits immediately.** A dirty tree means disk ≠ repo; `git status` is part of every reboot pre-flight.
- **`depends_on` works only within one project.** Cross-stack ordering lives in the boot/shutdown scripts, or in reconnection logic inside the service.
- **Never publish a port you don't need.** Published ports bypass the host firewall. Bind to `127.0.0.1:` when only the host needs it.
- **Never set `dns:` to the host's LAN IP.** Container DNS comes from `daemon.json` → the resolver's static bridge address.

## Healthchecks

A probe must pass when the service works **and** fail when it doesn't. Both halves have been violated here: a resolver healthcheck that depended on the WAN (too aggressive), a bot healthcheck that only stat'ed a SQLite file (reported healthy through a 13-day outage), an exporter healthcheck that could never pass (`localhost` → `::1` while binding IPv4).

- Pin probes to `127.0.0.1`. Container `localhost` resolves to `::1` even with IPv6 disabled on the host.
- Keep probes local: the resolver probes its built-in `localhost` zone; the bot probes the age of a heartbeat file touched by its main loop.
- Auto-restart (`docker-autoheal`) is **opt-in by label**, and the DNS stack is never labelled — restarting the resolver is a network-wide outage.
- `RestartCount` tracks Docker's restart *policy* only. API-initiated restarts (autoheal) leave it at 0 while a container bounces 24×/hour.

## Metrics and alerting

- **Fail-closed.** Detection rules evaluate `NoData` as `Alerting`. A missing metric means the thing producing it has died.
- **Never publish partial data.** Cron exporters write to a temp file in the same directory and `rename` it into place; on any failure the previous file is left untouched and `*_last_run_seconds` goes stale. A zero that looks like a measurement is worse than no metric.
- **Alert rules live in the repo** and are provisioned read-only into Grafana. Retiring a rule needs an explicit `deleteRules:` entry — removing the file alone leaves the rule in the database.
- **Instrument the cause and the effect** when a fault has both.
- **A probe answered from cache measures the cache.**

## Secrets

- `.env` files mode 600, owned by the operator, **never committed**. Root-owned scripts source `/etc/arklab/*.env` (600). Never inline a credential in `/usr/local/bin`.
- An unreadable `.env` under `docker compose` silently interpolates empty strings. Gate container recreation on a readability check *without* sudo.
- Validate the format, refuse empty, **then** write. A helper once wrote `TOKEN=` and exited 0.
- Never put a secret on a `sudo` command line — argv lands in `auth.log`, which ships to Loki. Search for secrets by variable *name*, never by value.
- Audit every new HTTP client's log level before deploying. One library logged the full API URL, token included, six times a minute for weeks. Log shipping now redacts the pattern as defence in depth, not as the control.
- Bracketed paste corrupts interactive secret entry: disable it (`printf '\e[?2004l'`) and strip only whitespace, so paste artefacts fail loudly.

## Privilege

- The operator account has full sudo **with password** and exactly one `NOPASSWD` grant: the certificate reload hook. Never `NOPASSWD` a general-purpose tool — `cp`/`tee` were once granted, which is a one-command root escalation.
- `docker` group membership is root-equivalent; treat it as such.
- Custom host daemons run as an unprivileged user with a single ambient capability under a hardened systemd sandbox.
- Privileged commands run with unprivileged shell semantics: `cd`, globs and redirections are evaluated before `sudo`. Use `sudo find … -exec` or `sudo bash -c '…'`.

## Debugging

- Every failure in this lab so far has been silent. Does it log, and does anything alert when it stops?
- Verify by content, not by exit status.
- Capture forensics before any container recreation — `compose up -d` destroys container logs. A credential rotation once destroyed the log sample behind an open investigation.
- Validate before writing, never after.
- `curl -k` hides certificate faults; verification loops omit it so an expired or undeployed cert fails loudly.
- After `nginx -s reload`, wait. Old workers keep serving in-flight connections; an immediate check reads the pre-reload state.
- Multi-line snippets go in a file, never the prompt — and in `~`, not `/tmp`, if they must survive a reboot.
