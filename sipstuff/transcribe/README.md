# sipstuff/transcribe — Live SIP Call Transcription

Real-time transcription of outgoing SIP calls using VAD-based audio chunking and Faster Whisper. The script places a SIP call via PJSUA2, taps the incoming audio stream on the PJSIP conference bridge, and feeds detected speech segments to a background Whisper transcription thread. Optionally, a WAV file or TTS-synthesised announcement is played at the start of the call, and the full conversation can be recorded and post-processed into a stereo or mono mix.

This subpackage is a standalone script — it is **not** wired into the main `sipstuff` CLI and is intended for direct execution or use as a reference implementation.

---

## File Overview

| File | Description |
|---|---|
| `pjsip_live_transcribe.py` | Single-file script: all classes, VAD logic, CLI entry point |
| `__init__.py` | Empty package marker |

---

## Architecture

```
SIP Call (PJSUA2)
      │
      ▼ RX audio (remote party)
 TranscriptionPort ──► VADAudioBuffer ──► TranscriptionThread (Whisper)
      │                                         │
      │                              prints timestamped transcripts
      ▼
 WavRecorder (rx)           WavRecorder (tx) ◄── CapturePort (local mic)
      │                           │
      └─────────── mix_wav_files() ─────────► mixed output WAV
```

An optional `AudioStreamPort` (from `sipstuff.sip_caller`) can forward the raw RX audio to a Unix domain socket for external consumers.

---

## Classes

### `WavRecorder`

Thread-safe writer that continuously appends raw PCM frames to a WAV file on disk.

| Attribute / Method | Description |
|---|---|
| `__init__(filepath, sample_rate, channels, sample_width)` | Opens the WAV file for writing; defaults to 16 kHz, mono, 16-bit |
| `write_frames(pcm_bytes)` | Appends raw S16-LE bytes; protected by a `threading.Lock` |
| `close()` | Flushes and closes the file; logs total duration |
| `duration` (property) | Current recorded length in seconds |

Two instances are created per call when dual-channel recording is enabled: one for RX (remote party) and one for TX (local microphone).

---

### `VADAudioBuffer`

Thread-safe ring buffer with RMS-based Voice Activity Detection. Accumulates incoming PCM frames and signals when a complete speech chunk is ready for transcription.

**Flush conditions** (evaluated on every `add_frames()` call):

| Condition | Trigger |
|---|---|
| Silence after speech | `silence_counter >= silence_trigger_samples` and `len(buffer) >= min_samples` |
| Maximum duration | `len(buffer) >= max_samples` |

When a silence-triggered flush occurs, trailing silent samples are stripped from the chunk before it is queued, so Whisper receives clean speech boundaries.

| Key Method | Description |
|---|---|
| `add_frames(pcm_bytes)` | Convert S16-LE bytes to float32, run VAD, check flush |
| `get_chunk(timeout)` | Block until a chunk is ready; returns `(audio_array, start_sec, end_sec)` or `None` |
| `flush_remaining()` | Drain any buffered audio that did not yet hit a flush condition |

**Default VAD parameters** (overridable via CLI):

| Parameter | Default | Description |
|---|---|---|
| `silence_threshold` | `0.01` | RMS below this value is classified as silence |
| `silence_trigger_sec` | `0.3` s | Consecutive silence required to end a chunk |
| `max_duration_sec` | `5.0` s | Hard maximum chunk length |
| `min_duration_sec` | `0.5` s | Discard chunks shorter than this |

---

### `TranscriptionPort`

Subclass of `pj.AudioMediaPort`. Inserted into the PJSIP conference bridge on the receive (RX) path to intercept audio from the remote party.

The port format is initialised via `fmt.init(pj.PJMEDIA_FORMAT_PCM, 16000, 1, 20000, 16)` — never by setting fields manually (see PJSIP pitfalls below).

On every `onFrameReceived()` callback (every ~20 ms):
1. Checks `frame.type == pj.PJMEDIA_FRAME_TYPE_AUDIO` (note: `PJMEDIA_FRAME_AUDIO` does not exist in the SWIG bindings).
2. Converts the SWIG `pj.MediaFrame.buf` to `bytes()` explicitly (required SWIG workaround).
3. Passes PCM data to `VADAudioBuffer.add_frames()`.
4. If a `WavRecorder` is attached, writes the same data there in parallel for an uncut recording.

