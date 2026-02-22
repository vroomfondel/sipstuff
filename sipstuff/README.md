# sipstuff — Core Package Reference

SIP telephony automation toolkit: place outgoing calls via PJSIP, play WAV or
Piper TTS audio, record remote-party audio, transcribe recordings with
faster-whisper, and stream live PCM audio over Unix domain sockets.  Designed
for headless/container operation (null audio device, no sound card required).

---

## Table of Contents

1. [Package Overview](#package-overview)
2. [Module Dependency Diagram](#module-dependency-diagram)
3. [Key Classes and Functions](#key-classes-and-functions)
4. [Public API (`__init__.py`)](#public-api-__init__py)
5. [SIP Engine (`sip_caller.py`)](#sip-engine-sip_callerpy)
6. [Configuration (`sipconfig.py`)](#configuration-sipconfigpy)
7. [CLI Entry Point (`cli.py`)](#cli-entry-point-clipy)
8. [Audio Utilities (`audio.py`)](#audio-utilities-audiopy)
9. [Shared PJSIP Helpers (`pjsip_common.py`)](#shared-pjsip-helpers-pjsip_commonpy)
10. [Text-to-Speech (`tts/`)](#text-to-speech-tts)
11. [Speech-to-Text (`stt/`)](#speech-to-text-stt)
12. [Environment Variables Reference](#environment-variables-reference)
13. [YAML Configuration Reference](#yaml-configuration-reference)
14. [NAT Traversal](#nat-traversal)

---

## Package Overview

```
sipstuff/
├── __init__.py          # Public API surface, configure_logging, make_sip_call
├── sip_caller.py        # PJSUA2 engine: SipCaller, SipCall, SipAccount, SilenceDetector, AudioStreamPort
├── sipconfig.py         # Pydantic v2 config: SipCallerConfig, load_config (YAML / env / overrides)
├── cli.py               # CLI entry point — call / tts / stt subcommands
├── audio.py             # resample_linear() — numpy linear interpolation resampler
├── pjsip_common.py      # Shared argparse + PJSIP helpers for experimental subpackages
├── tts/
│   ├── __init__.py      # Re-exports: generate_wav, TtsError, PiperTTSProducer, TTSMediaPort, audio constants
│   ├── tts.py           # Piper TTS via Python API, model auto-download, resampling
│   └── live.py          # Live TTS streaming: PiperTTSProducer, TTSMediaPort, interactive_console()
└── stt/
    ├── __init__.py      # Re-exports: transcribe_wav, SttError
    └── stt.py           # faster-whisper STT, Silero VAD, optional dependency
```

The package requires Python 3.14.  `pjsua2` (PJSIP C++ SWIG bindings) is not
pip-installable and must be built from source via `dist_scripts/install_pjsip.sh`
or the project Dockerfile.  `faster_whisper` is an optional dependency; the
package imports gracefully when it is absent.

---

## Module Dependency Diagram

```
cli.py
  ├── sipconfig.py  (load_config, SipCallerConfig)
  ├── sip_caller.py (SipCaller, SipCallError)
  │     ├── sipconfig.py
  │     └── [pjsua2]  (C extension, optional — graceful ImportError)
  ├── tts/tts.py    (generate_wav, TtsError)
  │     ├── audio.py  (resample_linear)
  │     └── [soundfile, piper]
  └── stt/stt.py    (transcribe_wav, SttError)
        └── [faster_whisper]  (optional — graceful ImportError)

__init__.py
  ├── sip_caller.py
  ├── sipconfig.py
  ├── tts/
  └── stt/

pjsip_common.py  (used only by experimental subpackages: transcribe/, realtime/, autoanswer/)
  ├── audio.py
  └── [pjsua2]
```

---

## Key Classes and Functions

### `sip_caller.py`

| Name | Type | Purpose |
|------|------|---------|
| `SipCaller` | class (context manager) | High-level PJSUA2 engine: endpoint lifecycle, account registration, call placement |
| `SipCall` | class (`pj.Call` subclass) | PJSUA2 call callbacks; bridges C++ events to Python `threading.Event` objects |
| `SipAccount` | class (`pj.Account` subclass) | Encapsulates SIP account registration, SRTP, ICE, TURN, keepalive |
| `SilenceDetector` | class (`pj.AudioMediaPort` subclass) | RMS-based silence detector; signals when continuous silence exceeds a threshold |
| `AudioStreamPort` | class (`pj.AudioMediaPort` subclass) | Streams raw PCM frames to a Unix domain socket |
| `_PjLogWriter` | class (`pj.LogWriter` subclass) | Routes native PJSIP logs through loguru and buffers them for JSON reports |
| `CallResult` | dataclass | Result metadata for a completed call (success, timestamps, disconnect reason) |
| `WavInfo` | class | Reads and validates WAV file metadata |
| `SipCallError` | exception | Raised on SIP registration, transport, or WAV playback errors |
| `_local_address_for()` | function | Resolves local IP for a given remote host via no-send UDP connect |

### `sipconfig.py`

| Name | Type | Purpose |
|------|------|---------|
| `SipCallerConfig` | Pydantic model | Top-level config aggregating four sub-models |
| `SipConfig` | Pydantic model | SIP server connection settings (server, port, user, password, transport, SRTP) |
| `CallConfig` | Pydantic model | Call timing and playback settings (timeout, delays, repeat, silence wait) |
| `TtsConfig` | Pydantic model | Piper TTS voice model and sample rate |
| `NatConfig` | Pydantic model | STUN, ICE, TURN, keepalive, public address override |
| `load_config()` | function | Merges YAML file + environment variables + Python overrides into a validated config |

### `tts/tts.py`

| Name | Type | Purpose |
|------|------|---------|
| `generate_wav()` | function | Synthesize text to a WAV file via piper Python API |
| `TtsError` | exception | Raised when piper is not found or synthesis fails |

### `stt/stt.py`

| Name | Type | Purpose |
|------|------|---------|
| `transcribe_wav()` | function | Transcribe a WAV file to text with faster-whisper |
| `SttError` | exception | Raised when faster-whisper is absent or transcription fails |

### `audio.py`

| Name | Type | Purpose |
|------|------|---------|
| `resample_linear()` | function | Resample a 1-D float audio array via numpy linear interpolation |

### `pjsip_common.py`

| Name | Type | Purpose |
|------|------|---------|
| `add_sip_args()` | function | Add `--sip-*` argument group to an `argparse.ArgumentParser` |
| `create_endpoint()` | function | Create and initialise a PJSUA2 `Endpoint` |
| `create_transport()` | function | Create a UDP transport on a PJSUA2 `Endpoint` |
| `use_null_audio()` | function | Activate the null audio device for headless operation |
| `ensure_wav_16k_mono()` | function | Convert a WAV file to 16 kHz / mono / 16-bit PCM |

---

## Public API (`__init__.py`)

`sipstuff/__init__.py` exposes the complete public surface of the package.
Everything listed in `__all__` is importable directly from `sipstuff`.

```python
from sipstuff import (
   make_sip_call,  # one-shot convenience wrapper
   SipCaller,  # context-manager engine
   SipCallerAccount,  # PJSUA2 account subclass
   SipCallError,  # SIP-related exceptions
   SipEndpointConfig,  # Pydantic config model
   load_config,  # config factory (YAML + env + overrides)
   generate_wav,  # Piper TTS
   TtsError,
   transcribe_wav,  # faster-whisper STT
   SttError,
   configure_logging,  # loguru sink setup
)
```

### `make_sip_call()`

One-shot convenience wrapper that handles PJSUA2 endpoint lifecycle and TTS
temp-file cleanup automatically.  Provide exactly one of `wav_file` or `text`.

```python
def make_sip_call(
    server: str,
    user: str,
    password: str,
    destination: str,
    wav_file: str | Path | None = None,   # mutually exclusive with text
    text: str | None = None,               # synthesized via piper TTS
    port: int = 5060,
    timeout: int = 60,
    transport: str = "udp",                # "udp" | "tcp" | "tls"
    pre_delay: float = 0.0,
    post_delay: float = 0.0,
    inter_delay: float = 0.0,
    repeat: int = 1,
    tts_model: str = "de_DE-thorsten-high",
) -> bool: ...
```

Returns `True` if the call was answered and playback started; `False` on
timeout or no answer.  Raises `SipCallError`, `TtsError`, or `ValueError`.

**Example:**

```python
from sipstuff import make_sip_call

# Play a WAV file
make_sip_call(
    server="pbx.example.com",
    user="1001",
    password="secret",
    destination="+491234567890",
    wav_file="alert.wav",
    repeat=3,
    inter_delay=2.0,
)

# Synthesize text via TTS and play it
make_sip_call(
    server="pbx.example.com",
    user="1001",
    password="secret",
    destination="1002",
    text="Achtung! Bitte sofort melden.",
    tts_model="de_DE-thorsten-high",
)
```

### `configure_logging()`

Configures a loguru sink to `stderr` with a coloured, structured format
including timestamp, log level, module, classname, function, and line.

```python
configure_logging()
# Override log level at startup:
import os; os.environ["LOGURU_LEVEL"] = "DEBUG"
configure_logging()
```

The format binds an `extra["classname"]` field used throughout the package.
A built-in `skiplog` filter suppresses records with `extra["skiplog"] = True`.

---

## SIP Engine (`sip_caller.py`)

### `SipCaller` — context manager

The central class.  Wraps the full PJSUA2 lifecycle: create endpoint → init →
start → register account → make calls → destroy.

```python
from sipstuff import SipCaller, load_config

config = load_config(overrides={"server": "pbx.local", "user": "1001", "password": "s3cr3t"})

with SipCaller(config) as caller:
    # Place one or more calls on the same registration
    success = caller.make_call("+491234567890", "alert.wav")
    print(caller.last_call_result)      # CallResult dataclass
    pjsip_logs = caller.get_pjsip_logs()  # list[str] for JSON reports
```

**Constructor parameters:**

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `config` | `SipCallerConfig` | required | Validated configuration |
| `pjsip_log_level` | `int \| None` | env `PJSIP_LOG_LEVEL` or 3 | PJSIP log verbosity routed to loguru (0=none, 6=trace) |
| `pjsip_console_level` | `int \| None` | env `PJSIP_CONSOLE_LEVEL` or 4 | PJSIP native console output level; set 0 to suppress |

**`make_call()` parameters:**

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `destination` | `str` | required | Phone number or full SIP URI |
| `wav_file` | `str \| Path` | required | WAV file to play on answer |
| `timeout` | `int \| None` | config value | Seconds to wait for answer |
| `pre_delay` | `float \| None` | config value | Seconds to wait after answer before playback |
| `post_delay` | `float \| None` | config value | Seconds to wait after playback before hangup |
| `inter_delay` | `float \| None` | config value | Seconds of silence between repeats |
| `repeat` | `int \| None` | config value | Number of times to play the WAV |
| `record_path` | `str \| Path \| None` | `None` | Record remote audio to this WAV path |
| `wait_for_silence` | `float \| None` | config value | Wait for N seconds of remote silence before playback |
| `audio_socket_path` | `str \| None` | `None` | Unix socket path for live PCM streaming |

### `SipCall` — PJSUA2 call callbacks

Subclasses `pj.Call` and bridges PJSIP C++ callbacks to Python
`threading.Event` objects so `SipCaller.make_call()` can synchronously
wait on call and media state.

| Event | Set when |
|-------|----------|
| `connected_event` | Call enters CONFIRMED state (answered) |
| `disconnected_event` | Call enters DISCONNECTED state |
| `media_ready_event` | An active audio media channel is available |

Key methods:

- `set_wav_path(path, autoplay=True)` — configure WAV file; `autoplay=False`
  lets `SipCaller` manage timing.
- `set_record_path(path)` — configure output WAV for recording.
- `set_audio_socket_path(path)` — configure Unix socket for live streaming.
- `play_wav()` — start looping WAV player.
- `stop_wav(_orphan_store)` — stop playback (see Orphan Pattern below).
- `start_recording()` / `stop_recording(_orphan_store)` — record remote audio.
- `start_audio_stream()` / `stop_audio_stream(_orphan_store)` — live PCM stream.

### `SipAccount` — account registration

Subclasses `pj.Account`.  Builds a complete `pj.AccountConfig` from
`SipCallerConfig` in its constructor, covering:

- Digest authentication credentials
- Media RTP socket binding to the correct local interface
- Public address override for SDP `c=` and Contact headers
- SRTP encryption mode (`disabled` / `optional` / `mandatory`)
- ICE, TURN relay, UDP keepalive (delegated to `NatConfig`)

### `SilenceDetector` — RMS-based silence detection

Subclasses `pj.AudioMediaPort`.  Attached to the call's conference bridge via
`startTransmit`.  Receives audio frames every ~20 ms and computes RMS energy.
When the RMS stays below `threshold` for `duration` continuous seconds, it
sets `silence_event`.

```python
detector = SilenceDetector(duration=1.5, threshold=200)
fmt = pj.MediaFormatAudio()
fmt.init(pj.PJMEDIA_FORMAT_PCM, 16000, 1, 20000, 16)
detector.createPort("silence_det", fmt)
call._audio_media.startTransmit(detector)
detector.silence_event.wait(timeout=10)
call._audio_media.stopTransmit(detector)
```

**SWIG quirk:** `MediaFrame.buf` is a `pj.ByteVector` (C++ `std::vector<unsigned char>`),
not Python `bytes`.  An explicit `bytes(frame.buf)` conversion is required
before passing it to `array.frombytes()`.

### `AudioStreamPort` — live PCM streaming

Subclasses `pj.AudioMediaPort`.  Connects as a Unix domain socket client on
construction.  Each `onFrameReceived()` call writes `bytes(frame.buf)` to the
socket.  On broken pipe or disconnection, subsequent frames are silently
discarded.

Audio format: 16 kHz, 16-bit signed LE, mono — compatible with
`aplay -r 16000 -f S16_LE -c 1 -t raw`.

**Usage:**

```bash
# Start a socat listener BEFORE placing the call
socat UNIX-LISTEN:/tmp/sip_audio.sock,fork EXEC:'aplay -r 16000 -f S16_LE -c 1 -t raw'
```

```python
caller.make_call(dest, "alert.wav", audio_socket_path="/tmp/sip_audio.sock")
```

### PJSIP Thread Registration

Any Python `threading.Thread` that calls PJSUA2 API functions **must** call
`pj.Endpoint.instance().libRegisterThread("<name>")` before making any PJSIP
calls.  PJSIP internally asserts that all calling threads are registered;
violating this crashes the process with:

```
pj_thread_this: Assertion `!"Calling pjlib from unknown/external thread. ..."' failed.
```

This requirement applies to daemon threads such as the delayed auto-answer
thread in `SipCalleeAccount.onIncomingCall()`, the `_playback_then_hangup()`
thread in `SipCalleeAutoAnswerCall`, and the `_play_wav()` thread in
`SipCalleeLiveTranscribeCall`.

PJSUA2 callbacks (`onCallState`, `onCallMediaState`, `onFrameReceived`, etc.)
run on PJSIP's own pre-registered threads and do **not** need this call.

### MediaFormatAudio Initialisation

Always initialise `pj.MediaFormatAudio` via `fmt.init()` — **never** by
setting individual fields manually:

```python
# Correct — sets type/detail_type discriminators internally
fmt = pj.MediaFormatAudio()
fmt.init(pj.PJMEDIA_FORMAT_PCM, 16000, 1, 20000, 16)

# WRONG — leaves type/detail_type uninitialised, causes assertion failure:
#   PJMEDIA_PIA_CCNT: Assertion ... fmt.type==PJMEDIA_TYPE_AUDIO
fmt = pj.MediaFormatAudio()
fmt.clockRate = 16000        # don't do this
fmt.channelCount = 1         # don't do this
fmt.bitsPerSample = 16       # don't do this
fmt.frameTimeUsec = 20000    # don't do this
```

Similarly, use `pj.PJMEDIA_FRAME_TYPE_AUDIO` (not `PJMEDIA_FRAME_AUDIO`,
which does not exist in the SWIG bindings) when checking frame types in
`onFrameReceived()`.

### Orphan Pattern for PJSIP Cleanup

`stop_wav()`, `stop_recording()`, and `stop_audio_stream()` accept an optional
`_orphan_store` list.  When provided, the `AudioMediaPlayer`, `AudioMediaRecorder`,
or `AudioStreamPort` object is moved into that list instead of being destroyed
immediately.  The objects are freed in `SipCaller.stop()` after the endpoint
shuts down, preventing the PJSIP "Remove port failed" warning that occurs when
CPython reference counting triggers the C++ destructor while the conference
bridge is still active.

```
make_call() exits
  → call.stop_wav(_orphan_store=self._orphaned_players)    # detach, defer destroy
  → call.stop_recording(_orphan_store=self._orphaned_players)
  → call.stop_audio_stream(_orphan_store=self._orphaned_players)
SipCaller.stop()
  → self._orphaned_players.clear()   # destroy while conference bridge still alive
  → ep.libDestroy()                  # now safe to shut down
```

### `_PjLogWriter` — PJSIP log routing

Subclasses `pj.LogWriter`.  Maps PJSIP integer log levels (1–6) to loguru
level names and emits each message via a loguru logger bound to
`classname="pjsip"`.  Also accumulates all messages in an internal buffer
accessible via `caller.get_pjsip_logs()`, which is included in JSON call
reports when `--transcribe` is used.

| PJSIP level | loguru level |
|-------------|--------------|
| 1 | ERROR |
| 2 | WARNING |
| 3 | INFO |
| 4 | DEBUG |
| 5, 6 | TRACE |

### `CallResult` dataclass

```python
@dataclasses.dataclass
class CallResult:
    success: bool           # True if call was answered and playback started
    call_start: float       # epoch timestamp when call was initiated
    call_end: float         # epoch timestamp when call finished
    call_duration: float    # wall-clock seconds
    answered: bool          # True if remote party answered
    disconnect_reason: str  # SIP disconnect reason string from PJSIP
```

Stored as `caller.last_call_result` after each `make_call()`.

---

## Configuration (`sipconfig.py`)

### Configuration Layering

Three sources are merged in order — later sources win:

```
1. YAML config file      (lowest priority)
2. SIP_* env variables
3. Python overrides dict  (highest priority)
```

### `load_config()`

```python
from sipstuff import load_config

# From YAML only
config = load_config(config_path="sip.yaml")

# From env variables only (server/user/password must be set via SIP_* vars)
config = load_config()

# From overrides only (flat dict — no YAML or env needed)
config = load_config(overrides={
    "server": "pbx.local",
    "user": "1001",
    "password": "secret",
    "timeout": 30,
    "tts_model": "en_US-lessac-high",
})

# Combine all three sources
config = load_config(config_path="sip.yaml", overrides={"timeout": 45})
```

### `SipCallerConfig` and Sub-models

`SipCallerConfig` accepts both flat and nested dict forms.  The
`_flatten_sip_fields` model validator reshapes flat dicts before Pydantic
validation.

#### `SipConfig` — SIP connection

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `server` | `str` | required | PBX hostname or IP address |
| `port` | `int` | `5060` | SIP port (1–65535) |
| `user` | `str` | required | SIP extension / username |
| `password` | `str` | required | SIP authentication password |
| `transport` | `"udp" \| "tcp" \| "tls"` | `"udp"` | SIP transport protocol |
| `srtp` | `"disabled" \| "optional" \| "mandatory"` | `"disabled"` | SRTP media encryption |
| `tls_verify_server` | `bool` | `False` | Verify TLS server certificate |
| `local_port` | `int` | `0` | Local bind port (0 = auto-assigned) |

#### `CallConfig` — call timing

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `timeout` | `int` | `60` | Max seconds to wait for answer (1–600) |
| `pre_delay` | `float` | `0.0` | Seconds to wait after answer before playback (0–30) |
| `post_delay` | `float` | `0.0` | Seconds to wait after playback before hangup (0–30) |
| `inter_delay` | `float` | `0.0` | Seconds of silence between WAV repeats (0–30) |
| `repeat` | `int` | `1` | Number of times to play the WAV (1–100) |
| `wait_for_silence` | `float` | `0.0` | Seconds of remote silence to wait for before playback (0–10); 0 = disabled |

#### `TtsConfig` — Piper TTS

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `model` | `str` | `"de_DE-thorsten-high"` | Piper voice model name (auto-downloaded on first use) |
| `sample_rate` | `int` | `0` | Resample TTS output to this rate in Hz; 0 = keep native (~22050 Hz) |

#### `NatConfig` — NAT traversal

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `stun_servers` | `list[str]` | `[]` | STUN servers for public IP discovery (`host:port`) |
| `stun_ignore_failure` | `bool` | `True` | Continue startup if STUN is unreachable |
| `ice_enabled` | `bool` | `False` | Enable ICE connectivity checks for media |
| `turn_enabled` | `bool` | `False` | Enable TURN relay (requires `turn_server`) |
| `turn_server` | `str` | `""` | TURN relay address (`host:port`) |
| `turn_username` | `str` | `""` | TURN authentication username |
| `turn_password` | `str` | `""` | TURN authentication password |
| `turn_transport` | `"udp" \| "tcp" \| "tls"` | `"udp"` | TURN transport protocol |
| `keepalive_sec` | `int` | `0` | UDP keepalive interval in seconds (0 = disabled) |
| `public_address` | `str` | `""` | Public IP to advertise in SDP `c=` and Contact headers |

---

## CLI Entry Point (`cli.py`)

Run as a module or via the installed entry point:

```bash
python -m sipstuff.cli {call,tts,stt,callee_autoanswer,callee_realtime-tts,callee_live-transcribe} [args...]
# or
sipstuff-cli {call,tts,stt,callee_autoanswer,callee_realtime-tts,callee_live-transcribe} [args...]
```

### `tts` subcommand

Generate a WAV file from text using piper TTS.

```bash
# Synthesize German text with the default model
python -m sipstuff.cli tts "Hallo Welt" -o hello.wav

# Use an English model, resample to 8 kHz narrowband
python -m sipstuff.cli tts "Hello World" -o hello.wav \
    --model en_US-lessac-high --sample-rate 8000

# Custom model cache directory
python -m sipstuff.cli tts "Text" -o out.wav \
    --data-dir /mnt/models/piper-voices
```

| Argument | Description |
|----------|-------------|
| `text` | Text to synthesize (positional) |
| `--output`, `-o` | Output WAV file path (required) |
| `--model`, `-m` | Piper voice model name (default: `de_DE-thorsten-high`) |
| `--sample-rate` | Resample output to this rate in Hz (0 = native) |
| `--data-dir` | Directory for piper voice models |
| `--verbose`, `-v` | Enable DEBUG logging |

### `stt` subcommand

Transcribe a WAV file to text using faster-whisper.

```bash
# Basic transcription (German, medium model)
python -m sipstuff.cli stt recording.wav

# English, small model, JSON output with metadata
python -m sipstuff.cli stt recording.wav --language en --model small --json

# Disable Silero VAD pre-filtering
python -m sipstuff.cli stt recording.wav --no-vad

# Use CUDA for faster inference
python -m sipstuff.cli stt recording.wav --device cuda --compute-type float16
```

| Argument | Description |
|----------|-------------|
| `wav` | Path to WAV file to transcribe (positional) |
| `--model`, `-m` | Whisper model size: `tiny` / `base` / `small` / `medium` / `large-v3` |
| `--language`, `-l` | Language code (default: `de`) |
| `--device` | Compute device: `cpu` (default) or `cuda` |
| `--compute-type` | Quantization: `int8` / `float16` / `float32` |
| `--data-dir` | Directory for Whisper model cache |
| `--json` | Output result as JSON with `audio_duration`, `language`, `language_probability`, `segments` |
| `--no-vad` | Disable Silero VAD pre-filtering |
| `--verbose`, `-v` | Enable DEBUG logging |

### `call` subcommand

Register with a SIP server, dial a destination, play audio, optionally record
and transcribe.

```bash
# Minimal: play a WAV (credentials from env vars SIP_SERVER / SIP_USER / SIP_PASSWORD)
python -m sipstuff.cli call --dest +491234567890 --wav alert.wav

# Play TTS-synthesized speech
python -m sipstuff.cli call --dest +491234567890 --text "Achtung!"

# Record remote audio and transcribe to a JSON call report
python -m sipstuff.cli call --dest +491234567890 --wav alert.wav \
    --record /tmp/recording.wav --transcribe

# Full example with all timing options
python -m sipstuff.cli call \
    --server 192.168.1.100 --port 5060 --transport udp --srtp disabled \
    --user 1001 --password secret \
    --dest +491234567890 \
    --text "Houston, wir haben ein Problem." \
    --tts-model de_DE-thorsten-high \
    --pre-delay 3.0 --post-delay 1.0 --inter-delay 2.1 --repeat 3 \
    --wait-for-silence 1.0 \
    --timeout 60 \
    --verbose

# Wait for remote silence before speaking (e.g. let callee finish "Hello?")
python -m sipstuff.cli call --dest 1002 --wav announcement.wav \
    --wait-for-silence 1.0

# Interactive live TTS — type text during the call that gets spoken in real-time
python -m sipstuff.cli call --dest +491234567890 \
    --interactive \
    --piper-model /path/to/de_DE-thorsten-high.onnx \
    --text "Hallo, hier spricht die Maschine." \
    --play-audio --play-tx --real-capture -v

# Stream live remote audio over a Unix socket while the call is active
socat UNIX-LISTEN:/tmp/sip_audio.sock,fork EXEC:'aplay -r 16000 -f S16_LE -c 1 -t raw' &
python -m sipstuff.cli call --dest 1002 --wav alert.wav \
    --audio-socket /tmp/sip_audio.sock

# Load settings from a YAML config file, override the destination at CLI
python -m sipstuff.cli call --config sip.yaml --dest +491234567890 --wav alert.wav
```

**SIP / auth arguments:**

| Argument | Description |
|----------|-------------|
| `--config`, `-c` | Path to YAML config file |
| `--server`, `-s` | PBX hostname or IP |
| `--port`, `-p` | SIP port (default: 5060) |
| `--user`, `-u` | SIP extension / username |
| `--password` | SIP password |
| `--transport` | `udp` / `tcp` / `tls` |
| `--srtp` | `disabled` / `optional` / `mandatory` |
| `--tls-verify` | Verify TLS server certificate |

**Audio source (`--wav` and `--interactive` are mutually exclusive):**

| Argument | Description |
|----------|-------------|
| `--wav`, `-w` | Path to WAV file to play |
| `--interactive` | Interactive live TTS mode: type text in the console during the call (requires `--piper-model`) |
| `--text` | Text to synthesize via piper TTS, or initial greeting in interactive mode |

**Call timing:**

| Argument | Description |
|----------|-------------|
| `--dest`, `-d` | Destination phone number or SIP URI (required) |
| `--timeout`, `-t` | Call timeout in seconds |
| `--pre-delay` | Seconds to wait after answer before playback |
| `--post-delay` | Seconds to wait after playback before hangup |
| `--inter-delay` | Seconds of silence between WAV repeats |
| `--repeat` | Number of times to play the WAV |
| `--wait-for-silence` | Wait for N seconds of remote silence before playback |

**Recording / streaming / TTS:**

| Argument | Description |
|----------|-------------|
| `--record` | Record remote-party audio to this WAV path |
| `--audio-socket` | Unix domain socket path for live PCM streaming |
| `--tts-model` | Piper voice model name (for pre-generated TTS) |
| `--piper-model` | Path to Piper `.onnx` model for live TTS in interactive mode |
| `--tts-sample-rate` | Resample TTS output to this rate |
| `--tts-data-dir` | Directory for piper voice models |
| `--transcribe` | Transcribe recorded audio and write a JSON call report (requires `--record`) |
| `--stt-model` | Whisper model size for transcription |
| `--stt-language` | Language code for STT (default: from config/env, then `de`) |
| `--stt-data-dir` | Directory for Whisper model cache |

**NAT traversal:**

| Argument | Description |
|----------|-------------|
| `--stun-servers` | Comma-separated STUN servers (`stun.l.google.com:19302`) |
| `--ice` | Enable ICE for media |
| `--turn-server` | TURN relay server (`host:port`); implies `--turn-enabled` |
| `--turn-username` | TURN username |
| `--turn-password` | TURN password |
| `--turn-transport` | TURN transport: `udp` / `tcp` / `tls` |
| `--keepalive` | UDP keepalive interval in seconds |
| `--public-address` | Public IP to advertise in SDP/Contact |

**Logging:**

| Argument | Description |
|----------|-------------|
| `--verbose`, `-v` | Enable DEBUG logging |
| `--pjsip-log-level` `0-6` | PJSIP log verbosity (0=none, 5=trace, 6=very verbose; default: 3) |

### JSON Call Report

When `--transcribe` is used together with `--record`, the CLI writes a JSON
report alongside the recording file (same name, `.json` extension):

```json
{
  "timestamp": "2026-02-20T14:32:00+01:00",
  "destination": "+491234567890",
  "wav_file": "alert.wav",
  "tts_text": null,
  "tts_model": null,
  "record_path": "/tmp/recording.wav",
  "call_duration": 12.4,
  "answered": true,
  "disconnect_reason": "Normal call clearing",
  "playback": {
    "repeat": 2,
    "pre_delay": 1.0,
    "post_delay": 0.5,
    "inter_delay": 2.0,
    "timeout": 60
  },
  "recording_duration": 8.2,
  "transcript": "Hallo, hier ist die Ansage.",
  "stt": {
    "model": "medium",
    "audio_duration": 8.2,
    "language": "de",
    "language_probability": 0.98,
    "segments": [...]
  },
  "pjsip_log": ["..."]
}
```

---

## Audio Utilities (`audio.py`)

### `resample_linear()`

```python
from sipstuff.audio import resample_linear
import numpy as np

samples = np.array([...], dtype=np.float32)   # 1-D audio signal
resampled = resample_linear(samples, source_rate=22050, target_rate=16000)
# Returns float32 array at 16 kHz
```

Uses `np.linspace` to compute target sample positions and `np.interp` for
linear interpolation.  This is sufficient for speech audio.  Music or signals
with significant high-frequency content benefit from a polyphase/sinc
resampler.

The function returns early (no copy) if `source_rate == target_rate`.

Used internally by `tts/tts.py` (`_resample_wav`) and
`pjsip_common.py` (`ensure_wav_16k_mono`).

---

## Shared PJSIP Helpers (`pjsip_common.py`)

This module is used exclusively by the **experimental subpackages**
(`transcribe/`, `autoanswer/`, `realtime/`) and is not part of the main
`SipCaller` engine.

### `add_sip_args(parser, *, include_sip_dest=False)`

Adds a standardised `--sip-*` argument group to any `argparse.ArgumentParser`.
Reads defaults from environment variables (`SIP_SERVER`, `SIP_USER`,
`SIP_PASSWORD`, `SIP_PORT`, `PUBLIC_IP`).

```python
import argparse
from sipstuff.pjsip_common import add_sip_args

parser = argparse.ArgumentParser()
add_sip_args(parser, include_sip_dest=True)  # adds --sip-dest as required arg
args = parser.parse_args()
```

### `create_endpoint(log_level=3)`

Creates, initialises, and returns a PJSUA2 `Endpoint` instance.

### `create_transport(ep, port=5060, public_ip="")`

Creates a UDP transport on a PJSUA2 `Endpoint`.

### `use_null_audio(ep)`

Activates the null audio device for headless operation:
`ep.audDevManager().setNullDev()`.

### `ensure_wav_16k_mono(input_path)`

Converts a WAV file to 16 kHz / mono / 16-bit PCM.  If the file already
matches, it is returned unchanged.  Otherwise a `_16k.wav` sibling is written
next to the original and its path returned.

Supports 8-bit (u8), 16-bit (s16), 32-bit (s32) input, stereo-to-mono
downmix, and arbitrary sample rate conversion via `resample_linear()`.

---

## Text-to-Speech (`tts/`)

### Live TTS Streaming (`tts/live.py`)

`PiperTTSProducer` and `TTSMediaPort` provide the building blocks for
real-time TTS audio streaming into PJSIP calls using a producer-consumer
pattern.

```python
from sipstuff.tts.live import PiperTTSProducer, TTSMediaPort, CLOCK_RATE
from queue import Queue

audio_queue: Queue[bytes] = Queue(maxsize=500)

producer = PiperTTSProducer(
    model_path="/models/de_DE-thorsten-high.onnx",
    audio_queue=audio_queue,
    target_rate=CLOCK_RATE,        # 16000 Hz
)
producer.start()
producer.speak("Hallo Welt!")      # non-blocking
producer.stop()
```

| Name | Type | Purpose |
|------|------|---------|
| `PiperTTSProducer` | class | Producer thread: synthesises text via Piper Python API, resamples, and enqueues 20 ms PCM chunks |
| `TTSMediaPort` | class (`pj.AudioMediaPort` subclass) | Consumer: dequeues PCM chunks every 20 ms in `onFrameRequested()` and feeds them to PJSIP |
| `CLOCK_RATE` | int | `16000` — audio sample rate |
| `SAMPLES_PER_FRAME` | int | `320` — samples per 20 ms frame |
| `BITS_PER_SAMPLE` | int | `16` — S16_LE |
| `CHANNEL_COUNT` | int | `1` — mono |
| `interactive_console()` | function | Reads text from stdin in a loop and calls `tts_producer.speak()` — generic console TTS loop for both callee and caller interactive modes |

Used by `sipstuff.realtime.pjsip_realtime_tts` (callee realtime-tts),
`sipstuff.sip_caller` (outgoing call live TTS), and `sipstuff.cli`
(`call --interactive` and `callee_realtime-tts --interactive` subcommands).

### `generate_wav()`

```python
from sipstuff.tts import generate_wav, TtsError

# Write to a specific file
path = generate_wav(
    text="Hallo, dies ist ein Test.",
    model="de_DE-thorsten-high",
    output_path="/tmp/test.wav",
    sample_rate=16000,   # 0 = keep native ~22050 Hz
    data_dir=None,       # defaults to PIPER_DATA_DIR or ~/.local/share/piper-voices
)

# Auto temp file (caller must clean up)
path = generate_wav(text="Hello World", model="en_US-lessac-high")
```

**How it works:**

1. `_ensure_model()` checks whether `{data_dir}/{model}.onnx` exists.  If not,
   it calls `piper.download_voices.download_voice()` to fetch the model from
   HuggingFace.
2. `PiperVoice.load()` loads the ONNX model into an inference session.
3. `PiperVoice.synthesize_wav()` synthesizes text directly into a WAV file.
4. If `sample_rate > 0`, `_resample_wav()` reads the WAV via `soundfile`,
   calls `resample_linear()`, and writes back as mono 16-bit PCM.

**Environment variables for TTS:**

| Variable | Default | Description |
|----------|---------|-------------|
| `PIPER_DATA_DIR` | `~/.local/share/piper-voices` | Directory for downloaded voice models |

---

## Speech-to-Text (`stt/`)

### `transcribe_wav()`

```python
from sipstuff.stt import transcribe_wav, SttError

text, meta = transcribe_wav(
    wav_path="/tmp/recording.wav",
    model="medium",          # tiny / base / small / medium / large-v3
    language="de",           # BCP-47 language code
    device="cpu",            # "cpu" or "cuda"
    compute_type="int8",     # int8 / float16 / float32; auto-selected if None
    data_dir=None,           # defaults to WHISPER_DATA_DIR or ~/.local/share/faster-whisper-models
    vad_filter=True,         # Silero VAD pre-filtering (strongly recommended for phone recordings)
)

print(text)
# "Hallo, hier ist die Automatische Ansage."

print(meta)
# {
#   "audio_duration": 4.7,
#   "language": "de",
#   "language_probability": 0.99,
#   "segments": [{"start": 0.0, "end": 4.7, "text": "Hallo ..."}]
# }
```

`faster_whisper` is an optional dependency.  If not installed, `transcribe_wav`
raises `SttError("faster-whisper not available...")` rather than an
`ImportError` at import time.

**Silero VAD** (`vad_filter=True`) is strongly recommended for phone call
recordings, which contain silence, ringing tones, and DTMF.  Disable only if
pre-trimmed audio is provided.

**Environment variables for STT:**

| Variable | Default | Description |
|----------|---------|-------------|
| `WHISPER_DATA_DIR` | `~/.local/share/faster-whisper-models` | Directory for downloaded Whisper models |
| `WHISPER_MODEL` | `medium` | Default model size |
| `WHISPER_DEVICE` | `cpu` | Compute device (`cpu` or `cuda`) |
| `WHISPER_COMPUTE_TYPE` | `int8` (cpu) / `float16` (cuda) | Quantization type |

---

## Environment Variables Reference

All `SIP_*` variables are read by `load_config()` and mapped to the
corresponding sub-model field.

### SIP Connection (`SipConfig`)

| Variable | Field | Default | Description |
|----------|-------|---------|-------------|
| `SIP_SERVER` | `sip.server` | — | PBX hostname or IP (required) |
| `SIP_PORT` | `sip.port` | `5060` | SIP port |
| `SIP_USER` | `sip.user` | — | SIP username / extension (required) |
| `SIP_PASSWORD` | `sip.password` | — | SIP password (required) |
| `SIP_TRANSPORT` | `sip.transport` | `udp` | `udp` / `tcp` / `tls` |
| `SIP_SRTP` | `sip.srtp` | `disabled` | `disabled` / `optional` / `mandatory` |
| `SIP_TLS_VERIFY_SERVER` | `sip.tls_verify_server` | `false` | Verify TLS certificate |
| `SIP_LOCAL_PORT` | `sip.local_port` | `0` | Local bind port (0 = auto) |

### Call Timing (`CallConfig`)

| Variable | Field | Default | Description |
|----------|-------|---------|-------------|
| `SIP_TIMEOUT` | `call.timeout` | `60` | Call timeout in seconds |
| `SIP_PRE_DELAY` | `call.pre_delay` | `0.0` | Seconds to wait after answer before playback |
| `SIP_POST_DELAY` | `call.post_delay` | `0.0` | Seconds to wait after playback before hangup |
| `SIP_INTER_DELAY` | `call.inter_delay` | `0.0` | Seconds of silence between WAV repeats |
| `SIP_REPEAT` | `call.repeat` | `1` | Number of times to play the WAV |
| `SIP_WAIT_FOR_SILENCE` | `call.wait_for_silence` | `0.0` | Seconds of remote silence before playback |

### TTS (`TtsConfig`)

| Variable | Field | Default | Description |
|----------|-------|---------|-------------|
| `SIP_TTS_MODEL` | `tts.model` | `de_DE-thorsten-high` | Piper voice model name |
| `SIP_TTS_SAMPLE_RATE` | `tts.sample_rate` | `0` | Resample TTS output (0 = native) |

### NAT Traversal (`NatConfig`)

| Variable | Field | Default | Description |
|----------|-------|---------|-------------|
| `SIP_STUN_SERVERS` | `nat.stun_servers` | `[]` | Comma-separated list of STUN servers |
| `SIP_STUN_IGNORE_FAILURE` | `nat.stun_ignore_failure` | `true` | Continue if STUN unreachable |
| `SIP_ICE_ENABLED` | `nat.ice_enabled` | `false` | Enable ICE |
| `SIP_TURN_ENABLED` | `nat.turn_enabled` | `false` | Enable TURN relay |
| `SIP_TURN_SERVER` | `nat.turn_server` | `""` | TURN server address |
| `SIP_TURN_USERNAME` | `nat.turn_username` | `""` | TURN username |
| `SIP_TURN_PASSWORD` | `nat.turn_password` | `""` | TURN password |
| `SIP_TURN_TRANSPORT` | `nat.turn_transport` | `udp` | TURN transport |
| `SIP_KEEPALIVE_SEC` | `nat.keepalive_sec` | `0` | UDP keepalive interval |
| `SIP_PUBLIC_ADDRESS` | `nat.public_address` | `""` | Public IP for SDP/Contact |

### PJSIP Logging

| Variable | Default | Description |
|----------|---------|-------------|
| `PJSIP_LOG_LEVEL` | `3` | PJSIP verbosity routed to loguru (0=none … 6=trace) |
| `PJSIP_CONSOLE_LEVEL` | `4` | Native PJSIP console output level; set 0 to suppress |

### General Logging

| Variable | Default | Description |
|----------|---------|-------------|
| `LOGURU_LEVEL` | `DEBUG` | loguru log level for the `stderr` sink |

---

## YAML Configuration Reference

```yaml
sip:
  server: pbx.example.com
  port: 5060
  user: "1001"
  password: "secret"
  transport: udp          # udp | tcp | tls
  srtp: disabled          # disabled | optional | mandatory
  tls_verify_server: false
  local_port: 0           # 0 = auto

call:
  timeout: 60
  pre_delay: 1.0
  post_delay: 0.5
  inter_delay: 2.0
  repeat: 2
  wait_for_silence: 1.0

tts:
  model: de_DE-thorsten-high
  sample_rate: 0          # 0 = native piper rate (~22050 Hz)

nat:
  stun_servers:
    - stun.l.google.com:19302
  stun_ignore_failure: true
  ice_enabled: false
  turn_enabled: false
  turn_server: ""
  turn_username: ""
  turn_password: ""
  turn_transport: udp
  keepalive_sec: 0
  public_address: ""      # e.g. "203.0.113.42" for K3s / container scenarios
```

---

## NAT Traversal

NAT traversal is disabled by default.  Enable features incrementally:

**STUN only** (discover public IP, no relay):

```bash
export SIP_STUN_SERVERS="stun.l.google.com:19302"
```

```yaml
nat:
  stun_servers: [stun.l.google.com:19302]
```

**STUN + ICE** (recommended for symmetric NAT):

```bash
export SIP_STUN_SERVERS="stun.l.google.com:19302"
export SIP_ICE_ENABLED="true"
```

**TURN relay** (for strict firewalls; `--turn-server` implies `turn_enabled`):

```bash
python -m sipstuff.cli call --dest 1002 --wav alert.wav \
    --stun-servers stun.l.google.com:19302 \
    --ice \
    --turn-server turn.example.com:3478 \
    --turn-username user \
    --turn-password secret \
    --turn-transport udp
```

**Public address override** (Kubernetes / container scenarios where the pod
IP is different from the node's public IP):

```bash
export SIP_PUBLIC_ADDRESS="203.0.113.42"
```

The socket stays bound to the actual local interface; only the SDP `c=` and
SIP `Contact` headers advertise the public address.

**UDP keepalive** (maintain NAT bindings for long-lived registrations):

```yaml
nat:
  keepalive_sec: 30
```
