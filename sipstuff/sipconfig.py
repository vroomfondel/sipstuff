"""Standalone Pydantic configuration for the SIP caller.

Loads configuration from a YAML file, ``SIP_``-prefixed environment variables,
and/or direct Python overrides.  Independent of the main ``somestuff/config.py``
settings system so that ``sipstuff`` can be used as a self-contained package.

Configuration priority (highest first):
    1. ``overrides`` dict passed to ``SipEndpointConfig.from_config``
    2. ``SIP_*`` environment variables
    3. YAML config file
"""

import os
from pathlib import Path
from typing import Annotated, Any, Literal, Self

from pydantic import BaseModel, Field, model_validator
from ruamel.yaml import YAML


class SipConfig(BaseModel):
    """SIP account and server connection settings.

    Attributes:
        server: PBX hostname or IP address.
        port: SIP port (1–65535, default 5060).
        user: SIP extension / username.
        password: SIP authentication password.
        transport: SIP transport protocol (``"udp"``, ``"tcp"``, or ``"tls"``).
        srtp: SRTP media encryption mode
            (``"disabled"``, ``"optional"``, or ``"mandatory"``).
        tls_verify_server: Whether to verify the TLS server certificate.
        local_port: Local bind port for SIP (0 = auto-assigned).
    """

    server: str = Field(description="PBX hostname or IP address")
    port: int = Field(default=5060, ge=1, le=65535, description="SIP port")
    user: str = Field(description="SIP extension / username")
    password: str = Field(description="SIP password")
    transport: Literal["udp", "tcp", "tls"] = Field(default="udp", description="SIP transport protocol")
    srtp: Literal["disabled", "optional", "mandatory"] = Field(default="disabled", description="SRTP media encryption")
    tls_verify_server: bool = Field(default=False, description="Verify TLS server certificate")
    local_port: int = Field(default=0, ge=0, le=65535, description="Local bind port (0 = auto)")


class CallConfig(BaseModel):
    """Call timing and playback behaviour settings.

    Attributes:
        timeout: Maximum seconds to wait for the remote party to answer.
        pre_delay: Seconds to wait after answer before starting WAV playback.
        post_delay: Seconds to wait after playback completes before hanging up.
        inter_delay: Seconds of silence between WAV repeats (only when
            ``repeat > 1``).
        repeat: Number of times to play the WAV file.
        wait_for_silence: Seconds of continuous silence from the remote party
            to wait for before starting playback (0 = disabled).  Applied
            after ``pre_delay``.
    """

    timeout: int = Field(default=60, ge=1, le=600, description="Call timeout in seconds")
    pre_delay: float = Field(default=0.0, ge=0.0, le=30.0, description="Seconds to wait after answer before playback")
    post_delay: float = Field(default=0.0, ge=0.0, le=30.0, description="Seconds to wait after playback before hangup")
    inter_delay: float = Field(
        default=0.0, ge=0.0, le=30.0, description="Seconds to wait between WAV repeats (only when repeat > 1)"
    )
    repeat: int = Field(default=1, ge=1, le=100, description="Number of times to play the WAV file")
    wait_for_silence: float = Field(
        default=0.0,
        ge=0.0,
        le=10.0,
        description="Seconds of remote silence to wait for before playback (0 = disabled)",
    )

    # TODO: startrecording (wav-file, tx-, rx], und  _start_stt(live=true, whole_at_end=true, model, savepath, etc) usw. als CallConfig ?!

    # TODO: check if using some kind of "sequence" list here for generalizing call-flow e.g.:
    #   [,
    #   wait_for_silence,
    #   pause(3),
    #   play_wav,
    #   pause(2),
    #   play_wav,
    #   pause(2),
    #   tts("huhu haha"),
    #   pause(4),
    #   hangup ]
    #
    # => and each sequece-step is defined by a config (WavPlayConfig, STTConfig, TtsConfig)


class TtsConfig(BaseModel):
    """Piper TTS voice model and output settings.

    Attributes:
        model: Piper voice model name (auto-downloaded on first use).
        sample_rate: Resample TTS output to this rate in Hz.
            0 keeps the native piper rate (~22 050 Hz).  Use 8000 for
            narrowband SIP or 16000 for wideband.
        data_dir: Piper voice model directory (None = default).
    """

    model: str = Field(default="de_DE-thorsten-high", description="Piper voice model name")
    sample_rate: int = Field(default=0, ge=0, le=48000, description="Resample to this rate (0 = keep native)")
    data_dir: str | None = Field(default=None, description="Piper voice model directory (None = default)")
    use_cuda: bool = Field(default=False, description="Use CUDA GPU acceleration for Piper TTS")
    model_type: Literal["piper"] = Field(
        default="piper", description="Type of model - at the moment only piper supported."
    )