---

### `CapturePort`

Subclass of `pj.AudioMediaPort`. Inserted on the transmit (TX) path to capture outgoing audio from the local microphone. Only instantiated when `--record-tx` is active and the call is not using a null audio device (headless playback-only mode disables TX capture automatically).

---

### `TranscribingCall`

Subclass of `pj.Call`. Manages the full PJSUA2 call lifecycle and wires all media ports together.

| Method | Description |
|---|---|
| `set_audio_buffer(buf)` | Attach the `VADAudioBuffer` for transcription |
| `set_wav_recorders(rx, tx)` | Attach RX and optional TX `WavRecorder` instances |
| `set_playback(wav_path, play_delay)` | Queue a WAV file for playback at call start |
| `set_audio_socket_path(path)` | Enable forwarding of RX audio to a Unix socket via `AudioStreamPort` |
| `onCallState(prm)` | Sets `connected` and `disconnected` threading events |
| `onCallMediaState(prm)` | Creates and connects all ports to the active audio media stream |
| `_play_wav(media_idx)` | Spawns a thread that calls `libRegisterThread("wav-playback")` then starts `pj.AudioMediaPlayer` after an optional delay. Thread registration is required because PJSIP asserts that all calling threads are known. |

`_players` is used as an orphan store: `AudioMediaPlayer` objects are appended to the list rather than destroyed inline. The list is cleared only after `ep.libDestroy()` to avoid "Remove port failed" PJSIP warnings.

---

### `TranscriptionThread`

Background daemon thread that continuously dequeues audio chunks from `VADAudioBuffer` and transcribes them with Faster Whisper.

| Constructor Parameter | Default | Description |
|---|---|---|
| `audio_buffer` | — | `VADAudioBuffer` to drain |
| `model_name` | `"base"` | Whisper model size |
| `device` | `"cpu"` | `"cpu"` or `"cuda"` |
| `language` | `None` | Force language (e.g. `"de"`, `"en"`); `None` = auto-detect |
| `call_start_time` | `datetime.now()` | Reference point for absolute timestamps in output |

Transcription output is logged at INFO level in the format:

```
[HH:MM:SS.f–HH:MM:SS.f] (MM:SS.ff–MM:SS.ff)  transcribed text here
```

The first timestamp is wall-clock time; the second is relative call duration.

Whisper is invoked with `vad_filter=True` as a second-pass safety net (min_silence_duration_ms=200, speech_pad_ms=100) and `beam_size=5`. Compute type is `int8` on CPU and `float16` on CUDA.

After the `running` flag is cleared, `flush_remaining()` is called to transcribe any leftover audio before the thread exits.

---

### `MyAccount`

Minimal `pj.Account` subclass. Logs registration state changes and silently drops incoming calls.

---

### `mix_wav_files(rx_path, tx_path, output_path, mode)`

Standalone utility function. Reads the RX and TX WAV recordings, pads the shorter one with zeros, and writes a combined output file.

| Mode | Description |
|---|---|
| `"mono"` | Adds both channels, normalises if peak exceeds 32767 |
| `"stereo"` | Interleaves RX as left channel, TX as right channel |

---

## PJSIP Pitfalls

### Thread Registration

Any `threading.Thread` that calls PJSUA2 API functions must call
`pj.Endpoint.instance().libRegisterThread("<name>")` **before** any PJSIP call.
This applies to `_play_wav()` (WAV playback thread) and the delayed auto-answer
thread.  PJSIP callbacks (`onCallState`, `onCallMediaState`, `onFrameReceived`)
run on pre-registered threads and do not need this.

### MediaFormatAudio

Always use `fmt.init(pj.PJMEDIA_FORMAT_PCM, ...)` — never set fields manually
(`fmt.clockRate = ...`).  Manual assignment does not set the internal
`type`/`detail_type` discriminators, causing `PJMEDIA_PIA_CCNT` assertion
failures at runtime.

### Frame Type Constants

