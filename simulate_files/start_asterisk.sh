#!/usr/bin/env bash
# ==============================================================
# Start Asterisk SIP server container standalone
# ==============================================================
# Run from simulate_files/ directory.
# ==============================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

podman run --rm -it \
  --network=host \
  --name pjsip-stack-asterisk \
  -v "${SCRIPT_DIR}/pjsip.conf:/etc/asterisk/pjsip.conf:ro" \
  -v "${SCRIPT_DIR}/extensions.conf:/etc/asterisk/extensions.conf:ro" \
  -v "${SCRIPT_DIR}/rtp.conf:/etc/asterisk/rtp.conf:ro" \
  -v "${SCRIPT_DIR}/modules.conf:/etc/asterisk/modules.conf:ro" \
  docker.io/andrius/asterisk:latest 2>&1 | sed $'s/\033\\[0;3/\033[1;3/g'
