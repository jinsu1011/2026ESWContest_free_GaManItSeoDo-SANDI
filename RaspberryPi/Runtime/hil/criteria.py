"""Pure PHASE 10 HIL criteria; no network or hardware access is performed here."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Any


SCENARIOS = (
    "test01_no_person",
    "test02_person_normal",
    "test03_stationary_person",
    "test04_abnormal_breathing",
    "test05_co2_rise",
    "test06_mmwave_false_positive",
    "test07_thermal_nonhuman",
    "test08_thermal_disconnect",
    "test09_esp32_reboot",
    "test10_ai_failure",
)


def evaluate(scenario: str, samples: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    if scenario not in SCENARIOS:
        raise ValueError(f"unknown HIL scenario: {scenario}")
    valid = [sample for sample in samples if isinstance(sample.get("status"), Mapping)]
    if not valid:
        return _result(scenario, "INCONCLUSIVE", "수집된 /api/status 응답이 없습니다", [])
    return _EVALUATORS[scenario](scenario, valid)


def _test01(scenario: str, samples: list[Mapping[str, Any]]) -> dict[str, Any]:
    latest = samples[-1]
    checks = [
        _check("risk_normal", _get(latest, "status", "risk", "risk_level") == "NORMAL",
               _get(latest, "status", "risk", "risk_level")),
        _check("presence_false", _get(latest, "status", "risk", "presence_detected") is False,
               _get(latest, "status", "risk", "presence_detected")),
        _check("thermal_no_human", _thermal_state(latest) in {"NO_HUMAN", "NOT_HUMAN"},
               _thermal_state(latest)),
    ]
    return _from_checks(scenario, "빈 공간에서 사람 없음과 NORMAL 확인", checks)


def _test02(scenario: str, samples: list[Mapping[str, Any]]) -> dict[str, Any]:
    latest = samples[-1]
    respiration = _get(latest, "status", "mmwave", "state", "values", "respiration_rate_bpm")
    checks = [
        _check("presence_true", _get(latest, "status", "risk", "presence_detected") is True,
               _get(latest, "status", "risk", "presence_detected")),
        _check("normal_respiration", _number(respiration) and 10 <= float(respiration) <= 24,
               respiration),
        _check("thermal_human", _thermal_state(latest) == "HUMAN_NORMAL", _thermal_state(latest)),
        _check("risk_normal", _get(latest, "status", "risk", "risk_level") == "NORMAL",
               _get(latest, "status", "risk", "risk_level")),
    ]
    return _from_checks(scenario, "사람 존재와 정상 호흡 확인", checks)


def _test03(scenario: str, samples: list[Mapping[str, Any]]) -> dict[str, Any]:
    matching = [sample for sample in samples if _get(sample, "status", "risk", "presence_detected") is True
                and _get(sample, "status", "pir", "state", "values", "motion") is False]
    if not matching:
        return _result(scenario, "INCONCLUSIVE", "사람 있음 + PIR 무움직임 표본이 없습니다", [])
    sample = matching[0]
    respiration = _get(sample, "status", "mmwave", "state", "values", "respiration_rate_bpm")
    checks = [
        _check("respiration_present", _number(respiration) and float(respiration) > 0, respiration),
        _check("not_immediate_emergency", _get(sample, "status", "risk", "is_emergency") is False,
               _get(sample, "status", "risk", "is_emergency")),
        _check("not_immediate_danger", _get(sample, "status", "risk", "risk_level") != "DANGER",
               _get(sample, "status", "risk", "risk_level")),
    ]
    return _from_checks(scenario, "정지 인체를 즉시 긴급 오탐하지 않음", checks)


def _test04(scenario: str, samples: list[Mapping[str, Any]]) -> dict[str, Any]:
    abnormal = []
    for sample in samples:
        value = _get(sample, "status", "mmwave", "state", "values", "respiration_rate_bpm")
        if _number(value) and (float(value) < 12 or float(value) > 20):
            abnormal.append(sample)
    if not abnormal:
        return _result(scenario, "INCONCLUSIVE", "비정상 호흡 표본이 없습니다", [])
    sample = abnormal[-1]
    level = _get(sample, "status", "risk", "risk_level")
    checks = [
        _check("abnormal_reason", "ABNORMAL_RESPIRATION_RPM" in _reasons(sample), _reasons(sample)),
        _check("warning_or_danger", level in {"WARNING", "DANGER"}, level),
    ]
    return _from_checks(scenario, "비정상 호흡이 경보 단계로 반영됨", checks)


def _test05(scenario: str, samples: list[Mapping[str, Any]]) -> dict[str, Any]:
    readings = [(sample, _get(sample, "status", "co2", "state", "values", "ppm")) for sample in samples]
    readings = [(sample, value) for sample, value in readings if _number(value)]
    if len(readings) < 2:
        return _result(scenario, "INCONCLUSIVE", "CO₂ 수치 표본이 2개 미만입니다", [])
    first, last = readings[0], readings[-1]
    first_component = _get(first[0], "status", "co2", "risk_component", "score")
    last_component = _get(last[0], "status", "co2", "risk_component", "score")
    checks = [
        _check("ppm_increased", float(last[1]) > float(first[1]), [first[1], last[1]]),
        _check("component_not_decreased", _number(first_component) and _number(last_component)
               and float(last_component) >= float(first_component), [first_component, last_component]),
        _check("co2_reason", bool({"CO2_IMMEDIATE_DANGER", "CO2_RELATIVE_RISE",
                                   "FAST_CO2_RISE", "VERY_FAST_CO2_RISE",
                                   "FLOOR_CO2_IMMEDIATE_DANGER", "FLOOR_CO2_RELATIVE_WARNING",
                                   "FLOOR_CO2_FAST_RISE"}
                                  & set(_reasons(last[0]))), _reasons(last[0])),
    ]
    return _from_checks(scenario, "CO₂ 상승이 환경 위험도에 반영됨", checks)


def _test06(scenario: str, samples: list[Mapping[str, Any]]) -> dict[str, Any]:
    matching = []
    for sample in samples:
        values = _get(sample, "status", "mmwave", "state", "values")
        if isinstance(values, Mapping) and values.get("presence_available") is True \
                and values.get("presence") is True and _thermal_state(sample) in {"NO_HUMAN", "NOT_HUMAN"}:
            matching.append(sample)
    if not matching:
        return _result(
            scenario,
            "INCONCLUSIVE",
            "TCP v1에서 mmWave presence=true와 Thermal no-human 조합을 관측하지 못했습니다",
            [],
        )
    sample = matching[-1]
    checks = [_check("mismatch_reason", "MMWAVE_THERMAL_MISMATCH" in _reasons(sample), _reasons(sample))]
    return _from_checks(scenario, "mmWave false positive 교차검증", checks)


def _test07(scenario: str, samples: list[Mapping[str, Any]]) -> dict[str, Any]:
    classified = [sample for sample in samples if _thermal_state(sample) in {"NO_HUMAN", "NOT_HUMAN"}]
    if not classified:
        return _result(scenario, "INCONCLUSIVE", "Thermal no-human AI 표본이 없습니다", [])
    sample = classified[-1]
    probabilities = _get(sample, "status", "thermal", "ai", "metadata", "probabilities")
    human_probability = None
    if isinstance(probabilities, list) and len(probabilities) == 3 and all(_number(v) for v in probabilities):
        human_probability = float(probabilities[1]) + float(probabilities[2])
    checks = [
        _check("classified_no_human", True, _thermal_state(sample)),
        _check("human_probability_below_half", human_probability is not None and human_probability < 0.5,
               human_probability),
    ]
    return _from_checks(scenario, "비생체 열원을 사람으로 분류하지 않음", checks)


def _test08(scenario: str, samples: list[Mapping[str, Any]]) -> dict[str, Any]:
    statuses = [_get(sample, "status", "thermal", "state", "status") for sample in samples]
    recovered = _transition_recovered(statuses)
    max_connections = _maximum(samples, "health", "receiver", "connections")
    checks = [
        _check("disconnect_observed", any(value in {"DISCONNECTED", "STALE"} for value in statuses), statuses),
        _check("live_recovered", recovered, statuses),
        _check("connection_reaccepted", max_connections is not None and max_connections >= 2, max_connections),
    ]
    return _from_checks(scenario, "Thermal 수신 단절 후 복구 (프레임 UDP 5005 · ESP 링크 TCP 9000)", checks)


def _test09(scenario: str, samples: list[Mapping[str, Any]]) -> dict[str, Any]:
    sequences = [_get(sample, "status", "thermal", "state", "sequence") for sample in samples]
    sequences = [int(value) for value in sequences if isinstance(value, int) and not isinstance(value, bool)]
    reset = any(after < before for before, after in zip(sequences, sequences[1:]))
    max_connections = _maximum(samples, "health", "receiver", "connections")
    checks = [
        _check("sequence_reset_observed", reset, sequences),
        _check("connection_reaccepted", max_connections is not None and max_connections >= 2, max_connections),
        _check("final_system_online", _get(samples[-1], "status", "system") == "ONLINE",
               _get(samples[-1], "status", "system")),
    ]
    return _from_checks(scenario, "ESP32 재부팅 후 sequence reset과 재연결", checks)


def _test10(scenario: str, samples: list[Mapping[str, Any]]) -> dict[str, Any]:
    failures = [sample for sample in samples if _get(sample, "status", "thermal", "ai", "available") is False]
    if not failures:
        return _result(scenario, "INCONCLUSIVE", "Thermal AI unavailable 표본이 없습니다", [])
    sample = failures[-1]
    checks = [
        _check("risk_continues", _get(sample, "status", "risk", "risk_level") is not None,
               _get(sample, "status", "risk", "risk_level")),
        _check("service_alive", _get(sample, "health", "ok") is True, _get(sample, "health", "ok")),
        _check("database_alive", _get(sample, "health", "database", "available") is True,
               _get(sample, "health", "database", "available")),
    ]
    return _from_checks(scenario, "AI 장애 중 rule/DB/API 생존", checks)


def _from_checks(scenario: str, summary: str, checks: list[dict[str, Any]]) -> dict[str, Any]:
    return _result(scenario, "PASS" if all(check["passed"] for check in checks) else "FAIL", summary, checks)


def _result(scenario: str, outcome: str, summary: str, checks: list[dict[str, Any]]) -> dict[str, Any]:
    return {"scenario": scenario, "outcome": outcome, "summary": summary, "checks": checks}


def _check(name: str, passed: object, observed: object) -> dict[str, Any]:
    return {"name": name, "passed": bool(passed), "observed": observed}


def _get(value: object, *path: str) -> Any:
    current = value
    for key in path:
        if not isinstance(current, Mapping):
            return None
        current = current.get(key)
    return current


def _reasons(sample: Mapping[str, Any]) -> list[str]:
    reasons = _get(sample, "status", "risk", "reasons")
    return [str(reason) for reason in reasons] if isinstance(reasons, (list, tuple)) else []


def _thermal_state(sample: Mapping[str, Any]) -> Any:
    return _get(sample, "status", "thermal", "ai", "state")


def _number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _maximum(samples: Sequence[Mapping[str, Any]], *path: str) -> float | None:
    values = [_get(sample, *path) for sample in samples]
    numeric = [float(value) for value in values if _number(value)]
    return max(numeric) if numeric else None


def _transition_recovered(statuses: Sequence[object]) -> bool:
    disconnected_at = next(
        (index for index, value in enumerate(statuses) if value in {"DISCONNECTED", "STALE"}),
        None,
    )
    return disconnected_at is not None and "LIVE" in statuses[disconnected_at + 1 :]


_EVALUATORS: dict[str, Callable[[str, list[Mapping[str, Any]]], dict[str, Any]]] = {
    "test01_no_person": _test01,
    "test02_person_normal": _test02,
    "test03_stationary_person": _test03,
    "test04_abnormal_breathing": _test04,
    "test05_co2_rise": _test05,
    "test06_mmwave_false_positive": _test06,
    "test07_thermal_nonhuman": _test07,
    "test08_thermal_disconnect": _test08,
    "test09_esp32_reboot": _test09,
    "test10_ai_failure": _test10,
}
