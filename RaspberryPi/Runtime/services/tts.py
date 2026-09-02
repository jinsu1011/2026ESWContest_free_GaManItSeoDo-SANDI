"""Non-blocking, risk-aware Korean TTS output for the Raspberry Pi runtime.

This service consumes the final RuntimeStore publication.  It never calculates
risk and keeps synthesis/playback on a dedicated worker with one pending slot.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import importlib.util
import logging
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any, Callable, Mapping, Protocol


LOGGER = logging.getLogger(__name__)
_PRIORITY = {"WARNING": 1, "DANGER": 2}
_DEFAULT_WARNING_CONFIRMATIONS = 3
_DEFAULT_PIPER_MODEL = (
    Path(__file__).resolve().parents[1]
    / "data"
    / "tts"
    / "ko_KR-kss-medium.onnx"
)
_ALERT_SOUND_ROOT = Path(__file__).resolve().parents[1] / "assets" / "audio"
_ALERT_SOUND_PATHS = {
    "WARNING": _ALERT_SOUND_ROOT / "warning_chime.wav",
    "DANGER": _ALERT_SOUND_ROOT / "danger_siren.wav",
}
_RECORDED_MESSAGE_ROOT = _ALERT_SOUND_ROOT / "messages"
_RECORDED_MESSAGE_PATHS = {
    "주의가 필요합니다. 호흡 이상 징후가 감지되었습니다. 주변 상태를 확인해 주세요.":
        _RECORDED_MESSAGE_ROOT / "warning_respiration.wav",
    "주의가 필요합니다. 이산화탄소 농도 이상이 감지되었습니다. 환기 상태를 확인해 주세요.":
        _RECORDED_MESSAGE_ROOT / "warning_co2.wav",
    "주의가 필요합니다. 장시간 움직임이 감지되지 않았습니다. 주변 상태를 확인해 주세요.":
        _RECORDED_MESSAGE_ROOT / "warning_no_motion.wav",
    "주의가 필요합니다. 열화상 이상 징후가 감지되었습니다. 주변 상태를 확인해 주세요.":
        _RECORDED_MESSAGE_ROOT / "warning_thermal.wav",
    "주의가 필요합니다. 위험 징후가 감지되었습니다. 주변 상태를 확인해 주세요.":
        _RECORDED_MESSAGE_ROOT / "warning_generic.wav",
    "낙상 위험이 감지되었습니다. 즉시 상태를 확인해 주세요.":
        _RECORDED_MESSAGE_ROOT / "danger_fall.wav",
    "심각한 호흡 이상이 감지되었습니다. 즉시 상태를 확인해 주세요.":
        _RECORDED_MESSAGE_ROOT / "danger_apnea.wav",
    "이산화탄소 농도가 위험 수준입니다. 즉시 환기하고 현장을 확인해 주세요.":
        _RECORDED_MESSAGE_ROOT / "danger_co2.wav",
    "위험 상황이 감지되었습니다. 즉시 현장 상태를 확인해 주세요.":
        _RECORDED_MESSAGE_ROOT / "danger_generic.wav",
}


class TTSProtocol(Protocol):
    def start(self) -> None:
        """Start optional worker resources."""

    def handle_publication(self, publication: Mapping[str, Any]) -> bool:
        """Accept a final risk publication without blocking its producer."""

    def status(self) -> dict[str, Any]:
        """Return a JSON-safe diagnostic snapshot."""

    def close(self) -> None:
        """Stop speech and release optional audio resources."""


class SpeechBackendProtocol(Protocol):
    def speak(
        self,
        text: str,
        level: str,
        cancel_event: threading.Event,
    ) -> None:
        """Synthesize and play one utterance in the worker thread."""

    def interrupt(self) -> None:
        """Request interruption of the active synthesis or playback process."""

    def status(self) -> dict[str, Any]:
        """Return backend diagnostics."""


class SpeechInterrupted(RuntimeError):
    """Internal control flow for a warning superseded by DANGER."""


class DisabledTTS:
    """Explicit no-hardware/no-backend implementation for development hosts."""

    def __init__(self, mode: str = "disabled", error: str | None = None) -> None:
        self.mode = mode
        self.error = error

    def start(self) -> None:
        return None

    def handle_publication(self, publication: Mapping[str, Any]) -> bool:
        return False

    def status(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "available": False,
            "active": False,
            "pending_level": None,
            "error": self.error,
        }

    def close(self) -> None:
        return None


class SubprocessSpeechBackend:
    """Offline Piper/eSpeak synthesis followed by ALSA ``aplay`` playback."""

    def __init__(
        self,
        *,
        engine: str,
        audio_device: str | None = None,
        piper_model: str | Path | None = None,
        command_timeout_seconds: float = 30.0,
    ) -> None:
        if engine not in {"piper", "espeak-ng", "espeak"}:
            raise ValueError(f"unsupported TTS engine: {engine}")
        if command_timeout_seconds <= 0:
            raise ValueError("TTS command timeout must be positive")
        self.engine = engine
        self.audio_device = audio_device or None
        self.piper_model = Path(piper_model).expanduser() if piper_model else None
        self.command_timeout_seconds = float(command_timeout_seconds)
        self._process_lock = threading.RLock()
        self._active_process: subprocess.Popen[str] | None = None

    def speak(
        self,
        text: str,
        level: str,
        cancel_event: threading.Event,
    ) -> None:
        if cancel_event.is_set():
            raise SpeechInterrupted()
        with tempfile.TemporaryDirectory(prefix="safenest-tts-") as temporary:
            recorded_message = _RECORDED_MESSAGE_PATHS.get(text)
            if recorded_message is not None and recorded_message.is_file():
                output = recorded_message
            else:
                output = Path(temporary) / "speech.wav"
                if self.engine == "piper":
                    if self.piper_model is None or not self.piper_model.is_file():
                        raise RuntimeError("Piper Korean model file is unavailable")
                    command = [
                        sys.executable,
                        "-m",
                        "piper",
                        "--model",
                        str(self.piper_model),
                        "--output_file",
                        str(output),
                    ]
                    self._run(command, cancel_event, input_text=text)
                else:
                    speed, pitch, volume = (
                        (155, 55, 55) if level == "DANGER" else (140, 45, 45)
                    )
                    command = [
                        self.engine,
                        "-v",
                        "ko",
                        "-s",
                        str(speed),
                        "-p",
                        str(pitch),
                        "-a",
                        str(volume),
                        "-w",
                        str(output),
                        text,
                    ]
                    self._run(command, cancel_event)
            if not output.is_file() or output.stat().st_size <= 44:
                raise RuntimeError(f"TTS synthesis produced no usable WAV ({self.engine})")
            if cancel_event.is_set():
                raise SpeechInterrupted()
            alert_sound = _ALERT_SOUND_PATHS.get(level)
            if alert_sound is not None and alert_sound.is_file():
                self._play_wav(alert_sound, cancel_event)
            elif alert_sound is not None:
                LOGGER.warning("TTS alert sound is unavailable: %s", alert_sound)
            self._play_wav(output, cancel_event)

    def interrupt(self) -> None:
        with self._process_lock:
            process = self._active_process
            if process is not None and process.poll() is None:
                try:
                    process.terminate()
                except OSError:
                    return

    def status(self) -> dict[str, Any]:
        with self._process_lock:
            active = self._active_process is not None and self._active_process.poll() is None
        return {
            "engine": self.engine,
            "audio_backend": "aplay",
            "audio_device": self.audio_device or "default",
            "active_process": active,
            "piper_model": str(self.piper_model) if self.piper_model else None,
        }

    def _play_wav(self, wav_path: Path, cancel_event: threading.Event) -> None:
        play_command = ["aplay", "-q"]
        if self.audio_device:
            play_command.extend(("-D", self.audio_device))
        play_command.append(str(wav_path))
        self._run(play_command, cancel_event)

    def _run(
        self,
        command: list[str],
        cancel_event: threading.Event,
        *,
        input_text: str | None = None,
    ) -> None:
        if cancel_event.is_set():
            raise SpeechInterrupted()
        process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE if input_text is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        with self._process_lock:
            self._active_process = process
        try:
            try:
                output, _ = process.communicate(
                    input=input_text,
                    timeout=self.command_timeout_seconds,
                )
            except subprocess.TimeoutExpired as error:
                process.kill()
                output, _ = process.communicate()
                raise RuntimeError(
                    f"TTS command timed out: {Path(command[0]).name}: {output.strip()}"
                ) from error
            if cancel_event.is_set():
                raise SpeechInterrupted()
            if process.returncode != 0:
                raise RuntimeError(
                    f"TTS command failed ({process.returncode}): "
                    f"{Path(command[0]).name}: {output.strip()}"
                )
        finally:
            with self._process_lock:
                if self._active_process is process:
                    self._active_process = None


@dataclass
class _SpeechRequest:
    level: str
    text: str
    cancel_event: threading.Event = field(default_factory=threading.Event)

    @property
    def priority(self) -> int:
        return _PRIORITY[self.level]


class AsyncRiskTTS:
    """Transition/cooldown controller with a single-slot priority queue."""

    def __init__(
        self,
        backend: SpeechBackendProtocol,
        *,
        warning_cooldown_seconds: float = 60.0,
        danger_cooldown_seconds: float = 30.0,
        error_handler: Callable[[Exception], Any] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if warning_cooldown_seconds < 0 or danger_cooldown_seconds < 0:
            raise ValueError("TTS cooldowns must be non-negative")
        self.backend = backend
        self.warning_cooldown_seconds = float(warning_cooldown_seconds)
        self.danger_cooldown_seconds = float(danger_cooldown_seconds)
        self._error_handler = error_handler
        self._clock = clock
        self._condition = threading.Condition(threading.RLock())
        self._thread: threading.Thread | None = None
        self._pending: _SpeechRequest | None = None
        self._current: _SpeechRequest | None = None
        self._effective_level: str | None = None
        self._last_accepted_at: dict[str, float] = {}
        self._warning_streak_count = 0
        self._warning_confirmed = False
        self._closed = False

    def start(self) -> None:
        with self._condition:
            if self._closed or (self._thread is not None and self._thread.is_alive()):
                return
            self._thread = threading.Thread(
                target=self._worker,
                name="safenest-tts-worker",
                daemon=True,
            )
            self._thread.start()
            LOGGER.info("TTS initialized: %s", self.backend.status())

    def handle_publication(self, publication: Mapping[str, Any]) -> bool:
        level = effective_risk_level(publication)
        LOGGER.debug("TTS risk level received: %s", level)
        interrupt_active_warning = False
        with self._condition:
            if self._closed:
                return False
            previous = self._effective_level
            self._effective_level = level
            transition = previous != level

            if level != "WARNING":
                self._warning_streak_count = 0
                self._warning_confirmed = False

            if level not in _PRIORITY:
                if self._pending is not None:
                    self._pending.cancel_event.set()
                    self._pending = None
                return False

            now = self._clock()
            if level == "WARNING":
                if not self._warning_confirmed:
                    self._warning_streak_count += 1
                    if self._warning_streak_count < _DEFAULT_WARNING_CONFIRMATIONS:
                        LOGGER.debug(
                            "TTS warning confirmation pending: count=%s required=%s",
                            self._warning_streak_count,
                            _DEFAULT_WARNING_CONFIRMATIONS,
                        )
                        return False
                    self._warning_confirmed = True

            cooldown = (
                self.danger_cooldown_seconds
                if level == "DANGER"
                else self.warning_cooldown_seconds
            )
            last_accepted = self._last_accepted_at.get(level)
            if not transition and last_accepted is not None and now - last_accepted < cooldown:
                LOGGER.debug("TTS cooldown suppressed: level=%s", level)
                return False

            if self._pending is not None and self._pending.level != level and transition:
                LOGGER.info(
                    "TTS pending event superseded: %s -> %s",
                    self._pending.level,
                    level,
                )
                self._pending.cancel_event.set()
                self._pending = None
            if self._pending is not None:
                LOGGER.debug("TTS duplicate suppressed: level=%s", level)
                return False

            request = _SpeechRequest(level, message_for_publication(publication, level))
            if (
                level == "DANGER"
                and self._current is not None
                and self._current.priority < request.priority
            ):
                self._current.cancel_event.set()
                interrupt_active_warning = True
            self._pending = request
            self._last_accepted_at[level] = now
            self._condition.notify()
            LOGGER.info("TTS speech event accepted: level=%s", level)

        if interrupt_active_warning:
            self.backend.interrupt()
        return True

    def status(self) -> dict[str, Any]:
        with self._condition:
            result = {
                "mode": "async_offline",
                "available": True,
                "active": self._current is not None,
                "current_level": self._current.level if self._current else None,
                "pending_level": self._pending.level if self._pending else None,
                "effective_level": self._effective_level,
                "warning_cooldown_seconds": self.warning_cooldown_seconds,
                "danger_cooldown_seconds": self.danger_cooldown_seconds,
            }
        result.update(self.backend.status())
        return result

    def close(self) -> None:
        with self._condition:
            if self._closed:
                return
            self._closed = True
            if self._pending is not None:
                self._pending.cancel_event.set()
                self._pending = None
            if self._current is not None:
                self._current.cancel_event.set()
            thread = self._thread
            self._condition.notify_all()
        self.backend.interrupt()
        if thread is not None:
            thread.join(timeout=2.0)
            if thread.is_alive():
                LOGGER.warning("TTS worker did not stop within shutdown timeout")

    def _worker(self) -> None:
        while True:
            with self._condition:
                while self._pending is None and not self._closed:
                    self._condition.wait()
                if self._closed:
                    return
                request = self._pending
                self._pending = None
                self._current = request
            assert request is not None
            try:
                LOGGER.info("TTS speech started: level=%s", request.level)
                self.backend.speak(request.text, request.level, request.cancel_event)
                LOGGER.info("TTS speech completed: level=%s", request.level)
            except SpeechInterrupted:
                LOGGER.info("TTS speech interrupted: level=%s", request.level)
            except Exception as error:
                LOGGER.exception("TTS speech failed: level=%s", request.level)
                self._report_error(error)
            finally:
                with self._condition:
                    if self._current is request:
                        self._current = None

    def _report_error(self, error: Exception) -> None:
        if self._error_handler is None:
            return
        try:
            self._error_handler(error)
        except Exception:
            LOGGER.exception("TTS error handler failed")


def effective_risk_level(publication: Mapping[str, Any]) -> str | None:
    """Follow the final emergency latch before the raw risk level."""

    emergency = _mapping(publication.get("emergency"))
    if emergency.get("active") is True:
        return "DANGER"
    risk = _mapping(publication.get("risk"))
    raw_level = risk.get("risk_level")
    if raw_level is None and "risk" not in publication:
        raw_level = publication.get("risk_level")
    level = str(raw_level).upper() if raw_level is not None else None
    return level if level in {"NORMAL", "WARNING", "DANGER", "INDETERMINATE"} else None


_CO2_CAUTION_LABELS = frozenset(
    {
        "co2_fast_rise",
        "co2_relative_warning",
        "co2_immediate_danger",
    }
)
_CO2_WARNING_REASON_TOKENS = (
    "CO2_",
    "HIGH_CO2",
    "FAST_CO2",
    "VERY_FAST_CO2",
    "RELATIVE_RISE",
)


def message_for_publication(publication: Mapping[str, Any], level: str) -> str:
    """Map existing reason codes to Korean speech without making new decisions."""

    risk = _mapping(publication.get("risk"))
    if not risk and "risk" not in publication:
        risk = publication
    reasons = {
        str(item).upper()
        for item in risk.get("reasons", ())
        if isinstance(item, str)
    }
    reasons.update(
        f"FLOOR_{str(item).upper()}"
        for item in risk.get("escalation_floors", ())
        if isinstance(item, str)
    )

    if level == "DANGER":
        if _contains(reasons, "THERMAL_FALL_CONFIDENT", "EMERGENCY_HUMAN_FALL"):
            return "낙상 위험이 감지되었습니다. 즉시 상태를 확인해 주세요."
        if _contains(
            reasons,
            "MMWAVE_APNEA_HARDWARE_VERIFIED",
            "EMERGENCY_HARDWARE_VERIFIED_APNEA",
        ):
            return "심각한 호흡 이상이 감지되었습니다. 즉시 상태를 확인해 주세요."
        if _contains(reasons, "CO2_IMMEDIATE_DANGER", "HIGH_CO2_DANGER"):
            return "이산화탄소 농도가 위험 수준입니다. 즉시 환기하고 현장을 확인해 주세요."
        return "위험 상황이 감지되었습니다. 즉시 현장 상태를 확인해 주세요."

    if level == "WARNING":
        if _co2_caution_from_risk(risk, reasons):
            return (
                "주의가 필요합니다. 이산화탄소 농도 이상이 감지되었습니다. "
                "환기 상태를 확인해 주세요."
            )
        if _contains(reasons, "ABNORMAL_RESPIRATION", "APNEA_PROXY"):
            return (
                "주의가 필요합니다. 호흡 이상 징후가 감지되었습니다. "
                "주변 상태를 확인해 주세요."
            )
        if _contains(reasons, "LONG_NO_MOTION", "NO_MOTION_DETECTED"):
            return (
                "주의가 필요합니다. 장시간 움직임이 감지되지 않았습니다. "
                "주변 상태를 확인해 주세요."
            )
        if _contains(reasons, "THERMAL_FALL"):
            return (
                "주의가 필요합니다. 열화상 이상 징후가 감지되었습니다. "
                "주변 상태를 확인해 주세요."
            )
        return "주의가 필요합니다. 위험 징후가 감지되었습니다. 주변 상태를 확인해 주세요."

    return "주의가 필요합니다. 위험 징후가 감지되었습니다. 주변 상태를 확인해 주세요."


def create_tts_from_env(
    *,
    error_handler: Callable[[Exception], Any] | None = None,
) -> TTSProtocol:
    """Create the Pi backend or an explicit safe development fallback."""

    enabled = os.getenv("SAFENEST_TTS_ENABLED", "1").strip().lower()
    mode = os.getenv("SAFENEST_TTS_MODE", "auto").strip().lower()
    if enabled in {"0", "false", "no", "off"} or mode in {"off", "disabled", "none"}:
        LOGGER.info("TTS disabled by configuration")
        return DisabledTTS("disabled")

    piper_model_value = os.getenv("SAFENEST_TTS_PIPER_MODEL", "").strip()
    piper_model = (
        Path(piper_model_value).expanduser()
        if piper_model_value
        else _DEFAULT_PIPER_MODEL
    )
    engine = _select_engine(mode, piper_model)
    if shutil.which("aplay") is None or engine is None:
        detail = "aplay or an offline Korean TTS engine is unavailable"
        log = LOGGER.info if mode == "auto" else LOGGER.warning
        log("TTS fallback disabled: %s", detail)
        return DisabledTTS("auto_unavailable", detail)

    command_timeout = _float_env("SAFENEST_TTS_COMMAND_TIMEOUT_SECONDS", 30.0)
    backend = SubprocessSpeechBackend(
        engine=engine,
        audio_device=os.getenv("SAFENEST_TTS_AUDIO_DEVICE", "").strip() or None,
        piper_model=piper_model,
        command_timeout_seconds=command_timeout if command_timeout > 0 else 30.0,
    )
    return AsyncRiskTTS(
        backend,
        warning_cooldown_seconds=_float_env(
            "SAFENEST_TTS_WARNING_COOLDOWN_SECONDS", 60.0
        ),
        danger_cooldown_seconds=_float_env(
            "SAFENEST_TTS_DANGER_COOLDOWN_SECONDS", 30.0
        ),
        error_handler=error_handler,
    )


def _select_engine(mode: str, piper_model: Path | None) -> str | None:
    if mode == "auto":
        if piper_model is not None and piper_model.is_file() and _piper_available():
            return "piper"
        for candidate in ("espeak-ng", "espeak"):
            if shutil.which(candidate):
                return candidate
        return None
    if mode == "piper":
        if piper_model is not None and piper_model.is_file() and _piper_available():
            return mode
        return None
    if mode in {"espeak-ng", "espeak"} and shutil.which(mode):
        return mode
    return None


def _piper_available() -> bool:
    return importlib.util.find_spec("piper") is not None


def _contains(reasons: set[str], *tokens: str) -> bool:
    return any(token in reason for token in tokens for reason in reasons)


def _string_labels(value: object) -> tuple[str, ...]:
    if isinstance(value, str):
        return (value,)
    if isinstance(value, (list, tuple, set)):
        return tuple(str(item) for item in value if isinstance(item, str))
    return ()


def _is_co2_caution_label(label: str) -> bool:
    stripped = label.strip()
    lower = stripped.lower()
    if lower.startswith("floor_"):
        lower = lower[len("floor_") :]
    if lower in _CO2_CAUTION_LABELS:
        return True
    return stripped.upper().startswith("FLOOR_CO2_")


def _co2_caution_from_risk(risk: Mapping[str, Any], reasons: set[str]) -> bool:
    for key in ("caution_reasons", "escalation_floors"):
        for item in _string_labels(risk.get(key)):
            if _is_co2_caution_label(item):
                return True
    return _contains(reasons, *_CO2_WARNING_REASON_TOKENS)


def _mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _float_env(name: str, default: float) -> float:
    try:
        value = float(os.getenv(name, str(default)))
        return value if value >= 0 else default
    except ValueError:
        return default
