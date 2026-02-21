"""PJSUA2 audio media ports — SilenceDetector, AudioStreamPort, TranscriptionPort, AudioPlayer."""

import array
import math
import os
import socket
import statistics
import threading
import time
from typing import TYPE_CHECKING, Any

import pjsua2 as pj
from loguru import logger

try:
    import sounddevice as sd

    SOUNDDEVICE_AVAILABLE: bool = True
except Exception:
    SOUNDDEVICE_AVAILABLE = False

from sipstuff.audio import WavInfo


def validate_audio_device(audio_device: int | str | None) -> int | str | None:
    """Validate and log the selected sounddevice audio device.

    Lists all available PortAudio devices, checks whether the requested
    device exists, and logs the resolved device name.

    Args:
        audio_device: Device index, device name substring, or ``None`` to use
            the system default output device.

    Returns:
        The same ``audio_device`` value passed in, unchanged, after validation.

    Raises:
        ImportError: If ``sounddevice`` is not installed.
        ValueError: If ``audio_device`` does not match any available output device.
    """
    if not SOUNDDEVICE_AVAILABLE:
        raise ImportError("sounddevice is required for --play-audio but not installed")

    devices = sd.query_devices()
    logger.info(f"Available audio devices:\n{devices}")

    if audio_device is None:
        default_idx = sd.default.device[1]  # output device index
        if default_idx is not None and default_idx >= 0:
            default_info = sd.query_devices(default_idx, "output")
            logger.info(f"Using default audio output device: [{default_idx}] {default_info['name']}")
        else:
            logger.warning("No default audio output device found")
        return audio_device

    try:
        info = sd.query_devices(audio_device, "output")
        logger.info(f"Using audio output device: [{audio_device}] {info['name']}")
        return audio_device
    except Exception as exc:
        raise ValueError(f"Audio device {audio_device!r} not found: {exc}\n" f"Available devices:\n{devices}") from exc


if TYPE_CHECKING:
    from sipstuff.audio import VADAudioBuffer, WavRecorder
    from sipstuff.sip_call import SipCalleeCall


