from __future__ import annotations

from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest import mock
import wave

from services.tts import (
    AsyncRiskTTS,
    SpeechInterrupted,
    SubprocessSpeechBackend,
    _ALERT_SOUND_PATHS,
    _RECORDED_MESSAGE_PATHS,
    _select_engine,
    effective_risk_level,
    message_for_publication,
)


def publication(
    level: str | None,
    *,
    reasons: tuple[str, ...] = (),
    floors: tuple[str, ...] = (),
    caution_reasons: tuple[str, ...] = (),
    emergency_active: bool = False,
) -> dict[str, object]:
    return {
        "risk": {
            "risk_level": level,
            "reasons": reasons,
            "escalation_floors": floors,
            "caution_reasons": caution_reasons,
        },
        "emergency": {"active": emergency_active},
    }


class MutableClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


class FakeSpeechBackend:
    def __init__(self, *, block_warning: bool = False) -> None:
        self.block_warning = block_warning
        self.started: list[tuple[str, str]] = []
        self.warning_started = threading.Event()
        self.interrupt_calls = 0
        self._condition = threading.Condition()

    def speak(self, text: str, level: str, cancel_event: threading.Event) -> None:
        with self._condition:
            self.started.append((level, text))
            self._condition.notify_all()
        if level == "WARNING" and self.block_warning:
            self.warning_started.set()
            if cancel_event.wait(1.0):
                raise SpeechInterrupted()

    def interrupt(self) -> None:
        self.interrupt_calls += 1

    def status(self):
        return {"engine": "fake", "audio_device": "fake"}

    def wait_for_count(self, count: int, timeout: float = 1.0) -> bool:
        deadline = time.monotonic() + timeout
        with self._condition:
            while len(self.started) < count:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._condition.wait(remaining)
            return True


