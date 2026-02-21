"""SIP caller package — place phone calls and play WAV files or TTS via PJSUA2.

Provides a high-level convenience function (``make_sip_call``) for one-shot
calls and a context-manager class (``SipCaller``) for placing multiple calls
on a single SIP registration.  Text-to-speech is handled by piper TTS
(``generate_wav``).  Speech-to-text transcription of recorded calls is
provided by faster-whisper (``transcribe_wav``).

Typical usage::

    from sipstuff import make_sip_call

    make_sip_call(
        server="pbx.local",
        user="1000",
        password="secret",
        destination="+491234567890",
        wav_file="alert.wav",
    )

See ``sipstuff/README.md`` for full CLI, library, and Docker usage examples.
"""

import os
import sys
from pathlib import Path

__version__ = "0.0.1"

from typing import Any, Callable, Dict

from loguru import logger as glogger
from tabulate import tabulate

# glogger.disable(__name__)


def _loguru_skiplog_filter(record: dict) -> bool:  # type: ignore[type-arg]
    """Filter function to hide records with ``extra['skiplog']`` set."""
    return not record.get("extra", {}).get("skiplog", False)


def configure_logging(
    loguru_filter: Callable[[Dict[str, Any]], bool] = _loguru_skiplog_filter,
) -> None:
    """Configure a default ``loguru`` sink with a convenient format and filter."""
    os.environ["LOGURU_LEVEL"] = os.getenv("LOGURU_LEVEL", "DEBUG")
    glogger.remove()
    logger_fmt: str = (
        "<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | <level>{level: <8}</level> | <cyan>{module}</cyan>::<cyan>{extra[classname]}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - <level>{message}</level>"
    )
    glogger.add(sys.stderr, level=os.getenv("LOGURU_LEVEL"), format=logger_fmt, filter=loguru_filter)  # type: ignore[arg-type]
    glogger.configure(extra={"classname": "None", "skiplog": False})


def print_banner() -> None:
    """Log a startup banner with version, build time, and project URLs.

    Renders a ``tabulate`` mixed-grid table with a Unicode box-drawing
    title row and emits it via loguru in raw mode.
    """
    startup_rows = [
        ["version", __version__],
        ["buildtime", os.environ.get("BUILDTIME", "n/a")],
        ["github", "https://github.com/vroomfondel/sipstuff"],
        ["Docker Hub", "https://hub.docker.com/r/xomoxcc/sipstuff"],
    ]
    table_str = tabulate(startup_rows, tablefmt="mixed_grid")
    lines = table_str.split("\n")
    table_width = len(lines[0])
    title = "sipstuff starting up"
    title_border = "\u250d" + "\u2501" * (table_width - 2) + "\u2511"
    title_row = "\u2502 " + title.center(table_width - 4) + " \u2502"
    separator = lines[0].replace("\u250d", "\u251d").replace("\u2511", "\u2525").replace("\u252f", "\u253f")

    glogger.opt(raw=True).info(
        "\n{}\n", title_border + "\n" + title_row + "\n" + separator + "\n" + "\n".join(lines[1:])
    )


from sipstuff.sip_account import SipAccount, SipCalleeAccount, SipCallerAccount
from sipstuff.sip_call import SipCalleeCall, SipCallerCall
from sipstuff.sip_callee import SipCallee
from sipstuff.sip_caller import SipCaller
from sipstuff.sip_endpoint import SipEndpoint
from sipstuff.sip_types import CallResult, SipCallError
from sipstuff.sipconfig import (
    AudioDeviceConfig,
    PauseConfig,
    PjsipConfig,
    PlaybackSequence,
    RecordingConfig,
    SipCalleeConfig,
    SipCallerConfig,
    SipEndpointConfig,
    SttConfig,
    TtsPlayConfig,
    VadConfig,
    WavPlayConfig,
)
from sipstuff.stt import SttError, transcribe_wav
from sipstuff.tts import TtsError, TtsModelInfo, clear_tts_cache, generate_wav, load_tts_model

