#!/usr/bin/env bash
# ==============================================================
# Start pjsip-autoanswer container standalone
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

data_dir="${SCRIPT_DIR}/../sipstuff_data.local"

if ! [ -d "${data_dir}" ] ; then
  mkdir -p "${data_dir}"
fi

set +x

PIPER_MODEL="${PIPER_MODEL:-de_DE-thorsten-high}"

podman run --rm -it --userns=keep-id:uid=1200,gid=1201 \
  "${SND_DEVICE_FLAGS[@]}" \
  "${GPU_FLAGS[@]}" \
  --network=host \
  --name pjsip-autoanswer \
  -e LOG_LEVEL=3 \
  -v "${SCRIPT_DIR}/../sipstuff/autoanswer/pjsip_autoanswer_tts_n_wav.py:/app/sipstuff/autoanswer/pjsip_autoanswer_tts_n_wav.py:ro" \
  -v "${data_dir}/piper-models:/piper-models" \
  "${sipstuff_image}" \
  sipstuff-cli callee_autoanswer \
    --server 127.0.0.1 \
    --user 1003 \
    --password geheim1003 \
    --local-port 5063 \
    --mode tts \
    --piper-model "${PIPER_MODEL}" \
    --tts-data-dir /piper-models \
    --tts-text "Willkommen! Sie sind mit dem automatischen Anrufbeantworter verbunden." \
    --answer-delay 1.0
