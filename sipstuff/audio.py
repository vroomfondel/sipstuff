"""Shared audio utilities for the sipstuff package.

Provides functions and classes for:
- Playing WAV files through a local sounddevice output.
- Resampling audio via linear interpolation.
- Converting arbitrary WAV files to 16 kHz / mono / 16-bit PCM.
- Mixing two separate WAV tracks (RX and TX) into a single output file.
- Inspecting WAV file metadata (``WavInfo``).
- Writing PCM frames to a WAV file in a thread-safe manner (``WavRecorder``).
- Voice-activity-detection buffering for chunked live transcription (``VADAudioBuffer``).
"""

import struct
import threading
import wave
from pathlib import Path

import numpy as np
import numpy.typing as npt
from loguru import logger

from sipstuff.sip_types import SipCallError

log = logger.bind(classname="Audio")


def play_wav(wav_path: str | Path, audio_device: int | str | None = None) -> None:
    """Play a WAV file through speakers using sounddevice.

    Args:
        wav_path: Path to the WAV file to play.
        audio_device: Sounddevice output device index (int) or name substring.
            ``None`` uses the system default output device.

    Raises:
        ImportError: If ``sounddevice`` or ``soundfile`` are not installed.
        FileNotFoundError: If *wav_path* does not exist.
    """
    import sounddevice as sd
    import soundfile as sf

    wav_path = Path(wav_path)
    if not wav_path.is_file():
        raise FileNotFoundError(f"WAV file not found: {wav_path}")

    data, samplerate = sf.read(str(wav_path), dtype="float32")

    # Ensure 2-D array (mono files come back as 1-D)
    if data.ndim == 1:
        data = data.reshape(-1, 1)

    finished = threading.Event()
    position = 0

    def callback(outdata: np.ndarray, frames: int, time_info: object, status: sd.CallbackFlags) -> None:
        """Sounddevice output-stream callback; fills *outdata* from the WAV array.

        Args:
            outdata: Buffer to fill with the next *frames* samples.
            frames: Number of frames requested by the stream.
            time_info: Timing information provided by sounddevice (unused).
            status: Stream status flags provided by sounddevice (unused).

        Raises:
            sd.CallbackStop: When the end of the WAV data is reached.
        """
        nonlocal position
        end = position + frames
        chunk = data[position:end]
        if len(chunk) < frames:
            outdata[: len(chunk)] = chunk
            outdata[len(chunk) :] = 0
            finished.set()
            raise sd.CallbackStop
        outdata[:] = chunk
        position = end

    stream = sd.OutputStream(
        samplerate=samplerate,
        channels=data.shape[1],
        dtype="float32",
        callback=callback,
        device=audio_device,
    )
    with stream:
        finished.wait()

    log.info(f"Played {wav_path.name} ({len(data) / samplerate:.1f}s)")


def resample_linear(samples: npt.NDArray[np.floating], source_rate: int, target_rate: int) -> npt.NDArray[np.float32]:
    """Resample a 1-D audio signal via linear interpolation.

    Treats the source audio as a function ``f(i) → amplitude`` at integer
    sample indices.  To change the sample rate we compute a new set of
    evenly-spaced positions over the same duration and read off amplitudes
    via ``np.interp`` (linear interpolation between the two nearest
    original samples).

    This is sufficient for speech audio.  Music or signals with significant
    high-frequency content would benefit from a polyphase/sinc resampler.

    Args:
        samples: 1-D array of audio samples (any float dtype).
        source_rate: Original sample rate in Hz.
        target_rate: Desired sample rate in Hz.

    Returns:
        1-D float32 array resampled to *target_rate*.
    """
    if source_rate == target_rate:
        return samples.astype(np.float32)

    src_length = len(samples)
    # New sample count = original count × (target_rate / source_rate), preserving duration
    dst_length = int(src_length * target_rate / source_rate)
    # Original sample positions: [0, 1, 2, ..., src_length-1]
    src_indices = np.arange(src_length)
    # Target sample positions: dst_length points spread evenly over [0, src_length-1]
    dst_indices = np.linspace(0, src_length - 1, dst_length)
    # For each dst index, linearly interpolate between the two nearest src samples
    return np.interp(dst_indices, src_indices, samples).astype(np.float32)  # type: ignore


