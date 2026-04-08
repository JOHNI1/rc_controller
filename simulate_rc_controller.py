#!/usr/bin/env python3
"""
simulate_rc_controller.py

Helper module for:
- loading rc_controller_channel_function_mapping.yaml
- loading rc_controller_axis_calibration.json
- reading Linux joystick events from /dev/input/js*
- converting live joystick input into RC channel values

This file is intentionally a helper module and has no main().
"""

from __future__ import annotations

import glob
import json
import os
import select
import struct
import sys
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

BASE_DIR = Path(__file__).resolve().parent
MAPPING_FILE = BASE_DIR / "rc_controller_channel_function_mapping.yaml"
CALIBRATION_FILE = BASE_DIR / "rc_controller_axis_calibration.json"
CHANNEL_COUNT_LIMIT = 16

JS_EVENT_BUTTON = 0x01
JS_EVENT_AXIS = 0x02
JS_EVENT_INIT = 0x80


def clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def clamp_int(value: float, lo: int, hi: int) -> int:
    return max(lo, min(hi, int(round(value))))


def list_joystick_devices() -> List[str]:
    return sorted(glob.glob("/dev/input/js*"))


def choose_default_joystick() -> str:
    devices = list_joystick_devices()
    if not devices:
        raise RuntimeError("No joystick devices found at /dev/input/js*")
    return devices[0]


def parse_scalar(text: str):
    text = text.strip()
    if text == "":
        return ""
    lowered = text.lower()
    if lowered == "null":
        return None
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    if (text.startswith('"') and text.endswith('"')) or (text.startswith("'") and text.endswith("'")):
        return text[1:-1]
    try:
        return int(text)
    except ValueError:
        try:
            return float(text)
        except ValueError:
            return text


def tiny_yaml_parser(path: Path) -> dict:
    lines = path.read_text(encoding="utf-8").splitlines()
    data: Dict[str, dict] = {}
    current_channel: Optional[str] = None
    current_submap: Optional[str] = None

    for raw in lines:
        if not raw.strip():
            continue
        if raw.lstrip().startswith("#"):
            continue

        indent = len(raw) - len(raw.lstrip(" "))
        stripped = raw.strip()

        if indent == 0 and stripped.endswith(":"):
            key = stripped[:-1].strip()
            data[key] = {}
            current_channel = key
            current_submap = None
            continue

        if current_channel is None:
            raise ValueError(f"Invalid YAML structure near line: {raw}")

        if indent == 2:
            if ":" not in stripped:
                raise ValueError(f"Expected key:value near line: {raw}")
            key, value = stripped.split(":", 1)
            key = key.strip()
            value = value.strip()

            if value == "":
                data[current_channel][key] = {}
                current_submap = key
            else:
                data[current_channel][key] = parse_scalar(value)
                current_submap = None
            continue

        if indent == 4:
            if current_submap is None:
                raise ValueError(f"Unexpected nested mapping near line: {raw}")
            if ":" not in stripped:
                raise ValueError(f"Expected nested key:value near line: {raw}")
            key, value = stripped.split(":", 1)
            key = parse_scalar(key.strip())
            value = parse_scalar(value.strip())
            data[current_channel][current_submap][key] = value
            continue

        raise ValueError(f"Unsupported indentation/structure near line: {raw}")

    return data


def load_yaml_mapping(path: Path) -> dict:
    try:
        import yaml  # type: ignore
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
        if not isinstance(data, dict):
            raise ValueError("Top-level YAML structure must be a mapping.")
        return data
    except ImportError:
        return tiny_yaml_parser(path)


def load_calibration(path: Path) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict) or "axes" not in data or not isinstance(data["axes"], list):
        raise ValueError(f"Invalid calibration JSON in {path}")
    return data


def index_calibration_axes(calibration: dict) -> Dict[int, dict]:
    out: Dict[int, dict] = {}
    for entry in calibration["axes"]:
        if not isinstance(entry, dict):
            continue
        axis_number = entry.get("axis_number")
        if isinstance(axis_number, int):
            if axis_number in out:
                raise ValueError(f"Duplicate calibration axis_number: {axis_number}")
            out[axis_number] = entry
    return out


def sorted_channel_items(mapping: dict) -> List[Tuple[str, dict]]:
    def channel_key(name: str) -> Tuple[int, str]:
        if name.startswith("channel_"):
            suffix = name.split("_", 1)[1]
            try:
                return int(suffix), name
            except ValueError:
                pass
        return (10**9, name)

    items = [(k, v) for k, v in mapping.items() if isinstance(v, dict)]
    items.sort(key=lambda pair: channel_key(pair[0]))
    return items


