"""Stage 9 smoke tooling tests. Fixtures only; no hardware and no real sleeps."""

from __future__ import annotations

from pathlib import Path
import json
import unittest

from backend.views import status_document
from hil.stage9_evaluate import evaluate_esp_connection, evaluate_observation, evaluate_sensor_progress
from hil.stage9_smoke import fixture_document, live_document, main, plan_document
from hil.stage9_sockets import parse_listen_ports
from tests.test_runtime_status import ai_result


ROOT = Path(__file__).resolve().parent.parent
FIXTURE_DIR = ROOT / "tests" / "fixtures" / "stage9"
SS_PASS = (
    "LISTEN 0 4096 0.0.0.0:8000 0.0.0.0:*\n"
    "LISTEN 0 2 0.0.0.0:9000 0.0.0.0:*\n"
    "UNCONN 0 0 0.0.0.0:5005 0.0.0.0:*\n"
)


def sensor_state(
    sensor_id: str,
    *,
    sequence: int,
    last_received_at: float,
    values: dict[str, object],
    status: str = "LIVE",
) -> dict[str, object]:
    return {
        "sensor_id": sensor_id,
        "status": status,
        "connected": status != "DISCONNECTED",
        "stale": False,
        "valid": True,
        "current": True,
        "sequence": sequence,
        "last_received_at": last_received_at,
        "last_valid_at": last_received_at,
        "values": values,
    }


def status_snapshot(
    *,
    sequence: int,
    last_received_at: float,
    co2_event: int,
    co2_ppm: float = 700.0,
    motion: bool = False,
    thermal_frame: int | None = None,
    tcp_status: str = "LIVE",
) -> dict[str, object]:
    frame = sequence if thermal_frame is None else thermal_frame
    publication = {
        "timestamp": last_received_at,
        "publication_revision": sequence,
        "state": {
            "system": "ONLINE",
            "revision": sequence,
            "sensors": {
                "co2": sensor_state(
                    "co2",
                    sequence=sequence,
                    last_received_at=last_received_at,
                    status=tcp_status,
                    values={
                        "ppm": co2_ppm,
                        "latest_measurement_ppm": co2_ppm,
                        "measurement_event_id": co2_event,
                        "measurement_event_count": co2_event,
                        "measurement_event_valid": True,
                    },
                ),
                "thermal": sensor_state(
                    "thermal",
                    sequence=sequence,
                    last_received_at=last_received_at,
                    values={"frame_sequence": frame, "minimum_raw": 1000, "maximum_raw": 2000},
                ),
                "mmwave": sensor_state(
                    "mmwave",
                    sequence=sequence,
                    last_received_at=last_received_at,
                    status=tcp_status,
                    values={"respiration_rate_bpm": 15.0, "presence_available": False},
                ),
                "pir": sensor_state(
                    "pir",
                    sequence=sequence,
                    last_received_at=last_received_at,
                    status=tcp_status,
                    values={"motion": motion, "event_id": 1},
                ),
            },
        },
        "ai": {"ai": {"co2": ai_result(available=True)}},
        "risk": {"system_health": "HEALTHY", "components": {}},
        "emergency": {},
    }
    return status_document(publication)


def health_document(
    *,
    connections: int = 1,
    disconnects: int = 0,
    dropped: dict[str, int] | None = None,
    ok: bool = True,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "ok": ok,
        "ready": True,
        "publication_revision": 4,
        "receiver": {
            "connections": connections,
            "disconnects": disconnects,
            "port": 9000,
            "sensor_logging": {
                "enabled": True,
                "dropped": dropped or {"co2": 0, "thermal": 0, "mmwave": 0, "pir": 0},
            },
        },
    }
    return payload


def observation(
    *,
    before_seq: int = 4,
    after_seq: int = 8,
    co2_event_before: int = 1,
    co2_event_after: int = 2,
    ppm: float = 700.0,
    motion: bool = False,
    socket_table: str = SS_PASS,
    health_before: dict[str, object] | None = None,
    health_after: dict[str, object] | None = None,
    status_before: dict[str, object] | None = None,
    status_after: dict[str, object] | None = None,
    health_error_after: str | None = None,
    status_error_after: str | None = None,
    socket_error: str | None = None,
    dropped_before: dict[str, int] | None = None,
    dropped_after: dict[str, int] | None = None,
) -> dict[str, object]:
    return {
        "health_before": health_before
        if health_before is not None
        else health_document(dropped=dropped_before),
        "health_after": health_after
        if health_after is not None
        else health_document(dropped=dropped_after),
        "health_error_before": None,
        "health_error_after": health_error_after,
        "status_before": status_before
        if status_before is not None
        else status_snapshot(
            sequence=before_seq,
            last_received_at=float(before_seq),
            co2_event=co2_event_before,
            co2_ppm=ppm,
            motion=motion,
        ),
        "status_after": status_after
        if status_after is not None
        else status_snapshot(
            sequence=after_seq,
            last_received_at=float(after_seq),
            co2_event=co2_event_after,
            co2_ppm=ppm,
            motion=motion,
        ),
        "status_error_before": None,
        "status_error_after": status_error_after,
        "socket_table": socket_table,
        "socket_error": socket_error,
        "window_seconds": 20,
    }