Use `pj.PJMEDIA_FRAME_TYPE_AUDIO` in `onFrameReceived()`.  The constant
`pj.PJMEDIA_FRAME_AUDIO` does not exist in the PJSUA2 SWIG bindings.

---

## VAD-Based Chunk Detection — Detailed Flow

```
onFrameReceived() called every ~20 ms
          │
          ▼
   Convert buf → bytes (SWIG)
          │
          ├──► WavRecorder.write_frames()   (uncut full recording)
          │
          └──► VADAudioBuffer.add_frames()
                    │
                    ▼
              Split into 10 ms windows (160 samples at 16 kHz)
              Compute RMS per window
                    │
                    ├── RMS < threshold → silence_counter += window_len
                    └── RMS ≥ threshold → silence_counter = 0, has_speech = True
                    │
                    ▼
              _check_flush():
                    │
                    ├── has_speech AND silence_counter ≥ trigger AND len ≥ min
                    │       → flush, trim trailing silence
                    │
                    └── len ≥ max_samples
                            → flush entire buffer
                    │
                    ▼
              chunk appended to ready_chunks
              chunk_ready Event set
                    │
                    ▼
         TranscriptionThread.get_chunk() unblocks
                    │
                    ▼
              WhisperModel.transcribe(chunk, vad_filter=True)
                    │
                    ▼
              log timestamped transcript
```

---

## Dependencies

| Dependency | Role |
|---|---|
| `pjsua2` | PJSIP SWIG Python bindings — must be compiled from source |
| `faster_whisper` | CTranslate2-based Whisper inference (`pip install faster-whisper`) |
| `numpy` | PCM-to-float32 conversion, RMS computation, WAV mixing |
| `loguru` | Structured logging throughout |
| `sipstuff.pjsip_common` | `add_sip_args`, `create_endpoint`, `create_transport`, `use_null_audio`, `ensure_wav_16k_mono` |
| `sipstuff.sip_caller.AudioStreamPort` | Optional Unix-socket audio forwarding |
| `sipstuff.tts.generate_wav` | TTS synthesis when `--tts-text` is used (imported lazily) |

---

## CLI Reference

```
python -m sipstuff.transcribe.pjsip_live_transcribe [OPTIONS]
# or directly:
python sipstuff/transcribe/pjsip_live_transcribe.py [OPTIONS]
```

### SIP Connection Arguments

| Argument | Default | Description |
|---|---|---|
| `--sip-dest DEST` | required | SIP destination (extension or URI) |
| `--sip-server HOST` | required | SIP registrar / proxy hostname or IP |
| `--sip-user USER` | — | SIP account username |
| `--sip-password PASS` | — | SIP account password |
| `--sip-port PORT` | `5060` | Local SIP UDP port |
| `--public-ip IP` | — | Public IP for NAT traversal |

### Whisper / Transcription Arguments

| Argument | Default | Description |
|---|---|---|
| `--whisper-model MODEL` | `base` | Model size: `tiny`, `base`, `small`, `medium`, `large-v3` |
| `--device DEVICE` | `cpu` | Inference device: `cpu` or `cuda` |
| `--language LANG` | auto | Force language code, e.g. `de`, `en`, `fr` |

### VAD / Chunking Arguments

| Argument | Default | Description |
|---|---|---|
| `--silence-threshold F` | `0.01` | RMS value below which audio is considered silence |
| `--silence-trigger F` | `0.3` | Seconds of consecutive silence that ends a chunk |
| `--max-chunk F` | `5.0` | Maximum chunk length in seconds |
| `--min-chunk F` | `0.5` | Minimum chunk length; shorter chunks are discarded |

### Recording Arguments

| Argument | Default | Description |
|---|---|---|
| `--wav-output PATH` | auto | Path for the RX WAV recording |
| `--wav-dir DIR` | `..` | Directory for auto-named WAV files |
| `--no-wav` | off | Disable WAV recording entirely |
| `--record-tx` | off | Also record the local microphone (TX) channel |
| `--mix-mode MODE` | `none` | Post-call mix: `none`, `mono`, `stereo` (implies `--record-tx`) |

### Playback Arguments (call start announcement)

