#!/usr/bin/env python3
"""PJSIP callee call handler for real-time Piper TTS streaming.

Implements a producer-consumer architecture for streaming synthesised speech
into incoming SIP calls:

- ``PiperTTSProducer`` (producer): synthesises text chunk-by-chunk in a
  dedicated thread and writes raw PCM data into a shared ``Queue``.
- ``TTSMediaPort`` (consumer): a custom ``pj.AudioMediaPort`` that drains the
  queue frame-by-frame and feeds the samples into the PJSIP conference bridge.

The call handler ``SipCalleeRealtimeTtsCall`` wires these two components
together when media becomes active and optionally plays a WAV sequence before
speaking the initial TTS text.

Optionally, the call handler can also:
- Live-transcribe the remote party's speech via ``TranscriptionPort`` +
  ``VADAudioBuffer``.
- Record RX and TX audio to WAV files via ``WavRecorder`` / ``RecordingPort``.
- Stream RX audio to a Unix domain socket or local sounddevice output via
  ``AudioStreamPort``.

This module is used by the ``callee_realtime-tts`` CLI subcommand.
"""

import threading
import time
from collections.abc import Callable
from queue import Queue
from typing import Any

import pjsua2 as pj
from loguru import logger

from sipstuff import SipCalleeAccount, SipCalleeCall
from sipstuff.audio import VADAudioBuffer, WavRecorder
from sipstuff.sip_media import AudioPlayer, AudioStreamPort, RecordingPort, TranscriptionPort
from sipstuff.sipconfig import AudioDeviceConfig, PlaybackSequence
from sipstuff.tts.live import (
    BITS_PER_SAMPLE,
    CHANNEL_COUNT,
    CLOCK_RATE,
    PiperTTSProducer,
    TTSMediaPort,
    interactive_console,
)

log = logger.bind(classname="RealtimeTTS")

SAMPLE_RATE = 16000

# ============================================================
# PJSUA2 Call
# ============================================================


