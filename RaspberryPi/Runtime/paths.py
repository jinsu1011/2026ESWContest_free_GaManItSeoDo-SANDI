"""Canonical repository paths for the SafeNest Raspberry Pi runtime.

The runtime lives in ``RaspberryPi/Runtime`` and consumes sibling components
(``RaspberryPi/Ondevice_AI``, ``RaspberryPi/Web``, ``RaspberryPi/LCD``) plus the
``ESP32`` firmware tree. Keeping every cross-component location here means a
future relocation touches one file instead of every module.
"""

from __future__ import annotations

from pathlib import Path


RUNTIME_ROOT = Path(__file__).resolve().parent
RASPBERRY_PI_ROOT = RUNTIME_ROOT.parent
REPOSITORY_ROOT = RASPBERRY_PI_ROOT.parent

ONDEVICE_AI_ROOT = RASPBERRY_PI_ROOT / "Ondevice_AI"
WEB_ROOT = RASPBERRY_PI_ROOT / "Web"
WEB_PORTAL = WEB_ROOT / "portal"
WEB_GUEST = WEB_ROOT / "guest"
LCD_ROOT = RASPBERRY_PI_ROOT / "LCD"
LCD_STATIC = LCD_ROOT / "static"

ESP32_ROOT = REPOSITORY_ROOT / "ESP32"
# Canonical firmware actually flashed to the sensor node. Keep this path in sync
# with the sketch directory name; the Arduino IDE requires .ino == folder name.
ESP32_SKETCH_DIR = ESP32_ROOT / "Arduino" / "esp32_sensor_node"
ESP32_SKETCH = ESP32_SKETCH_DIR / "esp32_sensor_node.ino"
ESP32_SECRET_TEMPLATE = ESP32_SKETCH_DIR / "secrets.example.h"

DATA_ROOT = RUNTIME_ROOT / "data"
DATABASE_PATH = DATA_ROOT / "safenest.db"

MODEL_MANIFEST = ONDEVICE_AI_ROOT / "models" / "model_manifest.json"
RISK_CONFIG = ONDEVICE_AI_ROOT / "risk" / "risk_config.json"
