"""Speech-to-text transcription using faster-whisper.

Transcribes WAV files (e.g. recorded by ``SipCaller.make_call(record_path=…)``)
to text using CTranslate2-accelerated Whisper models.

Models are auto-downloaded on first use into a persistent cache directory
(default: ``~/.local/share/faster-whisper-models``, override with the
``WHISPER_DATA_DIR`` environment variable).

Environment Variables:
    WHISPER_DATA_DIR: Directory for downloaded Whisper models
        (default: ``~/.local/share/faster-whisper-models``).
    WHISPER_MODEL: Default model size
        (default: ``medium``, options: tiny/base/small/medium/large-v3).
    WHISPER_DEVICE: Compute device (default: ``cpu``, or ``cuda``).
    WHISPER_COMPUTE_TYPE: Quantization type
        (default: ``int8`` for CPU, ``float16`` for CUDA).
"""

import os
import threading
from pathlib import Path
from typing import Any

from loguru import logger

from sipstuff.sipconfig import SttConfig

try:
    from faster_whisper import WhisperModel

    FASTER_WHISPER_AVAILABLE = True
except ImportError:
    WhisperModel = None
    FASTER_WHISPER_AVAILABLE = False

try:
    from optimum.intel.openvino import OVModelForSpeechSeq2Seq
    from transformers import AutoProcessor
    from transformers import pipeline as hf_pipeline

    OPENVINO_AVAILABLE = True
except ImportError:
    OPENVINO_AVAILABLE = False

_WHISPER_DATA_DIR = Path(os.getenv("WHISPER_DATA_DIR", Path.home() / ".local" / "share" / "faster-whisper-models"))
_DEFAULT_MODEL = os.getenv("WHISPER_MODEL", "medium")
_DEFAULT_DEVICE = os.getenv("WHISPER_DEVICE", "cpu")
_DEFAULT_COMPUTE_TYPE = os.getenv("WHISPER_COMPUTE_TYPE", "")

_MODEL_CACHE: dict[tuple[str, str, str, str, str], Any] = {}
_MODEL_CACHE_LOCK = threading.Lock()


def _make_cache_key(
    backend: str, model: str, device: str, compute_type: str, data_dir: str | None
) -> tuple[str, str, str, str, str]:
    """Build a cache key tuple from STT configuration parameters.

    Resolves defaults for ``compute_type`` and ``data_dir`` so that
    semantically identical configurations always map to the same key.

    Args:
        backend: STT backend identifier (e.g. ``"faster-whisper"`` or ``"openvino"``).
        model: Model name or HuggingFace model ID.
        device: Compute device (e.g. ``"cpu"`` or ``"cuda"``).
        compute_type: Quantization type; empty string triggers auto-selection
            based on ``device``.
        data_dir: Path to the model cache directory, or ``None`` to use the
            module-level default (``_WHISPER_DATA_DIR``).

    Returns:
        A five-element tuple ``(backend, model, device, compute_type, data_dir)``
        with all defaults resolved to concrete string values.
    """
    resolved_ct = compute_type or ("float16" if device == "cuda" else "int8")
    resolved_dd = str(data_dir) if data_dir else str(_WHISPER_DATA_DIR)
    return (backend, model, device, resolved_ct, resolved_dd)


def clear_stt_cache() -> None:
    """Clear the cached STT models, forcing a reload on next use."""
    with _MODEL_CACHE_LOCK:
        _MODEL_CACHE.clear()


class SttError(Exception):
    """Raised when speech-to-text transcription fails."""


def _require_faster_whisper() -> None:
    """Raise ``SttError`` if faster-whisper is not installed."""
    if not FASTER_WHISPER_AVAILABLE:
        raise SttError("faster-whisper not available. Install with: pip install faster-whisper")


def _require_openvino() -> None:
    """Raise ``SttError`` if optimum-intel[openvino] is not installed."""
    if not OPENVINO_AVAILABLE:
        raise SttError("OpenVINO STT not available. Install with: pip install optimum-intel[openvino]")


