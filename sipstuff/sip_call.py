"""PJSUA2 Call subclass with callbacks for state changes and media.

Exposes threading events so the caller can synchronously wait for
connection, media readiness, or disconnection.
"""

import threading
from typing import TYPE_CHECKING, Any

import pjsua2 as pj
from loguru import logger

from sipstuff.sip_account import SipAccount, SipCalleeAccount, SipCallerAccount
from sipstuff.sip_media import AudioStreamPort, RxStreamPort, StereoMixer, TranscriptionPort, TxStreamPort
from sipstuff.sipconfig import AudioDeviceConfig

if TYPE_CHECKING:
    from sipstuff.tts.live import TTSMediaPort

if TYPE_CHECKING:
    from sipstuff.audio import VADAudioBuffer


class SipCall(pj.Call):  # type: ignore[misc]
    """PJSUA2 Call subclass with shared state for state changes and audio routing.

    Provides threading events for connection/disconnection/media-readiness
    and audio-device routing.  Media management (WAV playback, recording,
    streaming, transcription) lives in ``SipCallerCall``.

    Args:
        account: The PJSUA2 account that owns this call.
        call_id: Existing PJSUA2 call ID, or ``PJSUA_INVALID_ID`` for a new
            outgoing call.

    Attributes:
        connected_event: Set when the call enters CONFIRMED state.
        disconnected_event: Set when the call enters DISCONNECTED state.
        media_ready_event: Set when an active audio media channel is available.
    """

    def __init__(
        self,
        account: SipCallerAccount | SipCalleeAccount,
        call_id: int = pj.PJSUA_INVALID_ID,
        audio: AudioDeviceConfig | None = None,
    ) -> None:
        """Initialise the call, threading events, and audio configuration.

        Args:
            account: The PJSUA2 account that owns this call.
            call_id: Existing PJSUA2 call ID, or ``PJSUA_INVALID_ID`` for a
                new outgoing call.
            audio: Audio device configuration.  Defaults to
                ``AudioDeviceConfig()`` (null devices) when ``None``.
        """
        super().__init__(account, call_id)
        self.connected_event = threading.Event()
        self.disconnected_event = threading.Event()
        self.media_ready_event = threading.Event()
        self._audio_cfg: AudioDeviceConfig = audio or AudioDeviceConfig()
        self._audio_media: Any = None
        self._account: SipCallerAccount | SipCalleeAccount = account
        self._disconnect_reason: str = ""

    @property
    def is_disconnected(self) -> bool:
        """Whether the call has entered DISCONNECTED state."""
        return self.disconnected_event.is_set()

    def wait_disconnect(self, timeout: float | None = None) -> bool:
        """Block until the call disconnects or *timeout* elapses.

        Args:
            timeout: Maximum seconds to wait.  ``None`` waits indefinitely.

        Returns:
            ``True`` if the call disconnected, ``False`` on timeout.
        """
        return self.disconnected_event.wait(timeout=timeout)

    def _route_audio_devices(self) -> None:
        """Route call audio to/from real audio devices when not using null devices."""
        if not self._audio_cfg.null_playback:
            ep = pj.Endpoint.instance()
            self._audio_media.startTransmit(ep.audDevManager().getPlaybackDevMedia())
        if not self._audio_cfg.null_capture:
            ep = pj.Endpoint.instance()
            ep.audDevManager().getCaptureDevMedia().startTransmit(self._audio_media)

    def onCallState(self, prm: "pj.OnCallStateParam") -> None:  # noqa: N802
        """PJSUA2 callback invoked on call state changes.

        Sets ``connected_event`` on CONFIRMED and ``disconnected_event``
        on DISCONNECTED.  On disconnect, ``connected_event`` is also set
        to unblock any thread waiting for an answer.

        Args:
            prm: PJSUA2 call-state callback parameter (unused directly).
        """
        ci = self.getInfo()
        logger.info(f"Call state: {ci.stateText} (last code: {ci.lastStatusCode})")

        if ci.state == pj.PJSIP_INV_STATE_CONFIRMED:
            self.connected_event.set()
        elif ci.state == pj.PJSIP_INV_STATE_DISCONNECTED:
            self._disconnect_reason = ci.lastReason
            self.disconnected_event.set()
            self.connected_event.set()  # unblock waiters


