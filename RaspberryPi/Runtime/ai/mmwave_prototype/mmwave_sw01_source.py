"""Frozen mmWave prototype module imported from sheepmeat/test M-PROT-5A.

SOURCE_REPO: https://github.com/sheepmeat/test.git
SOURCE_SHA: 809b78626b442f146eccd73595f239b93de3ae2e
SOURCE_PHASE: M-PROT-5A COMPLETE
Imports rewritten for RaspberryPi/Runtime. Numerical identity preserved.

SW-01 live/source transport abstraction (acquisition only).

Physical / runtime source
        ↓
MMWaveSW01Source (this module)
        ↓
StreamBundle / Sample
        ↓
evaluate_stream()  (adapters.mmwave_sw01_interface_checker)

Does not implement unproven MR60 UART frame parsing.
Does not import ROLE_L models or production sensor adapters that load interpreters.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator, Protocol, TextIO, runtime_checkable

from ai.mmwave_prototype.mmwave_sw01_interface_checker import Sample, StreamBundle

SOURCE_RECORD_SCHEMA = "MMWAVE_V2_D1_SW01_SOURCE_RECORD_V1"

BACKEND_JSONL = "JSONL_STREAM_SOURCE"
BACKEND_STDIN = "STDIN_STREAM_SOURCE"
BACKEND_UART = "MR60_UART_SOURCE"

STATUS_IMPLEMENTED = "IMPLEMENTED"
STATUS_PLUGGABLE = "PLUGGABLE_NOT_IMPLEMENTED"
UART_PROTOCOL = "MR60_UART_PROTOCOL_UNPROVEN"

SOURCE_RECORD_INVALID = "SOURCE_RECORD_INVALID"
SOURCE_STREAM_TRUNCATED = "SOURCE_STREAM_TRUNCATED"
SOURCE_REQUIRED_FIELD_MISSING = "SOURCE_REQUIRED_FIELD_MISSING"
SOURCE_SCHEMA_MISMATCH = "SOURCE_SCHEMA_MISMATCH"

TRANSPORT_PRESENT = "SERIAL_TRANSPORT_PRESENT"
TRANSPORT_ABSENT = "SERIAL_TRANSPORT_ABSENT"
TRANSPORT_NA = "TRANSPORT_NOT_APPLICABLE"
PARSER_UNAVAILABLE = "PARSER_BACKEND_UNAVAILABLE"
PARSER_APPLIED = "PARSER_APPLIED"


class SourceDecodeError(Exception):
    def __init__(self, code: str, detail: str = ""):
        self.code = code
        self.detail = detail
        super().__init__(f"{code}:{detail}" if detail else code)


@runtime_checkable
class MMWaveSW01Source(Protocol):
    def open(self) -> None: ...

    def read_bundle(self) -> StreamBundle: ...

    def close(self) -> None: ...

    def source_identity(self) -> dict[str, Any]: ...


def backend_registry() -> dict[str, dict[str, str]]:
    return {
        BACKEND_JSONL: {
            "status": STATUS_IMPLEMENTED,
            "description": "Versioned JSONL external stream (file)",
        },
        BACKEND_STDIN: {
            "status": STATUS_IMPLEMENTED,
            "description": "Versioned JSONL external stream (stdin)",
        },
        BACKEND_UART: {
            "status": STATUS_PLUGGABLE,
            "protocol": UART_PROTOCOL,
            "description": (
                "MR60 UART binary parser not implemented: repository documents "
                "0x0A13 breath_phase semantics but does not contain a proven "
                "frame/CRC/command parser safe to reimplement without guessing."
            ),
        },
    }


def _decode_header(obj: dict[str, Any]) -> dict[str, Any]:
    schema = obj.get("schema_version")
    if schema is not None and schema != SOURCE_RECORD_SCHEMA:
        raise SourceDecodeError(SOURCE_SCHEMA_MISMATCH, str(schema))
    return {
        "device_identity": obj.get("device_identity"),
        "interface_identity": obj.get("interface_identity"),
        "configuration_identity": obj.get("configuration_identity"),
        "observation_kind": obj.get("observation_kind"),
        "boot_identity": obj.get("boot_identity"),
    }


def _decode_sample(obj: dict[str, Any], line_no: int) -> Sample:
    if not isinstance(obj, dict):
        raise SourceDecodeError(SOURCE_RECORD_INVALID, f"line_{line_no}_not_object")
    rtype = obj.get("record_type", "sample")
    if rtype not in ("sample", "observation"):
        raise SourceDecodeError(SOURCE_RECORD_INVALID, f"line_{line_no}_bad_record_type:{rtype}")
    health = obj.get("health") if isinstance(obj.get("health"), dict) else {}
    return Sample(
        t=obj.get("t", obj.get("timestamp")),
        phase=obj.get("phase", obj.get("resp_phase")),
        seq=obj.get("seq", obj.get("sequence")),
        health_ok=health.get("ok", obj.get("health_ok")),
        fault_code=health.get("fault_code", obj.get("fault_code")),
        session_id=obj.get("session_id"),
        reset_flag=bool(obj.get("reset") or obj.get("reset_flag")),
        scalar_rr=obj.get("rr_bpm", obj.get("scalar_rr")),
    )


def parse_jsonl_lines(lines: Iterator[str], *, source_label: str) -> StreamBundle:
    samples: list[Sample] = []
    header: dict[str, Any] = {}
    source_faults: list[str] = []
    saw_header = False
    truncated = False
    non_empty_seen = False

    for line_no, raw_line in enumerate(lines, start=1):
        line = raw_line.strip()
        if not line:
            continue
        non_empty_seen = True
        try:
            obj = json.loads(line)
        except json.JSONDecodeError as exc:
            if line_no > 1 and not (line.endswith("}") or line.endswith("]")):
                truncated = True
                source_faults.append(f"{SOURCE_STREAM_TRUNCATED}:line_{line_no}")
                break
            raise SourceDecodeError(SOURCE_RECORD_INVALID, f"line_{line_no}:{exc.msg}") from exc

        if not isinstance(obj, dict):
            raise SourceDecodeError(SOURCE_RECORD_INVALID, f"line_{line_no}_not_object")

        rtype = obj.get("record_type")
        if not saw_header and rtype in ("stream_header", "header"):
            header = _decode_header(obj)
            if obj.get("schema_version") != SOURCE_RECORD_SCHEMA:
                raise SourceDecodeError(SOURCE_SCHEMA_MISMATCH, str(obj.get("schema_version")))
            for key in (
                "device_identity",
                "interface_identity",
                "configuration_identity",
                "observation_kind",
            ):
                if not header.get(key):
                    source_faults.append(f"{SOURCE_REQUIRED_FIELD_MISSING}:{key}")
            saw_header = True
            continue

        if not saw_header:
            raise SourceDecodeError(SOURCE_RECORD_INVALID, f"line_{line_no}_missing_stream_header")

        samples.append(_decode_sample(obj, line_no))

    if truncated:
        return StreamBundle(
            device_identity=header.get("device_identity"),
            interface_identity=header.get("interface_identity"),
            configuration_identity=header.get("configuration_identity"),
            observation_kind=header.get("observation_kind"),
            samples=samples,
            backend_error=SOURCE_STREAM_TRUNCATED,
            source_faults=source_faults + [SOURCE_STREAM_TRUNCATED],
        )

    if not non_empty_seen:
        raise SourceDecodeError(SOURCE_RECORD_INVALID, "empty_stream")
    if not saw_header:
        raise SourceDecodeError(SOURCE_RECORD_INVALID, "missing_stream_header")

    if any(SOURCE_REQUIRED_FIELD_MISSING in f for f in source_faults):
        return StreamBundle(
            device_identity=header.get("device_identity"),
            interface_identity=header.get("interface_identity"),
            configuration_identity=header.get("configuration_identity"),
            observation_kind=header.get("observation_kind"),
            samples=samples,
            backend_error=SOURCE_REQUIRED_FIELD_MISSING,
            source_faults=source_faults,
        )

    return StreamBundle(
        device_identity=header.get("device_identity"),
        interface_identity=header.get("interface_identity"),
        configuration_identity=header.get("configuration_identity"),
        observation_kind=header.get("observation_kind"),
        samples=samples,
        source_faults=source_faults,
    )


@dataclass
class JsonlFileSource:
    path: Path
    _bundle: StreamBundle | None = field(default=None, init=False, repr=False)

    def open(self) -> None:
        text = self.path.read_text()
        lines = text.splitlines()
        if text and not text.endswith("\n") and lines:
            try:
                json.loads(lines[-1])
            except json.JSONDecodeError:
                self._bundle = StreamBundle(
                    backend_error=SOURCE_STREAM_TRUNCATED,
                    source_faults=[SOURCE_STREAM_TRUNCATED],
                )
                return
        try:
            self._bundle = parse_jsonl_lines(iter(lines), source_label=str(self.path))
        except SourceDecodeError as exc:
            self._bundle = StreamBundle(
                backend_error=exc.code,
                source_faults=[f"{exc.code}:{exc.detail}" if exc.detail else exc.code],
            )

    def read_bundle(self) -> StreamBundle:
        if self._bundle is None:
            raise RuntimeError("source_not_open")
        return self._bundle

    def close(self) -> None:
        self._bundle = None

    def source_identity(self) -> dict[str, Any]:
        return {
            "backend": BACKEND_JSONL,
            "status": STATUS_IMPLEMENTED,
            "path": self.path.as_posix(),
            "schema_version": SOURCE_RECORD_SCHEMA,
        }


@dataclass
class JsonlStdinSource:
    stream: TextIO
    _bundle: StreamBundle | None = field(default=None, init=False, repr=False)

    def open(self) -> None:
        try:
            self._bundle = parse_jsonl_lines(self.stream, source_label="stdin")
        except SourceDecodeError as exc:
            self._bundle = StreamBundle(
                backend_error=exc.code,
                source_faults=[f"{exc.code}:{exc.detail}" if exc.detail else exc.code],
            )

    def read_bundle(self) -> StreamBundle:
        if self._bundle is None:
            raise RuntimeError("source_not_open")
        return self._bundle

    def close(self) -> None:
        self._bundle = None

    def source_identity(self) -> dict[str, Any]:
        return {
            "backend": BACKEND_STDIN,
            "status": STATUS_IMPLEMENTED,
            "schema_version": SOURCE_RECORD_SCHEMA,
        }


@dataclass
class Mr60UartSourceStub:
    port: str

    def open(self) -> None:
        raise SourceDecodeError(
            UART_PROTOCOL,
            "binary UART parser not implemented; do not guess frame/CRC/commands",
        )

    def read_bundle(self) -> StreamBundle:
        return StreamBundle(
            backend_error=UART_PROTOCOL,
            source_faults=[UART_PROTOCOL, PARSER_UNAVAILABLE],
        )

    def close(self) -> None:
        return None

    def source_identity(self) -> dict[str, Any]:
        return {
            "backend": BACKEND_UART,
            "status": STATUS_PLUGGABLE,
            "protocol": UART_PROTOCOL,
            "port": self.port,
        }


def open_source(
    kind: str,
    *,
    path: Path | None = None,
    port: str | None = None,
    stdin: TextIO | None = None,
) -> MMWaveSW01Source:
    if kind == BACKEND_JSONL:
        if path is None:
            raise ValueError("jsonl_path_required")
        return JsonlFileSource(path=path)
    if kind == BACKEND_STDIN:
        if stdin is None:
            raise ValueError("stdin_required")
        return JsonlStdinSource(stream=stdin)
    if kind == BACKEND_UART:
        if not port:
            raise ValueError("port_required")
        return Mr60UartSourceStub(port=port)
    raise ValueError(f"unknown_source_kind:{kind}")