def _transcribe_openvino(
    wav_path: Path,
    model: str,
    language: str,
    pipeline: Any,
) -> tuple[str, dict[str, Any]]:
    """Transcribe a WAV file using OpenVINO (optimum-intel) Whisper pipeline.

    Args:
        wav_path: Resolved path to the WAV file.
        model: HuggingFace model ID (e.g. ``"OpenVINO/whisper-base-int8-ov"``).
        language: Language code for transcription.
        pipeline: Pre-loaded HuggingFace ASR pipeline from ``load_stt_model()``.

    Returns:
        A tuple of ``(text, metadata)`` matching the faster-whisper return shape.
    """
    log = logger.bind(classname="STT-OpenVINO")
    log.info(f"Transcribing {wav_path.name} (model={model}, lang={language}, backend=openvino)")

    try:
        result = pipeline(str(wav_path), return_timestamps=True, generate_kwargs={"language": language})
    except Exception as exc:
        raise SttError(f"OpenVINO transcription failed: {exc}") from exc

    text = result.get("text", "").strip()
    chunks = result.get("chunks", [])
    segment_list: list[dict[str, Any]] = []
    for chunk in chunks:
        chunk_text = chunk.get("text", "").strip()
        ts = chunk.get("timestamp", (0.0, 0.0))
        if chunk_text:
            segment_list.append(
                {
                    "start": round(ts[0], 2) if ts[0] is not None else 0.0,
                    "end": round(ts[1], 2) if ts[1] is not None else 0.0,
                    "text": chunk_text,
                }
            )

    log.info(f"Transcribed ({language}, backend=openvino): {len(segment_list)} segments, {len(text)} chars")
    stt_meta: dict[str, Any] = {
        "audio_duration": None,
        "language": language,
        "language_probability": None,
        "segments": segment_list,
    }
    return text, stt_meta


def transcribe_wav(
    wav_path: str | Path,
    model: str | None = None,
    language: str = "de",
    device: str | None = None,
    compute_type: str | None = None,
    data_dir: str | Path | None = None,
    vad_filter: bool = True,
    backend: str = "faster-whisper",
) -> tuple[str, dict[str, Any]]:
    """Transcribe a WAV file to text using faster-whisper or OpenVINO.

    Args:
        wav_path: Path to the WAV file to transcribe.
        model: Whisper model size (``tiny``, ``base``, ``small``,
            ``medium``, ``large-v3``) for faster-whisper, or a HuggingFace
            model ID (e.g. ``"OpenVINO/whisper-base-int8-ov"``) for OpenVINO.
            ``None`` uses the ``WHISPER_MODEL`` env var or ``"medium"``.
        language: Language code for transcription (e.g. ``"de"``, ``"en"``).
        device: Compute device (``"cpu"`` or ``"cuda"``).
            ``None`` uses ``WHISPER_DEVICE`` env var or ``"cpu"``.
            Ignored when ``backend="openvino"`` (OpenVINO manages devices internally).
        compute_type: Quantization type (``"int8"``, ``"float16"``,
            ``"float32"``).  ``None`` auto-selects based on device.
            Ignored when ``backend="openvino"``.
        data_dir: Directory for model cache.
            ``None`` uses ``WHISPER_DATA_DIR`` env var or
            ``~/.local/share/faster-whisper-models``.
        vad_filter: Use Silero VAD to split audio into speech segments
            before transcription.  Strongly recommended for phone
            recordings with silences or ringing tones (default: ``True``).
            Ignored when ``backend="openvino"``.
        backend: STT backend engine (``"faster-whisper"`` or ``"openvino"``).

    Returns:
        A tuple of ``(text, metadata)`` where *text* is the transcribed
        string and *metadata* is a dict with keys ``audio_duration``,
        ``language``, ``language_probability``, and ``segments``.

    Raises:
        SttError: If the required backend is not installed, the WAV file
            does not exist, or transcription fails.
    """
    wav_path = Path(wav_path).resolve()
    if not wav_path.is_file():
        raise SttError(f"WAV file not found: {wav_path}")

    model = model or _DEFAULT_MODEL
    device = device or _DEFAULT_DEVICE
    model_dir = Path(data_dir) if data_dir else _WHISPER_DATA_DIR
    model_dir.mkdir(parents=True, exist_ok=True)

    cfg = SttConfig(
        backend=backend,
        model=model,
        device=device,
        compute_type=compute_type or _DEFAULT_COMPUTE_TYPE or None,
        data_dir=str(model_dir),
        language=language,
    )
    loaded_model = load_stt_model(cfg)

    if backend == "openvino":
        return _transcribe_openvino(wav_path, model, language, loaded_model)

    if compute_type is None:
        compute_type = _DEFAULT_COMPUTE_TYPE or ("float16" if device == "cuda" else "int8")

    log = logger.bind(classname="STT")
    log.info(
        f"Transcribing {wav_path.name} (model={model}, lang={language}, device={device}, "
        f"compute={compute_type}, vad={vad_filter})"
    )

    try:
        segments_iter, info = loaded_model.transcribe(str(wav_path), language=language, vad_filter=vad_filter)
        segment_list = []
        for segment in segments_iter:
            stripped = segment.text.strip()
            if stripped:
                segment_list.append({"start": round(segment.start, 2), "end": round(segment.end, 2), "text": stripped})
        text = " ".join(s["text"] for s in segment_list)
    except Exception as exc:
        raise SttError(f"Transcription failed: {exc}") from exc

    log.info(
        f"Transcribed {info.duration:.1f}s audio ({language}, p={info.language_probability:.2f}): "
        f"{len(segment_list)} segments, {len(text)} chars"
    )
    stt_meta: dict[str, Any] = {
        "audio_duration": info.duration,
        "language": language,
        "language_probability": info.language_probability,
        "segments": segment_list,
    }
    return text, stt_meta


