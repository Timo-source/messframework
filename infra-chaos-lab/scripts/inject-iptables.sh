#!/usr/bin/env bash
# ==============================================================================
# inject-iptables.sh
# Störungs-Simulator für die Seminararbeit (Transiente Netzwerkfehler via Kernel)
#
# Simuliert Paketverlust oder Latenz zur Git-Quelle (GitHub Port 443),
# um transiente Zwischenzustände (ComparisonError / Degraded) zu provozieren.
#
# WICHTIG: Greift sowohl auf OUTPUT (Host-Prozesse) als auch auf FORWARD (K8s-Pods),
# da Kubernetes-Pod-Traffic (ArgoCD repo-server) über die FORWARD-Chain geroutet wird!
# ==============================================================================

set -euo pipefail

# Standard-Parameter
PROBABILITY="${PROBABILITY:-0.80}"  # 80% Paketverlust als Standard-Störung
DURATION="${DURATION:-120}"         # Störungsdauer in Sekunden (Standard: 2 Minuten)
TARGET_PORT="${TARGET_PORT:-443}"   # Zielport (HTTPS / Git-Sync)
TARGET_DOMAIN="github.com"

# Hilfsfunktionen
log_info()  { echo -e "\033[1;34m[INFO]\033[0m $(date '+%Y-%m-%d %H:%M:%S') - $*"; }
log_warn()  { echo -e "\033[1;33m[WARN]\033[0m $(date '+%Y-%m-%d %H:%M:%S') - $*"; }
log_chaos() { echo -e "\033[1;31m[CHAOS]\033[0m $(date '+%Y-%m-%d %H:%M:%S') - $*"; }
log_ok()    { echo -e "\033[1;32m[OK]\033[0m $(date '+%Y-%m-%d %H:%M:%S') - $*"; }

cleanup() {
  log_info "Bereinige Firewall- und Traffic-Control-Regeln..."
  for chain in OUTPUT FORWARD; do
    while iptables -D "$chain" -p tcp --dport "$TARGET_PORT" -m statistic --mode random --probability "$PROBABILITY" -m comment --comment "gitops-chaos" -j DROP 2>/dev/null; do
      log_info "iptables-Drop-Regel aus $chain entfernt."
    done
    iptables -S "$chain" 2>/dev/null | grep "gitops-chaos" | sed 's/-A/-D/' | while read -r rule; do
      eval "iptables $rule" 2>/dev/null || true
    done
  done
  log_ok "Netzwerk vollständig normalisiert. Keine aktiven Störungen mehr."
}

# Trap sorgt dafür, dass bei Skriptabbruch (Ctrl+C, SIGTERM) das Netzwerk wieder sauber ist
trap cleanup EXIT INT TERM

show_status() {
  echo "=== Aktive iptables gitops-chaos Regeln ==="
  iptables -L -v -n --line-numbers | grep -E "Chain|gitops-chaos" || true
  echo ""
  echo "=== TCP Retransmission Statistik (Kernel) ==="
  netstat -s | grep -i retrans || true
}

inject_packet_loss() {
  log_chaos "Starte transiente Störung: ${PROBABILITY} Paketverlust auf Port ${TARGET_PORT} (${TARGET_DOMAIN})"
  log_info "Dauer der Störung: ${DURATION} Sekunden"

  # Regel im Kernel aktivieren:
  # 1. OUTPUT für Host-Prozesse
  # 2. FORWARD für K8s Pods (ArgoCD repo-server)
  for chain in OUTPUT FORWARD; do
    iptables -I "$chain" 1 -p tcp --dport "$TARGET_PORT" \
      -m statistic --mode random --probability "$PROBABILITY" \
      -m comment --comment "gitops-chaos" \
      -j DROP
  done

  log_ok "Kernel-Regeln aktiv (OUTPUT + FORWARD)! Beobachte jetzt den Status in ArgoCD."

  # Countdown mit Live-Paketverlust-Zähler über beide Chains
  local remaining=$DURATION
  while [ $remaining -gt 0 ]; do
    local dropped_packets
    dropped_packets=$(iptables -L -v -n | grep "gitops-chaos" | awk '{s+=$1} END {print s+0}')
    echo -ne "\r\033[1;31m[CHAOS AKTIV]\033[0m Restzeit: ${remaining}s | Bisher verworfene Pakete: ${dropped_packets}  "
    sleep 5
    remaining=$((remaining - 5))
  done
  echo ""

  log_info "Störungszeitfenster abgelaufen. Beginne Erholungsphase..."
}

# CLI-Verarbeitung
case "${1:-inject}" in
  inject)
    inject_packet_loss
    ;;
  cleanup)
    cleanup
    trap - EXIT INT TERM
    exit 0
    ;;
  status)
    show_status
    trap - EXIT INT TERM
    exit 0
    ;;
  help|--help|-h)
    echo "Verwendung: $0 [inject|cleanup|status]"
    echo ""
    echo "Umgebungsvariablen:"
    echo "  PROBABILITY   Wahrscheinlichkeit des Paketverlusts (Standard: 0.80 = 80%)"
    echo "  DURATION      Dauer der Störung in Sekunden (Standard: 120)"
    echo "  TARGET_PORT   Zielport für Paketverlust (Standard: 443 für HTTPS/Git)"
    echo ""
    echo "Beispiel:"
    echo "  PROBABILITY=0.80 DURATION=60 sudo ./inject-iptables.sh inject"
    trap - EXIT INT TERM
    exit 0
    ;;
  *)
    echo "Unbekannter Befehl: $1 (Verwende: inject, cleanup, status)"
    exit 1
    ;;
esac
