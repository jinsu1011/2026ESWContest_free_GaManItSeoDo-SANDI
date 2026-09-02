"""Real TCP loopback harness spanning gateway, state, AI, risk, DB, and API views."""

from __future__ import annotations

import json
import socket
import struct
import time
from types import SimpleNamespace
from typing import Callable

from ai.pipeline import OnDeviceAIPipeline
from backend.runtime import SafeNestRuntime
from backend.views import status_document
from database.store import PersistentRuntimeStore
from gateway.protocol import (
    HEADER,
    MAGIC,
    PACKET_TELEMETRY_JSON,
    PACKET_THERMAL_U16_BE,
    PROTOCOL_VERSION,
    THERMAL_HEIGHT,
    THERMAL_META,
    THERMAL_WIDTH,
)
from gateway.thermal_udp import encode_thermal_udp_frame
from risk.engine import SafeNestRiskEngine
from state.manager import SensorStateManager
from storage.sensor_logger import SensorStorageConfig


class ScriptedThermalModel:
    """Deterministic model boundary; it does not pretend to validate the TFLite binary."""

    def __init__(self) -> None:
        self.class_name = "NO_HUMAN"
        self.probabilities = [0.99, 0.005, 0.005]
        self.confidence = 0.99
        self.fail = False

    def set_state(self, class_name: str) -> None:
        states = {
            "NO_HUMAN": ([0.99, 0.005, 0.005], 0.99),
            "HUMAN_NORMAL": ([0.01, 0.98, 0.01], 0.98),
            "HUMAN_FALL": ([0.01, 0.04, 0.95], 0.95),
        }
        probabilities, confidence = states[class_name]
        self.class_name = class_name
        self.probabilities = probabilities
        self.confidence = confidence
        self.fail = False

    def predict(self, _frame):
        if self.fail:
            raise RuntimeError("injected TFLite runtime failure")
        return SimpleNamespace(
            class_name=self.class_name,
            probabilities=list(self.probabilities),
            confidence=self.confidence,
            latency_ms=1.0,
            model_id="phase10-scripted-thermal",
            model_version="test-boundary",
        )


def telemetry_packet(
    sequence: int,
    *,
    respiration: float | None = 15.0,
    heart: float | None = 70.0,
    co2: float | None = 700.0,
    motion: bool = False,
    uptime_ms: int = 10_000,
) -> bytes:
    document = {
        "schema": "safenest.telemetry.v1",
        "device_id": "phase10-loopback-node",
        "seq": sequence,
        "uptime_ms": uptime_ms,
        "resp_rate_bpm": respiration,
        "heart_rate_bpm": heart,
        "co2_ppm": co2,
        "pir_motion": motion,
        "valid": {
            "respiration": respiration is not None,
            "heart": heart is not None,
            "co2": co2 is not None,
        },
    }
    payload = json.dumps(document, separators=(",", ":"), allow_nan=False).encode("utf-8")
    return _packet(PACKET_TELEMETRY_JSON, sequence, payload)


def thermal_payload(
    sequence: int,
    *,
    minimum_raw: int = 1_000,
    maximum_raw: int = 2_000,
    uptime_ms: int = 10_010,
) -> bytes:
    count = THERMAL_WIDTH * THERMAL_HEIGHT
    pixels = [minimum_raw] * count
    pixels[-1] = maximum_raw
    pixel_bytes = struct.pack(f"!{count}H", *pixels)
    payload = THERMAL_META.pack(
        THERMAL_WIDTH,
        THERMAL_HEIGHT,
        sequence,
        uptime_ms,
        minimum_raw,
        maximum_raw,
    ) + pixel_bytes
    return payload


def _packet(packet_type: int, sequence: int, payload: bytes) -> bytes:
    return HEADER.pack(
        MAGIC,
        PROTOCOL_VERSION,
        packet_type,
        0,
        sequence,
        len(payload),
    ) + payload


class EndToEndHarness:
    def __init__(self) -> None:
        self.manager = SensorStateManager()
        self.thermal_model = ScriptedThermalModel()
        self.pipeline = OnDeviceAIPipeline(
            self.manager,
            {"thermal": self.thermal_model},
        )
        self.store = PersistentRuntimeStore(":memory:")
        self.runtime = SafeNestRuntime(
            sensor_host="127.0.0.1",
            sensor_port=0,
            thermal_udp_host="127.0.0.1",
            thermal_udp_port=0,
            packet_deadline_seconds=2.0,
            evaluation_interval_seconds=60.0,
            manager=self.manager,
            ai_pipeline=self.pipeline,
            risk_engine=SafeNestRiskEngine(),
            store=self.store,
            # Sensor file persistence has dedicated temporary-directory tests.
            storage_config=SensorStorageConfig(root=".", enabled=False),
        )
        self._clients: list[socket.socket] = []

    def __enter__(self) -> "EndToEndHarness":
        self.runtime.start()
        self.wait_until(
            lambda: self.runtime.server._listener is not None
            and self.runtime.server.port != 0
            and self.runtime.thermal_udp_server._socket is not None
            and self.runtime.thermal_udp_server.port != 0
        )
        return self

    def __exit__(self, _type, _value, _traceback) -> None:
        for client in self._clients:
            try:
                client.close()
            except OSError:
                pass
        self.runtime.stop()
        self.store.close()

    def connect_and_send(
        self,
        *,
        sequence: int,
        respiration: float | None = 15.0,
        heart: float | None = 70.0,
        co2: float | None = 700.0,
        motion: bool = False,
        fragment_size: int | None = None,
    ) -> socket.socket:
        starting_revision = int(self.manager.snapshot()["revision"])
        client = socket.create_connection(
            ("127.0.0.1", self.runtime.server.port),
            timeout=1.0,
        )
        client.settimeout(1.0)
        self._clients.append(client)
        data = telemetry_packet(
            sequence,
            respiration=respiration,
            heart=heart,
            co2=co2,
            motion=motion,
        )
        step = fragment_size or len(data)
        for offset in range(0, len(data), step):
            client.sendall(data[offset : offset + step])
        thermal_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            for datagram in encode_thermal_udp_frame(
                thermal_payload(sequence), sequence
            ):
                thermal_socket.sendto(
                    datagram,
                    ("127.0.0.1", self.runtime.thermal_udp_server.port),
                )
        finally:
            thermal_socket.close()
        self.wait_until(
            lambda: int(self.manager.snapshot()["revision"]) >= starting_revision + 2
        )
        return client

    def evaluate(self) -> dict[str, object]:
        return self.runtime.evaluate_once()

    def status(self) -> dict[str, object]:
        return status_document(self.store.latest())

    def close_client(self, client: socket.socket) -> None:
        client.close()
        if client in self._clients:
            self._clients.remove(client)

    def wait_for_system(self, expected: str) -> None:
        self.wait_until(lambda: self.manager.snapshot()["system"] == expected)

    def wait_for_sensor_status(self, sensor_id: str, expected: str) -> None:
        self.wait_until(
            lambda: self.manager.snapshot()["sensors"][sensor_id]["status"]
            == expected
        )

    @staticmethod
    def wait_until(condition: Callable[[], bool], timeout: float = 2.5) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if condition():
                return
            time.sleep(0.005)
        raise TimeoutError("PHASE 10 loopback condition was not reached")
