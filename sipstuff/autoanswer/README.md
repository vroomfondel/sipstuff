# autoanswer — SIP Auto-Answering Server with WAV/TTS Playback

This subpackage provides a standalone SIP auto-answering server that registers with a SIP server,
waits for incoming calls, answers them automatically, and plays back a configurable audio sequence
(WAV files and/or Piper TTS announcements) before hanging up.

It is an experimental/standalone script and is not integrated into the main `sipstuff` CLI.

---

## Purpose

`pjsip_autoanswer_tts_n_wav.py` implements a headless SIP endpoint that:

1. Registers a SIP account with a configurable server using PJSUA2.
2. Listens indefinitely for incoming calls.
3. Automatically answers each call (with a configurable delay).
4. Plays an ordered sequence of WAV segments into the call audio stream.
5. Hangs up after the full sequence has been played.

Audio content can come from three sources, which can be freely combined:

- A **start WAV** played at the beginning of the call.
- A **content WAV** supplied directly (`--mode wav`) or synthesised on the fly from text using
  Piper TTS (`--mode tts`).
- An **end WAV** played just before hanging up.

The server uses a **null audio device** (no physical microphone or speaker required), making it
suitable for headless and container deployments.

---

## Architecture Overview

```
main()
  |
  +-- parse_args()                          Parse CLI arguments
  |
  +-- ensure_wav_16k_mono()                 Normalise all WAV inputs to 16 kHz / mono / 16-bit
  |    (from sipstuff.pjsip_common)
  |
  +-- generate_tts_wav()                    Synthesise TTS audio via PiperVoice (if --mode tts)
  |
  +-- PlaybackSequence([PlaybackSegment…])  Ordered list of WAV paths with per-segment pauses
  |
  +-- create_endpoint() / create_transport() / use_null_audio()
  |    (from sipstuff.pjsip_common)          PJSUA2 lifecycle
  |
  +-- create_account()                      Registers MyAccount with PJSIP
  |
  +-- event loop: ep.libHandleEvents(100)   Drives all PJSIP callbacks
```

### Call flow

```
Incoming INVITE
      |
MyAccount.onIncomingCall()
      |-- creates MyCall
      |-- sleeps answer_delay seconds (daemon thread)
      +-- libRegisterThread("answer")   ← required for PJSIP calls from new threads
      +-- MyCall.answer(200 OK)
              |
        MyCall.onCallMediaState()
              |-- connects incoming audio to null playback device
              |-- launches _playback_then_hangup() in daemon thread
                        |
                  libRegisterThread("playback")  ← required for PJSIP calls from new threads
                  AudioPlayer.play_sequence()
                        |-- for each PlaybackSegment:
                        |     sleep pause_before
                        |     create AudioMediaPlayer
                        |     startTransmit to call audio media
                        |     wait for WAV duration (or disconnect)
                        +-- MyCall.hangup() after last segment
```

---

## Classes

### `PlaybackSegment`

A `dataclass` representing a single audio segment in a playback sequence.

| Field | Type | Description |
|---|---|---|
| `wav_path` | `str` | Absolute or relative path to the WAV file to play. |
| `pause_before` | `float` | Seconds of silence to wait before playing this segment (default `0.0`). |

### `PlaybackSequence`

A `dataclass` holding an ordered list of `PlaybackSegment` objects.

| Field | Type | Description |
|---|---|---|
| `segments` | `list[PlaybackSegment]` | The segments to play in order. An empty list means no audio is played. |

### `AudioPlayer`

Manages sequential playback of a `PlaybackSequence` into a live PJSUA2 call.

| Method | Description |
|---|---|
| `__init__(sequence)` | Stores the sequence; initialises an internal player list. |
| `play_sequence(call, media_idx, disconnected)` | Iterates over segments, honouring per-segment pauses and a `threading.Event` that signals early call termination. Creates one `pj.AudioMediaPlayer` per segment and transmits audio into the call's conference bridge slot. |
| `stop_all()` | Stops and clears all active `AudioMediaPlayer` instances (called on disconnect). |

`AudioPlayer` creates a new `pj.AudioMediaPlayer` for each segment with `PJMEDIA_FILE_NO_LOOP` so
the file plays exactly once. It waits for the WAV duration plus 100 ms, then moves on to the next
segment. If the `disconnected` event fires at any point, playback is aborted immediately.

