"""
Live TTS streaming into PJSIP calls using a producer-consumer pattern.

Provides the two core building blocks for real-time TTS streaming in SIP calls:

- ``PiperTTSProducer``: Producer thread that synthesizes text chunk-by-chunk
  via the Piper Python API and writes raw PCM data into a shared queue.
- ``TTSMediaPort``: PJSUA2 ``AudioMediaPort`` consumer that drains the queue
  every 20 ms and feeds PCM frames to the PJSIP conference bridge.
- ``interactive_console``: Helper that reads text from stdin and forwards it
  to a ``PiperTTSProducer`` for live speaking during an active call.
"""

import os
import readline
import struct
import sys
import threading
from queue import Empty, Queue
from typing import Optional

import numpy as np
from loguru import logger
from piper import PiperVoice

from sipstuff.audio import resample_linear
from sipstuff.sipconfig import TtsConfig

try:
    import pjsua2 as pj

    PJSUA2_AVAILABLE = True
except ImportError:
    pj = None
    PJSUA2_AVAILABLE = False


# ============================================================
# Konfiguration
# ============================================================

# PJSIP Audio-Parameter (müssen zum MediaPort passen)
CLOCK_RATE = 16000  # 16 kHz
SAMPLES_PER_FRAME = 320  # 20ms bei 16kHz
BITS_PER_SAMPLE = 16
CHANNEL_COUNT = 1


# ============================================================
# TTS Producer – Piper in eigenem Thread
# ============================================================


class PiperTTSProducer:
    """Producer thread that synthesizes text via the Piper Python API and enqueues PCM chunks.

    Text requests submitted via ``speak()`` are processed sequentially in a
    dedicated background thread.  Each synthesized utterance is split into
    20 ms PCM chunks and written to ``audio_queue``, followed by a ``b"__EOS__"``
    end-of-speech sentinel that the consumer can use to insert pauses.

    Example:
        producer = PiperTTSProducer(tts_config, audio_queue)
        producer.start()
        producer.speak("Hello world.")   # non-blocking
        producer.speak("Another line.") # queued, processed in order
        producer.stop()
    """

    def __init__(
        self,
        tts_config: TtsConfig,
        audio_queue: "Queue[bytes]",
        target_rate: int = CLOCK_RATE,
    ):
        """Initialize the producer.

        Args:
            tts_config: TTS configuration with model name, data directory, and
                CUDA flag.  The model is resolved and loaded via
                ``load_tts_model()`` when ``start()`` is called.
            audio_queue: Shared queue into which synthesized PCM chunks are placed.
            target_rate: Target PCM sample rate in Hz.  Audio is resampled to
                this rate when the Piper model's native rate differs.
        """
        self._tts_config = tts_config
        self.audio_queue = audio_queue
        self.target_rate = target_rate

        self.text_queue: "Queue[str | None]" = Queue()
        self._thread: Optional[threading.Thread] = None
        self._running = False
        self._voice: PiperVoice | None = None
        self._log = logger.bind(classname="PiperTTSProducer")

    def start(self) -> None:
        """Load the Piper voice model and start the producer thread."""
        from sipstuff.tts.tts import load_tts_model

        info = load_tts_model(self._tts_config)
        self._voice = info.voice

        self._log.info(f"Piper-Modell: {info.model_path}")

        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True, name="TTS-Producer")
        self._thread.start()
        self._log.info("TTS-Producer-Thread gestartet")

    def speak(self, text: str) -> None:
        """Enqueue a text string for synthesis.

        The call returns immediately; synthesis happens in the background thread.
        Empty or whitespace-only strings are silently ignored.

        Args:
            text: The text to synthesize and stream into the call.
        """
        if not text.strip():
            return
        self._log.info(f'TTS-Auftrag: "{text}"')
        self.text_queue.put(text)

    def stop(self) -> None:
        """Signal the producer thread to stop and wait for it to finish."""
        self._running = False
        self.text_queue.put(None)  # Sentinel
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5)
        self._log.info("TTS-Producer gestoppt")

    def _run(self) -> None:
        """Main loop of the producer thread; dequeues text items and synthesizes them."""
        while self._running:
            try:
                text = self.text_queue.get(timeout=0.5)
            except Empty:
                continue

            if text is None:  # Sentinel -> beenden
                break

            try:
                self._synthesize_and_enqueue(text)
            except Exception as e:
                self._log.error(f"TTS-Fehler: {e}")

    def _synthesize_and_enqueue(self, text: str) -> None:
        """Synthesize one text utterance and push PCM chunks onto the audio queue.

        Uses the Piper Python API to synthesize text directly, iterating over
        ``AudioChunk`` objects.  Raw PCM bytes are extracted, resampled to
        ``target_rate`` when the model's native rate differs, split into 20 ms
        frames (``SAMPLES_PER_FRAME`` samples each, zero-padded if the last
        chunk is short), and enqueued as raw little-endian 16-bit PCM bytes.
        A ``b"__EOS__"`` sentinel is appended after the last chunk.

        Args:
            text: The utterance to synthesize.
        """
        self._log.info(f'Synthetisiere: "{text}"')
        assert self._voice is not None

        all_samples: list[int] = []
        source_rate = self._voice.config.sample_rate

        for audio_chunk in self._voice.synthesize(text):
            raw_bytes = audio_chunk.audio_int16_bytes
            n_samples = len(raw_bytes) // 2
            chunk_samples = list(struct.unpack(f"<{n_samples}h", raw_bytes))
            all_samples.extend(chunk_samples)

        # Resampling falls nötig (Piper nutzt oft 22050 Hz)
        if source_rate != self.target_rate:
            arr = resample_linear(np.array(all_samples, dtype=np.float32), source_rate, self.target_rate)
            all_samples = np.clip(arr, -32768, 32767).astype(np.int16).tolist()

        # In Chunks aufteilen (SAMPLES_PER_FRAME pro Chunk = 20ms)
        chunk_size = SAMPLES_PER_FRAME
        total_chunks = 0

        for i in range(0, len(all_samples), chunk_size):
            chunk = all_samples[i : i + chunk_size]

            # Letzten Chunk mit Stille auffüllen
            if len(chunk) < chunk_size:
                chunk.extend([0] * (chunk_size - len(chunk)))

            # PCM-Bytes in die Queue
            pcm_bytes = struct.pack(f"<{chunk_size}h", *chunk)
            self.audio_queue.put(pcm_bytes)
            total_chunks += 1

        # End-of-Speech Marker (leerer Bytes-Block)
        # Wird vom Consumer erkannt um ggf. Pausen einzufügen
        self.audio_queue.put(b"__EOS__")

        duration_ms = (len(all_samples) / self.target_rate) * 1000
        self._log.info(f"TTS fertig: {total_chunks} Chunks, ~{duration_ms:.0f}ms Audio")


