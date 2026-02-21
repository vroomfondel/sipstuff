# sipstuff/realtime — Real-Time TTS Streaming over SIP

This subpackage implements a PJSIP-based SIP client that auto-answers incoming calls and streams
Piper TTS audio into them in real time, using a Producer-Consumer architecture.

The script registers at a SIP server, waits for inbound calls, accepts them automatically, and
immediately feeds synthesised speech into the call audio media — all without any sound hardware
(null audio device, headless-safe).

---

## Table of Contents

1. [Purpose](#purpose)
2. [Architecture](#architecture)
3. [Audio Parameters](#audio-parameters)
4. [Classes](#classes)
5. [Interactive Console Mode](#interactive-console-mode)
6. [CLI Arguments and Usage](#cli-arguments-and-usage)
7. [Environment Variables](#environment-variables)
8. [Dependencies](#dependencies)
9. [Module Structure](#module-structure)

---

## Purpose

- Register a SIP account and listen for incoming calls.
- On each incoming call, automatically answer after a configurable delay.
- Stream Piper TTS output (synthesised from arbitrary text) directly into the call audio in near-real time.
- Optionally accept live text input from the terminal during an active call (interactive mode).
- Run headlessly in containers — no real audio device required.

---

## Architecture

The core design separates synthesis (slow, I/O-bound) from audio delivery (hard real-time, called every 20 ms by PJSIP) through a shared thread-safe queue.

```
┌──────────────────────┐     Queue[bytes]     ┌──────────────────────┐     PJSIP media
│   PiperTTSProducer   │ ───── PCM chunks ──> │    TTSMediaPort      │ ──────────────> Call / Remote Party
│   (Producer Thread)  │                       │    (Consumer)        │
│                       │                       │                      │
│  text_queue (str)    │                       │  onFrameRequested()  │
│  → Piper subprocess  │                       │  called every 20 ms  │
│  → WAV → resample    │                       │  pulls next chunk or │
│  → 20 ms PCM chunks  │                       │  sends silence       │
│  → audio_queue.put() │                       │                      │
└──────────────────────┘                       └──────────────────────┘
         ↑
         │ producer.speak("Hello!")
         │
  ┌──────────────────┐
  │  SipCallee        │  ← onIncomingCall()  →  auto-answer after delay
  │  RealtimeTtsCall  │  ← on_media_active()  → wires TTSMediaPort to call
  └──────────────────┘
         ↑
  interactive_console()  (optional, separate thread)
  reads stdin → producer.speak()
```

**Data flow summary:**

1. `PiperTTSProducer.speak(text)` enqueues the text string into an internal `text_queue`.
2. The producer thread dequeues the text, runs the Piper binary as a subprocess, reads the output WAV file, resamples from Piper's native rate (typically 22050 Hz) to 16000 Hz, and slices the PCM data into 320-sample (20 ms) chunks, placing each into the shared `audio_queue`.
3. A sentinel value `b"__EOS__"` (End-of-Speech) is enqueued after each utterance.
4. PJSIP calls `TTSMediaPort.onFrameRequested()` every 20 ms; the port pops one chunk from the queue. If the queue is empty or the EOS marker is encountered, silence is returned instead.
5. The audio flows from `TTSMediaPort` into the active call via `startTransmit(aud_med)`.

---

## Audio Parameters

| Parameter         | Value             | Notes                                 |
|-------------------|-------------------|---------------------------------------|
| Clock rate        | 16000 Hz          | Matches PJSIP MediaPort configuration |
| Samples per frame | 320               | 20 ms at 16 kHz                       |
| Bits per sample   | 16                | S16_LE (signed 16-bit little-endian)  |
| Channels          | 1 (mono)          |                                       |
| Queue buffer      | 500 chunks max    | ~10 s of audio headroom               |
| Piper output rate | typically 22050 Hz| Resampled automatically to 16 kHz     |

---

## Classes

### `PiperTTSProducer` and `TTSMediaPort`

These core classes have been extracted to `sipstuff/tts/live.py` for reuse across the package. See the [sipstuff/tts/ README section](../README.md#live-tts-streaming-ttslive) for full documentation.

This module imports them as:

```python
from sipstuff.tts.live import PiperTTSProducer, TTSMediaPort, interactive_console, CLOCK_RATE, BITS_PER_SAMPLE, CHANNEL_COUNT
```

---

### `RealtimeTtsCall`

**Role:** `CalleeCall` subclass. Bridges the PJSIP call lifecycle to the TTS system by wiring a `TTSMediaPort` to the call's audio media.

**Constructor parameters:**

| Parameter | Type | Description |
|---|---|---|
| `account` | `CalleeAccount` | Owning account |
| `call_id` | `int` | PJSIP call ID |
| `audio_queue` | `Queue[bytes]` | Shared PCM queue (keyword-only) |
| `tts_producer` | `PiperTTSProducer` | Producer to use for initial text (keyword-only) |
| `initial_text` | `str \| None` | Text spoken immediately when media becomes active (keyword-only) |

**Key callback:**

| Method | Behaviour |
|---|---|
| `on_media_active(audio_media, media_idx)` | Creates a `TTSMediaPort`, initialises a `MediaFormatAudio` via `fmt.init(pj.PJMEDIA_FORMAT_PCM, 16000, 1, 20000, 16)` (never set fields manually — see note below), calls `startTransmit` to feed TTS audio into the call, and invokes `tts_producer.speak(initial_text)` if an initial text was configured. Appends the media port to `orphan_store` for safe PJSIP cleanup. |

---

### `CalleeAccount` and `SipCallee`

SIP account registration and incoming call dispatch are handled by the shared `SipCallee` framework from `sipstuff.sip_callee`. `main()` passes a `call_factory` function that creates `RealtimeTtsCall` instances for each incoming call. `SipCallee` manages auto-answer, answer delay, and the PJSIP event loop.

### PJSIP Threading and MediaFormat Notes

**Thread registration:** Any daemon thread that calls PJSUA2 API functions (e.g. the delayed auto-answer thread, playback threads) must call `pj.Endpoint.instance().libRegisterThread("<name>")` before making any PJSIP calls. PJSIP callbacks (`onCallState`, `onCallMediaState`, `onFrameRequested`) run on pre-registered threads and do not need this.

**MediaFormatAudio:** Always initialise via `fmt.init(pj.PJMEDIA_FORMAT_PCM, clock_rate, channels, frame_time_usec, bits)`. Setting fields manually (`fmt.clockRate = ...`) does not set the internal `type`/`detail_type` discriminators and causes a `PJMEDIA_PIA_CCNT` assertion failure.

---

## Interactive Console Mode

When `--interactive` is passed, a separate daemon thread (`Console`) runs `interactive_console()` alongside the PJSIP event loop. It reads lines from stdin and forwards each to `tts_producer.speak()`, so text typed in the terminal is synthesised and streamed into the active call in near-real time.

```
=== Interactive Mode ===
Type text and press Enter to speak it into the call.
Commands: 'quit' = Exit

TTS> Hello, can you hear me?
TTS> Please hold while I transfer your call.
TTS> quit
```

Special commands accepted at the `TTS>` prompt:

| Input | Action |
|---|---|
| `quit` | Terminates the console loop |
| `exit` | Terminates the console loop |
| `q` | Terminates the console loop |
| `Ctrl+C` / EOF | Terminates the console loop |
| Any other text | Enqueued for TTS synthesis and playback |

Note: the console thread is a daemon thread; it does not block program shutdown.

---

## CLI Arguments and Usage

Run the script directly:

```bash
python -m sipstuff.realtime.pjsip_realtime_tts [OPTIONS]
# or
python sipstuff/realtime/pjsip_realtime_tts.py [OPTIONS]
```

### TTS Arguments

| Argument | Type | Default | Description |
|---|---|---|---|
| `--tts-text TEXT` | str | None | Text spoken immediately when a call is answered |
| `--interactive` | flag | off | Enable interactive console mode |
| `--piper-model PATH` | str | `./de_DE-thorsten-high.onnx` | Path to the Piper `.onnx` voice model file |

### SIP Connection Arguments

Provided by `pjsip_common.add_sip_args()`. All accept environment variable fallbacks.

| Argument | Env Var | Default | Description |
|---|---|---|---|
| `--sip-server HOST` | `SIP_SERVER` | `127.0.0.1` | SIP server hostname or IP |
| `--sip-user USER` | `SIP_USER` | `testuser` | SIP account username |
| `--sip-password PASS` | `SIP_PASSWORD` | `testpassword` | SIP account password |
| `--sip-port PORT` | `SIP_PORT` | `5060` | Local UDP port for SIP signalling |
| `--public-ip IP` | `PUBLIC_IP` | `` | Public IP for NAT traversal (optional) |

### Call Behaviour Arguments

| Argument | Default | Description |
|---|---|---|
| `--answer-delay SECONDS` | `1.0` | Seconds to wait before accepting an inbound call |
| `--no-auto-answer` | off | Disable automatic call answering |

### Usage Examples

```bash
# Play a fixed announcement on every incoming call:
python -m sipstuff.realtime.pjsip_realtime_tts \
    --sip-server 192.168.1.10 \
    --sip-user 1001 \
    --sip-password secret \
    --tts-text "Welcome! Please hold the line." \
    --piper-model /models/de_DE-thorsten-high.onnx

# Interactive mode — type text while on a call:
python -m sipstuff.realtime.pjsip_realtime_tts \
    --sip-server 192.168.1.10 \
    --sip-user 1001 \
    --sip-password secret \
    --interactive \
    --piper-model /models/de_DE-thorsten-high.onnx

# Combination: play an initial greeting, then accept further input:
python -m sipstuff.realtime.pjsip_realtime_tts \
    --sip-server 192.168.1.10 \
    --sip-user 1001 \
    --sip-password secret \
    --tts-text "Hello! I am ready." \
    --interactive \
    --piper-model /models/de_DE-thorsten-high.onnx

# Slow-answer mode (3-second ring before pick-up):
python -m sipstuff.realtime.pjsip_realtime_tts \
    --sip-server pbx.example.com \
    --sip-user 2001 \
    --sip-password hunter2 \
    --answer-delay 3.0 \
    --tts-text "Thank you for calling." \
    --piper-model ./en_US-lessac-high.onnx

# Using environment variables instead of CLI flags:
export SIP_SERVER=pbx.example.com
export SIP_USER=2001
export SIP_PASSWORD=hunter2
export PIPER_BIN=/opt/piper-venv/bin/piper
python -m sipstuff.realtime.pjsip_realtime_tts \
    --tts-text "Hello from env config." \
    --piper-model ./en_US-lessac-high.onnx
```

---

## Environment Variables

| Variable | Default | Description |
|---|---|---|
| `PIPER_BIN` | `/opt/piper-venv/bin/piper` | Full path to the Piper TTS executable |
| `SIP_SERVER` | `127.0.0.1` | SIP server address (overridden by `--sip-server`) |
| `SIP_USER` | `testuser` | SIP username (overridden by `--sip-user`) |
| `SIP_PASSWORD` | `testpassword` | SIP password (overridden by `--sip-password`) |
| `SIP_PORT` | `5060` | Local SIP port (overridden by `--sip-port`) |
| `PUBLIC_IP` | `` | External IP for NAT (overridden by `--public-ip`) |
| `LOG_LEVEL` | `3` | PJSIP internal log level (0=none … 5=verbose) |

---

## Dependencies

### Runtime Dependencies

| Dependency | Import / Location | Role |
|---|---|---|
| `pjsua2` | `import pjsua2 as pj` | PJSIP Python SWIG bindings — SIP signalling, media ports, event loop |
| `piper` (CLI binary) | `$PIPER_BIN` or `PATH` | Text-to-speech synthesis, run as subprocess (used by `PiperTTSProducer` in `sipstuff.tts.live`) |
| `sipstuff.tts.live` | `from sipstuff.tts.live import PiperTTSProducer, TTSMediaPort, ...` | Live TTS producer-consumer classes and audio constants |
| `sipstuff.sip_callee` | `from sipstuff.sip_callee import CalleeAccount, CalleeCall, SipCallee` | SIP callee framework: account registration, auto-answer, event loop |
| `sipstuff.sipconfig` | `from sipstuff.sipconfig import add_sip_args, load_config` | Shared argparse helpers and config loading |
| `loguru` | `from loguru import logger` | Structured logging with class-name binding |

### Standard Library

`argparse`, `threading`, `queue`

### Build-time Note on `pjsua2`

`pjsua2` is not available on PyPI. It must be compiled from the PJSIP C source tree. The project's
`dist_scripts/install_pjsip.sh` script handles this, and the project Dockerfile builds it in the
`pjsip-builder` stage and copies the resulting `.so` files and Python bindings into the final image.

### Piper TTS Binary

The Piper binary is expected at `/opt/piper-venv/bin/piper` (the path created by the `piper-builder`
Dockerfile stage). Override with the `PIPER_BIN` environment variable. A Piper voice model (`.onnx`
file plus its `.onnx.json` config sidecar) must be provided separately via `--piper-model`.

---

## Module Structure

```
sipstuff/realtime/
├── __init__.py                  # Empty marker (py.typed present)
├── py.typed                     # PEP 561 marker for mypy
├── pjsip_realtime_tts.py        # RealtimeTtsCall, CLI entry point (interactive_console moved to tts/live.py)
└── README.md                    # This file
```

### Key Symbols in `pjsip_realtime_tts.py`

| Symbol | Kind | Description |
|---|---|---|
| `RealtimeTtsCall` | class | `CalleeCall` subclass — wires `TTSMediaPort` to call media, fires initial TTS |
| `interactive_console()` | re-export | Imported from `sipstuff.tts.live` (moved there for reuse by `call --interactive`) |
| `parse_args()` | function | argparse setup; returns `argparse.Namespace` |
| `main()` | function | Entry point: builds queue, starts producer, runs `SipCallee` event loop |

### Key Symbols in `sipstuff/tts/live.py` (imported by this module)

| Symbol | Kind | Description |
|---|---|---|
| `PiperTTSProducer` | class | Producer thread — synthesises text → PCM chunks → queue |
| `TTSMediaPort` | class | PJSIP `AudioMediaPort` consumer — feeds queue chunks to call |
| `interactive_console()` | function | Reads stdin in a loop, calls `producer.speak()` — generic, shared by callee and caller |
| `CLOCK_RATE` | int | `16000` — audio sample rate |
| `SAMPLES_PER_FRAME` | int | `320` — samples per 20 ms PJSIP frame |
| `BITS_PER_SAMPLE` | int | `16` — S16_LE |
| `CHANNEL_COUNT` | int | `1` — mono |
