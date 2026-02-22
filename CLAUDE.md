# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

SIP telephony automation toolkit: place calls via PJSIP, play WAV/TTS audio, record and transcribe responses. Designed for headless/container operation (null audio device via `audDevManager().setNullDev()`).

Core package: **`sipstuff/`** — SIP library + CLI (`call`, `tts`, `stt`, `callee_autoanswer`, `callee_realtime-tts`, `callee_live-transcribe` subcommands)

## Build & Development Commands

```bash
make install          # Create .venv (Python 3.14), install deps
make tests            # pytest .
make lint             # black .
make isort            # isort .
make tcheck           # mypy . (strict mode + pydantic plugin)
make commit-checks    # pre-commit run --all-files (black --check, mypy, gitleaks)
make prepare          # tests + commit-checks
make build            # docker build (two-stage: pjsip-builder, main)
make pypibuild        # hatch build --clean
```

Activate venv manually: `source .venv/bin/activate`

Run CLI: `python -m sipstuff.cli {call,tts,stt,callee_autoanswer,callee_realtime-tts,callee_live-transcribe} [args...]` or `sipstuff-cli ...` (installed entry point). The `call` subcommand supports `--interactive` mode for live console TTS during outgoing calls (requires `--piper-model`).

**Note:** `pjsua2` is not pip-installable — it must be built from PJSIP C source via `dist_scripts/install_pjsip.sh` (or the Dockerfile handles it). No tests directory exists yet; `pytest .` passes vacuously.

## Architecture

### Module Layout

The SIP call lifecycle is split across four core modules (formerly a single `sip_caller.py`):

| Module | Responsibility |
|---|---|
| `sip_endpoint.py` | `SipEndpoint` base class for PJSUA2 `Endpoint` lifecycle (create → init → start → destroy), `SipCaller(SipEndpoint)` with `make_call(destination, wav_file?, *, call?, wav_play?, recording?, audio?, stt?, vad?, tts_producer?, initial_tts_text?)` orchestration, `_PjLogWriter`, `_local_address_for()` |
| `sip_call.py` | `SipCall(pj.Call)` — shared base with `onCallState`, threading events, audio-device routing. `SipCallerCall(SipCall)` — caller-side subclass with `onCallMediaState` auto-start, WAV playback/recording/streaming/transcription/live-TTS media management (`set_tts_port()`, `stop_tts_port()`). `SipCalleeCall(SipCall)` — callee-side subclass with `on_media_active`/`on_disconnected` hooks |
| `sip_media.py` | `SilenceDetector`, `AudioStreamPort`, `TranscriptionPort` — PJSUA2 `AudioMediaPort` subclasses tapped into the conference bridge |
| `sip_account.py` | `SipAccount(pj.Account)` base class — account registration, credentials, SRTP, ICE/TURN, UDP keepalive, `build_pj_account_config()` static method. `SipCallerAccount(SipAccount)` adds incoming-call rejection/callback. `SipCalleeAccount(SipAccount)` adds factory dispatch + auto-answer. |
| `sip_types.py` | `CallResult` dataclass, `SipCallError`, `WavInfo` |
| `sip_callee.py` | `SipCallee(SipEndpoint)` — incoming call handling orchestration for callee subcommands |
| `sipconfig.py` | `SipEndpointConfig` (endpoint-level: `sip`, `nat`, `pjsip`, `audio`), `SipCallerConfig(SipEndpointConfig)` (adds call-level: `call`, `tts`, `recording`, `wav_play`, `stt`, `vad`), `SipCalleeConfig(SipEndpointConfig)` (adds `auto_answer`/`answer_delay`) — all Pydantic v2. Config layering (YAML → env → overrides) via `from_config()` classmethod factory inherited from `SipEndpointConfig`. `TtsConfig.data_dir` sets the Piper voice model directory. `SttConfig.live_transcribe` enables live STT during calls. `WavPlayConfig.tts_text` triggers TTS generation inside `make_call()`. |
| `vad.py` | `VADAudioBuffer` — voice activity detection buffer for live transcription |
| `audio.py` | `resample_linear()` (numpy), `ensure_wav_16k_mono()` — audio format utilities |
| `tts/live.py` | `PiperTTSProducer` (producer thread), `TTSMediaPort` (PJSIP consumer), `interactive_console()` (stdin→TTS loop), audio constants (`CLOCK_RATE`, `SAMPLES_PER_FRAME`, `BITS_PER_SAMPLE`, `CHANNEL_COUNT`) |
| `cli.py` | Argparse CLI with subcommand handlers (`call`, `tts`, `stt`, `callee_autoanswer`, `callee_realtime-tts`, `callee_live-transcribe`) |

