"""Long-running TCP → state → AI → risk publication service."""

from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
import threading
import time

from ai.pipeline import OnDeviceAIPipeline
from backend.store import RuntimeStore
from gateway.protocol import ConnectionClosed, ProtocolError, TelemetryPayload, ThermalFrame
from gateway.receiver import SafeNestTCPServer
from gateway.thermal_udp import ThermalUDPServer
from risk.formula_v1 import SafeNestRiskFormulaV1
from services.tts import TTSProtocol, create_tts_from_env
from state.manager import SensorStateManager
from storage.sensor_logger import SensorDataLogger, SensorStorageConfig


class SafeNestRuntime:
    def __init__(
        self,
        *,
        sensor_host: str = "0.0.0.0",
        sensor_port: int = 9000,
        thermal_udp_host: str = "0.0.0.0",
        thermal_udp_port: int = 5005,
        thermal_udp_frame_timeout_seconds: float = 0.5,
        thermal_udp_max_pending_frames: int = 8,
        packet_deadline_seconds: float = 5.0,
        evaluation_interval_seconds: float = 15.0,
        manager: SensorStateManager | None = None,
        ai_pipeline: OnDeviceAIPipeline | None = None,
        risk_engine: object | None = None,
        store: RuntimeStore | None = None,
        sensor_data_logger: SensorDataLogger | None = None,
        storage_config: SensorStorageConfig | None = None,
        tts: TTSProtocol | None = None,
    ) -> None:
        if evaluation_interval_seconds <= 0:
            raise ValueError("evaluation interval must be positive")
        if sensor_data_logger is not None and storage_config is not None:
            raise ValueError("pass sensor_data_logger or storage_config, not both")
        selected_storage_config = storage_config or (
            sensor_data_logger.config
            if sensor_data_logger is not None
            else SensorStorageConfig.from_env(
                Path(__file__).resolve().parent.parent / "data"
            )
        )
        self.sensor_data_logger = sensor_data_logger or SensorDataLogger(
            selected_storage_config
        )
        self.manager = manager or SensorStateManager(
            co2_update_interval_seconds=selected_storage_config.co2_interval_seconds
        )
        self.ai_pipeline = ai_pipeline or OnDeviceAIPipeline(self.manager)
        self.risk_engine = risk_engine or SafeNestRiskFormulaV1()
        self.store = store or RuntimeStore()
        self.tts = tts or create_tts_from_env(
            error_handler=lambda error: self.store.record_runtime_error("tts", error)
        )
        self.evaluation_interval_seconds = float(evaluation_interval_seconds)
        self.server = SafeNestTCPServer(
            self._on_tcp_packet,
            host=sensor_host,
            port=sensor_port,
            on_error=self._on_receiver_error,
            packet_deadline_seconds=packet_deadline_seconds,
        )
        self.thermal_udp_server = ThermalUDPServer(
            self._on_thermal_frame,
            host=thermal_udp_host,
            port=thermal_udp_port,
            frame_timeout_seconds=thermal_udp_frame_timeout_seconds,
            max_pending_frames=thermal_udp_max_pending_frames,
            on_error=self._on_thermal_udp_error,
        )
        self._stop_event = threading.Event()
        self._receiver_thread: threading.Thread | None = None
        self._thermal_udp_thread: threading.Thread | None = None
        self._evaluation_thread: threading.Thread | None = None
        self._lifecycle_lock = threading.Lock()
        self._started = False
        self._unexpected_tcp_thermal_packets = 0

    def start(self) -> None:
        with self._lifecycle_lock:
            if self._started:
                return
            self._started = True
            self._stop_event.clear()
            try:
                self.tts.start()
            except Exception as error:
                self.store.record_runtime_error("tts", error)
            self.sensor_data_logger.start()
            self.evaluate_once()
            self._receiver_thread = threading.Thread(
                target=self.server.serve_forever,
                name="safenest-tcp-receiver",
                daemon=True,
            )
            self._thermal_udp_thread = threading.Thread(
                target=self.thermal_udp_server.serve_forever,
                name="safenest-thermal-udp-receiver",
                daemon=True,
            )
            self._evaluation_thread = threading.Thread(
                target=self._evaluation_loop,
                name="safenest-state-publisher",
                daemon=True,
            )
            self._receiver_thread.start()
            self._thermal_udp_thread.start()
            self._evaluation_thread.start()

    def stop(self) -> None:
        with self._lifecycle_lock:
            if not self._started:
                return
            self._started = False
            self._stop_event.set()
            self.server.stop()
            self.thermal_udp_server.stop()
            receiver = self._receiver_thread
            thermal_receiver = self._thermal_udp_thread
            evaluator = self._evaluation_thread
        if evaluator is not None:
            evaluator.join(timeout=self.evaluation_interval_seconds + 1.0)
        if receiver is not None:
            receiver.join(timeout=self.server.processor.packet_deadline_seconds + 1.0)
        if thermal_receiver is not None:
            thermal_receiver.join(
                timeout=self.thermal_udp_server.reassembler.frame_timeout_seconds + 1.0
            )
        self.sensor_data_logger.stop()
        try:
            self.tts.close()
        except Exception as error:
            self.store.record_runtime_error("tts", error)

    def receiver_stats(self) -> dict[str, object]:
        return {
            **asdict(self.server.stats),
            "host": self.server.host,
            "port": self.server.port,
            "runtime_started": self._started,
            "unexpected_tcp_thermal_packets": self._unexpected_tcp_thermal_packets,
            "thermal_udp": self.thermal_udp_server.stats(),
            "sensor_logging": self.sensor_data_logger.diagnostics(),
            "tts": self._tts_status(),
        }

    def _on_tcp_packet(self, packet, peer) -> None:
        if isinstance(packet, ThermalFrame):
            self._unexpected_tcp_thermal_packets += 1
            return
        self._on_packet(packet, peer)

    def _on_thermal_frame(self, frame: ThermalFrame, peer) -> None:
        self._on_packet(frame, peer)

    def _on_packet(self, packet, peer) -> None:
        wall = time.time()
        monotonic = time.monotonic()
        self.manager.ingest(
            packet,
            peer,
            received_at=wall,
            monotonic_at=monotonic,
        )
        if isinstance(packet, TelemetryPayload):
            # B23 needs ~30 s of causal phase at wire rate. Publication is too
            # slow to satisfy that contract on its own.
            try:
                self.ai_pipeline.observe_telemetry(packet)
            except Exception as error:
                self.store.record_runtime_error("mmwave_phase_window", error)
        try:
            self.sensor_data_logger.submit(
                packet,
                received_at=wall,
                monotonic_at=monotonic,
            )
        except Exception as error:
            self.store.record_runtime_error("sensor_logging", error)

    def _on_receiver_error(self, error: Exception, peer) -> None:
        if peer is not None and isinstance(error, ProtocolError):
            self.manager.mark_peer_disconnected(peer)
        if isinstance(error, ConnectionClosed):
            return
        source = "listener" if peer is None else f"receiver:{peer[0]}:{peer[1]}"
        self.store.record_runtime_error(source, error)

    def _on_thermal_udp_error(self, error: Exception, peer) -> None:
        source = "thermal_udp" if peer is None else f"thermal_udp:{peer[0]}:{peer[1]}"
        self.store.record_runtime_error(source, error)

    def _evaluation_loop(self) -> None:
        while not self._stop_event.wait(self.evaluation_interval_seconds):
            try:
                self.evaluate_once()
            except Exception as error:
                self.store.record_runtime_error("evaluation", error)

    def evaluate_once(self) -> dict[str, object]:
        state = self.manager.snapshot()
        ai = self.ai_pipeline.evaluate(state, self.manager.latest_thermal_frame())
        risk = self.risk_engine.evaluate(state, ai)
        risk_document = risk.to_dict()
        publication = self.store.publish(state, ai, risk_document)
        try:
            self.tts.handle_publication(publication)
        except Exception as error:
            self.store.record_runtime_error("tts", error)
        try:
            self.sensor_data_logger.set_analysis_context(ai, risk_document)
        except Exception as error:
            self.store.record_runtime_error("sensor_logging_context", error)
        return publication

    def _tts_status(self) -> dict[str, object]:
        try:
            return dict(self.tts.status())
        except Exception as error:
            return {
                "mode": "error",
                "available": False,
                "error": f"{type(error).__name__}: {error}",
            }
