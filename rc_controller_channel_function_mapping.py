#!/usr/bin/env python3
"""
rc_controller_channel_function_mapping.py

Interactive Tkinter tool for building rc_controller_channel_function_mapping.yaml.

Usage:
    python3 rc_controller_channel_function_mapping.py
    python3 rc_controller_channel_function_mapping.py /dev/input/js0

Features
--------
- Select a local joystick device.
- Choose how many channels to configure.
- For each channel:
    * Channel type: axis  or  buttons
    * Axis:   detect axis number, set rc_min/rc_max, toggle channel invert.
    * Buttons logic:
        None     – each button individually mapped to an RC value.
        AND      – all listed buttons held → condition_met_value.
        Sequence – press & hold buttons in listed order → condition_met_value;
                   any release resets to default_value.
    * Live output indicator showing the simulated channel value in real time,
      computed with the SAME function used by simulate_rc_controller.py.

Output: rc_controller_channel_function_mapping.yaml  (next to this script)
"""

from __future__ import annotations

import glob
import struct
import sys
import threading
import time
import tkinter as tk
from dataclasses import dataclass, field
from pathlib import Path
from tkinter import messagebox
from typing import Dict, List, Optional, Tuple

# ── shared computation ────────────────────────────────────────────────────────
# Both this file and simulate_rc_controller.py use the SAME compute_channel_output
# function to guarantee the output indicator matches real runtime behaviour.

_THIS_DIR = str(Path(__file__).resolve().parent)
if _THIS_DIR not in sys.path:
    sys.path.insert(0, _THIS_DIR)

try:
    from simulate_rc_controller import (
        compute_channel_output,
        update_sequence_state,
        load_calibration,
        index_calibration_axes,
    )
    _SIMULATE_AVAILABLE = True
except ImportError:
    _SIMULATE_AVAILABLE = False

BASE_DIR     = Path(__file__).resolve().parent
OUTPUT_FILE  = BASE_DIR / "rc_controller_channel_function_mapping.yaml"
CALIB_FILE   = BASE_DIR / "rc_controller_axis_calibration.json"

JS_EVENT_BUTTON = 0x01
JS_EVENT_AXIS   = 0x02
JS_EVENT_INIT   = 0x80

MIN_AXIS_MOVE_THRESHOLD = 4000
AXIS_DETECTION_SECONDS  = 2.5
POLL_MS                 = 40

# ── colours ───────────────────────────────────────────────────────────────────
BG      = "#0e1117"
PANEL   = "#161b27"
ACCENT  = "#3b82f6"
ACCENT_L= "#60a5fa"
TEXT    = "#e2e8f0"
SUBTLE  = "#64748b"
SUCCESS = "#22c55e"
DANGER  = "#ef4444"
WARNING = "#f59e0b"
BORDER  = "#1e2738"
ROW_ALT = "#121826"

FONT_TITLE = ("Courier New", 16, "bold")
FONT_BIG   = ("Courier New", 13, "bold")
FONT_BODY  = ("Courier New", 11)
FONT_HINT  = ("Courier New",  9)
FONT_MONO  = ("Courier New", 10)

LOGIC_NONE     = "None"
LOGIC_AND      = "AND"
LOGIC_SEQUENCE = "Sequence"
LOGIC_OPTIONS  = [LOGIC_NONE, LOGIC_AND, LOGIC_SEQUENCE]


# ─────────────────────────────────────────────────────────────────────────────
# Data model
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class ChannelConfig:
    default_value:       int           = 1500
    input_type:          str           = "axis"
    # axis fields
    axis_number:         Optional[int] = None
    axis_min:            int           = 1000
    axis_max:            int           = 2000
    invert:              bool          = False
    # button fields
    logic:               str           = LOGIC_NONE
    buttons_map:         Dict[int,int] = field(default_factory=dict)  # logic=None
    buttons_list:        List[int]     = field(default_factory=list)  # logic=AND/Seq
    condition_met_value: int           = 2000


# ─────────────────────────────────────────────────────────────────────────────
# Joystick reader (local, lightweight – used only by the mapping tool)
# Exposes both axes/button state AND raw (button_id, 0_or_1) event stream
# so the sequence preview and "listen" mode both work.
# ─────────────────────────────────────────────────────────────────────────────

class _JsReader(threading.Thread):
    """Reads /dev/input/jsN; no reconnection logic needed here since the
    mapping tool only runs while the controller is connected."""

    def __init__(self, path: str) -> None:
        super().__init__(daemon=True)
        self.path    = path
        self.running = True
        self._lock   = threading.Lock()
        self._axes:       Dict[int, int]        = {}
        self._buttons:    Dict[int, int]        = {}
        self._btn_events: List[Tuple[int, int]] = []   # (bid, 0_or_1)

    def snapshot_axes(self) -> Dict[int, int]:
        with self._lock:
            return dict(self._axes)

    def snapshot_buttons(self) -> Dict[int, int]:
        with self._lock:
            return dict(self._buttons)

    def pop_button_events(self) -> List[Tuple[int, int]]:
        with self._lock:
            evts = list(self._btn_events)
            self._btn_events.clear()
            return evts

    def stop(self) -> None:
        self.running = False

    def run(self) -> None:
        try:
            with open(self.path, "rb", buffering=0) as f:
                while self.running:
                    raw = f.read(8)
                    if len(raw) != 8:
                        continue
                    _, value, etype, number = struct.unpack("IhBB", raw)
                    if etype & JS_EVENT_INIT:
                        etype &= ~JS_EVENT_INIT
                    with self._lock:
                        if etype == JS_EVENT_AXIS:
                            self._axes[number] = value
                        elif etype == JS_EVENT_BUTTON:
                            v = 1 if value else 0
                            self._buttons[number] = v
                            self._btn_events.append((number, v))
        except Exception:
            self.running = False


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _list_joystick_devices() -> List[str]:
    return sorted(glob.glob("/dev/input/js*"))


def _probe_joystick(path: str) -> Tuple[bool, str]:
    try:
        with open(path, "rb", buffering=0):
            pass
        return True, "OK"
    except PermissionError:
        return False, f"Permission denied. Try: sudo chmod a+r {path}"
    except FileNotFoundError:
        return False, "Device not found."
    except OSError as exc:
        return False, str(exc)


def _load_calibration_axes() -> Dict[int, dict]:
    """Try to load calibration file; return empty dict on failure."""
    if not _SIMULATE_AVAILABLE or not CALIB_FILE.exists():
        return {}
    try:
        cal  = load_calibration(CALIB_FILE)
        return index_calibration_axes(cal)
    except Exception:
        return {}


# ─────────────────────────────────────────────────────────────────────────────
# Main application
# ─────────────────────────────────────────────────────────────────────────────