### `MyCall`

Subclass of `pj.Call`. Handles per-call state and orchestrates audio playback.

| Method | Description |
|---|---|
| `__init__(acc, sequence, call_id)` | Stores account reference, playback sequence, and creates the `_disconnected` event. |
| `onCallState(prm)` | Logs call state transitions. On `PJSIP_INV_STATE_DISCONNECTED` sets the `_disconnected` event and calls `AudioPlayer.stop_all()`. |
| `onCallMediaState(prm)` | When an active audio media stream is detected: optionally connects incoming audio to the null playback device; starts `_playback_then_hangup()` in a daemon thread. If no segments are configured, falls back to connecting the capture device (normal call behaviour). |
| `_playback_then_hangup(media_idx)` | Calls `libRegisterThread("playback")`, creates an `AudioPlayer`, drives the full sequence, then hangs up the call if it is still connected. Thread registration is required because this runs in a daemon thread, not a PJSIP callback thread. |

### `MyAccount`

Subclass of `pj.Account`. Handles SIP registration and incoming call dispatch.

| Method | Description |
|---|---|
| `__init__(sequence)` | Stores the `PlaybackSequence` that will be handed to every new `MyCall`. |
| `onRegState(prm)` | Logs successful registration or registration failure. |
| `onIncomingCall(prm)` | Creates a `MyCall`, appends it to `self.calls`, then (if `auto_answer` is enabled) schedules `call.answer()` after `answer_delay` seconds in a daemon thread. The thread calls `pj.Endpoint.instance().libRegisterThread("answer")` before any PJSIP API call — required because PJSIP asserts thread registration. |

### `generate_tts_wav` (module-level function)

Synthesises speech from text using the Piper TTS Python API and writes a WAV file.

```python
def generate_tts_wav(text: str, model_path: str, output_path: str) -> str
```

| Parameter | Description |
|---|---|
| `text` | The text to synthesise. |
| `model_path` | Path to a Piper `.onnx` voice model file. |
| `output_path` | Destination path for the generated WAV file. |

Returns the `output_path`. Exits with an error if `piper-tts` is not installed or the model file
is missing.

### `create_account` (module-level function)

Builds a `pj.AccountConfig` from parsed CLI arguments and creates a `MyAccount`.

```python
def create_account(args: argparse.Namespace, sequence: PlaybackSequence) -> MyAccount
```

Configures digest authentication, sets the registrar URI to `sip:<sip_server>`, and uses a 30-second
retry interval.

### `get_wav_duration` (module-level function)

Returns the duration of a WAV file in seconds.

```python
def get_wav_duration(path: str) -> float
```

---

## Piper TTS Integration

In `--mode tts`, the script calls `generate_tts_wav()` which imports `PiperVoice` directly from
the `piper` Python package (the in-process API, not a subprocess):

```python
from piper import PiperVoice

voice = PiperVoice.load(model_path)
with wave.open(output_path, "wb") as wav_file:
    voice.synthesize(text, wav_file)
```

The synthesised WAV is then passed through `ensure_wav_16k_mono()` (from `sipstuff.pjsip_common`)
to normalise it to 16 kHz / mono / 16-bit PCM before playback via PJSUA2.

### Downloading a Piper voice model

```bash
# German voice (Thorsten, high quality)
wget https://huggingface.co/rhasspy/piper-voices/resolve/main/de/de_DE/thorsten/high/de_DE-thorsten-high.onnx
wget https://huggingface.co/rhasspy/piper-voices/resolve/main/de/de_DE/thorsten/high/de_DE-thorsten-high.onnx.json
```

Both the `.onnx` model file and the accompanying `.onnx.json` config file must be present in the
same directory. Pass the `.onnx` path to `--piper-model`.

> Note: In the main `sipstuff` package, Piper TTS is invoked via a subprocess into a separate
> Python 3.13 venv (because `piper-phonemize-fix` lacks 3.14 wheels). This autoanswer script uses
> the direct in-process `PiperVoice` API instead and therefore requires `piper-tts` to be
> installed in the same environment as the script.

---

## Dependencies

