#!/usr/bin/env bash
# ==============================================================
# Start pjsip-autoanswer-transcribe container standalone
# ==============================================================
# Run from simulate_files/ directory.
# ==============================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

GPU_FLAGS=()
STT_DEVICE_FLAGS=()

for arg in "$@"; do
  case "$arg" in
    --cuda)
      GPU_FLAGS=(--device nvidia.com/gpu=all)
      STT_DEVICE_FLAGS=(--stt-device cuda)
      ;;
    --openvino)
      STT_DEVICE_FLAGS=(--stt-backend openvino)
      ;;
  esac
done

set +x

podman run --rm -it --userns=keep-id:uid=1200,gid=1201 \
  --network=host \
  --name pjsip-autoanswer-transcribe \
  -e LOG_LEVEL=3 \
  "${GPU_FLAGS[@]}" \
  -v "${SCRIPT_DIR}/../sipstuff/realtime/pjsip_realtime_tts.py:/app/sipstuff/realtime/pjsip_realtime_tts.py:ro" \
  -v "${SCRIPT_DIR}/../sipstuff/transcribe/pjsip_live_transcribe.py:/app/sipstuff/transcribe/pjsip_live_transcribe.py:ro" \
  -v "${SCRIPT_DIR}/../sipstuff/autoanswer/pjsip_autoanswer_tts_n_wav.py:/app/sipstuff/autoanswer/pjsip_autoanswer_tts_n_wav.py:ro" \
  -v "${SCRIPT_DIR}/../sipstuff_data.local/piper-models:/piper-models" \
  -v "${SCRIPT_DIR}/../sipstuff_data.local/whisper-models:/whisper-models" \
  -v "${SCRIPT_DIR}/../sipstuff_data.local/recordings_callee:/recordings" \
  xomoxcc/sipstuff:latest \
  sipstuff-cli callee_live-transcribe \
    --server 127.0.0.1 \
    --user 1001 \
    --password geheim1001 \
    --local-port 5062 \
    --piper-model de_DE-thorsten-high \
    --tts-data-dir /piper-models \
    --tts-text "Willkommen! Sie sind mit dem automatischen Anrufbeantworter verbunden. ... Bitte hinterlassen Sie eine Nachricht nach dem Signalton." \
    --wav-dir /recordings \
    --stt-model large-v3 \
    --stt-live-model base \
    --stt-data-dir /whisper-models \
    --stt-language de \
    --transcribe \
    "${STT_DEVICE_FLAGS[@]}"

