# -*- coding: utf-8 -*-
"""Frozen inference adapters loaded by the SafeNest Raspberry Pi runtime.

``RaspberryPi/Runtime/ai/runtime.py`` loads each adapter by file path, so this
package only needs to expose the classes for direct imports (tests, tooling).
"""

from __future__ import annotations

from importlib import import_module

__all__ = [
    "ThermalInterpreter",
    "ThermalPrediction",
    "CB6Interpreter",
    "MN9Interpreter",
]

_EXPORT_MODULES = {
    "ThermalInterpreter": ".thermal_interpreter",
    "ThermalPrediction": ".thermal_interpreter",
    "CB6Interpreter": ".co2_c_b6_interpreter",
    "MN9Interpreter": ".mmwave_m_n9_interpreter",
}


def __getattr__(name: str):
    if name not in _EXPORT_MODULES:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module = import_module(_EXPORT_MODULES[name], __name__)
    value = getattr(module, name)
    globals()[name] = value
    return value
