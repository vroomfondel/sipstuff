"""
Live TTS streaming into PJSIP calls using a producer-consumer pattern.

Provides the two core building blocks for real-time TTS streaming in SIP calls:

- ``PiperTTSProducer``: Producer thread that synthesizes text chunk-by-chunk
  via the Piper CLI subprocess and writes raw PCM data into a shared queue.
- ``TTSMediaPort``: PJSUA2 ``AudioMediaPort`` consumer that drains the queue
  every 20 ms and feeds PCM frames to the PJSIP conference bridge.
- ``interactive_console``: Helper that reads text from stdin and forwards it
  to a ``PiperTTSProducer`` for live speaking during an active call.
"""

import os
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import wave
from queue import Empty, Queue
from typing import IO, Optional

import numpy as np
from loguru import logger

from sipstuff.audio import resample_linear
from sipstuff.tts.tts import TtsModelInfo

_PIPER_BIN = os.getenv("PIPER_BIN", "/opt/piper-venv/bin/piper")

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
    """Producer thread that synthesizes text via the Piper CLI and enqueues PCM chunks.

    Text requests submitted via ``speak()`` are processed sequentially in a
    dedicated background thread.  Each synthesized utterance is split into
    20 ms PCM chunks and written to ``audio_queue``, followed by a ``b"__EOS__"``
    end-of-speech sentinel that the consumer can use to insert pauses.

    Example:
        producer = PiperTTSProducer(model_path, audio_queue)
        producer.start()
        producer.speak("Hello world.")   # non-blocking
        producer.speak("Another line.") # queued, processed in order
        producer.stop()
    """

    def __init__(
        self,
        model_path: str,
        audio_queue: "Queue[bytes]",
        target_rate: int = CLOCK_RATE,
        tts_model_info: TtsModelInfo | None = None,
    ):
        """Initialize the producer.

        Args:
            model_path: Path to the Piper ONNX voice model file.
            audio_queue: Shared queue into which synthesized PCM chunks are placed.
            target_rate: Target PCM sample rate in Hz.  Audio is resampled to
                this rate when the Piper model's native rate differs.
            tts_model_info: Optional pre-loaded model metadata.  When provided,
                the Piper binary path and model path are taken from this object
                instead of ``model_path`` and the ``PIPER_BIN`` environment variable.
        """
        self.model_path = model_path
        self.audio_queue = audio_queue
        self.target_rate = target_rate
        self._tts_model_info = tts_model_info

        self.text_queue: "Queue[str | None]" = Queue()
        self._thread: Optional[threading.Thread] = None
        self._running = False
        self._piper_bin: str = ""
        self._log = logger.bind(classname="PiperTTSProducer")
        self._piper_log = logger.bind(classname="Piper")

    def start(self) -> None:
        """Resolve the Piper binary and model, then start the producer thread."""
        if self._tts_model_info is not None:
            self._piper_bin = self._tts_model_info.piper_bin
            self.model_path = str(self._tts_model_info.model_path)
        else:
            piper_bin = _PIPER_BIN
            if not os.path.isfile(piper_bin):
                piper_bin = shutil.which("piper") or ""
            if not piper_bin or not os.path.isfile(piper_bin):
                self._log.error(f"Piper-Binary nicht gefunden (PIPER_BIN={_PIPER_BIN})")
                sys.exit(1)
            self._piper_bin = piper_bin

        if not os.path.isfile(self.model_path):
            self._log.error(f"Piper-Modell nicht gefunden: {self.model_path}")
            sys.exit(1)

        self._log.info(f"Piper-Binary: {self._piper_bin}")
        self._log.info(f"Piper-Modell: {self.model_path}")

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

        Runs the Piper subprocess with ``text`` on stdin, writes WAV output to
        a temporary file, reads and decodes the PCM samples, resamples to
        ``target_rate`` when the model's native rate differs, splits the audio
        into 20 ms frames (``SAMPLES_PER_FRAME`` samples each, zero-padded if
        the last chunk is short), and enqueues each frame as raw little-endian
        16-bit PCM bytes.  A ``b"__EOS__"`` sentinel is appended after the last
        chunk.  The temporary WAV file is always deleted on exit.

        Args:
            text: The utterance to synthesize.
        """
        self._log.info(f'Synthetisiere: "{text}"')

        tmp_path = ""
        try:
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
                tmp_path = tmp.name

            proc = subprocess.Popen(
                [self._piper_bin, "--model", self.model_path, "--output_file", tmp_path],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )

            stderr_thread = threading.Thread(
                target=self._stream_piper_logs,
                args=(proc.stderr,),
                daemon=True,
            )
            stderr_thread.start()

            assert proc.stdin is not None
            proc.stdin.write(text)
            proc.stdin.close()
            assert proc.stdout is not None
            proc.stdout.read()  # drain stdout
            proc.wait(timeout=30)
            stderr_thread.join(timeout=5)

            if proc.returncode != 0:
                self._log.error(f"Piper fehlgeschlagen (rc={proc.returncode})")
                return

            with wave.open(tmp_path, "rb") as wav_file:
                source_rate = wav_file.getframerate()
                n_frames = wav_file.getnframes()
                raw_data = wav_file.readframes(n_frames)

            # Samples dekodieren
            n_samples = len(raw_data) // 2
            samples = list(struct.unpack(f"<{n_samples}h", raw_data))

            # Resampling falls nötig (Piper nutzt oft 22050 Hz)
            if source_rate != self.target_rate:
                arr = resample_linear(np.array(samples, dtype=np.float32), source_rate, self.target_rate)
                samples = np.clip(arr, -32768, 32767).astype(np.int16).tolist()

            # In Chunks aufteilen (SAMPLES_PER_FRAME pro Chunk = 20ms)
            chunk_size = SAMPLES_PER_FRAME
            total_chunks = 0

            for i in range(0, len(samples), chunk_size):
                chunk = samples[i : i + chunk_size]

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

            duration_ms = (len(samples) / self.target_rate) * 1000
            self._log.info(f"TTS fertig: {total_chunks} Chunks, ~{duration_ms:.0f}ms Audio")
        finally:
            if tmp_path and os.path.exists(tmp_path):
                os.unlink(tmp_path)

    def _stream_piper_logs(self, stream: IO[str] | None) -> None:
        """Read Piper's stderr line by line and forward each line to loguru."""
        if stream is None:
            return
        for line in stream:
            line = line.rstrip()
            if line:
                self._piper_log.info(line)