| Argument | Default | Description |
|---|---|---|
| `--wav-file PATH` | — | WAV file to play at call start (mutually exclusive with `--tts-text`) |
| `--tts-text TEXT` | — | Text to synthesise with Piper TTS and play at call start |
| `--piper-model MODEL` | `de_DE-thorsten-high` | Piper TTS voice model |
| `--tts-data-dir DIR` | — | Directory containing Piper model files |
| `--play-delay F` | `0.0` | Seconds to wait after connect before starting playback |

### Live Audio Streaming

| Argument | Default | Description |
|---|---|---|
| `--audio-socket PATH` | — | Unix domain socket path for raw PCM streaming (16 kHz, S16-LE, mono) |

---

## Usage Examples

**Minimal — transcribe remote party, auto-detect language:**

```bash
python sipstuff/transcribe/pjsip_live_transcribe.py \
    --sip-dest 1234 \
    --sip-server pbx.example.com \
    --sip-user 1001 \
    --sip-password secret
```

**Force German, use a larger model, run on GPU:**

```bash
python sipstuff/transcribe/pjsip_live_transcribe.py \
    --sip-dest 1234 \
    --sip-server pbx.example.com \
    --sip-user 1001 \
    --sip-password secret \
    --whisper-model small \
    --device cuda \
    --language de
```

**Play a WAV announcement at call start (headless / null audio device):**

```bash
python sipstuff/transcribe/pjsip_live_transcribe.py \
    --sip-dest 1234 \
    --sip-server pbx.example.com \
    --sip-user 1001 \
    --sip-password secret \
    --wav-file /path/to/announcement.wav \
    --play-delay 1.0
```

**Synthesise an announcement via Piper TTS:**

```bash
python sipstuff/transcribe/pjsip_live_transcribe.py \
    --sip-dest 1234 \
    --sip-server pbx.example.com \
    --sip-user 1001 \
    --sip-password secret \
    --tts-text "Hello, please speak after the tone." \
    --piper-model en_US-lessac-high
```

**Record both channels and produce a stereo mix:**

```bash
python sipstuff/transcribe/pjsip_live_transcribe.py \
    --sip-dest 1234 \
    --sip-server pbx.example.com \
    --sip-user 1001 \
    --sip-password secret \
    --record-tx \
    --mix-mode stereo \
    --wav-dir /tmp/recordings
```

**Stream live audio to an external process via Unix socket:**

```bash
# Terminal 1 — consume raw PCM
socat UNIX-LISTEN:/tmp/sip_audio.sock,fork - | ffmpeg -f s16le -ar 16000 -ac 1 -i - output.mp3

# Terminal 2 — start the transcriber
python sipstuff/transcribe/pjsip_live_transcribe.py \
    --sip-dest 1234 \
    --sip-server pbx.example.com \
    --sip-user 1001 \
    --sip-password secret \
    --audio-socket /tmp/sip_audio.sock
```

**Tune VAD sensitivity for a noisy line:**

```bash
python sipstuff/transcribe/pjsip_live_transcribe.py \
    --sip-dest 1234 \
    --sip-server pbx.example.com \
    --sip-user 1001 \
    --sip-password secret \
    --silence-threshold 0.03 \
    --silence-trigger 0.5 \
    --max-chunk 8.0 \
    --min-chunk 1.0
```

---

## Output Files

Auto-generated filenames follow the pattern `call_YYYYMMDD_HHMMSS_<suffix>.wav` in `--wav-dir`:

| Suffix | Content |
|---|---|
| `_rx.wav` | Remote party audio (uncut, 16 kHz mono) |
| `_tx.wav` | Local microphone audio (uncut, 16 kHz mono) |
| `_mix.wav` | Mono mix of RX + TX (when `--mix-mode mono`) |
| `_stereo.wav` | Stereo file — left=RX, right=TX (when `--mix-mode stereo`) |

---

## Headless / Container Operation

When `--wav-file` or `--tts-text` is provided, the script automatically calls `use_null_audio(ep)` before `ep.libStart()`. This sets a null audio device on the PJSIP endpoint, which allows operation without a physical sound card — suitable for container or CI environments. In this mode TX capture is suppressed because there is no real microphone.
