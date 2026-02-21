"""PJSUA2 endpoint lifecycle — SipEndpoint base class, SipCaller, log writer, helpers.

``SipEndpoint`` is the abstract base class for PJSUA2 endpoint lifecycle
management (create → init → start → destroy).  Both ``SipCaller`` (outgoing
calls) and ``SipCallee`` (incoming calls) inherit from it.

Note:
    Native PJSIP log output is captured by a ``pj.LogWriter`` subclass and
    forwarded to loguru (``classname="pjsip"``).  Two levels control verbosity:

    ``pjsip_log_level`` -- verbosity passed to the loguru writer
    (0 = none ... 6 = trace, default: 3).

    ``pjsip_console_level`` -- native PJSIP console output that goes
    directly to stdout in addition to the writer (default: 4, matching
    PJSIP's own default).

    Resolution order (highest priority first):

    1. ``config.pjsip.log_level`` / ``config.pjsip.console_level``
    2. Environment variable (``PJSIP_LOG_LEVEL``, ``PJSIP_CONSOLE_LEVEL``)
    3. ``PjsipConfig`` defaults (3 / 4)

    Set ``PJSIP_CONSOLE_LEVEL=0`` to suppress native console output and
    rely solely on the loguru writer.
"""

from __future__ import annotations

import socket
import time
from types import TracebackType

import pjsua2 as pj
from loguru import logger

from sipstuff.sip_types import PJTransports
from sipstuff.sipconfig import SipEndpointConfig


class _PjLogWriter(pj.LogWriter):  # type: ignore[misc]
    """PJSUA2 ``LogWriter`` subclass that routes native PJSIP log output through loguru.

    Maps PJSIP log levels (1 = error … 6 = trace) to loguru level names
    and emits each message via a loguru logger bound to ``classname="pjsip"``.
    """

    _PJ_TO_LOGURU = {1: "ERROR", 2: "WARNING", 3: "INFO", 4: "DEBUG", 5: "TRACE", 6: "TRACE"}

    def __init__(self) -> None:
        """Initialise the log writer, binding a loguru logger and an empty message buffer."""
        super().__init__()
        self._log = logger.bind(classname="pjsip")
        self._buffer: list[str] = []

    def write(self, entry: "pj.LogEntry") -> None:
        """Forward a single PJSIP log entry to loguru and capture it.

        Args:
            entry: PJSIP log entry containing ``level`` (int) and ``msg`` (str).
        """
        level = self._PJ_TO_LOGURU.get(entry.level, "DEBUG")
        msg = entry.msg.rstrip("\n")
        if msg:
            self._buffer.append(msg)
            self._log.log(level, "{}", msg)


