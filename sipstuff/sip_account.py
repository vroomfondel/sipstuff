"""PJSUA2 Account subclass with SIP registration and incoming call support.

Encapsulates credential setup, media transport binding, SRTP, ICE/TURN,
and UDP keepalive configuration.  Provides a common ``SipAccount`` base class
with shared config-building and registration logic.  ``SipCallerAccount`` and
``SipCalleeAccount`` add their specific incoming-call handling.

``SipAccount.build_pj_account_config()`` constructs a ``pj.AccountConfig``
from ``SipEndpointConfig`` and is shared by both subclasses to avoid
duplicating URI, credential, SRTP, and NAT configuration logic.
"""

import threading
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, TypeAlias

from loguru import logger

from sipstuff.sipconfig import AudioDeviceConfig, SipEndpointConfig

if TYPE_CHECKING:
    from sipstuff.sip_call import SipCalleeCall, SipCall, SipCallerCall

import pjsua2 as pj


class SipAccount(pj.Account):  # type: ignore[misc]
    """PJSUA2 Account base class with shared registration and config logic.

    Builds a ``pj.AccountConfig`` from ``SipEndpointConfig`` and creates the
    account in a single constructor call.  Subclasses override ``onIncomingCall``
    for caller vs callee behaviour.

    Args:
        config: SIP endpoint configuration.
        transport_id: PJSUA2 transport ID returned by ``Endpoint.transportCreate``.
        local_ip: Local IP address to bind media transports to.
    """

    @staticmethod
    def build_pj_account_config(config: SipEndpointConfig, transport_id: Any, local_ip: str) -> "pj.AccountConfig":
        """Build a fully configured ``pj.AccountConfig`` from endpoint config.

        Handles URI construction, credentials, media transport binding, SRTP,
        and NAT/ICE/TURN/keepalive setup.

        Args:
            config: SIP endpoint configuration.
            transport_id: PJSUA2 transport ID returned by ``Endpoint.transportCreate``.
            local_ip: Local IP address to bind media transports to.

        Returns:
            A ready-to-use ``pj.AccountConfig``.
        """
        log = logger.bind(classname="SipAccount")
        acfg = pj.AccountConfig()
        scheme = "sips" if config.sip.transport == "tls" else "sip"
        tp_param = f";transport={config.sip.transport}"
        acfg.idUri = f"{scheme}:{config.sip.user}@{config.sip.server}"
        acfg.regConfig.registrarUri = f"{scheme}:{config.sip.server}:{config.sip.port}{tp_param}"
        acfg.sipConfig.transportId = transport_id

        cred = pj.AuthCredInfo("digest", "*", config.sip.user, 0, config.sip.password)
        acfg.sipConfig.authCreds.append(cred)

        # Bind RTP/media sockets to the correct interface (avoids SDP
        # advertising the wrong IP on multi-homed hosts).
        acfg.mediaConfig.transportConfig.boundAddress = local_ip
        if config.nat.public_address:
            acfg.mediaConfig.transportConfig.publicAddress = config.nat.public_address

        # SRTP media encryption
        srtp_map = {
            "disabled": pj.PJMEDIA_SRTP_DISABLED,
            "optional": pj.PJMEDIA_SRTP_OPTIONAL,
            "mandatory": pj.PJMEDIA_SRTP_MANDATORY,
        }
        acfg.mediaConfig.srtpUse = srtp_map[config.sip.srtp]
        acfg.mediaConfig.srtpSecureSignaling = 0 if config.sip.srtp == "disabled" else 1
        if config.sip.srtp != "disabled":
            log.info(f"SRTP: {config.sip.srtp}")

        # NAT traversal — ICE, TURN, keepalive (account-level)
        nat = config.nat
        if not nat.stun_servers and not nat.ice_enabled and not nat.turn_enabled and nat.keepalive_sec == 0:
            log.info("NAT traversal: disabled (no STUN/ICE/TURN/keepalive configured)")

        if nat.ice_enabled:
            log.info("ICE enabled for media transport")
            acfg.natConfig.iceEnabled = True

        if nat.turn_enabled:
            log.info(f"TURN relay: {nat.turn_server} (transport: {nat.turn_transport}, user: {nat.turn_username})")
            acfg.natConfig.turnEnabled = True
            acfg.natConfig.turnServer = nat.turn_server
            acfg.natConfig.turnUserName = nat.turn_username
            acfg.natConfig.turnPassword = nat.turn_password
            acfg.natConfig.turnPasswordType = 0
            turn_tp = {"udp": pj.PJ_TURN_TP_UDP, "tcp": pj.PJ_TURN_TP_TCP, "tls": pj.PJ_TURN_TP_TLS}
            acfg.natConfig.turnConnType = turn_tp[nat.turn_transport]

        if nat.keepalive_sec > 0:
            log.info(f"UDP keepalive: {nat.keepalive_sec}s")
            acfg.natConfig.udpKaIntervalSec = nat.keepalive_sec
            acfg.natConfig.udpKaData = "\r\n"

        return acfg

    def __init__(
        self,
        config: SipEndpointConfig,
        transport_id: Any,
        local_ip: str,
    ) -> None:
        """Initialise and register the SIP account with PJSUA2.

        Calls ``build_pj_account_config()`` to produce a ``pj.AccountConfig``
        and immediately creates the account via ``pj.Account.create()``.

        Args:
            config: SIP endpoint configuration.
            transport_id: PJSUA2 transport ID returned by ``Endpoint.transportCreate``.
            local_ip: Local IP address to bind media transports to.
        """
        super().__init__()
        self._log = logger.bind(classname=self.__class__.__name__)

        acfg = self.build_pj_account_config(config, transport_id, local_ip)
        self._uri: str = str(acfg.idUri)
        self.create(acfg)

    @property
    def uri(self) -> str:
        """The SIP URI used for account registration."""
        return self._uri

    def onRegState(self, prm: "pj.OnRegStateParam") -> None:  # noqa: N802
        """PJSUA2 callback invoked on registration state changes.

        Logs whether registration is active or has failed.

        Args:
            prm: PJSUA2 registration-state callback parameter.
        """
        ai = self.getInfo()
        if ai.regIsActive:
            self._log.info(f"Registration active: {ai.uri}")
        else:
            self._log.warning(f"Registration failed (status {ai.regStatus})")


