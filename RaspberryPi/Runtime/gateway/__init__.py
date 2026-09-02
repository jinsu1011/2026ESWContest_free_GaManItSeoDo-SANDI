"""SafeNest Raspberry Pi gateway communication layer."""

from .protocol import (
    ConnectionClosed,
    PacketHeader,
    ProtocolError,
    ReceiveDeadlineExceeded,
    SequenceError,
    SequenceTracker,
    TelemetryPayload,
    ThermalFrame,
    read_packet,
)

__all__ = [
    "ConnectionClosed",
    "PacketHeader",
    "ProtocolError",
    "ReceiveDeadlineExceeded",
    "SequenceError",
    "SequenceTracker",
    "TelemetryPayload",
    "ThermalFrame",
    "read_packet",
]