class SipCallerCall(SipCall):
    """Caller-side call with media management (WAV playback, recording, streaming, transcription).

    Extends ``SipCall`` with all media attributes, setter methods, media
    start/stop methods, and an ``onCallMediaState`` override that auto-starts
    configured media when the audio channel becomes active.

    Args:
        account: The PJSUA2 account that owns this call.
        call_id: Existing PJSUA2 call ID, or ``PJSUA_INVALID_ID`` for a new
            outgoing call.

    Attributes:
        wav_player: The active ``AudioMediaPlayer``, or ``None``.
        audio_recorder: The active ``AudioMediaRecorder``, or ``None``.
    """

    def __init__(
        self,
        account: SipCallerAccount,
        call_id: int = pj.PJSUA_INVALID_ID,
        audio: AudioDeviceConfig | None = None,
    ) -> None:
        """Initialise the caller-side call and all media attributes to ``None``.

        Args:
            account: The ``SipCallerAccount`` that owns this call.
            call_id: Existing PJSUA2 call ID, or ``PJSUA_INVALID_ID`` for a
                new outgoing call.
            audio: Audio device configuration.  Defaults to
                ``AudioDeviceConfig()`` (null devices) when ``None``.
        """
        super().__init__(account, call_id, audio=audio)
        self.wav_player: pj.AudioMediaPlayer | None = None
        self.audio_recorder: pj.AudioMediaRecorder | None = None
        self.tx_recorder: pj.AudioMediaRecorder | None = None
        self.audio_stream_port: AudioStreamPort | None = None
        self.rx_stream_port: RxStreamPort | None = None
        self.tx_stream_port: TxStreamPort | None = None
        self.stereo_mixer: StereoMixer | None = None
        self.transcription_port: TranscriptionPort | None = None
        self._vad_buffer: "VADAudioBuffer | None" = None
        self._tts_port: "TTSMediaPort | None" = None
        self._wav_path: str | None = None
        self._record_path: str | None = None
        self._record_tx_path: str | None = None
        self._audio_socket_path: str | None = None
        self._play_audio: bool = False
        self._autoplay: bool = True

    def set_record_path(self, record_path: str | None) -> None:
        """Configure the output WAV path for recording remote-party audio.

        Args:
            record_path: Path to the output WAV file, or ``None`` to disable recording.
        """
        self._record_path = record_path

    def set_audio_socket_path(self, socket_path: str | None) -> None:
        """Configure a Unix domain socket path for live audio streaming.

        Args:
            socket_path: Path to the Unix domain socket, or ``None`` to disable streaming.
        """
        self._audio_socket_path = socket_path

    def set_play_audio(self, play_audio: bool) -> None:
        """Configure direct local audio playback via sounddevice.

        Args:
            play_audio: If ``True``, remote-party audio will be played on the
                local sound device during the call.
        """
        self._play_audio = play_audio

    def set_wav_path(self, wav_path: str | None, autoplay: bool = True) -> None:
        """Configure the WAV file to play and whether to start on media ready.

        Args:
            wav_path: Path to the WAV file, or ``None`` to disable playback.
            autoplay: If ``True``, playback starts automatically when the
                media channel becomes active.  Set to ``False`` when
                ``SipCaller.make_call`` manages playback timing.
        """
        self._wav_path = wav_path
        self._autoplay = autoplay

    def set_vad_buffer(self, buffer: "VADAudioBuffer") -> None:
        """Configure a VAD audio buffer for live transcription.

        Args:
            buffer: VAD-enabled audio buffer that will receive remote-party frames.
        """
        self._vad_buffer = buffer

    def set_tts_port(self, port: "TTSMediaPort") -> None:
        """Configure a live TTS media port for real-time speech synthesis.

        The port is wired into the conference bridge automatically in
        ``onCallMediaState`` when the audio channel becomes active.

        Args:
            port: The ``TTSMediaPort`` to transmit into the call.
        """
        self._tts_port = port

    def set_record_tx_path(self, record_tx_path: str | None) -> None:
        """Configure the output WAV path for recording local (TX) audio.

        Args:
            record_tx_path: Path to the output WAV file, or ``None`` to disable TX recording.
        """
        self._record_tx_path = record_tx_path

    def onCallMediaState(self, prm: "pj.OnCallMediaStateParam") -> None:  # noqa: N802
        """PJSUA2 callback invoked when media state changes.

        Finds the first active audio media channel, stores it, sets
        ``media_ready_event``, starts recording if a record path is
        configured, and optionally starts WAV playback if ``autoplay``
        is enabled.

        Args:
            prm: PJSUA2 media-state callback parameter (unused directly).
        """
        ci = self.getInfo()
        for mi in ci.media:
            if mi.type == pj.PJMEDIA_TYPE_AUDIO and mi.status == pj.PJSUA_CALL_MEDIA_ACTIVE:
                self._audio_media = self.getAudioMedia(mi.index)
                self.media_ready_event.set()
                self._route_audio_devices()
                if self._record_path:
                    self.start_recording()
                if self._record_tx_path and not self._audio_cfg.null_capture:
                    self.start_tx_recording()
                if self._audio_socket_path or self._play_audio:
                    self.start_audio_stream()
                if self._vad_buffer is not None:
                    self.start_transcription()
                if self._tts_port is not None:
                    self._tts_port.startTransmit(self._audio_media)
                    if self.tx_stream_port is not None:
                        self._tts_port.startTransmit(self.tx_stream_port)
                    elif self.audio_stream_port is not None:
                        self._tts_port.startTransmit(self.audio_stream_port)
                    logger.info("TTS media port connected to call")
                if self._autoplay and self._wav_path:
                    self.play_wav()
                break

    def play_wav(self) -> bool:
        """Start playing the configured WAV file in loop mode.

        The player is created once and loops continuously.  Repeat
        count and timing are managed by ``SipCaller.make_call``; the loop
        keeps the conference port alive for clean ``stopTransmit`` teardown.

        In stereo mode, the WAV player is also routed to the TX stream port
        (right channel) so the listener hears the outgoing audio.

        Returns:
            ``True`` if playback started successfully, ``False`` if no WAV
            path or audio media is configured, or if an error occurred.
        """
        if not self._wav_path or not self._audio_media:
            return False
        try:
            if self.wav_player is None:
                self.wav_player = pj.AudioMediaPlayer()
                self.wav_player.createPlayer(self._wav_path, pj.PJMEDIA_FILE_NO_LOOP)
                self.wav_player.startTransmit(self._audio_media)
                if self.tx_stream_port is not None:
                    self.wav_player.startTransmit(self.tx_stream_port)
                elif self.audio_stream_port is not None:
                    self.wav_player.startTransmit(self.audio_stream_port)
            logger.info(f"Playing WAV: {self._wav_path}")
            return True
        except Exception as exc:
            logger.error(f"Failed to play WAV: {exc}")
            return False

    def stop_wav(self, _orphan_store: list[Any] | None = None) -> None:
        """Stop current WAV playback and disconnect from the conference bridge.

        If ``_orphan_store`` is provided the player object is moved there
        instead of being destroyed immediately -- this avoids the PJSIP
        "Remove port failed" warning that occurs when CPython's ref-counting
        triggers the C++ destructor while the conference bridge is still
        active.  ``SipCaller.stop`` clears the store before ``libDestroy``
        when cleanup is safe.

        Args:
            _orphan_store: Optional list to receive the detached player
                reference for deferred destruction.
        """
        if self.wav_player is not None:
            if self._audio_media is not None:
                try:
                    self.wav_player.stopTransmit(self._audio_media)
                except Exception:
                    pass
            if self.tx_stream_port is not None:
                try:
                    self.wav_player.stopTransmit(self.tx_stream_port)
                except Exception:
                    pass
            elif self.audio_stream_port is not None:
                try:
                    self.wav_player.stopTransmit(self.audio_stream_port)
                except Exception:
                    pass
            if _orphan_store is not None:
                _orphan_store.append(self.wav_player)
            self.wav_player = None

    def start_recording(self) -> bool:
        """Start recording remote-party audio to the configured WAV file.

        Creates an ``AudioMediaRecorder`` and connects the call's audio
        media to it (reverse direction of playback).

        Returns:
            ``True`` if recording started successfully, ``False`` if no
            record path or audio media is configured, or if an error occurred.
        """
        if not self._record_path or not self._audio_media:
            return False
        try:
            if self.audio_recorder is None:
                self.audio_recorder = pj.AudioMediaRecorder()
                self.audio_recorder.createRecorder(self._record_path)
            self._audio_media.startTransmit(self.audio_recorder)
            logger.info(f"Recording remote audio to: {self._record_path}")
            return True
        except Exception as exc:
            logger.error(f"Failed to start recording: {exc}")
            return False

    def stop_recording(self, _orphan_store: list[Any] | None = None) -> None:
        """Stop recording and disconnect the recorder from the conference bridge.

        Uses the same orphan pattern as ``stop_wav`` to avoid the PJSIP
        conference bridge teardown race.

        Args:
            _orphan_store: Optional list to receive the detached recorder
                reference for deferred destruction.
        """
        if self.audio_recorder is not None:
            if self._audio_media is not None:
                try:
                    self._audio_media.stopTransmit(self.audio_recorder)
                except Exception:
                    pass
            if _orphan_store is not None:
                _orphan_store.append(self.audio_recorder)
            self.audio_recorder = None

    def start_tx_recording(self) -> bool:
        """Start recording local (TX) audio to the configured WAV file.

        Creates an ``AudioMediaRecorder`` and connects the capture device
        media to it.  Only possible when ``_null_capture`` is ``False``.

        Returns:
            ``True`` if recording started successfully, ``False`` otherwise.
        """
        if not self._record_tx_path or self._audio_cfg.null_capture:
            return False
        try:
            if self.tx_recorder is None:
                self.tx_recorder = pj.AudioMediaRecorder()
                self.tx_recorder.createRecorder(self._record_tx_path)
                ep = pj.Endpoint.instance()
                ep.audDevManager().getCaptureDevMedia().startTransmit(self.tx_recorder)
            logger.info(f"Recording local (TX) audio to: {self._record_tx_path}")
            return True
        except Exception as exc:
            logger.error(f"Failed to start TX recording: {exc}")
            return False

    def stop_tx_recording(self, _orphan_store: list[Any] | None = None) -> None:
        """Stop TX recording and disconnect from the capture device.

        Uses the same orphan pattern as ``stop_recording``.

        Args:
            _orphan_store: Optional list to receive the detached recorder
                reference for deferred destruction.
        """
        if self.tx_recorder is not None:
            if not self._audio_cfg.null_capture:
                try:
                    ep = pj.Endpoint.instance()
                    ep.audDevManager().getCaptureDevMedia().stopTransmit(self.tx_recorder)
                except Exception:
                    pass
            if _orphan_store is not None:
                _orphan_store.append(self.tx_recorder)
            self.tx_recorder = None

    def start_audio_stream(self) -> bool:
        """Start streaming call audio to a Unix socket and/or local playback.

        Three modes depending on ``play_rx`` / ``play_tx`` in ``AudioDeviceConfig``:

        * **RX only** (default): mono ``AudioStreamPort`` ← remote-party audio.
        * **TX only**: mono ``AudioStreamPort`` ← capture device.
        * **RX + TX (stereo)**: ``RxStreamPort`` ← remote audio,
          ``TxStreamPort`` ← capture device, both feeding a ``StereoMixer``
          that interleaves L=RX, R=TX into a stereo output.

        Returns:
            ``True`` if streaming started successfully, ``False`` if neither
            socket path nor play_audio is configured, or if an error occurred.
        """
        if (not self._audio_socket_path and not self._play_audio) or not self._audio_media:
            return False

        play_rx = self._audio_cfg.play_rx
        play_tx = self._audio_cfg.play_tx

        try:
            if play_rx and play_tx:
                # Stereo mode: RX (left) + TX (right)
                if self.stereo_mixer is None:
                    self.stereo_mixer = StereoMixer(
                        socket_path=self._audio_socket_path,
                        play_audio=self._play_audio,
                        audio_device=self._audio_cfg.audio_device,
                    )
                    fmt = pj.MediaFormatAudio()
                    fmt.init(pj.PJMEDIA_FORMAT_PCM, 16000, 1, 20000, 16)

                    self.rx_stream_port = RxStreamPort(self.stereo_mixer)
                    self.rx_stream_port.createPort("rx_stream", fmt)

                    self.tx_stream_port = TxStreamPort(self.stereo_mixer)
                    fmt_tx = pj.MediaFormatAudio()
                    fmt_tx.init(pj.PJMEDIA_FORMAT_PCM, 16000, 1, 20000, 16)
                    self.tx_stream_port.createPort("tx_stream", fmt_tx)

                # Always (re-)wire to current _audio_media — handles SWIG wrapper
                # reassignment across multiple onCallMediaState invocations.
                # startTransmit is idempotent for existing (src, sink) pairs.
                self._audio_media.startTransmit(self.rx_stream_port)
                if not self._audio_cfg.null_capture:
                    ep = pj.Endpoint.instance()
                    ep.audDevManager().getCaptureDevMedia().startTransmit(self.tx_stream_port)

                logger.info("Stereo audio stream started (L=RX, R=TX)")
            elif play_tx and not play_rx:
                # TX-only mono
                if self.audio_stream_port is None and not self._audio_cfg.null_capture:
                    self.audio_stream_port = AudioStreamPort(
                        socket_path=self._audio_socket_path,
                        play_audio=self._play_audio,
                        audio_device=self._audio_cfg.audio_device,
                    )
                    fmt = pj.MediaFormatAudio()
                    fmt.init(pj.PJMEDIA_FORMAT_PCM, 16000, 1, 20000, 16)
                    self.audio_stream_port.createPort("audio_stream", fmt)
                if self.audio_stream_port is not None and not self._audio_cfg.null_capture:
                    ep = pj.Endpoint.instance()
                    ep.audDevManager().getCaptureDevMedia().startTransmit(self.audio_stream_port)
                logger.info("Streaming local (TX) audio to output sinks")
            else:
                # RX-only mono (default, like before)
                if self.audio_stream_port is None:
                    self.audio_stream_port = AudioStreamPort(
                        socket_path=self._audio_socket_path,
                        play_audio=self._play_audio,
                        audio_device=self._audio_cfg.audio_device,
                    )
                    fmt = pj.MediaFormatAudio()
                    fmt.init(pj.PJMEDIA_FORMAT_PCM, 16000, 1, 20000, 16)
                    self.audio_stream_port.createPort("audio_stream", fmt)
                self._audio_media.startTransmit(self.audio_stream_port)
                if self._audio_socket_path:
                    logger.info(f"Streaming remote audio to: {self._audio_socket_path}")
                if self._play_audio:
                    logger.info("Streaming remote audio to local playback device")
            return True
        except Exception as exc:
            logger.error(f"Failed to start audio stream: {exc}")
            return False

    def stop_audio_stream(self, _orphan_store: list[Any] | None = None) -> None:
        """Stop audio streaming and disconnect from the conference bridge.

        Handles both mono (``audio_stream_port``) and stereo
        (``rx_stream_port`` + ``tx_stream_port`` + ``stereo_mixer``) paths.
        Uses the same orphan pattern as ``stop_wav`` / ``stop_recording``
        to avoid the PJSIP conference bridge teardown race.

        Args:
            _orphan_store: Optional list to receive the detached port
                reference for deferred destruction.
        """
        # Stereo path — close the mixer first so in-flight frames from
        # PJSIP callback threads see _stream=None and drop harmlessly,
        # then disconnect the conference bridge ports.
        if self.stereo_mixer is not None:
            self.stereo_mixer.close()
            self.stereo_mixer = None
        if self.rx_stream_port is not None:
            if self._audio_media is not None:
                try:
                    self._audio_media.stopTransmit(self.rx_stream_port)
                except Exception:
                    pass
            self.rx_stream_port.close()
            if _orphan_store is not None:
                _orphan_store.append(self.rx_stream_port)
            self.rx_stream_port = None
        if self.tx_stream_port is not None:
            if not self._audio_cfg.null_capture:
                try:
                    ep = pj.Endpoint.instance()
                    ep.audDevManager().getCaptureDevMedia().stopTransmit(self.tx_stream_port)
                except Exception:
                    pass
            self.tx_stream_port.close()
            if _orphan_store is not None:
                _orphan_store.append(self.tx_stream_port)
            self.tx_stream_port = None

        # Mono path
        if self.audio_stream_port is not None:
            play_tx = self._audio_cfg.play_tx
            play_rx = self._audio_cfg.play_rx
            if play_tx and not play_rx and not self._audio_cfg.null_capture:
                # TX-only mono: disconnect from capture device
                try:
                    ep = pj.Endpoint.instance()
                    ep.audDevManager().getCaptureDevMedia().stopTransmit(self.audio_stream_port)
                except Exception:
                    pass
            elif self._audio_media is not None:
                try:
                    self._audio_media.stopTransmit(self.audio_stream_port)
                except Exception:
                    pass
            self.audio_stream_port.close()
            if _orphan_store is not None:
                _orphan_store.append(self.audio_stream_port)
            self.audio_stream_port = None

    def start_transcription(self) -> bool:
        """Start the live transcription port for remote-party audio.

        Creates a ``TranscriptionPort`` connected to the VAD buffer and
        wires it into the conference bridge.

        Returns:
            ``True`` if the port was started successfully, ``False`` otherwise.
        """
        if self._vad_buffer is None or self._audio_media is None:
            return False
        try:
            if self.transcription_port is None:
                self.transcription_port = TranscriptionPort(self._vad_buffer)
                fmt = pj.MediaFormatAudio()
                fmt.init(pj.PJMEDIA_FORMAT_PCM, 16000, 1, 20000, 16)
                self.transcription_port.createPort("live_transcribe", fmt)
                self._audio_media.startTransmit(self.transcription_port)
            logger.info("Live transcription port connected")
            return True
        except Exception as exc:
            logger.error(f"Failed to start transcription port: {exc}")
            return False

    def stop_transcription(self, _orphan_store: list[Any] | None = None) -> None:
        """Stop the live transcription port and disconnect from the conference bridge.

        Uses the same orphan pattern as ``stop_wav`` / ``stop_recording``
        to avoid the PJSIP conference bridge teardown race.

        Args:
            _orphan_store: Optional list to receive the detached port
                reference for deferred destruction.
        """
        if self.transcription_port is not None:
            if self._audio_media is not None:
                try:
                    self._audio_media.stopTransmit(self.transcription_port)
                except Exception:
                    pass
            self.transcription_port.close()
            if _orphan_store is not None:
                _orphan_store.append(self.transcription_port)
            self.transcription_port = None

    def stop_tts_port(self, _orphan_store: list[Any] | None = None) -> None:
        """Stop the live TTS media port and disconnect from the conference bridge.

        Uses the same orphan pattern as ``stop_wav`` / ``stop_recording``
        to avoid the PJSIP conference bridge teardown race.

        Args:
            _orphan_store: Optional list to receive the detached port
                reference for deferred destruction.
        """
        if self._tts_port is not None:
            if self._audio_media is not None:
                try:
                    self._tts_port.stopTransmit(self._audio_media)
                except Exception:
                    pass
            if self.tx_stream_port is not None:
                try:
                    self._tts_port.stopTransmit(self.tx_stream_port)
                except Exception:
                    pass
            elif self.audio_stream_port is not None:
                try:
                    self._tts_port.stopTransmit(self.audio_stream_port)
                except Exception:
                    pass
            if _orphan_store is not None:
                _orphan_store.append(self._tts_port)
            self._tts_port = None