class SipEndpoint:
    """Base class for PJSUA2 endpoint lifecycle (create → init → start → destroy).

    Manages the shared PJSIP scaffolding that both ``SipCaller`` (outgoing)
    and ``SipCallee`` (incoming) need: endpoint creation, log writer setup,
    STUN configuration, transport creation, audio device setup, and teardown.

    Subclasses must override ``_create_account`` to create their specific
    account type (``SipAccount`` for caller, ``CalleeAccount`` for callee).

    Args:
        config: SIP caller configuration (from ``SipEndpointConfig.from_config``).
            PJSIP log levels are read from ``config.pjsip``, audio device
            settings from ``config.audio``.
    """

    def __init__(self, config: SipEndpointConfig) -> None:
        """Store the configuration and initialise all instance variables to their idle state.

        Args:
            config: Endpoint configuration produced by ``SipEndpointConfig.from_config()``.
                PJSIP log levels are read from ``config.pjsip``; audio device settings
                from ``config.audio``.
        """
        self.config: SipEndpointConfig = config
        self._ep: pj.Endpoint | None = None
        self._transport_id: int | None = None
        self._orphaned_ports: list[pj.AudioMedia] = []
        self._pj_log_writer: _PjLogWriter | None = None
        self._log = logger.bind(classname=type(self).__name__)

    @staticmethod
    def _local_address_for(remote_host: str, remote_port: int = 5060) -> str:
        """Return the local IP address that the OS would use to reach *remote_host*.

        Opens a UDP socket and connects (no data sent) so the kernel selects
        the correct source address based on the routing table.  This avoids
        multi-homed hosts advertising the wrong IP in SDP.

        Args:
            remote_host: Hostname or IP of the remote SIP server.
            remote_port: Port on the remote host (default 5060).

        Returns:
            Local IP address string selected by the OS routing table.
        """
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect((remote_host, remote_port))
            return str(s.getsockname()[0])

    def __enter__(self) -> "SipEndpoint":
        """Start the PJSUA2 endpoint and return ``self`` for use as a context manager."""
        self.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        """Shut down the PJSUA2 endpoint on context-manager exit."""
        self.stop()

    def start(self) -> None:
        """Initialize the PJSUA2 endpoint, create a SIP transport, and register an account.

        Performs local-IP detection via ``_local_address_for()`` and binds both
        SIP signaling and RTP media transports to that address.  Configures
        STUN/ICE/TURN/keepalive per ``self.config.nat`` and SRTP per
        ``self.config.sip.srtp``.

        At the end, calls ``_create_account()`` which subclasses must override.

        Raises:
            SipCallError: If pjsua2 is unavailable or account registration fails.
        """
        self._ep = pj.Endpoint()
        self._ep.libCreate()

        # Determine the local IP that routes to the SIP server so both
        # signaling and media (RTP) sockets bind to the correct interface.
        local_ip = self._local_address_for(self.config.sip.server, self.config.sip.port)
        self._log.info(f"Local address for SIP server: {local_ip}")

        ep_cfg: pj.EpConfig = pj.EpConfig()
        ep_cfg.logConfig.level = self.config.pjsip.log_level
        ep_cfg.logConfig.consoleLevel = self.config.pjsip.console_level
        self._pj_log_writer = _PjLogWriter()
        ep_cfg.logConfig.writer = self._pj_log_writer
        ep_cfg.logConfig.decor = 0  # skip PJSIP's own timestamp/prefix — loguru adds its own

        # STUN servers (endpoint-level)
        if self.config.nat.stun_servers:
            self._log.info(
                f"STUN servers: {self.config.nat.stun_servers} (ignore failure: {self.config.nat.stun_ignore_failure})"
            )
            for srv in self.config.nat.stun_servers:
                ep_cfg.uaConfig.stunServer.append(srv)
            ep_cfg.uaConfig.stunIgnoreFailure = self.config.nat.stun_ignore_failure

        ep_cfg.medConfig.clockRate = 16000

        self._ep.libInit(ep_cfg)

        # Transport(s) — also bound to the correct interface
        tp_cfg = pj.TransportConfig()
        tp_cfg.port = self.config.sip.local_port
        tp_cfg.boundAddress = local_ip
        if self.config.nat.public_address:
            tp_cfg.publicAddress = self.config.nat.public_address
            self._log.info(f"Public address override: {self.config.nat.public_address} (local bind: {local_ip})")

        tp_enum: PJTransports = PJTransports[f"TRANSPORT_{self.config.sip.transport.upper()}"]
        if tp_enum == PJTransports.TRANSPORT_TLS:
            tls_cfg: pj.TlsConfig = pj.TlsConfig()
            tls_cfg.method = pj.PJSIP_TLSV1_2_METHOD
            if not self.config.sip.tls_verify_server:
                tls_cfg.verifyServer = False
                tls_cfg.verifyClient = False
            tp_cfg.tlsConfig = tls_cfg

        self._transport_id = self._ep.transportCreate(tp_enum.value, tp_cfg)

        self._ep.libStart()

        # Audio device setup
        _null_capture: bool = self.config.audio.null_capture  # type: ignore[assignment]  # always bool after validator
        _null_playback: bool = self.config.audio.null_playback  # type: ignore[assignment]  # always bool after validator
        if _null_capture and _null_playback:
            self._ep.audDevManager().setNullDev()
            self._log.info("PJSUA2 endpoint started (null capture + null playback)")
        elif _null_capture:
            # Null capture, real playback
            self._ep.audDevManager().setNullDev()
            self._ep.audDevManager().setPlaybackDev(-2)  # PJMEDIA_AUD_DEFAULT_PLAYBACK_DEV
            self._log.info("PJSUA2 endpoint started (null capture, real playback)")
        elif _null_playback:
            # Real capture, null playback
            self._ep.audDevManager().setNullDev()
            self._ep.audDevManager().setCaptureDev(-1)  # PJMEDIA_AUD_DEFAULT_CAPTURE_DEV
            self._log.info("PJSUA2 endpoint started (real capture, null playback)")
        else:
            self._log.info("PJSUA2 endpoint started (real capture + real playback)")

        # Subclass hook — create and register the SIP account
        self._create_account(local_ip)

        # Give registration a moment
        time.sleep(1)

    def _create_account(self, local_ip: str) -> None:
        """Subclass hook — create and register the SIP account.

        Called at the end of ``start()`` after the endpoint and transport
        are initialised.  Must be overridden by subclasses.

        Args:
            local_ip: Local IP address to bind media transports to.

        Raises:
            NotImplementedError: If the subclass does not override this method.
        """
        raise NotImplementedError

    def stop(self) -> None:
        """Shut down the PJSUA2 endpoint and release all resources.

        Shuts down the SIP account (via ``_shutdown_account``), clears
        orphaned ports while the conference bridge is still alive, calls
        ``libDestroy``, and releases the log writer.  Safe to call multiple
        times.
        """
        self._shutdown_account()

        # Destroy orphaned ports while the conference bridge
        # (owned by the endpoint) is still alive.
        self._orphaned_ports.clear()

        if self._ep is not None:
            try:
                self._ep.libDestroy()
            except Exception:
                pass
            self._ep = None

        # Release log writer after endpoint is gone
        self._pj_log_writer = None

        self._log.info("PJSUA2 endpoint stopped")

    def _shutdown_account(self) -> None:
        """Shut down the SIP account.  Override in subclasses that hold an account reference."""

    def get_pjsip_logs(self) -> list[str]:
        """Return all PJSIP log messages captured since the endpoint was started.

        Returns:
            A copy of the internal message buffer, or an empty list if the log
            writer has not been initialised yet.
        """
        if self._pj_log_writer is not None:
            return list(self._pj_log_writer._buffer)
        return []