| Dependency | Source | Role |
|---|---|---|
| `pjsua2` | Built from PJSIP C source (`dist_scripts/install_pjsip.sh`) | SIP stack, audio media, call control |
| `piper-tts` | `pip install piper-tts` | In-process TTS synthesis (`--mode tts` only) |
| `loguru` | `pip install loguru` | Structured logging |
| `sipstuff.pjsip_common` | This repo | `add_sip_args`, `create_endpoint`, `create_transport`, `use_null_audio`, `ensure_wav_16k_mono` |
| `sipstuff.audio` | This repo | `resample_linear` (used internally by `ensure_wav_16k_mono`) |
| `numpy` | `pip install numpy` | Used by `resample_linear` for WAV resampling |

`pjsua2` is not pip-installable. It must be compiled from the PJSIP C source. See the project-level
`dist_scripts/install_pjsip.sh` or the Dockerfile for build instructions.

---

## Configuration

### Global CONFIG dict

The module-level `CONFIG` dict holds runtime settings updated from CLI arguments:

| Key | Default | CLI argument | Description |
|---|---|---|---|
| `transport` | `"udp"` | — | Transport protocol (always UDP). |
| `log_level` | `int($LOG_LEVEL)` or `3` | — | PJSIP log verbosity (0–6). |
| `auto_answer` | `True` | `--no-auto-answer` | Whether to answer calls automatically. |
| `answer_delay` | `1.0` | `--answer-delay` | Seconds to wait before answering. |

### Environment variables for SIP credentials

| Variable | Default | Description |
|---|---|---|
| `SIP_SERVER` | `127.0.0.1` | SIP server hostname or IP address. |
| `SIP_USER` | `testuser` | SIP account username. |
| `SIP_PASSWORD` | `testpassword` | SIP account password. |
| `SIP_PORT` | `5060` | Local UDP port for the SIP transport. |
| `PUBLIC_IP` | `""` | Optional public IP for NAT traversal. |
| `LOG_LEVEL` | `3` | PJSIP log level (0 = none, 6 = verbose). |

---

## CLI Arguments

Run the script directly:

```bash
python -m sipstuff.autoanswer.pjsip_autoanswer_tts_n_wav [OPTIONS]
# or
python sipstuff/autoanswer/pjsip_autoanswer_tts_n_wav.py [OPTIONS]
```

### Audio mode

| Argument | Choices | Default | Description |
|---|---|---|---|
| `--mode` | `none`, `wav`, `tts` | `none` | Select audio playback mode. `none` answers without playing audio; `wav` plays a WAV file; `tts` synthesises speech with Piper. |

### WAV mode options (`--mode wav`)

| Argument | Default | Description |
|---|---|---|
| `--wav-file PATH` | — | Path to the WAV file to play (required for `--mode wav`). |

### TTS mode options (`--mode tts`)

| Argument | Default | Description |
|---|---|---|
| `--tts-text TEXT` | — | Text to synthesise and play (required for `--mode tts`). |
| `--piper-model PATH` | `./de_DE-thorsten-high.onnx` | Path to the Piper `.onnx` voice model file. |

### Sequence options

These arguments allow building a multi-segment playback sequence around the main content audio.

| Argument | Type | Default | Description |
|---|---|---|---|
| `--start-wav PATH` | str | — | WAV file to play before the content segment. |
| `--end-wav PATH` | str | — | WAV file to play after the content segment (before hangup). |
| `--pause-before-start SECS` | float | `0.0` | Pause before the start WAV (seconds). |
| `--pause-before-content SECS` | float | `0.0` | Pause before the content WAV/TTS (seconds). |
| `--pause-before-end SECS` | float | `0.0` | Pause before the end WAV (seconds). |

### SIP connection options (from `pjsip_common.add_sip_args`)

| Argument | Env var | Default | Description |
|---|---|---|---|
| `--sip-server HOST` | `SIP_SERVER` | `127.0.0.1` | SIP server domain or IP. |
| `--sip-user USER` | `SIP_USER` | `testuser` | SIP account username. |
| `--sip-password PASS` | `SIP_PASSWORD` | `testpassword` | SIP account password. |
| `--sip-port PORT` | `SIP_PORT` | `5060` | Local SIP UDP port. |
| `--public-ip IP` | `PUBLIC_IP` | `""` | Public IP for NAT (optional). |

### Answer control

| Argument | Default | Description |
|---|---|---|
| `--answer-delay SECS` | `1.0` | Seconds to wait after receiving the INVITE before sending 200 OK. |
| `--no-auto-answer` | — | Disable automatic call answering (calls will ring but not be picked up). |

---

