"""Text-to-speech WAV generation using the piper Python API.

Generates WAV files from text suitable for SIP playback.  Uses piper-tts ≥1.4.0
which ships ``cp39-abi3`` wheels and works directly with Python 3.14 — no
separate venv or subprocess workaround required.

Voice models are auto-downloaded on first use into a persistent cache
directory (default: ``~/.local/share/piper-voices``, override with the
``PIPER_DATA_DIR`` environment variable).  Optional resampling via
soundfile/numpy converts the native piper output (22 050 Hz) to
SIP-friendly rates (8 000 Hz narrowband or 16 000 Hz wideband).

Environment Variables:
    PIPER_DATA_DIR: Directory for downloaded voice models
        (default: ``~/.local/share/piper-voices``).
"""

import os
import tempfile
import threading
import wave
from dataclasses import dataclass
from pathlib import Path

import soundfile as sf
from loguru import logger
from piper import PiperVoice
from piper.download_voices import download_voice

from sipstuff.audio import resample_linear
from sipstuff.sipconfig import TtsConfig

# Persistent model cache directory
_PIPER_DATA_DIR = Path(os.getenv("PIPER_DATA_DIR", Path.home() / ".local" / "share" / "piper-voices"))


class TtsError(Exception):
    """Raised when TTS generation fails."""


@dataclass(frozen=True)
class TtsModelInfo:
    """Resolved TTS model info returned by ``load_tts_model()``."""

    voice: PiperVoice
    model_path: Path
    data_dir: Path
    use_cuda: bool = False


_TTS_CACHE: dict[tuple[str, str, bool], TtsModelInfo] = {}
_TTS_CACHE_LOCK = threading.Lock()


def _ensure_model(model: str, data_dir: Path) -> None:
    """Download a piper voice model if not already present in ``data_dir``.

    Uses ``piper.download_voices.download_voice`` to fetch the model's
    ``.onnx`` and ``.onnx.json`` files from HuggingFace.

    Args:
        model: Piper model name (e.g. ``"de_DE-thorsten-high"``).
        data_dir: Directory to store downloaded model files.

    Raises:
        TtsError: If the download fails or the expected ``.onnx`` file
            is missing after download.
    """
    model_path = data_dir / f"{model}.onnx"
    if model_path.exists():
        return

    logger.info(f"TTS: downloading voice model '{model}' (first time only)...")
    try:
        download_voice(model, data_dir)
    except Exception as exc:
        raise TtsError(f"Failed to download voice model '{model}': {exc}") from exc

    if not model_path.exists():
        raise TtsError(f"Model download reported success but {model_path} not found")

    logger.info(f"TTS: model downloaded to {model_path}")


def generate_wav(
    text: str,
    model: str = "de_DE-thorsten-high",
    output_path: str | Path | None = None,
    sample_rate: int = 0,
    data_dir: str | Path | None = None,
    use_cuda: bool = False,
) -> Path:
    """Generate a WAV file from text using piper TTS.

    Args:
        text: Text to synthesize.
        model: Piper model name (auto-downloaded on first use).
        output_path: Output WAV path. None = auto-generated temp file.
        sample_rate: Resample to this rate (0 = keep piper native rate).
                     Use 8000 for narrowband SIP or 16000 for wideband.
        data_dir: Directory for voice models. None = PIPER_DATA_DIR env or ~/.local/share/piper-voices.

    Returns:
        Path to the generated WAV file.

    Raises:
        TtsError: If piper is not found or synthesis fails.
    """
    if not text.strip():
        raise TtsError("Empty text provided for TTS")

    cfg = TtsConfig(
        model=model, sample_rate=sample_rate, data_dir=str(data_dir) if data_dir else None, use_cuda=use_cuda
    )
    info = load_tts_model(cfg)

    if output_path is None:
        fd, tmp = tempfile.mkstemp(suffix=".wav", prefix="sipstuff_tts_")
        os.close(fd)
        output_path = Path(tmp)
    else:
        output_path = Path(output_path)

    logger.info(f"TTS: generating speech for {len(text)} chars with model '{model}'")

    try:
        with wave.open(str(output_path), "wb") as wav_file:
            info.voice.synthesize_wav(text, wav_file)
    except Exception as exc:
        raise TtsError(f"piper TTS synthesis failed: {exc}") from exc

    if not output_path.is_file() or output_path.stat().st_size == 0:
        raise TtsError("piper produced no output")

    # Resample if requested
    if sample_rate > 0:
        _resample_wav(output_path, sample_rate)

    logger.info(f"TTS: generated {output_path} ({output_path.stat().st_size} bytes)")
    return output_path


def _resample_wav(wav_path: Path, target_rate: int) -> None:
    """Resample a WAV file in-place to ``target_rate`` Hz.

    Reads the WAV via soundfile, delegates to :func:`sipstuff.audio.resample_linear`,
    and writes back as mono 16-bit PCM.

    Args:
        wav_path: Path to the WAV file to resample (modified in-place).
        target_rate: Target sample rate in Hz (e.g. 8000, 16000).

    Raises:
        TtsError: If reading or writing the WAV file fails.
    """
    try:
        data, src_rate = sf.read(wav_path, dtype="float32")
    except Exception as exc:
        raise TtsError(f"Failed to read WAV for resampling: {exc}") from exc

    if src_rate == target_rate:
        return

    # Convert to mono if stereo
    if data.ndim > 1:
        data = data.mean(axis=1)

    resampled = resample_linear(data, src_rate, target_rate)

    try:
        sf.write(wav_path, resampled, target_rate, subtype="PCM_16")
    except Exception as exc:
        raise TtsError(f"Failed to write resampled WAV: {exc}") from exc


def load_tts_model(cfg: TtsConfig) -> TtsModelInfo:
    """Load a PiperVoice, ensure model is downloaded, return info.

    Cached by ``(model, data_dir, use_cuda)`` — subsequent calls with the
    same config skip model loading and filesystem checks.

    Args:
        cfg: TTS configuration with model name and optional data directory.

    Returns:
        A :class:`TtsModelInfo` with loaded PiperVoice and model paths.

    Raises:
        TtsError: If the model cannot be found/downloaded or loaded.
    """
    data_dir = Path(cfg.data_dir) if cfg.data_dir else _PIPER_DATA_DIR
    key = (cfg.model, str(data_dir), cfg.use_cuda)

    with _TTS_CACHE_LOCK:
        if key in _TTS_CACHE:
            return _TTS_CACHE[key]

    data_dir.mkdir(parents=True, exist_ok=True)
    _ensure_model(cfg.model, data_dir)
    model_path = data_dir / f"{cfg.model}.onnx"

    try:
        voice = PiperVoice.load(str(model_path), use_cuda=cfg.use_cuda)
    except Exception as exc:
        raise TtsError(f"Failed to load piper voice model '{cfg.model}': {exc}") from exc

    info = TtsModelInfo(
        voice=voice,
        model_path=model_path,
        data_dir=data_dir,
        use_cuda=cfg.use_cuda,
    )
    with _TTS_CACHE_LOCK:
        _TTS_CACHE.setdefault(key, info)
        return _TTS_CACHE[key]


def clear_tts_cache() -> None:
    """Clear cached TTS model info, forcing re-discovery on next use."""
    with _TTS_CACHE_LOCK:
        _TTS_CACHE.clear()