class SipCallerAccount(SipAccount):
    """PJSUA2 Account subclass for outgoing calls.

    Rejects all incoming calls with ``486 Busy Here``.

    Args:
        config: SIP endpoint configuration.
        transport_id: PJSUA2 transport ID returned by ``Endpoint.transportCreate``.
        local_ip: Local IP address to bind media transports to.
    """

    def __init__(
        self,
        config: SipEndpointConfig,
        transport_id: Any,
        local_ip: str,
        call_factory: CallerCallFactory | None,
        auto_answer: bool = True,
        answer_delay: float = 1.0,
        orphan_store: list[Any] | None = None,
    ) -> None:
        """Initialise the caller account with optional incoming-call handling.

        Args:
            config: SIP endpoint configuration.
            transport_id: PJSUA2 transport ID returned by ``Endpoint.transportCreate``.
            local_ip: Local IP address to bind media transports to.
            call_factory: Optional factory for creating caller-side call objects.
                When ``None``, all incoming calls are rejected immediately.
            auto_answer: Whether to automatically answer incoming calls.
            answer_delay: Seconds to wait before answering.
            orphan_store: Shared list for deferred PJSIP object destruction.
        """
        super().__init__(config, transport_id, local_ip)
        self._call_factory = call_factory
        self._auto_answer = auto_answer
        self._answer_delay = answer_delay
        self.orphan_store: list[Any] = orphan_store if orphan_store is not None else []
        self.calls: list[SipCalleeCall] = []

    def onIncomingCall(self, prm: "pj.OnIncomingCallParam") -> None:  # noqa: N802
        """Reject incoming calls with ``486 Busy Here``."""
        from sipstuff.sip_call import SipCall  # local import to avoid circular dependency

        call = SipCall(self, call_id=prm.callId)
        op = pj.CallOpParam()
        op.statusCode = pj.PJSIP_SC_BUSY_HERE
        call.answer(op)