def normalize_channel_config(mapping: dict, calibration_axes: Dict[int, dict]) -> List[dict]:
    channels: List[dict] = []
    items = sorted_channel_items(mapping)

    if not items:
        raise ValueError("No channels found in rc_controller_channel_function_mapping.yaml")

    if len(items) > CHANNEL_COUNT_LIMIT:
        raise ValueError(f"Only up to {CHANNEL_COUNT_LIMIT} channels are supported in this helper")

    for index, (name, cfg) in enumerate(items, start=1):
        default_value = int(cfg.get("default_value", 1500))
        input_type = str(cfg.get("input_type", "")).strip().lower()

        channel = {
            "name": name,
            "index_1based": index,
            "default_value": default_value,
            "input_type": input_type,
        }

        if input_type == "buttons":
            button_map = cfg.get("buttons", {})
            if not isinstance(button_map, dict):
                raise ValueError(f"{name}: buttons must be a mapping")
            normalized_buttons: Dict[int, int] = {}
            for k, v in button_map.items():
                normalized_buttons[int(k)] = int(v)
            channel["buttons"] = normalized_buttons

        elif input_type == "axis":
            if "axis_number" not in cfg:
                raise ValueError(f"{name}: axis channel is missing axis_number")
            axis_number = int(cfg["axis_number"])

            if axis_number not in calibration_axes:
                raise ValueError(
                    f"{name}: axis_number {axis_number} not found in rc_controller_axis_calibration.json"
                )

            calib = calibration_axes[axis_number]

            if "raw_min" not in calib or "raw_max" not in calib:
                raise ValueError(f"{name}: calibration entry for axis {axis_number} missing raw_min/raw_max")

            rc_min = int(cfg.get("rc_min", 1000))
            rc_max = int(cfg.get("rc_max", 2000))
            if rc_min == rc_max:
                raise ValueError(f"{name}: rc_min and rc_max must differ")

            channel["axis_number"] = axis_number
            channel["rc_min"] = rc_min
            channel["rc_max"] = rc_max
            channel["calibration"] = calib

        else:
            raise ValueError(f"{name}: input_type must be 'axis' or 'buttons'")

        channels.append(channel)

    return channels


def load_channel_profile(
    mapping_path: Path | str = MAPPING_FILE,
    calibration_path: Path | str = CALIBRATION_FILE,
) -> List[dict]:
    mapping_path = Path(mapping_path)
    calibration_path = Path(calibration_path)

    if not mapping_path.exists():
        raise FileNotFoundError(f"Missing mapping file: {mapping_path.resolve()}")
    if not calibration_path.exists():
        raise FileNotFoundError(f"Missing calibration file: {calibration_path.resolve()}")

    mapping = load_yaml_mapping(mapping_path)
    calibration = load_calibration(calibration_path)
    calibration_axes = index_calibration_axes(calibration)
    return normalize_channel_config(mapping, calibration_axes)


class JsReader(threading.Thread):
    def __init__(self, path: str) -> None:
        super().__init__(daemon=True)
        self.path = path
        self.running = True
        self._fd: Optional[int] = None
        self._lock = threading.Lock()
        self._axes: Dict[int, int] = {}
        self._buttons: Dict[int, int] = {}

    def snapshot(self) -> Tuple[Dict[int, int], Dict[int, int]]:
        with self._lock:
            return dict(self._axes), dict(self._buttons)

    def stop(self) -> None:
        self.running = False
        fd = self._fd
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
            finally:
                self._fd = None

    def run(self) -> None:
        try:
            fd = os.open(self.path, os.O_RDONLY | os.O_NONBLOCK)
            self._fd = fd

            while self.running:
                try:
                    ready, _, _ = select.select([fd], [], [], 0.1)
                except (OSError, ValueError):
                    break

                if not ready:
                    continue

                try:
                    raw = os.read(fd, 8)
                except BlockingIOError:
                    continue
                except OSError:
                    break

                if len(raw) != 8:
                    continue

                _, value, etype, number = struct.unpack("<IhBB", raw)

                if etype & JS_EVENT_INIT:
                    etype &= ~JS_EVENT_INIT

                with self._lock:
                    if etype == JS_EVENT_AXIS:
                        self._axes[number] = value
                    elif etype == JS_EVENT_BUTTON:
                        self._buttons[number] = 1 if value else 0

        except Exception as exc:
            print(f"Joystick reader stopped: {exc}", file=sys.stderr)
        finally:
            self.running = False
            fd = self._fd
            self._fd = None
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass


