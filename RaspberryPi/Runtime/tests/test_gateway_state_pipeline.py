from __future__ import annotations

import json
import socket
import struct
import threading
import time
import unittest

from gateway.protocol import (
    HEADER,
    MAGIC,
    PACKET_TELEMETRY_JSON,
    PACKET_THERMAL_U16_BE,
    PROTOCOL_VERSION,
    THERMAL_META,
    ProtocolError,
)
from gateway.receiver import SafeNestTCPServer
from state.manager import SensorStateManager


def packet(packet_type: int, sequence: int, payload: bytes) -> bytes:
    return HEADER.pack(MAGIC, PROTOCOL_VERSION, packet_type, 0, sequence, len(payload)) + payload


def telemetry(sequence: int) -> bytes:
    return json.dumps(
        {
            "schema": "safenest.telemetry.v1",
            "device_id": "esp32-01",
            "seq": sequence,
            "uptime_ms": 10_000,
            "resp_rate_bpm": 15.0,
            "heart_rate_bpm": 70.0,
            "co2_ppm": 700,
            "pir_motion": False,
            "valid": {"respiration": True, "heart": True, "co2": True},
        },
        separators=(",", ":"),
    ).encode()


def thermal(sequence: int) -> bytes:
    count = 80 * 62
    pixels = struct.pack(f"!{count}H", *([1_234] * count))
    return THERMAL_META.pack(80, 62, sequence, 10_010, 1_234, 1_234) + pixels


class GatewayStatePipelineTests(unittest.TestCase):
    def test_valid_packets_become_online_then_disconnect(self) -> None:
        manager = SensorStateManager()

        def on_error(error: Exception, peer) -> None:
            if peer is not None and isinstance(error, ProtocolError):
                manager.mark_peer_disconnected(peer)

        server = SafeNestTCPServer(
            lambda decoded, peer: manager.ingest(decoded, peer),
            host="127.0.0.1",
            port=0,
            on_error=on_error,
            packet_deadline_seconds=0.3,
        )
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        deadline = time.monotonic() + 2.0
        while (server._listener is None or server.port == 0) and time.monotonic() < deadline:
            time.sleep(0.005)

        client = socket.create_connection(("127.0.0.1", server.port), timeout=1.0)
        try:
            client.sendall(packet(PACKET_TELEMETRY_JSON, 1, telemetry(1)))
            client.sendall(packet(PACKET_THERMAL_U16_BE, 1, thermal(1)))
            deadline = time.monotonic() + 2.0
            while manager.snapshot()["system"] != "ONLINE" and time.monotonic() < deadline:
                time.sleep(0.005)
            self.assertEqual(manager.snapshot()["system"], "ONLINE")
        finally:
            client.close()

        deadline = time.monotonic() + 2.0
        while manager.snapshot()["system"] != "OFFLINE" and time.monotonic() < deadline:
            time.sleep(0.005)
        try:
            state = manager.snapshot()
            self.assertEqual(state["system"], "OFFLINE")
            for sensor in state["sensors"].values():
                self.assertEqual(sensor["status"], "DISCONNECTED")
        finally:
            server.stop()
            thread.join(timeout=2.0)

        self.assertFalse(thread.is_alive())


if __name__ == "__main__":
    unittest.main()