class NatConfig(BaseModel):
    """NAT traversal configuration (STUN, ICE, TURN, keepalive).

    All fields are optional and NAT traversal is disabled by default.
    See the ``sipstuff/README.md`` NAT Traversal section for usage guidance.

    Attributes:
        stun_servers: STUN servers for public IP discovery (``host:port``).
        stun_ignore_failure: Continue startup if STUN is unreachable.
        ice_enabled: Enable ICE connectivity checks for media.
        turn_enabled: Enable TURN relay (requires ``turn_server``).
        turn_server: TURN relay address (``host:port``).
        turn_username: TURN authentication username.
        turn_password: TURN authentication password.
        turn_transport: TURN transport protocol
            (``"udp"``, ``"tcp"``, or ``"tls"``).
        keepalive_sec: UDP keepalive interval in seconds (0 = disabled).
        public_address: Public IP to advertise in SDP ``c=`` and SIP
            Contact headers.  Overrides auto-detected local IP while the
            socket stays bound to the actual local interface.
    """

    stun_servers: list[str] = Field(default_factory=list, description="STUN servers (host:port)")
    stun_ignore_failure: bool = Field(default=True, description="Continue startup if STUN unreachable")
    ice_enabled: bool = Field(default=False, description="Enable ICE for media NAT traversal")
    turn_enabled: bool = Field(default=False, description="Enable TURN relay")
    turn_server: str = Field(default="", description="TURN server (host:port)")
    turn_username: str = Field(default="", description="TURN auth username")
    turn_password: str = Field(default="", description="TURN auth password")
    turn_transport: Literal["udp", "tcp", "tls"] = Field(default="udp", description="TURN transport")
    keepalive_sec: int = Field(default=0, ge=0, le=600, description="UDP keepalive interval (0 = disabled)")
    public_address: str = Field(
        default="",
        description="Public IP to advertise in SDP/Contact (e.g. K3s node IP). "
        "Overrides auto-detected local IP in signaling and media headers while keeping socket binding to the actual local interface.",
    )

    @model_validator(mode="after")
    def _check_turn(self) -> "NatConfig":
        """Validate that ``turn_server`` is set when ``turn_enabled`` is ``True``.

        Returns:
            The validated ``NatConfig`` instance.

        Raises:
            ValueError: If TURN is enabled without a server address.
        """
        if self.turn_enabled and not self.turn_server:
            raise ValueError("turn_enabled requires turn_server to be set")
        return self


class RecordingConfig(BaseModel):
    """Recording configuration for RX/TX audio capture and post-call mixing.

    Attributes:
        rx_path: Path for recording remote-party (RX) audio, or ``None`` to disable.
        tx_path: Path for recording local (TX) audio, or ``None`` to disable.
        mix_mode: Post-call mixing of RX and TX recordings.
        mix_output_path: Output path for the mixed file, or ``None`` for auto-generated.
    """

    rx_path: str | None = Field(default=None, description="Record remote-party (RX) audio to this WAV path")
    tx_path: str | None = Field(default=None, description="Record local (TX) audio to this WAV path")
    mix_mode: Literal["none", "mono", "stereo"] = Field(default="none", description="Post-call mix mode for RX+TX")
    mix_output_path: str | None = Field(default=None, description="Output path for mixed file (None = auto)")

    @model_validator(mode="after")
    def _check_mix(self) -> "RecordingConfig":
        """Validate that mixing requires both RX and TX paths.

        Returns:
            The validated ``RecordingConfig`` instance.

        Raises:
            ValueError: If ``mix_mode`` is not ``"none"`` but either ``rx_path``
                or ``tx_path`` is unset.
        """
        if self.mix_mode != "none" and (not self.rx_path or not self.tx_path):
            raise ValueError("mix_mode requires both rx_path and tx_path")
        return self


