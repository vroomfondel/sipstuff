"""Live transcription thread for real-time speech-to-text during SIP calls.

Consumes audio chunks from a ``VADAudioBuffer`` and transcribes them using
faster-whisper in a background thread.  Transcribed segments are logged,
collected in ``self.segments``, and optionally forwarded to a callback.
"""

import threading
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any, TypeAlias

import numpy as np
from loguru import logger

TranscriptCallback: TypeAlias = Callable[[str, float, float], None]
"""Callback signature for live transcript events: ``(text, start_sec, end_sec)``."""

from sipstuff.audio import VADAudioBuffer
from sipstuff.sipconfig import SttConfig
from sipstuff.stt.stt import FASTER_WHISPER_AVAILABLE, OPENVINO_AVAILABLE, load_stt_model


class LiveTranscriptionThread(threading.Thread):
    """Background thread that transcribes audio chunks from a VAD buffer.

    Args:
        audio_buffer: VAD-enabled audio buffer supplying chunked audio.
        stt_config: Speech-to-text configuration (model, device, backend, data_dir, etc.).
        on_transcript: Optional callback invoked for each transcribed segment.
            See ``TranscriptCallback`` for the signature.
        beam_size: Whisper beam size (default: 5).
        vad_filter: Enable Whisper's internal VAD filter (default: ``True``).
        vad_parameters: Parameters for Whisper's VAD filter.
        call_start_time: Absolute start time for timecode calculation.
            Defaults to ``datetime.now()`` if not provided.
    """

    def __init__(
        self,
        audio_buffer: VADAudioBuffer,
        stt_config: SttConfig | None = None,
        on_transcript: TranscriptCallback | None = None,
        beam_size: int = 5,
        vad_filter: bool = True,
        vad_parameters: dict[str, Any] | None = None,
        call_start_time: datetime | None = None,
        model: Any = None,
    ):
        super().__init__(daemon=True)
        self.stt_config = stt_config or SttConfig()
        if model is None:
            if self.stt_config.backend == "openvino" and not OPENVINO_AVAILABLE:
                raise ImportError("OpenVINO STT not available. Install with: pip install optimum-intel[openvino]")
            elif self.stt_config.backend != "openvino" and not FASTER_WHISPER_AVAILABLE:
                raise ImportError("faster-whisper not available. Install with: pip install faster-whisper")
        self._log = logger.bind(classname="LiveSTT")
        self.audio_buffer = audio_buffer
        self.running = True
        self.on_transcript = on_transcript
        self.beam_size = beam_size
        self.vad_filter = vad_filter
        self.vad_parameters = vad_parameters or dict(min_silence_duration_ms=200, speech_pad_ms=100)
        self.model: Any = model  # pre-loaded WhisperModel/HF pipeline, or None (load in run())
        self.segments: list[dict[str, object]] = []
        self.call_start_time = call_start_time or datetime.now()

    def run(self) -> None:
        """Main loop: load the model, transcribe incoming chunks, flush on stop.

        Loads the STT model if one was not pre-supplied, then polls the
        ``VADAudioBuffer`` for completed speech chunks at 0.3-second intervals.
        Each chunk is passed to ``_transcribe()``.  When ``running`` is set to
        ``False`` (via ``stop()``), the loop exits and any data remaining in
        the buffer is flushed and transcribed before the thread terminates.
        """
        if self.model is not None:
            self._log.info("Using pre-loaded STT model.")
        else:
            self.model = load_stt_model(self.stt_config)
        self._log.info("Waiting for speech...")

        while self.running:
            result = self.audio_buffer.get_chunk(timeout=0.3)
            if result is not None:
                chunk, chunk_start_sec, chunk_end_sec = result
                self._transcribe(chunk, chunk_start_sec, chunk_end_sec)

        # Flush remaining data
        remaining = self.audio_buffer.flush_remaining()
        if remaining is not None:
            chunk, chunk_start_sec, chunk_end_sec = remaining
            self._transcribe(chunk, chunk_start_sec, chunk_end_sec)

    def _format_timecode(self, offset_sec: float) -> str:
        """Convert a relative offset in seconds to an absolute clock time string (HH:MM:SS.f)."""
        absolute = self.call_start_time + timedelta(seconds=offset_sec)
        return absolute.strftime("%H:%M:%S.") + f"{absolute.microsecond // 100000}"

    def _format_duration(self, seconds: float) -> str:
        """Format seconds as MM:SS.ff."""
        m, s = divmod(seconds, 60)
        return f"{int(m):02d}:{s:05.2f}"

    def _transcribe(self, audio: np.ndarray, chunk_start_sec: float, chunk_end_sec: float) -> None:
        """Transcribe a single audio chunk and record the result.

        Dispatches to the appropriate backend (``_transcribe_faster_whisper``
        or ``_transcribe_openvino``), logs the recognised text with absolute
        and relative timestamps, appends the segment to ``self.segments``, and
        invokes ``on_transcript`` if set.  Transcription errors are caught and
        logged without propagating.

        Args:
            audio: Float32 PCM array at 16 kHz (mono) representing the speech
                chunk to transcribe.
            chunk_start_sec: Start offset of the chunk in seconds, relative to
                ``call_start_time``.
            chunk_end_sec: End offset of the chunk in seconds, relative to
                ``call_start_time``.
        """
        assert self.model is not None

        abs_start = self._format_timecode(chunk_start_sec)
        abs_end = self._format_timecode(chunk_end_sec)
        rel_start = self._format_duration(chunk_start_sec)
        rel_end = self._format_duration(chunk_end_sec)

        try:
            if self.stt_config.backend == "openvino":
                segment_texts = self._transcribe_openvino(audio)
            else:
                segment_texts = self._transcribe_faster_whisper(audio)

            if segment_texts:
                full_text = " ".join(segment_texts)
                self._log.info(f"[{abs_start}–{abs_end}] ({rel_start}–{rel_end})  {full_text}")
                abs_start_dt = self.call_start_time + timedelta(seconds=chunk_start_sec)
                abs_end_dt = self.call_start_time + timedelta(seconds=chunk_end_sec)
                self.segments.append(
                    {
                        "start": chunk_start_sec,
                        "end": chunk_end_sec,
                        "text": full_text,
                        "abs_start": abs_start_dt.isoformat(),
                        "abs_end": abs_end_dt.isoformat(),
                    }
                )
                if self.on_transcript is not None:
                    self.on_transcript(full_text, chunk_start_sec, chunk_end_sec)

        except Exception as e:
            self._log.error(f"Transcription error: {e}")

    def _transcribe_faster_whisper(self, audio: np.ndarray) -> list[str]:
        """Transcribe an audio chunk using the faster-whisper backend.

        Args:
            audio: Float32 PCM array at 16 kHz (mono) to transcribe.

        Returns:
            A list of non-empty stripped text strings, one per recognised
            Whisper segment.  Returns an empty list when no speech is detected.
        """
        segments, info = self.model.transcribe(
            audio,
            beam_size=self.beam_size,
            language=self.stt_config.language,
            vad_filter=self.vad_filter,
            vad_parameters=self.vad_parameters,
        )
        return [segment.text.strip() for segment in segments if segment.text.strip()]

    def _transcribe_openvino(self, audio: np.ndarray) -> list[str]:
        """Transcribe audio chunk using OpenVINO pipeline.

        The HuggingFace pipeline accepts a float32 ndarray at 16 kHz
        (same format ``VADAudioBuffer.get_chunk()`` returns).
        """
        generate_kwargs: dict[str, Any] = {}
        if self.stt_config.language:
            generate_kwargs["language"] = self.stt_config.language
        result = self.model(audio, return_timestamps=True, generate_kwargs=generate_kwargs)
        chunks = result.get("chunks", [])
        if chunks:
            return [c["text"].strip() for c in chunks if c.get("text", "").strip()]
        text = result.get("text", "").strip()
        return [text] if text else []

    def stop(self) -> None:
        """Signal the thread to stop after processing remaining chunks."""
        self.running = False
