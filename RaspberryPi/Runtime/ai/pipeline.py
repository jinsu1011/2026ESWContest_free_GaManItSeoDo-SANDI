"""Convert current sensor state into isolated model/rule evaluations."""

from __future__ import annotations

import time
from typing import Any, Mapping

from ai.result import AIResult
from ai.runtime import LazyModel
from ai.co2_canonical_runtime import (
    CO2BaselineLock,
    CO2SlopeWindowBuilder,
    h150_model_input_eligible,
)
from ai.mmwave_b23_runtime import B23TeamRuntime
from gateway.protocol import TelemetryPayload, ThermalFrame
from state.manager import SensorStateManager


class OnDeviceAIPipeline:
    def __init__(
        self,
        manager: SensorStateManager,
        models: Mapping[str, object] | None = None,
        *,
        clock=time.time,
    ) -> None:
        self.manager = manager
        supplied = dict(models or {})
        self.models = {
            sensor_id: supplied.get(sensor_id, LazyModel(sensor_id))
            for sensor_id in ("thermal", "mmwave", "co2")
        }
        self._clock = clock
        self._mmwave_b23 = B23TeamRuntime()
        self._mmwave_wire_observed = False
        self._co2_window = CO2SlopeWindowBuilder()
        self._co2_baseline = CO2BaselineLock.from_risk_config()
        self._co2_wire_observed = False
        self._co2_domain: dict[str, Any] = {}
        self._co2_ingested_sensor_model: str | None = None

    def observe_telemetry(self, packet: TelemetryPayload) -> None:
        """Accumulate the MR60 phase stream at wire rate, not at publication rate.

        Both accumulators are fed here. B23 needs ~30 s of causal phase
        coverage (R1 owns resampling to 300 @ 10 Hz). The C-B6 CO2 slope needs
        >=150 s of measurement-event history. Feeding either from ``evaluate``
        would sample the stream once per publication interval (15 s by default),
        which can never satisfy those contracts.

        Nested ``mmwave.seq`` is the physical phase-event identity. Outer
        packet sequence is publication identity only. Same nested seq across
        100 ms snapshots is not a new B23 sample.
        """

        self._mmwave_b23.observe_packet(packet)
        self._mmwave_wire_observed = True
        self.observe_co2(_co2_sensor_from_packet(packet))

    def observe_co2(self, sensor: Mapping[str, object]) -> None:
        """Accumulate CO2 measurement events at state-update rate.

        The canonical slope needs >=150 s of source-clock history anchored on
        ``measurement_event_id``; sampling it from the publication loop would
        both starve the history and let the 60 s presentation throttle
        masquerade as the physical measurement cadence.

        Live H150 ingest is fail-closed: missing event identity is not
        synthesized from ``seq``, and MH-Z19B preheat-unknown/false samples
        never enter the frozen C-B6 window. Slope math itself is unchanged.
        """

        self._co2_wire_observed = True
        self._remember_co2_domain(sensor)
        if not h150_model_input_eligible(sensor):
            return
        self._reset_co2_history_on_device_domain_change(sensor)
        self._co2_window.observe(sensor)
        self._co2_baseline.observe(sensor)

    def evaluate(
        self,
        snapshot: dict[str, object] | None = None,
        thermal_frame: ThermalFrame | None = None,
    ) -> dict[str, object]:
        current = self.manager.snapshot() if snapshot is None else snapshot
        frame = self.manager.latest_thermal_frame() if thermal_frame is None else thermal_frame
        timestamp = float(current.get("timestamp", self._clock()))
        sensors = current["sensors"]

        results = {
            "thermal": self._thermal(sensors["thermal"], frame, timestamp),
            "mmwave": self._mmwave(sensors["mmwave"], timestamp),
            "co2": self._co2(sensors["co2"], timestamp),
            "pir": self._pir(sensors["pir"], timestamp),
        }
        model_results = [results[name] for name in ("thermal", "mmwave", "co2")]
        return {
            "timestamp": timestamp,
            "state_revision": current.get("revision"),
            "ai": {name: result.to_dict() for name, result in results.items()},
            "all_models_available": all(result.available for result in model_results),
            "degraded": any(not result.available for result in model_results),
        }

    def _thermal(self, sensor: dict[str, object], frame: ThermalFrame | None, now: float) -> AIResult:
        unavailable = self._sensor_unavailable("thermal", sensor, now)
        if unavailable:
            return unavailable
        if frame is None:
            return self._unavailable("thermal", now, "THERMAL_FRAME_MISSING")
        try:
            import numpy as np

            pixels = np.frombuffer(frame.pixel_bytes, dtype=">u2").astype(np.float32)
            pixels = pixels.reshape(frame.height, frame.width)
        except Exception as error:
            return self._model_error("thermal", now, error)
        metadata = {
            "raw_minimum": frame.minimum_raw,
            "raw_maximum": frame.maximum_raw,
            "temperature_calibrated": False,
            "preprocessing": "per_frame_minmax",
            "heatmap_preview": _thermal_preview(pixels),
            "model_selector": getattr(
                self.models["thermal"], "model_selector", "thermal"
            ),
            "source_geometry_bridge": "TEAM_RUNTIME_62X80_AS_RECEIVED_EXPERIMENTAL_BRIDGE",
        }
        try:
            prediction = self.models["thermal"].predict(pixels)
            model_meta = getattr(self.models["thermal"], "model_meta", {})
            if not isinstance(model_meta, Mapping):
                model_meta = {}
            preprocessing_id = str(
                model_meta.get("preprocessing_id") or metadata["preprocessing"]
            )
            metadata.update(
                {
                    "preprocessing": preprocessing_id,
                    "preprocessing_id": preprocessing_id,
                    "probabilities": list(prediction.probabilities),
                    "model_selector": getattr(
                        prediction,
                        "model_selector",
                        getattr(self.models["thermal"], "model_selector", "thermal"),
                    ),
                    "model_sha256": getattr(
                        prediction, "model_sha256", model_meta.get("sha256")
                    ),
                    "safety_authority": model_meta.get("safety_authority", True),
                    "risk_authority": model_meta.get("risk_authority"),
                    "risk_contribution": model_meta.get("risk_contribution"),
                    "runtime_role": model_meta.get("runtime_role"),
                    "safety_semantic": model_meta.get("safety_semantic"),
                    "thermal_overlay_applied": bool(
                        getattr(prediction, "overlay_applied", False)
                    ),
                    "thermal_posture_source": str(
                        getattr(prediction, "posture_source", "MODEL")
                    ),
                    "thermal_model_class_name": str(
                        getattr(prediction, "model_class_name", "")
                        or prediction.class_name
                    ),
                    "thermal_bbox_height": getattr(prediction, "bbox_height", None),
                    "thermal_bbox_width": getattr(prediction, "bbox_width", None),
                }
            )
            return self._prediction_result(
                "thermal",
                prediction,
                now,
                score=(
                    1.0
                    if prediction.class_name == "HUMAN_FALL"
                    else float(model_meta.get("proxy_risk_score", 0.4))
                    if prediction.class_name == "HUMAN_FALL_PROXY"
                    else 0.0
                ),
                metadata=metadata,
            )
        except Exception as error:
            return self._model_error("thermal", now, error, metadata)

    def _mmwave(self, sensor: dict[str, object], now: float) -> AIResult:
        unavailable = self._sensor_unavailable("mmwave", sensor, now)
        if unavailable:
            return unavailable
        # Default active path is frozen B23. The injected LazyModel/M-N9 adapter
        # is never called. Snapshot-driven tests without observe_telemetry are
        # admitted inside B23TeamRuntime.evaluate.
        return self._mmwave_b23.evaluate(sensor, now)

    def _co2(self, sensor: dict[str, object], now: float) -> AIResult:
        unavailable = self._sensor_unavailable("co2", sensor, now)
        if unavailable:
            return unavailable
        if not self._co2_wire_observed:
            # Snapshot-driven callers (offline replay, unit tests) have no wire feed.
            self._co2_window.observe(sensor)
            self._co2_baseline.observe(sensor)
        slope = self._co2_window.latest()
        diagnostics = dict(slope.metadata)
        diagnostics.update(self._co2_baseline.latest().as_metadata())
        if self._co2_domain:
            diagnostics.update(self._co2_domain)
        if not slope.ready or slope.ppm is None or slope.slope_ppm_per_min is None:
            return self._unavailable(
                "co2",
                now,
                slope.reason or slope.status,
                diagnostics,
                state=slope.status,
            )
        diagnostics["co2_slope_ppm_per_min"] = slope.slope_ppm_per_min
        try:
            # C-B6 reduced contract: ppm and ppm/min only. Humidity and
            # temperature are in forbidden_additional_inputs.
            prediction = self.models["co2"].predict(slope.ppm, slope.slope_ppm_per_min)
            return self._prediction_result(
                "co2",
                prediction,
                now,
                # class_map declares risk_semantic NONE / safety_semantic NONE, so
                # occupancy must not enter the safety score as a hazard weight.
                score=0.0,
                metadata={
                    **diagnostics,
                    "probabilities": list(prediction.probabilities),
                    "occupancy_probability": getattr(prediction, "occupancy_probability", None),
                    "threshold": getattr(prediction, "threshold", None),
                    "contract_id": getattr(prediction, "contract_id", None),
                    "model_sha256": getattr(prediction, "model_sha256", None),
                    "risk_semantic": getattr(prediction, "risk_semantic", "NONE"),
                    "safety_semantic": getattr(prediction, "safety_semantic", "NONE"),
                    "risk_contribution_deferred": True,
                    "humidity_required": False,
                },
            )
        except Exception as error:
            return self._model_error("co2", now, error, diagnostics)

    @staticmethod
    def _pir(sensor: dict[str, object], now: float) -> AIResult:
        unavailable = OnDeviceAIPipeline._sensor_unavailable("pir", sensor, now)
        if unavailable:
            return unavailable
        motion = bool(sensor.get("values", {}).get("motion"))
        return AIResult(
            sensor_id="pir",
            timestamp=now,
            available=True,
            source="rule",
            state="MOTION" if motion else "NO_MOTION",
            score=0.0,
            confidence=1.0,
            metadata={"motion": motion, "risk_contribution_deferred": True},
        )

    @staticmethod
    def _sensor_unavailable(sensor_id: str, sensor: dict[str, object], now: float) -> AIResult | None:
        status = str(sensor.get("status", "NO_DATA"))
        if status == "LIVE":
            return None
        return OnDeviceAIPipeline._unavailable(sensor_id, now, f"SENSOR_{status}")

    @staticmethod
    def _prediction_result(
        sensor_id: str, prediction: object, now: float, *, score: float, metadata: dict[str, object]
    ) -> AIResult:
        return AIResult(
            sensor_id=sensor_id,
            timestamp=now,
            available=True,
            source="tflite",
            state=str(prediction.class_name),
            score=float(score),
            confidence=float(prediction.confidence),
            latency_ms=float(prediction.latency_ms),
            model_id=str(prediction.model_id),
            model_version=str(prediction.model_version),
            metadata=metadata,
        )

    @staticmethod
    def _model_error(
        sensor_id: str,
        now: float,
        error: Exception,
        metadata: dict[str, object] | None = None,
    ) -> AIResult:
        details = dict(metadata or {})
        details["detail"] = f"{type(error).__name__}: {error}"
        return OnDeviceAIPipeline._unavailable(
            sensor_id,
            now,
            "MODEL_RUNTIME_UNAVAILABLE",
            details,
        )

    @staticmethod
    def _unavailable(
        sensor_id: str,
        now: float,
        error: str,
        metadata: dict[str, object] | None = None,
        *,
        state: str = "INPUT_UNAVAILABLE",
    ) -> AIResult:
        return AIResult(
            sensor_id=sensor_id,
            timestamp=now,
            available=False,
            source="unavailable",
            state=state,
            error=error,
            metadata=metadata or {},
        )

    @staticmethod
    def _suppressed(
        sensor_id: str,
        now: float,
        reason: str,
        metadata: dict[str, object],
    ) -> AIResult:
        """Represent an intentional safety gate, not an invalid neural class."""
        return AIResult(
            sensor_id=sensor_id,
            timestamp=now,
            available=False,
            source="unavailable",
            state="RESPIRATORY_INFERENCE_SUPPRESSED",
            error=reason,
            metadata=metadata,
        )

    def _remember_co2_domain(self, sensor: Mapping[str, object]) -> None:
        values = sensor.get("values")
        values = values if isinstance(values, Mapping) else {}
        model = values.get("sensor_model")
        identity = values.get("event_identity_class")
        limitation = "EVENT_IDENTITY_UNVERIFIED"
        if identity == "INFERRED_UART_SAMPLE":
            limitation = "INFERRED_UART_SAMPLE_NOT_SCD40_DATA_READY"
        self._co2_domain = {
            "co2_sensor_model": model,
            "co2_event_identity_class": identity,
            "co2_preheat_complete": values.get("preheat_complete"),
            "abc_enabled": values.get("abc_enabled"),
            "configured_range_ppm": values.get("configured_range_ppm"),
            "co2_device_domain": model if isinstance(model, str) and model else "UNKNOWN",
            "co2_identity_limitation": limitation,
            "co2_h150_ingest_eligible": h150_model_input_eligible(sensor),
        }

    def _reset_co2_history_on_device_domain_change(
        self, sensor: Mapping[str, object]
    ) -> None:
        """MH-Z19B history must not be pooled with a prior SCD40 (or other) domain."""

        values = sensor.get("values")
        model = values.get("sensor_model") if isinstance(values, Mapping) else None
        if (
            self._co2_ingested_sensor_model is not None
            and isinstance(model, str)
            and model
            and model != self._co2_ingested_sensor_model
        ):
            self._co2_window.reset("DEVICE_DOMAIN")
            self._co2_baseline.reset("DEVICE_DOMAIN")
        if isinstance(model, str) and model:
            self._co2_ingested_sensor_model = model