class AudioDeviceConfig(BaseModel):
    """Audio device and live streaming configuration.

    Combines PJSIP audio device choice with live streaming sink settings.

    Attributes:
        use_null_audio: Use null audio device for headless/container operation.
        null_capture: Null capture/mic device (TX direction). None inherits use_null_audio.
        null_playback: Null playback/speaker device (RX direction). None inherits use_null_audio.
        socket_path: Unix domain socket path for live PCM streaming.
        play_audio: Play remote-party audio on local sound device via sounddevice.
        play_rx: Route remote-party (RX) audio to output sinks.
        play_tx: Route local (TX) audio to output sinks.
    """

    use_null_audio: bool = Field(default=True, description="Use null audio device (headless mode)")
    audio_device: int | str | None = Field(
        default=None, description="Sounddevice output device (index or name substring). None = system default."
    )
    null_capture: bool | None = Field(
        default=None, description="Null capture/mic device (TX direction). None inherits use_null_audio."
    )
    null_playback: bool | None = Field(
        default=None, description="Null playback/speaker device (RX direction). None inherits use_null_audio."
    )
    socket_path: str | None = Field(default=None, description="Unix domain socket for live PCM streaming")
    play_audio: bool = Field(default=False, description="Play audio on local sound device")
    play_rx: bool = Field(default=True, description="Route RX audio to output sinks")
    play_tx: bool = Field(default=False, description="Route TX audio to output sinks")

    @model_validator(mode="after")
    def _resolve_null_devices(self) -> "AudioDeviceConfig":
        """Resolve ``null_capture``/``null_playback`` from ``use_null_audio`` when not explicitly set.

        Returns:
            The ``AudioDeviceConfig`` instance with ``null_capture`` and
            ``null_playback`` guaranteed to be concrete booleans.
        """
        if self.null_capture is None:
            self.null_capture = self.use_null_audio
        if self.null_playback is None:
            self.null_playback = self.use_null_audio
        return self


class WavPlayConfig(BaseModel):
    """Playback segment that plays a WAV file from disk.

    Attributes:
        type: Discriminator literal, always ``"wav"``.
        wav_path: Path to the WAV file to play.
        pause_before: Seconds of silence to insert before playback begins.
    """

    type: Literal["wav"] = "wav"
    wav_path: str = Field(description="Path to WAV file to play")
    pause_before: float = Field(default=0.0, ge=0.0, description="Pause seconds before playback")


class TtsPlayConfig(BaseModel):
    """Playback segment that synthesizes text via Piper TTS and plays the result.

    Attributes:
        type: Discriminator literal, always ``"tts"``.
        tts_text: Text to synthesize into speech.
        tts_config: TTS voice model settings to use; ``None`` inherits the
            caller-level ``TtsConfig``.
        pause_before: Seconds of silence to insert before playback begins.
    """

    type: Literal["tts"] = "tts"
    tts_text: str = Field(description="Text for TTS synthesis")
    tts_config: TtsConfig | None = Field(default=None, description="TTS configuration (None = use default)")
    pause_before: float = Field(default=0.0, ge=0.0, description="Pause seconds before playback")


class PauseConfig(BaseModel):
    """Playback segment that inserts a silent pause of a fixed duration.

    Attributes:
        type: Discriminator literal, always ``"pause"``.
        duration: Length of the pause in seconds.
    """

    type: Literal["pause"] = "pause"
    duration: float = Field(ge=0.0, description="Pause duration in seconds")


PlaybackSegment = Annotated[
    WavPlayConfig | TtsPlayConfig | PauseConfig,
    Field(discriminator="type"),
]


class PlaybackSequence(BaseModel):
    """Ordered sequence of heterogeneous playback segments.

    Each element of ``segments`` is one of ``WavPlayConfig``, ``TtsPlayConfig``,
    or ``PauseConfig``, discriminated by the ``type`` field.

    Attributes:
        segments: Ordered list of playback segments to execute in sequence.
    """

    segments: list[PlaybackSegment] = Field(default_factory=list)


class SttConfig(BaseModel):
    """Speech-to-text configuration for Whisper-based transcription.

    Unifies naming across ``call`` (``--stt-model``) and ``callee_live-transcribe``
    (``--whisper-model``) subcommands.

    Attributes:
        backend: STT backend engine (``"faster-whisper"`` or ``"openvino"``).
        model: Whisper model size (e.g. ``"base"``, ``"small"``, ``"large-v3"``).
        language: BCP-47 language code for transcription; ``None`` = auto-detect.
        device: Compute device (``"cpu"`` or ``"cuda"``).
        compute_type: Quantization type (e.g. ``"int8"``, ``"float16"``);
            ``None`` uses the backend default.
        data_dir: Model cache directory; ``None`` uses the backend default.
        live_transcribe: Enable live STT during a call via ``TranscriptionPort``.
    """

    backend: Literal["faster-whisper", "openvino"] = Field(
        default="faster-whisper", description="STT backend engine (faster-whisper or openvino)"
    )
    model: str = Field(default="base", description="Whisper model size")
    language: str | None = Field(default=None, description="Language code for transcription")
    device: Literal["cpu", "cuda", "openvino"] = Field(default="cpu", description="Compute device")
    compute_type: str | None = Field(default=None, description="Quantization type")
    data_dir: str | None = Field(default=None, description="Model cache directory (None = backend default)")
    live_transcribe: bool = Field(default=False, description="Enable live STT during call")
    model_type: Literal["whisper"] = Field(
        default="whisper", description="STT model type. At the moment only whisper available."
    )


