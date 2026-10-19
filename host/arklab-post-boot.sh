#!/bin/bash
# ArkLab post-boot bring-up & health check — /usr/local/bin/arklab-post-boot.sh (root:root 0750)
#
# Design (see docs/post-mortems/03):
#   (1) `docker compose config -q` gate per stack — an invalid compose file is skipped
#       and reported instead of taking the stack down (two house-wide DNS outages came
#       from volume-source typos deployed at boot);
#   (2) `up -d --no-recreate` — boot is NOT a deploy mechanism: an uncommitted compose
#       edit is not shipped unreviewed. Deploys are deliberate;
#   (3) DNS readiness requires a NON-EMPTY answer (dig exits 0 on NXDOMAIN);
#   (4) the verifier fails on restart loops: .State.Restarting, or RestartCount growing
#       across a 30 s window, or >=3 restarts since boot ("Running" is true during a loop).

set -uo pipefail

LOG_FILE="/var/log/arklab-post-boot.log"
REPO_ROOT="/home/arklab/arklab"
MAX_RETRIES=20
RETRY_DELAY=15
DNS_CHECK_NAME="monitor.arklab.app"

# Reverse of shutdown order: DNS first, then infra, then leaf services.
COMPOSE_DIRS=(
    "dns" "nginx-proxy-manager" "monitoring" "homepage" "media" "apps"
)

EXPECTED_CONTAINERS=(
    pihole unbound nginx-proxy-manager
    prometheus node-exporter cadvisor loki promtail grafana uptime-kuma
    blackbox-exporter zyxel-exporter docker-autoheal diun
    homepage
)

TELEGRAM_BOT_TOKEN=""
TELEGRAM_CHAT_ID=""
# shellcheck disable=SC1091
[ -r /etc/arklab/shutdown.env ] && . /etc/arklab/shutdown.env

log() { printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$1" >> "$LOG_FILE"; }

send_telegram() {
    [ -n "$TELEGRAM_BOT_TOKEN" ] && [ -n "$TELEGRAM_CHAT_ID" ] || return 0
    curl -s --max-time 10 -X POST \
        "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
        -d chat_id="${TELEGRAM_CHAT_ID}" -d text="$1" \
        -d parse_mode="Markdown" >/dev/null 2>&1 || true
}

log "========================================="
log "ArkLab Post-Boot Bring-Up"
log "========================================="

log "Waiting for Docker daemon..."
for i in $(seq 1 "$MAX_RETRIES"); do
    if docker info >/dev/null 2>&1; then log "✓ Docker ready (attempt $i)"; break; fi
    sleep "$RETRY_DELAY"
done

if ! docker info >/dev/null 2>&1; then
    log "✗ FATAL: Docker not ready after $((MAX_RETRIES * RETRY_DELAY))s"
    send_telegram "⚠️ *ArkLab Boot Failure*

Docker daemon never became ready."
    exit 1
fi

BRINGUP_FAILED=()
for stack in "${COMPOSE_DIRS[@]}"; do
    dir="${REPO_ROOT}/${stack}"
    if [ ! -f "$dir/docker-compose.yml" ]; then
        log "⚠ Skipping (no compose file): $stack"
        BRINGUP_FAILED+=("$stack:missing")
        continue
    fi

    # Gate: never act on a compose file that does not parse.
    if ! docker compose -f "$dir/docker-compose.yml" --project-directory "$dir" config -q >> "$LOG_FILE" 2>&1; then
        log "✗ Compose file INVALID, stack skipped: $stack"
        BRINGUP_FAILED+=("$stack:config-invalid")
        continue
    fi

    log "Bringing up (no-recreate): $stack"
    timeout 180 docker compose -f "$dir/docker-compose.yml" \
        --project-directory "$dir" up -d --no-recreate >> "$LOG_FILE" 2>&1
    rc=$?
    if [ "$rc" -eq 0 ]; then
        log "✓ Up: $stack"
    else
        log "✗ Failed (rc=$rc): $stack"
        BRINGUP_FAILED+=("$stack:rc-$rc")
    fi

    # DNS must ANSWER (non-empty) before anything that resolves local names starts.
    if [ "$stack" = "dns" ]; then
        dns_ok=0
        for i in $(seq 1 30); do
            ans=$(dig +short +time=2 +tries=1 @127.0.0.1 "$DNS_CHECK_NAME" 2>/dev/null)
            if [ -n "$ans" ]; then log "✓ DNS answering after ${i}s ($DNS_CHECK_NAME → $ans)"; dns_ok=1; break; fi
            sleep 1
        done
        [ "$dns_ok" -eq 1 ] || log "⚠ DNS gave no answer within 30s — continuing, expect resolution failures below"
    fi
done

log "Settling before verification..."
sleep 30

MISSING=()
for c in "${EXPECTED_CONTAINERS[@]}"; do
    if docker ps --format '{{.Names}}' | grep -qx "$c"; then
        log "✓ Running: $c"
    else
        log "✗ Not running: $c"
        MISSING+=("$c")
    fi
done

UNHEALTHY=()
while read -r name health; do
    [ "$health" = "unhealthy" ] && UNHEALTHY+=("$name")
done < <(docker ps -q | xargs -r -I{} docker inspect {} \
    --format '{{.Name}} {{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' \
    | sed 's|^/||')

# Restart-loop detection: "Running" is true during a restart loop. Sample twice.
snapshot() { docker ps -aq | xargs -r -I{} docker inspect {} --format '{{.Name}} {{.RestartCount}} {{.State.Restarting}}' | sed 's|^/||'; }
S1=$(snapshot); sleep 30; S2=$(snapshot)
LOOPING=()
while read -r name count restarting; do
    prev=$(printf '%s\n' "$S1" | awk -v n="$name" '$1==n {print $2}')
    prev=${prev:-$count}
    if [ "$restarting" = "true" ] || [ "$count" -gt "$prev" ] || [ "$count" -ge 3 ]; then
        log "✗ Restart loop: $name (restarts ${prev}→${count}, restarting=$restarting)"
        LOOPING+=("$name:${count}")
    fi
done <<< "$S2"

TOTAL=${#EXPECTED_CONTAINERS[@]}
RUNNING=$((TOTAL - ${#MISSING[@]}))

log "========================================="
log "Summary: ${RUNNING}/${TOTAL} containers running"
[ ${#BRINGUP_FAILED[@]} -gt 0 ] && log "Stack failures: ${BRINGUP_FAILED[*]}"
[ ${#MISSING[@]}        -gt 0 ] && log "Missing: ${MISSING[*]}"
[ ${#UNHEALTHY[@]}      -gt 0 ] && log "Unhealthy: ${UNHEALTHY[*]}"
[ ${#LOOPING[@]}        -gt 0 ] && log "Restart loops: ${LOOPING[*]}"
log "========================================="

if [ ${#MISSING[@]} -eq 0 ] && [ ${#UNHEALTHY[@]} -eq 0 ] && [ ${#BRINGUP_FAILED[@]} -eq 0 ] && [ ${#LOOPING[@]} -eq 0 ]; then
    log "✓ All systems operational"
    send_telegram "✅ *ArkLab Boot Complete*

All ${TOTAL} containers running, healthy, no restart loops.
Uptime: $(uptime -p)
Time: $(date '+%Y-%m-%d %H:%M:%S')"
    exit 0
fi

send_telegram "⚠️ *ArkLab Boot Issues*

Containers: ${RUNNING}/${TOTAL}
Stack failures: ${BRINGUP_FAILED[*]:-none}
Missing: ${MISSING[*]:-none}
Unhealthy: ${UNHEALTHY[*]:-none}
Restart loops: ${LOOPING[*]:-none}
Time: $(date '+%Y-%m-%d %H:%M:%S')"
exit 1