class SipCalleeAccount(SipAccount):
    """PJSUA2 Account subclass that dispatches incoming calls via a factory.

    Handles auto-answer with configurable delay.  Created internally by
    ``SipCallee``.

    Args:
        config: SIP endpoint configuration.
        transport_id: PJSUA2 transport ID returned by ``Endpoint.transportCreate``.
        local_ip: Local IP address to bind media transports to.
        call_factory: Callable that creates a ``CalleeCall`` subclass instance
            given ``(account, call_id)``.
        auto_answer: Whether to automatically answer incoming calls.
        answer_delay: Seconds to wait before answering (allows ring tone).
        orphan_store: Shared list for deferred PJSIP object destruction.
    """

    def __init__(
        self,
        config: SipEndpointConfig,
        transport_id: Any,
        local_ip: str,
        call_factory: CalleeCallFactory,
        auto_answer: bool = True,
        answer_delay: float = 1.0,
        orphan_store: list[Any] | None = None,
    ) -> None:
        """Initialise the callee account with a call factory and auto-answer settings.

        Args:
            config: SIP endpoint configuration.
            transport_id: PJSUA2 transport ID returned by ``Endpoint.transportCreate``.
            local_ip: Local IP address to bind media transports to.
            call_factory: Callable that creates a ``SipCalleeCall`` subclass instance
                given ``(account, call_id)``.
            auto_answer: Whether to automatically answer incoming calls.
            answer_delay: Seconds to wait before answering (allows ring tone to play).
            orphan_store: Shared list for deferred PJSIP object destruction.
        """
        super().__init__(config, transport_id, local_ip)
        self._call_factory = call_factory
        self._auto_answer = auto_answer
        self._answer_delay = answer_delay
        self.orphan_store: list[Any] = orphan_store if orphan_store is not None else []
        self.calls: list[SipCalleeCall] = []

    def onIncomingCall(self, prm: "pj.OnIncomingCallParam") -> None:  # noqa: N802
        """PJSUA2 callback invoked for each incoming call.

        Creates a ``CalleeCall`` via the factory, logs the remote URI, and
        optionally starts a daemon thread for delayed auto-answer.
        """
        call = self._call_factory(self, prm.callId)
        ci = call.getInfo()
        self._log.info(f">>> Eingehender Anruf von: {ci.remoteUri}")
        self.calls.append(call)

        if self._auto_answer:
            delay = self._answer_delay
            self._log.info(f"    Nehme Anruf in {delay}s an...")

            def answer_call() -> None:
                """Sleep for the configured delay then answer the call on a registered thread."""
                time.sleep(delay)
                try:
                    pj.Endpoint.instance().libRegisterThread("answer")
                    call_prm = pj.CallOpParam()
                    call_prm.statusCode = pj.PJSIP_SC_OK
                    call.answer(call_prm)
                    self._log.info("    Anruf angenommen!")
                except Exception as e:
                    self._log.error(f"    Fehler beim Annehmen: {e}")

            threading.Thread(target=answer_call, daemon=True).start()
        else:
            self._log.info("    Auto-Answer deaktiviert — Anruf klingelt.")


CalleeCallFactory: TypeAlias = Callable[["SipCalleeAccount", int], "SipCalleeCall"]
"""Factory callable ``(account, call_id) -> SipCalleeCall`` for custom callee-side call subclasses."""

CallerCallFactory: TypeAlias = Callable[["SipCallerAccount", int, AudioDeviceConfig | None], "SipCallerCall"]
"""Factory callable ``(account, call_id, audio) -> SipCallerCall`` for custom caller-side call subclasses."""