class Stage9SmokeToolingTests(unittest.TestCase):
    def test_a_full_valid_fixture_passes_evaluator_only(self) -> None:
        evaluated = evaluate_observation(observation(), mode="OFFLINE_FIXTURE")
        self.assertEqual(evaluated["result"], "PASS")
        self.assertEqual(evaluated["probes"]["thermal_progress"]["status"], "PASS")
        self.assertEqual(evaluated["probes"]["runtime_status"]["observed"]["sensors"]["thermal"]["ai_status"], "BLOCKED")
        report = fixture_document(FIXTURE_DIR / "pass.json")
        self.assertEqual(report["result"], "PASS")
        self.assertEqual(report["mode"], "OFFLINE_FIXTURE")
        self.assertEqual(report["stage_9_live_smoke"], "NOT_RUN")

    def test_b_backend_unreachable_fails(self) -> None:
        payload = observation(health_after={}, health_error_after="unreachable: connection refused")
        payload["health_after"] = None
        payload["status_after"] = None
        payload["status_error_after"] = "unreachable: connection refused"
        evaluated = evaluate_observation(payload, mode="OFFLINE_FIXTURE")
        self.assertEqual(evaluated["probes"]["backend_health"]["status"], "FAIL")
        self.assertIn("unreachable", evaluated["probes"]["backend_health"]["reason"].lower())
        self.assertEqual(evaluated["result"], "FAIL")

    def test_c_tcp_9000_missing_fails(self) -> None:
        table = "LISTEN 0 4096 0.0.0.0:8000 0.0.0.0:*\nUNCONN 0 0 0.0.0.0:5005 0.0.0.0:*\n"
        evaluated = evaluate_observation(observation(socket_table=table), mode="OFFLINE_FIXTURE")
        self.assertEqual(evaluated["probes"]["tcp_9000"]["status"], "FAIL")
        self.assertEqual(evaluated["result"], "FAIL")

    def test_d_udp_5005_missing_fails(self) -> None:
        table = "LISTEN 0 4096 0.0.0.0:8000 0.0.0.0:*\nLISTEN 0 2 0.0.0.0:9000 0.0.0.0:*\n"
        evaluated = evaluate_observation(observation(socket_table=table), mode="OFFLINE_FIXTURE")
        self.assertEqual(evaluated["probes"]["udp_5005"]["status"], "FAIL")
        self.assertEqual(evaluated["result"], "FAIL")

    def test_e_thermal_progress_with_blocked_ai_is_not_sensor_failure(self) -> None:
        evaluated = evaluate_observation(observation(), mode="OFFLINE_FIXTURE")
        self.assertEqual(evaluated["probes"]["thermal_progress"]["status"], "PASS")
        runtime = evaluated["probes"]["runtime_status"]
        self.assertEqual(runtime["status"], "PASS")
        self.assertEqual(runtime["observed"]["sensors"]["thermal"]["ai_status"], "BLOCKED")
        self.assertTrue(runtime["observed"]["thermal_sensor_available_ai_blocked"])

    def test_f_pir_no_motion_is_valid(self) -> None:
        evaluated = evaluate_observation(observation(motion=False), mode="OFFLINE_FIXTURE")
        pir = evaluated["probes"]["pir_progress"]
        self.assertEqual(pir["status"], "PASS")
        self.assertEqual(pir["observed"]["sensor_value_status"], "NO_MOTION")
        self.assertEqual(
            evaluated["probes"]["runtime_status"]["observed"]["sensors"]["pir"]["ai_status"],
            "NOT_APPLICABLE",
        )

    def test_g_co2_same_ppm_new_measurement_identity_counts(self) -> None:
        payload = observation(co2_event_before=3, co2_event_after=4, ppm=812.0)
        probe = evaluate_sensor_progress(payload, "co2")
        self.assertEqual(probe["status"], "PASS")
        self.assertEqual(probe["observed"]["ppm_before"], 812.0)
        self.assertEqual(probe["observed"]["ppm_after"], 812.0)
        self.assertGreater(
            probe["observed"]["identities"]["values.measurement_event_count"]["after"],
            probe["observed"]["identities"]["values.measurement_event_count"]["before"],
        )

    def test_co2_same_physical_event_newer_last_received_at_does_not_pass(self) -> None:
        payload = observation(
            before_seq=4,
            after_seq=8,
            co2_event_before=52,
            co2_event_after=52,
        )
        payload["status_before"] = status_snapshot(
            sequence=4, last_received_at=100.0, co2_event=52, co2_ppm=812.0
        )
        payload["status_after"] = status_snapshot(
            sequence=8, last_received_at=120.0, co2_event=52, co2_ppm=812.0
        )
        probe = evaluate_sensor_progress(payload, "co2")
        self.assertNotEqual(probe["status"], "PASS")
        self.assertEqual(probe["status"], "FAIL")
        self.assertEqual(probe["observed"]["identities"]["values.measurement_event_count"]["before"], 52)
        self.assertEqual(probe["observed"]["identities"]["values.measurement_event_count"]["after"], 52)
        self.assertEqual(probe["observed"]["identities"]["last_received_at"]["after"], 120.0)

    def test_co2_missing_measurement_identity_is_not_observable(self) -> None:
        before = status_snapshot(sequence=4, last_received_at=100.0, co2_event=7, co2_ppm=812.0)
        after = status_snapshot(sequence=8, last_received_at=120.0, co2_event=7, co2_ppm=900.0)
        for document in (before, after):
            values = document["co2"]["state"]["values"]
            values.pop("measurement_event_id", None)
            values.pop("measurement_event_count", None)
        probe = evaluate_sensor_progress(
            observation(status_before=before, status_after=after),
            "co2",
        )
        self.assertEqual(probe["status"], "NOT_OBSERVABLE")
        evaluated = evaluate_observation(
            observation(status_before=before, status_after=after),
            mode="OFFLINE_FIXTURE",
        )
        self.assertEqual(evaluated["probes"]["co2_progress"]["status"], "NOT_OBSERVABLE")
        self.assertEqual(evaluated["result"], "PASS_WITH_LIMITATIONS")

    def test_esp_stale_counters_do_not_pass_when_tcp_sensors_disconnected(self) -> None:
        payload = observation(
            health_after=health_document(connections=1, disconnects=0),
            status_after=status_snapshot(
                sequence=8,
                last_received_at=8.0,
                co2_event=2,
                tcp_status="DISCONNECTED",
            ),
        )
        payload["health_after"]["receiver"]["protocol_errors"] = 1
        probe = evaluate_esp_connection(payload)
        self.assertNotEqual(probe["status"], "PASS")
        self.assertEqual(probe["status"], "FAIL")
        self.assertEqual(probe["observed"]["state"], "DISCONNECTED")

    def test_esp_current_tcp_connectivity_passes_with_consistent_receiver(self) -> None:
        probe = evaluate_esp_connection(observation())
        self.assertEqual(probe["status"], "PASS")
        self.assertEqual(probe["observed"]["state"], "CONNECTED")
        self.assertEqual(probe["observed"]["sensor_connectivity"]["co2"], "CONNECTED")

    def test_esp_missing_connectivity_is_not_observable(self) -> None:
        probe = evaluate_esp_connection(observation(status_after={"schema": "empty"}))
        self.assertEqual(probe["status"], "NOT_OBSERVABLE")
        self.assertNotEqual(probe["status"], "PASS")

    def test_live_remote_host_is_rejected_to_keep_socket_http_provenance(self) -> None:
        slept = []
        report = live_document(
            host="192.168.1.20",
            http_port=8000,
            window_seconds=20,
            sleep=slept.append,
            platform_name="linux",
        )
        self.assertEqual(slept, [])
        self.assertEqual(report["mode"], "LIVE")
        self.assertEqual(report["result"], "FAIL")
        self.assertTrue(report["live_unsupported_remote_host"])
        self.assertEqual(report["stage_9_live_smoke"], "FAIL")

    def test_h_stalled_sensor_fails(self) -> None:
        evaluated = evaluate_observation(
            observation(before_seq=8, after_seq=8, co2_event_before=2, co2_event_after=2),
            mode="OFFLINE_FIXTURE",
        )
        self.assertEqual(evaluated["probes"]["co2_progress"]["status"], "FAIL")
        self.assertEqual(evaluated["result"], "FAIL")

    def test_i_new_logger_drops_fail(self) -> None:
        evaluated = evaluate_observation(
            observation(
                dropped_before={"co2": 1, "thermal": 0, "mmwave": 0, "pir": 0},
                dropped_after={"co2": 2, "thermal": 0, "mmwave": 0, "pir": 0},
            ),
            mode="OFFLINE_FIXTURE",
        )
        self.assertEqual(evaluated["probes"]["logger_drops"]["status"], "FAIL")
        self.assertEqual(evaluated["result"], "FAIL")

    def test_j_historical_logger_count_without_new_drops_passes(self) -> None:
        dropped = {"co2": 5, "thermal": 0, "mmwave": 0, "pir": 0}
        evaluated = evaluate_observation(
            observation(dropped_before=dropped, dropped_after=dropped),
            mode="OFFLINE_FIXTURE",
        )
        self.assertEqual(evaluated["probes"]["logger_drops"]["status"], "PASS")
        self.assertEqual(evaluated["probes"]["logger_drops"]["observed"]["new_drops"], 0)
        self.assertEqual(evaluated["result"], "PASS")

    def test_k_missing_logger_counter_is_not_observable(self) -> None:
        before = health_document()
        after = health_document()
        del after["receiver"]["sensor_logging"]  # type: ignore[index]
        evaluated = evaluate_observation(
            observation(health_before=before, health_after=after),
            mode="OFFLINE_FIXTURE",
        )
        self.assertEqual(evaluated["probes"]["logger_drops"]["status"], "NOT_OBSERVABLE")
        self.assertEqual(evaluated["result"], "PASS_WITH_LIMITATIONS")

    def test_l_unknown_runtime_ai_enum_fails_safe(self) -> None:
        after = status_snapshot(sequence=9, last_received_at=9.0, co2_event=3)
        after["runtime_status"]["sensors"]["thermal"]["ai_status"] = "WEIRD"
        after["thermal"]["runtime_status"]["ai_status"] = "WEIRD"
        evaluated = evaluate_observation(observation(status_after=after), mode="OFFLINE_FIXTURE")
        self.assertEqual(evaluated["probes"]["runtime_status"]["status"], "FAIL")
        self.assertIn("unknown", evaluated["probes"]["runtime_status"]["reason"])
        self.assertEqual(evaluated["result"], "FAIL")

    def test_m_offline_fixture_cannot_claim_live_success(self) -> None:
        report = fixture_document(FIXTURE_DIR / "pass.json")
        self.assertEqual(report["mode"], "OFFLINE_FIXTURE")
        self.assertNotEqual(report["stage_9_live_smoke"], "PASS")
        self.assertEqual(report["stage_9_live_smoke"], "NOT_RUN")
        self.assertTrue(report["mac_tooling_does_not_imply_live_smoke"])

    def test_ss_parser_is_isolated_from_linux_execution(self) -> None:
        parsed = parse_listen_ports(SS_PASS)
        self.assertIn(9000, parsed["tcp"])
        self.assertIn(5005, parsed["udp"])
        self.assertNotIn(9000, parse_listen_ports("LISTEN 0 2 0.0.0.0:19000 0.0.0.0:*\n")["tcp"])

    def test_default_cli_is_plan_and_does_not_claim_live(self) -> None:
        report = plan_document()
        self.assertEqual(report["mode"], "PLAN")
        self.assertEqual(report["result"], "NOT_RUN")
        self.assertEqual(report["stage_9_live_smoke"], "NOT_RUN")
        self.assertEqual(main([]), 0)

    def test_live_on_mac_is_rejected_without_sleeping(self) -> None:
        slept = []
        report = live_document(
            host="127.0.0.1",
            http_port=8000,
            window_seconds=20,
            sleep=slept.append,
            platform_name="darwin",
        )
        self.assertEqual(slept, [])
        self.assertEqual(report["mode"], "LIVE")
        self.assertEqual(report["result"], "FAIL")
        self.assertTrue(report["live_unsupported_platform"])
        self.assertNotEqual(report["stage_9_live_smoke"], "PASS")

    def test_live_injected_clock_does_not_sleep_real_duration(self) -> None:
        samples = observation()
        calls = {"health": 0, "status": 0}

        def http_get(path: str):
            if path == "/health":
                calls["health"] += 1
                payload = samples["health_before"] if calls["health"] == 1 else samples["health_after"]
                return payload, None
            calls["status"] += 1
            payload = samples["status_before"] if calls["status"] == 1 else samples["status_after"]
            return payload, None

        slept = []
        report = live_document(
            host="127.0.0.1",
            http_port=8000,
            window_seconds=20,
            http_get=http_get,
            collect_sockets=lambda: (SS_PASS, None),
            sleep=slept.append,
            clock=lambda: 0.0,
            platform_name="linux",
        )
        self.assertEqual(slept, [20])
        self.assertEqual(report["mode"], "LIVE")
        self.assertEqual(report["result"], "PASS")
        self.assertEqual(report["stage_9_live_smoke"], "PASS")

    def test_required_not_run_cannot_pass_live(self) -> None:
        empty = {name: {"name": name, "status": "NOT_RUN"} for name in (
            "backend_health",
            "tcp_9000",
            "udp_5005",
            "co2_progress",
            "thermal_progress",
            "mmwave_progress",
            "pir_progress",
            "runtime_status",
            "esp_connection",
            "logger_drops",
        )}
        from hil.stage9_evaluate import overall_result

        self.assertEqual(overall_result(empty, mode="LIVE"), "FAIL")


if __name__ == "__main__":
    unittest.main()
