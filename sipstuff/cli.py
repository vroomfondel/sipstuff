#!/usr/bin/env python3
"""CLI entry point for sipstuff: SIP calls, TTS, STT, and callee automations.

Provides the ``python -m sipstuff.cli`` command with five subcommands:

- **tts**: Generate a WAV file from text using piper TTS.
- **stt**: Transcribe a WAV file to text using faster-whisper.
- **call**: Register with a SIP server, dial a destination, play a WAV file
  or piper-TTS-generated speech, and hang up.
- **callee_autoanswer**: Auto-answer incoming calls and play WAV/TTS audio.
- **callee_realtime-tts**: Auto-answer incoming calls with real-time Piper TTS.

Examples:
    # TTS: generate a WAV file from text
    $ python -m sipstuff.cli tts "Hallo Welt" -o hello.wav
    $ python -m sipstuff.cli tts "Hello World" -o hello.wav --model en_US-lessac-high --sample-rate 8000

    # STT: transcribe a WAV file to text
    $ python -m sipstuff.cli stt recording.wav
    $ python -m sipstuff.cli stt recording.wav --language en --model small --json
    $ python -m sipstuff.cli stt recording.wav --no-vad  # disable Silero VAD pre-filtering

    # Call: place a SIP call
    $ python -m sipstuff.cli call --dest +491234567890 --wav alert.wav
    $ python -m sipstuff.cli call --dest +491234567890 --text "Achtung!"
    $ python -m sipstuff.cli call --dest +491234567890 --wav alert.wav \
        --wait-for-silence 1.0 --record /tmp/recording.wav --transcribe
    $ python -m sipstuff.cli call --server 192.168.123.123 --port 5060 \
        --transport udp --srtp disabled --user sipuser --password sippasword \
        --dest +491234567890 --text "Houston, wir haben ein Problem." \
        --pre-delay 3.0 --post-delay 1.0 --inter-delay 2.1 --repeat 3 -v

    # Callee auto-answer: answer calls and play a TTS announcement
    $ python -m sipstuff.cli callee_autoanswer --server 192.168.1.100 --user 1001 \
        --password secret --mode tts --tts-text "Willkommen!"

    # Callee realtime TTS: answer calls with interactive real-time speech
    $ python -m sipstuff.cli callee_realtime-tts --server 192.168.1.100 --user 1001 \
        --password secret --interactive --tts-text "Hallo!"
"""

import argparse
import json
import os
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from queue import Queue

from loguru import logger

from sipstuff import SipCalleeAccount, SipCaller, __version__, configure_logging, print_banner
from sipstuff.sip_types import SipCallError
from sipstuff.sipconfig import (
    AudioDeviceConfig,
    PlaybackSequence,
    RecordingConfig,
    SipCalleeConfig,
    SipCallerConfig,
    SipEndpointConfig,
    SttConfig,
    TtsConfig,
    TtsPlayConfig,
    VadConfig,
    WavPlayConfig,
)

# ── Shared argparse helpers ────────────────────────────────────────────


def _add_sip_connection_args(parser: argparse.ArgumentParser) -> None:
    """Add SIP connection arguments shared by call and callee subcommands.

    Registers ``--config``, ``--server``, ``--port``, ``--user``,
    ``--password``, ``--transport``, ``--srtp``, and ``--tls-verify``
    on the given parser.

    Args:
        parser: The argparse parser (or subparser) to augment.
    """
    parser.add_argument("--config", "-c", help="Path to YAML config file")
    parser.add_argument("--server", "-s", help="PBX hostname or IP")
    parser.add_argument("--port", "-p", type=int, help="SIP port (default: 5060)")
    parser.add_argument("--user", "-u", help="SIP extension / username")
    parser.add_argument("--password", help="SIP password")
    parser.add_argument("--transport", choices=["udp", "tcp", "tls"], help="SIP transport (default: udp)")
    parser.add_argument(
        "--srtp", choices=["disabled", "optional", "mandatory"], help="SRTP encryption (default: disabled)"
    )
    parser.add_argument(
        "--tls-verify",
        dest="tls_verify_server",
        action="store_true",
        default=None,
        help="Verify TLS server certificate",
    )


def _add_nat_args(parser: argparse.ArgumentParser) -> None:
    """Add NAT traversal arguments shared by call and callee subcommands.

    Registers ``--stun-servers``, ``--ice``, ``--turn-*``, ``--keepalive``,
    and ``--public-address`` in a dedicated argument group.

    Args:
        parser: The argparse parser (or subparser) to augment.
    """
    nat_group = parser.add_argument_group("NAT traversal")
    nat_group.add_argument(
        "--stun-servers", dest="stun_servers", help="Comma-separated STUN servers (e.g. stun.l.google.com:19302)"
    )
    nat_group.add_argument("--ice", dest="ice_enabled", action="store_true", default=None, help="Enable ICE for media")
    nat_group.add_argument("--turn-server", dest="turn_server", help="TURN relay server (host:port)")
    nat_group.add_argument("--turn-username", dest="turn_username", help="TURN username")
    nat_group.add_argument("--turn-password", dest="turn_password", help="TURN password")
    nat_group.add_argument(
        "--turn-transport", dest="turn_transport", choices=["udp", "tcp", "tls"], help="TURN transport (default: udp)"
    )
    nat_group.add_argument("--keepalive", dest="keepalive_sec", type=int, help="UDP keepalive interval in seconds")
    nat_group.add_argument(
        "--public-address", dest="public_address", help="Public IP to advertise in SDP/Contact (e.g. K3s node IP)"
    )


def _add_logging_args(parser: argparse.ArgumentParser) -> None:
    """Add logging arguments shared by call and callee subcommands.

    Registers ``--verbose`` and ``--pjsip-log-level`` on the given parser.

    Args:
        parser: The argparse parser (or subparser) to augment.
    """
    parser.add_argument("--verbose", "-v", action="store_true", help="Verbose logging (DEBUG level)")
    parser.add_argument(
        "--pjsip-log-level",
        dest="pjsip_log_level",
        type=int,
        choices=range(7),
        metavar="0-6",
        help="PJSIP log verbosity (0=none, 5=trace, 6=very verbose; default: 3)",
    )


# ── Shared override builder ───────────────────────────────────────────


