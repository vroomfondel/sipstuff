#!/usr/bin/env bash
# ==============================================================
# PJSIP Stack – Asterisk + Realtime TTS Client
# ==============================================================
# Wrapper für podman kube play mit pjsip-stack.yaml.
#
# Verwendung:
#   ./stack.sh start   – Pod + Container starten
#   ./stack.sh stop    – Pod + Container entfernen
#   ./stack.sh logs    – Logs aller Container anzeigen
#   ./stack.sh status  – Status des Pods anzeigen
#   ./stack.sh exec    – Asterisk CLI öffnen
# ==============================================================

set -euo pipefail

# ---- Konfiguration -------------------------------------------

POD_NAME="pjsip-stack"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

ASTERISK_NAME="${POD_NAME}-asterisk"
TTS_NAME="${POD_NAME}-pjsip-tts"

# Piper TTS (für Pre-Flight-Check)
PIPER_MODEL_DIR="${SCRIPT_DIR}/../sipstuff_data.local/piper-models"
PIPER_MODEL_NAME="de_DE-thorsten-high.onnx"

SIP_DOMAIN="127.0.0.1"

# ---- Hilfsfunktionen -----------------------------------------

require_model() {
    if [[ ! -f "${PIPER_MODEL_DIR}/${PIPER_MODEL_NAME}" ]]; then
        echo "FEHLER: Piper-Modell nicht gefunden: ${PIPER_MODEL_DIR}/${PIPER_MODEL_NAME}"
        echo ""
        echo "Modell herunterladen:"
        echo "  mkdir -p ${PIPER_MODEL_DIR}"
        echo "  wget -P ${PIPER_MODEL_DIR}/ \\"
        echo "    https://huggingface.co/rhasspy/piper-voices/resolve/main/de/de_DE/thorsten/high/de_DE-thorsten-high.onnx"
        echo "  wget -P ${PIPER_MODEL_DIR}/ \\"
        echo "    https://huggingface.co/rhasspy/piper-voices/resolve/main/de/de_DE/thorsten/high/de_DE-thorsten-high.onnx.json"
        exit 1
    fi
}

wait_for_asterisk() {
    echo "Warte auf Asterisk (${SIP_DOMAIN}:5060)..."
    local max_retries=30
    local retry=0
    while [[ $retry -lt $max_retries ]]; do
        if timeout 2 bash -c "echo > /dev/tcp/${SIP_DOMAIN}/5060" 2>/dev/null; then
            echo "Asterisk erreichbar!"
            return 0
        fi
        retry=$((retry + 1))
        echo "  Warte... ($retry/$max_retries)"
        sleep 2
    done
    echo "WARNUNG: Asterisk nicht erreichbar nach ${max_retries} Versuchen."
}

# ---- Subcommands ---------------------------------------------

cmd_start() {
    require_model

    echo "=== Starte PJSIP Stack via kube play ==="
    cd "$SCRIPT_DIR"
    podman kube play pjsip-stack.yaml --replace

    wait_for_asterisk

    echo ""
    echo "=== Stack gestartet ==="
    echo "  Pod:       ${POD_NAME}"
    echo "  Asterisk:  ${ASTERISK_NAME}"
    echo "  TTS:       ${TTS_NAME}"
    echo ""
    echo "Softphone mit 1002/geheim1002 registrieren, dann 1001 anrufen."
}

cmd_stop() {
    echo "=== Stoppe PJSIP Stack ==="
    cd "$SCRIPT_DIR"
    podman kube down pjsip-stack.yaml 2>/dev/null || true
    echo "Pod entfernt."
}

cmd_logs() {
    local target="${1:-all}"
    case "$target" in
        asterisk) podman logs -f "$ASTERISK_NAME" ;;
        tts)      podman logs -f "$TTS_NAME" ;;
        all)      podman pod logs -f "$POD_NAME" ;;
        *)        echo "Verwendung: $0 logs [all|asterisk|tts]"; exit 1 ;;
    esac
}

cmd_status() {
    podman pod ps --filter "name=${POD_NAME}"
    echo ""
    podman ps --filter "pod=${POD_NAME}" --format "table {{.Names}}\t{{.Status}}\t{{.Image}}"
}

cmd_exec() {
    podman exec -it "$ASTERISK_NAME" asterisk -rvvv
}

# ---- Main ----------------------------------------------------

case "${1:-}" in
    start)  cmd_start ;;
    stop)   cmd_stop ;;
    logs)   cmd_logs "${2:-all}" ;;
    status) cmd_status ;;
    exec)   cmd_exec ;;
    *)
        echo "Verwendung: $0 {start|stop|logs|status|exec}"
        echo ""
        echo "  start   – Pod + Container starten (podman kube play)"
        echo "  stop    – Pod + Container entfernen (podman kube down)"
        echo "  logs    – Logs anzeigen (optional: logs asterisk|tts)"
        echo "  status  – Pod-Status anzeigen"
        echo "  exec    – Asterisk CLI öffnen"
        exit 1
        ;;
esac