### PJSUA2 Callback Model
Everything is event-driven via PJSIP C++ SWIG bindings. Key subclasses:
- `SipCall(pj.Call)` — shared base with `onCallState`, threading events (`connected_event`, `disconnected_event`, `media_ready_event`), audio-device routing
- `SipCallerCall(SipCall)` — caller-side subclass with `onCallMediaState` auto-start, WAV playback/recording/streaming/transcription/live-TTS media management
- `SilenceDetector(pj.AudioMediaPort)` — RMS-based silence detection; `onFrameReceived()` called every ~20 ms, converts `pj.ByteVector` to `bytes()` explicitly (SWIG quirk)
- `AudioStreamPort(pj.AudioMediaPort)` — dual-sink audio port: streams raw PCM (16 kHz, S16_LE, mono) to a Unix domain socket and/or local sounddevice output. Either or both sinks can be active simultaneously.
- `TranscriptionPort(pj.AudioMediaPort)` — feeds PCM frames to `VADAudioBuffer` for live speech-to-text; frame type check uses `pj.PJMEDIA_FRAME_TYPE_AUDIO` (not `PJMEDIA_FRAME_AUDIO`, which doesn't exist in the SWIG bindings)

### PJSIP Thread Registration
**Any Python thread that calls PJSUA2/PJSIP API functions must first call `pj.Endpoint.instance().libRegisterThread("<name>")`.** PJSIP asserts that all calling threads are registered; violating this crashes with `pj_thread_this: Assertion ... "Calling pjlib from unknown/external thread"`. This applies to:
- `SipCalleeAccount.onIncomingCall()` delayed auto-answer thread (`time.sleep(delay)` → `call.answer()`)
- `SipCalleeAutoAnswerCall._playback_then_hangup()` thread (creates `AudioMediaPlayer`, calls `hangup()`)
- `SipCalleeLiveTranscribeCall._play_wav()` thread (creates `AudioMediaPlayer`, calls `startTransmit()`)
- Any new `threading.Thread` that touches PJSIP objects

PJSUA2 callbacks (`onCallState`, `onCallMediaState`, `onFrameReceived`, etc.) run on PJSIP's own registered threads and do **not** need `libRegisterThread()`.

### MediaFormatAudio Initialisation
Always use `fmt.init(pj.PJMEDIA_FORMAT_PCM, clock_rate, channel_count, frame_time_usec, bits_per_sample)` instead of setting fields manually. Manual field assignment (`fmt.clockRate = ...`) does **not** set the internal `type`/`detail_type` discriminators, causing assertion failures (`PJMEDIA_PIA_CCNT: Assertion ... fmt.type==PJMEDIA_TYPE_AUDIO`).

After `make_call()`, a `CallResult` dataclass is stored as `caller.last_call_result`. `make_call()` accepts config class parameters (`CallConfig`, `WavPlayConfig`, `RecordingConfig`, `AudioDeviceConfig`, `SttConfig`, `VadConfig`) with fallback to `self.config` defaults. When `WavPlayConfig.tts_text` is set, TTS generation and temp-file cleanup happen inside `make_call()` automatically. For interactive live TTS, pass `tts_producer: PiperTTSProducer` and optionally `initial_tts_text: str` — `make_call()` creates and wires a `TTSMediaPort`, speaks the initial text after media is ready, then takes the "no WAV — wait for disconnect" path.

### Orphan Pattern for PJSIP Cleanup
`stop_wav()`, `stop_recording()`, `stop_audio_stream()`, `stop_transcription()`, and `stop_tts_port()` move media objects to an `_orphan_store` list instead of destroying immediately. Objects are only freed in `SipCaller.stop()` after the endpoint shuts down, avoiding "Remove port failed" warnings.

### Config Layering (`sipconfig.py`)
`SipEndpointConfig.from_config()` (inherited by `SipCallerConfig` and `SipCalleeConfig`) merges three sources (later wins):
1. YAML file (`ruamel.yaml`)
2. `SIP_*` environment variables (full map in `SipEndpointConfig.from_config()`: `SIP_SERVER`, `SIP_PORT`, `SIP_USER`, `SIP_PASSWORD`, `SIP_TRANSPORT`, `SIP_SRTP`, `SIP_TIMEOUT`, `SIP_TTS_MODEL`, `SIP_STUN_SERVERS`, etc.)
3. Python `overrides` dict

`SipEndpointConfig` holds endpoint-level infrastructure fields (`sip`, `nat`, `pjsip`, `audio`). `SipCallerConfig(SipEndpointConfig)` adds the 6 call-level defaults (`call`, `tts`, `recording`, `wav_play`, `stt`, `vad`) used only in `SipCaller.make_call()`. `SipCalleeConfig(SipEndpointConfig)` adds callee-specific fields (`auto_answer`, `answer_delay`). Pydantic v2 `extra="ignore"` (default) ensures that call-level env/override keys are silently dropped when constructing a plain `SipEndpointConfig`.

A `@model_validator(mode="before")` on each config class accepts both flat and nested dict forms. Sub-models: `SipConfig`, `CallConfig`, `TtsConfig`, `NatConfig` (STUN/ICE/TURN/keepalive).

`AudioDeviceConfig` supports separate null-device control for capture (TX) and playback (RX) via `null_capture` / `null_playback` fields. Both default to `None` and inherit from `use_null_audio` via a `model_validator`. Environment variables: `SIP_NULL_CAPTURE`, `SIP_NULL_PLAYBACK`. CLI flags: `--real-capture`, `--real-playback`.

### PJSIP Log Verbosity
Two environment variables control PJSIP log output (also settable via `SipCaller` constructor args):
- `PJSIP_LOG_LEVEL` (default 3): verbosity routed through loguru writer (0=none, 6=trace)
- `PJSIP_CONSOLE_LEVEL` (default 4): native PJSIP stdout output; set to 0 to suppress

### TTS via Python API (`tts/tts.py`)
Piper TTS (≥1.4.0) is called directly via the Python API (`PiperVoice.synthesize_wav()` / `PiperVoice.synthesize()`). No separate venv or subprocess needed. Optional resampling via soundfile/numpy to 8000/16000 Hz for SIP.

### Optional Dependencies
`pjsua2` and `faster_whisper` are imported with `try/except ImportError`, setting availability flags (`PJSUA2_AVAILABLE`). Errors raised only when the feature is actually used. PJSUA2 classes use conditional base classes: `pj.Call if PJSUA2_AVAILABLE else object`. Similarly, `sounddevice` has a `SOUNDDEVICE_AVAILABLE` flag in `sip_media.py`.

### Public API (`sipstuff/__init__.py`)
`make_sip_call`, `SipAccount`, `SipCaller`, `SipCallerCall`, `SipEndpoint`, `SipCallError`, `SipCallerConfig`, `generate_wav`, `TtsError`, `transcribe_wav`, `SttError`, `configure_logging`

### Experimental Subpackages
`transcribe/`, `realtime/`, `autoanswer/`, `training/` — the `autoanswer` and `realtime` packages are integrated into the CLI as `callee_autoanswer` and `callee_realtime-tts` subcommands. `transcribe/` and `training/` are standalone scripts (mostly German), not integrated into the main CLI.

## Dockerfile (Two-Stage Build)

1. **`pjsip-builder`** (`python:3.14-slim-trixie`): builds PJSIP from source, copies `.so` libs + Python SWIG bindings
2. **Main image** (`python:3.14-slim-trixie`): copies PJSIP artifacts, installs sipstuff (including `piper-tts` via pip). Runs as non-root `pythonuser` (UID 1200). Locale `de_DE.UTF-8`, tz `Europe/Berlin`. Entrypoint: `tini`.

Optional build args:
- `INSTALL_CUDA=true`: installs NVIDIA CUDA runtime libs for faster-whisper GPU inference
- `INSTALL_OPENVINO=true`: installs `optimum-intel[openvino]` for OpenVINO STT backend (Intel GPU/CPU)

## CI Pipeline (GitHub Actions)

`checkblack.yml` → `mypynpytests.yml` → `buildmultiarchandpush.yml` (amd64+arm64, pushes to DockerHub)

CI skips venv setup when `GITHUB_RUN_ID` is set — installs deps directly.

## Local SIP Test Stack (`simulate_files/`)

Asterisk-based integration test environment. Two SIP extensions: `1001` (TTS client) and `1002` (softphone).
- Start: `./simulate_files/stack.sh start` or `podman-compose -f simulate_files/docker-compose.yml up -d`
- Stop: `./simulate_files/stack.sh stop`

## Repo Scripts (`repo_scripts/`)

Secret injection pattern: `include.sh` sources the first `include.local.sh` found on disk. `include.local.sh` holds real credentials and is gitignored via `*.local.*` pattern. Any script needing secrets sources `include.sh` first.

Key flags: `DH_REPO_PUBLIC` / `GH_REPO_PUBLIC` (default `true`) control DockerHub/GitHub repo visibility in `initial_setup_github_dockerhub.sh`.

## Code Style

- **Black** line length 120, **isort** black profile
- **mypy** strict mode with pydantic plugin; `ignore_missing_imports` for: `pjsua2`, `faster_whisper`, `kubernetes`, `cv2`, `numpy`, `pytesseract`, `soundfile`, `sounddevice`, `piper`, `pynput`
- Logging via **loguru** with `classname` binding
- **Type annotations**: Always use concrete types instead of `Any`. This includes container types — use e.g. `list[str]`, `dict[str, int]`, `Sequence[CallConfig]` instead of `list[Any]`, `dict[str, Any]`, etc. `Any` should only be used as a last resort when the type is truly unknowable (e.g. untyped third-party APIs).
- Pre-commit hooks (`fail_fast: true`): check-yaml, black --check, mypy, gitleaks