def _co2_sensor_from_packet(packet: TelemetryPayload) -> dict[str, object]:
    """Map wire fields into the slope builder's sensor record.

    Does not invent ``measurement_event_id`` from publication ``seq``.
    """

    return {
        "device_id": packet.device_id,
        "boot_id": packet.boot_id,
        "values": {
            "measurement_event_valid": packet.co2_measurement_event_valid,
            "measurement_event_id": packet.co2_measurement_event_id,
            "measurement_monotonic_ms": packet.co2_measurement_monotonic_ms,
            "latest_measurement_ppm": packet.co2_ppm,
            "preheat_complete": packet.co2_preheat_complete,
            "sensor_model": packet.co2_sensor_model,
            "event_identity_class": packet.co2_event_identity_class,
            "abc_enabled": packet.abc_enabled,
            "configured_range_ppm": packet.configured_range_ppm,
        },
    }


def _thermal_preview(pixels: object, width: int = 20, height: int = 16) -> dict[str, object]:
    source_height, source_width = pixels.shape
    minimum = float(pixels.min())
    maximum = float(pixels.max())
    span = maximum - minimum
    x_indices = [round(index * (source_width - 1) / (width - 1)) for index in range(width)]
    y_indices = [round(index * (source_height - 1) / (height - 1)) for index in range(height)]
    values = []
    for y_index in y_indices:
        for x_index in x_indices:
            value = 0.0 if span <= 0 else (float(pixels[y_index, x_index]) - minimum) / span
            values.append(round(min(1.0, max(0.0, value)), 4))
    return {
        "width": width,
        "height": height,
        "source_width": int(source_width),
        "source_height": int(source_height),
        "normalized": True,
        "values": values,
    }
