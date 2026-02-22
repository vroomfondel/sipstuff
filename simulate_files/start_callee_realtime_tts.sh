#!/usr/bin/env bash
# ==============================================================
# Start pjsip-realtime-tts container standalone
# ==============================================================
# Run from simulate_files/ directory.
# ==============================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

USE_CUDA=false
USE_OPENVINO=false

for arg in "$@"; do
  case "$arg" in
    --cuda)     USE_CUDA=true ;;
    --openvino) USE_OPENVINO=true ;;
  esac
done

GPU_FLAGS=()

sipstuff_image="xomoxcc/sipstuff:latest"

if "$USE_CUDA" || "$USE_OPENVINO" ; then
  sipstuff_image="xomoxcc/sipstuff:latest"
  PYTHON_VERSION=3.14
  DEBIAN_VERSION=slim-trixie
  PJSIP_VERSION=2.16
  sipstuff_image="xomoxcc/sipstuff:python-${PYTHON_VERSION}-${DEBIAN_VERSION}-pjsip_${PJSIP_VERSION}-nocuda-noopenvino"
fi

if "$USE_CUDA"; then
  GPU_FLAGS=(--device nvidia.com/gpu=all)
  sipstuff_image="${sipstuff_image/nocuda/cuda}"
fi

if "$USE_OPENVINO"; then
  GPU_FLAGS=("--device" "/dev/dri/renderD128:/dev/dri/renderD128" "--group-add" "226" "--group-add" "993" "--group-add" "128")
  sipstuff_image="${sipstuff_image/noopenvino/openvino}"
fi

SND_DEVICE_FLAGS=()
#SND_DEVICE_FLAGS+=("-v" "/run/user/$(id -u)/pulse:/run/user/1200/pulse")
#SND_DEVICE_FLAGS+=("-e" "PULSE_SERVER=unix:/run/user/1200/pulse/native")

STT_DEVICE_FLAGS=()
if "$USE_CUDA"; then
  STT_DEVICE_FLAGS+=("--stt-device" "cuda")
fi
if "$USE_OPENVINO"; then
  STT_DEVICE_FLAGS+=("--stt-backend" "openvino")
fi

data_dir="${SCRIPT_DIR}/../sipstuff_data.local"

if ! [ -d "${data_dir}" ] ; then
  mkdir -p "${data_dir}"
fi

set +x

podman run --rm -it --userns=keep-id:uid=1200,gid=1201 \
  "${SND_DEVICE_FLAGS[@]}" \
  "${GPU_FLAGS[@]}" \
  --network=host \
  --name pjsip-realtime-tts \
  -e LOG_LEVEL=3 \
  -v "${SCRIPT_DIR}/../sipstuff/realtime/pjsip_realtime_tts.py:/app/sipstuff/realtime/pjsip_realtime_tts.py:ro" \
  -v "${data_dir}/piper-models:/piper-models" \
  -v "${data_dir}/whisper-models:/whisper-models" \
  "${sipstuff_image}" \
  sipstuff-cli callee_realtime-tts \
    --server 127.0.0.1 \
    --user 1004 \
    --password geheim1004 \
    --local-port 5064 \
    --piper-model /piper-models/de_DE-thorsten-high.onnx \
    --tts-text "Willkommen! Sie sind mit dem Echtzeit-TTS-Client verbunden." \
    --answer-delay 1.0 \
    --interactive \
    --stt-model base \
    --stt-data-dir /whisper-models \
    --stt-language de \
    "${STT_DEVICE_FLAGS[@]}"
