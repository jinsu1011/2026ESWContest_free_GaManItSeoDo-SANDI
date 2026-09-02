from __future__ import annotations

from pathlib import Path
import socket
import struct
import tempfile
import threading
import time
import unittest

from backend.runtime import SafeNestRuntime
from gateway.protocol import (
    PACKET_THERMAL_U16_BE,
    THERMAL_HEIGHT,
    THERMAL_META,
    THERMAL_WIDTH,
    PacketHeader,
    ThermalFrame,
)
from gateway.thermal_udp import (
    THERMAL_UDP_CHUNK_BYTES,
    THERMAL_UDP_DATAGRAM_BYTES,
    THERMAL_UDP_EXPECTED_CHUNKS,
    ThermalUDPReassembler,
    ThermalUDPServer,
    encode_thermal_udp_frame,
)
from state.manager import SensorStateManager
from storage.sensor_logger import SensorDataLogger, SensorStorageConfig


PEER = ("192.168.1.20", 45_000)


def payload(sequence: int, value: int = 1_000) -> bytes:
    count = THERMAL_WIDTH * THERMAL_HEIGHT
    pixels = [value] * count
    pixels[-1] = value + 100
    return THERMAL_META.pack(
        THERMAL_WIDTH,
        THERMAL_HEIGHT,
        sequence,
        sequence * 100,
        value,
        value + 100,
    ) + struct.pack(f"!{count}H", *pixels)


class ThermalUDPReassemblyTests(unittest.TestCase):
    def test_datagrams_stay_below_mtu_and_complete_frame_is_identical(self) -> None:
        source = payload(10)
        datagrams = encode_thermal_udp_frame(source, 10)
        self.assertEqual(len(datagrams), THERMAL_UDP_EXPECTED_CHUNKS)
        self.assertTrue(all(len(item) <= THERMAL_UDP_DATAGRAM_BYTES for item in datagrams))
        self.assertEqual(THERMAL_UDP_CHUNK_BYTES, 1_168)

        reassembler = ThermalUDPReassembler()
        frame = None
        for index, datagram in enumerate(datagrams):
            frame = reassembler.accept(
                datagram,
                PEER,
                received_monotonic=10.0 + index * 0.001,
            ) or frame
        self.assertIsNotNone(frame)
        self.assertEqual(frame.frame_sequence, 10)
        self.assertEqual(frame.pixel_bytes, source[THERMAL_META.size :])
        self.assertEqual(reassembler.snapshot()["completed_frames"], 1)

    def test_out_of_order_chunks_reconstruct_correctly(self) -> None:
        datagrams = encode_thermal_udp_frame(payload(11), 11)
        order = [8, 2, 0, 7, 1, 6, 3, 5, 4]
        reassembler = ThermalUDPReassembler()
        completed = [
            frame
            for index in order
            if (frame := reassembler.accept(datagrams[index], PEER)) is not None
        ]
        self.assertEqual(len(completed), 1)
        self.assertEqual(completed[0].frame_sequence, 11)
        self.assertGreater(reassembler.snapshot()["out_of_order_chunks"], 0)

    def test_duplicate_chunk_is_ignored(self) -> None:
        datagrams = encode_thermal_udp_frame(payload(12), 12)
        reassembler = ThermalUDPReassembler()
        self.assertIsNone(reassembler.accept(datagrams[0], PEER))
        self.assertIsNone(reassembler.accept(datagrams[0], PEER))
        completed = None
        for datagram in datagrams[1:]:
            completed = reassembler.accept(datagram, PEER) or completed
        self.assertIsNotNone(completed)
        self.assertEqual(reassembler.snapshot()["duplicate_chunks"], 1)

    def test_conflicting_duplicate_discards_frame_fail_closed(self) -> None:
        datagrams = encode_thermal_udp_frame(payload(121), 121)
        conflicting = bytearray(datagrams[0])
        conflicting[-1] ^= 0x01
        reassembler = ThermalUDPReassembler()
        self.assertIsNone(reassembler.accept(datagrams[0], PEER))
        self.assertIsNone(reassembler.accept(bytes(conflicting), PEER))
        for datagram in datagrams[1:]:
            self.assertIsNone(reassembler.accept(datagram, PEER))
        stats = reassembler.snapshot()
        self.assertEqual(stats["conflicting_duplicates"], 1)
        self.assertEqual(stats["completed_frames"], 0)

    def test_missing_chunk_times_out_without_producing_frame(self) -> None:
        datagrams = encode_thermal_udp_frame(payload(13), 13)
        reassembler = ThermalUDPReassembler(frame_timeout_seconds=0.5)
        for datagram in datagrams[:-1]:
            self.assertIsNone(
                reassembler.accept(datagram, PEER, received_monotonic=20.0)
            )
        self.assertEqual(reassembler.evict_expired(now=20.5), 1)
        stats = reassembler.snapshot()
        self.assertEqual(stats["completed_frames"], 0)
        self.assertEqual(stats["incomplete_frames"], 1)
        self.assertEqual(stats["reconstruction_timeouts"], 1)
        self.assertEqual(stats["pending_frames"], 0)

    def test_crc_corruption_is_rejected(self) -> None:
        datagrams = encode_thermal_udp_frame(payload(14), 14)
        corrupted = bytearray(datagrams[4])
        corrupted[-1] ^= 0x01
        datagrams[4] = bytes(corrupted)
        reassembler = ThermalUDPReassembler()
        result = None
        for datagram in datagrams:
            result = reassembler.accept(datagram, PEER) or result
        self.assertIsNone(result)
        self.assertEqual(reassembler.snapshot()["checksum_failures"], 1)

    def test_pending_memory_is_bounded_and_continuous_frames_leave_no_buffers(self) -> None:
        reassembler = ThermalUDPReassembler(max_pending_frames=2)
        for sequence in (1, 2, 3):
            reassembler.accept(
                encode_thermal_udp_frame(payload(sequence), sequence)[0],
                (PEER[0], PEER[1] + sequence),
            )
        self.assertEqual(reassembler.snapshot()["pending_frames"], 2)
        self.assertEqual(reassembler.snapshot()["pending_limit_evictions"], 1)

        continuous = ThermalUDPReassembler()
        for sequence in range(1, 101):
            for datagram in encode_thermal_udp_frame(payload(sequence), sequence):
                continuous.accept(datagram, PEER)
        stats = continuous.snapshot()
        self.assertEqual(stats["completed_frames"], 100)
        self.assertEqual(stats["pending_frames"], 0)


