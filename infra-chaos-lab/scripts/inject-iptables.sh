#!/usr/bin/env bash
# Kernel-Fehlerinjektor (mangle/FORWARD-Chain, ausgehender TCP-Port 443 fuer K8s-Pods)
set -euo pipefail

PROBABILITY="${PROBABILITY:-0.80}"
DURATION="${DURATION:-120}"
TARGET_PORT="${TARGET_PORT:-443}"
SLEEP_PID=""

log() { echo "[$1] $(date -u '+%Y-%m-%dT%H:%M:%SZ') - $2"; }

cleanup() {
  trap - EXIT INT TERM
  [ -n "$SLEEP_PID" ] && kill "$SLEEP_PID" 2>/dev/null || true
  SLEEP_PID=""

  local final_drops
  final_drops=$(iptables -t mangle -L FORWARD -v -n -x 2>/dev/null | awk '/gitops-chaos/ {if ($1+0 > m) m=$1+0} END {print m+0}')
  if [ "${final_drops:-0}" -eq 0 ]; then
    final_drops=$(iptables -t filter -L FORWARD -v -n -x 2>/dev/null | awk '/gitops-chaos/ {if ($1+0 > m) m=$1+0} END {print m+0}')
  fi
  echo "FINAL_DROPS=${final_drops:-0}"

  local tbl
  for tbl in mangle filter; do
    while IFS= read -r rule; do
      [ -n "$rule" ] && eval "iptables -t $tbl ${rule/-A/-D}" 2>/dev/null || true
    done < <(iptables -t "$tbl" -S FORWARD 2>/dev/null | grep "gitops-chaos" || true)
  done

  log "OK" "mangle/filter FORWARD-Regeln bereinigt (FINAL_DROPS=${final_drops:-0})."
}

inject_packet_loss() {
  trap cleanup EXIT INT TERM

  if LC_ALL=C awk "BEGIN {exit !($PROBABILITY >= 1.0)}"; then
    iptables -t mangle -I FORWARD 1 -p tcp --dport "$TARGET_PORT" \
      -m comment --comment "gitops-chaos" -j DROP
  else
    iptables -t mangle -I FORWARD 1 -p tcp --dport "$TARGET_PORT" \
      -m statistic --mode random --probability "$PROBABILITY" \
      -m comment --comment "gitops-chaos" -j DROP
  fi

  log "CHAOS" "mangle/FORWARD-Drop aktiv: p=${PROBABILITY}, port=${TARGET_PORT}, duration=${DURATION}s"

  sleep "$DURATION" &
  SLEEP_PID=$!
  wait "$SLEEP_PID" || true
}

case "${1:-inject}" in
  inject)  inject_packet_loss ;;
  cleanup) cleanup ;;
  *)
    echo "Verwendung: $0 [inject|cleanup]" >&2
    exit 1
    ;;
esac
