#!/usr/bin/env python3
"""Incoming-call handler for live transcription of remote audio via PJSIP.

This module implements a callee-side SIP call handler that live-transcribes
the remote party's speech using VAD-based chunking (via ``VADAudioBuffer``
and ``TranscriptionPort``). It is used by the ``callee_live-transcribe`` CLI
subcommand.

Key behaviour:
- Transcription is triggered after 300 ms of detected silence (speech pause).
- A forced transcription flush occurs at least every 5 seconds regardless of
  silence, preventing indefinite buffering.
- Audio chunks shorter than 0.5 s are skipped to avoid micro-chunk artefacts.

Optional features activated at call start:
- WAV file or TTS announcement playback via a ``PlaybackSequence``.
- TX-side (microphone / local outgoing) audio recording to a WAV file.
- RX-side audio streaming to a Unix domain socket and/or local sounddevice
  output via ``AudioStreamPort``.

Requires:
    faster-whisper, numpy, and PJSUA2 Python bindings.
"""

import threading
import time
from collections.abc import Callable
from typing import Any

import pjsua2 as pj
from loguru import logger

from sipstuff import SipCalleeAccount, SipCalleeCall
from sipstuff.audio import VADAudioBuffer, WavRecorder
from sipstuff.sip_media import AudioPlayer, AudioStreamPort, RecordingPort, TranscriptionPort
from sipstuff.sipconfig import AudioDeviceConfig, PlaybackSequence

SAMPLE_RATE = 16000

log = logger.bind(classname="LiveTranscribe")


