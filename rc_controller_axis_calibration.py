#!/usr/bin/env python3
"""
RC controller calibration tool.

Behavior:
1) Calibrates roll / pitch / throttle / yaw first.
2) After stick calibration, it does NOT finish immediately.
3) It shows two buttons:
      - CALIBRATE ANOTHER INPUT
      - FINISH
4) Extra inputs are calibrated one-by-one as generic axes (switches / sliders /
   knobs) and are appended into the same top-level "axes" list.
5) Extra inputs store only raw_min and raw_max. No center is stored for them.

Output:
    rc_controller_axis_calibration.json
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
import tkinter as tk
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

BASE_DIR = Path(__file__).resolve().parent
CALIBRATION_FILE = BASE_DIR / "rc_controller_axis_calibration.json"

INPUT_DURATION_SEC = 2.5
CENTER_DURATION_SEC = 1.5
EXTRA_INPUT_DURATION_SEC = 3.0
MIN_MOVE_THRESHOLD = 4000

JS_EVENT_BUTTON = 0x01
JS_EVENT_AXIS = 0x02
JS_EVENT_INIT = 0x80

BG = "#0e1117"
PANEL = "#161b27"
ACCENT = "#3b82f6"
ACCENT_L = "#60a5fa"
TEXT = "#e2e8f0"
SUBTLE = "#64748b"
SUCCESS = "#22c55e"
DANGER = "#ef4444"
WARNING = "#f59e0b"
BORDER = "#1e2738"

BOX_SIZE = 200
TARGET_R = 12

FONT_TITLE = ("Courier New", 15, "bold")
FONT_BIG = ("Courier New", 13, "bold")
FONT_BODY = ("Courier New", 11)
FONT_HINT = ("Courier New", 9)
FONT_MONO = ("Courier New", 10)


BASE_INPUT_ORDER = ["roll", "pitch", "throttle", "yaw"]


def list_joystick_devices() -> List[str]:
    return sorted(glob.glob("/dev/input/js*"))


def probe_joystick(path: str) -> Tuple[bool, str]:
    try:
        with open(path, "rb", buffering=0):
            pass
        return True, "OK"
    except PermissionError:
        return (
            False,
            (
                "Permission denied. Add your user to the appropriate input group "
                "or install a udev rule. Temporary workaround: "
                f"sudo chmod a+r {path}"
            ),
        )
    except FileNotFoundError:
        return False, "Device not found."
    except OSError as exc:
        return False, str(exc)


class JsReader(threading.Thread):
    def __init__(self, path: str) -> None:
        super().__init__(daemon=True)
        self.path = path
        self.running = True
        self._fd: Optional[int] = None
        self._lock = threading.Lock()
        self._axes: Dict[int, int] = {}

    def snapshot(self) -> Dict[int, int]:
        with self._lock:
            return dict(self._axes)

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

                if etype == JS_EVENT_AXIS:
                    with self._lock:
                        self._axes[number] = value

        except Exception:
            pass
        finally:
            self.running = False
            fd = self._fd
            self._fd = None
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass


@dataclass
class Step:
    label: str
    hint: str
    duration: float
    left_target: Tuple[float, float]
    right_target: Tuple[float, float]
    action: str


STEPS: List[Step] = [
    Step("Hold ALL sticks at CENTER",
         "Let both sticks rest at neutral. This records baseline centers.",
         CENTER_DURATION_SEC, (0.5, 0.5), (0.5, 0.5), "center_all"),

    Step("Move RIGHT STICK ROLL to MAX (fully RIGHT)",
         "Move only the right-stick horizontal axis fully RIGHT and hold.",
         INPUT_DURATION_SEC, (0.5, 0.5), (1.0, 0.5), "roll_max"),
    Step("Move RIGHT STICK ROLL to MIN (fully LEFT)",
         "Move the same right-stick horizontal axis fully LEFT and hold.",
         INPUT_DURATION_SEC, (0.5, 0.5), (0.0, 0.5), "roll_min"),

    Step("Move RIGHT STICK PITCH to MAX (fully UP)",
         "Move only the right-stick vertical axis fully UP and hold.",
         INPUT_DURATION_SEC, (0.5, 0.5), (0.5, 0.0), "pitch_max"),
    Step("Move RIGHT STICK PITCH to MIN (fully DOWN)",
         "Move the same right-stick vertical axis fully DOWN and hold.",
         INPUT_DURATION_SEC, (0.5, 0.5), (0.5, 1.0), "pitch_min"),

    Step("Return RIGHT STICK to CENTER",
         "Release the right stick back to center.",
         CENTER_DURATION_SEC, (0.5, 0.5), (0.5, 0.5), "right_center"),

    Step("Move LEFT STICK YAW to MAX (fully RIGHT)",
         "Move only the left-stick horizontal axis fully RIGHT and hold.",
         INPUT_DURATION_SEC, (1.0, 0.5), (0.5, 0.5), "yaw_max"),
    Step("Move LEFT STICK YAW to MIN (fully LEFT)",
         "Move the same left-stick horizontal axis fully LEFT and hold.",
         INPUT_DURATION_SEC, (0.0, 0.5), (0.5, 0.5), "yaw_min"),

    Step("Move LEFT STICK THROTTLE to MAX (fully UP)",
         "Move only the left-stick vertical axis fully UP and hold.",
         INPUT_DURATION_SEC, (0.5, 0.0), (0.5, 0.5), "throttle_max"),
    Step("Move LEFT STICK THROTTLE to MIN (fully DOWN)",
         "Move the same left-stick vertical axis fully DOWN and hold.",
         INPUT_DURATION_SEC, (0.5, 1.0), (0.5, 0.5), "throttle_min"),

    Step("Return LEFT STICK to CENTER",
         "Return left stick to its resting position.",
         CENTER_DURATION_SEC, (0.5, 0.5), (0.5, 0.5), "left_center"),
]


class CalibrationBuilder:
    def __init__(self) -> None:
        self.baseline: Dict[int, int] = {}
        self.assigned_axes: Dict[str, int] = {}
        self.axis_info: Dict[str, dict] = {}
        self.partial: Dict[str, dict] = {}
        self.extra_inputs: List[dict] = []

    def record_center(self, samples: List[Dict[int, int]]) -> Optional[str]:
        if not samples:
            return "No baseline samples recorded."

        totals: Dict[int, int] = {}
        counts: Dict[int, int] = {}
        for snap in samples:
            for axis, value in snap.items():
                totals[axis] = totals.get(axis, 0) + value
                counts[axis] = counts.get(axis, 0) + 1

        self.baseline = {
            axis: round(totals[axis] / counts[axis])
            for axis in totals
            if counts[axis] > 0
        }
        if not self.baseline:
            return "Could not read any joystick axes."
        return None

    def next_extra_input_name(self) -> str:
        return f"input{len(self.extra_inputs) + 5}"

    def _detect_axis(self, role: str, samples: List[Dict[int, int]]) -> Tuple[Optional[int], float]:
        reserved = set(self.assigned_axes.values())
        pinned_axis = self.partial.get(role, {}).get("axis_number")

        best_axis: Optional[int] = None
        best_peak = 0.0

        for axis, center in self.baseline.items():
            if pinned_axis is not None and axis != pinned_axis:
                continue
            if axis in reserved and axis != pinned_axis:
                continue

            peak = 0.0
            for snap in samples:
                value = snap.get(axis, center)
                peak = max(peak, abs(value - center))

            if peak > best_peak:
                best_peak = peak
                best_axis = axis

        return best_axis, best_peak

    def capture_extreme(self, role: str, direction: str, samples: List[Dict[int, int]]) -> Optional[str]:
        if direction not in ("max", "min"):
            return "Internal error: invalid direction."
        if not self.baseline:
            return "No baseline yet."
        if not samples:
            return "No samples collected."

        axis, peak = self._detect_axis(role, samples)
        if axis is None:
            return "Could not identify the moved axis."
        if peak < MIN_MOVE_THRESHOLD:
            return f"Movement too small ({int(peak)}). Move the stick fully."

        center = self.baseline[axis]
        values = [snap.get(axis, center) for snap in samples]
        observed_min = int(min(values))
        observed_max = int(max(values))

        include_center = role != "throttle"
        entry = self.partial.get(role, {
            "axis_number": axis,
            "input": role,
            "raw_min": None,
            "raw_max": None,
            "inverted": False,
        })
        if include_center:
            entry["raw_center"] = int(center)

        if entry["axis_number"] != axis:
            return "Different axis detected. Retry and move only the requested control."

        if direction == "max":
            if abs(observed_max - center) >= abs(observed_min - center):
                entry["raw_max"] = observed_max
                entry["inverted"] = False
            else:
                entry["raw_max"] = observed_min
                entry["inverted"] = True
        else:
            if entry["inverted"]:
                entry["raw_min"] = observed_max
            else:
                entry["raw_min"] = observed_min

        self.partial[role] = entry

        if entry["raw_min"] is not None and entry["raw_max"] is not None:
            if entry["raw_min"] == entry["raw_max"]:
                return "Min and max ended up identical. Retry this input."

            final_entry = {
                "input": role,
                "axis_number": int(entry["axis_number"]),
                "raw_min": int(min(entry["raw_min"], entry["raw_max"])),
                "raw_max": int(max(entry["raw_min"], entry["raw_max"])),
                "inverted": bool(entry["inverted"]),
            }
            if include_center:
                final_entry["raw_center"] = int(entry["raw_center"])

            self.assigned_axes[role] = final_entry["axis_number"]
            self.axis_info[role] = final_entry

        return None

    def capture_extra_input(self, input_name: str, samples: List[Dict[int, int]]) -> Optional[str]:
        if not self.baseline:
            return "No baseline yet."
        if not samples:
            return "No samples collected."

        axis, peak = self._detect_axis(input_name, samples)
        if axis is None:
            return "Could not identify the moved axis."
        if peak < MIN_MOVE_THRESHOLD:
            return f"Movement too small ({int(peak)}). Move the switch / slider / knob through its full range."

        center = self.baseline[axis]
        values = [snap.get(axis, center) for snap in samples]
        raw_min = int(min(values))
        raw_max = int(max(values))
        if raw_min == raw_max:
            return "Min and max ended up identical. Retry this input."

        entry = {
            "input": input_name,
            "axis_number": int(axis),
            "raw_min": min(raw_min, raw_max),
            "raw_max": max(raw_min, raw_max),
        }
        self.assigned_axes[input_name] = int(axis)
        self.extra_inputs.append(entry)
        return None

    def refine_center(self, roles: List[str], samples: List[Dict[int, int]]) -> None:
        if not samples:
            return
        for role in roles:
            info = self.axis_info.get(role)
            if info is None:
                continue
            axis = info["axis_number"]
            if "raw_center" not in info:
                continue
            values = [snap.get(axis, info["raw_center"]) for snap in samples]
            if values:
                info["raw_center"] = round(sum(values) / len(values))

    def build(self) -> dict:
        ordered = [
            self.axis_info.get("roll", self._fallback("roll", 0, centered=True)),
            self.axis_info.get("pitch", self._fallback("pitch", 1, centered=True)),
            self.axis_info.get("throttle", self._fallback("throttle", 2, centered=False)),
            self.axis_info.get("yaw", self._fallback("yaw", 3, centered=True)),
        ]
        ordered.extend(self.extra_inputs)
        return {
            "axes": ordered,
        }

    @staticmethod
    def _fallback(input_name: str, axis_number: int, centered: bool) -> dict:
        entry = {
            "axis_number": axis_number,
            "input": input_name,
            "raw_min": -32767 if centered else 0,
            "raw_max": 32767,
            "inverted": False,
        }
        if centered:
            entry["raw_center"] = 0
        return entry


class App(tk.Tk):
    def __init__(self, initial_device: Optional[str] = None) -> None:
        super().__init__()
        self.title("RC Controller Calibration")
        self.configure(bg=BG)
        self.resizable(False, False)
        self.geometry("980x640")

        self.js_path: Optional[str] = None
        self.reader: Optional[JsReader] = None
        self.builder = CalibrationBuilder()

        self._selected: Optional[str] = None
        self._step_idx = 0
        self._step_samples: List[Dict[int, int]] = []
        self._left_guide = [0.5, 0.5]
        self._right_guide = [0.5, 0.5]
        self._extra_input_name: Optional[str] = None

        self.protocol("WM_DELETE_WINDOW", self._on_close)

        if initial_device:
            self._selected = initial_device
            self._show_direct_start_screen(initial_device)
        else:
            self._show_device_screen()

    def _stop_reader(self) -> None:
        reader = self.reader
        if reader is None:
            return
        self.reader = None
        reader.stop()
        if reader.is_alive():
            reader.join(timeout=1.0)

    def _on_close(self) -> None:
        self._stop_reader()
        self.destroy()

    def _show_direct_start_screen(self, dev: str) -> None:
        self._clear()
        self._header("RC CONTROLLER CALIBRATION", f"Using joystick device: {dev}")

        panel = tk.Frame(self, bg=PANEL, highlightthickness=1, highlightbackground=BORDER)
        panel.pack(padx=60, fill="both", expand=True, pady=(0, 4))

        tk.Label(
            panel,
            text="  SELECTED DEVICE",
            font=FONT_HINT,
            fg=SUBTLE,
            bg=PANEL,
            anchor="w",
        ).pack(fill="x", padx=12, pady=(14, 4))

        tk.Label(
            panel,
            text=dev,
            font=FONT_BODY,
            fg=TEXT,
            bg=PANEL,
            anchor="w",
        ).pack(fill="x", padx=20, pady=(0, 14))

        self._status_var = tk.StringVar(value="")
        self._status_lbl = tk.Label(self, textvariable=self._status_var, font=FONT_HINT, fg=SUBTLE, bg=BG, height=2)
        self._status_lbl.pack()

        self._start_btn = tk.Button(
            self,
            text="▶   START CALIBRATION",
            font=FONT_BIG,
            fg=BG,
            bg=ACCENT,
            activebackground=ACCENT_L,
            activeforeground=BG,
            bd=0,
            padx=24,
            pady=10,
            cursor="hand2",
            command=self._on_start,
        )
        self._start_btn.pack(pady=(0, 24))

        ok, msg = probe_joystick(dev)
        if ok:
            self._status_var.set(f"✓  {dev} is accessible")
            self._status_lbl.configure(fg=SUCCESS)
        else:
            self._status_var.set(f"✗  {msg}")
            self._status_lbl.configure(fg=DANGER)
            self._start_btn.configure(state="disabled")

    def _show_device_screen(self) -> None:
        self._clear()
        self._header("RC CONTROLLER CALIBRATION", "Select your joystick device to begin")

        list_frame = tk.Frame(self, bg=PANEL, highlightthickness=1, highlightbackground=BORDER)
        list_frame.pack(padx=60, fill="both", expand=True, pady=(0, 4))

        devices = list_joystick_devices()
        self._dev_rows: Dict[str, tk.Frame] = {}

        if not devices:
            tk.Label(
                list_frame,
                text="\n  No joystick devices found at /dev/input/js*\n",
                font=FONT_BODY,
                fg=DANGER,
                bg=PANEL,
            ).pack()
        else:
            tk.Label(
                list_frame,
                text="  AVAILABLE DEVICES",
                font=FONT_HINT,
                fg=SUBTLE,
                bg=PANEL,
                anchor="w",
            ).pack(fill="x", padx=12, pady=(10, 4))
            for dev in devices:
                self._add_dev_row(list_frame, dev)

        self._status_var = tk.StringVar(value="")
        self._status_lbl = tk.Label(self, textvariable=self._status_var, font=FONT_HINT, fg=SUBTLE, bg=BG, height=2)
        self._status_lbl.pack()

        self._start_btn = tk.Button(
            self,
            text="▶   START CALIBRATION",
            font=FONT_BIG,
            fg=BG,
            bg=ACCENT,
            activebackground=ACCENT_L,
            activeforeground=BG,
            bd=0,
            padx=24,
            pady=10,
            cursor="hand2",
            command=self._on_start,
        )
        self._start_btn.pack(pady=(0, 24))
        self._start_btn.pack_forget()

    def _add_dev_row(self, parent: tk.Widget, dev: str) -> None:
        row = tk.Frame(parent, bg=PANEL, cursor="hand2")
        row.pack(fill="x", padx=10, pady=2)
        self._dev_rows[dev] = row

        dot = tk.Label(row, text="◈", font=FONT_BODY, fg=SUBTLE, bg=PANEL)
        name = tk.Label(row, text=dev, font=FONT_BODY, fg=TEXT, bg=PANEL)
        dot.pack(side="left", padx=(8, 0), pady=8)
        name.pack(side="left", padx=8, pady=8)

        for widget in (row, dot, name):
            widget.bind("<Button-1>", lambda e, d=dev: self._select_dev(d))
            widget.bind("<Enter>", lambda e, r=row: r.configure(bg=BORDER))
            widget.bind("<Leave>", lambda e, r=row, d=dev: r.configure(bg=ACCENT if d == self._selected else PANEL))

    def _select_dev(self, dev: str) -> None:
        self._selected = dev
        for name, row in self._dev_rows.items():
            bg = ACCENT if name == dev else PANEL
            row.configure(bg=bg)
            for child in row.winfo_children():
                child.configure(bg=bg)

        ok, msg = probe_joystick(dev)
        if ok:
            self._status_var.set(f"✓  {dev} is accessible")
            self._status_lbl.configure(fg=SUCCESS)
            self._start_btn.pack(pady=(0, 24))
        else:
            self._status_var.set(f"✗  {msg}")
            self._status_lbl.configure(fg=DANGER)
            self._start_btn.pack_forget()

    def _on_start(self) -> None:
        if not self._selected:
            return

        self._stop_reader()

        self.js_path = self._selected
        self.reader = JsReader(self.js_path)
        self.reader.start()
        time.sleep(0.25)

        if not self.reader.is_alive():
            self._status_var.set(f"✗  Failed to start joystick reader for {self.js_path}")
            self._status_lbl.configure(fg=DANGER)
            self._stop_reader()
            return

        self._step_idx = 0
        self._show_calib_screen()

    def _show_calib_screen(self) -> None:
        self._clear()

        top = tk.Frame(self, bg=BG)
        top.pack(fill="x", padx=30, pady=(22, 0))
        self._instr_var = tk.StringVar()
        self._hint_var = tk.StringVar()
        tk.Label(top, textvariable=self._instr_var, font=FONT_BIG, fg=TEXT, bg=BG, wraplength=780, justify="center").pack(fill="x")
        tk.Label(top, textvariable=self._hint_var, font=FONT_HINT, fg=SUBTLE, bg=BG).pack(pady=(3, 0))

        boxes = tk.Frame(self, bg=BG)
        boxes.pack(expand=True)

        def stick_col(parent: tk.Widget, label: str) -> tk.Canvas:
            col = tk.Frame(parent, bg=BG)
            col.pack(side="left", padx=55)
            tk.Label(col, text=label, font=FONT_HINT, fg=SUBTLE, bg=BG).pack()
            canvas = tk.Canvas(
                col,
                width=BOX_SIZE,
                height=BOX_SIZE,
                bg=PANEL,
                bd=0,
                highlightthickness=1,
                highlightbackground=BORDER,
            )
            canvas.pack(pady=6)
            return canvas

        self._left_cv = stick_col(boxes, "LEFT STICK")
        self._right_cv = stick_col(boxes, "RIGHT STICK")

        tk.Label(self, text="RECORDING", font=FONT_HINT, fg=SUBTLE, bg=BG).pack()
        self._bar = tk.Canvas(self, height=10, bg=PANEL, bd=0, highlightthickness=1, highlightbackground=BORDER)
        self._bar.pack(fill="x", padx=90, pady=(2, 14))
        self._bar_fill = self._bar.create_rectangle(0, 0, 0, 10, fill=ACCENT, width=0)

        bottom = tk.Frame(self, bg=BG)
        bottom.pack(pady=(0, 14))
        self._step_var = tk.StringVar()
        self._err_var = tk.StringVar()
        tk.Label(bottom, textvariable=self._step_var, font=FONT_HINT, fg=SUBTLE, bg=BG).pack()
        tk.Label(bottom, textvariable=self._err_var, font=FONT_HINT, fg=WARNING, bg=BG, wraplength=700, justify="center").pack()

        self._draw_box(self._left_cv, (0.5, 0.5))
        self._draw_box(self._right_cv, (0.5, 0.5))
        self._run_step()

    def _draw_box(
        self,
        canvas: tk.Canvas,
        guide: Tuple[float, float],
        path: Optional[List[Tuple[float, float]]] = None,
    ) -> None:
        canvas.delete("all")
        size = BOX_SIZE
        canvas.create_line(size // 2, 4, size // 2, size - 4, fill=BORDER, dash=(4, 4), width=1)
        canvas.create_line(4, size // 2, size - 4, size // 2, fill=BORDER, dash=(4, 4), width=1)

        def to_xy(pt: Tuple[float, float]) -> Tuple[int, int]:
            return (int(pt[0] * (size - 24)) + 12, int(pt[1] * (size - 24)) + 12)

        if path and len(path) >= 2:
            coords: List[int] = []
            for point in path:
                x, y = to_xy(point)
                coords.extend([x, y])
            canvas.create_line(*coords, fill=ACCENT, width=2, smooth=True, dash=(6, 4))

        gx, gy = to_xy(guide)
        r = TARGET_R
        canvas.create_oval(gx - r - 6, gy - r - 6, gx + r + 6, gy + r + 6, outline=ACCENT, width=1, dash=(3, 5))
        canvas.create_oval(gx - r, gy - r, gx + r, gy + r, fill=ACCENT, outline=ACCENT_L, width=2)

    def _animate_path(
        self,
        left_path: List[Tuple[float, float]],
        right_path: List[Tuple[float, float]],
        duration_ms: int,
        on_done: callable,
    ) -> None:
        t0 = time.monotonic()

        def point_at(path: List[Tuple[float, float]], frac: float) -> Tuple[float, float]:
            if len(path) == 1:
                return path[0]
            segs = len(path) - 1
            pos = min(frac * segs, segs)
            idx = min(int(pos), segs - 1)
            local = pos - idx
            a = path[idx]
            b = path[idx + 1]
            return (a[0] + (b[0] - a[0]) * local, a[1] + (b[1] - a[1]) * local)

        def ease(t: float) -> float:
            return t * t * (3 - 2 * t)

        def tick() -> None:
            frac = min((time.monotonic() - t0) / (duration_ms / 1000.0), 1.0)
            eased = ease(frac)
            left = point_at(left_path, eased)
            right = point_at(right_path, eased)
            self._left_guide = [left[0], left[1]]
            self._right_guide = [right[0], right[1]]
            self._draw_box(self._left_cv, left, left_path)
            self._draw_box(self._right_cv, right, right_path)
            if frac < 1.0:
                self.after(16, tick)
            else:
                on_done()

        tick()

    def _run_step(self) -> None:
        if self._step_idx >= len(STEPS):
            self._show_post_stick_screen()
            return

        step = STEPS[self._step_idx]
        self._instr_var.set(step.label)
        self._hint_var.set(step.hint)
        self._step_var.set(f"Step {self._step_idx + 1} / {len(STEPS)}")
        self._err_var.set("")
        self._bar.coords(self._bar_fill, 0, 0, 0, 10)

        left_path = [(self._left_guide[0], self._left_guide[1]), step.left_target]
        right_path = [(self._right_guide[0], self._right_guide[1]), step.right_target]
        self._animate_path(left_path, right_path, 900, lambda: self._record(step))

    def _record(self, step: Step) -> None:
        self._step_samples = []
        start = time.monotonic()
        duration = step.duration

        def tick() -> None:
            elapsed = time.monotonic() - start
            frac = min(elapsed / duration, 1.0)

            if self.reader:
                snap = self.reader.snapshot()
                if snap:
                    self._step_samples.append(snap)

            width = self._bar.winfo_width()
            self._bar.coords(self._bar_fill, 0, 0, int(width * frac), 10)

            if frac < 1.0:
                self.after(30, tick)
            else:
                self._process(step)

        tick()

    def _process(self, step: Step) -> None:
        err: Optional[str] = None

        if step.action == "center_all":
            err = self.builder.record_center(self._step_samples)
        elif step.action == "roll_max":
            err = self.builder.capture_extreme("roll", "max", self._step_samples)
        elif step.action == "roll_min":
            err = self.builder.capture_extreme("roll", "min", self._step_samples)
        elif step.action == "pitch_max":
            err = self.builder.capture_extreme("pitch", "max", self._step_samples)
        elif step.action == "pitch_min":
            err = self.builder.capture_extreme("pitch", "min", self._step_samples)
        elif step.action == "right_center":
            self.builder.refine_center(["roll", "pitch"], self._step_samples)
        elif step.action == "yaw_max":
            err = self.builder.capture_extreme("yaw", "max", self._step_samples)
        elif step.action == "yaw_min":
            err = self.builder.capture_extreme("yaw", "min", self._step_samples)
        elif step.action == "throttle_max":
            err = self.builder.capture_extreme("throttle", "max", self._step_samples)
        elif step.action == "throttle_min":
            err = self.builder.capture_extreme("throttle", "min", self._step_samples)
        elif step.action == "left_center":
            self.builder.refine_center(["yaw"], self._step_samples)

        if err:
            self._err_var.set(f"⚠  {err}  — retrying in 1 s...")
            self.after(1200, self._run_step)
            return

        self._step_idx += 1
        self.after(200, self._run_step)

    def _show_post_stick_screen(self) -> None:
        self._clear()
        self._header("STICK CALIBRATION COMPLETE", "Choose whether to calibrate another axis or finish")

        panel = tk.Frame(self, bg=PANEL, highlightthickness=1, highlightbackground=BORDER)
        panel.pack(padx=140, pady=35, fill="both", expand=True)

        next_name = self.builder.next_extra_input_name()
        msg = (
            "Roll, pitch, throttle, and yaw are done.\n\n"
            f"Next extra input will be saved as: {next_name}\n"
            "Use this for switches, sliders, or knobs."
        )
        tk.Label(
            panel,
            text=msg,
            font=FONT_BODY,
            fg=TEXT,
            bg=PANEL,
            justify="center",
        ).pack(expand=True)

        buttons = tk.Frame(panel, bg=PANEL)
        buttons.pack(pady=(0, 30))

        tk.Button(
            buttons,
            text="CALIBRATE ANOTHER INPUT",
            font=FONT_BIG,
            fg=BG,
            bg=ACCENT,
            activebackground=ACCENT_L,
            activeforeground=BG,
            bd=0,
            padx=24,
            pady=10,
            cursor="hand2",
            command=self._start_extra_input_calibration,
        ).pack(side="left", padx=10)

        tk.Button(
            buttons,
            text="FINISH",
            font=FONT_BIG,
            fg=TEXT,
            bg=BORDER,
            activebackground=PANEL,
            activeforeground=TEXT,
            bd=0,
            padx=24,
            pady=10,
            cursor="hand2",
            command=self._finish,
        ).pack(side="left", padx=10)

    def _start_extra_input_calibration(self) -> None:
        self._extra_input_name = self.builder.next_extra_input_name()
        self._show_extra_input_screen()

    def _show_extra_input_screen(self) -> None:
        self._clear()
        self._header(
            f"CALIBRATE {self._extra_input_name.upper()}",
            "Move one switch / slider / knob across its full range and hold / sweep it clearly",
        )

        panel = tk.Frame(self, bg=PANEL, highlightthickness=1, highlightbackground=BORDER)
        panel.pack(padx=90, fill="both", expand=True, pady=(0, 10))

        self._extra_instr_var = tk.StringVar(
            value=(
                f"Now calibrating {self._extra_input_name}.\n\n"
                "Move only ONE extra controller input through its full range.\n"
                "The tool will detect the axis number automatically.\n"
                "No center will be stored for this input."
            )
        )
        tk.Label(
            panel,
            textvariable=self._extra_instr_var,
            font=FONT_BODY,
            fg=TEXT,
            bg=PANEL,
            justify="center",
            wraplength=700,
        ).pack(pady=(30, 16))

        tk.Label(panel, text="RECORDING", font=FONT_HINT, fg=SUBTLE, bg=PANEL).pack()
        self._bar = tk.Canvas(panel, height=10, bg=BG, bd=0, highlightthickness=1, highlightbackground=BORDER)
        self._bar.pack(fill="x", padx=80, pady=(2, 14))
        self._bar_fill = self._bar.create_rectangle(0, 0, 0, 10, fill=ACCENT, width=0)

        self._extra_err_var = tk.StringVar(value="")
        tk.Label(panel, textvariable=self._extra_err_var, font=FONT_HINT, fg=WARNING, bg=PANEL, wraplength=700, justify="center").pack()

        buttons = tk.Frame(panel, bg=PANEL)
        buttons.pack(pady=(20, 30))
        tk.Button(
            buttons,
            text="START",
            font=FONT_BIG,
            fg=BG,
            bg=ACCENT,
            activebackground=ACCENT_L,
            activeforeground=BG,
            bd=0,
            padx=24,
            pady=10,
            cursor="hand2",
            command=self._record_extra_input,
        ).pack(side="left", padx=10)
        tk.Button(
            buttons,
            text="BACK",
            font=FONT_BIG,
            fg=TEXT,
            bg=BORDER,
            activebackground=PANEL,
            activeforeground=TEXT,
            bd=0,
            padx=24,
            pady=10,
            cursor="hand2",
            command=self._show_post_stick_screen,
        ).pack(side="left", padx=10)

    def _record_extra_input(self) -> None:
        self._step_samples = []
        self._extra_err_var.set("")
        start = time.monotonic()
        duration = EXTRA_INPUT_DURATION_SEC

        def tick() -> None:
            elapsed = time.monotonic() - start
            frac = min(elapsed / duration, 1.0)

            if self.reader:
                snap = self.reader.snapshot()
                if snap:
                    self._step_samples.append(snap)

            width = self._bar.winfo_width()
            self._bar.coords(self._bar_fill, 0, 0, int(width * frac), 10)

            if frac < 1.0:
                self.after(30, tick)
            else:
                self._process_extra_input()

        tick()

    def _process_extra_input(self) -> None:
        if not self._extra_input_name:
            self._show_post_stick_screen()
            return

        err = self.builder.capture_extra_input(self._extra_input_name, self._step_samples)
        if err:
            self._extra_err_var.set(f"⚠  {err}")
            return

        self._show_extra_input_success()

    def _show_extra_input_success(self) -> None:
        latest = self.builder.extra_inputs[-1]
        self._clear()
        self._header(
            f"{latest['input'].upper()} CALIBRATED",
            f"Detected axis {latest['axis_number']} | min={latest['raw_min']} | max={latest['raw_max']}",
            title_fg=SUCCESS,
        )

        panel = tk.Frame(self, bg=PANEL, highlightthickness=1, highlightbackground=BORDER)
        panel.pack(padx=140, pady=35, fill="both", expand=True)

        tk.Label(
            panel,
            text="You can calibrate another extra input, or finish and save the JSON file.",
            font=FONT_BODY,
            fg=TEXT,
            bg=PANEL,
            justify="center",
        ).pack(expand=True)

        buttons = tk.Frame(panel, bg=PANEL)
        buttons.pack(pady=(0, 30))

        tk.Button(
            buttons,
            text="CALIBRATE ANOTHER INPUT",
            font=FONT_BIG,
            fg=BG,
            bg=ACCENT,
            activebackground=ACCENT_L,
            activeforeground=BG,
            bd=0,
            padx=24,
            pady=10,
            cursor="hand2",
            command=self._start_extra_input_calibration,
        ).pack(side="left", padx=10)

        tk.Button(
            buttons,
            text="FINISH",
            font=FONT_BIG,
            fg=TEXT,
            bg=BORDER,
            activebackground=PANEL,
            activeforeground=TEXT,
            bd=0,
            padx=24,
            pady=10,
            cursor="hand2",
            command=self._finish,
        ).pack(side="left", padx=10)

    def _finish(self) -> None:
        self._stop_reader()

        calib = self.builder.build()
        out = Path(CALIBRATION_FILE)
        with open(out, "w", encoding="utf-8") as f:
            json.dump(calib, f, indent=2)

        self._clear()
        self._header("CALIBRATION COMPLETE", f"Saved -> {out.resolve()}", title_fg=SUCCESS)

        tbl = tk.Frame(self, bg=PANEL, highlightthickness=1, highlightbackground=BORDER)
        tbl.pack(padx=20, fill="x", pady=10)

        headers = ["input", "axis", "raw_min", "center", "raw_max", "inverted"]
        widths = [120, 60, 110, 100, 110, 100]

        grid = tk.Frame(tbl, bg=PANEL)
        grid.pack(fill="x", padx=1, pady=1)

        for i, width in enumerate(widths):
            grid.grid_columnconfigure(i, minsize=width, weight=1)

        for i, header in enumerate(headers):
            tk.Label(
                grid,
                text=header.upper(),
                font=FONT_MONO,
                fg=SUBTLE,
                bg=BORDER,
                anchor="center",
                padx=6,
                pady=6,
            ).grid(row=0, column=i, sticky="nsew", pady=(0, 1))

        for row_idx, axis in enumerate(calib["axes"], start=1):
            values = [
                axis.get("input", ""),
                axis.get("axis_number", ""),
                axis.get("raw_min", ""),
                axis.get("raw_center", "—"),
                axis.get("raw_max", ""),
                "YES" if axis.get("inverted", False) else ("—" if "inverted" not in axis else "no"),
            ]
            for col_idx, value in enumerate(values):
                fg = WARNING if value == "YES" else TEXT
                tk.Label(
                    grid,
                    text=str(value),
                    font=FONT_MONO,
                    fg=fg,
                    bg=PANEL,
                    anchor="center",
                    padx=6,
                    pady=4,
                ).grid(row=row_idx, column=col_idx, sticky="nsew")

        tk.Button(
            self,
            text="CLOSE",
            font=FONT_BIG,
            fg=BG,
            bg=ACCENT,
            activebackground=ACCENT_L,
            activeforeground=BG,
            bd=0,
            padx=24,
            pady=10,
            cursor="hand2",
            command=self._on_close,
        ).pack(pady=24)

    def _clear(self) -> None:
        for widget in self.winfo_children():
            widget.destroy()

    def _header(self, title: str, sub: str, title_fg: str = ACCENT) -> None:
        tk.Label(self, text=title, font=FONT_TITLE, fg=title_fg, bg=BG, anchor="center").pack(fill="x", pady=(26, 2))
        tk.Label(self, text=sub, font=FONT_HINT, fg=SUBTLE, bg=BG).pack(pady=(0, 14))


if __name__ == "__main__":
    if sys.platform != "linux":
        print("This calibration tool is Linux-only.")
        sys.exit(1)

    device = sys.argv[1] if len(sys.argv) > 1 else None
    App(device).mainloop()