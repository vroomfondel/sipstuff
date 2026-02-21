"""Speech-to-text transcription via faster-whisper and OpenVINO backends."""

from sipstuff.stt.stt import SttError, clear_stt_cache, load_stt_model, transcribe_wav

__all__ = ["SttError", "transcribe_wav", "load_stt_model", "clear_stt_cache"]