class ThermalUDPIntegrationTests(unittest.TestCase):
    def test_udp_server_receives_and_reconstructs_a_frame(self) -> None:
        received = []
        server = ThermalUDPServer(
            lambda frame, peer: received.append((frame, peer)),
            host="127.0.0.1",
            port=0,
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        deadline = time.monotonic() + 2.0
        while server._socket is None and time.monotonic() < deadline:
            time.sleep(0.005)
        sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            for datagram in encode_thermal_udp_frame(payload(21), 21):
                sender.sendto(datagram, ("127.0.0.1", server.port))
            while not received and time.monotonic() < deadline:
                time.sleep(0.005)
        finally:
            sender.close()
            server.stop()
            thread.join(timeout=2.0)
        self.assertEqual(len(received), 1)
        self.assertEqual(received[0][0].frame_sequence, 21)
        self.assertEqual(server.stats()["completed_frames"], 1)

    def test_only_complete_reconstructed_frame_reaches_state_ai_boundary(self) -> None:
        manager = SensorStateManager()
        reassembler = ThermalUDPReassembler(frame_timeout_seconds=0.1)
        datagrams = encode_thermal_udp_frame(payload(22), 22)
        for datagram in datagrams[:-1]:
            frame = reassembler.accept(datagram, PEER, received_monotonic=1.0)
            if frame is not None:
                manager.ingest(frame, PEER, received_at=1.0, monotonic_at=1.0)
        reassembler.evict_expired(now=1.1)
        self.assertEqual(
            manager.snapshot(now=1.1, monotonic_now=1.1)["sensors"]["thermal"]["status"],
            "NO_DATA",
        )

        for datagram in encode_thermal_udp_frame(payload(23), 23):
            frame = reassembler.accept(datagram, PEER, received_monotonic=2.0)
            if frame is not None:
                manager.ingest(frame, PEER, received_at=2.0, monotonic_at=2.0)
        thermal = manager.snapshot(now=2.0, monotonic_now=2.0)["sensors"]["thermal"]
        self.assertEqual(thermal["status"], "LIVE")
        self.assertEqual(thermal["sequence"], 23)

    def test_reconstructed_frame_continues_through_existing_npz_logger(self) -> None:
        import numpy as np

        with tempfile.TemporaryDirectory() as temporary:
            config = SensorStorageConfig(
                root=Path(temporary),
                min_free_bytes=0,
                thermal_batch_frames=1,
                thermal_flush_seconds=0.05,
            )
            logger = SensorDataLogger(config)
            logger.start()
            reassembler = ThermalUDPReassembler()
            for datagram in encode_thermal_udp_frame(payload(24), 24):
                frame = reassembler.accept(datagram, PEER)
                if frame is not None:
                    logger.submit(frame, received_at=100.0, monotonic_at=100.0)
            logger.stop()
            saved = list((Path(temporary) / "thermal").glob("*.npz"))
            self.assertEqual(len(saved), 1)
            with np.load(saved[0], allow_pickle=False) as dataset:
                self.assertEqual(dataset["frame_sequences"].tolist(), [24])

    def test_runtime_rejects_legacy_thermal_on_tcp_callback(self) -> None:
        manager = SensorStateManager()
        with tempfile.TemporaryDirectory() as temporary:
            runtime = SafeNestRuntime(
                sensor_port=0,
                thermal_udp_port=0,
                manager=manager,
                storage_config=SensorStorageConfig(root=Path(temporary), enabled=False),
            )
            frame = ThermalFrame(
                header=PacketHeader(PACKET_THERMAL_U16_BE, 30, len(payload(30))),
                width=THERMAL_WIDTH,
                height=THERMAL_HEIGHT,
                frame_sequence=30,
                uptime_ms=3_000,
                minimum_raw=1_000,
                maximum_raw=1_100,
                pixel_bytes=payload(30)[THERMAL_META.size :],
            )
            runtime._on_tcp_packet(frame, PEER)
            self.assertEqual(manager.snapshot()["sensors"]["thermal"]["status"], "NO_DATA")
            runtime._on_thermal_frame(frame, PEER)
            self.assertEqual(manager.snapshot()["sensors"]["thermal"]["status"], "LIVE")
            self.assertEqual(runtime.receiver_stats()["unexpected_tcp_thermal_packets"], 1)


if __name__ == "__main__":
    unittest.main()
