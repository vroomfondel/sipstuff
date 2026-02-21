#!/usr/bin/env python3
"""Auto-answer incoming SIP calls and play a configurable WAV/TTS sequence.

Registers with a SIP server, waits for incoming calls, answers them
automatically, plays a configurable sequence of WAV files and/or Piper TTS
announcements into the call, then hangs up. Used via the
``callee_autoanswer`` CLI subcommand.
"""

import threading
from typing import Any

import pjsua2 as pj

from sipstuff import SipCalleeAccount, SipCalleeCall
from sipstuff.sip_media import AudioPlayer
from sipstuff.sipconfig import PlaybackSequence


class SipCalleeAutoAnswerCall(SipCalleeCall):
    """Callee-side call handler that plays a WAV/TTS sequence then hangs up.

    After the call media becomes active, plays each segment of the configured
    ``PlaybackSequence`` in order, then initiates a hangup. If the remote
    party disconnects first, cleanup is handled via ``on_disconnected``.
    """

    def __init__(self, account: SipCalleeAccount, call_id: int, *, sequence: PlaybackSequence) -> None:
        """Initialise the auto-answer call handler.

        Args:
            account: The callee SIP account that received the incoming call.
            call_id: PJSUA2 call identifier passed to the base ``pj.Call``.
            sequence: Ordered list of WAV/TTS segments to play during the call.
        """
        super().__init__(account, call_id)
        self._sequence = sequence
        self._player: AudioPlayer | None = None

    def on_media_active(self, audio_media: Any, media_idx: int) -> None:
        """Handle the media-active event once the call audio stream is ready.

        If the sequence contains segments, spawns a non-daemon thread to run
        ``_playback_then_hangup``. Otherwise falls back to routing the capture
        device directly into the call (pass-through mode with no playback).

        Args:
            audio_media: The active PJSUA2 ``AudioMedia`` object for this call.
            media_idx: Index of the active media slot within the call.
        """
        if self._sequence.segments:
            threading.Thread(target=self._playback_then_hangup, args=(media_idx,), daemon=False).start()
        else:
            pj.Endpoint.instance().audDevManager().getCaptureDevMedia().startTransmit(audio_media)

    def _playback_then_hangup(self, media_idx: int) -> None:
        """Register the playback thread with PJSIP, play the sequence, then hang up.

        Registers this thread with the PJSUA2 library (required before any
        PJSIP API calls from a non-PJSIP thread), plays every segment in the
        sequence via ``AudioPlayer``, cleans up any TTS temp files, and
        finally hangs up unless the remote party has already disconnected.

        Args:
            media_idx: Index of the active media slot used to connect the
                player to the conference bridge.
        """
        pj.Endpoint.instance().libRegisterThread("playback")
        self._player = AudioPlayer(self._sequence)
        self._player.play_sequence(self, media_idx, self.disconnected_event)
        self._player.cleanup_tts()
        if not self.disconnected_event.is_set():
            self._log.info("Sequenz abgeschlossen, lege auf.")
            try:
                call_prm = pj.CallOpParam()
                self.hangup(call_prm)
            except Exception as e:
                self._log.error(f"Fehler beim Auflegen: {e}")

    def on_disconnected(self) -> None:
        """Handle call disconnection by stopping any active playback.

        Called by the base class when the call enters a disconnected state.
        Stops the ``AudioPlayer`` so that in-progress segment playback and
        any open TTS processes are terminated promptly.
        """
        if self._player:
            self._player.stop_all()
