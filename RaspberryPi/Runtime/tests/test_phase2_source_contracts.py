"""Canonical ESP32 firmware <-> Raspberry Pi gateway <-> protocol doc contract.

The three sources must agree byte-for-byte on the SafeNest v1 wire format:

  ESP32/Arduino/esp32_sensor_node/  (sender)
  RaspberryPi/Runtime/gateway/protocol.py                       (receiver)
  ESP32/docs/COMMUNICATION_PROTOCOL.md                          (specification)

Paths come from ``paths.py`` so the canonical firmware is asserted, not a copy.
"""

from __future__ import annotations

from pathlib import Path
import re
import unittest

from paths import ESP32_ROOT, ESP32_SKETCH, ESP32_SECRET_TEMPLATE, RUNTIME_ROOT

ROOT = RUNTIME_ROOT
INTEGRATED_FW = ESP32_SKETCH
SECRETS_EXAMPLE = ESP32_SECRET_TEMPLATE
GATEWAY_PROTOCOL = RUNTIME_ROOT / "gateway" / "protocol.py"
GATEWAY_RECEIVER = RUNTIME_ROOT / "gateway" / "receiver.py"
THERMAL_UDP = RUNTIME_ROOT / "gateway" / "thermal_udp.py"
PROTOCOL_DOC = ESP32_ROOT / "docs" / "COMMUNICATION_PROTOCOL.md"


class Phase2SourceContracts(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.firmware = INTEGRATED_FW.read_text(encoding="utf-8")
        cls.protocol_py = GATEWAY_PROTOCOL.read_text(encoding="utf-8")
        cls.receiver = GATEWAY_RECEIVER.read_text(encoding="utf-8")
        cls.thermal_udp = THERMAL_UDP.read_text(encoding="utf-8")
        cls.protocol = PROTOCOL_DOC.read_text(encoding="utf-8")

    def test_selected_sources_exist(self) -> None:
        for path in (
            INTEGRATED_FW,
            SECRETS_EXAMPLE,
            GATEWAY_PROTOCOL,
            GATEWAY_RECEIVER,
            THERMAL_UDP,
            PROTOCOL_DOC,
        ):
            self.assertTrue(path.is_file(), path)

    def test_canonical_sketch_matches_its_directory_name(self) -> None:
        """The Arduino IDE requires sketch.ino to match its folder name."""
        self.assertEqual(INTEGRATED_FW.stem, INTEGRATED_FW.parent.name)
        self.assertEqual(SECRETS_EXAMPLE.parent, INTEGRATED_FW.parent)

    def test_all_four_sensors_are_present_in_integrated_firmware(self) -> None:
        for marker in (
            "Seeed_Arduino_mmWave.h",  # MR60BHA2 mmWave on UART2
            "PIN_MHZ19_RX",            # Winsen MH-Z19B CO2 on UART1
            "PIN_PIR",                 # PIR digital input
            "thermalSpi",              # MI48xx thermal over I2C control + SPI data
        ):
            self.assertIn(marker, self.firmware)

    def test_active_sender_exposes_reboot_safe_sensor_provenance(self) -> None:
        for token in (
            "boot_id",
            "co2_measurement_event_id",
            "co2_measurement_monotonic_ms",
            "co2_measurement_event_valid",
        ):
            self.assertIn(token, self.firmware)
        self.assertIn("++co2MeasurementEventId", self.firmware)

    def test_tcp_v1_header_contract_matches(self) -> None:
        self.assertIn('memcpy(header, "SNST", 4)', self.firmware)
        self.assertIn("constexpr size_t PACKET_HEADER_SIZE = 16", self.firmware)
        self.assertIn('MAGIC: Final = b"SNST"', self.protocol_py)
        self.assertIn("PROTOCOL_VERSION: Final = 1", self.protocol_py)
        self.assertIn('HEADER: Final = struct.Struct("!4sBBHII")', self.protocol_py)
        self.assertIn("헤더 크기는 16바이트", self.protocol)

    def test_tcp_receiver_is_resilient_to_partial_reads_and_drops(self) -> None:
        self.assertIn("def enable_tcp_keepalive", self.receiver)
        self.assertIn("connection.settimeout", self.receiver)
        self.assertIn("def serve_forever", self.receiver)

    def test_thermal_payload_is_9936_bytes_by_contract(self) -> None:
        width = self._constexpr("THERMAL_WIDTH")
        height = self._constexpr("THERMAL_HEIGHT")
        metadata = self._constexpr("THERMAL_META_SIZE")
        self.assertEqual((width, height, metadata), (80, 62, 16))
        self.assertEqual(metadata + width * height * 2, 9_936)
        self.assertIn("THERMAL_WIDTH: Final = 80", self.protocol_py)
        self.assertIn("THERMAL_HEIGHT: Final = 62", self.protocol_py)
        self.assertIn('THERMAL_META: Final = struct.Struct("!HHIIHH")', self.protocol_py)
        self.assertIn("9,920", self.protocol)

    def test_sender_has_timeout_and_reconnect_loop(self) -> None:
        self.assertIn("client.connect(RPI_HOST, RPI_PORT", self.firmware)
        self.assertIn("constexpr uint32_t TCP_CONNECT_TIMEOUT_MS", self.firmware)
        self.assertIn("constexpr uint32_t TCP_RECONNECT_DELAY_MS", self.firmware)
        self.assertIn("vTaskDelay(pdMS_TO_TICKS(", self.firmware)

    def test_only_thermal_uses_chunked_udp(self) -> None:
        self.assertIn("void telemetryTcpTask", self.firmware)
        self.assertIn("sendTelemetry(", self.firmware)
        self.assertIn("void thermalUdpTask", self.firmware)
        self.assertIn("sendThermalUdp(", self.firmware)
        self.assertNotIn("sendThermal(client", self.firmware)
        self.assertIn("THERMAL_UDP_DATAGRAM_SIZE = 1200", self.firmware)
        self.assertIn("THERMAL_UDP_PORT = 5005", self.firmware)

    def test_secrets_are_externalized_in_selected_sender(self) -> None:
        """Credentials must live in the ignored secrets.h, never in the sketch.

        Asserting on the *shape* of a credential assignment catches any future
        hardcoded network, instead of only the one pair that leaked before.
        """
        self.assertIn('#include "secrets.h"', self.firmware)
        for symbol in ("WIFI_SSID", "WIFI_PASSWORD", "RPI_HOST"):
            assignment = re.search(
                rf'{symbol}\s*\[\]\s*=\s*"([^"]*)"', self.firmware
            )
            self.assertIsNone(
                assignment,
                f"{symbol} must come from secrets.h, not a literal in the sketch",
            )

    def test_secrets_example_carries_no_real_credential(self) -> None:
        example = SECRETS_EXAMPLE.read_text(encoding="utf-8")
        for symbol in ("WIFI_SSID", "WIFI_PASSWORD"):
            match = re.search(rf'{symbol}\s*\[\]\s*=\s*"([^"]*)"', example)
            self.assertIsNotNone(match, symbol)
            self.assertTrue(
                match.group(1).startswith("YOUR_"),
                f"{symbol} in the example template must stay a placeholder",
            )

    def _constexpr(self, name: str) -> int:
        match = re.search(
            rf"constexpr\s+size_t\s+{re.escape(name)}\s*=\s*(\d+)",
            self.firmware,
        )
        self.assertIsNotNone(match, name)
        return int(match.group(1))


if __name__ == "__main__":
    unittest.main()
