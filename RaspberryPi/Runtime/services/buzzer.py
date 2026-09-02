"""GPIO buzzer adapter with a deterministic development fallback.

The integration backend must also run on a development PC.  GPIO is therefore
loaded lazily and an unavailable Raspberry Pi backend becomes an explicitly
labelled simulated buzzer instead of preventing the service from starting.
"""

from __future__ import annotations

import os
import threading
from typing import Any, Protocol


class BuzzerProtocol(Protocol):
    def activate(self) -> bool:
        """Start the alarm and return whether the active state changed."""

    def silence(self) -> bool:
        """Stop the alarm and return whether the active state changed."""

    def status(self) -> dict[str, Any]:
        """Return a JSON-safe diagnostic snapshot."""

    def close(self) -> None:
        """Release optional hardware resources."""


class MockBuzzer:
    """No-hardware buzzer that makes simulation explicit in diagnostics."""

    def __init__(self, mode: str = "mock", error: str | None = None, pin: int | None = None) -> None:
        self.mode = mode
        self.error = error
        self.pin = pin
        self._active = False
        self._lock = threading.RLock()

    def activate(self) -> bool:
        with self._lock:
            changed = not self._active
            self._active = True
            return changed

    def silence(self) -> bool:
        with self._lock:
            changed = self._active
            self._active = False
            return changed

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "mode": self.mode,
                "available": False,
                "simulated": True,
                "active": self._active,
                "pin": self.pin,
                "error": self.error,
            }

    def close(self) -> None:
        self.silence()


class GPIOBuzzer:
    """Passive-piezo buzzer using optional RPi.GPIO PWM."""

    def __init__(
        self,
        pin: int = 18,
        frequency_hz: float = 880.0,
        *,
        gpio_module: Any | None = None,
    ) -> None:
        self.pin = int(pin)
        self.frequency_hz = float(frequency_hz)
        if self.pin < 0 or self.frequency_hz <= 0:
            raise ValueError("buzzer pin and frequency must be positive")
        self._gpio = gpio_module or _import_gpio()
        self._lock = threading.RLock()
        self._active = False
        self._pwm = None
        self._gpio.setmode(self._gpio.BCM)
        self._gpio.setup(self.pin, self._gpio.OUT, initial=self._gpio.LOW)
        pwm_factory = getattr(self._gpio, "PWM", None)
        if pwm_factory is not None:
            self._pwm = pwm_factory(self.pin, self.frequency_hz)
            self._pwm.start(0)

    def activate(self) -> bool:
        with self._lock:
            changed = not self._active
            if self._pwm is not None:
                self._pwm.ChangeDutyCycle(50 if not self._active else 50)
            else:
                self._gpio.output(self.pin, self._gpio.HIGH)
            self._active = True
            return changed

    def silence(self) -> bool:
        with self._lock:
            changed = self._active
            if self._pwm is not None:
                self._pwm.ChangeDutyCycle(0)
            else:
                self._gpio.output(self.pin, self._gpio.LOW)
            self._active = False
            return changed

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "mode": "gpio",
                "available": True,
                "simulated": False,
                "active": self._active,
                "pin": self.pin,
                "frequency_hz": self.frequency_hz,
                "error": None,
            }

    def close(self) -> None:
        with self._lock:
            try:
                self.silence()
                if self._pwm is not None:
                    self._pwm.stop()
                cleanup = getattr(self._gpio, "cleanup", None)
                if callable(cleanup):
                    cleanup(self.pin)
            except Exception:
                # Shutdown must never mask the original application error.
                return


def create_buzzer_from_env() -> BuzzerProtocol:
    mode = os.getenv("SAFENEST_GPIO_MODE", "auto").strip().lower()
    pin = _int_env("SAFENEST_BUZZER_GPIO_PIN", 18)
    frequency = _float_env("SAFENEST_BUZZER_FREQUENCY_HZ", 880.0)
    if mode in {"off", "disabled"}:
        return MockBuzzer("disabled", pin=pin)
    if mode in {"mock", "simulation", "simulated"}:
        return MockBuzzer("mock", pin=pin)
    try:
        return GPIOBuzzer(pin=pin, frequency_hz=frequency)
    except Exception as error:
        return MockBuzzer(
            "mock_fallback",
            error=f"{type(error).__name__}: {error}",
            pin=pin,
        )


def _import_gpio() -> Any:
    try:
        import RPi.GPIO as gpio  # type: ignore
    except ImportError as error:
        raise RuntimeError("RPi.GPIO is not installed") from error
    return gpio


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


def _float_env(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except ValueError:
        return default