# ============================================================
# Audio Consumer – Custom PJSIP MediaPort
# ============================================================


class TTSMediaPort(pj.AudioMediaPort):  # type: ignore[misc]
    """PJSUA2 AudioMediaPort consumer that drains PCM chunks from a queue.

    PJSIP calls ``onFrameRequested()`` every 20 ms expecting exactly
    ``SAMPLES_PER_FRAME`` samples.  When the queue is empty or an EOS sentinel
    is encountered, a silent frame is returned so the conference bridge always
    receives valid audio.
    """

    def __init__(self, audio_queue: "Queue[bytes]", num_frames_silence: int = 8):
        """Initialize the media port and pre-compute the silence frame.

        Args:
            audio_queue: Shared queue populated by ``PiperTTSProducer``.  Each
                item is either a ``SAMPLES_PER_FRAME``-length raw PCM bytes
                object or the ``b"__EOS__"`` sentinel.
        """
        if PJSUA2_AVAILABLE:
            pj.AudioMediaPort.__init__(self)
        self.audio_queue = audio_queue
        self._silence = b"\x00" * (SAMPLES_PER_FRAME * num_frames_silence)  # 16-bit Stille

    def onFrameRequested(self, frame: "pj.MediaFrame") -> None:  # noqa: N802
        """Supply the next audio frame to PJSIP.

        Called by PJSIP every 20 ms.  Dequeues the next PCM chunk from
        ``audio_queue`` and writes it into ``frame``.  A silent frame is written
        when the queue is empty or an EOS sentinel is dequeued.

        Args:
            frame: The PJSIP media frame to populate with audio data.
        """
        try:
            chunk = self.audio_queue.get_nowait()

            if chunk == b"__EOS__":
                # End-of-Speech: Stille senden
                frame.type = pj.PJMEDIA_FRAME_TYPE_AUDIO
                frame.buf = self._silence
                return

            frame.type = pj.PJMEDIA_FRAME_TYPE_AUDIO
            frame.buf = chunk

        except Empty:
            # Queue leer -> Stille senden
            frame.type = pj.PJMEDIA_FRAME_TYPE_AUDIO
            frame.buf = self._silence

    def onFrameReceived(self, frame: "pj.MediaFrame") -> None:  # noqa: N802
        """Handle an inbound audio frame from the remote end.

        Called by PJSIP when audio arrives from the far side.  This port acts
        as a pure source and does not process received audio, so this method
        is intentionally left empty.

        Args:
            frame: The inbound media frame (ignored).
        """
        pass


# ============================================================
# Interaktive Konsole (für Live-TTS während des Calls)
# ============================================================


def interactive_console(tts_producer: PiperTTSProducer) -> None:
    """Read text from stdin and speak it into the active call.

    Intended to run in a dedicated thread alongside an ongoing SIP call.
    Each line of input is forwarded to ``tts_producer.speak()``.  The loop
    exits when the user types ``quit``, ``exit``, or ``q``, or when stdin
    reaches EOF (e.g. Ctrl-D / Ctrl-C).

    While the interactive prompt is active, loguru output is routed through
    a readline-aware sink that clears the current input line before printing
    log messages and re-displays the prompt + any partially typed text
    afterward.  This prevents log output from interleaving with the ``TTS>``
    prompt.

    Args:
        tts_producer: The producer instance that will synthesize and stream
            the entered text.
    """
    from sipstuff import LOGURU_FORMAT, configure_logging

    log = logger.bind(classname="InteractiveConsole")
    log.info("")
    log.info("=== Interaktiver Modus ===")
    log.info("Tippe Text ein und drücke Enter um ihn in den Call zu sprechen.")
    log.info("Befehle: 'quit' = Beenden")
    log.info("")

    prompt = "TTS> "
    _lock = threading.Lock()

    def _interactive_sink(message: str) -> None:
        with _lock:
            buf = readline.get_line_buffer()
            sys.stderr.write(f"\r\033[K{message}")
            sys.stderr.flush()
            sys.stdout.write(f"\r\033[K{prompt}{buf}")
            sys.stdout.flush()
            readline.redisplay()

    # Swap loguru sink for readline-aware interactive sink
    from loguru import logger as glogger

    glogger.remove()
    glogger.add(
        _interactive_sink,
        level=os.getenv("LOGURU_LEVEL", "DEBUG"),
        format=LOGURU_FORMAT,
        colorize=False,
    )

    try:
        while True:
            try:
                text = input(prompt).strip()
            except (EOFError, KeyboardInterrupt):
                break

            if text.lower() in ("quit", "exit", "q"):
                break

            if text:
                tts_producer.speak(text)
    finally:
        configure_logging()