class RiskAwareTTSTests(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = MutableClock()
        self.backend = FakeSpeechBackend()
        self.tts = AsyncRiskTTS(
            self.backend,
            warning_cooldown_seconds=60.0,
            danger_cooldown_seconds=30.0,
            clock=self.clock,
        )
        self.tts.start()

    def tearDown(self) -> None:
        self.tts.close()

    def test_warning_requires_three_consecutive_decisions(self) -> None:
        self.assertFalse(self.tts.handle_publication(publication("WARNING")))
        self.assertFalse(self.tts.handle_publication(publication("WARNING")))
        self.assertEqual(self.backend.started, [])

        self.assertTrue(self.tts.handle_publication(publication("WARNING")))
        self.assertTrue(self.backend.wait_for_count(1))
        self.assertEqual([item[0] for item in self.backend.started], ["WARNING"])

    def test_non_warning_resets_warning_streak(self) -> None:
        self.assertFalse(self.tts.handle_publication(publication("WARNING")))
        self.assertFalse(self.tts.handle_publication(publication("WARNING")))
        self.assertFalse(self.tts.handle_publication(publication("NORMAL")))
        self.assertFalse(self.tts.handle_publication(publication("WARNING")))
        self.assertFalse(self.tts.handle_publication(publication("WARNING")))
        self.assertEqual(self.backend.started, [])

        self.assertTrue(self.tts.handle_publication(publication("WARNING")))
        self.assertTrue(self.backend.wait_for_count(1))

    def test_warning_reason_changes_keep_streak_and_latest_reason_is_spoken(self) -> None:
        self.assertFalse(self.tts.handle_publication(
            publication("WARNING", reasons=("CO2_HIGH",))
        ))
        self.assertFalse(self.tts.handle_publication(
            publication("WARNING", reasons=("LONG_NO_MOTION",))
        ))
        self.assertTrue(self.tts.handle_publication(
            publication("WARNING", reasons=("ABNORMAL_RESPIRATION",))
        ))
        self.assertTrue(self.backend.wait_for_count(1))
        self.assertIn("호흡 이상", self.backend.started[0][1])

    def test_continuous_warning_uses_sixty_second_reminder_cooldown(self) -> None:
        for _ in range(2):
            self.assertFalse(self.tts.handle_publication(publication("WARNING")))
        self.assertTrue(self.tts.handle_publication(publication("WARNING")))
        self.assertTrue(self.backend.wait_for_count(1))

        self.clock.now = 59.0
        self.assertFalse(self.tts.handle_publication(publication("WARNING")))
        self.clock.now = 60.0
        self.assertTrue(self.tts.handle_publication(publication("WARNING")))
        self.assertTrue(self.backend.wait_for_count(2))

    def test_warning_clear_requires_new_episode_confirmation(self) -> None:
        for _ in range(2):
            self.assertFalse(self.tts.handle_publication(publication("WARNING")))
        self.assertTrue(self.tts.handle_publication(publication("WARNING")))
        self.assertTrue(self.backend.wait_for_count(1))

        self.assertFalse(self.tts.handle_publication(publication("NORMAL")))
        self.clock.now = 60.0
        self.assertFalse(self.tts.handle_publication(publication("WARNING")))
        self.assertFalse(self.tts.handle_publication(publication("WARNING")))
        self.assertEqual(len(self.backend.started), 1)
        self.assertTrue(self.tts.handle_publication(publication("WARNING")))
        self.assertTrue(self.backend.wait_for_count(2))

    def test_reconfirmed_warning_waits_for_cooldown_without_recounting(self) -> None:
        for _ in range(2):
            self.assertFalse(self.tts.handle_publication(publication("WARNING")))
        self.assertTrue(self.tts.handle_publication(publication("WARNING")))
        self.assertTrue(self.backend.wait_for_count(1))

        self.assertFalse(self.tts.handle_publication(publication("NORMAL")))
        for now in (15.0, 30.0):
            self.clock.now = now
            self.assertFalse(self.tts.handle_publication(publication("WARNING")))
        self.clock.now = 45.0
        self.assertFalse(self.tts.handle_publication(publication("WARNING")))

        self.clock.now = 60.0
        self.assertTrue(self.tts.handle_publication(publication("WARNING")))
        self.assertTrue(self.backend.wait_for_count(2))

    def test_warning_to_danger_interrupts_and_supersedes(self) -> None:
        self.tts.close()
        backend = FakeSpeechBackend(block_warning=True)
        self.tts = AsyncRiskTTS(backend, clock=self.clock)
        self.tts.start()
        self.assertFalse(self.tts.handle_publication(publication("WARNING")))
        self.assertFalse(self.tts.handle_publication(publication("WARNING")))
        self.assertTrue(self.tts.handle_publication(publication("WARNING")))
        self.assertTrue(backend.warning_started.wait(1.0))

        self.assertTrue(self.tts.handle_publication(publication("DANGER")))
        self.assertTrue(backend.wait_for_count(2))
        self.assertEqual([item[0] for item in backend.started], ["WARNING", "DANGER"])
        self.assertEqual(backend.interrupt_calls, 1)

    def test_danger_is_immediate(self) -> None:
        self.assertTrue(self.tts.handle_publication(publication("DANGER")))
        self.assertTrue(self.backend.wait_for_count(1))

    def test_repeated_danger_uses_thirty_second_cooldown(self) -> None:
        self.assertTrue(self.tts.handle_publication(publication("DANGER")))
        self.assertTrue(self.backend.wait_for_count(1))
        self.clock.now = 1.0
        self.assertFalse(self.tts.handle_publication(publication("DANGER")))
        self.clock.now = 30.0
        self.assertTrue(self.tts.handle_publication(publication("DANGER")))
        self.assertTrue(self.backend.wait_for_count(2))

    def test_danger_interrupts_unconfirmed_warning_sequence(self) -> None:
        self.assertFalse(self.tts.handle_publication(publication("WARNING")))
        self.assertFalse(self.tts.handle_publication(publication("WARNING")))
        self.assertTrue(self.tts.handle_publication(publication("DANGER")))
        self.assertTrue(self.backend.wait_for_count(1))
        self.assertEqual(self.backend.started[0][0], "DANGER")

    def test_danger_resets_warning_confirmation(self) -> None:
        self.assertFalse(self.tts.handle_publication(publication("WARNING")))
        self.assertFalse(self.tts.handle_publication(publication("WARNING")))
        self.assertTrue(self.tts.handle_publication(publication("DANGER")))
        self.assertTrue(self.backend.wait_for_count(1))
        self.assertFalse(self.tts.handle_publication(publication("WARNING")))
        self.assertFalse(self.tts.handle_publication(publication("WARNING")))
        self.assertTrue(self.tts.handle_publication(publication("WARNING")))
        self.assertTrue(self.backend.wait_for_count(2))
        self.assertEqual([item[0] for item in self.backend.started], ["DANGER", "WARNING"])

    def test_indeterminate_is_silent_and_latched_emergency_remains_danger(self) -> None:
        self.assertFalse(self.tts.handle_publication(publication("INDETERMINATE")))
        self.assertEqual(self.backend.started, [])

        latched = publication(None, emergency_active=True)
        self.assertEqual(effective_risk_level(latched), "DANGER")
        self.assertTrue(self.tts.handle_publication(latched))
        self.assertTrue(self.backend.wait_for_count(1))
        self.assertNotIn("안전", self.backend.started[0][1])

    def test_existing_reason_taxonomy_selects_specific_messages(self) -> None:
        fall = publication(
            "DANGER", floors=("thermal_fall_confident",), emergency_active=True
        )
        apnea = publication(
            "DANGER", floors=("mmwave_apnea_hardware_verified",), emergency_active=True
        )
        co2 = publication("DANGER", reasons=("CO2_IMMEDIATE_DANGER",))
        respiration = publication("WARNING", reasons=("ABNORMAL_RESPIRATION_RPM",))
        self.assertIn("낙상", message_for_publication(fall, "DANGER"))
        self.assertIn("호흡 이상", message_for_publication(apnea, "DANGER"))
        self.assertIn("이산화탄소", message_for_publication(co2, "DANGER"))
        self.assertIn("호흡 이상", message_for_publication(respiration, "WARNING"))

    def test_warning_co2_fast_rise_beats_abnormal_respiration(self) -> None:
        event = publication(
            "WARNING",
            reasons=(
                "FLOOR_CO2_FAST_RISE",
                "VERY_FAST_CO2_RISE",
                "ABNORMAL_RESPIRATION_RPM_SUSTAINED",
            ),
            floors=("co2_fast_rise",),
            caution_reasons=("co2_fast_rise",),
        )
        text = message_for_publication(event, "WARNING")
        self.assertIn("이산화탄소", text)
        self.assertNotIn("호흡 이상 징후", text)

    def test_warning_co2_relative_beats_abnormal_respiration(self) -> None:
        event = publication(
            "WARNING",
            reasons=("CO2_RELATIVE_RISE", "ABNORMAL_RESPIRATION_RPM"),
            floors=("co2_relative_warning",),
            caution_reasons=("co2_relative_warning",),
        )
        text = message_for_publication(event, "WARNING")
        self.assertIn("이산화탄소", text)
        self.assertNotIn("호흡 이상 징후", text)

    def test_warning_without_co2_floor_still_speaks_respiration(self) -> None:
        text = message_for_publication(
            publication("WARNING", reasons=("ABNORMAL_RESPIRATION",)),
            "WARNING",
        )
        self.assertIn("호흡 이상 징후", text)
        self.assertNotIn("이산화탄소", text)

    def test_normal_abnormal_respiration_does_not_speak(self) -> None:
        self.assertFalse(
            self.tts.handle_publication(
                publication("NORMAL", reasons=("ABNORMAL_RESPIRATION_RPM",))
            )
        )
        self.assertEqual(self.backend.started, [])

    def test_danger_hardware_apnea_keeps_severe_respiration_message(self) -> None:
        event = publication(
            "DANGER",
            reasons=("MMWAVE_APNEA_HARDWARE_VERIFIED",),
            floors=("mmwave_apnea_hardware_verified",),
            emergency_active=True,
        )
        text = message_for_publication(event, "DANGER")
        self.assertIn("심각한 호흡 이상", text)
        self.assertNotIn("호흡 이상 징후", text)


class KoreanPiperBackendTests(unittest.TestCase):
    def test_auto_prefers_installed_piper_model(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            model = Path(temporary) / "ko_KR-kss-medium.onnx"
            model.write_bytes(b"model")
            with mock.patch("services.tts._piper_available", return_value=True):
                self.assertEqual(_select_engine("auto", model), "piper")

    def test_piper_runs_from_the_runtime_python_environment(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            model = Path(temporary) / "ko_KR-kss-medium.onnx"
            model.write_bytes(b"model")
            backend = SubprocessSpeechBackend(engine="piper", piper_model=model)
            commands: list[tuple[list[str], str | None]] = []

            def fake_run(command, _cancel_event, *, input_text=None) -> None:
                commands.append((command, input_text))
                if "--output_file" in command:
                    output = Path(command[command.index("--output_file") + 1])
                    output.write_bytes(b"0" * 45)

            with mock.patch.object(backend, "_run", side_effect=fake_run):
                backend.speak("주의가 필요합니다.", "WARNING", threading.Event())

        self.assertEqual(commands[0][0][1:3], ["-m", "piper"])
        self.assertEqual(commands[0][1], "주의가 필요합니다.")
        self.assertEqual(len(commands), 3)
        self.assertEqual(commands[1][0][:2], ["aplay", "-q"])
        self.assertEqual(Path(commands[1][0][-1]).name, "warning_chime.wav")
        self.assertEqual(Path(commands[2][0][-1]).name, "speech.wav")

    def test_danger_siren_plays_before_the_danger_message(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            model = Path(temporary) / "ko_KR-kss-medium.onnx"
            model.write_bytes(b"model")
            backend = SubprocessSpeechBackend(engine="piper", piper_model=model)
            commands: list[list[str]] = []

            def fake_run(command, _cancel_event, *, input_text=None) -> None:
                commands.append(command)
                if "--output_file" in command:
                    output = Path(command[command.index("--output_file") + 1])
                    output.write_bytes(b"0" * 45)

            with mock.patch.object(backend, "_run", side_effect=fake_run):
                backend.speak("위험 상황입니다.", "DANGER", threading.Event())

        self.assertEqual(len(commands), 3)
        self.assertEqual(Path(commands[1][-1]).name, "danger_siren.wav")
        self.assertEqual(Path(commands[2][-1]).name, "speech.wav")

    def test_recorded_message_plays_after_alert_without_synthesis(self) -> None:
        backend = SubprocessSpeechBackend(engine="piper")
        commands: list[list[str]] = []
        text = message_for_publication(
            publication("WARNING", reasons=("ABNORMAL_RESPIRATION",)),
            "WARNING",
        )

        with mock.patch.object(
            backend,
            "_run",
            side_effect=lambda command, _cancel_event: commands.append(command),
        ):
            backend.speak(text, "WARNING", threading.Event())

        self.assertEqual(len(commands), 2)
        self.assertEqual(Path(commands[0][-1]).name, "warning_chime.wav")
        self.assertEqual(Path(commands[1][-1]).name, "warning_respiration.wav")

    def test_every_risk_message_maps_to_the_matching_recorded_wav(self) -> None:
        cases = (
            (publication("WARNING", reasons=("ABNORMAL_RESPIRATION",)), "WARNING", "warning_respiration.wav"),
            (publication("WARNING", reasons=("CO2_HIGH",)), "WARNING", "warning_co2.wav"),
            (publication("WARNING", reasons=("LONG_NO_MOTION",)), "WARNING", "warning_no_motion.wav"),
            (publication("WARNING", reasons=("THERMAL_FALL",)), "WARNING", "warning_thermal.wav"),
            (publication("WARNING"), "WARNING", "warning_generic.wav"),
            (publication("DANGER", floors=("thermal_fall_confident",)), "DANGER", "danger_fall.wav"),
            (publication("DANGER", floors=("mmwave_apnea_hardware_verified",)), "DANGER", "danger_apnea.wav"),
            (publication("DANGER", reasons=("CO2_IMMEDIATE_DANGER",)), "DANGER", "danger_co2.wav"),
            (publication("DANGER"), "DANGER", "danger_generic.wav"),
        )
        for event, level, expected_name in cases:
            with self.subTest(level=level, expected_name=expected_name):
                text = message_for_publication(event, level)
                self.assertEqual(_RECORDED_MESSAGE_PATHS[text].name, expected_name)

    def test_recorded_message_wavs_are_aplay_compatible(self) -> None:
        self.assertEqual(len(_RECORDED_MESSAGE_PATHS), 9)
        for path in _RECORDED_MESSAGE_PATHS.values():
            with self.subTest(path=path), wave.open(str(path), "rb") as audio:
                self.assertEqual(audio.getnchannels(), 1)
                self.assertEqual(audio.getsampwidth(), 2)
                self.assertEqual(audio.getframerate(), 22_050)
                self.assertGreater(audio.getnframes(), 0)

    def test_alert_wav_durations_match_the_warning_and_danger_contract(self) -> None:
        expected_ranges = {
            "WARNING": (0.5, 1.0),
            "DANGER": (1.0, 2.0),
        }
        for level, (minimum, maximum) in expected_ranges.items():
            path = _ALERT_SOUND_PATHS[level]
            with self.subTest(level=level), wave.open(str(path), "rb") as audio:
                self.assertEqual(audio.getnchannels(), 1)
                self.assertEqual(audio.getsampwidth(), 2)
                duration = audio.getnframes() / audio.getframerate()
                self.assertGreaterEqual(duration, minimum)
                self.assertLessEqual(duration, maximum)

    def test_interrupt_terminates_an_active_alert_playback_process(self) -> None:
        backend = SubprocessSpeechBackend(engine="piper")
        process = mock.Mock()
        process.poll.return_value = None
        backend._active_process = process

        backend.interrupt()

        process.terminate.assert_called_once_with()


class ExplodingTTS:
    def start(self) -> None:
        return None

    def handle_publication(self, _publication) -> bool:
        raise RuntimeError("injected TTS failure")

    def status(self):
        return {"mode": "test"}

    def close(self) -> None:
        return None


class ExplodingStartTTS(ExplodingTTS):
    def start(self) -> None:
        raise RuntimeError("injected TTS initialization failure")

    def handle_publication(self, _publication) -> bool:
        return False


class RuntimeTTSIsolationTests(unittest.TestCase):
    @staticmethod
    def _runtime_imports():
        try:
            from backend.runtime import SafeNestRuntime
            from backend.store import RuntimeStore
            from storage.sensor_logger import SensorStorageConfig
        except ImportError as error:
            raise unittest.SkipTest(f"runtime imports unavailable: {error}") from error
        return SafeNestRuntime, RuntimeStore, SensorStorageConfig

    def test_tts_failure_does_not_prevent_publication(self) -> None:
        SafeNestRuntime, RuntimeStore, SensorStorageConfig = self._runtime_imports()
        with tempfile.TemporaryDirectory() as temporary:
            store = RuntimeStore()
            runtime = SafeNestRuntime(
                sensor_port=0,
                store=store,
                storage_config=SensorStorageConfig(
                    root=Path(temporary),
                    enabled=False,
                ),
                tts=ExplodingTTS(),
            )
            result = runtime.evaluate_once()

        self.assertEqual(result["publication_revision"], 1)
        self.assertTrue(store.diagnostics()["ready"])
        self.assertEqual(
            store.diagnostics()["last_error"]["details"]["source"],
            "tts",
        )

    def test_tts_start_failure_does_not_prevent_runtime_start(self) -> None:
        SafeNestRuntime, RuntimeStore, SensorStorageConfig = self._runtime_imports()
        with tempfile.TemporaryDirectory() as temporary:
            store = RuntimeStore()
            runtime = SafeNestRuntime(
                sensor_host="127.0.0.1",
                sensor_port=0,
                thermal_udp_host="127.0.0.1",
                thermal_udp_port=0,
                store=store,
                storage_config=SensorStorageConfig(
                    root=Path(temporary),
                    enabled=False,
                ),
                tts=ExplodingStartTTS(),
            )
            with (
                mock.patch.object(runtime.server, "serve_forever"),
                mock.patch.object(runtime.server, "stop"),
                mock.patch.object(runtime.thermal_udp_server, "serve_forever"),
                mock.patch.object(runtime.thermal_udp_server, "stop"),
            ):
                runtime.start()
                try:
                    self.assertTrue(store.diagnostics()["ready"])
                    self.assertTrue(runtime.receiver_stats()["runtime_started"])
                finally:
                    runtime.stop()


if __name__ == "__main__":
    unittest.main()