# ============================================================
# Audio Consumer – Custom PJSIP MediaPort
# ============================================================


class TTSMediaPort(pj.AudioMediaPort if PJSUA2_AVAILABLE else object):  # type: ignore[misc]
    """PJSUA2 AudioMediaPort consumer that drains PCM chunks from a queue.

    PJSIP calls ``onFrameRequested()`` every 20 ms expecting exactly
    ``SAMPLES_PER_FRAME`` samples.  When the queue is empty or an EOS sentinel
    is encountered, a silent frame is returned so the conference bridge always
    receives valid audio.
    """

    def __init__(self, audio_queue: "Queue[bytes]"):
        """Initialize the media port and pre-compute the silence frame.

        Args:
            audio_queue: Shared queue populated by ``PiperTTSProducer``.  Each
                item is either a ``SAMPLES_PER_FRAME``-length raw PCM bytes
                object or the ``b"__EOS__"`` sentinel.
        """
        if PJSUA2_AVAILABLE:
            pj.AudioMediaPort.__init__(self)
        self.audio_queue = audio_queue
        self._silence = b"\x00" * (SAMPLES_PER_FRAME * 2)  # 16-bit Stille

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

    Args:
        tts_producer: The producer instance that will synthesize and stream
            the entered text.
    """
    log = logger.bind(classname="InteractiveConsole")
    log.info("")
    log.info("=== Interaktiver Modus ===")
    log.info("Tippe Text ein und drücke Enter um ihn in den Call zu sprechen.")
    log.info("Befehle: 'quit' = Beenden")
    log.info("")

    while True:
        try:
            text = input("TTS> ").strip()
        except (EOFError, KeyboardInterrupt):
            break

        if text.lower() in ("quit", "exit", "q"):
            break

        if text:
            tts_producer.speak(text)
