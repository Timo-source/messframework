#!/usr/bin/env bash
# Kernel-Fehlerinjektor (FORWARD-Chain, ausgehender TCP-Port 443 fuer K8s-Pods)
set -euo pipefail

PROBABILITY="${PROBABILITY:-0.80}"
DURATION="${DURATION:-120}"
TARGET_PORT="${TARGET_PORT:-443}"

log() { echo "[$1] $(date -u '+%Y-%m-%dT%H:%M:%SZ') - $2"; }

cleanup() {
  trap - EXIT INT TERM
  local final_drops
  final_drops=$(iptables -L FORWARD -v -n -x 2>/dev/null | awk '/gitops-chaos/ {s+=$1} END {print s+0}')
  echo "FINAL_DROPS=${final_drops:-0}"

  while IFS= read -r rule; do
    [ -n "$rule" ] && eval "iptables ${rule/-A/-D}" 2>/dev/null || true
  done < <(iptables -S FORWARD 2>/dev/null | grep "gitops-chaos" || true)

  log "OK" "FORWARD-Regeln bereinigt (FINAL_DROPS=${final_drops:-0})."
}

inject_packet_loss() {
  trap cleanup EXIT INT TERM

  if LC_ALL=C awk "BEGIN {exit !($PROBABILITY >= 1.0)}"; then
    iptables -I FORWARD 1 -p tcp --dport "$TARGET_PORT" \
      -m comment --comment "gitops-chaos" -j DROP
  else
    iptables -I FORWARD 1 -p tcp --dport "$TARGET_PORT" \
      -m statistic --mode random --probability "$PROBABILITY" \
      -m comment --comment "gitops-chaos" -j DROP
  fi

  log "CHAOS" "FORWARD-Drop aktiv: p=${PROBABILITY}, port=${TARGET_PORT}, duration=${DURATION}s"

  local remaining="$DURATION"
  while [ "$remaining" -gt 0 ]; do
    local step=$((remaining < 5 ? remaining : 5))
    sleep "$step"
    remaining=$((remaining - step))
  done
}

case "${1:-inject}" in
  inject)  inject_packet_loss ;;
  cleanup) cleanup ;;
  *)
    echo "Verwendung: $0 [inject|cleanup]" >&2
    exit 1
    ;;
esac
