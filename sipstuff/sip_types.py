"""SIP type definitions — CallResult, SipCallError, WavInfo, factory type aliases.

Pure data types with no PJSUA2 dependency (except ``PJTransports`` enum).
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable
from enum import Enum
from typing import TYPE_CHECKING, TypeAlias

import pjsua2 as pj


@dataclasses.dataclass
class CallResult:
    """Result of a SIP call placed by ``SipCaller.make_call``.

    Attributes:
        success: Whether the call was answered and playback started.
        call_start: Epoch timestamp when the call was initiated.
        call_end: Epoch timestamp when the call finished.
        call_duration: Wall-clock duration of the call in seconds.
        answered: Whether the remote party answered.
        disconnect_reason: SIP disconnect reason string from PJSIP.
        live_transcript: Accumulated live-transcription segments collected during the call.
        mix_output_path: Filesystem path to the recorded mix output file, or None if not recorded.
    """

    success: bool
    call_start: float
    call_end: float
    call_duration: float
    answered: bool
    disconnect_reason: str
    live_transcript: list[dict[str, object]] = dataclasses.field(default_factory=list)
    mix_output_path: str | None = None


class SipCallError(Exception):
    """Raised on SIP call errors (registration, transport, WAV issues)."""


class PJTransports(Enum):
    """Enumeration of PJSIP transport types wrapping ``pj.PJSIP_TRANSPORT_*`` constants."""

    TRANSPORT_UNSPECIFIED = pj.PJSIP_TRANSPORT_UNSPECIFIED
    TRANSPORT_UDP = pj.PJSIP_TRANSPORT_UDP
    TRANSPORT_TCP = pj.PJSIP_TRANSPORT_TCP
    TRANSPORT_TLS = pj.PJSIP_TRANSPORT_TLS
    TRANSPORT_DTLS = pj.PJSIP_TRANSPORT_DTLS
    TRANSPORT_SCTP = pj.PJSIP_TRANSPORT_SCTP
    TRANSPORT_LOOP = pj.PJSIP_TRANSPORT_LOOP
    TRANSPORT_LOOP_DGRAM = pj.PJSIP_TRANSPORT_LOOP_DGRAM
    TRANSPORT_START_OTHER = pj.PJSIP_TRANSPORT_START_OTHER
    TRANSPORT_UDP6 = pj.PJSIP_TRANSPORT_UDP6
    TRANSPORT_TCP6 = pj.PJSIP_TRANSPORT_TCP6
    TRANSPORT_TLS6 = pj.PJSIP_TRANSPORT_TLS6
    TRANSPORT_DTLS6 = pj.PJSIP_TRANSPORT_DTLS6
