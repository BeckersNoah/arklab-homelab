#!/bin/bash
# ArkLab graceful shutdown — /usr/local/bin/arklab-shutdown.sh (root:root 0750)
# Secrets: /etc/arklab/shutdown.env (0600) — never inline a credential here.
# `set -e` omitted deliberately: one failing stack must not abort the loop and
# leave later stacks running. Exit codes are captured from the command, never
# after a pipe.

set -uo pipefail

LOG_FILE="/var/log/arklab-shutdown.log"
REPO_ROOT="/home/arklab/arklab"

# Leaf services first, infrastructure next, DNS last.
COMPOSE_DIRS=(
    "apps" "media" "homepage" "monitoring" "nginx-proxy-manager" "dns"
)

MAX_WAIT_TIME=120
GRACEFUL_TIMEOUT=90
FORCE_TIMEOUT=10

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

if [ "$EUID" -ne 0 ]; then log "ERROR: must run as root"; exit 1; fi

log "========================================="
log "ArkLab Graceful Shutdown Initiated"
log "========================================="
send_telegram "🚨 *ArkLab Shutdown Initiated*

Stopping ${#COMPOSE_DIRS[@]} stacks in dependency order.
Time: $(date '+%Y-%m-%d %H:%M:%S')"

FAILED_STACKS=()
for stack in "${COMPOSE_DIRS[@]}"; do
    dir="${REPO_ROOT}/${stack}"
    if [ ! -f "$dir/docker-compose.yml" ]; then
        log "⚠ Skipping (no compose file): $stack"
        FAILED_STACKS+=("$stack:missing")
        continue
    fi

    log "Stopping stack: $stack"
    timeout "$MAX_WAIT_TIME" docker compose -f "$dir/docker-compose.yml" \
        --project-directory "$dir" down --timeout "$GRACEFUL_TIMEOUT" >> "$LOG_FILE" 2>&1
    rc=$?

    if [ "$rc" -eq 0 ]; then
        log "✓ Stopped cleanly: $stack"
    elif [ "$rc" -eq 124 ]; then
        log "⚠ Timeout after ${MAX_WAIT_TIME}s: $stack — forcing"
        timeout 60 docker compose -f "$dir/docker-compose.yml" \
            --project-directory "$dir" down --timeout "$FORCE_TIMEOUT" >> "$LOG_FILE" 2>&1
        frc=$?
        if [ "$frc" -eq 0 ]; then
            log "✓ Force-stopped: $stack"
        else
            log "✗ Force stop failed (rc=$frc): $stack"
            FAILED_STACKS+=("$stack:force-rc-$frc")
        fi
    else
        log "✗ Error stopping (rc=$rc): $stack"
        FAILED_STACKS+=("$stack:rc-$rc")
    fi
done

log "Waiting for containers to settle..."
sleep 5

RUNNING=$(docker ps -q | wc -l)
if [ "$RUNNING" -gt 0 ]; then
    log "⚠ $RUNNING container(s) still running — force stopping"
    docker ps --format '{{.Names}}\t{{.Status}}' >> "$LOG_FILE" 2>&1
    docker ps -q | xargs -r docker stop --time 30 >> "$LOG_FILE" 2>&1 || true
    sleep 3
    REMAINING=$(docker ps -q | wc -l)
    if [ "$REMAINING" -gt 0 ]; then
        log "✗ $REMAINING container(s) unstoppable — data integrity at risk"
        docker ps --format '{{.Names}}' >> "$LOG_FILE" 2>&1
        send_telegram "⚠️ *ArkLab Shutdown Warning*

$REMAINING container(s) could not be stopped.
System will still power off — check
/var/log/arklab-shutdown.log after boot."
    fi
fi

log "Syncing filesystems..."
sync
sleep 2

if [ "${#FAILED_STACKS[@]}" -eq 0 ]; then
    log "✓ All stacks stopped cleanly"
    send_telegram "✅ *ArkLab Shutdown Complete*

All ${#COMPOSE_DIRS[@]} stacks stopped cleanly.
Time: $(date '+%Y-%m-%d %H:%M:%S')"
else
    log "⚠ Completed with issues: ${FAILED_STACKS[*]}"
    send_telegram "⚠️ *ArkLab Shutdown Completed With Issues*

Problem stacks: ${FAILED_STACKS[*]}
Time: $(date '+%Y-%m-%d %H:%M:%S')"
fi

log "========================================="
log "Graceful Shutdown Complete"
log "========================================="
# Always exit 0 — a non-zero ExecStop cannot abort a systemd shutdown and only
# leaves the unit failed after boot.
exit 0
