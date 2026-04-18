#!/usr/bin/env python3
"""
simulate_rc_controller.py

Helper module for:
- loading rc_controller_channel_function_mapping.yaml
- loading rc_controller_axis_calibration.json
- reading Linux joystick events from /dev/input/js*
- converting live joystick input into RC channel values
- seamless silent reconnection when controller disconnects / reconnects

Axis inversion two-stage pipeline
----------------------------------
Stage 1  calibration "inverted"  corrects the physical stick direction.
Stage 2  channel     "invert"    flips the final RC output value AFTER stage 1:
           result = rc_min + rc_max - result

Button logic types
-------------------
None      each button independently maps to its own RC value.
AND       every listed button must be held simultaneously → condition_met_value.
Sequence  buttons must be pressed AND HELD in listed order →  condition_met_value.
          Resets to default_value the instant any sequence button is released.

Public API shared with rc_controller_channel_function_mapping.py
-----------------------------------------------------------------
  compute_channel_output(channel, axes, buttons, seq_state=None) -> int
  update_sequence_state(seq_state, button_events, sequence)
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

BASE_DIR            = Path(__file__).resolve().parent
MAPPING_FILE        = BASE_DIR / "rc_controller_channel_function_mapping.yaml"
CALIBRATION_FILE    = BASE_DIR / "rc_controller_axis_calibration.json"
CHANNEL_COUNT_LIMIT = 16

JS_EVENT_BUTTON = 0x01
JS_EVENT_AXIS   = 0x02
JS_EVENT_INIT   = 0x80

RECONNECT_DELAY_S = 0.5   # seconds between reconnect attempts


# ─────────────────────────────────────────────────────────────────────────────
# Math helpers
# ─────────────────────────────────────────────────────────────────────────────

def clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def clamp_int(value: float, lo: int, hi: int) -> int:
    return max(lo, min(hi, int(round(value))))


# ─────────────────────────────────────────────────────────────────────────────
# Device helpers
# ─────────────────────────────────────────────────────────────────────────────

def list_joystick_devices() -> List[str]:
    return sorted(glob.glob("/dev/input/js*"))


def choose_default_joystick() -> str:
    devices = list_joystick_devices()
    if not devices:
        raise RuntimeError("No joystick devices found at /dev/input/js*")
    return devices[0]


# ─────────────────────────────────────────────────────────────────────────────
# YAML parser (zero external dependencies)
# ─────────────────────────────────────────────────────────────────────────────

def _parse_scalar(text: str):
    text = text.strip()
    if text == "":
        return ""
    low = text.lower()
    if low == "null":
        return None
    if low == "true":
        return True
    if low == "false":
        return False
    if (text.startswith('"') and text.endswith('"')) or \
       (text.startswith("'") and text.endswith("'")):
        return text[1:-1]
    try:
        return int(text)
    except ValueError:
        pass
    try:
        return float(text)
    except ValueError:
        pass
    return text


def _parse_inline_list(text: str) -> list:
    """Parse  '[1, 2, 3]'  into a Python list."""
    text = text.strip()
    if not (text.startswith("[") and text.endswith("]")):
        raise ValueError(f"Expected inline list, got: {text!r}")
    inner = text[1:-1].strip()
    if not inner:
        return []
    return [_parse_scalar(item.strip()) for item in inner.split(",")]


def _tiny_yaml_parser(path: Path) -> dict:
    """
    Minimal YAML parser for the exact subset produced by this tool:
      indent 0   channel_N:
      indent 2     key: scalar_or_inline_list
      indent 2     buttons:          (blank → sub-mapping follows)
      indent 4       btn_id: value
    """
    lines = path.read_text(encoding="utf-8").splitlines()
    data: Dict[str, dict] = {}
    cur_ch:  Optional[str] = None
    cur_sub: Optional[str] = None

    for raw in lines:
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue

        indent   = len(raw) - len(raw.lstrip(" "))
        stripped = raw.strip()

        if indent == 0 and stripped.endswith(":"):
            cur_ch  = stripped[:-1].strip()
            cur_sub = None
            data[cur_ch] = {}
            continue

        if cur_ch is None:
            raise ValueError(f"Line before any channel key: {raw!r}")

        if indent == 2:
            if ":" not in stripped:
                raise ValueError(f"Expected key:value at indent 2: {raw!r}")
            key, val_raw = stripped.split(":", 1)
            key     = key.strip()
            val_raw = val_raw.strip()
            if val_raw == "":
                data[cur_ch][key] = {}
                cur_sub = key
            elif val_raw.startswith("["):
                data[cur_ch][key] = _parse_inline_list(val_raw)
                cur_sub = None
            else:
                data[cur_ch][key] = _parse_scalar(val_raw)
                cur_sub = None
            continue

        if indent == 4:
            if cur_sub is None:
                raise ValueError(f"Indent-4 line outside a sub-mapping: {raw!r}")
            if ":" not in stripped:
                raise ValueError(f"Expected key:value at indent 4: {raw!r}")
            k, v = stripped.split(":", 1)
            data[cur_ch][cur_sub][_parse_scalar(k.strip())] = _parse_scalar(v.strip())
            continue

        raise ValueError(f"Unsupported indent {indent} near: {raw!r}")

    return data


def load_yaml_mapping(path: Path) -> dict:
    try:
        import yaml          # type: ignore
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
        if not isinstance(data, dict):
            raise ValueError("Top-level YAML must be a mapping.")
        return data
    except ImportError:
        return _tiny_yaml_parser(path)


# ─────────────────────────────────────────────────────────────────────────────
# Calibration
# ─────────────────────────────────────────────────────────────────────────────

def load_calibration(path: Path) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict) or "axes" not in data or \
            not isinstance(data["axes"], list):
        raise ValueError(f"Invalid calibration JSON: {path}")
    return data


def index_calibration_axes(calibration: dict) -> Dict[int, dict]:
    out: Dict[int, dict] = {}
    for entry in calibration["axes"]:
        if not isinstance(entry, dict):
            continue
        n = entry.get("axis_number")
        if isinstance(n, int):
            if n in out:
                raise ValueError(f"Duplicate calibration axis_number: {n}")
            out[n] = entry
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Channel config normaliser
# ─────────────────────────────────────────────────────────────────────────────

def _sorted_channel_items(mapping: dict) -> List[Tuple[str, dict]]:
    def key(name: str) -> Tuple[int, str]:
        if name.startswith("channel_"):
            try:
                return int(name.split("_", 1)[1]), name
            except ValueError:
                pass
        return (10 ** 9, name)
    items = [(k, v) for k, v in mapping.items() if isinstance(v, dict)]
    items.sort(key=lambda p: key(p[0]))
    return items


def normalize_channel_config(
    mapping:          dict,
    calibration_axes: Dict[int, dict],
) -> List[dict]:
    """
    Resolve every channel to a flat dict ready for compute_channel_output().
    Calibration data is embedded inside axis channels so no extra context is needed.
    """
    items = _sorted_channel_items(mapping)
    if not items:
        raise ValueError("No channels found in mapping YAML.")
    if len(items) > CHANNEL_COUNT_LIMIT:
        raise ValueError(f"Max {CHANNEL_COUNT_LIMIT} channels supported.")

    channels: List[dict] = []

    for index, (name, cfg) in enumerate(items, start=1):
        default_val = int(cfg.get("default_value", 1500))
        input_type  = str(cfg.get("input_type", "")).strip().lower()

        ch: dict = {
            "name":          name,
            "index_1based":  index,
            "default_value": default_val,
            "input_type":    input_type,
        }

        # ── axis ──────────────────────────────────────────────────────────────
        if input_type == "axis":
            if "axis_number" not in cfg:
                raise ValueError(f"{name}: missing axis_number")
            axis_n = int(cfg["axis_number"])
            if axis_n not in calibration_axes:
                raise ValueError(
                    f"{name}: axis_number {axis_n} not found in calibration file"
                )
            calib = dict(calibration_axes[axis_n])   # shallow copy
            if "raw_min" not in calib or "raw_max" not in calib:
                raise ValueError(
                    f"{name}: calibration for axis {axis_n} missing raw_min/raw_max"
                )
            rc_min = int(cfg.get("rc_min", 1000))
            rc_max = int(cfg.get("rc_max", 2000))
            if rc_min == rc_max:
                raise ValueError(f"{name}: rc_min == rc_max")

            raw_inv = cfg.get("invert", False)
            if isinstance(raw_inv, str):
                raw_inv = raw_inv.strip().lower() == "true"

            ch["axis_number"] = axis_n
            ch["rc_min"]      = rc_min
            ch["rc_max"]      = rc_max
            ch["calibration"] = calib
            ch["invert"]      = bool(raw_inv)

        # ── buttons ───────────────────────────────────────────────────────────
        elif input_type == "buttons":
            raw_logic = str(cfg.get("logic", "None")).strip()
            logic = {"none": "None", "and": "AND", "sequence": "Sequence"}.get(
                raw_logic.lower(), raw_logic
            )
            ch["logic"]       = logic
            raw_buttons       = cfg.get("buttons", {})

            if logic == "None":
                if not isinstance(raw_buttons, dict):
                    raise ValueError(f"{name}: logic=None requires buttons to be a mapping")
                ch["buttons"] = {int(k): int(v) for k, v in raw_buttons.items()}

            elif logic in ("AND", "Sequence"):
                if isinstance(raw_buttons, list):
                    btn_list = [int(b) for b in raw_buttons]
                elif isinstance(raw_buttons, dict):
                    btn_list = [int(k) for k in sorted(raw_buttons.keys())]
                else:
                    raise ValueError(f"{name}: logic={logic} requires buttons to be a list")
                ch["buttons"]             = btn_list
                ch["condition_met_value"] = int(cfg.get("condition_met_value", 2000))
            else:
                raise ValueError(f"{name}: unknown button logic '{logic}'")

        else:
            raise ValueError(f"{name}: input_type must be 'axis' or 'buttons'")

        channels.append(ch)

    return channels


def load_channel_profile(
    mapping_path:     Path | str = MAPPING_FILE,
    calibration_path: Path | str = CALIBRATION_FILE,
) -> List[dict]:
    mapping_path     = Path(mapping_path)
    calibration_path = Path(calibration_path)
    if not mapping_path.exists():
        raise FileNotFoundError(f"Missing mapping file: {mapping_path.resolve()}")
    if not calibration_path.exists():
        raise FileNotFoundError(f"Missing calibration file: {calibration_path.resolve()}")
    mapping          = load_yaml_mapping(mapping_path)
    calibration      = load_calibration(calibration_path)
    calibration_axes = index_calibration_axes(calibration)
    return normalize_channel_config(mapping, calibration_axes)


# ─────────────────────────────────────────────────────────────────────────────
# Core computation  –  single source of truth, used by BOTH files
# ─────────────────────────────────────────────────────────────────────────────

def apply_axis_mapping(
    joystick_value: int,
    calib:          dict,
    rc_min:         int,
    rc_max:         int,
    ch_invert:      bool = False,
) -> int:
    """
    Map a raw joystick axis value to an RC µs value.

    Stage 1 – normalise using calibration data (incl. calib["inverted"]).
    Stage 2 – apply channel-level invert:  result = rc_min + rc_max - result
    """
    j       = float(joystick_value)
    j_min   = float(calib["raw_min"])
    j_max   = float(calib["raw_max"])
    cal_inv = bool(calib.get("inverted", False))

    if "raw_center" in calib:
        # Centered (two-sided) axis
        j0    = float(calib["raw_center"])
        denom = (j_max - j0) if j >= j0 else (j0 - j_min)
        n     = 0.0 if abs(denom) < 1e-9 else (j - j0) / denom
        n     = clamp(n, -1.0, 1.0)
        if cal_inv:
            n = -n
        r_mid = (rc_min + rc_max) / 2.0
        r = r_mid + n * (rc_max - r_mid) if n >= 0.0 else r_mid + n * (r_mid - rc_min)
    else:
        # Non-centered (one-sided) axis, e.g. throttle
        denom = j_max - j_min
        n     = 0.0 if abs(denom) < 1e-9 else (j - j_min) / denom
        n     = clamp(n, 0.0, 1.0)
        if cal_inv:
            n = 1.0 - n
        r = rc_min + n * (rc_max - rc_min)

    result = clamp_int(r, min(rc_min, rc_max), max(rc_min, rc_max))

    # Stage 2: channel-level invert applied AFTER calibration
    if ch_invert:
        result = rc_min + rc_max - result

    return result


def compute_channel_output(
    channel:   dict,
    axes:      Dict[int, int],
    buttons:   Dict[int, int],
    seq_state: Optional[dict] = None,
) -> int:
    """
    Compute the RC µs output for one channel.

    This is the single authoritative function used by both
    simulate_rc_controller.RcJoystickController  and
    rc_controller_channel_function_mapping.App  (for the live indicator).

    Parameters
    ----------
    channel   : fully-resolved channel dict (from normalize_channel_config)
    axes      : {axis_number: raw_joystick_int}
    buttons   : {button_id: 0_or_1}
    seq_state : mutable dict {'order_buffer': [...]} for Sequence channels;
                pass None for all other channel types.

    Returns
    -------
    int  RC µs value
    """
    default  = int(channel["default_value"])
    inp      = channel["input_type"]

    # ── axis ──────────────────────────────────────────────────────────────────
    if inp == "axis":
        calib  = channel["calibration"]
        axis_n = int(channel["axis_number"])
        rc_min = int(channel["rc_min"])
        rc_max = int(channel["rc_max"])
        neutral = int(calib.get("raw_center", calib.get("raw_min", 0)))
        raw     = int(axes.get(axis_n, neutral))
        return apply_axis_mapping(
            raw, calib, rc_min, rc_max,
            ch_invert=bool(channel.get("invert", False)),
        )

    # ── buttons ───────────────────────────────────────────────────────────────
    if inp == "buttons":
        logic = channel.get("logic", "None")

        if logic == "None":
            for bid, val in channel["buttons"].items():
                if buttons.get(bid, 0):
                    return int(val)
            return default

        if logic == "AND":
            bl: List[int] = channel["buttons"]
            if bl and all(buttons.get(b, 0) for b in bl):
                return int(channel["condition_met_value"])
            return default

        if logic == "Sequence":
            if seq_state is not None:
                buf: List[int] = seq_state.get("order_buffer", [])
                seq: List[int] = channel["buttons"]
                # All sequence buttons must be held AND have been pressed in order
                if seq and buf == seq and all(buttons.get(b, 0) for b in seq):
                    return int(channel["condition_met_value"])
            return default

    return default


def update_sequence_state(
    seq_state:     dict,
    button_events: List[Tuple[int, int]],
    sequence:      List[int],
) -> None:
    """
    Update seq_state in-place for a Sequence channel.

    Button press:
      - Correct next button in sequence → advance order_buffer.
      - Any other button → reset order_buffer (restart from this press
        if it equals sequence[0]).

    Button release:
      - Any button that is part of the sequence → reset order_buffer.
        This enforces the "press & HOLD in order" rule.
    """
    buf: List[int] = seq_state.setdefault("order_buffer", [])

    for bid, val in button_events:
        if val == 1:   # press
            if bid in sequence:
                pos = len(buf)
                if pos < len(sequence) and sequence[pos] == bid:
                    buf.append(bid)
                else:
                    buf.clear()
                    if sequence and sequence[0] == bid:
                        buf.append(bid)
        else:          # release
            if bid in sequence:
                buf.clear()   # any sequence button released → full reset


# ─────────────────────────────────────────────────────────────────────────────
# Joystick reader thread (with silent reconnection)
# ─────────────────────────────────────────────────────────────────────────────

class JsReader(threading.Thread):
    """
    Reads raw Linux joystick events.

    When the device disappears (USB unplug, etc.) the thread sleeps and
    retries opening the path every RECONNECT_DELAY_S seconds — silently,
    with no external intervention required.

    Thread-safe API
    ---------------
    snapshot()           → (axes_dict, buttons_dict)  current state snapshot
    pop_button_events()  → List[(button_id, 0_or_1)]  events since last call
    connected            → bool
    """

    def __init__(self, path: str) -> None:
        super().__init__(daemon=True)
        self.path      = path
        self.running   = True
        self._lock     = threading.Lock()
        self._fd: Optional[int] = None
        self._connected = False

        self._axes:       Dict[int, int]        = {}
        self._buttons:    Dict[int, int]        = {}
        self._btn_events: List[Tuple[int, int]] = []   # (bid, 0_or_1)

    # ── public ─────────────────────────────────────────────────────────────────
    @property
    def connected(self) -> bool:
        return self._connected

    def snapshot(self) -> Tuple[Dict[int, int], Dict[int, int]]:
        with self._lock:
            return dict(self._axes), dict(self._buttons)

    def pop_button_events(self) -> List[Tuple[int, int]]:
        with self._lock:
            evts = list(self._btn_events)
            self._btn_events.clear()
            return evts

    def stop(self) -> None:
        self.running = False
        self._close_fd()

    # ── internals ──────────────────────────────────────────────────────────────
    def _close_fd(self) -> None:
        fd, self._fd = self._fd, None
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass

    def run(self) -> None:
        while self.running:
            try:
                fd = os.open(self.path, os.O_RDONLY | os.O_NONBLOCK)
            except OSError:
                time.sleep(RECONNECT_DELAY_S)
                continue

            self._fd        = fd
            self._connected = True

            try:
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
                            v = 1 if value else 0
                            self._buttons[number] = v
                            self._btn_events.append((number, v))

            except Exception as exc:
                print(f"[JsReader] error: {exc}", file=sys.stderr)
            finally:
                self._connected = False
                self._close_fd()

            if self.running:
                time.sleep(RECONNECT_DELAY_S)


# ─────────────────────────────────────────────────────────────────────────────
# High-level controller class
# ─────────────────────────────────────────────────────────────────────────────

class RcJoystickController:
    """
    Load the channel profile, start the joystick reader thread, and expose
    get_channel_values().  Sequence channel state is managed here.
    """

    def __init__(
        self,
        js_path:          Optional[str] = None,
        mapping_path:     Path | str    = MAPPING_FILE,
        calibration_path: Path | str    = CALIBRATION_FILE,
    ) -> None:
        self.js_path  = js_path or choose_default_joystick()
        self.channels = load_channel_profile(mapping_path, calibration_path)
        self.reader   = JsReader(self.js_path)
        self._started = False
        # Sequence state per channel index (only allocated for Sequence channels)
        self._seq_states: Dict[int, dict] = {
            i: {"order_buffer": []}
            for i, ch in enumerate(self.channels)
            if ch["input_type"] == "buttons" and ch.get("logic") == "Sequence"
        }

    def start(self) -> None:
        if self._started:
            return
        self.reader.start()
        deadline = time.monotonic() + 2.0
        while not self.reader.connected and time.monotonic() < deadline:
            time.sleep(0.05)
        self._started = True

    def stop(self) -> None:
        self.reader.stop()
        if self.reader.is_alive():
            self.reader.join(timeout=1.0)
        self._started = False

    def get_channel_values(self) -> List[int]:
        axes, buttons = self.reader.snapshot()
        btn_events    = self.reader.pop_button_events()

        # Feed button events into sequence state machines
        for i, ch in enumerate(self.channels):
            if i in self._seq_states:
                update_sequence_state(self._seq_states[i], btn_events, ch["buttons"])

        return [
            compute_channel_output(
                ch, axes, buttons,
                seq_state=self._seq_states.get(i),
            )
            for i, ch in enumerate(self.channels)
        ]

    def print_channel_summary(self) -> None:
        print("Loaded channel mapping:")
        for ch in self.channels:
            name = ch["name"]
            if ch["input_type"] == "axis":
                cal = ch["calibration"]
                ctr = f", j0={cal['raw_center']}" if "raw_center" in cal else ""
                print(
                    f"  {name}: axis"
                    f"  axis_number={ch['axis_number']}"
                    f"  rc={ch['rc_min']}..{ch['rc_max']}"
                    f"  j={cal['raw_min']}..{cal['raw_max']}{ctr}"
                    f"  calib_inv={bool(cal.get('inverted', False))}"
                    f"  ch_invert={ch['invert']}"
                )
            else:
                logic = ch.get("logic", "None")
                if logic == "None":
                    print(f"  {name}: buttons  logic=None  map={ch['buttons']}")
                else:
                    print(
                        f"  {name}: buttons  logic={logic}"
                        f"  seq={ch['buttons']}"
                        f"  cond={ch['condition_met_value']}"
                    )
def main() -> int:
    controller = RcJoystickController()

    try:
        controller.start()
        print(f"Reading joystick: {controller.js_path}")
        print("Press Ctrl+C to stop.")

        while True:
            values = controller.get_channel_values()
            line = "Channel values: " + "  ".join(
                f"CH{i + 1}={v}" for i, v in enumerate(values)
            )

            # '\r' returns to the start of the same line, and end='' avoids newlines
            print(f"\r{line}", end="", flush=True)
            time.sleep(0.05)

    except KeyboardInterrupt:
        print()  # move to a clean new line on exit
        return 0
    finally:
        controller.stop()


if __name__ == "__main__":
    raise SystemExit(main())