"""Background sensor recorder with per-sensor and global FIFO retention."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import queue
import shutil
import threading
import time
from typing import Any, Mapping

from gateway.protocol import TelemetryPayload, ThermalFrame


SENSOR_NAMES = ("mmwave", "co2", "thermal")
_STOP = object()


@dataclass(frozen=True)
class SensorStorageConfig:
    root: Path
    enabled: bool = True
    co2_interval_seconds: float = 60.0
    queue_capacity: int = 512
    thermal_batch_frames: int = 64
    thermal_flush_seconds: float = 2.0
    cleanup_interval_seconds: float = 30.0
    max_total_bytes: int = 10_000_000_000
    min_free_bytes: int = 2_000_000_000
    max_sensor_bytes: Mapping[str, int] | None = None

    def __post_init__(self) -> None:
        defaults = {
            "mmwave": 1_000_000_000,
            "co2": 250_000_000,
            "thermal": 8_500_000_000,
        }
        limits = defaults if self.max_sensor_bytes is None else dict(self.max_sensor_bytes)
        if set(limits) != set(SENSOR_NAMES):
            raise ValueError("sensor storage limits must define mmwave, co2, and thermal")
        positive = {
            "co2_interval_seconds": self.co2_interval_seconds,
            "thermal_flush_seconds": self.thermal_flush_seconds,
            "cleanup_interval_seconds": self.cleanup_interval_seconds,
        }
        for name, value in positive.items():
            if not math.isfinite(float(value)) or float(value) <= 0:
                raise ValueError(f"{name} must be positive and finite")
        for name, value in {
            "queue_capacity": self.queue_capacity,
            "thermal_batch_frames": self.thermal_batch_frames,
            "max_total_bytes": self.max_total_bytes,
            "min_free_bytes": self.min_free_bytes,
            **{f"max_{key}_bytes": item for key, item in limits.items()},
        }.items():
            if isinstance(value, bool) or int(value) < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        if self.queue_capacity < 1 or self.thermal_batch_frames < 1:
            raise ValueError("queue capacity and thermal batch size must be positive")
        object.__setattr__(self, "root", Path(self.root))
        object.__setattr__(self, "max_sensor_bytes", limits)

    @classmethod
    def from_env(cls, default_root: str | Path) -> "SensorStorageConfig":
        return cls(
            root=Path(_optional_env("SAFENEST_SENSOR_DATA_ROOT", str(default_root))),
            enabled=_bool_env("SAFENEST_SENSOR_DATA_ENABLED", True),
            co2_interval_seconds=_float_env("SAFENEST_CO2_UPDATE_INTERVAL_SECONDS", 60.0),
            queue_capacity=_int_env("SAFENEST_SENSOR_DATA_QUEUE_CAPACITY", 512),
            thermal_batch_frames=_int_env("SAFENEST_THERMAL_BATCH_FRAMES", 64),
            thermal_flush_seconds=_float_env("SAFENEST_THERMAL_FLUSH_SECONDS", 2.0),
            cleanup_interval_seconds=_float_env("SAFENEST_SENSOR_CLEANUP_INTERVAL_SECONDS", 30.0),
            max_total_bytes=_gb_env("SAFENEST_SENSOR_DATA_MAX_GB", 10.0),
            min_free_bytes=_gb_env("SAFENEST_MIN_FREE_DISK_GB", 2.0),
            max_sensor_bytes={
                "mmwave": _gb_env("SAFENEST_MMWAVE_DATA_MAX_GB", 1.0),
                "co2": _gb_env("SAFENEST_CO2_DATA_MAX_GB", 0.25),
                "thermal": _gb_env("SAFENEST_THERMAL_DATA_MAX_GB", 8.5),
            },
        )


@dataclass(frozen=True)
class _LogItem:
    sensor: str
    received_at: float
    payload: Any
    analysis: Mapping[str, Any] | None = None
    received_monotonic: float | None = None


class SensorDataLogger:
    """Queue sensor records without performing disk work in receiver callbacks."""

    def __init__(
        self,
        config: SensorStorageConfig,
        *,
        wall_clock=time.time,
        monotonic_clock=time.monotonic,
    ) -> None:
        self.config = config
        self._wall_clock = wall_clock
        self._monotonic_clock = monotonic_clock
        self._queue: queue.Queue[object] = queue.Queue(maxsize=config.queue_capacity)
        self._state_lock = threading.RLock()
        self._io_lock = threading.RLock()
        self._thread: threading.Thread | None = None
        self._running = False
        self._last_co2_monotonic: float | None = None
        self._last_co2_event_key: tuple[str, str, int] | None = None
        self._analysis: dict[str, Any] | None = None
        self._scalar_files: dict[str, tuple[str, Any, Path]] = {}
        self._last_cleanup = 0.0
        self._accepted = {name: 0 for name in SENSOR_NAMES}
        self._written = {name: 0 for name in SENSOR_NAMES}
        self._dropped = {name: 0 for name in SENSOR_NAMES}
        self._deleted = {name: 0 for name in SENSOR_NAMES}
        self._errors = 0
        self._last_error: str | None = None

    def start(self) -> None:
        if not self.config.enabled:
            return
        with self._state_lock:
            if self._running:
                return
            for sensor in SENSOR_NAMES:
                (self.config.root / sensor).mkdir(parents=True, exist_ok=True)
            self._running = True
            self._thread = threading.Thread(
                target=self._worker,
                name="safenest-sensor-data-writer",
                daemon=True,
            )
            self._thread.start()

    def stop(self, timeout: float = 10.0) -> None:
        with self._state_lock:
            if not self._running:
                return
            self._running = False
        thread = self._thread
        if thread is None or not thread.is_alive():
            return
        try:
            self._queue.put(_STOP, timeout=max(0.1, timeout / 2.0))
        except queue.Full:
            self._record_error(RuntimeError("sensor logger stop queue timeout"))
        if thread is not None:
            thread.join(timeout=timeout)
            if thread.is_alive():
                self._record_error(RuntimeError("sensor logger stop timeout"))

    def submit(
        self,
        packet: TelemetryPayload | ThermalFrame,
        *,
        received_at: float | None = None,
        monotonic_at: float | None = None,
    ) -> None:
        if not self.config.enabled:
            return
        wall = self._wall_clock() if received_at is None else float(received_at)
        monotonic = self._monotonic_clock() if monotonic_at is None else float(monotonic_at)
        if isinstance(packet, ThermalFrame):
            self._enqueue(
                _LogItem(
                    "thermal",
                    wall,
                    packet,
                    self._analysis_snapshot(),
                    received_monotonic=monotonic,
                )
            )
            return
        if not isinstance(packet, TelemetryPayload):
            return
        self._enqueue(_LogItem("mmwave", wall, packet, received_monotonic=monotonic))
        if not packet.valid.get("co2"):
            return
        with self._state_lock:
            event_key = None
            if (
                packet.co2_measurement_event_valid is True
                and packet.boot_id is not None
                and packet.co2_measurement_event_id is not None
            ):
                event_key = (packet.device_id, packet.boot_id, packet.co2_measurement_event_id)
            due = (
                event_key != self._last_co2_event_key
                if event_key is not None
                else self._last_co2_monotonic is None
                or monotonic - self._last_co2_monotonic >= self.config.co2_interval_seconds
            )
            if not due:
                return
            if self._enqueue(
                _LogItem("co2", wall, packet, received_monotonic=monotonic)
            ):
                self._last_co2_monotonic = monotonic
                if event_key is not None:
                    self._last_co2_event_key = event_key

    def set_analysis_context(
        self,
        ai: Mapping[str, Any],
        risk: Mapping[str, Any],
    ) -> None:
        thermal_ai = ai.get("ai", {}).get("thermal", {}) if isinstance(ai.get("ai"), Mapping) else {}
        context = {
            "ai": dict(thermal_ai) if isinstance(thermal_ai, Mapping) else {},
            "risk_level": risk.get("risk_level"),
            "risk_score": risk.get("risk_score"),
            "evaluation_timestamp": risk.get("timestamp"),
        }
        json.dumps(context, allow_nan=False)
        with self._state_lock:
            self._analysis = context

    def diagnostics(self) -> dict[str, Any]:
        with self._state_lock:
            return {
                "enabled": self.config.enabled,
                "running": self._running,
                "root": str(self.config.root),
                "queue_size": self._queue.qsize(),
                "queue_capacity": self.config.queue_capacity,
                "accepted": dict(self._accepted),
                "written": dict(self._written),
                "dropped": dict(self._dropped),
                "deleted": dict(self._deleted),
                "errors": self._errors,
                "last_error": self._last_error,
            }

    def cleanup_now(self) -> None:
        if not self.config.enabled:
            return
        with self._io_lock:
            self._cleanup()

    def _enqueue(self, item: _LogItem) -> bool:
        with self._state_lock:
            if not self._running:
                return False
        try:
            self._queue.put_nowait(item)
        except queue.Full:
            with self._state_lock:
                self._dropped[item.sensor] += 1
            return False
        with self._state_lock:
            self._accepted[item.sensor] += 1
        return True

    def _worker(self) -> None:
        thermal_batch: list[_LogItem] = []
        flush_deadline = self._monotonic_clock() + self.config.thermal_flush_seconds
        try:
            while True:
                timeout = max(0.05, flush_deadline - self._monotonic_clock())
                try:
                    queued = self._queue.get(timeout=timeout)
                except queue.Empty:
                    queued = None
                if queued is _STOP:
                    self._write_thermal_batch(thermal_batch)
                    break
                if isinstance(queued, _LogItem):
                    if queued.sensor == "thermal":
                        thermal_batch.append(queued)
                        if len(thermal_batch) >= self.config.thermal_batch_frames:
                            self._write_thermal_batch(thermal_batch)
                            thermal_batch.clear()
                            flush_deadline = self._monotonic_clock() + self.config.thermal_flush_seconds
                    else:
                        try:
                            self._write_scalar(queued)
                        except Exception as error:
                            self._record_error(error)
                if thermal_batch and self._monotonic_clock() >= flush_deadline:
                    self._write_thermal_batch(thermal_batch)
                    thermal_batch.clear()
                    flush_deadline = self._monotonic_clock() + self.config.thermal_flush_seconds
                try:
                    self._cleanup_if_due()
                except Exception as error:
                    self._record_error(error)
        except Exception as error:  # Logging failure must never stop sensor reception.
            self._record_error(error)
        finally:
            for _, handle, _ in self._scalar_files.values():
                try:
                    handle.close()
                except OSError as error:
                    self._record_error(error)
            self._scalar_files.clear()

    def _write_scalar(self, item: _LogItem) -> None:
        packet = item.payload
        if not isinstance(packet, TelemetryPayload):
            return
        if item.sensor == "mmwave":
            document = {
                "timestamp": item.received_at,
                "receive_monotonic": item.received_monotonic,
                "device_id": packet.device_id,
                "boot_id": packet.boot_id,
                "sequence": packet.header.sequence,
                "source_uptime_ms": packet.uptime_ms,
                "breath_phase": packet.breath_phase,
                "ts_monotonic_ms": packet.ts_monotonic_ms,
                "phase_age_ms": packet.phase_age_ms,
                "mmwave_sequence": packet.mmwave_sequence,
                "publication_sequence": packet.header.sequence,
                "breath_rate_raw": packet.breath_rate_raw,
                "human_detected_raw": packet.human_detected_raw,
                "presence": packet.human_detected_raw,
                "session_id": packet.session_id,
                "respiration_rate_bpm": packet.respiration_rate_bpm,
                "heart_rate_bpm": packet.heart_rate_bpm,
                "respiration_valid": packet.valid["respiration"],
                "heart_valid": packet.valid["heart"],
            }
        else:
            document = {
                "timestamp": item.received_at,
                "receive_monotonic": item.received_monotonic,
                "device_id": packet.device_id,
                "boot_id": packet.boot_id,
                "sequence": packet.header.sequence,
                "source_uptime_ms": packet.uptime_ms,
                "co2_ppm": packet.co2_ppm,
                "co2_measurement_event_id": packet.co2_measurement_event_id,
                "co2_measurement_monotonic_ms": packet.co2_measurement_monotonic_ms,
                "co2_measurement_event_valid": packet.co2_measurement_event_valid,
                "co2_sensor_model": packet.co2_sensor_model,
                "co2_event_identity_class": packet.co2_event_identity_class,
                "co2_preheat_complete": packet.co2_preheat_complete,
                "abc_enabled": packet.abc_enabled,
                "configured_range_ppm": packet.configured_range_ppm,
            }
        with self._io_lock:
            _, handle, _ = self._scalar_handle(item.sensor, item.received_at)
            handle.write(json.dumps(document, separators=(",", ":"), allow_nan=False) + "\n")
            handle.flush()
        with self._state_lock:
            self._written[item.sensor] += 1

    def _scalar_handle(self, sensor: str, timestamp: float) -> tuple[str, Any, Path]:
        stamp = datetime.fromtimestamp(timestamp, timezone.utc)
        segment = stamp.strftime("%Y%m%d_%H")
        current = self._scalar_files.get(sensor)
        if current is not None and current[0] == segment:
            return current
        if current is not None:
            current[1].close()
        path = self.config.root / sensor / f"{segment}_{sensor}.jsonl"
        handle = path.open("a", encoding="utf-8", newline="\n")
        selected = (segment, handle, path)
        self._scalar_files[sensor] = selected
        return selected

    def _write_thermal_batch(self, items: list[_LogItem]) -> None:
        if not items:
            return
        try:
            import numpy as np

            frames = []
            for item in items:
                frame = item.payload
                frames.append(
                    np.frombuffer(frame.pixel_bytes, dtype=">u2")
                    .astype(np.uint16)
                    .reshape(frame.height, frame.width)
                )
            first = items[0].payload
            last = items[-1].payload
            stamp = datetime.fromtimestamp(items[0].received_at, timezone.utc)
            name = (
                f"{stamp.strftime('%Y%m%d_%H%M%S_%f')}_"
                f"{first.frame_sequence:010d}-{last.frame_sequence:010d}.npz"
            )
            path = self.config.root / "thermal" / name
            temporary = path.with_suffix(".npz.tmp")
            analysis = [
                json.dumps(item.analysis or {}, separators=(",", ":"), allow_nan=False)
                for item in items
            ]
            with self._io_lock:
                with temporary.open("wb") as stream:
                    np.savez_compressed(
                        stream,
                        frames=np.stack(frames),
                        timestamps=np.asarray([item.received_at for item in items], dtype=np.float64),
                        receive_monotonic=np.asarray([item.received_monotonic for item in items], dtype=np.float64),
                        frame_sequences=np.asarray([item.payload.frame_sequence for item in items], dtype=np.uint32),
                        source_uptime_ms=np.asarray([item.payload.uptime_ms for item in items], dtype=np.uint32),
                        minimum_raw=np.asarray([item.payload.minimum_raw for item in items], dtype=np.uint16),
                        maximum_raw=np.asarray([item.payload.maximum_raw for item in items], dtype=np.uint16),
                        analysis_json=np.asarray(analysis),
                    )
                os.replace(temporary, path)
            with self._state_lock:
                self._written["thermal"] += len(items)
        except Exception as error:
            self._record_error(error)
            try:
                temporary.unlink(missing_ok=True)
            except (OSError, UnboundLocalError):
                pass

    def _cleanup_if_due(self) -> None:
        now = self._monotonic_clock()
        if now - self._last_cleanup < self.config.cleanup_interval_seconds:
            return
        with self._io_lock:
            self._cleanup()
        self._last_cleanup = now

    def _cleanup(self) -> None:
        active = {entry[2].resolve() for entry in self._scalar_files.values()}
        per_sensor: dict[str, list[tuple[float, Path, int]]] = {}
        for sensor in SENSOR_NAMES:
            files = []
            directory = self.config.root / sensor
            directory.mkdir(parents=True, exist_ok=True)
            for path in directory.iterdir():
                if not path.is_file() or path.name.startswith(".") or path.suffix == ".tmp":
                    continue
                if path.resolve() in active:
                    continue
                stat = path.stat()
                files.append((stat.st_mtime, path, stat.st_size))
            files.sort(key=lambda item: (item[0], item[1].name))
            per_sensor[sensor] = files
            self._trim(files, int(self.config.max_sensor_bytes[sensor]), sensor)

        all_files = sorted(
            (item[0], item[1], item[2], sensor)
            for sensor, files in per_sensor.items()
            for item in files
            if item[1].exists()
        )
        total = sum(item[2] for item in all_files)
        while all_files and total > self.config.max_total_bytes:
            _, path, size, sensor = all_files.pop(0)
            if path.exists():
                path.unlink()
                total -= size
                self._deleted[sensor] += 1

        while all_files and shutil.disk_usage(self.config.root).free < self.config.min_free_bytes:
            _, path, size, sensor = all_files.pop(0)
            if path.exists():
                path.unlink()
                total -= size
                self._deleted[sensor] += 1

    def _trim(self, files: list[tuple[float, Path, int]], limit: int, sensor: str) -> None:
        total = sum(item[2] for item in files)
        while files and total > limit:
            _, path, size = files.pop(0)
            path.unlink()
            total -= size
            self._deleted[sensor] += 1

    def _analysis_snapshot(self) -> dict[str, Any] | None:
        with self._state_lock:
            if self._analysis is None:
                return None
            return json.loads(json.dumps(self._analysis, allow_nan=False))

    def _record_error(self, error: Exception) -> None:
        with self._state_lock:
            self._errors += 1
            self._last_error = f"{type(error).__name__}: {error}"


def _bool_env(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    normalized = raw.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be a boolean")


def _optional_env(name: str, default: str) -> str:
    value = os.getenv(name)
    return default if value is None or not value.strip() else value.strip()


def _float_env(name: str, default: float) -> float:
    return float(os.getenv(name, str(default)))


def _int_env(name: str, default: int) -> int:
    return int(os.getenv(name, str(default)))


def _gb_env(name: str, default: float) -> int:
    value = _float_env(name, default)
    if not math.isfinite(value) or value < 0:
        raise ValueError(f"{name} must be non-negative and finite")
    return int(value * 1_000_000_000)