class SilenceDetector(pj.AudioMediaPort):  # type: ignore[misc]
    """PJSUA2 audio port that monitors incoming RMS energy and signals when
    continuous silence exceeds a configurable duration.

    Attach to the call's audio media via ``startTransmit`` to receive remote-
    party audio frames.  The ``silence_event`` is set once the incoming RMS
    stays below ``threshold`` for ``duration`` seconds.

    Note:
        PJSUA2 SWIG bindings expose ``MediaFrame.buf`` as a ``pj.ByteVector``
        (C++ ``std::vector<unsigned char>``), **not** Python ``bytes``.
        ``array.frombytes()`` requires a bytes-like object, so an explicit
        ``bytes()`` conversion is needed.

    Args:
        duration: Required seconds of continuous silence (default: 1.0).
        threshold: RMS threshold below which audio is considered silence
            (16-bit PCM scale, default: 200).
    """

    def __init__(self, duration: float = 1.0, threshold: int = 200) -> None:
        super().__init__()
        self._duration = duration
        self._threshold = threshold
        self._silence_start: float | None = None
        self._last_log: float = 0.0
        self._rms_buf: list[int] = []
        self.silence_event = threading.Event()
        self._log = logger.bind(classname="SilenceDetector")

    def _flush_rms_stats(self, now: float, label: str) -> None:
        """Log buffered RMS stats (avg/median/stddev) and reset the buffer.

        Args:
            now: Current monotonic timestamp in seconds.
            label: Prefix string used in the log message (e.g. ``"Audio activity"``).
        """
        if not self._rms_buf:
            return
        avg = statistics.mean(self._rms_buf)
        med = statistics.median(self._rms_buf)
        std = statistics.stdev(self._rms_buf) if len(self._rms_buf) > 1 else 0.0
        self._log.info(f"{label} (n={len(self._rms_buf)}, avg={avg:.0f}, med={med:.0f}, std={std:.0f})")
        self._rms_buf.clear()
        self._last_log = now

    def onFrameReceived(self, frame: "pj.MediaFrame") -> None:  # noqa: N802
        """Called by PJSUA2 for every incoming audio frame (~20 ms).

        Computes the RMS of the frame, updates the silence timer, and sets
        ``silence_event`` once continuous silence exceeds the configured duration.

        Args:
            frame: PJSUA2 media frame whose ``buf`` is a SWIG ``ByteVector``.
        """
        if self.silence_event.is_set():
            return

        try:
            samples = array.array("h")
            samples.frombytes(bytes(frame.buf))  # ByteVector -> bytes for array.frombytes()
            if len(samples) == 0:
                return
            rms = math.isqrt(sum(s * s for s in samples) // len(samples))
        except Exception:
            return

        now = time.monotonic()
        self._rms_buf.append(rms)
        if rms < self._threshold:
            if self._silence_start is None:
                self._silence_start = now
            elif now - self._silence_start >= self._duration:
                self._flush_rms_stats(now, "Silence detected")
                self._log.info(f"Silence threshold reached ({self._duration}s, last_rms={rms})")
                self.silence_event.set()
        else:
            self._silence_start = None

        if now - self._last_log >= 0.5:
            # log not more often than 0.5s
            self._flush_rms_stats(now, "Audio activity")


class AudioStreamPort(pj.AudioMediaPort):  # type: ignore[misc]
    """PJSUA2 audio port that streams raw PCM frames to a Unix domain socket
    and/or plays them on the local sound device via ``sounddevice``.

    Supports two output sinks that can be used independently or together:

    * **Unix socket** (``socket_path``): connects as a client to an
      already-listening Unix socket (e.g. started via ``socat``).
    * **Local playback** (``play_audio=True``): opens a ``sounddevice``
      ``RawOutputStream`` and writes PCM frames directly — no socat,
      no aplay, no external process needed.

    Audio format: 16 kHz, 16-bit signed LE, mono — matching
    ``aplay -r 16000 -f S16_LE -c 1 -t raw``.

    Example — socket-based streaming::

        socat UNIX-LISTEN:/tmp/sip_audio.sock,fork EXEC:'aplay -r 16000 -f S16_LE -c 1 -t raw'

    Then pass ``--audio-socket /tmp/sip_audio.sock`` to the CLI.

    Example — direct local playback (simpler alternative)::

        python -m sipstuff.cli call --dest 1002 --wav test.wav --play-audio

    Both flags can be combined for simultaneous socket streaming and
    local playback.

    Args:
        socket_path: Filesystem path of the Unix domain socket to connect to,
            or ``None`` to disable socket streaming.
        play_audio: If ``True``, open a local ``sounddevice`` output stream
            for direct audio playback.
    """

    def __init__(
        self, socket_path: str | None = None, play_audio: bool = False, audio_device: int | str | None = None
    ) -> None:
        super().__init__()
        self._log = logger.bind(classname="AudioStreamPort")
        self._sock: socket.socket | None = None
        self._stream: Any = None

        if socket_path is not None:
            try:
                sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                sock.connect(socket_path)
                self._sock = sock
                self._log.info(f"Connected to audio socket: {socket_path}")
            except OSError as exc:
                self._log.warning(f"Cannot connect to audio socket {socket_path}: {exc} — frames will be discarded")

        if play_audio:
            if not SOUNDDEVICE_AVAILABLE:
                raise ImportError(
                    "sounddevice is required for --play-audio but not installed. "
                    "Install it with: pip install sounddevice"
                )
            self._stream = sd.RawOutputStream(samplerate=16000, channels=1, dtype="int16", device=audio_device)
            self._stream.start()
            self._log.info("Local audio playback started (sounddevice)")

    def onFrameReceived(self, frame: "pj.MediaFrame") -> None:  # noqa: N802
        """Called by PJSUA2 for every incoming audio frame (~20 ms).

        Converts the SWIG ``ByteVector`` to ``bytes`` once and writes to
        both sinks (socket and/or sounddevice stream).  Errors on each
        sink are handled independently.

        Args:
            frame: PJSUA2 media frame whose ``buf`` is a SWIG ``ByteVector``.
        """
        if self._sock is None and self._stream is None:
            return
        pcm_data = bytes(frame.buf)
        if self._sock is not None:
            try:
                self._sock.sendall(pcm_data)
            except OSError:
                self._log.debug("Audio socket disconnected — stopping socket stream")
                self._close_socket()
        if self._stream is not None:
            try:
                self._stream.write(pcm_data)
            except Exception as exc:
                self._log.debug(f"Sounddevice write failed ({exc}) — stopping playback stream")
                self._close_stream()

    def _close_socket(self) -> None:
        """Close the Unix socket.  Safe for multiple calls."""
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None

    def _close_stream(self) -> None:
        """Close the sounddevice output stream.  Safe for multiple calls."""
        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception:
                pass
            self._stream = None

    def close(self) -> None:
        """Close all output sinks.  Safe for multiple calls."""
        self._close_socket()
        self._close_stream()


class StereoMixer:
    """Combines two mono PCM streams (RX + TX) into a stereo output.

    RX is routed to the left channel, TX to the right channel.
    Output sinks: sounddevice stereo stream and/or Unix domain socket.

    Args:
        socket_path: Unix domain socket for stereo PCM streaming, or ``None``.
        play_audio: If ``True``, open a stereo sounddevice output stream.
    """

    def __init__(
        self, socket_path: str | None = None, play_audio: bool = False, audio_device: int | str | None = None
    ) -> None:
        self._log = logger.bind(classname="StereoMixer")
        self._lock = threading.Lock()
        self._rx_buf: bytes | None = None
        self._tx_buf: bytes | None = None
        self._sock: socket.socket | None = None
        self._stream: Any = None

        if socket_path is not None:
            try:
                sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                sock.connect(socket_path)
                self._sock = sock
                self._log.info(f"StereoMixer connected to audio socket: {socket_path}")
            except OSError as exc:
                self._log.warning(f"Cannot connect to audio socket {socket_path}: {exc} — frames will be discarded")

        if play_audio:
            if not SOUNDDEVICE_AVAILABLE:
                raise ImportError(
                    "sounddevice is required for --play-audio but not installed. "
                    "Install it with: pip install sounddevice"
                )
            self._stream = sd.RawOutputStream(samplerate=16000, channels=2, dtype="int16", device=audio_device)
            self._stream.start()
            self._log.info("Stereo local audio playback started (sounddevice)")

    def deposit_rx(self, pcm: bytes) -> None:
        """Deposit an RX (remote-party) mono frame and try to emit stereo.

        Args:
            pcm: Raw 16-bit signed LE mono PCM bytes for the receive channel.
        """
        with self._lock:
            self._rx_buf = pcm
            self._try_emit()

    def deposit_tx(self, pcm: bytes) -> None:
        """Deposit a TX (local) mono frame and try to emit stereo.

        Args:
            pcm: Raw 16-bit signed LE mono PCM bytes for the transmit channel.
        """
        with self._lock:
            self._tx_buf = pcm
            self._try_emit()

    def _try_emit(self) -> None:
        """Emit a stereo frame once both channels have arrived for the current tick.

        Waiting for both RX and TX prevents double-rate output: without this,
        each deposit call would independently emit a half-silence frame,
        producing two stereo frames per 20ms PJSIP tick and causing
        double-speed / distorted playback.
        """
        if self._rx_buf is None or self._tx_buf is None:
            return

        rx = self._rx_buf
        tx = self._tx_buf
        self._rx_buf = None
        self._tx_buf = None

        rx_samples = array.array("h")
        if rx is not None:
            rx_samples.frombytes(rx)

        tx_samples = array.array("h")
        if tx is not None:
            tx_samples.frombytes(tx)

        # Determine target length — use whichever side is available,
        # fall back to the other (at least one is non-empty).
        target_len = max(len(rx_samples), len(tx_samples))

        # Pad with silence to target length
        while len(rx_samples) < target_len:
            rx_samples.append(0)
        while len(tx_samples) < target_len:
            tx_samples.append(0)

        stereo = array.array("h")
        for i in range(target_len):
            stereo.append(rx_samples[i])
            stereo.append(tx_samples[i])

        stereo_bytes = stereo.tobytes()

        if self._sock is not None:
            try:
                self._sock.sendall(stereo_bytes)
            except OSError:
                self._log.debug("Audio socket disconnected — stopping socket stream")
                self._close_socket()

        if self._stream is not None:
            try:
                self._stream.write(stereo_bytes)
            except Exception as exc:
                self._log.debug(f"Sounddevice write failed ({exc}) — stopping playback stream")
                self._close_stream()

    def _close_socket(self) -> None:
        """Close the Unix socket. Safe for multiple calls."""
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None

    def _close_stream(self) -> None:
        """Close the sounddevice output stream. Safe for multiple calls."""
        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception:
                pass
            self._stream = None

    def close(self) -> None:
        """Close all output sinks. Thread-safe; safe for multiple calls.

        Nulls references under the lock so concurrent ``_try_emit`` calls
        see ``None`` and drop frames, then closes the actual I/O objects
        outside the lock.
        """
        with self._lock:
            stream = self._stream
            self._stream = None
            sock = self._sock
            self._sock = None
        if stream is not None:
            try:
                stream.stop()
                stream.close()
            except Exception:
                pass
        if sock is not None:
            try:
                sock.close()
            except Exception:
                pass


class RxStreamPort(pj.AudioMediaPort):  # type: ignore[misc]
    """PJSUA2 audio port that forwards RX (remote-party) frames to a ``StereoMixer``.

    Args:
        mixer: The ``StereoMixer`` to deposit RX frames into.
    """

    def __init__(self, mixer: StereoMixer) -> None:
        super().__init__()
        self._mixer = mixer
        self._frame_count: int = 0
        self._log = logger.bind(classname="RxStreamPort")

    def onFrameReceived(self, frame: "pj.MediaFrame") -> None:  # noqa: N802
        """Called by PJSUA2 for every incoming audio frame (~20 ms).

        Args:
            frame: PJSUA2 media frame whose ``buf`` is a SWIG ``ByteVector``.
        """
        try:
            self._mixer.deposit_rx(bytes(frame.buf))
            self._frame_count += 1
            if self._frame_count % 250 == 0:
                self._log.debug(f"RxStreamPort: {self._frame_count} frames deposited")
        except Exception as exc:
            self._log.error(f"RxStreamPort.onFrameReceived error: {exc}")

    def close(self) -> None:
        """No-op for orphan-pattern compatibility."""


class TxStreamPort(pj.AudioMediaPort):  # type: ignore[misc]
    """PJSUA2 audio port that forwards TX (local) frames to a ``StereoMixer``.

    Args:
        mixer: The ``StereoMixer`` to deposit TX frames into.
    """

    def __init__(self, mixer: StereoMixer) -> None:
        super().__init__()
        self._mixer = mixer

    def onFrameReceived(self, frame: "pj.MediaFrame") -> None:  # noqa: N802
        """Called by PJSUA2 for every incoming audio frame (~20 ms).

        Args:
            frame: PJSUA2 media frame whose ``buf`` is a SWIG ``ByteVector``.
        """
        self._mixer.deposit_tx(bytes(frame.buf))

    def close(self) -> None:
        """No-op for orphan-pattern compatibility."""


class RecordingPort(pj.AudioMediaPort):  # type: ignore[misc]
    """PJSUA2 audio port that writes received frames to a ``WavRecorder``.

    Attach to the call's audio media via ``startTransmit`` to capture the
    incoming audio stream directly to a WAV file.

    Args:
        wav_recorder: Open ``WavRecorder`` instance that frames are written to.
    """

    def __init__(self, wav_recorder: "WavRecorder") -> None:
        super().__init__()
        self.wav_recorder = wav_recorder

    def onFrameReceived(self, frame: "pj.MediaFrame") -> None:  # noqa: N802
        """Called by PJSUA2 for every incoming audio frame (~20 ms).

        Writes the PCM payload to the WAV recorder when the frame carries
        audio data.

        Args:
            frame: PJSUA2 media frame whose ``buf`` is a SWIG ``ByteVector``.
        """
        if frame.type == pj.PJMEDIA_FRAME_TYPE_AUDIO and frame.size > 0:
            self.wav_recorder.write_frames(bytes(frame.buf))

    def close(self) -> None:
        """No-op for orphan-pattern compatibility."""


class TranscriptionPort(pj.AudioMediaPort):  # type: ignore[misc]
    """PJSUA2 audio port that feeds incoming audio frames into a VADAudioBuffer.

    Attach to the call's audio media via ``startTransmit`` to receive remote-
    party audio frames.  Each frame is converted from the SWIG ``ByteVector``
    to ``bytes`` and forwarded to the VAD buffer for chunking and subsequent
    transcription.

    Args:
        audio_buffer: VAD-enabled audio buffer to feed frames into.
    """

    def __init__(self, audio_buffer: "VADAudioBuffer", wav_recorder: "WavRecorder | None" = None) -> None:
        super().__init__()
        self.audio_buffer = audio_buffer
        self.wav_recorder = wav_recorder

    def onFrameReceived(self, frame: "pj.MediaFrame") -> None:  # noqa: N802
        """Called by PJSUA2 for every incoming audio frame (~20 ms).

        Forwards audio frames to the VAD buffer and, if a recorder is attached,
        also writes the raw PCM to the WAV file.

        Args:
            frame: PJSUA2 media frame whose ``buf`` is a SWIG ``ByteVector``.
        """
        if frame.type == pj.PJMEDIA_FRAME_TYPE_AUDIO and frame.size > 0:
            pcm_bytes = bytes(frame.buf)
            self.audio_buffer.add_frames(pcm_bytes)
            if self.wav_recorder:
                self.wav_recorder.write_frames(pcm_bytes)

    def close(self) -> None:
        """No-op for orphan-pattern compatibility."""


from sipstuff.sipconfig import PauseConfig, PlaybackSequence, TtsConfig, TtsPlayConfig, WavPlayConfig


class AudioPlayer:
    """Plays a ``PlaybackSequence`` into an active PJSUA2 call.

    Handles ``WavPlayConfig``, ``TtsPlayConfig`` (generates WAV on-the-fly),
    and ``PauseConfig`` segments.  TTS temp files are tracked and removed by
    ``cleanup_tts()`` or ``stop_all()``.

    Args:
        sequence: The ordered list of playback segments to execute.
        default_tts_config: Fallback TTS configuration used when a
            ``TtsPlayConfig`` segment does not specify its own ``tts_config``.
    """

    def __init__(self, sequence: PlaybackSequence, default_tts_config: TtsConfig | None = None):
        self._log = logger.bind(classname="AudioPlayer")
        self.sequence = sequence
        self._players: list[Any] = []
        self._default_tts_config = default_tts_config
        self._tts_temp_paths: list[str] = []

    def play_sequence(
        self,
        call: "SipCalleeCall",
        media_idx: int,
        disconnected: threading.Event,
        extra_targets: list[Any] | None = None,
    ) -> None:
        """Play all segments in the sequence into the call's audio media.

        Iterates over each segment in order.  ``PauseConfig`` segments sleep
        using ``disconnected.wait()`` so the pause is interrupted immediately
        on hangup.  ``TtsPlayConfig`` segments generate a temporary WAV file
        before playback.  Returns early if the call disconnects at any point.

        Args:
            call: The active callee call whose audio media receives the audio.
            media_idx: Index passed to ``call.getAudioMedia()`` to obtain the
                conference bridge sink.
            disconnected: Event that is set when the call ends; used to
                abort playback and pauses promptly.
            extra_targets: Additional ``AudioMedia`` targets that each player
                also transmits to (e.g. a recording port).
        """
        for segment in self.sequence.segments:
            if disconnected.is_set():
                return

            if isinstance(segment, PauseConfig):
                if segment.duration > 0:
                    self._log.info(f"Pause: {segment.duration}s")
                    if disconnected.wait(timeout=segment.duration):
                        return
                continue

            # WavPlayConfig or TtsPlayConfig — both have pause_before
            if segment.pause_before > 0:
                self._log.info(f"Pause: {segment.pause_before}s")
                if disconnected.wait(timeout=segment.pause_before):
                    return

            # Resolve to a WAV path
            if isinstance(segment, TtsPlayConfig):
                wav_path = self._generate_tts_wav(segment)
                if wav_path is None:
                    continue  # error logged, skip segment
            else:
                wav_path = segment.wav_path

            # Play WAV
            try:
                duration = WavInfo(wav_path).duration
                player = pj.AudioMediaPlayer()
                player.createPlayer(wav_path, pj.PJMEDIA_FILE_NO_LOOP)
                self._players.append(player)

                aud_med = call.getAudioMedia(media_idx)
                player.startTransmit(aud_med)
                for target in extra_targets or []:
                    player.startTransmit(target)
                self._log.info(f"Playing: {wav_path} ({duration:.1f}s)")

                if disconnected.wait(timeout=duration + 0.1):
                    return

            except Exception as e:
                self._log.error(f"Playback error: {e}")
                return

    def _generate_tts_wav(self, segment: TtsPlayConfig) -> str | None:
        """Generate a temporary WAV file for a TTS playback segment.

        Uses the segment's own ``tts_config`` if present, falling back to
        ``default_tts_config``.  The generated path is appended to
        ``_tts_temp_paths`` for later cleanup.

        Args:
            segment: The TTS segment whose ``tts_text`` and optional
                ``tts_config`` drive synthesis.

        Returns:
            Absolute path of the generated WAV file, or ``None`` if
            no TTS config is available or synthesis fails.
        """
        tts_cfg = segment.tts_config or self._default_tts_config
        if tts_cfg is None:
            self._log.error("TTS segment has no tts_config and no default — skipping")
            return None
        try:
            from sipstuff.tts import generate_wav as _generate_wav

            tmp_path = _generate_wav(
                text=segment.tts_text,
                model=tts_cfg.model,
                sample_rate=tts_cfg.sample_rate or 16000,
                data_dir=tts_cfg.data_dir,
            )
            self._tts_temp_paths.append(str(tmp_path))
            return str(tmp_path)
        except Exception as e:
            self._log.error(f"TTS generation failed: {e}")
            return None

    def cleanup_tts(self) -> None:
        """Remove temporary TTS WAV files."""
        for path in self._tts_temp_paths:
            try:
                os.unlink(path)
            except OSError:
                pass
        self._tts_temp_paths.clear()

    def stop_all(self) -> None:
        """Stop all active players and remove temporary TTS WAV files.

        Attempts to stop transmission for each player, clears the player list,
        and then calls ``cleanup_tts()`` to delete any temporary WAV files
        created during TTS segments.
        """
        for player in self._players:
            try:
                player.stopTransmit(pj.Endpoint.instance().audDevManager().getPlaybackDevMedia())
            except Exception:
                pass
        self._players.clear()
        self.cleanup_tts()