class VadConfig(BaseModel):
    """Voice activity detection configuration for live transcription chunking.

    Unifies naming across ``call`` (``--vad-silence-threshold``) and
    ``callee_live-transcribe`` (``--silence-threshold``) subcommands.

    Attributes:
        silence_threshold: RMS threshold below which audio is considered silence.
        silence_trigger: Seconds of continuous silence to trigger a chunk boundary.
        max_chunk: Maximum seconds per transcription chunk.
        min_chunk: Minimum seconds per transcription chunk.
    """

    silence_threshold: float = Field(default=0.01, description="RMS silence threshold")
    silence_trigger: float = Field(default=0.3, description="Seconds of silence to trigger chunk")
    max_chunk: float = Field(default=5.0, description="Max seconds per chunk")
    min_chunk: float = Field(default=0.5, description="Min seconds per chunk")

    def to_vad_buffer_kwargs(self) -> dict[str, float]:
        """Map this config to ``VADAudioBuffer`` constructor keyword arguments.

        Returns:
            A dict with keys ``silence_threshold``, ``silence_trigger_sec``,
            ``max_duration_sec``, and ``min_duration_sec`` ready to be
            unpacked into ``VADAudioBuffer(**kwargs)``.
        """
        return {
            "silence_threshold": self.silence_threshold,
            "silence_trigger_sec": self.silence_trigger,
            "max_duration_sec": self.max_chunk,
            "min_duration_sec": self.min_chunk,
        }


class PjsipConfig(BaseModel):
    """PJSIP engine log verbosity configuration.

    Falls back to ``PJSIP_LOG_LEVEL`` / ``PJSIP_CONSOLE_LEVEL`` environment
    variables when not set explicitly.

    Attributes:
        log_level: PJSIP log verbosity routed through loguru (0–6).
        console_level: PJSIP native console output level (0–6).
    """

    log_level: int = Field(default=3, ge=0, le=6, description="PJSIP log verbosity (loguru writer)")
    console_level: int = Field(default=4, ge=0, le=6, description="PJSIP native console output level")

    @model_validator(mode="before")
    @classmethod
    def _apply_env_defaults(cls, data: Any) -> Any:
        """Apply ``PJSIP_LOG_LEVEL`` / ``PJSIP_CONSOLE_LEVEL`` environment variable defaults.

        Only sets values that are not already present in ``data``, so explicit
        construction always takes precedence over the environment.

        Args:
            data: Raw input data passed to the model validator.  Non-dict
                values are returned unchanged.

        Returns:
            The (possibly modified) input data with environment variable
            defaults merged in.
        """
        if not isinstance(data, dict):
            return data
        if "log_level" not in data:
            env_val = os.getenv("PJSIP_LOG_LEVEL")
            if env_val is not None:
                data["log_level"] = int(env_val)
        if "console_level" not in data:
            env_val = os.getenv("PJSIP_CONSOLE_LEVEL")
            if env_val is not None:
                data["console_level"] = int(env_val)
        return data