def _build_sip_overrides(args: argparse.Namespace) -> dict[str, object]:
    """Build a config-override dict from shared SIP/NAT CLI args.

    Iterates the connection and NAT keys present on *args*, skips ``None``
    values, handles ``stun_servers`` comma-split and ``turn_server`` →
    ``turn_enabled`` implication.
    """
    overrides: dict[str, object] = {}
    for key in (
        "server",
        "port",
        "user",
        "password",
        "transport",
        "srtp",
        "tls_verify_server",
        "ice_enabled",
        "turn_server",
        "turn_username",
        "turn_password",
        "turn_transport",
        "keepalive_sec",
        "public_address",
    ):
        val = getattr(args, key, None)
        if val is not None:
            overrides[key] = val

    # --stun-servers: comma-separated → list
    stun = getattr(args, "stun_servers", None)
    if stun:
        overrides["stun_servers"] = [s.strip() for s in stun.split(",") if s.strip()]
    # --turn-server implies turn_enabled
    if getattr(args, "turn_server", None):
        overrides["turn_enabled"] = True

    # PJSIP log level → config.pjsip.log_level
    pjsip_ll = getattr(args, "pjsip_log_level", None)
    if pjsip_ll is not None:
        overrides["pjsip_log_level"] = pjsip_ll

    return overrides


# ── Argument parsing ──────────────────────────────────────────────────


