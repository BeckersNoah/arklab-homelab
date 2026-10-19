#!/usr/bin/env bash
# Pi-hole blocklist + blocking-efficacy metrics -> Prometheus textfile collector.
# Runs hourly from cron as the operator user (docker group).
# Writes atomically. On ANY failure, exits non-zero WITHOUT touching the output
# file, so pihole_metrics_last_run_seconds goes stale and alerts. Never emit
# partial data — a zero that looks like a measurement is worse than no metric.
set -euo pipefail

OUT_DIR="/home/arklab/arklab-data/node-exporter/textfile"
OUT="${OUT_DIR}/pihole.prom"
TMP="$(mktemp "${OUT_DIR}/.pihole.prom.XXXXXX")"
trap 'rm -f "$TMP"' EXIT

gq() { docker exec pihole pihole-FTL sqlite3 /etc/pihole/gravity.db    "$1"; }
fq() { docker exec pihole pihole-FTL sqlite3 /etc/pihole/pihole-FTL.db "$1"; }
num() { [[ "${1:-}" =~ ^[0-9]+$ ]] || { echo "non-numeric value: '${1:-}'" >&2; exit 1; }; }

# FTL v6 notes: `queries` is a VIEW with client/domain as text; blocked statuses
# are exactly 1,4,5,6,7,9,10,11; COUNT(*) on gravity counts raw rows, so DISTINCT.
GRAVITY=$(gq   "SELECT COUNT(DISTINCT domain) FROM gravity;")
ADLISTS=$(gq   "SELECT COUNT(*) FROM adlist WHERE enabled=1;")
UNHEALTHY=$(gq "SELECT COUNT(*) FROM adlist WHERE enabled=1 AND (status>=3 OR number=0);")
LASTUPD=$(gq   "SELECT COALESCE(MAX(date_updated),0) FROM adlist WHERE enabled=1;")
ALLOWED=$(gq   "SELECT COUNT(*) FROM domainlist WHERE type IN (0,2) AND enabled=1;")

CLIENTS=$(fq "SELECT COUNT(DISTINCT client) FROM queries WHERE timestamp > strftime('%s','now','-1 hour');")
QUERIES=$(fq "SELECT COUNT(*) FROM queries WHERE timestamp > strftime('%s','now','-1 hour');")
BLOCKED=$(fq "SELECT COUNT(*) FROM queries WHERE status IN (1,4,5,6,7,9,10,11) AND timestamp > strftime('%s','now','-1 hour');")

for v in "$GRAVITY" "$ADLISTS" "$UNHEALTHY" "$LASTUPD" "$ALLOWED" "$CLIENTS" "$QUERIES" "$BLOCKED"; do
  num "$v"
done

{
  echo '# HELP pihole_gravity_domains Unique domains in the gravity database.'
  echo '# TYPE pihole_gravity_domains gauge'
  echo "pihole_gravity_domains ${GRAVITY}"

  echo '# HELP pihole_adlist_enabled_total Enabled adlists.'
  echo '# TYPE pihole_adlist_enabled_total gauge'
  echo "pihole_adlist_enabled_total ${ADLISTS}"

  echo '# HELP pihole_adlist_unhealthy Enabled adlists unavailable (status>=3) or parsing to zero domains.'
  echo '# TYPE pihole_adlist_unhealthy gauge'
  echo "pihole_adlist_unhealthy ${UNHEALTHY}"

  echo '# HELP pihole_adlist_last_update_seconds Newest adlist date_updated, unix epoch.'
  echo '# TYPE pihole_adlist_last_update_seconds gauge'
  echo "pihole_adlist_last_update_seconds ${LASTUPD}"

  echo '# HELP pihole_allowlist_entries Enabled exact + regex allowlist entries.'
  echo '# TYPE pihole_allowlist_entries gauge'
  echo "pihole_allowlist_entries ${ALLOWED}"

  echo '# HELP pihole_clients_active_1h Distinct clients querying in the last hour.'
  echo '# TYPE pihole_clients_active_1h gauge'
  echo "pihole_clients_active_1h ${CLIENTS}"

  echo '# HELP pihole_queries_1h Total queries in the last hour.'
  echo '# TYPE pihole_queries_1h gauge'
  echo "pihole_queries_1h ${QUERIES}"

  echo '# HELP pihole_blocked_1h Blocked queries in the last hour.'
  echo '# TYPE pihole_blocked_1h gauge'
  echo "pihole_blocked_1h ${BLOCKED}"

  echo '# HELP pihole_adlist_domains Domains contributed per adlist.'
  echo '# TYPE pihole_adlist_domains gauge'
  echo '# HELP pihole_adlist_status Adlist status: 1 ok, 2 unchanged/local copy, 3-4 unavailable.'
  echo '# TYPE pihole_adlist_status gauge'
  while IFS='|' read -r id number status; do
    [[ -n "${id:-}" ]] || continue
    printf 'pihole_adlist_domains{adlist_id="%s"} %s\n' "$id" "$number"
    printf 'pihole_adlist_status{adlist_id="%s"} %s\n'  "$id" "$status"
  done < <(gq "SELECT id, number, status FROM adlist WHERE enabled=1 ORDER BY id;")

  echo '# HELP pihole_metrics_last_run_seconds Unix time this exporter last completed successfully.'
  echo '# TYPE pihole_metrics_last_run_seconds gauge'
  echo "pihole_metrics_last_run_seconds $(date +%s)"
} > "$TMP"

[[ -s "$TMP" ]] || { echo "empty output, refusing to publish" >&2; exit 1; }
chmod 644 "$TMP"
mv "$TMP" "$OUT"
trap - EXIT