def _load_stt_model_uncached(cfg: SttConfig) -> Any:
    """Load an STT model without caching.

    Instantiates either a ``WhisperModel`` (faster-whisper backend) or a
    HuggingFace ASR pipeline (OpenVINO backend) based on ``cfg.backend``.

    Args:
        cfg: STT configuration specifying the backend, model, device,
            compute type, and optional model cache directory.

    Returns:
        A loaded ``WhisperModel`` instance for the ``"faster-whisper"`` backend,
        or a HuggingFace ASR ``pipeline`` object for the ``"openvino"`` backend.

    Raises:
        ImportError: If the required backend library (``faster-whisper`` or
            ``optimum-intel[openvino]``) is not installed.
    """
    log = logger.bind(classname="LiveSTT")
    if cfg.backend == "openvino":
        if not OPENVINO_AVAILABLE:
            raise ImportError("OpenVINO STT not available. Install with: pip install optimum-intel[openvino]")
        log.info(f"Loading OpenVINO model '{cfg.model}'...")
        ov_kwargs: dict[str, Any] = {}
        if cfg.data_dir is not None:
            ov_kwargs["cache_dir"] = str(cfg.data_dir)
        ov_model = OVModelForSpeechSeq2Seq.from_pretrained(cfg.model, **ov_kwargs)
        processor = AutoProcessor.from_pretrained(cfg.model, **ov_kwargs)
        return hf_pipeline(
            "automatic-speech-recognition",
            model=ov_model,
            tokenizer=processor.tokenizer,
            feature_extractor=processor.feature_extractor,
        )
    else:
        if not FASTER_WHISPER_AVAILABLE:
            raise ImportError("faster-whisper not available. Install with: pip install faster-whisper")
        log.info(f"Loading Whisper model '{cfg.model}' on {cfg.device}...")
        compute_type = cfg.compute_type or ("int8" if cfg.device == "cpu" else "float16")
        fw_kwargs: dict[str, Any] = {}
        if cfg.data_dir is not None:
            fw_kwargs["download_root"] = str(cfg.data_dir)
        model = WhisperModel(cfg.model, device=cfg.device, compute_type=compute_type, **fw_kwargs)
        log.info("STT model loaded.")
        return model


def load_stt_model(cfg: SttConfig) -> Any:
    """Load an STT model with caching. Returns WhisperModel or HF pipeline.

    Uses a module-level cache keyed by ``(backend, model, device, compute_type, data_dir)``.
    Concurrent loads of the same model are safe: the first loaded instance wins.
    """
    key = _make_cache_key(
        cfg.backend, cfg.model, cfg.device, cfg.compute_type or "", str(cfg.data_dir) if cfg.data_dir else None
    )
    with _MODEL_CACHE_LOCK:
        if key in _MODEL_CACHE:
            logger.bind(classname="LiveSTT").debug(f"STT cache hit: {key[:2]}")
            return _MODEL_CACHE[key]
    # Load outside lock (slow), then re-check
    loaded = _load_stt_model_uncached(cfg)
    with _MODEL_CACHE_LOCK:
        if key not in _MODEL_CACHE:
            _MODEL_CACHE[key] = loaded
        return _MODEL_CACHE[key]
