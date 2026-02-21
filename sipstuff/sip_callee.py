"""Generic passive SIP module — answer incoming calls and dispatch to subclass hooks.

Provides ``SipCallee`` (inherits ``SipEndpoint`` for PJSIP lifecycle),
``CalleeAccount`` (internal account with auto-answer), and ``CalleeCall``
(base incoming-call handler with ``on_media_active`` / ``on_disconnected``
hooks).

Mirrors ``SipCaller``'s structure for the outgoing-call side.  Subpackages
subclass ``CalleeCall`` and override hooks instead of reimplementing the PJSIP
scaffolding.

Example::

    from sipstuff.sip_callee import SipCallee, CalleeCall, CalleeAccount

    class EchoCall(CalleeCall):
        def on_media_active(self, audio_media, media_idx):
            # wire up custom media processing
            ...
        def on_disconnected(self):
            # cleanup
            ...

    def make_call(acc, call_id):
        return EchoCall(acc, call_id)

    config = SipCalleeConfig.from_config(config_path="config.yml")
    with SipCallee(config, call_factory=make_call) as callee:
        callee.run()  # blocks until Ctrl+C
"""

import time
from collections.abc import Callable
from typing import TypeAlias

from sipstuff.sip_account import CalleeCallFactory, SipCalleeAccount
from sipstuff.sip_call import SipCalleeCall
from sipstuff.sip_endpoint import SipEndpoint
from sipstuff.sip_types import SipCallError
from sipstuff.sipconfig import AudioDeviceConfig, SipCalleeConfig


class SipCallee(SipEndpoint):
    """Context manager for passive SIP operation — receive and handle incoming calls.

    Inherits endpoint lifecycle from ``SipEndpoint`` and waits for incoming
    calls instead of placing outgoing ones.

    Args:
        config: Callee configuration (inherits ``SipEndpointConfig`` fields
            plus ``auto_answer`` and ``answer_delay``).
        call_factory: Callable ``(account, call_id) → CalleeCall`` that creates
            call handler instances.  If ``None``, uses the base ``CalleeCall``.

    Examples::

        with SipCallee(config) as callee:
            callee.run()  # blocks until Ctrl+C
    """

    def __init__(
        self,
        config: SipCalleeConfig,
        call_factory: CalleeCallFactory | None = None,
    ) -> None:
        """Initialise the callee with configuration and an optional call factory.

        Args:
            config: Callee configuration including SIP credentials, NAT settings,
                audio device options, auto-answer flag, and answer delay.
            call_factory: Optional callable ``(account, call_id) → SipCalleeCall``
                used to create a handler for each incoming call.  When ``None``,
                a default ``SipCalleeCall`` is instantiated for every call.
        """
        super().__init__(config=config)
        _audio_cfg = config.audio
        self._call_factory: CalleeCallFactory = (
            call_factory
            if call_factory is not None
            else (lambda acc, cid: SipCalleeCall(account=acc, call_id=cid, audio=_audio_cfg))
        )
        self._auto_answer = config.auto_answer
        self._answer_delay = config.answer_delay
        self._account: SipCalleeAccount | None = None

    def __enter__(self) -> "SipCallee":
        """Start the PJSUA2 endpoint and return ``self`` for use as a context manager.

        Returns:
            This ``SipCallee`` instance with the endpoint and account active.
        """
        self.start()
        return self

    def _create_account(self, local_ip: str) -> None:
        """Create and register a ``CalleeAccount`` for incoming calls.

        Args:
            local_ip: Local IP address to bind media transports to.

        Raises:
            SipCallError: If account creation or registration fails.
        """
        try:
            self._account = SipCalleeAccount(
                config=self.config,
                transport_id=self._transport_id,
                local_ip=local_ip,
                call_factory=self._call_factory,
                auto_answer=self._auto_answer,
                answer_delay=self._answer_delay,
                orphan_store=self._orphaned_ports,
            )
        except Exception as exc:
            self.stop()
            raise SipCallError(f"SIP registration failed: {exc}") from exc
        self._log.info("Warte auf eingehende Anrufe...")

    def _shutdown_account(self) -> None:
        """Shut down the ``CalleeAccount``."""
        if self._account is not None:
            try:
                self._account.shutdown()
            except Exception:
                pass
            self._account = None

    def run(self) -> None:
        """Blocking event loop — processes PJSIP events until ``KeyboardInterrupt``.

        Calls ``ep.libHandleEvents(100)`` in a tight loop with 100 ms sleeps.
        """
        if self._ep is None:
            raise RuntimeError("SipCallee not started — call start() or use context manager")
        try:
            while True:
                self._ep.libHandleEvents(100)
                time.sleep(0.1)
        except KeyboardInterrupt:
            self._log.info("Beende...")

    @property
    def account(self) -> SipCalleeAccount:
        """The registered ``CalleeAccount`` instance.

        Raises:
            RuntimeError: If the callee has not been started.
        """
        if self._account is None:
            raise RuntimeError("SipCallee not started — call start() or use context manager")
        return self._account

    @property
    def calls(self) -> list[SipCalleeCall]:
        """List of all ``CalleeCall`` instances created by incoming calls."""
        if self._account is None:
            return []
        return self._account.calls
