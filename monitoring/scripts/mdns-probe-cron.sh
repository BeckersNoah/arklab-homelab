#!/usr/bin/env bash
# Cron wrapper (every 5 min): run the mDNS probe, publish a fail-closed metric.
# Writes atomically. On a probe crash (exit 2) the file is left untouched so
# arklab_mdns_probe_last_run_seconds goes stale and alerts.
set -uo pipefail

OUT_DIR="/home/arklab/arklab-data/node-exporter/textfile"
OUT="${OUT_DIR}/mdns-speaker.prom"
TMP="$(mktemp "${OUT_DIR}/.mdns.prom.XXXXXX")"
trap 'rm -f "$TMP"' EXIT

# shellcheck disable=SC1091
[ -r /home/arklab/arklab-data/mdns-probe.env ] && . /home/arklab/arklab-data/mdns-probe.env
export MDNS_TARGET_IP

python3 /home/arklab/arklab/monitoring/scripts/mdns-probe.py >/dev/null 2>&1
rc=$?
[ "$rc" -le 1 ] || { echo "probe crashed (rc=$rc), not publishing" >&2; exit 1; }
disc=$(( rc == 0 ? 1 : 0 ))

{
  echo '# HELP arklab_mdns_speaker_discoverable 1 if the speaker answered every mDNS PTR query over the multicast path.'
  echo '# TYPE arklab_mdns_speaker_discoverable gauge'
  echo "arklab_mdns_speaker_discoverable ${disc}"
  echo '# HELP arklab_mdns_probe_last_run_seconds Unix time the probe last completed.'
  echo '# TYPE arklab_mdns_probe_last_run_seconds gauge'
  echo "arklab_mdns_probe_last_run_seconds $(date +%s)"
} > "$TMP"
chmod 644 "$TMP"
mv "$TMP" "$OUT"
trap - EXIT