class App(tk.Tk):
    def __init__(self, initial_device: Optional[str] = None) -> None:
        super().__init__()
        self.title("RC Channel Function Mapping")
        self.configure(bg=BG)
        self.geometry("1240x860")
        self.minsize(1100, 740)

        self.initial_device   = initial_device
        self.selected_device: Optional[str] = initial_device
        self.reader:          Optional[_JsReader] = None

        self.channel_count   = 0
        self.channel_configs: List[ChannelConfig] = []
        self.current_channel_idx = 0

        # Calibration axes – loaded once; used for live output indicator
        self._calib_axes: Dict[int, dict] = _load_calibration_axes()

        # Sequence preview state for the current channel
        self._preview_seq_state: dict = {"order_buffer": []}

        self._detecting_axis    = False
        self._listening_buttons = False

        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self._show_start_screen()

    # ── lifecycle ──────────────────────────────────────────────────────────────
    def _on_close(self) -> None:
        if self.reader:
            self.reader.stop()
        self.destroy()

    def _clear(self) -> None:
        for w in self.winfo_children():
            w.destroy()

    # ── shared widget helpers ──────────────────────────────────────────────────
    def _header(self, title: str, sub: str, fg: str = ACCENT) -> None:
        tk.Label(self, text=title, font=FONT_TITLE, fg=fg, bg=BG).pack(fill="x", pady=(22, 2))
        tk.Label(self, text=sub,   font=FONT_HINT,  fg=SUBTLE, bg=BG).pack(pady=(0, 16))

    def _make_button(self, parent, text, cmd, *, primary=True, side=None) -> tk.Button:
        b = tk.Button(
            parent, text=text, font=FONT_BIG,
            fg=BG if primary else TEXT,
            bg=ACCENT if primary else BORDER,
            activebackground=ACCENT_L if primary else PANEL,
            activeforeground=BG if primary else TEXT,
            bd=0, padx=20, pady=10, cursor="hand2", command=cmd,
        )
        if side:
            b.pack(side=side, padx=8)
        return b

    # ─────────────────────────────────────────────────────────────────────────
    # START SCREEN
    # ─────────────────────────────────────────────────────────────────────────

    def _show_start_screen(self) -> None:
        self._clear()
        self._header("RC CHANNEL FUNCTION MAPPING",
                     "Select the controller and enter the number of channels")

        content = tk.Frame(self, bg=BG)
        content.pack(fill="both", expand=True, padx=28, pady=(0, 18))
        content.grid_columnconfigure(0, weight=1)
        content.grid_columnconfigure(1, weight=1)
        content.grid_rowconfigure(0, weight=1)

        left = tk.Frame(content, bg=PANEL, highlightthickness=1, highlightbackground=BORDER)
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 12))
        tk.Label(left, text="  JOYSTICK DEVICES", font=FONT_HINT, fg=SUBTLE, bg=PANEL, anchor="w"
                 ).pack(fill="x", padx=12, pady=(12, 4))
        self._device_list_body = tk.Frame(left, bg=PANEL)
        self._device_list_body.pack(fill="both", expand=True, padx=10, pady=(0, 10))
        self._dev_rows: Dict[str, tk.Frame] = {}
        self._render_device_list()

        right = tk.Frame(content, bg=PANEL, highlightthickness=1, highlightbackground=BORDER)
        right.grid(row=0, column=1, sticky="nsew", padx=(12, 0))
        tk.Label(right, text="  MAPPING SETUP", font=FONT_HINT, fg=SUBTLE, bg=PANEL, anchor="w"
                 ).pack(fill="x", padx=12, pady=(12, 12))

        form = tk.Frame(right, bg=PANEL)
        form.pack(fill="x", padx=18, pady=(0, 8))
        form.grid_columnconfigure(1, weight=1)

        tk.Label(form, text="Selected device", font=FONT_BODY, fg=TEXT, bg=PANEL, anchor="w"
                 ).grid(row=0, column=0, sticky="w", pady=8)
        self._sel_dev_var = tk.StringVar(value=self.selected_device or "None")
        tk.Label(form, textvariable=self._sel_dev_var, font=FONT_MONO, fg=TEXT, bg=PANEL, anchor="w"
                 ).grid(row=0, column=1, sticky="ew", pady=8)

        tk.Label(form, text="Channel count", font=FONT_BODY, fg=TEXT, bg=PANEL, anchor="w"
                 ).grid(row=1, column=0, sticky="w", pady=8)
        self._ch_count_var = tk.StringVar(value="0")
        ch_entry = tk.Entry(form, textvariable=self._ch_count_var, font=FONT_BODY,
                            bg=BG, fg=TEXT, insertbackground=TEXT, relief="flat", width=12)
        ch_entry.grid(row=1, column=1, sticky="w", pady=8)
        ch_entry.bind("<KeyRelease>", lambda _e: self._refresh_start_btn())

        hint = ("Enter the total number of channels." if (self.initial_device and
                _probe_joystick(self.initial_device)[0]) else
                "First select a joystick, then enter the channel count.\n"
                "Start becomes available only when both are valid.")
        tk.Label(right, text=hint, font=FONT_HINT, fg=SUBTLE, bg=PANEL, justify="left"
                 ).pack(anchor="w", padx=18, pady=(4, 8))

        self._dev_status_var = tk.StringVar(value="")
        self._dev_status_lbl = tk.Label(right, textvariable=self._dev_status_var,
                                        font=FONT_HINT, fg=SUBTLE, bg=PANEL, justify="left")
        self._dev_status_lbl.pack(anchor="w", padx=18, pady=(0, 12))

        bot = tk.Frame(right, bg=PANEL)
        bot.pack(fill="x", padx=18, pady=(4, 16))
        self._start_btn = self._make_button(bot, "START", self._start_mapping, primary=True, side="right")
        self._start_btn.configure(state="disabled")

        if self.selected_device:
            ok, msg = _probe_joystick(self.selected_device)
            self._dev_status_var.set(("✓  " if ok else "✗  ") + msg)
            self._dev_status_lbl.configure(fg=SUCCESS if ok else DANGER)

        self._refresh_start_btn()

    def _render_device_list(self) -> None:
        for w in self._device_list_body.winfo_children():
            w.destroy()
        self._dev_rows.clear()

        if self.initial_device is not None:
            row = tk.Frame(self._device_list_body, bg=PANEL)
            row.pack(fill="x", pady=2)
            tk.Label(row, text="◈", font=FONT_BODY, fg=TEXT, bg=PANEL).pack(side="left", padx=(8,0), pady=8)
            tk.Label(row, text=self.initial_device, font=FONT_BODY, fg=TEXT, bg=PANEL, anchor="w"
                     ).pack(side="left", padx=8, pady=8)
            self._dev_rows[self.initial_device] = row
            return

        devices = _list_joystick_devices()
        if not devices:
            tk.Label(self._device_list_body, text="No joystick devices found at /dev/input/js*",
                     font=FONT_BODY, fg=DANGER, bg=PANEL).pack(anchor="w", padx=8, pady=8)
            return
        for dev in devices:
            row = tk.Frame(self._device_list_body, bg=PANEL, cursor="hand2")
            row.pack(fill="x", pady=2)
            self._dev_rows[dev] = row
            dot  = tk.Label(row, text="◈", font=FONT_BODY, fg=SUBTLE, bg=PANEL)
            name = tk.Label(row, text=dev, font=FONT_BODY, fg=TEXT, bg=PANEL, anchor="w")
            dot.pack(side="left", padx=(8,0), pady=8)
            name.pack(side="left", padx=8, pady=8)
            for w in (row, dot, name):
                w.bind("<Button-1>", lambda _e, d=dev: self._select_device(d))
                w.bind("<Enter>",    lambda _e, r=row: self._hover_row(r, True))
                w.bind("<Leave>",    lambda _e, d=dev, r=row: self._hover_row(r, False, d))

    def _hover_row(self, row, entering, dev=None):
        bg = BORDER if entering else (ACCENT if dev == self.selected_device else PANEL)
        row.configure(bg=bg)
        for c in row.winfo_children():
            c.configure(bg=bg)

    def _select_device(self, dev: str) -> None:
        if self.initial_device is not None:
            return
        self.selected_device = dev
        self._sel_dev_var.set(dev)
        for name, row in self._dev_rows.items():
            bg = ACCENT if name == dev else PANEL
            row.configure(bg=bg)
            for c in row.winfo_children():
                c.configure(bg=bg)
        ok, msg = _probe_joystick(dev)
        self._dev_status_var.set(("✓  " if ok else "✗  ") + msg)
        self._dev_status_lbl.configure(fg=SUCCESS if ok else DANGER)
        self._refresh_start_btn()

    def _parsed_channel_count(self) -> int:
        try:
            v = int(self._ch_count_var.get().strip())
            return v if v > 0 else 0
        except ValueError:
            return 0

    def _refresh_start_btn(self) -> None:
        ok = (self._parsed_channel_count() > 0 and
              bool(self.selected_device) and
              _probe_joystick(self.selected_device)[0])
        self._start_btn.configure(state="normal" if ok else "disabled")

    def _start_mapping(self) -> None:
        if not self.selected_device:
            return
        n = self._parsed_channel_count()
        if n <= 0:
            return
        ok, msg = _probe_joystick(self.selected_device)
        if not ok:
            messagebox.showerror("Controller error", msg)
            return
        self.channel_count   = n
        self.channel_configs = [ChannelConfig() for _ in range(n)]
        self.current_channel_idx = 0
        if self.reader:
            self.reader.stop()
        self.reader = _JsReader(self.selected_device)
        self.reader.start()
        time.sleep(0.15)
        self._show_channel_screen()
        self.after(POLL_MS, self._poll_live_input)

    # ─────────────────────────────────────────────────────────────────────────
    # CHANNEL EDITOR SCREEN
    # ─────────────────────────────────────────────────────────────────────────

    def _show_channel_screen(self) -> None:
        self._clear()
        self._preview_seq_state = {"order_buffer": []}
        ch = self.current_channel_idx + 1
        self._header("CHANNEL PROFILE CREATION",
                     f"Controller: {self.selected_device}    |    Channel {ch} of {self.channel_count}")

        root = tk.Frame(self, bg=BG)
        root.pack(fill="both", expand=True, padx=24, pady=(0, 18))
        root.grid_columnconfigure(0, weight=3)
        root.grid_columnconfigure(1, weight=2)
        root.grid_rowconfigure(0, weight=1)

        left  = tk.Frame(root, bg=PANEL, highlightthickness=1, highlightbackground=BORDER)
        right = tk.Frame(root, bg=PANEL, highlightthickness=1, highlightbackground=BORDER)
        left.grid( row=0, column=0, sticky="nsew", padx=(0, 12))
        right.grid(row=0, column=1, sticky="nsew", padx=(12, 0))

        self._build_channel_editor(left)
        self._build_live_panel(right)
        self._load_channel_into_widgets()

    # ── editor (left panel) ────────────────────────────────────────────────────
    def _build_channel_editor(self, parent: tk.Widget) -> None:
        ch = self.current_channel_idx + 1
        tk.Label(parent, text=f"  CHANNEL {ch}", font=FONT_HINT, fg=SUBTLE, bg=PANEL, anchor="w"
                 ).pack(fill="x", padx=12, pady=(12, 6))

        # ── Output indicator ────────────────────────────────────────────────────
        out_frame = tk.Frame(parent, bg=ROW_ALT, highlightthickness=1, highlightbackground=BORDER)
        out_frame.pack(fill="x", padx=18, pady=(0, 10))
        out_top = tk.Frame(out_frame, bg=ROW_ALT)
        out_top.pack(fill="x", padx=12, pady=(8, 4))
        tk.Label(out_top, text="CHANNEL OUTPUT  (simulated)", font=FONT_HINT,
                 fg=SUBTLE, bg=ROW_ALT, anchor="w").pack(side="left")
        self._out_value_var = tk.StringVar(value="—")
        tk.Label(out_top, textvariable=self._out_value_var, font=FONT_BIG,
                 fg=ACCENT_L, bg=ROW_ALT, width=8, anchor="e").pack(side="right")
        self._out_canvas = tk.Canvas(out_frame, bg=ROW_ALT, height=14, highlightthickness=0)
        self._out_canvas.pack(fill="x", padx=12, pady=(0, 10))

        # ── Default value + channel type ────────────────────────────────────────
        form = tk.Frame(parent, bg=PANEL)
        form.pack(fill="x", padx=18, pady=(0, 6))
        form.grid_columnconfigure(1, weight=1)

        tk.Label(form, text="Default value", font=FONT_BODY, fg=TEXT, bg=PANEL, anchor="w"
                 ).grid(row=0, column=0, sticky="w", pady=8)
        self._default_var = tk.StringVar()
        tk.Entry(form, textvariable=self._default_var, font=FONT_BODY,
                 bg=BG, fg=TEXT, insertbackground=TEXT, relief="flat", width=14
                 ).grid(row=0, column=1, sticky="w", pady=8)

        tk.Label(form, text="Channel type", font=FONT_BODY, fg=TEXT, bg=PANEL, anchor="w"
                 ).grid(row=1, column=0, sticky="w", pady=8)
        self._input_type_var = tk.StringVar(value="axis")
        sel = tk.Frame(form, bg=PANEL)
        sel.grid(row=1, column=1, sticky="w", pady=8)
        for val in ("axis", "buttons"):
            tk.Radiobutton(sel, text=val, variable=self._input_type_var, value=val,
                           command=self._on_input_type_changed,
                           font=FONT_BODY, fg=TEXT, bg=PANEL, selectcolor=BG,
                           activebackground=PANEL, activeforeground=TEXT,
                           ).pack(side="left", padx=(0, 14))

        self._input_err_var = tk.StringVar(value="")
        tk.Label(parent, textvariable=self._input_err_var, font=FONT_HINT,
                 fg=WARNING, bg=PANEL, justify="left").pack(anchor="w", padx=18, pady=(0, 4))

        # ── Mode section holder (axis | buttons) ────────────────────────────────
        self._mode_holder = tk.Frame(parent, bg=PANEL)
        self._mode_holder.pack(fill="both", expand=True, padx=18, pady=(0, 6))

        # ── AXIS SECTION ────────────────────────────────────────────────────────
        self._axis_sec = tk.Frame(self._mode_holder, bg=PANEL)

        axis_top = tk.Frame(self._axis_sec, bg=PANEL)
        axis_top.pack(fill="x")
        tk.Label(axis_top, text="Axis mapping", font=FONT_BIG, fg=TEXT, bg=PANEL).pack(side="left")
        self._detect_btn = self._make_button(
            axis_top, "DETECT AXIS", self._detect_axis, primary=True)
        self._detect_btn.pack(side="right")

        rng = tk.Frame(self._axis_sec, bg=PANEL)
        rng.pack(fill="x", pady=(12, 4))
        tk.Label(rng, text="rc_min", font=FONT_BODY, fg=TEXT, bg=PANEL
                 ).grid(row=0, column=0, sticky="w", pady=4)
        self._axis_min_var = tk.StringVar()
        tk.Entry(rng, textvariable=self._axis_min_var, width=10, font=FONT_BODY,
                 bg=BG, fg=TEXT, insertbackground=TEXT, relief="flat"
                 ).grid(row=0, column=1, sticky="w", padx=(10, 24), pady=4)
        tk.Label(rng, text="rc_max", font=FONT_BODY, fg=TEXT, bg=PANEL
                 ).grid(row=0, column=2, sticky="w", pady=4)
        self._axis_max_var = tk.StringVar()
        tk.Entry(rng, textvariable=self._axis_max_var, width=10, font=FONT_BODY,
                 bg=BG, fg=TEXT, insertbackground=TEXT, relief="flat"
                 ).grid(row=0, column=3, sticky="w", padx=(10, 0), pady=4)

        # Invert toggle
        inv_row = tk.Frame(self._axis_sec, bg=PANEL)
        inv_row.pack(fill="x", pady=(6, 4))
        tk.Label(inv_row, text="Channel invert", font=FONT_BODY, fg=TEXT, bg=PANEL
                 ).pack(side="left")
        self._invert_var = tk.BooleanVar(value=False)
        self._inv_canvas = tk.Canvas(inv_row, width=52, height=26, bg=PANEL,
                                     highlightthickness=0, cursor="hand2")
        self._inv_canvas.pack(side="left", padx=(12, 6))
        self._inv_canvas.bind("<Button-1>", lambda _e: self._toggle_invert())
        self._inv_lbl = tk.Label(inv_row, text="OFF", font=FONT_HINT, fg=SUBTLE, bg=PANEL)
        self._inv_lbl.pack(side="left")
        tk.Label(inv_row,
                 text="  (applied after calibration inversion — flips final RC value)",
                 font=FONT_HINT, fg=SUBTLE, bg=PANEL).pack(side="left")
        self._draw_toggle(self._inv_canvas, False)

        self._axis_status_var = tk.StringVar(value="No axis assigned yet")
        tk.Label(self._axis_sec, textvariable=self._axis_status_var,
                 font=FONT_MONO, fg=TEXT, bg=PANEL, anchor="w", justify="left"
                 ).pack(fill="x", pady=(8, 4))
        self._axis_hint_var = tk.StringVar(
            value="Press DETECT AXIS, then move only the intended axis through its full range.")
        tk.Label(self._axis_sec, textvariable=self._axis_hint_var,
                 font=FONT_HINT, fg=SUBTLE, bg=PANEL, anchor="w", justify="left"
                 ).pack(fill="x")

        # ── BUTTONS SECTION ─────────────────────────────────────────────────────
        self._btn_sec = tk.Frame(self._mode_holder, bg=PANEL)

        btns_top = tk.Frame(self._btn_sec, bg=PANEL)
        btns_top.pack(fill="x")
        tk.Label(btns_top, text="Button mapping", font=FONT_BIG, fg=TEXT, bg=PANEL).pack(side="left")
        self._listen_btn = self._make_button(
            btns_top, "LISTEN FOR BUTTONS", self._toggle_listen, primary=True)
        self._listen_btn.pack(side="right")

        # Logic selector
        logic_row = tk.Frame(self._btn_sec, bg=PANEL)
        logic_row.pack(fill="x", pady=(10, 4))
        tk.Label(logic_row, text="Logic:", font=FONT_BODY, fg=TEXT, bg=PANEL
                 ).pack(side="left", padx=(0, 10))
        self._logic_var = tk.StringVar(value=LOGIC_NONE)
        for opt in LOGIC_OPTIONS:
            tk.Radiobutton(logic_row, text=opt, variable=self._logic_var, value=opt,
                           command=self._on_logic_changed,
                           font=FONT_BODY, fg=TEXT, bg=PANEL, selectcolor=BG,
                           activebackground=PANEL, activeforeground=TEXT,
                           ).pack(side="left", padx=(0, 16))

        self._btn_status_var = tk.StringVar(value="Listening is OFF")
        tk.Label(self._btn_sec, textvariable=self._btn_status_var,
                 font=FONT_MONO, fg=TEXT, bg=PANEL, anchor="w").pack(fill="x", pady=(6, 2))
        self._btn_hint_lbl = tk.Label(self._btn_sec, font=FONT_HINT, fg=SUBTLE, bg=PANEL,
                                      justify="left", anchor="w")
        self._btn_hint_lbl.pack(fill="x")

        # Condition met value row (AND / Sequence)
        self._cond_row = tk.Frame(self._btn_sec, bg=PANEL)
        tk.Label(self._cond_row, text="Condition met value:", font=FONT_BODY,
                 fg=TEXT, bg=PANEL).pack(side="left", padx=(0, 8))
        self._cond_met_var = tk.StringVar(value="2000")
        tk.Entry(self._cond_row, textvariable=self._cond_met_var, width=10,
                 font=FONT_BODY, bg=BG, fg=TEXT, insertbackground=TEXT, relief="flat"
                 ).pack(side="left")

        self._btn_rows_holder = tk.Frame(self._btn_sec, bg=PANEL)
        self._btn_rows_holder.pack(fill="both", expand=True, pady=(8, 0))

        # State for button rows
        self._btn_value_vars:  Dict[int, tk.StringVar] = {}
        self._btn_row_frames:  Dict[int, tk.Frame]     = {}
        self._buttons_ordered: List[int]               = []

        # ── Navigation ──────────────────────────────────────────────────────────
        nav = tk.Frame(parent, bg=PANEL)
        nav.pack(fill="x", padx=18, pady=(14, 16))
        self._prev_btn   = self._make_button(nav, "PREVIOUS",     self._go_prev,    primary=False, side="left")
        self._next_btn   = self._make_button(nav, "SAVE & NEXT",  self._save_next,  primary=True,  side="right")
        self._finish_btn = self._make_button(nav, "SAVE & FINISH",self._save_finish,primary=True,  side="right")

        if self.current_channel_idx == 0:
            self._prev_btn.configure(state="disabled")
        if self.current_channel_idx == self.channel_count - 1:
            self._next_btn.pack_forget()
        else:
            self._finish_btn.pack_forget()

    # ── toggle helpers ─────────────────────────────────────────────────────────
    def _draw_toggle(self, canvas: tk.Canvas, state: bool) -> None:
        canvas.delete("all")
        W, H   = 52, 26
        r      = H // 2
        track  = SUCCESS if state else BORDER
        canvas.create_oval(0, 0, H, H, fill=track, outline="")
        canvas.create_oval(W-H, 0, W, H, fill=track, outline="")
        canvas.create_rectangle(r, 0, W-r, H, fill=track, outline="")
        kx = W-H+4 if state else 4
        canvas.create_oval(kx, 4, kx+H-8, H-4, fill=TEXT, outline="")

    def _toggle_invert(self) -> None:
        s = not self._invert_var.get()
        self._invert_var.set(s)
        self._draw_toggle(self._inv_canvas, s)
        self._inv_lbl.configure(text="ON" if s else "OFF", fg=SUCCESS if s else SUBTLE)

    # ── mode switching ─────────────────────────────────────────────────────────
    def _on_input_type_changed(self) -> None:
        self._axis_sec.pack_forget()
        self._btn_sec.pack_forget()
        if self._input_type_var.get() == "axis":
            self._axis_sec.pack(fill="both", expand=True)
        else:
            self._btn_sec.pack(fill="both", expand=True)
            self._update_logic_ui()

    def _on_logic_changed(self) -> None:
        # Wipe existing button assignments when switching logic
        self._buttons_ordered = []
        self._btn_value_vars.clear()
        self._btn_row_frames.clear()
        self._update_logic_ui()
        logic = self._logic_var.get()
        if logic == LOGIC_NONE:
            self._rebuild_rows_none({})
        else:
            self._rebuild_rows_and_seq()

    def _update_logic_ui(self) -> None:
        logic = self._logic_var.get()
        hints = {
            LOGIC_NONE:
                "None: each button press sets its own RC value.",
            LOGIC_AND:
                "AND: all listed buttons must be held simultaneously → condition met value.",
            LOGIC_SEQUENCE:
                "Sequence: press & HOLD buttons in listed order → condition met value.\n"
                "Releases any button → immediately resets to default value.",
        }
        self._btn_hint_lbl.configure(text=hints.get(logic, ""))
        if logic in (LOGIC_AND, LOGIC_SEQUENCE):
            self._cond_row.pack(fill="x", pady=(6, 4))
        else:
            self._cond_row.pack_forget()

    # ── button row builders ────────────────────────────────────────────────────
    def _remove_placeholder(self) -> None:
        for c in self._btn_rows_holder.winfo_children():
            if isinstance(c, tk.Label) and c.cget("text") == "No buttons assigned yet":
                c.destroy()
                return

    def _rebuild_rows_none(self, mapping: Dict[int, int]) -> None:
        for w in self._btn_rows_holder.winfo_children():
            w.destroy()
        self._btn_value_vars.clear()
        self._btn_row_frames.clear()

        hdr = tk.Frame(self._btn_rows_holder, bg=BORDER)
        hdr.pack(fill="x", pady=(0, 4))
        for text, width in [("BUTTON", 18), ("RC VALUE WHEN PRESSED", 24), ("", 10)]:
            tk.Label(hdr, text=text, width=width, font=FONT_MONO,
                     fg=SUBTLE, bg=BORDER, pady=6).pack(side="left", padx=4)

        if not mapping:
            tk.Label(self._btn_rows_holder, text="No buttons assigned yet",
                     font=FONT_HINT, fg=SUBTLE, bg=PANEL, anchor="w").pack(fill="x", pady=10)
            return
        for bid in sorted(mapping):
            self._add_row_none(bid, mapping[bid])

    def _add_row_none(self, bid: int, value: int) -> None:
        self._remove_placeholder()
        row = tk.Frame(self._btn_rows_holder, bg=ROW_ALT,
                       highlightthickness=1, highlightbackground=BORDER)
        row.pack(fill="x", pady=3)
        self._btn_row_frames[bid] = row
        tk.Label(row, text=f"button_{bid}", width=18, font=FONT_MONO,
                 fg=TEXT, bg=ROW_ALT, anchor="w").pack(side="left", padx=8, pady=8)
        var = tk.StringVar(value=str(value))
        self._btn_value_vars[bid] = var
        tk.Entry(row, textvariable=var, width=18, font=FONT_BODY,
                 bg=BG, fg=TEXT, insertbackground=TEXT, relief="flat"
                 ).pack(side="left", padx=8, pady=8)
        tk.Button(row, text="DELETE", font=FONT_HINT, fg=TEXT, bg=BORDER,
                  activebackground=PANEL, activeforeground=TEXT, bd=0,
                  padx=12, pady=6, cursor="hand2",
                  command=lambda b=bid: self._delete_row_none(b)
                  ).pack(side="right", padx=8, pady=8)

    def _delete_row_none(self, bid: int) -> None:
        frame = self._btn_row_frames.pop(bid, None)
        if frame is not None:
            frame.destroy()
        self._btn_value_vars.pop(bid, None)
        if not self._btn_row_frames:
            self._rebuild_rows_none({})

    def _rebuild_rows_and_seq(self) -> None:
        for w in self._btn_rows_holder.winfo_children():
            w.destroy()
        self._btn_row_frames.clear()

        seq_mode = (self._logic_var.get() == LOGIC_SEQUENCE)
        hdr = tk.Frame(self._btn_rows_holder, bg=BORDER)
        hdr.pack(fill="x", pady=(0, 4))
        for text, width in [("ORDER" if seq_mode else "#", 8), ("BUTTON", 18), ("", 10)]:
            tk.Label(hdr, text=text, width=width, font=FONT_MONO,
                     fg=SUBTLE, bg=BORDER, pady=6).pack(side="left", padx=4)

        if not self._buttons_ordered:
            tk.Label(self._btn_rows_holder, text="No buttons assigned yet",
                     font=FONT_HINT, fg=SUBTLE, bg=PANEL, anchor="w").pack(fill="x", pady=10)
            return
        for pos, bid in enumerate(self._buttons_ordered, start=1):
            self._add_row_and_seq(bid, pos)

    def _add_row_and_seq(self, bid: int, pos: int) -> None:
        self._remove_placeholder()
        seq_mode = (self._logic_var.get() == LOGIC_SEQUENCE)
        row = tk.Frame(self._btn_rows_holder, bg=ROW_ALT,
                       highlightthickness=1, highlightbackground=BORDER)
        row.pack(fill="x", pady=3)
        self._btn_row_frames[bid] = row
        tk.Label(row, text=str(pos) if seq_mode else "·", width=8,
                 font=FONT_MONO, fg=SUBTLE, bg=ROW_ALT, anchor="center"
                 ).pack(side="left", padx=8, pady=8)
        tk.Label(row, text=f"button_{bid}", width=18, font=FONT_MONO,
                 fg=TEXT, bg=ROW_ALT, anchor="w").pack(side="left", padx=8, pady=8)
        tk.Button(row, text="DELETE", font=FONT_HINT, fg=TEXT, bg=BORDER,
                  activebackground=PANEL, activeforeground=TEXT, bd=0,
                  padx=12, pady=6, cursor="hand2",
                  command=lambda b=bid: self._delete_row_and_seq(b)
                  ).pack(side="right", padx=8, pady=8)

    def _delete_row_and_seq(self, bid: int) -> None:
        if bid in self._btn_row_frames:
            self._btn_row_frames.pop(bid).destroy()
        if bid in self._buttons_ordered:
            self._buttons_ordered.remove(bid)
        self._rebuild_rows_and_seq()

    # ── live panel (right panel) ───────────────────────────────────────────────
    def _build_live_panel(self, parent: tk.Widget) -> None:
        tk.Label(parent, text="  LIVE INPUT MONITOR", font=FONT_HINT,
                 fg=SUBTLE, bg=PANEL, anchor="w").pack(fill="x", padx=12, pady=(12, 8))
        self._live_info_var = tk.StringVar(value="Waiting for controller activity…")
        tk.Label(parent, textvariable=self._live_info_var, font=FONT_HINT,
                 fg=SUBTLE, bg=PANEL, justify="left", anchor="w"
                 ).pack(fill="x", padx=18, pady=(0, 10))

        axis_card = tk.Frame(parent, bg=ROW_ALT, highlightthickness=1, highlightbackground=BORDER)
        axis_card.pack(fill="both", expand=True, padx=18, pady=(0, 10))
        tk.Label(axis_card, text="AXES", font=FONT_BIG, fg=TEXT, bg=ROW_ALT
                 ).pack(anchor="w", padx=12, pady=(10, 6))
        self._axis_live_var = tk.StringVar(value="")
        tk.Label(axis_card, textvariable=self._axis_live_var, font=FONT_MONO,
                 fg=TEXT, bg=ROW_ALT, justify="left", anchor="nw"
                 ).pack(fill="both", expand=True, padx=12, pady=(0, 12))

        btn_card = tk.Frame(parent, bg=ROW_ALT, highlightthickness=1, highlightbackground=BORDER)
        btn_card.pack(fill="both", expand=True, padx=18, pady=(0, 16))
        tk.Label(btn_card, text="BUTTONS", font=FONT_BIG, fg=TEXT, bg=ROW_ALT
                 ).pack(anchor="w", padx=12, pady=(10, 6))
        self._btn_live_var = tk.StringVar(value="")
        tk.Label(btn_card, textvariable=self._btn_live_var, font=FONT_MONO,
                 fg=TEXT, bg=ROW_ALT, justify="left", anchor="nw"
                 ).pack(fill="both", expand=True, padx=12, pady=(0, 12))

    # ── load channel into UI ────────────────────────────────────────────────────
    def _load_channel_into_widgets(self) -> None:
        cfg = self.channel_configs[self.current_channel_idx]
        self._detecting_axis    = False
        self._listening_buttons = False

        self._default_var.set(str(cfg.default_value))
        self._axis_min_var.set(str(cfg.axis_min))
        self._axis_max_var.set(str(cfg.axis_max))
        self._input_type_var.set(cfg.input_type)
        self._logic_var.set(cfg.logic)
        self._cond_met_var.set(str(cfg.condition_met_value))

        self._invert_var.set(cfg.invert)
        self._draw_toggle(self._inv_canvas, cfg.invert)
        self._inv_lbl.configure(text="ON" if cfg.invert else "OFF",
                                fg=SUCCESS if cfg.invert else SUBTLE)

        self._axis_status_var.set(
            f"Assigned axis_number: {cfg.axis_number}"
            if cfg.axis_number is not None else "No axis assigned yet"
        )
        self._axis_hint_var.set(
            "Press DETECT AXIS, then move only the intended axis through its full range.")
        self._btn_status_var.set("Listening is OFF")
        self._listen_btn.configure(text="LISTEN FOR BUTTONS")
        self._input_err_var.set("")

        # Rebuild button rows
        self._buttons_ordered = list(cfg.buttons_list)
        if cfg.logic == LOGIC_NONE:
            self._rebuild_rows_none(cfg.buttons_map)
        else:
            self._rebuild_rows_and_seq()

        self._on_input_type_changed()

    # ── poll loop ───────────────────────────────────────────────────────────────
    def _poll_live_input(self) -> None:
        if not self.reader or not self.winfo_exists():
            return

        axes     = self.reader.snapshot_axes()
        buttons  = self.reader.snapshot_buttons()
        btn_evts = self.reader.pop_button_events()

        # Update live monitor labels
        self._axis_live_var.set(
            "\n".join(f"axis_{k}: {v}" for k, v in sorted(axes.items())) or "No axis data")
        self._btn_live_var.set(
            "\n".join(f"button_{k}: {v}" for k, v in sorted(buttons.items())) or "No button data")
        self._live_info_var.set("Live values update automatically.")

        # Listen mode: add newly pressed buttons
        if self._listening_buttons:
            logic = self._logic_var.get()
            for bid, val in btn_evts:
                if val != 1:
                    continue
                if logic == LOGIC_NONE:
                    if bid not in self._btn_value_vars:
                        try:
                            dv = int(self._default_var.get())
                        except ValueError:
                            dv = 1500
                        self._add_row_none(bid, dv)
                        self._btn_status_var.set(f"Added button_{bid}")
                else:
                    if bid not in self._buttons_ordered:
                        self._buttons_ordered.append(bid)
                        self._add_row_and_seq(bid, len(self._buttons_ordered))
                        self._btn_status_var.set(f"Added button_{bid} (position {len(self._buttons_ordered)})")

        # Update sequence preview state (uses button press+release events)
        inp   = self._input_type_var.get()
        logic = self._logic_var.get()
        if inp == "buttons" and logic == LOGIC_SEQUENCE and _SIMULATE_AVAILABLE:
            update_sequence_state(
                self._preview_seq_state,
                btn_evts,
                self._buttons_ordered,
            )

        # Update output indicator
        self._update_output_indicator(axes, buttons)
        self.after(POLL_MS, self._poll_live_input)

    # ── output indicator ────────────────────────────────────────────────────────
    def _build_preview_channel_dict(
        self,
        axes:    Dict[int, int],
        buttons: Dict[int, int],
    ) -> Optional[dict]:
        """
        Build a channel dict in the format expected by compute_channel_output().
        Returns None if the channel is not yet fully configured.
        """
        inp = self._input_type_var.get()
        try:
            default = int(self._default_var.get())
        except ValueError:
            return None

        ch: dict = {"input_type": inp, "default_value": default}

        if inp == "axis":
            cfg = self.channel_configs[self.current_channel_idx]
            if cfg.axis_number is None:
                return None
            if cfg.axis_number not in self._calib_axes:
                return None   # calibration file not loaded or axis not found
            try:
                rc_min = int(self._axis_min_var.get())
                rc_max = int(self._axis_max_var.get())
                if rc_min >= rc_max:
                    return None
            except ValueError:
                return None
            ch["axis_number"] = cfg.axis_number
            ch["rc_min"]      = rc_min
            ch["rc_max"]      = rc_max
            ch["calibration"] = self._calib_axes[cfg.axis_number]
            ch["invert"]      = self._invert_var.get()
            return ch

        # buttons
        logic = self._logic_var.get()
        ch["logic"] = logic
        if logic == LOGIC_NONE:
            bmap: Dict[int, int] = {}
            for bid, var in self._btn_value_vars.items():
                try:
                    bmap[bid] = int(var.get())
                except ValueError:
                    pass
            ch["buttons"] = bmap
            return ch
        else:
            try:
                cond = int(self._cond_met_var.get())
            except ValueError:
                cond = 2000
            ch["buttons"]             = list(self._buttons_ordered)
            ch["condition_met_value"] = cond
            return ch

    def _update_output_indicator(
        self,
        axes:    Dict[int, int],
        buttons: Dict[int, int],
    ) -> None:
        if not _SIMULATE_AVAILABLE:
            self._out_value_var.set("n/a")
            return

        ch = self._build_preview_channel_dict(axes, buttons)
        if ch is None:
            self._out_value_var.set("—")
            self._draw_output_bar(None, 1000, 2000)
            return

        seq_s = (self._preview_seq_state
                 if ch.get("logic") == LOGIC_SEQUENCE else None)
        output = compute_channel_output(ch, axes, buttons, seq_s)
        self._out_value_var.set(str(output))

        rc_min = ch.get("rc_min", 1000)
        rc_max = ch.get("rc_max", 2000)
        if ch["input_type"] == "buttons":
            # Infer range from all possible output values for the bar
            vals = []
            if ch["logic"] == LOGIC_NONE:
                vals = list(ch["buttons"].values()) + [ch["default_value"]]
            else:
                vals = [ch.get("condition_met_value", 2000), ch["default_value"]]
            rc_min = min(vals) if vals else 1000
            rc_max = max(vals) if vals else 2000
            if rc_min == rc_max:
                rc_min -= 500
                rc_max += 500
        self._draw_output_bar(output, rc_min, rc_max)

    def _draw_output_bar(self, value: Optional[int], rc_min: int, rc_max: int) -> None:
        c = self._out_canvas
        c.update_idletasks()
        W = c.winfo_width() or 300
        H = 14
        c.delete("all")
        c.create_rectangle(0, 0, W, H, fill=BORDER, outline="")
        if value is None:
            return
        lo, hi = min(rc_min, rc_max), max(rc_min, rc_max)
        span   = hi - lo
        ratio  = 0.5 if span == 0 else max(0.0, min(1.0, (value - lo) / span))
        mid    = (rc_min + rc_max) / 2.0
        spread = (hi - lo) * 0.05
        fill   = ACCENT if abs(value - mid) <= spread else (SUCCESS if value > mid else WARNING)
        c.create_rectangle(0, 0, int(W * ratio), H, fill=fill, outline="")
        cx = int(W * 0.5)
        c.create_rectangle(cx - 1, 0, cx + 1, H, fill=SUBTLE, outline="")

    # ── axis detection ─────────────────────────────────────────────────────────
    def _toggle_listen(self) -> None:
        self._listening_buttons = not self._listening_buttons
        if self._listening_buttons:
            if self.reader:
                self.reader.pop_button_events()
            self._btn_status_var.set("Listening is ON — press buttons now")
            self._listen_btn.configure(text="STOP LISTENING")
        else:
            self._btn_status_var.set("Listening is OFF")
            self._listen_btn.configure(text="LISTEN FOR BUTTONS")

    def _detect_axis(self) -> None:
        if not self.reader or self._detecting_axis:
            return
        self._input_err_var.set("")
        self._detecting_axis = True
        self._detect_btn.configure(state="disabled")
        self._axis_status_var.set("Detecting… move ONLY the intended axis through its full range")
        self._axis_hint_var.set("Recording axis movement now.")

        baseline = self.reader.snapshot_axes()
        samples: List[Dict[int, int]] = []
        start   = time.monotonic()

        def collect() -> None:
            if not self.reader:
                self._finish_detect(None, 0.0)
                return
            samples.append(self.reader.snapshot_axes())
            if time.monotonic() - start < AXIS_DETECTION_SECONDS:
                self.after(POLL_MS, collect)
                return
            axis, peak = self._find_moved_axis(baseline, samples)
            self._finish_detect(axis, peak)

        collect()

    def _find_moved_axis(
        self,
        baseline: Dict[int, int],
        samples:  List[Dict[int, int]],
    ) -> Tuple[Optional[int], float]:
        if not samples:
            return None, 0.0
        axes: set = set(baseline.keys())
        for s in samples:
            axes.update(s.keys())
        best_ax:   Optional[int] = None
        best_peak: float         = 0.0
        for ax in sorted(axes):
            center = baseline.get(ax)
            if center is None:
                center = next((s[ax] for s in samples if ax in s), 0)
            peak = max(abs(s.get(ax, center) - center) for s in samples)
            if peak > best_peak:
                best_peak = peak
                best_ax   = ax
        return best_ax, best_peak

    def _finish_detect(self, axis: Optional[int], peak: float) -> None:
        self._detecting_axis = False
        self._detect_btn.configure(state="normal")
        if axis is None or peak < MIN_AXIS_MOVE_THRESHOLD:
            self._axis_status_var.set(
                f"Movement too small ({int(peak)})" if axis is not None
                else "Could not identify a moved axis"
            )
            self._axis_hint_var.set("Retry and move the control through a larger range.")
            return
        self.channel_configs[self.current_channel_idx].axis_number = axis
        self._axis_status_var.set(f"Assigned axis_number: {axis}")
        self._axis_hint_var.set(f"Detected successfully. Peak movement: {int(peak)}")

    # ── gather / validate ──────────────────────────────────────────────────────
    def _read_default(self, required=True) -> Tuple[Optional[int], Optional[str]]:
        raw = self._default_var.get().strip()
        if not raw:
            return (None, None) if not required else (None, "Default value is required.")
        try:
            return int(raw), None
        except ValueError:
            return None, "Default value must be an integer."

    def _read_axis_range(self, required=True) -> Tuple[Optional[int], Optional[int], Optional[str]]:
        r_min = self._axis_min_var.get().strip()
        r_max = self._axis_max_var.get().strip()
        if not r_min or not r_max:
            return (None, None, None) if not required else (None, None, "Axis min and max are required.")
        try:
            vmin, vmax = int(r_min), int(r_max)
        except ValueError:
            return None, None, "Axis min and max must be integers."
        if vmin >= vmax:
            return None, None, "rc_min must be less than rc_max."
        return vmin, vmax, None

    def _gather_buttons(self, required=True):
        """Returns (data, error). data type depends on logic."""
        logic = self._logic_var.get()
        if logic == LOGIC_NONE:
            m: Dict[int, int] = {}
            for bid, var in sorted(self._btn_value_vars.items()):
                raw = var.get().strip()
                if not raw:
                    return None, (f"button_{bid} is missing its RC value." if required else None)
                try:
                    m[bid] = int(raw)
                except ValueError:
                    return None, f"button_{bid}: RC value must be an integer."
            if required and not m:
                return None, "At least one button must be assigned."
            return m, None
        else:
            if required and not self._buttons_ordered:
                return None, "At least one button must be assigned."
            try:
                cond = int(self._cond_met_var.get().strip())
            except ValueError:
                if required:
                    return None, "Condition met value must be an integer."
                cond = 2000
            return (list(self._buttons_ordered), cond), None

    def _save_current_channel(self, validate=True) -> bool:
        default, err = self._read_default(required=validate)
        if err:
            self._input_err_var.set(err)
            return False

        cfg = self.channel_configs[self.current_channel_idx]
        if default is not None:
            cfg.default_value = default
        cfg.input_type = self._input_type_var.get()

        if cfg.input_type == "axis":
            vmin, vmax, err = self._read_axis_range(required=validate)
            if err:
                self._input_err_var.set(err)
                return False
            if vmin is not None:
                cfg.axis_min = vmin
            if vmax is not None:
                cfg.axis_max = vmax
            if validate and cfg.axis_number is None:
                self._input_err_var.set("Axis channel requires a detected axis_number.")
                return False
            cfg.invert       = self._invert_var.get()
            cfg.buttons_map  = {}
            cfg.buttons_list = []
        else:
            cfg.logic = self._logic_var.get()
            data, err = self._gather_buttons(required=validate)
            if err:
                self._input_err_var.set(err)
                return False
            if data is not None:
                if cfg.logic == LOGIC_NONE:
                    cfg.buttons_map  = data
                    cfg.buttons_list = []
                else:
                    bl, cond              = data
                    cfg.buttons_list      = bl
                    cfg.buttons_map       = {}
                    cfg.condition_met_value = cond
            cfg.axis_number = None

        self._input_err_var.set("")
        return True

    # ── navigation ─────────────────────────────────────────────────────────────
    def _go_prev(self) -> None:
        if not self._save_current_channel(validate=False):
            return
        if self.current_channel_idx <= 0:
            return
        self.current_channel_idx -= 1
        self._show_channel_screen()

    def _save_next(self) -> None:
        if not self._save_current_channel():
            return
        if self.current_channel_idx >= self.channel_count - 1:
            return
        self.current_channel_idx += 1
        self._show_channel_screen()

    def _save_finish(self) -> None:
        if not self._save_current_channel():
            return
        self._write_yaml()
        self._show_done_screen()

    # ── YAML writer ────────────────────────────────────────────────────────────
    def _write_yaml(self) -> None:
        lines: List[str] = []
        for i, cfg in enumerate(self.channel_configs, start=1):
            lines.append(f"channel_{i}:")
            lines.append(f"  default_value: {cfg.default_value}")
            lines.append(f"  input_type: {cfg.input_type}")
            if cfg.input_type == "axis":
                lines.append(f"  rc_min: {cfg.axis_min}")
                lines.append(f"  rc_max: {cfg.axis_max}")
                lines.append(f"  axis_number: {0 if cfg.axis_number is None else cfg.axis_number}")
                lines.append(f"  invert: {cfg.invert}")
            else:
                lines.append(f"  logic: {cfg.logic}")
                if cfg.logic == LOGIC_NONE:
                    lines.append("  buttons:")
                    for bid, val in sorted(cfg.buttons_map.items()):
                        lines.append(f"    {bid}: {val}")
                else:
                    btn_yaml = "[" + ", ".join(str(b) for b in cfg.buttons_list) + "]"
                    lines.append(f"  buttons: {btn_yaml}")
                    lines.append(f"  condition_met_value: {cfg.condition_met_value}")
        Path(OUTPUT_FILE).write_text("\n".join(lines) + "\n", encoding="utf-8")

    # ── done screen ────────────────────────────────────────────────────────────
    def _show_done_screen(self) -> None:
        self._clear()
        self._header("CHANNEL PROFILE SAVED",
                     f"Saved → {Path(OUTPUT_FILE).resolve()}", fg=SUCCESS)

        card = tk.Frame(self, bg=PANEL, highlightthickness=1, highlightbackground=BORDER)
        card.pack(fill="x", padx=26, pady=12)
        tk.Label(card, text="SUMMARY", font=FONT_BIG, fg=TEXT, bg=PANEL
                 ).pack(anchor="w", padx=16, pady=(14, 8))

        table = tk.Frame(card, bg=PANEL)
        table.pack(fill="x", padx=16, pady=(0, 14))
        headers = ["CHANNEL", "DEFAULT", "TYPE", "LOGIC / DETAIL"]
        widths  = [12, 10, 10, 56]
        for col, (h, w) in enumerate(zip(headers, widths)):
            tk.Label(table, text=h, width=w, font=FONT_MONO,
                     fg=SUBTLE, bg=BORDER, pady=6
                     ).grid(row=0, column=col, sticky="nsew", padx=1, pady=1)

        for row_i, cfg in enumerate(self.channel_configs, start=1):
            if cfg.input_type == "axis":
                detail = (f"axis={cfg.axis_number}  rc={cfg.axis_min}..{cfg.axis_max}"
                          f"  invert={cfg.invert}")
                logic  = "—"
            elif cfg.logic == LOGIC_NONE:
                detail = ", ".join(f"btn{b}→{v}" for b, v in sorted(cfg.buttons_map.items()))
                logic  = LOGIC_NONE
            else:
                detail = f"buttons={cfg.buttons_list}  cond={cfg.condition_met_value}"
                logic  = cfg.logic
            for col_i, (val, w) in enumerate(zip(
                [f"channel_{row_i}", str(cfg.default_value), cfg.input_type, detail if logic == "—" else f"[{logic}] {detail}"],
                widths,
            )):
                tk.Label(table, text=val, width=w, font=FONT_MONO,
                         fg=TEXT, bg=PANEL, pady=5, anchor="w"
                         ).grid(row=row_i, column=col_i, sticky="nsew", padx=1, pady=1)

        act = tk.Frame(self, bg=BG)
        act.pack(pady=(10, 22))
        self._make_button(act, "CLOSE", self._on_close, primary=True).pack()


if __name__ == "__main__":
    if sys.platform != "linux":
        print("This tool is Linux-only.")
        sys.exit(1)
    if len(sys.argv) > 2:
        print("Usage:")
        print("  python3 rc_controller_channel_function_mapping.py")
        print("  python3 rc_controller_channel_function_mapping.py /dev/input/js0")
        sys.exit(1)
    App(sys.argv[1] if len(sys.argv) > 1 else None).mainloop()