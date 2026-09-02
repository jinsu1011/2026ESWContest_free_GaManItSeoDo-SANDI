"""Parse Linux listener tables for Stage 9. Execution is separate from parsing."""

from __future__ import annotations

import re
from typing import Mapping


_LOCAL_PORT = re.compile(r"(?:^|\s)(\S+):(\d+)(?:\s|$)")
_TCP_HINTS = ("LISTEN",)
_UDP_HINTS = ("UNCONN", "UNCONNED", "UDP")


def parse_listen_ports(text: str) -> dict[str, set[int]]:
    """Return TCP/UDP listen ports from `ss -H -l -t -u -n` (or equivalent) text."""

    tcp: set[int] = set()
    udp: set[int] = set()
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.lower().startswith("state"):
            continue
        match = _LOCAL_PORT.search(line)
        if match is None:
            continue
        port = int(match.group(2))
        upper = line.upper()
        if any(hint in upper for hint in _TCP_HINTS):
            tcp.add(port)
        if any(hint in upper for hint in _UDP_HINTS) or upper.startswith("UDP"):
            udp.add(port)
    return {"tcp": tcp, "udp": udp}


def listener_present(parsed: Mapping[str, set[int]], protocol: str, port: int) -> bool:
    return port in set(parsed.get(protocol, set()))