__all__ = [
    "__version__",
    "make_sip_call",
    "AudioDeviceConfig",
    "SipCalleeAccount",
    "SipCalleeCall",
    "SipCalleeConfig",
    "SipCallerCall",
    "SipCallerConfig",
    "CallResult",
    "PjsipConfig",
    "RecordingConfig",
    "SipAccount",
    "SipCallerAccount",
    "SipCallError",
    "SipCallee",
    "SipCaller",
    "SipEndpointConfig",
    "SipEndpoint",
    "SttConfig",
    "SttError",
    "transcribe_wav",
    "TtsError",
    "TtsModelInfo",
    "clear_tts_cache",
    "generate_wav",
    "load_tts_model",
    "TtsPlayConfig",
    "PauseConfig",
    "PlaybackSequence",
    "VadConfig",
    "WavPlayConfig",
    "configure_logging",
    "print_banner",
]


def make_sip_call(
    server: str,
    user: str,
    password: str,
    destination: str,
    wav_file: str | Path | None = None,
    text: str | None = None,
    port: int = 5060,
    timeout: int = 60,
    transport: str = "udp",
    pre_delay: float = 0.0,
    post_delay: float = 0.0,
    inter_delay: float = 0.0,
    repeat: int = 1,
    tts_model: str = "de_DE-thorsten-high",
    play_audio: bool = False,
) -> bool:
    """Convenience function: register, call, play WAV or TTS, and hang up.

    One-shot wrapper around ``SipCaller`` that handles endpoint lifecycle
    and TTS temp-file cleanup automatically.  Provide exactly one of
    ``wav_file`` or ``text`` (not both, not neither).

    Args:
        server: PBX hostname or IP address.
        user: SIP extension / username.
        password: SIP authentication password.
        destination: Phone number or full SIP URI to call.
        wav_file: Path to the WAV file to play on answer.
            Mutually exclusive with ``text``.
        text: Text to synthesize via piper TTS and play on answer.
            Mutually exclusive with ``wav_file``.
        port: SIP server port (default: 5060).
        timeout: Maximum seconds to wait for the remote party to answer.
        transport: SIP transport protocol (``"udp"``, ``"tcp"``, or ``"tls"``).
        pre_delay: Seconds to wait after answer before starting playback.
        post_delay: Seconds to wait after playback completes before hanging up.
        inter_delay: Seconds of silence between WAV repeats.
        repeat: Number of times to play the WAV file.
        tts_model: Piper voice model name for TTS (auto-downloaded on
            first use).
        play_audio: If ``True``, play remote-party audio on the local
            sound device via ``sounddevice``.

    Returns:
        ``True`` if the call was answered and the WAV played (at least
        partially).  ``False`` if the call was not answered or timed out.

    Raises:
        SipCallError: On SIP registration, transport, or WAV playback errors.
        TtsError: If piper TTS generation fails.
        ValueError: If neither ``wav_file`` nor ``text`` is provided,
            or if both are provided.
    """
    if wav_file is None and text is None:
        raise ValueError("Provide either wav_file or text")
    if wav_file is not None and text is not None:
        raise ValueError("Provide either wav_file or text, not both")

    config = SipCallerConfig.from_config(
        overrides={
            "server": server,
            "user": user,
            "password": password,
            "port": port,
            "timeout": timeout,
            "transport": transport,
            "pre_delay": pre_delay,
            "post_delay": post_delay,
            "inter_delay": inter_delay,
            "repeat": repeat,
            "tts_model": tts_model,
        }
    )

    audio = AudioDeviceConfig(play_audio=play_audio)
    if text is not None:
        wav_play_cfg: WavPlayConfig | TtsPlayConfig = TtsPlayConfig(
            tts_text=text,
            tts_config=config.tts,
        )
        with SipCaller(config) as caller:
            return caller.make_call(destination, wav_play=wav_play_cfg, audio=audio)
    else:
        wav_play_cfg = WavPlayConfig(wav_path=str(wav_file))
        with SipCaller(config) as caller:
            return caller.make_call(destination, wav_play=wav_play_cfg, audio=audio)