def parse_args() -> argparse.Namespace:
    """Parse command-line arguments for the sipstuff CLI.

    Returns:
        Parsed argument namespace.  The ``command`` attribute identifies
        the subcommand.
    """
    parser = argparse.ArgumentParser(
        prog="sipstuff",
        description="sipstuff — SIP calls, text-to-speech, and speech-to-text",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # ── tts subcommand ──────────────────────────────────────────────
    tts_parser = sub.add_parser("tts", help="Generate a WAV file from text using piper TTS")
    tts_parser.add_argument("text", help="Text to synthesize")
    tts_parser.add_argument("--output", "-o", default=None, help="Output WAV file path")
    tts_parser.add_argument(
        "--model", "-m", default="de_DE-thorsten-high", help="Piper voice model (default: de_DE-thorsten-high)"
    )
    tts_parser.add_argument(
        "--sample-rate", dest="sample_rate", type=int, default=0, help="Resample output to this rate in Hz (0 = native)"
    )
    tts_parser.add_argument(
        "--tts-data-dir",
        dest="tts_data_dir",
        help="Directory for piper voice models (default: ~/.local/share/piper-voices)",
    )
    tts_parser.add_argument(
        "--play-audio",
        dest="play_audio",
        action="store_true",
        default=False,
        help="Play the generated audio on speakers (requires sounddevice + soundfile)",
    )
    tts_parser.add_argument(
        "--audio-device",
        dest="audio_device",
        default=None,
        help="Sounddevice output device index (int) or name substring for --play-audio (default: system default)",
    )
    tts_parser.add_argument("--verbose", "-v", action="store_true", help="Verbose logging (DEBUG level)")

    # ── stt subcommand ──────────────────────────────────────────────
    stt_parser = sub.add_parser("stt", help="Transcribe a WAV file to text using faster-whisper")
    stt_parser.add_argument("wav", help="Path to WAV file to transcribe")
    stt_parser.add_argument(
        "--backend",
        choices=["faster-whisper", "openvino"],
        default="faster-whisper",
        help="STT backend engine (default: faster-whisper)",
    )
    stt_parser.add_argument("--model", "-m", help="Whisper model size or HuggingFace model ID (default: medium)")
    stt_parser.add_argument("--language", "-l", default="de", help="Language code for transcription (default: de)")
    stt_parser.add_argument("--device", choices=["cpu", "cuda"], help="Compute device (default: cpu)")
    stt_parser.add_argument("--compute-type", dest="compute_type", help="Quantization type (int8/float16/float32)")
    stt_parser.add_argument(
        "--data-dir",
        dest="data_dir",
        help="Directory for Whisper models (default: ~/.local/share/faster-whisper-models)",
    )
    stt_parser.add_argument(
        "--json", dest="json_output", action="store_true", help="Output result as JSON (includes metadata)"
    )
    stt_parser.add_argument(
        "--no-vad", dest="no_vad", action="store_true", help="Disable Silero VAD pre-filtering (VAD is on by default)"
    )
    stt_parser.add_argument("--verbose", "-v", action="store_true", help="Verbose logging (DEBUG level)")

    # ── call subcommand ─────────────────────────────────────────────
    call_parser = sub.add_parser("call", help="Place a SIP call")
    _add_sip_connection_args(call_parser)
    _add_nat_args(call_parser)
    _add_logging_args(call_parser)

    # Audio source (WAV or TTS — optional, omit for listen-only call)
    # --interactive and --wav are mutually exclusive (interactive replaces file-based playback)
    audio_group = call_parser.add_mutually_exclusive_group(required=False)
    audio_group.add_argument("--wav", "-w", help="Path to WAV file to play")
    audio_group.add_argument(
        "--interactive",
        action="store_true",
        default=False,
        help="Interactive mode: type text in the console that gets spoken via live Piper TTS during the call",
    )
    call_parser.add_argument(
        "--text", help="Text to synthesize via piper TTS (or initial greeting in interactive mode)"
    )

    # TTS options
    call_parser.add_argument("--tts-model", dest="tts_model", help="Piper voice model (default: de_DE-thorsten-high)")
    call_parser.add_argument(
        "--piper-model",
        dest="piper_model",
        default=None,
        help="Path to Piper .onnx model for live TTS in interactive mode (default: ./de_DE-thorsten-high.onnx)",
    )
    call_parser.add_argument(
        "--tts-sample-rate", dest="tts_sample_rate", type=int, help="Resample TTS output to this rate (default: native)"
    )
    call_parser.add_argument(
        "--tts-data-dir",
        dest="tts_data_dir",
        help="Directory for piper voice models (default: ~/.local/share/piper-voices)",
    )

    # Call parameters
    call_parser.add_argument("--dest", "-d", required=True, help="Destination phone number or SIP URI")
    call_parser.add_argument("--timeout", "-t", type=int, help="Call timeout in seconds (default: 60)")
    call_parser.add_argument(
        "--pre-delay", dest="pre_delay", type=float, help="Seconds to wait after answer before playback (default: 0)"
    )
    call_parser.add_argument(
        "--post-delay", dest="post_delay", type=float, help="Seconds to wait after playback before hangup (default: 0)"
    )
    call_parser.add_argument(
        "--inter-delay", dest="inter_delay", type=float, help="Seconds to wait between WAV repeats (default: 0)"
    )
    call_parser.add_argument("--repeat", type=int, help="Number of times to play the WAV (default: 1)")
    call_parser.add_argument(
        "--wait-for-silence",
        dest="wait_for_silence",
        type=float,
        help="Wait for N seconds of remote silence before playback (e.g. 1.0 to let callee finish 'Hello?')",
    )
    call_parser.add_argument(
        "--record", dest="record_path", help="Record remote party (RX) audio to this WAV file path"
    )
    call_parser.add_argument(
        "--record-tx",
        dest="record_tx_path",
        help="Record local (TX) audio to this WAV file path (requires --no-null-audio or --real-capture)",
    )
    call_parser.add_argument(
        "--mix-mode",
        dest="mix_mode",
        choices=["none", "mono", "stereo"],
        default=None,
        help="Post-call mix of RX+TX recordings: none | mono | stereo (requires --record and --record-tx)",
    )
    call_parser.add_argument(
        "--mix-output", dest="mix_output", help="Output path for post-call RX+TX mix (default: auto-generated)"
    )
    call_parser.add_argument(
        "--no-null-audio",
        dest="no_null_audio",
        action="store_true",
        default=False,
        help="Use real audio device instead of null device (required for TX recording and local mic input)",
    )
    call_parser.add_argument(
        "--real-capture",
        dest="real_capture",
        action="store_true",
        default=False,
        help="Use real capture (mic) device even when playback stays null. Enables TX recording without --no-null-audio.",
    )
    call_parser.add_argument(
        "--real-playback",
        dest="real_playback",
        action="store_true",
        default=False,
        help="Use real playback (speaker) device even when capture stays null.",
    )
    call_parser.add_argument(
        "--audio-socket",
        dest="audio_socket",
        help=(
            "Unix domain socket path for live-streaming remote-party audio (PCM 16 kHz, S16_LE, mono). "
            "Start a socat listener BEFORE the call, e.g.: "
            "socat UNIX-LISTEN:/tmp/sip_audio.sock,fork EXEC:'aplay -r 16000 -f S16_LE -c 1 -t raw' — "
            "then pass --audio-socket /tmp/sip_audio.sock. "
            "For a simpler alternative, use --play-audio instead"
        ),
    )
    call_parser.add_argument(
        "--play-audio",
        dest="play_audio",
        action="store_true",
        default=False,
        help=(
            "Play remote-party audio on the local sound device via sounddevice (requires 'pip install sounddevice'). "
            "Simpler alternative to --audio-socket — no socat or external process needed. "
            "Can be combined with --audio-socket for simultaneous socket streaming and local playback"
        ),
    )
    call_parser.add_argument(
        "--play-tx",
        dest="play_tx",
        action="store_true",
        default=False,
        help=(
            "Route local (TX) audio to output sinks. When combined with --play-audio, "
            "creates a stereo stream with RX on left channel and TX on right channel. "
            "Requires --real-capture or --no-null-audio"
        ),
    )
    call_parser.add_argument(
        "--audio-device",
        dest="audio_device",
        default=None,
        help="Sounddevice output device index (int) or name substring for --play-audio (default: system default)",
    )
    call_parser.add_argument(
        "--no-play-rx",
        dest="play_rx",
        action="store_false",
        default=True,
        help="Disable routing of remote-party (RX) audio to output sinks",
    )
    call_parser.add_argument(
        "--stt-data-dir",
        dest="stt_data_dir",
        help="Directory for Whisper STT models (default: ~/.local/share/faster-whisper-models)",
    )
    call_parser.add_argument(
        "--stt-backend",
        dest="stt_backend",
        choices=["faster-whisper", "openvino"],
        default="faster-whisper",
        help="STT backend engine (default: faster-whisper)",
    )
    call_parser.add_argument(
        "--stt-model",
        dest="stt_model",
        help="Whisper model size (tiny, base, small, medium, large-v3) or HuggingFace model ID for transcription (default: medium)",
        # one of: tiny.en, tiny, base.en, base, small.en, small, medium.en, medium, large-v1, large-v2, large-v3, large, distil-large-v2, distil-medium.en, distil-small.en, distil-large-v3, distil-large-v3.5, large-v3-turbo, turbo
    )
    call_parser.add_argument(
        "--stt-language",
        dest="stt_language",
        default=None,
        help="Language code for STT transcription (default: de)",
    )
    call_parser.add_argument(
        "--transcribe",
        action="store_true",
        default=False,
        help="Transcribe recorded audio via STT and write a JSON call report (requires --record)",
    )
    call_parser.add_argument(
        "--live-transcribe",
        dest="live_transcribe",
        action="store_true",
        default=False,
        help="Enable live speech-to-text transcription of remote-party audio during the call",
    )
    call_parser.add_argument(
        "--vad-silence-threshold",
        dest="vad_silence_threshold",
        type=float,
        default=None,
        help="RMS silence threshold for live transcription VAD (default: 0.01)",
    )
    call_parser.add_argument(
        "--vad-silence-trigger",
        dest="vad_silence_trigger",
        type=float,
        default=None,
        help="Seconds of silence to trigger transcription chunk (default: 0.3)",
    )
    call_parser.add_argument(
        "--vad-max-chunk",
        dest="vad_max_chunk",
        type=float,
        default=None,
        help="Max seconds per transcription chunk (default: 5.0)",
    )
    call_parser.add_argument(
        "--vad-min-chunk",
        dest="vad_min_chunk",
        type=float,
        default=None,
        help="Min seconds per transcription chunk (default: 0.5)",
    )

    # ── callee_autoanswer subcommand ────────────────────────────────
    aa_parser = sub.add_parser("callee_autoanswer", help="Auto-answer incoming calls and play WAV/TTS audio")
    _add_sip_connection_args(aa_parser)
    _add_nat_args(aa_parser)
    _add_logging_args(aa_parser)
    aa_parser.add_argument("--local-port", dest="local_port", type=int, help="Local SIP bind port")
    aa_parser.add_argument(
        "--mode",
        choices=["none", "wav", "tts"],
        default="none",
        help="Audio mode: none=no audio, wav=play WAV, tts=Piper TTS (default: none)",
    )
    aa_parser.add_argument("--wav-file", dest="wav_file", help="Path to WAV file (requires --mode wav)")
    aa_parser.add_argument("--tts-text", dest="tts_text", help="Text for TTS announcement (requires --mode tts)")
    aa_parser.add_argument(
        "--piper-model",
        dest="piper_model",
        default="de_DE-thorsten-high",
        help="Piper TTS model name (default: de_DE-thorsten-high)",
    )
    aa_parser.add_argument("--tts-data-dir", dest="tts_data_dir", default=None, help="Piper data directory")
    aa_parser.add_argument("--start-wav", dest="start_wav", help="WAV file to play at the start of the call")
    aa_parser.add_argument("--end-wav", dest="end_wav", help="WAV file to play at the end of the call (before hangup)")
    aa_parser.add_argument(
        "--pause-before-start",
        dest="pause_before_start",
        type=float,
        default=0.0,
        help="Pause before start WAV (default: 0.0)",
    )
    aa_parser.add_argument(
        "--pause-before-content",
        dest="pause_before_content",
        type=float,
        default=0.0,
        help="Pause before content WAV/TTS (default: 0.0)",
    )
    aa_parser.add_argument(
        "--pause-before-end",
        dest="pause_before_end",
        type=float,
        default=0.0,
        help="Pause before end WAV (default: 0.0)",
    )
    aa_parser.add_argument(
        "--answer-delay",
        dest="answer_delay",
        type=float,
        default=1.0,
        help="Seconds to wait before answering (default: 1.0)",
    )
    aa_parser.add_argument(
        "--no-auto-answer", dest="no_auto_answer", action="store_true", help="Do NOT auto-answer calls"
    )

    # ── callee_realtime-tts subcommand ──────────────────────────────
    rt_parser = sub.add_parser("callee_realtime-tts", help="Auto-answer incoming calls with real-time Piper TTS")
    _add_sip_connection_args(rt_parser)
    _add_nat_args(rt_parser)
    _add_logging_args(rt_parser)
    rt_parser.add_argument("--local-port", dest="local_port", type=int, help="Local SIP bind port")
    rt_parser.add_argument("--tts-text", dest="tts_text", help="Initial TTS text spoken on call answer")
    rt_parser.add_argument("--interactive", action="store_true", help="Interactive mode: type text in the console")
    rt_parser.add_argument(
        "--piper-model",
        dest="piper_model",
        default="./de_DE-thorsten-high.onnx",
        help="Path to Piper .onnx model (default: ./de_DE-thorsten-high.onnx)",
    )
    rt_parser.add_argument(
        "--answer-delay",
        dest="answer_delay",
        type=float,
        default=1.0,
        help="Seconds to wait before answering (default: 1.0)",
    )
    rt_parser.add_argument(
        "--no-auto-answer", dest="no_auto_answer", action="store_true", help="Do NOT auto-answer calls"
    )

    # Playback at call start
    rt_playback = rt_parser.add_argument_group("Playback at call start")
    rt_playback.add_argument("--wav-file", dest="wav_file", default=None, help="WAV file to play at call start")
    rt_playback.add_argument(
        "--play-delay", dest="play_delay", type=float, default=0.0, help="Seconds to wait before playback (default: 0)"
    )

    # ── callee_live-transcribe subcommand ─────────────────────────
    lt_parser = sub.add_parser("callee_live-transcribe", help="Auto-answer incoming calls with live transcription")
    _add_sip_connection_args(lt_parser)
    _add_nat_args(lt_parser)
    _add_logging_args(lt_parser)
    lt_parser.add_argument("--local-port", dest="local_port", type=int, help="Local SIP bind port")
    lt_parser.add_argument(
        "--answer-delay",
        dest="answer_delay",
        type=float,
        default=1.0,
        help="Seconds to wait before answering (default: 1.0)",
    )
    lt_parser.add_argument(
        "--no-auto-answer", dest="no_auto_answer", action="store_true", help="Do NOT auto-answer calls"
    )

    # STT (unified naming with call subcommand)
    lt_parser.add_argument(
        "--stt-backend",
        "--backend",
        dest="stt_backend",
        choices=["faster-whisper", "openvino"],
        default="faster-whisper",
        help="STT backend engine (default: faster-whisper)",
    )
    lt_parser.add_argument(
        "--stt-model",
        "--whisper-model",
        dest="whisper_model",
        default="base",
        help="Whisper model size or HuggingFace model ID (default: base)",
    )
    lt_parser.add_argument(
        "--stt-live-model",
        dest="stt_live_model",
        default=None,
        help="Whisper model for live transcription (default: same as --stt-model). "
        "Use a smaller model (e.g. 'base') for faster startup.",
    )
    lt_parser.add_argument(
        "--stt-device",
        "--device",
        dest="device",
        default="cpu",
        choices=["cpu", "cuda"],
        help="Compute device (default: cpu)",
    )
    lt_parser.add_argument(
        "--stt-language", "--language", dest="language", default=None, help="Force language, e.g. 'de', 'en'"
    )
    lt_parser.add_argument(
        "--stt-data-dir",
        dest="stt_data_dir",
        help="Model cache directory for Whisper STT models",
    )

    # VAD / Chunking (unified naming with call subcommand)
    lt_parser.add_argument(
        "--vad-silence-threshold",
        "--silence-threshold",
        dest="vad_silence_threshold",
        type=float,
        default=0.01,
        help="RMS silence threshold (default: 0.01)",
    )
    lt_parser.add_argument(
        "--vad-silence-trigger",
        "--silence-trigger",
        dest="vad_silence_trigger",
        type=float,
        default=0.3,
        help="Seconds of silence to trigger transcription (default: 0.3)",
    )
    lt_parser.add_argument(
        "--vad-max-chunk",
        "--max-chunk",
        dest="vad_max_chunk",
        type=float,
        default=5.0,
        help="Max seconds per chunk (default: 5.0)",
    )
    lt_parser.add_argument(
        "--vad-min-chunk",
        "--min-chunk",
        dest="vad_min_chunk",
        type=float,
        default=0.5,
        help="Min seconds per chunk (default: 0.5)",
    )

    # Recording
    lt_parser.add_argument("--wav-output", dest="wav_output", default=None, help="Path for RX WAV recording")
    lt_parser.add_argument(
        "--wav-dir", dest="wav_dir", default="..", help="Directory for WAV files (default: parent directory)"
    )
    lt_parser.add_argument("--wav-output-tx", dest="wav_output_tx", default=None, help="Path for TX WAV recording")
    lt_parser.add_argument("--no-wav", dest="no_wav", action="store_true", help="Do not save WAV recording")
    lt_parser.add_argument(
        "--transcribe",
        action="store_true",
        default=False,
        help="Transcribe full RX recording after call and write JSON report",
    )

    # Playback
    lt_playback = lt_parser.add_argument_group("Playback at call start")
    lt_playback.add_argument("--wav-file", dest="wav_file", default=None, help="WAV file to play at call start")
    lt_playback.add_argument("--tts-text", dest="tts_text", default=None, help="Text for Piper TTS playback")
    lt_playback.add_argument(
        "--piper-model",
        dest="piper_model",
        default="de_DE-thorsten-high",
        help="Piper TTS model (default: de_DE-thorsten-high)",
    )
    lt_playback.add_argument("--tts-data-dir", dest="tts_data_dir", default=None, help="Piper data directory")
    lt_playback.add_argument(
        "--play-delay", dest="play_delay", type=float, default=0.0, help="Seconds to wait before playback (default: 0)"
    )

    # Audio output sinks
    lt_parser.add_argument(
        "--audio-socket",
        dest="audio_socket",
        default=None,
        help="Unix socket path for live audio streaming (PCM 16kHz)",
    )
    lt_parser.add_argument(
        "--play-audio",
        dest="play_audio",
        action="store_true",
        default=False,
        help="Play remote-party audio on local sound device via sounddevice",
    )
    lt_parser.add_argument(
        "--audio-device",
        dest="audio_device",
        default=None,
        help="Sounddevice output device index or name substring for --play-audio",
    )

    return parser.parse_args()


# ── Subcommand handlers ───────────────────────────────────────────────


def cmd_tts(args: argparse.Namespace) -> int:
    """Execute the ``tts`` subcommand.

    Generates a WAV file from text using piper TTS, optionally plays it.

    Args:
        args: Parsed CLI arguments.

    Returns:
        Exit code: 0 on success, 1 on failure.
    """
    import tempfile

    from sipstuff.tts import TtsError, generate_wav

    if not args.output and not args.play_audio:
        logger.error("Provide -o/--output and/or --play-audio")
        return 1

    use_temp = args.output is None
    if use_temp:
        tmp_fd, tmp_path = tempfile.mkstemp(suffix=".wav")
        os.close(tmp_fd)
        output_path = tmp_path
    else:
        output_path = args.output

    try:
        result_path = generate_wav(
            text=args.text,
            model=args.model,
            output_path=output_path,
            sample_rate=args.sample_rate,
            data_dir=args.tts_data_dir,
        )
        logger.info(f"WAV written to {result_path}")

        if args.play_audio:
            from sipstuff.audio import play_wav

            _audio_device: int | str | None = None
            if args.audio_device is not None:
                try:
                    _audio_device = int(args.audio_device)
                except ValueError:
                    _audio_device = args.audio_device
            play_wav(result_path, audio_device=_audio_device)

        return 0
    except TtsError as exc:
        logger.error(f"TTS failed: {exc}")
        return 1
    finally:
        if use_temp and os.path.isfile(output_path):
            os.unlink(output_path)
            logger.debug(f"Cleaned up temp file {output_path}")


def cmd_stt(args: argparse.Namespace) -> int:
    """Execute the ``stt`` subcommand.

    Transcribes a WAV file to text using faster-whisper.

    Args:
        args: Parsed CLI arguments.

    Returns:
        Exit code: 0 on success, 1 on failure.
    """
    from sipstuff.stt import SttError, transcribe_wav

    try:
        text, meta = transcribe_wav(
            wav_path=args.wav,
            model=args.model,
            language=args.language,
            device=args.device,
            compute_type=args.compute_type,
            data_dir=args.data_dir,
            vad_filter=not args.no_vad,
            backend=args.backend,
        )
    except SttError as exc:
        logger.error(f"STT failed: {exc}")
        return 1

    if args.json_output:
        output = {"text": text, **meta}
        print(json.dumps(output, indent=2, ensure_ascii=False))
    else:
        print(text)

    return 0


def cmd_call(args: argparse.Namespace) -> int:
    """Execute the ``call`` subcommand.

    Loads configuration (YAML / env / CLI overrides), optionally generates
    a TTS WAV file, places the SIP call, and cleans up temporary files.

    Args:
        args: Parsed CLI arguments from ``parse_args``.

    Returns:
        Exit code: 0 on success, 1 on failure (config error, TTS error,
        SIP error, or unanswered call).
    """

    overrides = _build_sip_overrides(args)

    # Call-specific overrides
    for key in (
        "timeout",
        "pre_delay",
        "post_delay",
        "inter_delay",
        "repeat",
        "wait_for_silence",
        "tts_model",
        "tts_sample_rate",
    ):
        val = getattr(args, key, None)
        if val is not None:
            overrides[key] = val

    tts_data_dir = getattr(args, "tts_data_dir", None)
    if tts_data_dir is not None:
        overrides["tts_data_dir"] = tts_data_dir

    try:
        config = SipCallerConfig.from_config(config_path=args.config, overrides=overrides)
    except Exception as exc:
        logger.error(f"Configuration error: {exc}")
        return 1

    # Resolve audio source into WavPlayConfig or TtsPlayConfig
    wav_path = args.wav if hasattr(args, "wav") else None
    wav_play_override: WavPlayConfig | TtsPlayConfig | None = None
    _interactive = getattr(args, "interactive", False)

    if _interactive:
        # In interactive mode, --text is the initial greeting (not pre-generated WAV)
        pass
    elif getattr(args, "text", None):
        wav_play_override = TtsPlayConfig(
            tts_text=args.text,
            tts_config=config.tts,
        )
    elif wav_path:
        wav_play_override = WavPlayConfig(wav_path=wav_path)

    if _interactive and not getattr(args, "piper_model", None):
        logger.error("--interactive requires --piper-model (path to Piper .onnx model)")
        return 1

    if args.transcribe and not args.record_path:
        logger.error("--transcribe requires --record")
        return 1

    logger.info(f"Calling {args.dest} via {config.sip.server}:{config.sip.port}")

    # Build per-call config objects from CLI args (override config defaults)
    _no_null = getattr(args, "no_null_audio", False)
    _null_capture = not _no_null and not getattr(args, "real_capture", False)
    _null_playback = not _no_null and not getattr(args, "real_playback", False)
    recording = RecordingConfig(
        rx_path=args.record_path,
        tx_path=getattr(args, "record_tx_path", None),
        mix_mode=getattr(args, "mix_mode", None) or "none",
        mix_output_path=getattr(args, "mix_output", None),
    )
    _audio_device: int | str | None = None
    if args.audio_device is not None:
        try:
            _audio_device = int(args.audio_device)
        except ValueError:
            _audio_device = args.audio_device
    audio = AudioDeviceConfig(
        use_null_audio=not _no_null,
        null_capture=_null_capture,
        null_playback=_null_playback,
        socket_path=args.audio_socket,
        play_audio=args.play_audio,
        play_rx=args.play_rx,
        play_tx=args.play_tx,
        audio_device=_audio_device,
    )
    stt_cfg = SttConfig(
        backend=args.stt_backend,
        model=args.stt_model or config.stt.model,
        language=args.stt_language or config.stt.language,
        device=config.stt.device,
        data_dir=args.stt_data_dir or config.stt.data_dir,
        live_transcribe=args.live_transcribe,
    )
    vad_cfg = VadConfig(
        silence_threshold=(
            args.vad_silence_threshold if args.vad_silence_threshold is not None else config.vad.silence_threshold
        ),
        silence_trigger=(
            args.vad_silence_trigger if args.vad_silence_trigger is not None else config.vad.silence_trigger
        ),
        max_chunk=args.vad_max_chunk if args.vad_max_chunk is not None else config.vad.max_chunk,
        min_chunk=args.vad_min_chunk if args.vad_min_chunk is not None else config.vad.min_chunk,
    )

    config = config.model_copy(update={"audio": audio})
    pjsip_logs: list[str] = []

    # Interactive TTS setup
    _tts_producer = None
    if _interactive:
        from sipstuff.tts.live import CLOCK_RATE, PiperTTSProducer, interactive_console

        _audio_queue: Queue[bytes] = Queue(maxsize=500)
        _tts_producer = PiperTTSProducer(
            model_path=args.piper_model,
            audio_queue=_audio_queue,
            target_rate=CLOCK_RATE,
        )
        _tts_producer.start()

    try:
        with SipCaller(config) as caller:
            if _interactive and _tts_producer is not None:
                threading.Thread(
                    target=interactive_console,
                    args=(_tts_producer,),
                    daemon=True,
                    name="Console",
                ).start()
            success = caller.make_call(
                args.dest,
                wav_play=wav_play_override,
                recording=recording,
                audio=audio,
                stt=stt_cfg,
                vad=vad_cfg,
                tts_producer=_tts_producer,
                initial_tts_text=getattr(args, "text", None) if _interactive else None,
            )
            call_result = caller.last_call_result
            pjsip_logs = caller.get_pjsip_logs()
    except SipCallError as exc:
        logger.error(f"SIP call failed: {exc}")
        return 1
    finally:
        if _tts_producer is not None:
            _tts_producer.stop()

    if success:
        logger.info("Call completed successfully")

        # Transcribe recording and write JSON report if --transcribe was given
        if args.transcribe and args.record_path and os.path.isfile(args.record_path):
            from sipstuff.stt import SttError, transcribe_wav

            transcript_text: str | None = None
            stt_meta: dict[str, object] = {}
            try:
                transcript_text, stt_meta = transcribe_wav(
                    args.record_path,
                    model=stt_cfg.model,
                    language=stt_cfg.language or "de",
                    device=stt_cfg.device,
                    compute_type=stt_cfg.compute_type,
                    data_dir=stt_cfg.data_dir,
                    backend=stt_cfg.backend,
                )
                logger.info(f"Transcript: {transcript_text}")
            except SttError as exc:
                logger.error(f"STT transcription failed: {exc}")

            # Build and write JSON call report
            report: dict[str, object] = {
                "timestamp": datetime.now(timezone.utc).astimezone().isoformat(),
                "destination": args.dest,
                "wav_file": args.wav or "(tts)",
                "tts_text": args.text,
                "tts_model": config.tts.model,
                "record_path": args.record_path,
                "call_duration": call_result.call_duration if call_result else None,
                "answered": call_result.answered if call_result else None,
                "disconnect_reason": call_result.disconnect_reason if call_result else None,
                "playback": {
                    "repeat": config.call.repeat,
                    "pre_delay": config.call.pre_delay,
                    "post_delay": config.call.post_delay,
                    "inter_delay": config.call.inter_delay,
                    "timeout": config.call.timeout,
                },
                "recording_duration": stt_meta.get("audio_duration"),
                "transcript": transcript_text,
                "stt": {"model": stt_cfg.model, **stt_meta},
                "live_transcript": call_result.live_transcript if call_result else [],
                "pjsip_log": pjsip_logs,
            }

            report_json = json.dumps(report, indent=2, ensure_ascii=False)
            report_path = Path(args.record_path).with_suffix(".json")
            report_path.write_text(report_json)
            logger.info(f"Call report written to {report_path}")
            logger.opt(raw=True).info(
                "\n{border}\n***** CALL REPORT *****\n{border}\n{report}\n{border}\n",
                border="*" * 60,
                report=report_json,
            )

        return 0
    else:
        logger.warning("Call was not answered or failed")
        return 1


def cmd_callee_autoanswer(args: argparse.Namespace) -> int:
    """Execute the ``callee_autoanswer`` subcommand.

    Auto-answers incoming SIP calls and plays back WAV files or TTS audio.

    Args:
        args: Parsed CLI arguments.

    Returns:
        Exit code: 0 on success, 1 on failure.
    """
    from sipstuff.audio import ensure_wav_16k_mono
    from sipstuff.autoanswer.pjsip_autoanswer_tts_n_wav import SipCalleeAutoAnswerCall
    from sipstuff.sip_callee import SipCallee

    overrides = _build_sip_overrides(args)
    if args.local_port is not None:
        overrides["local_port"] = args.local_port

    try:
        config = SipEndpointConfig.from_config(config_path=args.config, overrides=overrides)
    except Exception as exc:
        logger.error(f"Configuration error: {exc}")
        return 1

    # Assemble playback sequence
    segments: list[WavPlayConfig | TtsPlayConfig] = []

    if args.start_wav:
        if not os.path.isfile(args.start_wav):
            logger.error(f"Start WAV not found: {args.start_wav}")
            return 1
        segments.append(
            WavPlayConfig(wav_path=ensure_wav_16k_mono(args.start_wav), pause_before=args.pause_before_start)
        )

    if args.mode == "wav":
        if not args.wav_file:
            logger.error("--wav-file is required with --mode wav")
            return 1
        if not os.path.isfile(args.wav_file):
            logger.error(f"WAV file not found: {args.wav_file}")
            return 1
        segments.append(
            WavPlayConfig(wav_path=ensure_wav_16k_mono(args.wav_file), pause_before=args.pause_before_content)
        )
    elif args.mode == "tts":
        if not args.tts_text:
            logger.error("--tts-text is required with --mode tts")
            return 1
        tts_cfg = TtsConfig(model=args.piper_model, sample_rate=16000, data_dir=getattr(args, "tts_data_dir", None))
        segments.append(
            TtsPlayConfig(tts_text=args.tts_text, tts_config=tts_cfg, pause_before=args.pause_before_content)
        )

    if args.end_wav:
        if not os.path.isfile(args.end_wav):
            logger.error(f"End WAV not found: {args.end_wav}")
            return 1
        segments.append(WavPlayConfig(wav_path=ensure_wav_16k_mono(args.end_wav), pause_before=args.pause_before_end))

    sequence = PlaybackSequence(segments=segments)

    callee_config = SipCalleeConfig(
        **config.model_dump(),
        auto_answer=not args.no_auto_answer,
        answer_delay=args.answer_delay,
    )

    def call_factory(acc: SipCalleeAccount, call_id: int) -> SipCalleeAutoAnswerCall:
        """Create a new auto-answer call instance for an incoming call.

        Args:
            acc: The SIP account that received the call.
            call_id: PJSUA2 call identifier.

        Returns:
            Configured auto-answer call with the assembled playback sequence.
        """
        return SipCalleeAutoAnswerCall(acc, call_id, sequence=sequence)

    with SipCallee(
        callee_config,
        call_factory=call_factory,
    ) as callee:
        callee.run()

    return 0


def cmd_callee_realtime_tts(args: argparse.Namespace) -> int:
    """Execute the ``callee_realtime-tts`` subcommand.

    Auto-answers incoming SIP calls with real-time Piper TTS audio.

    Args:
        args: Parsed CLI arguments.

    Returns:
        Exit code: 0 on success, 1 on failure.
    """
    from sipstuff.realtime.pjsip_realtime_tts import SipCalleeRealtimeTtsCall
    from sipstuff.sip_account import SipCalleeAccount
    from sipstuff.sip_callee import SipCallee
    from sipstuff.tts.live import CLOCK_RATE, PiperTTSProducer, interactive_console

    overrides = _build_sip_overrides(args)
    if args.local_port is not None:
        overrides["local_port"] = args.local_port

    try:
        config = SipEndpointConfig.from_config(config_path=args.config, overrides=overrides)
    except Exception as exc:
        logger.error(f"Configuration error: {exc}")
        return 1

    # --- Playback sequence ---
    segments: list[WavPlayConfig] = []
    if getattr(args, "wav_file", None):
        from sipstuff.audio import ensure_wav_16k_mono

        if not os.path.isfile(args.wav_file):
            logger.error(f"WAV file not found: {args.wav_file}")
            return 1
        wav_path = ensure_wav_16k_mono(args.wav_file)
        segments.append(WavPlayConfig(wav_path=wav_path, pause_before=args.play_delay))
    sequence = PlaybackSequence(segments=segments)

    audio_queue: Queue[bytes] = Queue(maxsize=500)
    tts_producer = PiperTTSProducer(
        model_path=args.piper_model,
        audio_queue=audio_queue,
        target_rate=CLOCK_RATE,
    )
    tts_producer.start()

    callee_config = SipCalleeConfig(
        **config.model_dump(),
        auto_answer=not args.no_auto_answer,
        answer_delay=args.answer_delay,
    )

    def call_factory(acc: SipCalleeAccount, call_id: int) -> SipCalleeRealtimeTtsCall:
        """Create a new real-time TTS call instance for an incoming call.

        Args:
            acc: The SIP account that received the call.
            call_id: PJSUA2 call identifier.

        Returns:
            Configured real-time TTS call wired to the shared audio queue
            and TTS producer.
        """
        return SipCalleeRealtimeTtsCall(
            acc,
            call_id,
            audio_queue=audio_queue,
            tts_producer=tts_producer,
            initial_text=args.tts_text,
            sequence=sequence,
        )

    try:
        with SipCallee(
            callee_config,
            call_factory=call_factory,
        ) as callee:
            if args.interactive:
                threading.Thread(
                    target=interactive_console,
                    args=(tts_producer,),
                    daemon=True,
                    name="Console",
                ).start()
            callee.run()
    finally:
        tts_producer.stop()

    return 0


def cmd_callee_live_transcribe(args: argparse.Namespace) -> int:
    """Execute the ``callee_live-transcribe`` subcommand.

    Auto-answers incoming SIP calls and live-transcribes the remote party's audio.

    Args:
        args: Parsed CLI arguments.

    Returns:
        Exit code: 0 on success, 1 on failure.
    """
    from sipstuff.audio import VADAudioBuffer, WavRecorder
    from sipstuff.sip_account import SipCalleeAccount
    from sipstuff.sip_callee import SipCallee
    from sipstuff.stt import load_stt_model
    from sipstuff.stt.live import LiveTranscriptionThread
    from sipstuff.transcribe.pjsip_live_transcribe import SipCalleeLiveTranscribeCall

    overrides = _build_sip_overrides(args)
    if args.local_port is not None:
        overrides["local_port"] = args.local_port

    try:
        config = SipEndpointConfig.from_config(config_path=args.config, overrides=overrides)
    except Exception as exc:
        logger.error(f"Configuration error: {exc}")
        return 1

    # --- Playback sequence ---
    if args.wav_file and args.tts_text:
        logger.error("--wav-file and --tts-text cannot be used together")
        return 1

    segments: list[WavPlayConfig | TtsPlayConfig] = []
    if args.wav_file:
        from sipstuff.audio import ensure_wav_16k_mono

        if not os.path.isfile(args.wav_file):
            logger.error(f"WAV file not found: {args.wav_file}")
            return 1
        segments.append(WavPlayConfig(wav_path=ensure_wav_16k_mono(args.wav_file), pause_before=args.play_delay))
    elif args.tts_text:
        tts_cfg = TtsConfig(model=args.piper_model, sample_rate=16000, data_dir=args.tts_data_dir)
        segments.append(TtsPlayConfig(tts_text=args.tts_text, tts_config=tts_cfg, pause_before=args.play_delay))

    sequence = PlaybackSequence(segments=segments)

    # --- Build config objects from CLI args ---
    vad_cfg = VadConfig(
        silence_threshold=args.vad_silence_threshold,
        silence_trigger=args.vad_silence_trigger,
        max_chunk=args.vad_max_chunk,
        min_chunk=args.vad_min_chunk,
    )
    # Post-call transcription config (used by --transcribe, can be large model)
    stt_cfg = SttConfig(
        backend=args.stt_backend,
        model=args.whisper_model,
        device=args.device,
        language=args.language,
        data_dir=getattr(args, "stt_data_dir", None),
    )
    # Live transcription config (pre-loaded at startup, can be a different/smaller model)
    live_model_name = args.stt_live_model or args.whisper_model
    stt_live_cfg = (
        stt_cfg.model_copy(update={"model": live_model_name}) if live_model_name != args.whisper_model else stt_cfg
    )

    # Pre-load live STT model at startup (avoids per-call load delay)
    stt_model = load_stt_model(stt_live_cfg)

    # --- Per-call transcription callback ---
    def _on_call_ended(call: SipCalleeLiveTranscribeCall) -> None:
        """Handle post-call cleanup: stop live STT, transcribe recording, and write a JSON report.

        Called when an incoming call disconnects.  Stops the per-call
        ``LiveTranscriptionThread``, collects its segments, and — when
        ``--transcribe`` is active — runs full-file STT on the RX recording
        and writes a JSON call report next to the WAV file.

        Args:
            call: The call instance that just ended.
        """
        # Stop per-call STT thread and collect its segments
        call_stt = call.call_stt
        if call_stt is not None:
            call_stt.stop()
            call_stt.join(timeout=10)
            live_segments = list(call_stt.segments)
        else:
            live_segments = []

        if not args.transcribe:
            return
        wav_path_rx = call._wav_recorder_rx.filepath if call._wav_recorder_rx else None
        tx_path = call._wav_recorder_tx.filepath if call._wav_recorder_tx else None
        if not wav_path_rx or not os.path.isfile(wav_path_rx):
            return

        from sipstuff.stt import SttError, transcribe_wav

        transcript_text: str | None = None
        stt_meta: dict[str, object] = {}
        try:
            transcript_text, stt_meta = transcribe_wav(
                wav_path_rx,
                model=stt_cfg.model,
                language=stt_cfg.language or "de",
                device=stt_cfg.device,
                compute_type=stt_cfg.compute_type,
                data_dir=stt_cfg.data_dir,
                backend=stt_cfg.backend,
            )
            logger.info(f"Transcript: {transcript_text}")
        except SttError as exc:
            logger.error(f"STT transcription failed: {exc}")

        report: dict[str, object] = {
            "timestamp": datetime.now(timezone.utc).astimezone().isoformat(),
            "direction": "incoming",
            "call_tracking_id": call.call_tracking_id,
            "record_path_rx": wav_path_rx,
            "record_path_tx": tx_path,
            "call_duration": (
                (call.call_end_time - call.call_start_time) if call.call_start_time and call.call_end_time else None
            ),
            "playback_wav": args.wav_file,
            "tts_text": args.tts_text,
            "recording_duration": stt_meta.get("audio_duration"),
            "transcript": transcript_text,
            "stt": {"model": stt_cfg.model, "live_model": stt_live_cfg.model, **stt_meta},
            "live_transcript": live_segments,
        }

        report_json = json.dumps(report, indent=2, ensure_ascii=False)
        report_path = Path(wav_path_rx).with_suffix(".json")
        report_path.write_text(report_json)
        logger.info(f"Call report written to {report_path}")
        logger.opt(raw=True).info(
            "\n{border}\n***** CALL REPORT *****\n{border}\n{report}\n{border}\n",
            border="*" * 60,
            report=report_json,
        )

    def call_factory(acc: SipCalleeAccount, call_id: int) -> SipCalleeLiveTranscribeCall:
        """Create a new live-transcription call instance for an incoming call.

        Each call gets its own ``VADAudioBuffer``, ``LiveTranscriptionThread``,
        and optional RX/TX ``WavRecorder`` instances.  The pre-loaded STT model
        is shared across calls.

        Args:
            acc: The SIP account that received the call.
            call_id: PJSUA2 call identifier.

        Returns:
            Configured live-transcription call with per-call recording and STT.
        """
        # Per-call: each incoming call gets its own VADAudioBuffer, STT thread, and recording files
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        call_tracking_id = f"{call_id}_{timestamp}"

        audio_buf = VADAudioBuffer(
            sample_rate=16000,
            **vad_cfg.to_vad_buffer_kwargs(),
        )
        call_stt = LiveTranscriptionThread(
            audio_buf,
            stt_config=stt_live_cfg,
            call_start_time=datetime.now(),
            model=stt_model,
        )
        call_stt.start()

        wav_recorder_rx: WavRecorder | None = None
        wav_recorder_tx: WavRecorder | None = None

        if not args.no_wav:
            if args.wav_output:
                wav_path_rx = args.wav_output
                logger.warning("--wav-output set: subsequent calls will overwrite the same RX file")
            else:
                wav_path_rx = os.path.join(args.wav_dir, f"call_{timestamp}_rx.wav")
            wav_recorder_rx = WavRecorder(wav_path_rx, sample_rate=16000)
            logger.info(f"RX recording → {wav_path_rx}")

            if args.wav_output_tx:
                tx_path = args.wav_output_tx
                logger.warning("--wav-output-tx set: subsequent calls will overwrite the same TX file")
            else:
                tx_path = os.path.join(args.wav_dir, f"call_{timestamp}_tx.wav")
            wav_recorder_tx = WavRecorder(tx_path, sample_rate=16000)
            logger.info(f"TX recording → {tx_path}")

        _audio_device: int | str | None = None
        if args.audio_device is not None:
            try:
                _audio_device = int(args.audio_device)
            except ValueError:
                _audio_device = args.audio_device
        audio_cfg = AudioDeviceConfig(
            socket_path=args.audio_socket,
            play_audio=args.play_audio,
            audio_device=_audio_device,
        )
        call = SipCalleeLiveTranscribeCall(
            acc,
            call_id,
            audio_buffer=audio_buf,
            wav_recorder_rx=wav_recorder_rx,
            wav_recorder_tx=wav_recorder_tx,
            sequence=sequence,
            audio=audio_cfg,
            on_call_ended=_on_call_ended,
        )
        call.call_tracking_id = call_tracking_id
        call.call_stt = call_stt
        logger.info(f"Per-call STT started for call {call_tracking_id}")
        return call

    callee_config = SipCalleeConfig(
        **config.model_dump(),
        auto_answer=not args.no_auto_answer,
        answer_delay=args.answer_delay,
    )

    with SipCallee(
        callee_config,
        call_factory=call_factory,
    ) as callee:
        callee.run()

    return 0


# ── Main entry point ──────────────────────────────────────────────────


def main() -> int:
    """CLI entry point: print the startup banner, parse args, and dispatch.

    Returns:
        Exit code: 0 on success, 1 on failure.
    """

    # Initial logging + banner before arg parsing so version is always visible
    os.environ.setdefault("LOGURU_LEVEL", "INFO")
    configure_logging()
    print_banner()

    args = parse_args()

    # Reconfigure logging if --verbose was given
    if args.verbose:
        os.environ["LOGURU_LEVEL"] = "DEBUG"
        configure_logging()

    logger.info(f"LOGURU_LEVEL={os.getenv('LOGURU_LEVEL')}")
    logger.info("INFO LOG TEST")
    logger.debug("DEBUG LOG TEST")

    if args.command == "call":
        return cmd_call(args)
    elif args.command == "tts":
        return cmd_tts(args)
    elif args.command == "stt":
        return cmd_stt(args)
    elif args.command == "callee_autoanswer":
        return cmd_callee_autoanswer(args)
    elif args.command == "callee_realtime-tts":
        return cmd_callee_realtime_tts(args)
    elif args.command == "callee_live-transcribe":
        return cmd_callee_live_transcribe(args)
    return 1


if __name__ == "__main__":
    sys.exit(main())