def ensure_wav_16k_mono(input_path: str) -> str:
    """Return a path to a 16 kHz / mono / 16-bit PCM copy of *input_path*.

    If the file already matches the target format it is returned as-is.
    Otherwise a ``_16k.wav`` sibling is written next to the original.

    Supports 8-bit unsigned, 16-bit signed, and 32-bit signed PCM input.
    Stereo is downmixed by averaging the two channels.  Multi-channel (> 2)
    audio is decimated by picking every *n*-th sample.  Resampling uses
    ``resample_linear()``.

    Args:
        input_path: Path to the source WAV file.

    Returns:
        Path to a WAV file that is 16 kHz, mono, 16-bit PCM.  This is
        either *input_path* itself (when no conversion was needed) or a
        new ``*_16k.wav`` file written alongside the original.
    """
    with wave.open(input_path, "rb") as wf:
        rate = wf.getframerate()
        channels = wf.getnchannels()
        sampwidth = wf.getsampwidth()
        frames = wf.readframes(wf.getnframes())

    if rate == 16000 and channels == 1 and sampwidth == 2:
        log.info(f"WAV bereits im richtigen Format: {input_path}")
        return input_path

    log.info(f"Konvertiere WAV: {rate}Hz {channels}ch {sampwidth * 8}bit -> 16000Hz mono 16bit")

    # Decode samples
    if sampwidth == 1:
        fmt = f"<{len(frames)}B"
        samples = [(s - 128) * 256 for s in struct.unpack(fmt, frames)]
    elif sampwidth == 2:
        fmt = f"<{len(frames) // 2}h"
        samples = list(struct.unpack(fmt, frames))
    elif sampwidth == 4:
        fmt = f"<{len(frames) // 4}i"
        samples = [s >> 16 for s in struct.unpack(fmt, frames)]
    else:
        log.warning(f"Unbekannte Sample-Breite: {sampwidth}, verwende Originaldatei")
        return input_path

    # Stereo -> Mono
    if channels == 2:
        mono: list[int] = []
        for i in range(0, len(samples), 2):
            if i + 1 < len(samples):
                mono.append((samples[i] + samples[i + 1]) // 2)
            else:
                mono.append(samples[i])
        samples = mono
    elif channels > 2:
        samples = samples[::channels]

    # Resample
    if rate != 16000:
        arr = resample_linear(np.array(samples, dtype=np.float32), rate, 16000)
        samples = np.clip(arr, -32768, 32767).astype(np.int16).tolist()

    converted_path = input_path.rsplit(".", 1)[0] + "_16k.wav"
    with wave.open(converted_path, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(16000)
        wf.writeframes(struct.pack(f"<{len(samples)}h", *samples))

    log.info(f"Konvertierte WAV-Datei: {converted_path}")
    return converted_path


def mix_wav_files(rx_path: str, tx_path: str, output_path: str, mode: str = "mono") -> None:
    """Mix two WAV files (RX and TX) into a single output file.

    Args:
        rx_path: Path to the RX (remote party) WAV file.
        tx_path: Path to the TX (local microphone) WAV file.
        output_path: Path to write the mixed output WAV file.
        mode: ``"mono"`` adds both tracks to a single channel (normalised),
            ``"stereo"`` places RX on the left and TX on the right channel.
    """
    with wave.open(rx_path, "rb") as rx_wav:
        rx_params = rx_wav.getparams()
        rx_frames = rx_wav.readframes(rx_params.nframes)

    with wave.open(tx_path, "rb") as tx_wav:
        tx_frames = tx_wav.readframes(tx_wav.getnframes())

    rx_samples = np.frombuffer(rx_frames, dtype=np.int16).astype(np.float32)
    tx_samples = np.frombuffer(tx_frames, dtype=np.int16).astype(np.float32)

    # Pad to equal length
    max_len = max(len(rx_samples), len(tx_samples))
    if len(rx_samples) < max_len:
        rx_samples = np.pad(rx_samples, (0, max_len - len(rx_samples)))
    if len(tx_samples) < max_len:
        tx_samples = np.pad(tx_samples, (0, max_len - len(tx_samples)))

    if mode == "stereo":
        # Interleave: [L, R, L, R, ...]
        stereo = np.empty(max_len * 2, dtype=np.float32)
        stereo[0::2] = rx_samples  # Left = remote party (RX)
        stereo[1::2] = tx_samples  # Right = local mic (TX)
        out_samples = np.clip(stereo, -32768, 32767).astype(np.int16)
        channels = 2
    else:  # mono
        mixed = rx_samples + tx_samples
        peak = float(np.max(np.abs(mixed)))
        if peak > 32767:
            mixed = mixed * (32767.0 / peak)
        out_samples = np.clip(mixed, -32768, 32767).astype(np.int16)
        channels = 1

    with wave.open(output_path, "wb") as out_wav:
        out_wav.setnchannels(channels)
        out_wav.setsampwidth(2)
        out_wav.setframerate(rx_params.framerate)
        out_wav.writeframes(out_samples.tobytes())

    duration = max_len / rx_params.framerate
    ch_label = "Stereo" if mode == "stereo" else "Mono"
    log.info(f"{ch_label} mix saved: {output_path} ({duration:.1f}s)")


class WavInfo:
    """WAV file metadata extracted via the ``wave`` module.

    Reads channel count, sample width, framerate, frame count, and
    computed duration on construction.

    Args:
        path: Path to the WAV file.

    Raises:
        SipCallError: If the file does not exist or cannot be parsed.

    Attributes:
        path: Resolved absolute path to the WAV file.
        channels: Number of audio channels.
        sample_width: Sample width in bytes (2 = 16-bit).
        framerate: Sample rate in Hz.
        n_frames: Total number of audio frames.
        duration: Duration in seconds.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).resolve()
        if not self.path.is_file():
            raise SipCallError(f"WAV file not found: {self.path}")

        try:
            with wave.open(str(self.path), "rb") as wf:
                self.channels: int = wf.getnchannels()
                self.sample_width: int = wf.getsampwidth()
                self.framerate: int = wf.getframerate()
                self.n_frames: int = wf.getnframes()
                self.duration: float = self.n_frames / self.framerate if self.framerate else 0.0
        except wave.Error as exc:
            raise SipCallError(f"Cannot read WAV file {self.path}: {exc}") from exc

    def validate(self) -> None:
        """Log warnings for non-standard WAV formats without blocking playback.

        Warns on non-16-bit samples, stereo, or unusual sample rates.
        Always logs a summary line with file name, duration, and format.
        """
        if self.sample_width != 2:
            logger.warning(f"WAV sample width is {self.sample_width * 8}-bit, expected 16-bit PCM")
        if self.channels != 1:
            logger.warning(f"WAV has {self.channels} channels, expected mono")
        if self.framerate not in (8000, 16000, 44100, 48000):
            logger.warning(f"WAV sample rate is {self.framerate} Hz, typical SIP rates: 8000 or 16000 Hz")
        logger.info(
            f"WAV: {self.path.name} — {self.duration:.1f}s, {self.framerate}Hz, {self.channels}ch, {self.sample_width * 8}bit"
        )


class WavRecorder:
    """Writes all received PCM frames to a WAV file in parallel.

    Thread-safe — can be fed from multiple threads.
    """

    def __init__(self, filepath: str, sample_rate: int = 16000, channels: int = 1, sample_width: int = 2):
        """Open *filepath* for writing and configure WAV parameters.

        Args:
            filepath: Destination path for the WAV file.
            sample_rate: Sample rate in Hz (default 16000).
            channels: Number of audio channels (default 1 = mono).
            sample_width: Sample width in bytes (default 2 = 16-bit).
        """
        self._log = logger.bind(classname="WavRecorder")
        self.filepath = filepath
        self.sample_rate = sample_rate
        self.channels = channels
        self.sample_width = sample_width
        self.lock = threading.Lock()
        self._closed = False
        self.total_samples = 0

        self.wav_file = wave.open(filepath, "wb")
        self.wav_file.setnchannels(channels)
        self.wav_file.setsampwidth(sample_width)
        self.wav_file.setframerate(sample_rate)

    def write_frames(self, pcm_bytes: bytes) -> None:
        """Write raw PCM bytes (16-bit signed LE) to the WAV file.

        Args:
            pcm_bytes: Raw PCM data in 16-bit signed little-endian format.
        """
        with self.lock:
            if self._closed:
                return
            self.wav_file.writeframes(pcm_bytes)
            self.total_samples += len(pcm_bytes) // (self.sample_width * self.channels)

    def close(self) -> None:
        """Close the WAV file cleanly."""
        with self.lock:
            self._closed = True
            self.wav_file.close()
        duration = self.total_samples / self.sample_rate
        self._log.info(f"Gespeichert: {self.filepath} ({duration:.1f}s)")

    @property
    def duration(self) -> float:
        """Current recorded duration in seconds.

        Returns:
            Elapsed recording time based on the number of samples written
            and the configured sample rate.
        """
        return self.total_samples / self.sample_rate


class VADAudioBuffer:
    """Thread-safe audio buffer with voice activity detection.

    Signals "chunk ready" when:
      1) At least ``silence_trigger_sec`` seconds of silence after speech
      2) OR ``max_duration_sec`` exceeded
    Only returns chunks >= ``min_duration_sec``.
    """

    def __init__(
        self,
        sample_rate: int = 16000,
        silence_threshold: float = 0.01,
        silence_trigger_sec: float = 0.3,
        max_duration_sec: float = 5.0,
        min_duration_sec: float = 0.5,
    ):
        """Initialise the VAD buffer with detection parameters.

        Args:
            sample_rate: Sample rate of incoming PCM audio in Hz (default 16000).
            silence_threshold: RMS amplitude below which a 10 ms window is
                considered silent (default 0.01, range 0.0–1.0 normalised).
            silence_trigger_sec: Seconds of continuous silence after speech
                required to trigger a chunk flush (default 0.3).
            max_duration_sec: Maximum chunk duration in seconds before a
                forced flush regardless of silence (default 5.0).
            min_duration_sec: Minimum chunk duration in seconds; chunks
                shorter than this are discarded (default 0.5).
        """
        self.sample_rate = sample_rate
        self.silence_threshold = silence_threshold
        self.silence_trigger_samples = int(sample_rate * silence_trigger_sec)
        self.max_samples = int(sample_rate * max_duration_sec)
        self.min_samples = int(sample_rate * min_duration_sec)

        self.lock = threading.Lock()
        self.chunk_ready = threading.Event()

        # Current buffer
        self.buffer = np.array([], dtype=np.float32)

        # Silence tracking
        self.silence_counter = 0  # consecutive silence samples
        self.has_speech = False  # whether speech was detected in this chunk

        # Absolute position (in samples since start)
        self.absolute_position = 0  # start of current buffer
        self.total_samples_received = 0

        # Ready chunks: (audio, abs_start_sec, abs_end_sec)
        self.ready_chunks: list[tuple[np.ndarray, float, float]] = []

    def add_frames(self, pcm_bytes: bytes) -> None:
        """Add PCM 16-bit signed LE frames and run VAD analysis.

        Appends *pcm_bytes* to the internal buffer, runs RMS-based VAD on
        the new samples, and checks whether a chunk should be flushed.

        Args:
            pcm_bytes: Raw audio data in 16-bit signed little-endian format.
        """
        samples = np.frombuffer(pcm_bytes, dtype=np.int16).astype(np.float32) / 32768.0

        with self.lock:
            self.buffer = np.concatenate([self.buffer, samples])
            self.total_samples_received += len(samples)
            self._analyze_vad(samples)
            self._check_flush()

    def _analyze_vad(self, new_samples: np.ndarray) -> None:
        """Run RMS-based voice activity detection on newly received samples.

        Processes *new_samples* in 10 ms windows.  Windows whose RMS falls
        below ``silence_threshold`` increment the silence counter; windows
        above it reset the counter and set ``has_speech`` to ``True``.

        Args:
            new_samples: Normalised float32 audio samples (range −1.0 to 1.0)
                for the current frame.
        """
        window_size = self.sample_rate // 100  # 10ms at 16kHz = 160 samples

        for i in range(0, len(new_samples), window_size):
            window = new_samples[i : i + window_size]
            if len(window) < window_size // 2:
                continue

            rms = float(np.sqrt(np.mean(window**2)))

            if rms < self.silence_threshold:
                self.silence_counter += len(window)
            else:
                self.silence_counter = 0
                self.has_speech = True

    def _check_flush(self) -> None:
        """Check whether the current buffer should be flushed as a ready chunk.

        A flush is triggered when either:
        - Speech was detected and silence has lasted at least
          ``silence_trigger_samples`` samples and the buffer meets the
          minimum duration (``min_samples``); or
        - The buffer has reached the maximum duration (``max_samples``).

        On a silence-triggered flush, trailing silence is trimmed before the
        chunk is appended to ``ready_chunks``.  The buffer and all tracking
        counters are reset afterwards.
        """
        buf_len = len(self.buffer)

        should_flush = False
        reason = ""

        # Condition 1: silence after speech
        if self.has_speech and self.silence_counter >= self.silence_trigger_samples and buf_len >= self.min_samples:
            should_flush = True
            reason = "silence"

        # Condition 2: max duration reached
        elif buf_len >= self.max_samples:
            should_flush = True
            reason = "max_duration"

        if should_flush:
            # On silence trigger: trim trailing silence
            if reason == "silence" and self.silence_counter < buf_len:
                cut_point = buf_len - self.silence_counter
                chunk = self.buffer[:cut_point].copy()
            else:
                chunk = self.buffer.copy()

            # Calculate absolute timecodes
            chunk_start_sec = self.absolute_position / self.sample_rate
            chunk_end_sec = (self.absolute_position + len(chunk)) / self.sample_rate

            # Only queue chunks with enough content
            if len(chunk) >= self.min_samples:
                self.ready_chunks.append((chunk, chunk_start_sec, chunk_end_sec))
                self.chunk_ready.set()

            # Reset buffer, advance absolute position
            self.absolute_position = self.total_samples_received
            self.buffer = np.array([], dtype=np.float32)
            self.silence_counter = 0
            self.has_speech = False

    def get_chunk(self, timeout: float = 0.5) -> tuple[np.ndarray, float, float] | None:
        """Wait for a ready chunk and return it.

        Blocks until a flushed chunk is available or the timeout expires.
        When multiple chunks have been queued, they are returned one at a time
        in FIFO order.

        Args:
            timeout: Maximum seconds to wait for a chunk (default 0.5).

        Returns:
            A 3-tuple ``(audio, abs_start_sec, abs_end_sec)`` where *audio*
            is a float32 numpy array of normalised samples, *abs_start_sec*
            is the chunk's start time relative to the beginning of the stream,
            and *abs_end_sec* is the corresponding end time.  Returns ``None``
            if no chunk becomes available within *timeout* seconds.
        """
        if self.chunk_ready.wait(timeout=timeout):
            with self.lock:
                if self.ready_chunks:
                    item = self.ready_chunks.pop(0)
                    if not self.ready_chunks:
                        self.chunk_ready.clear()
                    return item
        return None

    def flush_remaining(self) -> tuple[np.ndarray, float, float] | None:
        """Return any remaining buffered data at end of stream.

        Should be called once after the audio source has stopped producing
        frames to retrieve any partial chunk that has not yet met the normal
        flush conditions.  The internal buffer is cleared after the call.

        Returns:
            A 3-tuple ``(audio, abs_start_sec, abs_end_sec)`` with the same
            semantics as ``get_chunk()``, or ``None`` if the remaining buffer
            is shorter than ``min_samples``.
        """
        with self.lock:
            if len(self.buffer) >= self.min_samples:
                chunk = self.buffer.copy()
                chunk_start = self.absolute_position / self.sample_rate
                chunk_end = (self.absolute_position + len(chunk)) / self.sample_rate
                self.buffer = np.array([], dtype=np.float32)
                return (chunk, chunk_start, chunk_end)
        return None