## Usage Examples

### Answer calls without playing audio

```bash
python sipstuff/autoanswer/pjsip_autoanswer_tts_n_wav.py \
    --sip-server 192.168.1.100 \
    --sip-user 1001 \
    --sip-password secret
```

### Play a WAV file on every incoming call

```bash
python sipstuff/autoanswer/pjsip_autoanswer_tts_n_wav.py \
    --sip-server 192.168.1.100 \
    --sip-user 1001 \
    --sip-password secret \
    --mode wav \
    --wav-file /path/to/announcement.wav
```

### Synthesise and play a TTS announcement

```bash
python sipstuff/autoanswer/pjsip_autoanswer_tts_n_wav.py \
    --sip-server 192.168.1.100 \
    --sip-user 1001 \
    --sip-password secret \
    --mode tts \
    --tts-text "Welcome. Please leave a message after the beep." \
    --piper-model ./de_DE-thorsten-high.onnx
```

### Use environment variables for credentials

```bash
export SIP_SERVER="192.168.1.100"
export SIP_USER="1001"
export SIP_PASSWORD="secret"

python sipstuff/autoanswer/pjsip_autoanswer_tts_n_wav.py \
    --mode tts \
    --tts-text "Guten Tag, bitte hinterlassen Sie eine Nachricht."
```

### Play a greeting, a TTS message, and a farewell WAV in sequence

```bash
python sipstuff/autoanswer/pjsip_autoanswer_tts_n_wav.py \
    --sip-server 192.168.1.100 \
    --sip-user 1001 \
    --sip-password secret \
    --mode tts \
    --tts-text "Bitte hinterlassen Sie Ihre Nachricht nach dem Signalton." \
    --piper-model ./de_DE-thorsten-high.onnx \
    --start-wav ./greeting.wav \
    --end-wav ./beep.wav \
    --pause-before-start 0.5 \
    --pause-before-content 0.3 \
    --pause-before-end 0.2
```

This builds the following `PlaybackSequence`:

```
[PlaybackSegment("greeting_16k.wav", pause_before=0.5),
 PlaybackSegment("/tmp/pjsip_tts_ansage_16k.wav", pause_before=0.3),
 PlaybackSegment("beep_16k.wav", pause_before=0.2)]
```

### Adjust answer delay and log verbosity

```bash
LOG_LEVEL=5 python sipstuff/autoanswer/pjsip_autoanswer_tts_n_wav.py \
    --sip-server 192.168.1.100 \
    --sip-user 1001 \
    --sip-password secret \
    --answer-delay 2.0 \
    --mode wav \
    --wav-file announcement.wav
```

---

## WAV Format Normalisation

All WAV files (both user-supplied and TTS-generated) are automatically converted to **16 kHz,
mono, 16-bit PCM** before being passed to PJSUA2, using `ensure_wav_16k_mono()` from
`sipstuff.pjsip_common`. If the input file already matches this format it is used as-is; otherwise
a `_16k.wav` sibling file is written next to the original.

The conversion pipeline handles:

- 8-bit, 16-bit, and 32-bit integer PCM.
- Stereo-to-mono downmix (channel averaging).
- Multi-channel to mono (first channel only).
- Sample-rate conversion via linear interpolation (`sipstuff.audio.resample_linear`).

---

## Null Audio Device

The server calls `use_null_audio(ep)` from `sipstuff.pjsip_common` to activate PJSIP's built-in
null audio device (`ep.audDevManager().setNullDev()`). This means no physical sound card is
required, and the process can run inside a Docker container or on a headless server without audio
hardware.

Incoming audio from the caller is routed to the null playback device (silently discarded).
Outgoing audio (WAV playback) is injected directly into the call's conference bridge slot via
`pj.AudioMediaPlayer.startTransmit(call.getAudioMedia(media_idx))`.

---

## Lifecycle and Shutdown

The main event loop calls `ep.libHandleEvents(100)` every 100 ms. On `KeyboardInterrupt` or any
unhandled exception, the `finally` block:

1. Calls `acc.shutdown()` to unregister the SIP account.
2. Calls `ep.libDestroy()` to tear down the PJSUA2 endpoint.

This mirrors the lifecycle pattern used by the main `SipCaller` context manager in
`sipstuff/sip_caller.py`, but without the orphan-store cleanup for `AudioMediaPlayer` instances
that the main caller uses.