# ---------------------------------------------------------------------------
# Callee-Modus: LiveTranscribeCall
# ---------------------------------------------------------------------------
class SipCalleeLiveTranscribeCall(SipCalleeCall):
    """Callee-side SIP call that live-transcribes the remote party's audio.

    On answer, the call wires a ``TranscriptionPort`` to the conference bridge
    so that every incoming PCM frame is fed into a ``VADAudioBuffer``. An
    optional ``PlaybackSequence`` (WAV files or TTS segments) is played toward
    the remote party immediately after media becomes active. TX-side audio can
    be recorded in parallel via a ``RecordingPort``, and RX-side audio can be
    streamed to an external sink via ``AudioStreamPort``.

    When the call ends, ``on_disconnected`` closes all media resources and
    optionally invokes a post-call report callback in a background thread.
    """

    def __init__(
        self,
        account: SipCalleeAccount,
        call_id: int,
        *,
        audio_buffer: VADAudioBuffer,
        wav_recorder_rx: WavRecorder | None = None,
        wav_recorder_tx: WavRecorder | None = None,
        sequence: PlaybackSequence,
        audio: AudioDeviceConfig | None = None,
        on_call_ended: Callable[["SipCalleeLiveTranscribeCall"], None] | None = None,
    ) -> None:
        """Initialise the live-transcribe call handler.

        Args:
            account: The ``SipCalleeAccount`` that accepted the incoming call.
            call_id: PJSUA2 call identifier passed to the ``pj.Call`` base class.
            audio_buffer: Shared VAD audio buffer that receives transcription frames
                and drives chunking/flush logic for the STT backend.
            wav_recorder_rx: Optional WAV recorder that captures the RX (remote)
                audio stream alongside transcription. ``None`` disables RX recording.
            wav_recorder_tx: Optional WAV recorder that captures the TX (local /
                outgoing) audio stream. ``None`` disables TX recording.
            sequence: Ordered sequence of WAV / TTS segments to play toward the
                remote party once media is active. An empty sequence skips playback.
            audio: Audio device configuration controlling null-device usage, optional
                Unix domain socket path, and local sounddevice playback. Defaults to
                ``AudioDeviceConfig()`` when ``None``.
            on_call_ended: Optional callback invoked in a background thread after the
                call disconnects and all media resources have been released. Receives
                the ``SipCalleeLiveTranscribeCall`` instance as its sole argument.
        """
        super().__init__(account, call_id)
        self._audio_buffer = audio_buffer
        self._wav_recorder_rx = wav_recorder_rx
        self._wav_recorder_tx = wav_recorder_tx
        self._recording_port: RecordingPort | None = None
        self._sequence = sequence
        self._audio_cfg = audio or AudioDeviceConfig()
        self._on_call_ended = on_call_ended
        self._transcription_port: TranscriptionPort | None = None
        self.call_tracking_id: str | None = None
        self.call_stt: Any = None
        self._audio_stream_port: AudioStreamPort | None = None
        self._player: AudioPlayer | None = None
        self.call_start_time: float | None = None
        self.call_end_time: float | None = None

    def on_media_active(self, audio_media: Any, media_idx: int) -> None:
        """Wire all media ports once the call's audio media stream is active.

        Called by the base class ``SipCalleeCall`` when PJSIP reports that the
        audio media slot is in the ``ACTIVE`` state. This method:

        1. Records ``call_start_time`` as the current epoch timestamp.
        2. Creates and connects a ``TranscriptionPort`` to ``audio_media`` for
           RX live transcription (feeding ``self._audio_buffer``).
        3. If ``wav_recorder_tx`` was provided, creates a ``RecordingPort`` for
           TX audio capture.
        4. If the audio config specifies a socket path or local playback, creates
           an ``AudioStreamPort`` and connects it to ``audio_media`` for RX
           streaming.
        5. If ``sequence`` contains segments, spawns a daemon-less background
           thread to run ``_playback_sequence``.

        Args:
            audio_media: The active PJSUA2 ``AudioMedia`` object for this call's
                media slot, used to call ``startTransmit`` on each port.
            media_idx: Index of the active media slot within the call, forwarded
                to ``_playback_sequence`` so the player can route its output.
        """
        self.call_start_time = time.time()
        # RX transcription port
        self._transcription_port = TranscriptionPort(self._audio_buffer, self._wav_recorder_rx)

        fmt = pj.MediaFormatAudio()
        fmt.init(pj.PJMEDIA_FORMAT_PCM, SAMPLE_RATE, 1, 20000, 16)

        self._transcription_port.createPort("transcribe", fmt)
        audio_media.startTransmit(self._transcription_port)
        log.info("Transkriptions-Port verbunden (RX).")
        self.orphan_store.append(self._transcription_port)

        # Optional: TX recording via RecordingPort
        if self._wav_recorder_tx is not None:
            self._recording_port = RecordingPort(self._wav_recorder_tx)
            fmt_tx = pj.MediaFormatAudio()
            fmt_tx.init(pj.PJMEDIA_FORMAT_PCM, SAMPLE_RATE, 1, 20000, 16)
            self._recording_port.createPort("tx_recording", fmt_tx)
            log.info("TX RecordingPort erstellt.")
            self.orphan_store.append(self._recording_port)

        # Optional: audio streaming (RX) — socket and/or local playback
        if self._audio_cfg.socket_path or self._audio_cfg.play_audio:
            self._audio_stream_port = AudioStreamPort(
                socket_path=self._audio_cfg.socket_path,
                play_audio=self._audio_cfg.play_audio,
                audio_device=self._audio_cfg.audio_device,
            )
            fmt_sock = pj.MediaFormatAudio()
            fmt_sock.init(pj.PJMEDIA_FORMAT_PCM, 16000, 1, 20000, 16)
            self._audio_stream_port.createPort("audio_stream", fmt_sock)
            audio_media.startTransmit(self._audio_stream_port)
            log.info(
                "Audio-Stream verbunden (socket={}, play_audio={})".format(
                    self._audio_cfg.socket_path, self._audio_cfg.play_audio
                )
            )
            self.orphan_store.append(self._audio_stream_port)

        # Optional: WAV playback via AudioPlayer
        if self._sequence.segments:
            threading.Thread(target=self._playback_sequence, args=(media_idx,), daemon=False).start()

    def _playback_sequence(self, media_idx: int) -> None:
        """Play the configured WAV/TTS sequence toward the remote party.

        Runs in a background thread spawned by ``on_media_active``. Registers
        the thread with PJSIP via ``libRegisterThread`` (required for any
        non-PJSIP thread that touches PJSUA2 objects), then delegates to
        ``AudioPlayer.play_sequence``. If a TX ``RecordingPort`` is active, it
        is passed as an extra transmit target so the played audio is captured.
        After playback finishes, TTS temporary files are cleaned up and the
        internal player objects are moved to ``orphan_store`` for deferred
        cleanup after endpoint shutdown.

        Args:
            media_idx: Index of the active media slot, forwarded to
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

    def on_disconnected(self) -> None:
        """Release all media resources and invoke the post-call callback.

        Called by the base class ``SipCalleeCall`` when the call transitions
        to the ``DISCONNECTED`` state. This method:

        1. Records ``call_end_time`` as the current epoch timestamp.
        2. Stops any in-progress ``AudioPlayer`` playback.
        3. Closes the ``AudioStreamPort`` (flushes socket / sounddevice output).
        4. Closes the RX and TX ``WavRecorder`` instances (finalises WAV headers).
        5. Spawns a daemon-less background thread to invoke ``on_call_ended``
           (if provided), passing this call instance so the caller can access
           ``call_stt``, timing fields, and other collected data.
        """
        self.call_end_time = time.time()
        if self._player:
            self._player.stop_all()
        if self._audio_stream_port is not None:
            self._audio_stream_port.close()
        if self._wav_recorder_rx is not None:
            self._wav_recorder_rx.close()
        if self._wav_recorder_tx is not None:
            self._wav_recorder_tx.close()
        log.info("LiveTranscribeCall beendet.")
        if self._on_call_ended is not None:
            threading.Thread(
                target=self._on_call_ended,
                args=(self,),
                daemon=False,
                name="post-call-report",
            ).start()
