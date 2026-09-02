"""Chunked UDP transport for complete, validated SafeNest Thermal frames."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
import math
import socket
import struct
import threading
import time
from typing import Callable, Final
import zlib

from .protocol import (
    PACKET_THERMAL_U16_BE,
    THERMAL_PAYLOAD_BYTES,
    PacketHeader,
    ProtocolError,
    ThermalFrame,
    decode_thermal,
)


THERMAL_UDP_MAGIC: Final = b"SNTU"
THERMAL_UDP_VERSION: Final = 1
THERMAL_UDP_MESSAGE_TYPE: Final = PACKET_THERMAL_U16_BE
THERMAL_UDP_HEADER: Final = struct.Struct("!4sBBHIHHIIHHI")
THERMAL_UDP_HEADER_BYTES: Final = THERMAL_UDP_HEADER.size
THERMAL_UDP_DATAGRAM_BYTES: Final = 1_200
THERMAL_UDP_CHUNK_BYTES: Final = THERMAL_UDP_DATAGRAM_BYTES - THERMAL_UDP_HEADER_BYTES
THERMAL_UDP_EXPECTED_CHUNKS: Final = math.ceil(
    THERMAL_PAYLOAD_BYTES / THERMAL_UDP_CHUNK_BYTES
)
THERMAL_UDP_MAX_CHUNKS: Final = 16


class ThermalUDPError(ProtocolError):
    """A UDP chunk or reconstructed Thermal frame violated the contract."""


@dataclass(frozen=True)
class ThermalUDPChunk:
    frame_id: int
    chunk_index: int
    chunk_count: int
    frame_size: int
    chunk_offset: int
    frame_crc32: int
    payload: bytes


@dataclass
class ThermalUDPMetrics:
    received_datagrams: int = 0
    invalid_datagrams: int = 0
    completed_frames: int = 0
    incomplete_frames: int = 0
    duplicate_chunks: int = 0
    conflicting_duplicates: int = 0
    out_of_order_chunks: int = 0
    reconstruction_timeouts: int = 0
    pending_limit_evictions: int = 0
    checksum_failures: int = 0
    parser_failures: int = 0
    callback_errors: int = 0
    latest_frame_sequence: int | None = None
    first_completed_at: float | None = None
    last_completed_at: float | None = None
    total_reassembly_seconds: float = 0.0


@dataclass
class _PendingFrame:
    frame_id: int
    chunk_count: int
    frame_size: int
    frame_crc32: int
    started_at: float
    updated_at: float
    chunks: dict[int, bytes] = field(default_factory=dict)


class ThermalUDPReassembler:
    """Bounded, timeout-aware UDP reassembly that fails closed."""

    def __init__(
        self,
        *,
        frame_timeout_seconds: float = 0.5,
        max_pending_frames: int = 8,
        clock=time.monotonic,
    ) -> None:
        if not math.isfinite(float(frame_timeout_seconds)) or frame_timeout_seconds <= 0:
            raise ValueError("Thermal UDP frame timeout must be positive and finite")
        if isinstance(max_pending_frames, bool) or max_pending_frames < 1:
            raise ValueError("Thermal UDP max pending frames must be positive")
        self.frame_timeout_seconds = float(frame_timeout_seconds)
        self.max_pending_frames = int(max_pending_frames)
        self._clock = clock
        self._pending: dict[tuple[str, int, int], _PendingFrame] = {}
        self._completed_keys: dict[tuple[str, int, int], float] = {}
        self._lock = threading.RLock()
        self.metrics = ThermalUDPMetrics()

    def accept(
        self,
        datagram: bytes,
        peer: tuple[str, int],
        *,
        received_monotonic: float | None = None,
    ) -> ThermalFrame | None:
        now = self._clock() if received_monotonic is None else float(received_monotonic)
        with self._lock:
            self.metrics.received_datagrams += 1
            self._evict_expired_locked(now)
            try:
                chunk = decode_thermal_udp_datagram(datagram)
            except ThermalUDPError:
                self.metrics.invalid_datagrams += 1
                return None

            key = (peer[0], peer[1], chunk.frame_id)
            if key in self._completed_keys:
                self.metrics.duplicate_chunks += 1
                return None
            pending = self._pending.get(key)
            if pending is None:
                self._ensure_capacity_locked()
                pending = _PendingFrame(
                    frame_id=chunk.frame_id,
                    chunk_count=chunk.chunk_count,
                    frame_size=chunk.frame_size,
                    frame_crc32=chunk.frame_crc32,
                    started_at=now,
                    updated_at=now,
                )
                self._pending[key] = pending
            elif (
                pending.chunk_count != chunk.chunk_count
                or pending.frame_size != chunk.frame_size
                or pending.frame_crc32 != chunk.frame_crc32
            ):
                self.metrics.invalid_datagrams += 1
                self.metrics.incomplete_frames += 1
                del self._pending[key]
                return None

            existing = pending.chunks.get(chunk.chunk_index)
            if existing is not None:
                if existing == chunk.payload:
                    self.metrics.duplicate_chunks += 1
                else:
                    self.metrics.conflicting_duplicates += 1
                    self.metrics.invalid_datagrams += 1
                    self.metrics.incomplete_frames += 1
                    del self._pending[key]
                return None

            if chunk.chunk_index != len(pending.chunks):
                self.metrics.out_of_order_chunks += 1
            pending.chunks[chunk.chunk_index] = chunk.payload
            pending.updated_at = now
            if len(pending.chunks) != pending.chunk_count:
                return None

            del self._pending[key]
            payload = b"".join(pending.chunks[index] for index in range(pending.chunk_count))
            if len(payload) != pending.frame_size:
                self.metrics.incomplete_frames += 1
                self.metrics.invalid_datagrams += 1
                return None
            if zlib.crc32(payload) & 0xFFFFFFFF != pending.frame_crc32:
                self.metrics.checksum_failures += 1
                self.metrics.incomplete_frames += 1
                return None
            try:
                frame = decode_thermal(
                    PacketHeader(
                        PACKET_THERMAL_U16_BE,
                        pending.frame_id,
                        pending.frame_size,
                    ),
                    payload,
                )
            except ProtocolError:
                self.metrics.parser_failures += 1
                self.metrics.incomplete_frames += 1
                return None

            self.metrics.completed_frames += 1
            self.metrics.latest_frame_sequence = frame.frame_sequence
            self.metrics.total_reassembly_seconds += max(0.0, now - pending.started_at)
            if self.metrics.first_completed_at is None:
                self.metrics.first_completed_at = now
            self.metrics.last_completed_at = now
            self._completed_keys[key] = now
            while len(self._completed_keys) > 64:
                oldest = next(iter(self._completed_keys))
                del self._completed_keys[oldest]
            return frame

    def evict_expired(self, *, now: float | None = None) -> int:
        selected = self._clock() if now is None else float(now)
        with self._lock:
            return self._evict_expired_locked(selected)

    def snapshot(self) -> dict[str, object]:
        with self._lock:
            result = asdict(self.metrics)
            completed = self.metrics.completed_frames
            dropped = self.metrics.incomplete_frames
            duration = (
                None
                if self.metrics.first_completed_at is None
                or self.metrics.last_completed_at is None
                else self.metrics.last_completed_at - self.metrics.first_completed_at
            )
            result.update(
                {
                    "pending_frames": len(self._pending),
                    "max_pending_frames": self.max_pending_frames,
                    "frame_timeout_seconds": self.frame_timeout_seconds,
                    "chunk_payload_bytes": THERMAL_UDP_CHUNK_BYTES,
                    "datagram_bytes_max": THERMAL_UDP_DATAGRAM_BYTES,
                    "expected_chunks_per_frame": THERMAL_UDP_EXPECTED_CHUNKS,
                    "effective_fps": (
                        (completed - 1) / duration
                        if completed > 1 and duration is not None and duration > 0
                        else 0.0
                    ),
                    "average_reassembly_ms": (
                        self.metrics.total_reassembly_seconds * 1000.0 / completed
                        if completed
                        else 0.0
                    ),
                    "incomplete_frame_rate": (
                        dropped / (completed + dropped)
                        if completed + dropped
                        else 0.0
                    ),
                }
            )
            return result

    def _evict_expired_locked(self, now: float) -> int:
        expired = [
            key
            for key, pending in self._pending.items()
            if now - pending.updated_at >= self.frame_timeout_seconds
        ]
        for key in expired:
            del self._pending[key]
            self.metrics.reconstruction_timeouts += 1
            self.metrics.incomplete_frames += 1
        completed_expiry = self.frame_timeout_seconds * 4.0
        for key, completed_at in list(self._completed_keys.items()):
            if now - completed_at >= completed_expiry:
                del self._completed_keys[key]
        return len(expired)

    def _ensure_capacity_locked(self) -> None:
        if len(self._pending) < self.max_pending_frames:
            return
        oldest = min(self._pending, key=lambda key: self._pending[key].started_at)
        del self._pending[oldest]
        self.metrics.pending_limit_evictions += 1
        self.metrics.incomplete_frames += 1


FrameCallback = Callable[[ThermalFrame, tuple[str, int]], None]
ErrorCallback = Callable[[Exception, tuple[str, int] | None], None]


class ThermalUDPServer:
    """Receive Thermal UDP chunks independently from scalar TCP telemetry."""

    def __init__(
        self,
        on_frame: FrameCallback,
        *,
        host: str = "0.0.0.0",
        port: int = 5005,
        frame_timeout_seconds: float = 0.5,
        max_pending_frames: int = 8,
        on_error: ErrorCallback | None = None,
    ) -> None:
        if not 0 <= int(port) <= 65535:
            raise ValueError("Thermal UDP port must be between 0 and 65535")
        self.on_frame = on_frame
        self.on_error = on_error
        self.host = host
        self.port = int(port)
        self.reassembler = ThermalUDPReassembler(
            frame_timeout_seconds=frame_timeout_seconds,
            max_pending_frames=max_pending_frames,
        )
        self.stop_event = threading.Event()
        self._socket: socket.socket | None = None

    def serve_forever(self) -> None:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as receiver:
            self._socket = receiver
            receiver.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            receiver.bind((self.host, self.port))
            self.port = int(receiver.getsockname()[1])
            receiver.settimeout(min(0.1, self.reassembler.frame_timeout_seconds / 2.0))
            while not self.stop_event.is_set():
                try:
                    datagram, peer = receiver.recvfrom(THERMAL_UDP_DATAGRAM_BYTES + 1)
                except socket.timeout:
                    self.reassembler.evict_expired()
                    continue
                except OSError as error:
                    if self.stop_event.is_set():
                        break
                    self._report(error, None)
                    continue
                frame = self.reassembler.accept(datagram, peer)
                if frame is None:
                    continue
                try:
                    self.on_frame(frame, peer)
                except Exception as error:
                    self.reassembler.metrics.callback_errors += 1
                    self._report(error, peer)
            self._socket = None

    def stop(self) -> None:
        self.stop_event.set()
        receiver = self._socket
        if receiver is not None:
            try:
                receiver.close()
            except OSError:
                pass

    def stats(self) -> dict[str, object]:
        return {
            **self.reassembler.snapshot(),
            "host": self.host,
            "port": self.port,
        }

    def _report(self, error: Exception, peer: tuple[str, int] | None) -> None:
        if self.on_error is not None:
            self.on_error(error, peer)


def decode_thermal_udp_datagram(datagram: bytes) -> ThermalUDPChunk:
    if len(datagram) < THERMAL_UDP_HEADER_BYTES:
        raise ThermalUDPError("Thermal UDP datagram is shorter than its header")
    (
        magic,
        version,
        message_type,
        header_size,
        frame_id,
        chunk_index,
        chunk_count,
        frame_size,
        chunk_offset,
        chunk_length,
        reserved,
        frame_crc32,
    ) = THERMAL_UDP_HEADER.unpack_from(datagram)
    if magic != THERMAL_UDP_MAGIC:
        raise ThermalUDPError("invalid Thermal UDP magic")
    if version != THERMAL_UDP_VERSION:
        raise ThermalUDPError("unsupported Thermal UDP version")
    if message_type != THERMAL_UDP_MESSAGE_TYPE:
        raise ThermalUDPError("unsupported Thermal UDP message type")
    if header_size != THERMAL_UDP_HEADER_BYTES or reserved != 0:
        raise ThermalUDPError("invalid Thermal UDP header fields")
    if frame_size != THERMAL_PAYLOAD_BYTES:
        raise ThermalUDPError("invalid Thermal UDP frame size")
    expected_chunks = math.ceil(frame_size / THERMAL_UDP_CHUNK_BYTES)
    if (
        chunk_count != expected_chunks
        or chunk_count < 1
        or chunk_count > THERMAL_UDP_MAX_CHUNKS
        or chunk_index >= chunk_count
    ):
        raise ThermalUDPError("invalid Thermal UDP chunk count or index")
    expected_offset = chunk_index * THERMAL_UDP_CHUNK_BYTES
    expected_length = min(THERMAL_UDP_CHUNK_BYTES, frame_size - expected_offset)
    payload = datagram[header_size:]
    if (
        chunk_offset != expected_offset
        or chunk_length != expected_length
        or len(payload) != chunk_length
    ):
        raise ThermalUDPError("invalid Thermal UDP chunk offset or length")
    return ThermalUDPChunk(
        frame_id=frame_id,
        chunk_index=chunk_index,
        chunk_count=chunk_count,
        frame_size=frame_size,
        chunk_offset=chunk_offset,
        frame_crc32=frame_crc32,
        payload=payload,
    )


def encode_thermal_udp_frame(payload: bytes, frame_id: int) -> list[bytes]:
    """Reference encoder used by loopback tests and non-ESP32 tooling."""

    if len(payload) != THERMAL_PAYLOAD_BYTES:
        raise ValueError("Thermal UDP payload must match the logical Thermal frame size")
    if not 0 <= int(frame_id) <= 0xFFFFFFFF:
        raise ValueError("Thermal UDP frame ID must fit uint32")
    frame_crc32 = zlib.crc32(payload) & 0xFFFFFFFF
    chunks = []
    for chunk_index in range(THERMAL_UDP_EXPECTED_CHUNKS):
        offset = chunk_index * THERMAL_UDP_CHUNK_BYTES
        chunk = payload[offset : offset + THERMAL_UDP_CHUNK_BYTES]
        header = THERMAL_UDP_HEADER.pack(
            THERMAL_UDP_MAGIC,
            THERMAL_UDP_VERSION,
            THERMAL_UDP_MESSAGE_TYPE,
            THERMAL_UDP_HEADER_BYTES,
            int(frame_id),
            chunk_index,
            THERMAL_UDP_EXPECTED_CHUNKS,
            len(payload),
            offset,
            len(chunk),
            0,
            frame_crc32,
        )
        chunks.append(header + chunk)
    return chunks
