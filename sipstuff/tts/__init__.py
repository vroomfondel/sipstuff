"""Text-to-speech generation via Piper TTS (subprocess and live streaming)."""

from sipstuff.tts.live import (
    BITS_PER_SAMPLE,
    CHANNEL_COUNT,
    CLOCK_RATE,
    SAMPLES_PER_FRAME,
    PiperTTSProducer,
    TTSMediaPort,
)
from sipstuff.tts.tts import TtsError, TtsModelInfo, clear_tts_cache, generate_wav, load_tts_model

__all__ = [
    "BITS_PER_SAMPLE",
    "CHANNEL_COUNT",
    "CLOCK_RATE",
    "PiperTTSProducer",
    "SAMPLES_PER_FRAME",
    "TTSMediaPort",
    "TtsError",
    "TtsModelInfo",
    "clear_tts_cache",
    "generate_wav",
    "load_tts_model",
]
