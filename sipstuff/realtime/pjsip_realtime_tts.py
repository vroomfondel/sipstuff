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

This module is used by the ``callee_realtime-tts`` CLI subcommand.
"""

import threading
from queue import Queue
from typing import Any

import pjsua2 as pj
from loguru import logger

from sipstuff import SipCalleeAccount, SipCalleeCall
from sipstuff.sip_media import AudioPlayer
from sipstuff.sipconfig import PlaybackSequence
from sipstuff.tts.live import (
    BITS_PER_SAMPLE,
    CHANNEL_COUNT,
    CLOCK_RATE,
    PiperTTSProducer,
    TTSMediaPort,
    interactive_console,
)

log = logger.bind(classname="RealtimeTTS")

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
        """
        super().__init__(account, call_id)
        self._audio_queue = audio_queue
        self._tts_producer = tts_producer
        self._initial_text = initial_text
        self._sequence = sequence or PlaybackSequence(segments=[])
        self._media_port: TTSMediaPort | None = None
        self._player: AudioPlayer | None = None

    def on_media_active(self, audio_media: Any, media_idx: int) -> None:
        """Set up the TTS media port and start audio delivery once media is active.

        Creates a ``TTSMediaPort`` with the correct PCM format, connects it to
        ``audio_media`` via ``startTransmit``, and then either launches a
        background thread to play the WAV sequence (followed by ``initial_text``)
        or speaks ``initial_text`` directly when no sequence is configured.

        The media port is appended to ``orphan_store`` so it is kept alive
        until the endpoint shuts down.

        Args:
            audio_media: The active PJSIP ``AudioMedia`` object representing
                the call's audio stream on the conference bridge.
            media_idx: Index of the active media stream within the call.
        """
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
        self._player = AudioPlayer(self._sequence)
        self._player.play_sequence(self, media_idx, self.disconnected_event)
        self._player.cleanup_tts()
        self.orphan_store.extend(self._player._players)
        # Nach WAV-Sequence: initial_text sprechen
        if self._initial_text and not self.disconnected_event.is_set():
            self._tts_producer.speak(self._initial_text)

    def on_disconnected(self) -> None:
        """Handle call disconnection by stopping any in-progress WAV playback."""
        if self._player:
            self._player.stop_all()
