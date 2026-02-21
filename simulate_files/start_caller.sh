#!/bin/bash

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

function list_snddevices {
  podman run -it --rm \
    --userns=keep-id:uid=1200,gid=1201 \
    -v /run/user/$(id -u)/pulse:/run/user/1200/pulse \
    -v ../alert.wav:/alert.wav:ro \
    -e PULSE_SERVER=unix:/run/user/1200/pulse/native \
    xomoxcc/sipstuff:latest \
    python3 -m sipstuff.snddevice_list  # /alert.wav
}

list_snddevices

data_dir="${SCRIPT_DIR}/../sipstuff_data.local"

if ! [ -d "${data_dir}" ] ; then
  mkdir -p "${data_dir}"
fi

podman run --network=host -it --rm \
  --userns=keep-id:uid=1200,gid=1201 \
  -v /run/user/$(id -u)/pulse:/run/user/1200/pulse \
  -e PULSE_SERVER=unix:/run/user/1200/pulse/native \
  -v "${data_dir}:/data" \
  xomoxcc/sipstuff:latest \
  python3 -m sipstuff.cli call \
  --stt-data-dir /data/whisper-models \
  --tts-data-dir /data/piper-models \
  --server 127.0.0.1 \
  --port 5060 \
  --transport udp \
  --srtp disabled \
  --user 1002 \
  --password geheim1002 \
  --dest 1001 \
  --text "Houston, wir wollen Kartoffeln. Wenn wir Kartoffeln haben, haben wir Kartoffeln. Warum Kartoffeln? Naja, Cliché!" \
  --pre-delay 1.0 \
  --post-delay 1.0 \
  --inter-delay 2.1 \
  --repeat 2 \
  --wait-for-silence 2.0 \
  --transcribe \
  --record /data/recordings_caller/recording_$(date +%Y%m%s_%H%M%S).wav \
  --play-audio \
  --play-tx \
  --audio-device 0 \
  --verbose



#podman run --network=host -it --rm \
#  --userns=keep-id:uid=1200,gid=1201 \
#  -v /run/user/$(id -u)/pulse:/run/user/1200/pulse \
#  -e PULSE_SERVER=unix:/run/user/1200/pulse/native \
#  -v "${SCRIPT_DIR}/../sipstuff_data.local:/data" \
#  xomoxcc/sipstuff:latest \
#  python3 -m sipstuff.cli tts \
#  --stt-data-dir /data/whisper-models \
#  --tts-data-dir /data/piper-models \
#  --play-audio \
#  --audio-device 0 \
#  "Hallo Welt"