"""Outgoing SIP call orchestration — SipCaller high-level caller class.

This module provides :class:`SipCaller`, a high-level context-manager wrapper
around the PJSUA2 endpoint lifecycle for placing outgoing SIP calls.  It
composes :class:`~sipstuff.sip_endpoint.SipEndpoint` (endpoint lifecycle),
:class:`~sipstuff.sip_account.SipCallerAccount` (account registration and
incoming-call rejection), and :class:`~sipstuff.sip_call.SipCallerCall`
(per-call media management) into a single, easy-to-use object.

Typical usage::

    with SipCaller(config) as caller:
        answered = caller.make_call("+491234567890")

:data:`SipCaller.last_call_result` is populated after every :meth:`make_call`
invocation with a :class:`~sipstuff.sip_types.CallResult` dataclass containing
timing, transcript, and mix-output information.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

import pjsua2 as pj

from sipstuff.audio import VADAudioBuffer, WavInfo
from sipstuff.sip_account import CalleeCallFactory, CallerCallFactory, SipCallerAccount
from sipstuff.sip_call import SipCallerCall
from sipstuff.sip_endpoint import SipEndpoint
from sipstuff.sip_media import SilenceDetector
from sipstuff.sip_types import CallResult, SipCallError
from sipstuff.sipconfig import (
    AudioDeviceConfig,
    CallConfig,
    RecordingConfig,
    SipCallerConfig,
    SttConfig,
    TtsPlayConfig,
    VadConfig,
    WavPlayConfig,
)
from sipstuff.tts.live import BITS_PER_SAMPLE, CHANNEL_COUNT, CLOCK_RATE, PiperTTSProducer, TTSMediaPort


class SipCaller(SipEndpoint):
    """High-level SIP caller with context-manager support.

    Wraps PJSUA2 endpoint creation, account registration, call placement,
    and WAV playback into a single context manager.  Inherits endpoint
    lifecycle from ``SipEndpoint``.

    Args:
        config: SIP caller configuration (from ``SipEndpointConfig.from_config``).
            PJSIP log levels, audio device, recording, WAV playback, STT,
            and VAD settings are all read from the respective config sections.

    Examples:
        with SipCaller(config) as caller:
            success = caller.make_call("+491234567890")
    """

    def __init__(
        self,
        config: SipCallerConfig,
        call_factory: CallerCallFactory | None = None,
    ) -> None:
        """Store configuration and initialise the caller to its idle state.

        Args:
            config: Caller configuration produced by
                ``SipCallerConfig.from_config()``.  Carries endpoint-level
                settings (SIP server, transport, NAT, audio) as well as
                call-level defaults (timeout, WAV playback, recording, STT,
                VAD, TTS).
            call_factory: Optional factory callable used to construct
                :class:`~sipstuff.sip_call.SipCallerCall` instances.
                Defaults to a lambda that creates a plain
                ``SipCallerCall``.
        """
        super().__init__(config=config)
        self.config: SipCallerConfig = config
        _audio_cfg = config.audio
        self._account: SipCallerAccount | None = None
        self.last_call_result: CallResult | None = None
        self._call_factory: CallerCallFactory = (
            call_factory
            if call_factory is not None
            else (lambda acc, cid, audio: SipCallerCall(account=acc, call_id=cid, audio=audio))
        )

    def __enter__(self) -> "SipCaller":
        """Start the PJSUA2 endpoint and return ``self`` for use as a context manager."""
        self.start()
        return self

    def _create_account(self, local_ip: str) -> None:
        """Create and register a ``SipAccount`` for outgoing calls.

        Args:
            local_ip: Local IP address to bind media transports to.

        Raises:
            SipCallError: If account registration fails.
        """
        try:
            self._account = SipCallerAccount(
                config=self.config, transport_id=self._transport_id, local_ip=local_ip, call_factory=self._call_factory
            )
        except Exception as exc:
            self.stop()
            raise SipCallError(f"SIP registration failed: {exc}") from exc
        self._log.info(f"SIP account registered: {self._account.uri}")

    def _shutdown_account(self) -> None:
        """Shut down the ``SipAccount``."""
        if self._account is not None:
            try:
                self._account.shutdown()
            except Exception:
                pass
            self._account = None

    def make_call(
        self,
        destination: str,
        *,
        call: CallConfig | None = None,
        wav_play: WavPlayConfig | TtsPlayConfig | None = None,
        recording: RecordingConfig | None = None,
        audio: AudioDeviceConfig | None = None,
        stt: SttConfig | None = None,
        vad: VadConfig | None = None,
        tts_producer: PiperTTSProducer | None = None,
        initial_tts_text: str | None = None,
    ) -> bool:
        """Place a SIP call, optionally play a WAV file on answer, and hang up.

        Builds a SIP URI from ``destination`` and the configured server,
        initiates the call, waits for an answer (up to ``timeout``), then
        plays the WAV file ``repeat`` times with optional pre/inter/post
        delays.  If no WAV file is configured the call stays up until the
        remote party hangs up (or Ctrl+C).

        When ``wav_play.tts_text`` is set, TTS generation runs automatically
        inside this method and the resulting temp file is cleaned up on exit.

        Args:
            destination: Phone number or SIP URI to call.
            call: Call timing config.  ``None`` uses ``self.config.call``.
            wav_play: WAV/TTS playback config.  ``None`` uses ``self.config.wav_play``.
            recording: Recording config.  ``None`` uses ``self.config.recording``.
            audio: Audio device config.  ``None`` uses ``self.config.audio``.
            stt: STT config.  ``None`` uses ``self.config.stt``.
            vad: VAD config.  ``None`` uses ``self.config.vad``.

        Returns:
            ``True`` if the call was answered (and WAV played if provided).
            ``False`` if the call was not answered or timed out.

        Raises:
            SipCallError: If the caller is not started or the call cannot
                be initiated.
        """
        self.last_call_result = None
        call_start = time.time()

        if self._account is None:
            raise SipCallError("SipCaller not started — call start() or use context manager")

        # Resolve config objects with fallback to self.config defaults
        _call = call or self.config.call
        _wav_play = wav_play or self.config.wav_play
        _recording = recording or self.config.recording
        _audio = audio or self.config.audio
        _stt = stt or self.config.stt
        _vad = vad or self.config.vad

        # Extract scalars from resolved config
        timeout = _call.timeout
        pre_delay = _call.pre_delay
        post_delay = _call.post_delay
        inter_delay = _call.inter_delay
        repeat = _call.repeat
        wait_for_silence = _call.wait_for_silence
        live_transcribe = _stt.live_transcribe

        # TTS generation (when wav_play is TtsPlayConfig)
        _tts_temp_path: str | None = None
        if isinstance(_wav_play, TtsPlayConfig):
            from sipstuff.tts import generate_wav as _generate_wav

            _tts_cfg = _wav_play.tts_config or self.config.tts
            _tts_temp = _generate_wav(
                text=_wav_play.tts_text,
                model=_tts_cfg.model,
                sample_rate=_tts_cfg.sample_rate or 16000,
                data_dir=_tts_cfg.data_dir,
            )
            _tts_temp_path = str(_tts_temp)
            _wav_play = WavPlayConfig(wav_path=_tts_temp_path, pause_before=_wav_play.pause_before)

        # Validate WAV (only when provided)
        wav_path: str | None = _wav_play.wav_path if isinstance(_wav_play, WavPlayConfig) else None
        wav_info: WavInfo | None = None
        if wav_path is not None:
            wav_info = WavInfo(wav_path)
            wav_info.validate()
            self._log.debug(
                f"WAV details: path={wav_info.path}, duration={wav_info.duration:.3f}s, "
                f"framerate={wav_info.framerate}Hz, channels={wav_info.channels}, "
                f"sample_width={wav_info.sample_width * 8}bit, n_frames={wav_info.n_frames}"
            )

        # Build SIP URI — always include ;transport= so PJSIP uses the correct
        # transport directly without NAPTR/SRV fallback attempts.
        scheme = "sips" if self.config.sip.transport == "tls" else "sip"
        tp_param = f";transport={self.config.sip.transport}"
        default_port = 5061 if self.config.sip.transport == "tls" else 5060
        if destination.startswith("sip:") or destination.startswith("sips:"):
            sip_uri = destination
        elif self.config.sip.port != default_port:
            sip_uri = f"{scheme}:{destination}@{self.config.sip.server}:{self.config.sip.port}{tp_param}"
        else:
            sip_uri = f"{scheme}:{destination}@{self.config.sip.server}{tp_param}"

        self._log.info(
            f"Calling {sip_uri} (timeout: {timeout}s, repeat: {repeat}x, pre: {pre_delay}s, inter: {inter_delay}s, post: {post_delay}s)"
        )

        # Don't autoplay — we manage playback timing ourselves
        call = self._call_factory(self._account, pj.PJSUA_INVALID_ID, _audio)
        if wav_info is not None:
            call.set_wav_path(str(wav_info.path), autoplay=False)
        if _recording.rx_path is not None:
            resolved_record = Path(_recording.rx_path).resolve()
            resolved_record.parent.mkdir(parents=True, exist_ok=True)
            call.set_record_path(str(resolved_record))
        if _recording.tx_path is not None:
            if _audio.null_capture:
                self._log.warning("TX recording requires real capture device (null_capture=False) — skipping")
            else:
                resolved_tx = Path(_recording.tx_path).resolve()
                resolved_tx.parent.mkdir(parents=True, exist_ok=True)
                call.set_record_tx_path(str(resolved_tx))
        if _audio.socket_path is not None:
            call.set_audio_socket_path(_audio.socket_path)
        if _audio.play_audio:
            call.set_play_audio(_audio.play_audio)
        if _audio.play_tx and _audio.null_capture:
            self._log.info("play_tx with null capture: only WAV/TTS audio will be audible (no mic)")

        if _audio.play_audio:
            from sipstuff.sip_media import validate_audio_device

            try:
                validate_audio_device(_audio.audio_device)
            except (ValueError, ImportError) as exc:
                self._log.error(f"Audio device validation failed: {exc}")
                call.set_play_audio(False)

        # Live TTS port setup (interactive mode)
        _tts_media_port: TTSMediaPort | None = None
        if tts_producer is not None:
            _tts_media_port = TTSMediaPort(tts_producer.audio_queue)
            fmt = pj.MediaFormatAudio()
            fmt.init(pj.PJMEDIA_FORMAT_PCM, CLOCK_RATE, CHANNEL_COUNT, 20000, BITS_PER_SAMPLE)
            _tts_media_port.createPort("tts_port", fmt)
            call.set_tts_port(_tts_media_port)
            self._log.info("Live TTS media port created")

        prm = pj.CallOpParam(True)
        try:
            call.makeCall(sip_uri, prm)
        except Exception as exc:
            raise SipCallError(f"Failed to initiate call to {sip_uri}: {exc}") from exc

        # Outer try/finally: guarantee the call is hung up even if an
        # unexpected exception occurs anywhere after makeCall.
        try:
            # Wait for answer or timeout
            answered = call.connected_event.wait(timeout=timeout)

            if not answered or call.disconnected_event.is_set():
                reason = call._disconnect_reason or "timeout / no answer"
                self._log.warning(f"Call not answered: {reason}")
                if not call.disconnected_event.is_set():
                    try:
                        call.hangup(pj.CallOpParam())
                    except Exception:
                        pass
                call_end = time.time()
                self.last_call_result = CallResult(
                    success=False,
                    call_start=call_start,
                    call_end=call_end,
                    call_duration=call_end - call_start,
                    answered=False,
                    disconnect_reason=reason,
                )
                return False

            self._log.info("Call answered")

            # Wait for media to be ready
            if not call.media_ready_event.wait(timeout=5):
                self._log.error("Media channel not ready after 5s — hanging up")
                try:
                    call.hangup(pj.CallOpParam())
                except Exception:
                    pass
                call_end = time.time()
                self.last_call_result = CallResult(
                    success=False,
                    call_start=call_start,
                    call_end=call_end,
                    call_duration=call_end - call_start,
                    answered=True,
                    disconnect_reason="media not ready",
                )
                return False

            # Log negotiated media info for diagnostics
            try:
                ci = call.getInfo()
                for mi in ci.media:
                    if mi.type == pj.PJMEDIA_TYPE_AUDIO:
                        self._log.debug(f"Audio media: dir={mi.dir}, status={mi.status}")
            except Exception:
                pass

            # Live transcription setup
            _vad_buffer = None
            _stt_thread = None
            if live_transcribe:
                from sipstuff.stt.live import LiveTranscriptionThread

                _vad_buffer = VADAudioBuffer(
                    silence_threshold=_vad.silence_threshold,
                    silence_trigger_sec=_vad.silence_trigger,
                    max_duration_sec=_vad.max_chunk,
                    min_duration_sec=_vad.min_chunk,
                )
                call.set_vad_buffer(_vad_buffer)
                # The transcription port is wired in onCallMediaState, but media
                # is already ready at this point — start it manually if needed.
                if call.transcription_port is None:
                    call.start_transcription()
                _stt_thread = LiveTranscriptionThread(
                    _vad_buffer,
                    stt_config=_stt,
                )
                _stt_thread.start()
                self._log.info("Live transcription started")

            # Pre-delay
            if pre_delay > 0:
                self._log.info(f"Pre-delay: {pre_delay}s")
                if call.disconnected_event.wait(timeout=pre_delay):
                    self._log.info("Remote party hung up during pre-delay")
                    call_end = time.time()
                    self.last_call_result = CallResult(
                        success=True,
                        call_start=call_start,
                        call_end=call_end,
                        call_duration=call_end - call_start,
                        answered=True,
                        disconnect_reason=call._disconnect_reason,
                    )
                    return True

            # Wait for silence from remote party before playback (e.g. wait
            # for callee's "Hello?" to finish).
            if wait_for_silence and wait_for_silence > 0 and call._audio_media is not None:
                silence_timeout = min(wait_for_silence + 10.0, timeout)
                self._log.info(f"Waiting for {wait_for_silence}s of silence (timeout: {silence_timeout}s)")
                detector = SilenceDetector(duration=wait_for_silence)
                try:
                    fmt = pj.MediaFormatAudio()
                    fmt.init(pj.PJMEDIA_FORMAT_PCM, 16000, 1, 20000, 16)
                    detector.createPort("silence_det", fmt)
                    call._audio_media.startTransmit(detector)
                    # Wait for silence or disconnect, whichever comes first
                    start_wait = time.monotonic()
                    while not detector.silence_event.is_set() and not call.disconnected_event.is_set():
                        remaining = silence_timeout - (time.monotonic() - start_wait)
                        if remaining <= 0:
                            self._log.warning("Silence wait timed out — proceeding with playback")
                            break
                        detector.silence_event.wait(timeout=min(remaining, 0.25))
                    call._audio_media.stopTransmit(detector)
                except Exception as exc:
                    self._log.warning(f"Silence detection failed ({exc}) — proceeding with playback")

                if call.disconnected_event.is_set():
                    self._log.info("Remote party hung up while waiting for silence")
                    call_end = time.time()
                    self.last_call_result = CallResult(
                        success=True,
                        call_start=call_start,
                        call_end=call_end,
                        call_duration=call_end - call_start,
                        answered=True,
                        disconnect_reason=call._disconnect_reason,
                    )
                    return True

            # Speak initial TTS text (interactive mode greeting)
            if initial_tts_text and tts_producer is not None:
                tts_producer.speak(initial_tts_text)

            if wav_info is not None:
                # Start the looping WAV player once; wait for duration × repeats.
                try:
                    call.play_wav()
                    for i in range(repeat):
                        if call.disconnected_event.is_set():
                            self._log.info("Remote party hung up during playback")
                            break

                        if repeat > 1:
                            self._log.info(f"Playing WAV pass ({i + 1}/{repeat})")

                        if call.disconnected_event.wait(timeout=wav_info.duration + 0.3):
                            self._log.info("Remote party hung up during playback")
                            break

                        # Inter-delay between repeats (not after the last one)
                        if inter_delay > 0 and i < repeat - 1:
                            # Pause transmission so the remote side hears silence
                            if call.wav_player is not None and call._audio_media is not None:
                                try:
                                    call.wav_player.stopTransmit(call._audio_media)
                                except Exception:
                                    pass
                            if call.wav_player is not None and call.tx_stream_port is not None:
                                try:
                                    call.wav_player.stopTransmit(call.tx_stream_port)
                                except Exception:
                                    pass
                            elif call.wav_player is not None and call.audio_stream_port is not None:
                                try:
                                    call.wav_player.stopTransmit(call.audio_stream_port)
                                except Exception:
                                    pass
                            self._log.info(f"Inter-delay: {inter_delay}s")
                            if call.disconnected_event.wait(timeout=inter_delay):
                                self._log.info("Remote party hung up during inter-delay")
                                break
                            # Rewind the WAV player to the start before resuming
                            if call.wav_player is not None:
                                try:
                                    call.wav_player.setPos(0)
                                except Exception:
                                    pass
                            # Resume transmission for the next repeat
                            if call.wav_player is not None and call._audio_media is not None:
                                try:
                                    call.wav_player.startTransmit(call._audio_media)
                                except Exception:
                                    pass
                            if call.wav_player is not None and call.tx_stream_port is not None:
                                try:
                                    call.wav_player.startTransmit(call.tx_stream_port)
                                except Exception:
                                    pass
                            elif call.wav_player is not None and call.audio_stream_port is not None:
                                try:
                                    call.wav_player.startTransmit(call.audio_stream_port)
                                except Exception:
                                    pass

                    # Post-delay: keep WAV player connected so RTP buffers drain
                    if post_delay > 0 and not call.disconnected_event.is_set():
                        self._log.info(f"Post-delay: {post_delay}s")
                        if call.disconnected_event.wait(timeout=post_delay):
                            self._log.info("Remote party hung up during post-delay")
                finally:
                    call.stop_wav(_orphan_store=self._orphaned_ports)
                    call.stop_recording(_orphan_store=self._orphaned_ports)
                    call.stop_tx_recording(_orphan_store=self._orphaned_ports)
                    call.stop_audio_stream(_orphan_store=self._orphaned_ports)
                    call.stop_transcription(_orphan_store=self._orphaned_ports)
                    call.stop_tts_port(_orphan_store=self._orphaned_ports)
            else:
                # No WAV — wait for remote hangup
                try:
                    self._log.info("No WAV — waiting for remote hangup (Ctrl+C to end)")
                    call.disconnected_event.wait()
                finally:
                    call.stop_recording(_orphan_store=self._orphaned_ports)
                    call.stop_tx_recording(_orphan_store=self._orphaned_ports)
                    call.stop_audio_stream(_orphan_store=self._orphaned_ports)
                    call.stop_transcription(_orphan_store=self._orphaned_ports)
                    call.stop_tts_port(_orphan_store=self._orphaned_ports)

            # Post-call mix
            _mix_result_path: str | None = None
            if _recording.mix_mode != "none" and _recording.rx_path and _recording.tx_path:
                from sipstuff.audio import mix_wav_files

                _mix_out = _recording.mix_output_path
                if _mix_out is None:
                    suffix = "stereo" if _recording.mix_mode == "stereo" else "mix"
                    _mix_out = str(Path(str(_recording.rx_path)).with_suffix("")) + f"_{suffix}.wav"
                try:
                    mix_wav_files(
                        str(_recording.rx_path), str(_recording.tx_path), str(_mix_out), mode=_recording.mix_mode
                    )
                    _mix_result_path = str(_mix_out)
                except Exception as exc:
                    self._log.error(f"Mix failed: {exc}")

            # Post-delay (skip if remote already hung up, or if WAV path handled it above)
            if wav_info is None and not call.disconnected_event.is_set() and post_delay > 0:
                self._log.info(f"Post-delay: {post_delay}s")
                if call.disconnected_event.wait(timeout=post_delay):
                    self._log.info("Remote party hung up during post-delay")

            # Hang up (if still connected)
            if not call.disconnected_event.is_set():
                self._log.info("Call completed, hanging up")
                try:
                    call.hangup(pj.CallOpParam())
                except Exception:
                    pass
                call.disconnected_event.wait(timeout=5)

            # Stop live transcription thread and collect segments
            live_segments: list[dict[str, object]] = []
            if _stt_thread is not None:
                _stt_thread.stop()
                _stt_thread.join(timeout=10)
                live_segments = list(_stt_thread.segments)
                self._log.info(f"Live transcription: {len(live_segments)} segments collected")

            call_end = time.time()
            self.last_call_result = CallResult(
                success=True,
                call_start=call_start,
                call_end=call_end,
                call_duration=call_end - call_start,
                answered=True,
                disconnect_reason=call._disconnect_reason,
                live_transcript=live_segments,
                mix_output_path=_mix_result_path,
            )
            return True
        finally:
            # Clean up TTS temp file
            if _tts_temp_path:
                try:
                    os.unlink(_tts_temp_path)
                except OSError:
                    pass
            # Failsafe: if we exit make_call for any reason (exception, early
            # return) and the call is still active, send BYE so the remote
            # side doesn't stay connected indefinitely.
            if not call.disconnected_event.is_set():
                self._log.warning("Failsafe hangup — call still active on make_call exit")
                try:
                    call.hangup(pj.CallOpParam())
                except Exception:
                    pass
