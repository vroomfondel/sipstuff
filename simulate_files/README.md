# PJSIP Stack – Asterisk + Realtime TTS Client

Podman setup with an Asterisk SIP server and a PJSIP Python client
that answers incoming calls with Piper TTS.

## Architecture

```
  LAN (Softphone)                    Pod: pjsip-stack
  ┌──────────┐          ┌──────────────────────────────────────────┐
  │ Linphone │──SIP────>│  ┌───────────┐       ┌──────────────┐   │
  │ / Zoiper │   :5060  │  │ Asterisk  │──SIP──│ PJSIP TTS    │   │
  │          │<──RTP────│  │ SIP-Server│       │ Client       │   │
  │ Ext 1002 │  10000-  │  │           │       │ (Piper TTS)  │   │
  └──────────┘  10100   │  └───────────┘       │ Ext 1001     │   │
                        │                      └──────────────┘   │
                        └──────────────────────────────────────────┘
```

Asterisk runs via the `andrius/asterisk` image, the TTS client via
the locally built `xomoxcc/sipstuff:latest` image (`make build`).

### Container Images

| Variant          | Image Tag                                                              |
|------------------|------------------------------------------------------------------------|
| Standard (CPU)   | `xomoxcc/sipstuff:latest`                                              |
| CUDA             | `xomoxcc/sipstuff:python-3.14-slim-trixie-pjsip_2.16-cuda-noopenvino` |
| OpenVINO         | `xomoxcc/sipstuff:python-3.14-slim-trixie-pjsip_2.16-nocuda-openvino` |
| CUDA + OpenVINO  | `xomoxcc/sipstuff:python-3.14-slim-trixie-pjsip_2.16-cuda-openvino`   |

## Standalone Scripts

The scripts `start_callee.sh`, `start_callee_autoanswer.sh`,
`start_callee_realtime_tts.sh` and `start_caller.sh` start containers individually
and support GPU backends via command-line flags:

```bash
# Standard (CPU)
./start_callee.sh

# NVIDIA CUDA
./start_callee.sh --cuda

# Intel OpenVINO (GPU via /dev/dri/renderD128)
./start_callee.sh --openvino
```

With `--openvino`, `/dev/dri/renderD128` is mounted into the container and
group IDs 226, 993, 128 (render/video) are added. Without a GPU, OpenVINO
falls back to CPU automatically.

With `--cuda` or `--openvino` the matching image tag is selected automatically
(see table above).

### PulseAudio (Sound Output)

For local audio output (e.g. `--play-audio`), uncomment the PulseAudio lines
in `start_callee.sh` / `start_caller.sh`, or remove the corresponding comments
in `docker-compose.yml` / `pjsip-stack.yaml`.

## Quick Start

### 1. Build the sipstuff image (project root)

```bash
make build
```

### 2. Download a Piper voice model

```bash
mkdir -p piper-models
wget -P piper-models/ \
  https://huggingface.co/rhasspy/piper-voices/resolve/main/de/de_DE/thorsten/high/de_DE-thorsten-high.onnx
wget -P piper-models/ \
  https://huggingface.co/rhasspy/piper-voices/resolve/main/de/de_DE/thorsten/high/de_DE-thorsten-high.onnx.json
```

### 3. Start the stack

**Option A – podman kube play (via stack.sh):**

```bash
./stack.sh start
```

**Option B – podman-compose:**

```bash
podman-compose up -d
```

### 4. Test

Configure a softphone (Linphone, Zoiper, MicroSIP, ...):

| Setting   | Value                      |
|-----------|----------------------------|
| Server    | IP of your host            |
| Port      | 5060                       |
| User      | 1002                       |
| Password  | geheim1002                 |
| Transport | UDP                        |

Then call **1001**, **1003** or **1004** — the callee picks up and plays the announcement.

### 5. Stop the stack

```bash
# Option A
./stack.sh stop

# Option B
podman-compose down
```

## stack.sh Commands

| Command                | Description                           |
|------------------------|---------------------------------------|
| `./stack.sh start`     | Start pod (podman kube play)          |
| `./stack.sh stop`      | Remove pod (podman kube down)         |
| `./stack.sh logs`      | Show logs of all containers           |
| `./stack.sh logs asterisk` | Asterisk logs only                |
| `./stack.sh logs tts`  | TTS client logs only                  |
| `./stack.sh status`    | Show pod status                       |
| `./stack.sh exec`      | Open Asterisk CLI                     |