class SipCalleeRealtimeTtsCall(SipCalleeCall):
    """Callee-side call handler that streams real-time Piper TTS into the call.

    On media activation this handler:

    1. Creates a ``TTSMediaPort`` backed by the shared ``audio_queue`` and
       connects it to the PJSIP conference bridge so the remote party hears
       synthesised speech.
    2. If a ``PlaybackSequence`` of WAV files was provided, plays that sequence
       first (in a background thread) and speaks ``initial_text`` afterwards.
    3. If no sequence is configured but ``initial_text`` is set, speaks the
       text immediately after the port is connected.

    Optionally (when ``audio_buffer`` is provided):
    4. Creates a ``TranscriptionPort`` for live STT of the remote party's speech.
    5. Records RX/TX audio to WAV files via ``WavRecorder``.
    6. Streams RX audio to a Unix socket or local playback device.
    """

    def __init__(
        self,
        account: SipCalleeAccount,
        call_id: int,
        *,
        audio_queue: "Queue[bytes]",
        tts_producer: PiperTTSProducer,
        initial_text: str | None = None,
        sequence: PlaybackSequence | None = None,
        audio_buffer: VADAudioBuffer | None = None,
        wav_recorder_rx: WavRecorder | None = None,
        wav_recorder_tx: WavRecorder | None = None,
        audio: AudioDeviceConfig | None = None,
        on_call_ended: Callable[["SipCalleeRealtimeTtsCall"], None] | None = None,
    ) -> None:
        """Initialise the call handler.

        Args:
            account: The ``SipCalleeAccount`` that received the incoming call.
            call_id: PJSIP call identifier passed to the ``pj.Call`` base class.
            audio_queue: Shared queue of raw PCM chunks produced by
                ``PiperTTSProducer`` and consumed by ``TTSMediaPort``.
            tts_producer: Producer instance used to enqueue synthesised speech.
            initial_text: Text to speak once the media port is active (and
                after any WAV sequence has finished). ``None`` skips TTS.
            sequence: Optional sequence of WAV segments to play before
                ``initial_text``. Defaults to an empty sequence.
            audio_buffer: Optional VAD audio buffer for live transcription.
                ``None`` disables RX transcription.
            wav_recorder_rx: Optional WAV recorder for RX (remote) audio.
                ``None`` disables RX recording.
            wav_recorder_tx: Optional WAV recorder for TX (local) audio.
                ``None`` disables TX recording.
            audio: Audio device configuration for socket streaming and local
                playback. Defaults to ``AudioDeviceConfig()`` when ``None``.
            on_call_ended: Optional callback invoked in a background thread
                after the call disconnects and all media resources are released.
        """
        super().__init__(account, call_id)
        self._audio_queue = audio_queue
        self._tts_producer = tts_producer
        self._initial_text = initial_text
        self._sequence = sequence or PlaybackSequence(segments=[])
        self._media_port: TTSMediaPort | None = None
        self._player: AudioPlayer | None = None

        # RX media handling (optional — all default to None for backwards compatibility)
        self._audio_buffer = audio_buffer
        self._wav_recorder_rx = wav_recorder_rx
        self._wav_recorder_tx = wav_recorder_tx
        self._audio_cfg = audio or AudioDeviceConfig()
        self._on_call_ended = on_call_ended
        self._transcription_port: TranscriptionPort | None = None
        self._recording_port: RecordingPort | None = None
        self._audio_stream_port: AudioStreamPort | None = None
        self.call_stt: Any = None
        self.call_tracking_id: str | None = None
        self.call_start_time: float | None = None
        self.call_end_time: float | None = None

    def on_media_active(self, audio_media: Any, media_idx: int) -> None:
        """Set up TTS and optional RX media ports once media is active.

        Creates a ``TTSMediaPort`` with the correct PCM format, connects it to
        ``audio_media`` via ``startTransmit``, and then either launches a
        background thread to play the WAV sequence (followed by ``initial_text``)
        or speaks ``initial_text`` directly when no sequence is configured.

        When ``audio_buffer`` was provided at construction time, also wires up:
        - ``TranscriptionPort`` for live STT of RX audio
        - ``RecordingPort`` for TX audio capture
        - ``AudioStreamPort`` for RX audio streaming / local playback

        Args:
            audio_media: The active PJSIP ``AudioMedia`` object representing
                the call's audio stream on the conference bridge.
            media_idx: Index of the active media stream within the call.
        """
        self.call_start_time = time.time()

        # --- RX transcription (optional) ---
        if self._audio_buffer is not None:
            self._transcription_port = TranscriptionPort(self._audio_buffer, self._wav_recorder_rx)

            fmt_rx = pj.MediaFormatAudio()
            fmt_rx.init(pj.PJMEDIA_FORMAT_PCM, SAMPLE_RATE, 1, 20000, 16)

            self._transcription_port.createPort("transcribe", fmt_rx)
            audio_media.startTransmit(self._transcription_port)
            log.info("Transkriptions-Port verbunden (RX).")
            self.orphan_store.append(self._transcription_port)

        # --- TX recording (optional) ---
        if self._wav_recorder_tx is not None:
            self._recording_port = RecordingPort(self._wav_recorder_tx)
            fmt_tx = pj.MediaFormatAudio()
            fmt_tx.init(pj.PJMEDIA_FORMAT_PCM, SAMPLE_RATE, 1, 20000, 16)
            self._recording_port.createPort("tx_recording", fmt_tx)
            log.info("TX RecordingPort erstellt.")
            self.orphan_store.append(self._recording_port)

        # --- Audio streaming (optional) ---
        if self._audio_cfg.socket_path or self._audio_cfg.play_audio:
            self._audio_stream_port = AudioStreamPort(
                socket_path=self._audio_cfg.socket_path,
                play_audio=self._audio_cfg.play_audio,
                audio_device=self._audio_cfg.audio_device,
            )
            fmt_sock = pj.MediaFormatAudio()
            fmt_sock.init(pj.PJMEDIA_FORMAT_PCM, SAMPLE_RATE, 1, 20000, 16)
            self._audio_stream_port.createPort("audio_stream", fmt_sock)
            audio_media.startTransmit(self._audio_stream_port)
            log.info(
                "Audio-Stream verbunden (socket={}, play_audio={})".format(
                    self._audio_cfg.socket_path, self._audio_cfg.play_audio
                )
            )
            self.orphan_store.append(self._audio_stream_port)

        # --- TTS media port ---
        self._media_port = TTSMediaPort(self._audio_queue)

        fmt = pj.MediaFormatAudio()
        fmt.init(pj.PJMEDIA_FORMAT_PCM, CLOCK_RATE, CHANNEL_COUNT, 20000, BITS_PER_SAMPLE)

        self._media_port.createPort("tts_port", fmt)

        # TTS-MediaPort -> Call Audio (Anrufer hört TTS)
        self._media_port.startTransmit(audio_media)

        log.info("TTS-MediaPort mit Call verbunden")

        if self._sequence.segments:
            # WAV-Sequence zuerst abspielen, dann initial_text
            threading.Thread(target=self._playback_sequence, args=(media_idx,), daemon=False).start()
        elif self._initial_text:
            # Kein WAV → initial_text sofort sprechen
            self._tts_producer.speak(self._initial_text)

        self.orphan_store.append(self._media_port)

    def _playback_sequence(self, media_idx: int) -> None:
        """Play the configured WAV sequence and then speak the initial text.

        Runs in a dedicated non-daemon thread. Registers the thread with PJSIP
        via ``libRegisterThread`` before touching any PJSIP objects (required
        for all threads that call PJSUA2 API functions).

        After the sequence finishes (or if the call disconnects mid-playback),
        any TTS temp files are cleaned up and the underlying
        ``AudioMediaPlayer`` objects are moved to ``orphan_store``.  If the
        call is still connected and ``initial_text`` is set, the text is
        enqueued for TTS synthesis.

        Args:
            media_idx: Index of the active media stream, forwarded to
                ``AudioPlayer.play_sequence`` for conference-bridge routing.
        """
        pj.Endpoint.instance().libRegisterThread("playback")
        extra_targets: list[Any] = []
        if self._recording_port is not None:
            extra_targets.append(self._recording_port)
        self._player = AudioPlayer(self._sequence)
        self._player.play_sequence(self, media_idx, self.disconnected_event, extra_targets=extra_targets)
        self._player.cleanup_tts()
        self.orphan_store.extend(self._player._players)
        # Nach WAV-Sequence: initial_text sprechen
        if self._initial_text and not self.disconnected_event.is_set():
            self._tts_producer.speak(self._initial_text)

    def on_disconnected(self) -> None:
        """Handle call disconnection: release media resources and invoke callback."""
        self.call_end_time = time.time()
        if self._player:
            self._player.stop_all()
        if self._audio_stream_port is not None:
            self._audio_stream_port.close()
        if self._wav_recorder_rx is not None:
            self._wav_recorder_rx.close()
        if self._wav_recorder_tx is not None:
            self._wav_recorder_tx.close()
        log.info("RealtimeTtsCall beendet.")
        if self._on_call_ended is not None:
            threading.Thread(
                target=self._on_call_ended,
                args=(self,),
                daemon=False,
                name="post-call-report",
            ).start()