def apply_axis_mapping(joystick_value: int, calib: dict, rc_min: int, rc_max: int) -> int:
    """
    Compact joystick math:

    Centered axis:
        n = (j-j0)/(jmax-j0)   if j >= j0
        n = (j-j0)/(j0-jmin)   if j <  j0
        n <- clamp(n, -1, 1)
        if inverted: n <- -n

        r_middle = (r_min + r_max)/2
        r = r_middle + n*(r_max-r_middle)   if n >= 0
        r = r_middle + n*(r_middle-r_min)   if n <  0

    Non-centered axis:
        n = (j-jmin)/(jmax-jmin)
        n <- clamp(n, 0, 1)
        if inverted: n <- 1-n

        r = r_min + n*(r_max-r_min)
    """
    j = float(joystick_value)
    j_min = float(calib["raw_min"])
    j_max = float(calib["raw_max"])
    inverted = bool(calib.get("inverted", False))

    if "raw_center" in calib:
        j0 = float(calib["raw_center"])
        if j >= j0:
            denom = j_max - j0
            n = 0.0 if abs(denom) < 1e-9 else (j - j0) / denom
        else:
            denom = j0 - j_min
            n = 0.0 if abs(denom) < 1e-9 else (j - j0) / denom

        n = clamp(n, -1.0, 1.0)
        if inverted:
            n = -n

        r_middle = (rc_min + rc_max) / 2.0
        if n >= 0.0:
            r = r_middle + n * (rc_max - r_middle)
        else:
            r = r_middle + n * (r_middle - rc_min)

        return clamp_int(r, min(rc_min, rc_max), max(rc_min, rc_max))

    denom = j_max - j_min
    n = 0.0 if abs(denom) < 1e-9 else (j - j_min) / denom
    n = clamp(n, 0.0, 1.0)
    if inverted:
        n = 1.0 - n

    r = rc_min + n * (rc_max - rc_min)
    return clamp_int(r, min(rc_min, rc_max), max(rc_min, rc_max))


def resolve_channel_value(channel: dict, axes: Dict[int, int], buttons: Dict[int, int]) -> int:
    default_value = int(channel["default_value"])
    input_type = channel["input_type"]

    if input_type == "buttons":
        button_map: Dict[int, int] = channel["buttons"]
        for button_id, pressed_value in button_map.items():
            if buttons.get(button_id, 0):
                return int(pressed_value)
        return default_value

    if input_type == "axis":
        axis_number = int(channel["axis_number"])
        calib = channel["calibration"]
        neutral = int(calib.get("raw_center", calib.get("raw_min", 0)))
        joystick_value = int(axes.get(axis_number, neutral))
        rc_min = int(channel["rc_min"])
        rc_max = int(channel["rc_max"])
        return apply_axis_mapping(joystick_value, calib, rc_min, rc_max)

    return default_value


class RcJoystickController:
    def __init__(
        self,
        js_path: Optional[str] = None,
        mapping_path: Path | str = MAPPING_FILE,
        calibration_path: Path | str = CALIBRATION_FILE,
    ) -> None:
        self.js_path = js_path or choose_default_joystick()
        self.channels = load_channel_profile(mapping_path, calibration_path)
        self.reader = JsReader(self.js_path)
        self._started = False

    def start(self) -> None:
        if self._started:
            return

        self.reader.start()
        time.sleep(0.25)

        if not self.reader.is_alive():
            raise RuntimeError(f"Joystick reader failed to start for {self.js_path}")

        self._started = True

    def stop(self) -> None:
        self.reader.stop()
        if self.reader.is_alive():
            self.reader.join(timeout=1.0)
        self._started = False

    def get_channel_values(self) -> List[int]:
        axes, buttons = self.reader.snapshot()
        return [resolve_channel_value(channel, axes, buttons) for channel in self.channels]

    def print_channel_summary(self) -> None:
        print("Loaded channel mapping:")
        for ch in self.channels:
            name = ch["name"]
            if ch["input_type"] == "axis":
                calib = ch["calibration"]
                center_text = f", j0={calib['raw_center']}" if "raw_center" in calib else ""
                print(
                    f"  {name}: axis"
                    f" axis_number={ch['axis_number']}"
                    f" rc_min={ch['rc_min']}"
                    f" rc_max={ch['rc_max']}"
                    f" j_min={calib['raw_min']}"
                    f" j_max={calib['raw_max']}"
                    f"{center_text}"
                    f" inverted={bool(calib.get('inverted', False))}"
                )
            else:
                print(f"  {name}: buttons default={ch['default_value']} buttons={ch['buttons']}")