## SIP Accounts

| Extension | Password    | Purpose                                  |
|-----------|-------------|------------------------------------------|
| 1001      | geheim1001  | Live-transcribe client (auto-registered) |
| 1002      | geheim1002  | External softphone for testing           |
| 1003      | geheim1003  | Autoanswer TTS client (auto-registered)  |
| 1004      | geheim1004  | Realtime TTS + STT client (auto-registered) |

## Special Numbers

| Number | Function                                          |
|--------|---------------------------------------------------|
| 1001   | Call live-transcribe client (answering machine)   |
| 1002   | Call external softphone                           |
| 1003   | Call autoanswer TTS client                        |
| 1004   | Call realtime TTS client (interactive, live STT)  |
| *99    | Echo test (hear yourself)                         |

## Configuration

### Changing SIP Credentials / TTS Text

SIP credentials, the Piper model and the TTS text are configured directly in
`pjsip-stack.yaml` (or `docker-compose.yml`). Edit the `args` section (YAML)
or `command` (Compose) accordingly.

When changing SIP accounts, also update `pjsip.conf`.

### Adding SIP Accounts

Add new entries in `pjsip.conf` following the pattern of existing accounts
and add the corresponding dial rules in `extensions.conf`.

**Important:** Endpoint, Auth and AOR sections must share the same name
(e.g. all `[1001]` with different `type=`). Asterisk's PJSIP registration
extracts the username from the To header of the REGISTER request and looks
up an AOR section with that name. If the names don't match (e.g. AOR is
named `1001-aors` instead of `1001`), registration fails with
`AOR '' not found` / HTTP 404.

### Enabling a GPU Backend (docker-compose / kube play)

CUDA and OpenVINO configurations are prepared as comments in `docker-compose.yml`
and `pjsip-stack.yaml`. To enable:

1. Change the `image:` to the matching tag (see Container Images table)
2. Uncomment the corresponding GPU block
3. For OpenVINO, adjust group IDs to match your host system (`stat -c %g /dev/dri/renderD128`)

### Using a Different Piper Model

Available voices: https://rhasspy.github.io/piper-samples/

```bash
# Example: English voice
wget -P piper-models/ \
  https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US/lessac/high/en_US-lessac-high.onnx
wget -P piper-models/ \
  https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US/lessac/high/en_US-lessac-high.onnx.json
```

Then update the `--piper-model` path in `pjsip-stack.yaml`:

```yaml
- --piper-model
- /models/en_US-lessac-high.onnx
```

## Troubleshooting

### Firewall

If Asterisk is not reachable from the LAN:

```bash
# Firewalld
sudo firewall-cmd --add-port=5060/udp --permanent
sudo firewall-cmd --add-port=5060/tcp --permanent
sudo firewall-cmd --add-port=10000-10100/udp --permanent
sudo firewall-cmd --reload

# UFW
sudo ufw allow 5060/udp
sudo ufw allow 5060/tcp
sudo ufw allow 10000:10100/udp
```

## File Structure

```
simulate_files/
├── stack.sh                       # Wrapper script (podman kube play)
├── start_callee.sh                # Standalone: live-transcribe container (--cuda / --openvino)
├── start_callee_autoanswer.sh     # Standalone: autoanswer TTS container (--cuda / --openvino)
├── start_callee_realtime_tts.sh   # Standalone: realtime TTS container (--cuda / --openvino)
├── start_caller.sh                # Standalone: caller container (--cuda / --openvino)
├── pjsip-stack.yaml               # Pod definition (Kubernetes YAML)
├── docker-compose.yml             # Alternative: podman-compose
├── pjsip.conf                     # SIP accounts
├── extensions.conf                # Dial plan
├── rtp.conf                       # RTP port range
├── modules.conf                   # Asterisk modules
├── piper-models/                  # Piper voice models (download manually)
│   ├── de_DE-thorsten-high.onnx
│   └── de_DE-thorsten-high.onnx.json
└── README.md
```