class SipEndpointConfig(BaseModel):
    """Top-level SIP endpoint configuration aggregating infrastructure sub-configs.

    Accepts either a nested dict (``{"sip": {...}, "nat": {...}}``) or a
    flat dict with SIP field names at the top level.  The
    ``_flatten_sip_fields`` validator reshapes flat dicts into the nested
    form before Pydantic validation.

    Call-level defaults (``call``, ``tts``, ``recording``, ``wav_play``,
    ``stt``, ``vad``) live in ``SipCallerConfig``.

    Attributes:
        sip: SIP account and server connection settings.
        nat: NAT traversal settings (disabled by default).
        pjsip: PJSIP engine log verbosity settings.
        audio: Audio device and streaming settings.
    """

    sip: SipConfig
    nat: NatConfig = NatConfig()
    pjsip: PjsipConfig = Field(default_factory=PjsipConfig)
    audio: AudioDeviceConfig = Field(default_factory=AudioDeviceConfig)

    @model_validator(mode="before")
    @classmethod
    def _flatten_sip_fields(cls, data: Any) -> Any:
        """Reshape a flat dict into the nested ``{sip: …, nat: …}`` form.

        Allows callers to pass SIP fields (``server``, ``port``, …) at the
        top level instead of nesting them under a ``"sip"`` key.  NAT fields
        are grouped under ``"nat"``.

        Args:
            data: Raw input data (dict or other).  Non-dict values are
                returned unchanged.

        Returns:
            The (possibly restructured) dict ready for Pydantic validation.
        """
        if not isinstance(data, dict):
            return data
        # Already has nested 'sip' key — use as-is
        if "sip" in data:
            return data
        # Try to build from flat keys (CLI / env var usage)
        sip_keys = {"server", "port", "user", "password", "transport", "srtp", "tls_verify_server", "local_port"}
        if sip_keys & set(data.keys()):
            sip_data = {k: data.pop(k) for k in list(data.keys()) if k in sip_keys}
            nat_keys = {
                "stun_servers",
                "stun_ignore_failure",
                "ice_enabled",
                "turn_enabled",
                "turn_server",
                "turn_username",
                "turn_password",
                "turn_transport",
                "keepalive_sec",
                "public_address",
            }
            nat_data = {k: data.pop(k) for k in list(data.keys()) if k in nat_keys}
            data["sip"] = sip_data
            if nat_data:
                data["nat"] = nat_data
        return data

    @staticmethod
    def _build_config_data(
        config_path: str | Path | None = None,
        overrides: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Build a merged config dict from YAML file, environment variables, and overrides.

        Sources are applied in order (later wins):
            1. YAML config file (if ``config_path`` is given and exists).
            2. ``SIP_*`` environment variables (see ``sipstuff/README.md``).
            3. ``overrides`` dict (e.g. from CLI arguments).

        Args:
            config_path: Path to a YAML configuration file.  ``None`` skips
                file loading.
            overrides: Key/value overrides applied on top of file and env
                values.  Keys may be flat SIP field names (``"server"``,
                ``"timeout"``, ``"piper_model"``, …) or NAT field names
                (``"stun_servers"``, ``"ice_enabled"``, …).

        Returns:
            A dict ready to be passed to a config class constructor.
        """
        data: dict[str, Any] = {}

        # 1. YAML file
        if config_path is not None:
            path = Path(config_path)
            if path.is_file():
                loaded = YAML().load(path)
                if isinstance(loaded, dict):
                    data = loaded

        # 2. Environment variables (SIP_ prefix)
        env_map = {
            "SIP_SERVER": ("sip", "server"),
            "SIP_PORT": ("sip", "port"),
            "SIP_USER": ("sip", "user"),
            "SIP_PASSWORD": ("sip", "password"),
            "SIP_TRANSPORT": ("sip", "transport"),
            "SIP_SRTP": ("sip", "srtp"),
            "SIP_TLS_VERIFY_SERVER": ("sip", "tls_verify_server"),
            "SIP_LOCAL_PORT": ("sip", "local_port"),
            "SIP_TIMEOUT": ("call", "timeout"),
            "SIP_PRE_DELAY": ("call", "pre_delay"),
            "SIP_POST_DELAY": ("call", "post_delay"),
            "SIP_INTER_DELAY": ("call", "inter_delay"),
            "SIP_REPEAT": ("call", "repeat"),
            "SIP_WAIT_FOR_SILENCE": ("call", "wait_for_silence"),
            "SIP_TTS_MODEL": ("tts", "model"),
            "SIP_TTS_SAMPLE_RATE": ("tts", "sample_rate"),
            "SIP_STUN_SERVERS": ("nat", "stun_servers"),
            "SIP_STUN_IGNORE_FAILURE": ("nat", "stun_ignore_failure"),
            "SIP_ICE_ENABLED": ("nat", "ice_enabled"),
            "SIP_TURN_ENABLED": ("nat", "turn_enabled"),
            "SIP_TURN_SERVER": ("nat", "turn_server"),
            "SIP_TURN_USERNAME": ("nat", "turn_username"),
            "SIP_TURN_PASSWORD": ("nat", "turn_password"),
            "SIP_TURN_TRANSPORT": ("nat", "turn_transport"),
            "SIP_KEEPALIVE_SEC": ("nat", "keepalive_sec"),
            "SIP_PUBLIC_ADDRESS": ("nat", "public_address"),
            "PJSIP_LOG_LEVEL": ("pjsip", "log_level"),
            "PJSIP_CONSOLE_LEVEL": ("pjsip", "console_level"),
            "SIP_NULL_AUDIO": ("audio", "use_null_audio"),
            "SIP_NULL_CAPTURE": ("audio", "null_capture"),
            "SIP_NULL_PLAYBACK": ("audio", "null_playback"),
            "SIP_PLAY_RX": ("audio", "play_rx"),
            "SIP_PLAY_TX": ("audio", "play_tx"),
            "SIP_AUDIO_DEVICE": ("audio", "audio_device"),
            "SIP_STT_BACKEND": ("stt", "backend"),
            "SIP_STT_MODEL": ("stt", "model"),
            "SIP_STT_LANGUAGE": ("stt", "language"),
            "SIP_STT_DEVICE": ("stt", "device"),
            "SIP_VAD_SILENCE_THRESHOLD": ("vad", "silence_threshold"),
            "SIP_VAD_SILENCE_TRIGGER": ("vad", "silence_trigger"),
            "SIP_VAD_MAX_CHUNK": ("vad", "max_chunk"),
            "SIP_VAD_MIN_CHUNK": ("vad", "min_chunk"),
            "SIP_LIVE_TRANSCRIBE": ("stt", "live_transcribe"),
            "SIP_STT_DATA_DIR": ("stt", "data_dir"),
            "SIP_STT_LIVE_MODEL": ("stt_live", "model"),
            "SIP_STT_LIVE_BACKEND": ("stt_live", "backend"),
            "SIP_STT_LIVE_DEVICE": ("stt_live", "device"),
            "SIP_STT_LIVE_LANGUAGE": ("stt_live", "language"),
            "SIP_STT_LIVE_DATA_DIR": ("stt_live", "data_dir"),
            "SIP_TTS_CUDA": ("tts", "use_cuda"),
            "SIP_TTS_DATA_DIR": ("tts", "data_dir"),
            "SIP_AUTO_ANSWER": ("callee", "auto_answer"),
            "SIP_ANSWER_DELAY": ("callee", "answer_delay"),
        }
        for env_key, (section, field) in env_map.items():
            val = os.getenv(env_key)
            if val is not None:
                if env_key == "SIP_STUN_SERVERS":
                    data.setdefault(section, {})[field] = [s.strip() for s in val.split(",") if s.strip()]
                else:
                    data.setdefault(section, {})[field] = val

        # 3. Overrides from caller (e.g. CLI args)
        nat_override_keys = {
            "stun_servers",
            "stun_ignore_failure",
            "ice_enabled",
            "turn_enabled",
            "turn_server",
            "turn_username",
            "turn_password",
            "turn_transport",
            "keepalive_sec",
            "public_address",
        }
        if overrides:
            for key, val in overrides.items():
                if val is None:
                    continue
                if key in (
                    "server",
                    "port",
                    "user",
                    "password",
                    "transport",
                    "srtp",
                    "tls_verify_server",
                    "local_port",
                ):
                    data.setdefault("sip", {})[key] = val
                elif key in ("timeout", "pre_delay", "post_delay", "inter_delay", "repeat", "wait_for_silence"):
                    data.setdefault("call", {})[key] = val
                elif key == "piper_model":
                    data.setdefault("tts", {})["model"] = val
                elif key == "tts_sample_rate":
                    data.setdefault("tts", {})["sample_rate"] = val
                elif key == "live_transcribe":
                    data.setdefault("stt", {})[key] = val
                elif key == "tts_cuda":
                    data.setdefault("tts", {})["use_cuda"] = val
                elif key == "tts_data_dir":
                    data.setdefault("tts", {})["data_dir"] = val
                elif key == "stt_data_dir":
                    data.setdefault("stt", {})["data_dir"] = val
                elif key == "stt_model":
                    data.setdefault("stt", {})["model"] = val
                elif key == "stt_backend":
                    data.setdefault("stt", {})["backend"] = val
                elif key == "stt_device":
                    data.setdefault("stt", {})["device"] = val
                elif key == "stt_language":
                    data.setdefault("stt", {})["language"] = val
                elif key == "stt_live_model":
                    data.setdefault("stt_live", {})["model"] = val
                elif key == "stt_live_backend":
                    data.setdefault("stt_live", {})["backend"] = val
                elif key == "stt_live_device":
                    data.setdefault("stt_live", {})["device"] = val
                elif key == "stt_live_language":
                    data.setdefault("stt_live", {})["language"] = val
                elif key == "stt_live_data_dir":
                    data.setdefault("stt_live", {})["data_dir"] = val
                elif key == "vad_silence_threshold":
                    data.setdefault("vad", {})["silence_threshold"] = val
                elif key == "vad_silence_trigger":
                    data.setdefault("vad", {})["silence_trigger"] = val
                elif key == "vad_max_chunk":
                    data.setdefault("vad", {})["max_chunk"] = val
                elif key == "vad_min_chunk":
                    data.setdefault("vad", {})["min_chunk"] = val
                elif key in nat_override_keys:
                    data.setdefault("nat", {})[key] = val

        return data

    @classmethod
    def from_config(
        cls,
        config_path: str | Path | None = None,
        overrides: dict[str, Any] | None = None,
    ) -> Self:
        """Load a ``SipEndpointConfig`` by merging YAML, environment variables, and overrides.

        Sources are applied in order (later wins):
            1. YAML config file (if ``config_path`` is given and exists).
            2. ``SIP_*`` environment variables (see ``sipstuff/README.md``).
            3. ``overrides`` dict (e.g. from CLI arguments).

        Args:
            config_path: Path to a YAML configuration file.  ``None`` skips
                file loading.
            overrides: Key/value overrides applied on top of file and env
                values.  Keys may be flat SIP field names (``"server"``,
                ``"timeout"``, ``"piper_model"``, …) or NAT field names
                (``"stun_servers"``, ``"ice_enabled"``, …).

        Returns:
            A fully validated config instance of the calling class.

        Raises:
            pydantic.ValidationError: If required fields are missing or
                values fail validation.
        """
        data = cls._build_config_data(config_path, overrides)
        return cls(**data)


class SipCallerConfig(SipEndpointConfig):
    """SIP caller configuration — adds call-level defaults to endpoint config.

    Attributes:
        call: Call timing and playback behaviour (defaults apply).
        tts: Piper TTS voice model settings.
        recording: Recording configuration for RX/TX capture.
        wav_play: Audio playback source (WAV file or TTS).
        stt: Speech-to-text configuration.
        vad: Voice activity detection configuration.
    """

    call: CallConfig = CallConfig()
    tts: TtsConfig = TtsConfig()
    recording: RecordingConfig = Field(default_factory=RecordingConfig)
    wav_play: WavPlayConfig | TtsPlayConfig | None = Field(
        default=None, description="Audio playback source (WAV or TTS)"
    )
    stt: SttConfig = Field(default_factory=SttConfig)
    stt_live: SttConfig | None = Field(default=None, description="STT config for live transcription (None = use stt)")
    vad: VadConfig = Field(default_factory=VadConfig)

    @model_validator(mode="before")
    @classmethod
    def _flatten_caller_fields(cls, data: Any) -> Any:
        """Reshape flat call/tts keys into the nested ``{call: …, tts: …}`` form.

        Allows callers to pass ``CallConfig`` fields (``timeout``, ``pre_delay``,
        …) and TTS shorthand keys (``piper_model``, ``tts_sample_rate``) at the
        top level instead of nesting them.  No-ops when ``"call"`` is already
        present in ``data``.

        Args:
            data: Raw input data passed to the model validator.  Non-dict
                values are returned unchanged.

        Returns:
            The (possibly restructured) dict ready for Pydantic validation.
        """
        if not isinstance(data, dict):
            return data
        if "call" in data:
            return data
        call_keys = {"timeout", "pre_delay", "post_delay", "inter_delay", "repeat", "wait_for_silence"}
        call_data = {k: data.pop(k) for k in list(data.keys()) if k in call_keys}
        tts_data: dict[str, Any] = {}
        for k in list(data.keys()):
            if k == "piper_model":
                tts_data["model"] = data.pop(k)
            elif k == "tts_sample_rate":
                tts_data["sample_rate"] = data.pop(k)
        if call_data:
            data["call"] = call_data
        if tts_data:
            data["tts"] = tts_data
        return data

    @model_validator(mode="after")
    def _sync_tts_to_wav_play(self) -> "SipCallerConfig":
        """Propagate the caller-level ``TtsConfig`` into ``TtsPlayConfig.tts_config`` when unset.

        Ensures that a ``TtsPlayConfig`` created without an explicit
        ``tts_config`` inherits the top-level ``tts`` settings.

        Returns:
            The ``SipCallerConfig`` instance with ``wav_play.tts_config``
            populated where applicable.
        """
        if isinstance(self.wav_play, TtsPlayConfig) and self.wav_play.tts_config is None:
            self.wav_play = self.wav_play.model_copy(update={"tts_config": self.tts})
        return self

    @model_validator(mode="after")
    def _resolve_stt_live(self) -> "SipCallerConfig":
        """Resolve ``stt_live`` from ``stt`` when not explicitly set.

        Ensures ``stt_live`` is always a concrete ``SttConfig`` after
        construction — callers never need ``if None`` checks.

        Returns:
            The ``SipCallerConfig`` instance with ``stt_live`` guaranteed
            to be a concrete ``SttConfig``.
        """
        if self.stt_live is None:
            self.stt_live = self.stt.model_copy()
        return self


class SipCalleeConfig(SipEndpointConfig):
    """Callee (incoming call) behaviour configuration.

    Extends ``SipEndpointConfig`` with callee-specific fields (auto-answer
    behaviour) and call-level defaults (TTS, STT, VAD) so that
    ``from_config()`` routes YAML / env / override values for these sections
    correctly — mirroring the pattern used by ``SipCallerConfig``.

    Attributes:
        auto_answer: Whether to automatically answer incoming calls.
        answer_delay: Seconds to wait before answering (allows ring tone).
        tts: Piper TTS voice model settings.
        stt: Speech-to-text configuration.
        vad: Voice activity detection configuration.
    """

    auto_answer: bool = Field(default=True, description="Auto-answer incoming calls")
    answer_delay: float = Field(default=1.0, ge=0.0, description="Seconds before answering")
    tts: TtsConfig = Field(default_factory=TtsConfig)
    stt: SttConfig = Field(default_factory=SttConfig)
    stt_live: SttConfig | None = Field(default=None, description="STT config for live transcription (None = use stt)")
    vad: VadConfig = Field(default_factory=VadConfig)
    # TODO: consider wait_for_silence for callee — requires SilenceDetector hookup in CalleeCall.on_media_active

    @model_validator(mode="before")
    @classmethod
    def _flatten_callee_fields(cls, data: Any) -> Any:
        """Reshape flat callee/tts keys into the nested form.

        Handles:
        - ``callee.auto_answer`` / ``callee.answer_delay`` → top-level
        - ``piper_model`` → ``tts.model``, ``tts_sample_rate`` → ``tts.sample_rate``

        Args:
            data: Raw input data passed to the model validator.

        Returns:
            The (possibly restructured) dict ready for Pydantic validation.
        """
        if not isinstance(data, dict):
            return data

        # Promote callee section fields to top level
        callee_section = data.pop("callee", None)
        if isinstance(callee_section, dict):
            for key in ("auto_answer", "answer_delay"):
                if key in callee_section and key not in data:
                    data[key] = callee_section[key]

        # Flatten TTS shorthand keys (same as SipCallerConfig._flatten_caller_fields)
        tts_data: dict[str, Any] = {}
        for k in list(data.keys()):
            if k == "piper_model":
                tts_data["model"] = data.pop(k)
            elif k == "tts_sample_rate":
                tts_data["sample_rate"] = data.pop(k)
        if tts_data:
            if "tts" in data and isinstance(data["tts"], dict):
                data["tts"].update(tts_data)
            else:
                data["tts"] = tts_data

        return data

    @model_validator(mode="after")
    def _resolve_stt_live(self) -> "SipCalleeConfig":
        """Resolve ``stt_live`` from ``stt`` when not explicitly set.

        Ensures ``stt_live`` is always a concrete ``SttConfig`` after
        construction — callers never need ``if None`` checks.

        Returns:
            The ``SipCalleeConfig`` instance with ``stt_live`` guaranteed
            to be a concrete ``SttConfig``.
        """
        if self.stt_live is None:
            self.stt_live = self.stt.model_copy()
        return self

    @classmethod
    def from_config(
        cls,
        config_path: str | Path | None = None,
        overrides: dict[str, Any] | None = None,
    ) -> "SipCalleeConfig":
        """Load a ``SipCalleeConfig`` by merging YAML, environment variables, and overrides.

        Uses ``_build_config_data`` for the shared YAML → env → override
        pipeline, then extracts callee-specific override keys
        (``auto_answer``, ``answer_delay``) and merges them into the data
        dict before constructing the config.

        Args:
            config_path: Path to a YAML configuration file.
            overrides: Key/value overrides.  Callee-specific keys
                (``"auto_answer"``, ``"answer_delay"``) are extracted and
                applied to the callee fields; remaining keys are forwarded
                through ``_build_config_data``.

        Returns:
            A fully validated ``SipCalleeConfig`` instance.
        """
        callee_keys = {"auto_answer", "answer_delay"}
        callee_overrides: dict[str, Any] = {}
        remaining: dict[str, Any] | None = None

        if overrides:
            remaining = {}
            for k, v in overrides.items():
                if k in callee_keys:
                    callee_overrides[k] = v
                else:
                    remaining[k] = v

        data = SipEndpointConfig._build_config_data(config_path=config_path, overrides=remaining)
        data.update(callee_overrides)
        return cls(**data)
