# Post-mortem 3 — The shutdown unit that silently never ran

| | |
|---|---|
| **Impact** | For ~3 months every host shutdown killed 20+ containers via dockerd's SIGTERM → 15 s → SIGKILL instead of a dependency-ordered `compose down`. No data loss observed; the exposure was real |
| **Duration** | Undetected from June to October; found during a reboot audit |
| **Class** | systemd semantics — a unit that looks correct and never executes |

## Symptom

None. That is the whole point of this write-up. The shutdown log file simply had no entries after a certain date, and the Telegram "shutdown initiated" message had not been seen in a while. Nobody noticed, because nothing is expected to happen on a clean reboot except for the lab coming back.

## Investigation

`journalctl -b -1 -u arklab-shutdown` for the previous boot: empty. Not "failed" — **absent**. The unit had never been started during that boot's shutdown sequence.

The unit looked like this:

```ini
[Unit]
Requires=docker.service
[Service]
Type=oneshot
ExecStart=/usr/local/bin/arklab-shutdown.sh
[Install]
WantedBy=shutdown.target
```

The trap: at shutdown, systemd queues a **stop** job for `docker.service`. The unit's **start** job `Requires=docker.service`, which means it needs a start job for Docker — and that start job *conflicts* with the already-queued stop job. systemd resolves the conflict by cancelling the unit's start job. **No journal line is written for a cancelled job.** The script never ran, and nothing said so.

It had worked once, at the boot where it was first enabled, which is why it was believed to work.

## Root cause

`ExecStart` + `WantedBy=shutdown.target` + `Requires=docker.service` is a known anti-pattern for "run something before Docker stops". The correct shape runs the work in `ExecStop`.

## Fix

[`host/arklab-shutdown.service`](../../host/arklab-shutdown.service):

```ini
[Unit]
After=docker.service network-online.target
Requires=docker.service
[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/bin/true
ExecStop=/usr/local/bin/arklab-shutdown.sh
TimeoutStopSec=22min
[Install]
WantedBy=multi-user.target
```

The unit is *active for the whole uptime*. At shutdown systemd **stops** it, and because stop order is the reverse of start order (`After=docker.service`), it is stopped **before** Docker. `TimeoutStopSec` is raised because the default 90 s would kill the script mid-run across ten stacks.

Verification is now part of the reboot runbook: `systemctl is-active arklab-shutdown` must say `active` during normal operation, and after a reboot `journalctl -b -1 -u arklab-shutdown` must show `Stopping … Deactivated successfully`.

## The boot side

The same audit hardened the post-boot counterpart, [`host/arklab-post-boot.sh`](../../host/arklab-post-boot.sh), after two earlier incidents where a bad compose file was deployed at boot and took house-wide DNS down:

- **`docker compose config -q` gate per stack** — an invalid file is skipped and reported, never acted on.
- **`up -d --no-recreate`** — boot is not a deploy mechanism. An uncommitted compose edit is not shipped unreviewed; deploys are deliberate.
- **DNS readiness requires a non-empty answer** — `dig` exits 0 on NXDOMAIN, so exit status alone proved nothing.
- **Restart-loop detection** — `docker ps` reports `Up` during a restart loop. The verifier samples `RestartCount` twice, 30 s apart, and fails on `.State.Restarting`, a growing count, or ≥ 3 restarts since boot.

The two scripts cover all stacks in dependency order: leaf services → infrastructure → DNS on shutdown, reversed on boot. Both run root-owned and source their notification credentials from a mode-600 file outside the repo — an earlier version had the token inline, which is the "early mistake" referenced in the README and the reason this public mirror starts with fresh history.

## Lessons

- **Every failure in this lab so far has been silent.** The two questions for any automated job: does it log, and does anything alert when it stops?
- **"It ran once" is not "it runs."** The first execution happened under different job-ordering conditions than every subsequent one.
- **Verify by content, not by exit status** — `dig` exit 0, `install` exit 0 and `docker ps` saying `Up` all proved less than they appeared to.