class SipCalleeCall(SipCall):
    """Base incoming-call handler with hooks for subclass customisation.

    Inherits from ``SipCall`` to reuse connection/disconnection events,
    null-audio flags, and audio-device routing.  Adds callee-specific hooks
    (``on_media_active``, ``on_disconnected``) and an ``orphan_store``
    property that delegates to the owning account.

    Subclasses override ``on_media_active`` and ``on_disconnected`` to wire up
    domain-specific media (WAV players, TTS ports, transcription ports, etc.).

    Args:
        account: The ``SipCalleeAccount`` that owns this call.
        call_id: PJSUA2 call ID from ``OnIncomingCallParam.callId``.
        audio: Audio device configuration.
    """

    _account: "SipCalleeAccount"  # narrow type from SipCall

    def __init__(
        self,
        account: "SipCalleeAccount",
        call_id: int = pj.PJSUA_INVALID_ID,
        audio: AudioDeviceConfig | None = None,
    ) -> None:
        """Initialise the callee-side call and bind a class-tagged logger.

        Args:
            account: The ``SipCalleeAccount`` that owns this call.
            call_id: PJSUA2 call ID from ``OnIncomingCallParam.callId``.
            audio: Audio device configuration.  Defaults to
                ``AudioDeviceConfig()`` (null devices) when ``None``.
        """
        super().__init__(account, call_id, audio=audio)
        self._log = logger.bind(classname="CalleeCall")

    @property
    def orphan_store(self) -> list[Any]:
        """Reference to the shared orphan store for deferred port destruction.

        Subclasses should append PJSIP media objects (players, recorders,
        custom ports) here instead of destroying them immediately.
        """
        return self._account.orphan_store

    # -- PJSUA2 callbacks ---------------------------------------------------

    def onCallState(self, prm: "pj.OnCallStateParam") -> None:  # noqa: N802
        """PJSUA2 callback invoked on call state changes.

        Calls ``super().onCallState()`` (sets connected/disconnected events),
        then logs callee-specific info and calls ``on_disconnected()`` hook.
        """
        super().onCallState(prm)
        ci = self.getInfo()
        self._log.info(
            f"Call-Status: {ci.stateText}  |  " f"Remote: {ci.remoteUri}  |  " f"Dauer: {ci.connectDuration.sec}s"
        )
        if ci.state == pj.PJSIP_INV_STATE_DISCONNECTED:
            self._log.info("Anruf beendet.")
            self.on_disconnected()

    def onCallMediaState(self, prm: "pj.OnCallMediaStateParam") -> None:  # noqa: N802
        """PJSUA2 callback invoked when media state changes.

        Finds the first active audio media, routes audio devices via
        ``_route_audio_devices()``, sets ``media_ready_event``, then
        delegates to ``on_media_active``.
        """
        ci = self.getInfo()
        for mi_idx, mi in enumerate(ci.media):
            if mi.type == pj.PJMEDIA_TYPE_AUDIO and mi.status == pj.PJSUA_CALL_MEDIA_ACTIVE:
                self._audio_media = self.getAudioMedia(mi_idx)
                self.media_ready_event.set()
                self._route_audio_devices()
                self.on_media_active(self._audio_media, mi_idx)
                break

    # -- Hooks for subclasses -----------------------------------------------

    def on_media_active(self, audio_media: Any, media_idx: int) -> None:
        """Called when the first active audio media channel is available.

        Override in subclasses to wire up custom media (WAV player, TTS port,
        transcription port, etc.).

        Args:
            audio_media: The PJSUA2 ``AudioMedia`` object for this call.
            media_idx: Index of the media channel in the call info.
        """

    def on_disconnected(self) -> None:
        """Called when the call enters DISCONNECTED state.

        Override in subclasses for cleanup (stop players, close ports, etc.).
        """
