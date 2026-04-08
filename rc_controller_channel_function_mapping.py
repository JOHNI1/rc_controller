#!/usr/bin/env python3
"""
RC channel function mapping tool.

What it does:
1) Lets the user select a local joystick device.
2) Lets the user choose how many channels will be configured.
3) Walks channel-by-channel through a mapping UI.
4) Supports channel input type = axis or buttons.
5) For axis channels, detects the moved axis number.
6) For button channels, listens for pressed buttons and lets the user assign a
   channel value for each button.
7) Saves the resulting mapping to YAML.

Output:
    rc_controller_channel_function_mapping.yaml
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

BASE_DIR = Path(__file__).resolve().parent
OUTPUT_FILE = BASE_DIR / "rc_controller_channel_function_mapping.yaml"

JS_EVENT_BUTTON = 0x01
JS_EVENT_AXIS = 0x02
JS_EVENT_INIT = 0x80

MIN_AXIS_MOVE_THRESHOLD = 4000
AXIS_DETECTION_SECONDS = 2.5
POLL_MS = 40

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
ROW_ALT = "#121826"

FONT_TITLE = ("Courier New", 16, "bold")
FONT_BIG = ("Courier New", 13, "bold")
FONT_BODY = ("Courier New", 11)
FONT_HINT = ("Courier New", 9)
FONT_MONO = ("Courier New", 10)


@dataclass
class ChannelConfig:
    default_value: int = 1500
    input_type: str = "axis"
    axis_number: Optional[int] = None
    axis_min: int = 1000
    axis_max: int = 2000
    buttons: Dict[int, int] = field(default_factory=dict)


class JsReader(threading.Thread):
    def __init__(self, path: str) -> None:
        super().__init__(daemon=True)
        self.path = path
        self.running = True
        self._lock = threading.Lock()
        self._axes: Dict[int, int] = {}
        self._buttons: Dict[int, int] = {}
        self._button_press_events: List[int] = []

    def snapshot_axes(self) -> Dict[int, int]:
        with self._lock:
            return dict(self._axes)

    def snapshot_buttons(self) -> Dict[int, int]:
        with self._lock:
            return dict(self._buttons)

    def pop_button_press_events(self) -> List[int]:
        with self._lock:
            events = list(self._button_press_events)
            self._button_press_events.clear()
            return events

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
                            self._buttons[number] = value
                            if value:
                                self._button_press_events.append(number)
        except Exception:
            self.running = False


def list_joystick_devices() -> List[str]:
    return sorted(glob.glob("/dev/input/js*"))


def probe_joystick(path: str) -> Tuple[bool, str]:
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


class App(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("RC Channel Function Mapping")
        self.configure(bg=BG)
        self.geometry("1120x760")
        self.minsize(1040, 700)

        self.selected_device: Optional[str] = None
        self.reader: Optional[JsReader] = None
        self.channel_count = 0
        self.channel_configs: List[ChannelConfig] = []
        self.current_channel_idx = 0

        self._detecting_axis = False
        self._listening_buttons = False

        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self._show_start_screen()

    def _on_close(self) -> None:
        if self.reader:
            self.reader.stop()
        self.destroy()

    def _clear(self) -> None:
        for widget in self.winfo_children():
            widget.destroy()

    def _header(self, title: str, sub: str, title_fg: str = ACCENT) -> None:
        tk.Label(self, text=title, font=FONT_TITLE, fg=title_fg, bg=BG).pack(fill="x", pady=(22, 2))
        tk.Label(self, text=sub, font=FONT_HINT, fg=SUBTLE, bg=BG).pack(pady=(0, 16))

    def _card(self, parent: tk.Widget, padx: int = 20, pady: int = 12) -> tk.Frame:
        frame = tk.Frame(parent, bg=PANEL, highlightthickness=1, highlightbackground=BORDER)
        frame.pack(fill="x", padx=padx, pady=pady)
        return frame

    def _make_button(self, parent: tk.Widget, text: str, command, *, primary: bool = True, side: Optional[str] = None) -> tk.Button:
        button = tk.Button(
            parent,
            text=text,
            font=FONT_BIG,
            fg=BG if primary else TEXT,
            bg=ACCENT if primary else BORDER,
            activebackground=ACCENT_L if primary else PANEL,
            activeforeground=BG if primary else TEXT,
            bd=0,
            padx=20,
            pady=10,
            cursor="hand2",
            command=command,
        )
        if side:
            button.pack(side=side, padx=8)
        return button

    def _show_start_screen(self) -> None:
        self._clear()
        self._header("RC CHANNEL FUNCTION MAPPING", "Select the controller and the number of channels to define")

        content = tk.Frame(self, bg=BG)
        content.pack(fill="both", expand=True, padx=28, pady=(0, 18))
        content.grid_columnconfigure(0, weight=1)
        content.grid_columnconfigure(1, weight=1)
        content.grid_rowconfigure(0, weight=1)

        left = tk.Frame(content, bg=PANEL, highlightthickness=1, highlightbackground=BORDER)
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 12))

        tk.Label(left, text="  AVAILABLE JOYSTICKS", font=FONT_HINT, fg=SUBTLE, bg=PANEL, anchor="w").pack(fill="x", padx=12, pady=(12, 4))
        self._device_list_body = tk.Frame(left, bg=PANEL)
        self._device_list_body.pack(fill="both", expand=True, padx=10, pady=(0, 10))
        self._dev_rows: Dict[str, tk.Frame] = {}
        self._render_device_list()

        right = tk.Frame(content, bg=PANEL, highlightthickness=1, highlightbackground=BORDER)
        right.grid(row=0, column=1, sticky="nsew", padx=(12, 0))

        tk.Label(right, text="  MAPPING SETUP", font=FONT_HINT, fg=SUBTLE, bg=PANEL, anchor="w").pack(fill="x", padx=12, pady=(12, 12))

        form = tk.Frame(right, bg=PANEL)
        form.pack(fill="x", padx=18, pady=(0, 8))
        form.grid_columnconfigure(1, weight=1)

        tk.Label(form, text="Selected device", font=FONT_BODY, fg=TEXT, bg=PANEL, anchor="w").grid(row=0, column=0, sticky="w", pady=8)
        self._selected_device_var = tk.StringVar(value="None")
        tk.Label(form, textvariable=self._selected_device_var, font=FONT_MONO, fg=TEXT, bg=PANEL, anchor="w").grid(row=0, column=1, sticky="ew", pady=8)

        tk.Label(form, text="Channel count", font=FONT_BODY, fg=TEXT, bg=PANEL, anchor="w").grid(row=1, column=0, sticky="w", pady=8)
        self._channel_count_var = tk.StringVar(value="0")
        channel_count_entry = tk.Entry(form, textvariable=self._channel_count_var, font=FONT_BODY, bg=BG, fg=TEXT, insertbackground=TEXT, relief="flat", width=12)
        channel_count_entry.grid(row=1, column=1, sticky="w", pady=8)
        channel_count_entry.bind("<KeyRelease>", lambda _e: self._refresh_start_button_state())

        hint_text = (
            "First select the joystick. Then enter the total number of channels you want in the profile.\n"
            "The Start button becomes available only when both are valid."
        )
        tk.Label(right, text=hint_text, font=FONT_HINT, fg=SUBTLE, bg=PANEL, justify="left").pack(anchor="w", padx=18, pady=(6, 10))

        self._device_status_var = tk.StringVar(value="")
        self._device_status_lbl = tk.Label(right, textvariable=self._device_status_var, font=FONT_HINT, fg=SUBTLE, bg=PANEL, justify="left")
        self._device_status_lbl.pack(anchor="w", padx=18, pady=(0, 12))

        bottom = tk.Frame(right, bg=PANEL)
        bottom.pack(fill="x", padx=18, pady=(4, 16))
        self._refresh_devices_btn = self._make_button(bottom, "REFRESH DEVICES", self._refresh_devices, primary=False, side="left")
        self._start_btn = self._make_button(bottom, "START", self._start_mapping, primary=True, side="right")
        self._start_btn.configure(state="disabled")

    def _render_device_list(self) -> None:
        for widget in self._device_list_body.winfo_children():
            widget.destroy()
        self._dev_rows.clear()

        devices = list_joystick_devices()
        if not devices:
            tk.Label(self._device_list_body, text="No joystick devices found at /dev/input/js*", font=FONT_BODY, fg=DANGER, bg=PANEL).pack(anchor="w", padx=8, pady=8)
            return

        for dev in devices:
            row = tk.Frame(self._device_list_body, bg=PANEL, cursor="hand2")
            row.pack(fill="x", pady=2)
            self._dev_rows[dev] = row

            dot = tk.Label(row, text="◈", font=FONT_BODY, fg=SUBTLE, bg=PANEL)
            name = tk.Label(row, text=dev, font=FONT_BODY, fg=TEXT, bg=PANEL, anchor="w")
            dot.pack(side="left", padx=(8, 0), pady=8)
            name.pack(side="left", padx=8, pady=8)

            for widget in (row, dot, name):
                widget.bind("<Button-1>", lambda _e, d=dev: self._select_device(d))
                widget.bind("<Enter>", lambda _e, r=row: self._hover_row(r, True))
                widget.bind("<Leave>", lambda _e, d=dev, r=row: self._hover_row(r, False, d))

    def _hover_row(self, row: tk.Frame, entering: bool, dev: Optional[str] = None) -> None:
        if entering:
            bg = BORDER
        else:
            bg = ACCENT if dev == self.selected_device else PANEL
        row.configure(bg=bg)
        for child in row.winfo_children():
            child.configure(bg=bg)

    def _refresh_devices(self) -> None:
        self._render_device_list()
        if self.selected_device and self.selected_device not in self._dev_rows:
            self.selected_device = None
            self._selected_device_var.set("None")
        self._refresh_start_button_state()

    def _select_device(self, dev: str) -> None:
        self.selected_device = dev
        self._selected_device_var.set(dev)
        for name, row in self._dev_rows.items():
            bg = ACCENT if name == dev else PANEL
            row.configure(bg=bg)
            for child in row.winfo_children():
                child.configure(bg=bg)

        ok, msg = probe_joystick(dev)
        self._device_status_var.set(("✓  " if ok else "✗  ") + msg)
        self._device_status_lbl.configure(fg=SUCCESS if ok else DANGER)
        self._refresh_start_button_state()

    def _parsed_channel_count(self) -> int:
        try:
            value = int(self._channel_count_var.get().strip())
        except ValueError:
            return 0
        return value if value > 0 else 0

    def _refresh_start_button_state(self) -> None:
        valid_count = self._parsed_channel_count() > 0
        valid_device = False
        if self.selected_device:
            valid_device, _msg = probe_joystick(self.selected_device)
        self._start_btn.configure(state="normal" if valid_device and valid_count else "disabled")

    def _start_mapping(self) -> None:
        if not self.selected_device:
            return

        channel_count = self._parsed_channel_count()
        if channel_count <= 0:
            return

        ok, msg = probe_joystick(self.selected_device)
        if not ok:
            messagebox.showerror("Controller error", msg)
            return

        self.channel_count = channel_count
        self.channel_configs = [ChannelConfig() for _ in range(channel_count)]
        self.current_channel_idx = 0

        if self.reader:
            self.reader.stop()
        self.reader = JsReader(self.selected_device)
        self.reader.start()
        time.sleep(0.15)

        self._show_channel_screen()
        self.after(POLL_MS, self._poll_live_input)

    def _show_channel_screen(self) -> None:
        self._clear()
        channel_number = self.current_channel_idx + 1
        self._header(
            "CHANNEL PROFILE CREATION",
            f"Controller: {self.selected_device}    |    Channel {channel_number} of {self.channel_count}",
        )

        root = tk.Frame(self, bg=BG)
        root.pack(fill="both", expand=True, padx=24, pady=(0, 18))
        root.grid_columnconfigure(0, weight=1)
        root.grid_columnconfigure(1, weight=1)
        root.grid_rowconfigure(0, weight=1)

        left = tk.Frame(root, bg=PANEL, highlightthickness=1, highlightbackground=BORDER)
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 12))

        right = tk.Frame(root, bg=PANEL, highlightthickness=1, highlightbackground=BORDER)
        right.grid(row=0, column=1, sticky="nsew", padx=(12, 0))

        self._build_channel_editor(left)
        self._build_live_panel(right)
        self._load_channel_into_widgets()

    def _build_channel_editor(self, parent: tk.Widget) -> None:
        channel_number = self.current_channel_idx + 1

        tk.Label(parent, text=f"  CHANNEL {channel_number}", font=FONT_HINT, fg=SUBTLE, bg=PANEL, anchor="w").pack(fill="x", padx=12, pady=(12, 8))

        form = tk.Frame(parent, bg=PANEL)
        form.pack(fill="x", padx=18, pady=(0, 8))
        form.grid_columnconfigure(1, weight=1)

        tk.Label(form, text="Default value", font=FONT_BODY, fg=TEXT, bg=PANEL, anchor="w").grid(row=0, column=0, sticky="w", pady=8)
        self._default_value_var = tk.StringVar()
        self._default_entry = tk.Entry(form, textvariable=self._default_value_var, font=FONT_BODY, bg=BG, fg=TEXT, insertbackground=TEXT, relief="flat", width=14)
        self._default_entry.grid(row=0, column=1, sticky="w", pady=8)

        tk.Label(form, text="Channel type", font=FONT_BODY, fg=TEXT, bg=PANEL, anchor="w").grid(row=1, column=0, sticky="w", pady=8)
        self._input_type_var = tk.StringVar(value="axis")
        selector = tk.Frame(form, bg=PANEL)
        selector.grid(row=1, column=1, sticky="w", pady=8)
        tk.Radiobutton(selector, text="axis", variable=self._input_type_var, value="axis", command=self._on_input_type_changed, font=FONT_BODY, fg=TEXT, bg=PANEL, selectcolor=BG, activebackground=PANEL, activeforeground=TEXT).pack(side="left", padx=(0, 10))
        tk.Radiobutton(selector, text="buttons", variable=self._input_type_var, value="buttons", command=self._on_input_type_changed, font=FONT_BODY, fg=TEXT, bg=PANEL, selectcolor=BG, activebackground=PANEL, activeforeground=TEXT).pack(side="left")

        self._input_error_var = tk.StringVar(value="")
        self._input_error_lbl = tk.Label(parent, textvariable=self._input_error_var, font=FONT_HINT, fg=WARNING, bg=PANEL, justify="left")
        self._input_error_lbl.pack(anchor="w", padx=18, pady=(0, 8))

        self._mode_section_holder = tk.Frame(parent, bg=PANEL)
        self._mode_section_holder.pack(fill="both", expand=True, padx=18, pady=(4, 8))

        self._axis_section = tk.Frame(self._mode_section_holder, bg=PANEL)

        axis_top = tk.Frame(self._axis_section, bg=PANEL)
        axis_top.pack(fill="x")
        tk.Label(axis_top, text="Axis mapping", font=FONT_BIG, fg=TEXT, bg=PANEL).pack(side="left")
        self._detect_axis_btn = self._make_button(axis_top, "DETECT AXIS", self._detect_axis_for_current_channel, primary=True)
        self._detect_axis_btn.pack(side="right")

        axis_range = tk.Frame(self._axis_section, bg=PANEL)
        axis_range.pack(fill="x", pady=(12, 8))
        tk.Label(axis_range, text="Min value", font=FONT_BODY, fg=TEXT, bg=PANEL).grid(row=0, column=0, sticky="w", pady=4)
        self._axis_min_var = tk.StringVar()
        tk.Entry(axis_range, textvariable=self._axis_min_var, width=12, font=FONT_BODY, bg=BG, fg=TEXT, insertbackground=TEXT, relief="flat").grid(row=0, column=1, sticky="w", padx=(10, 20), pady=4)
        tk.Label(axis_range, text="Max value", font=FONT_BODY, fg=TEXT, bg=PANEL).grid(row=0, column=2, sticky="w", pady=4)
        self._axis_max_var = tk.StringVar()
        tk.Entry(axis_range, textvariable=self._axis_max_var, width=12, font=FONT_BODY, bg=BG, fg=TEXT, insertbackground=TEXT, relief="flat").grid(row=0, column=3, sticky="w", padx=(10, 0), pady=4)

        self._axis_status_var = tk.StringVar(value="No axis assigned yet")
        tk.Label(self._axis_section, textvariable=self._axis_status_var, font=FONT_MONO, fg=TEXT, bg=PANEL, anchor="w", justify="left").pack(fill="x", pady=(6, 6))
        self._axis_hint_var = tk.StringVar(value="Press Detect Axis, then move only the intended axis through its range.")
        tk.Label(self._axis_section, textvariable=self._axis_hint_var, font=FONT_HINT, fg=SUBTLE, bg=PANEL, anchor="w", justify="left").pack(fill="x")

        self._buttons_section = tk.Frame(self._mode_section_holder, bg=PANEL)

        buttons_top = tk.Frame(self._buttons_section, bg=PANEL)
        buttons_top.pack(fill="x")
        tk.Label(buttons_top, text="Button mapping", font=FONT_BIG, fg=TEXT, bg=PANEL).pack(side="left")
        self._listen_buttons_btn = self._make_button(buttons_top, "LISTEN FOR BUTTONS", self._toggle_button_listen, primary=True)
        self._listen_buttons_btn.pack(side="right")

        self._button_status_var = tk.StringVar(value="Listening is OFF")
        tk.Label(self._buttons_section, textvariable=self._button_status_var, font=FONT_MONO, fg=TEXT, bg=PANEL, anchor="w").pack(fill="x", pady=(12, 4))
        tk.Label(
            self._buttons_section,
            text="Press each button that should affect this channel. A new row is added automatically.\nEach row lets you define the channel value for that button.",
            font=FONT_HINT,
            fg=SUBTLE,
            bg=PANEL,
            justify="left",
            anchor="w",
        ).pack(fill="x")

        self._button_rows_holder = tk.Frame(self._buttons_section, bg=PANEL)
        self._button_rows_holder.pack(fill="both", expand=True, pady=(10, 0))

        nav = tk.Frame(parent, bg=PANEL)
        nav.pack(fill="x", padx=18, pady=(18, 16))
        self._prev_btn = self._make_button(nav, "PREVIOUS", self._go_prev_channel, primary=False, side="left")
        self._save_next_btn = self._make_button(nav, "SAVE & NEXT", self._save_and_next, primary=True, side="right")
        self._save_finish_btn = self._make_button(nav, "SAVE & FINISH", self._save_and_finish, primary=True, side="right")

        if self.current_channel_idx == 0:
            self._prev_btn.configure(state="disabled")
        if self.current_channel_idx == self.channel_count - 1:
            self._save_next_btn.pack_forget()
        else:
            self._save_finish_btn.pack_forget()

        self._button_value_vars: Dict[int, tk.StringVar] = {}
        self._button_row_frames: Dict[int, tk.Frame] = {}

    def _build_live_panel(self, parent: tk.Widget) -> None:
        tk.Label(parent, text="  LIVE INPUT MONITOR", font=FONT_HINT, fg=SUBTLE, bg=PANEL, anchor="w").pack(fill="x", padx=12, pady=(12, 8))

        self._live_info_var = tk.StringVar(value="Waiting for controller activity...")
        tk.Label(parent, textvariable=self._live_info_var, font=FONT_HINT, fg=SUBTLE, bg=PANEL, justify="left", anchor="w").pack(fill="x", padx=18, pady=(0, 10))

        self._axis_live_var = tk.StringVar(value="")
        self._button_live_var = tk.StringVar(value="")

        axis_card = tk.Frame(parent, bg=ROW_ALT, highlightthickness=1, highlightbackground=BORDER)
        axis_card.pack(fill="both", expand=True, padx=18, pady=(0, 10))
        tk.Label(axis_card, text="AXES", font=FONT_BIG, fg=TEXT, bg=ROW_ALT).pack(anchor="w", padx=12, pady=(10, 6))
        tk.Label(axis_card, textvariable=self._axis_live_var, font=FONT_MONO, fg=TEXT, bg=ROW_ALT, justify="left", anchor="nw").pack(fill="both", expand=True, padx=12, pady=(0, 12))

        button_card = tk.Frame(parent, bg=ROW_ALT, highlightthickness=1, highlightbackground=BORDER)
        button_card.pack(fill="both", expand=True, padx=18, pady=(0, 16))
        tk.Label(button_card, text="BUTTONS", font=FONT_BIG, fg=TEXT, bg=ROW_ALT).pack(anchor="w", padx=12, pady=(10, 6))
        tk.Label(button_card, textvariable=self._button_live_var, font=FONT_MONO, fg=TEXT, bg=ROW_ALT, justify="left", anchor="nw").pack(fill="both", expand=True, padx=12, pady=(0, 12))

    def _load_channel_into_widgets(self) -> None:
        config = self.channel_configs[self.current_channel_idx]

        self._detecting_axis = False
        self._listening_buttons = False
        self._default_value_var.set(str(config.default_value))
        self._axis_min_var.set(str(config.axis_min))
        self._axis_max_var.set(str(config.axis_max))
        self._input_type_var.set(config.input_type)
        self._axis_status_var.set(
            f"Assigned axis_number: {config.axis_number}" if config.axis_number is not None else "No axis assigned yet"
        )
        self._axis_hint_var.set("Press Detect Axis, then move only the intended axis through its range.")
        self._button_status_var.set("Listening is OFF")
        self._listen_buttons_btn.configure(text="LISTEN FOR BUTTONS")
        self._input_error_var.set("")

        self._rebuild_button_rows(config.buttons)
        self._on_input_type_changed()

    def _on_input_type_changed(self) -> None:
        chosen = self._input_type_var.get()
        self._axis_section.pack_forget()
        self._buttons_section.pack_forget()
        if chosen == "axis":
            self._axis_section.pack(fill="both", expand=True)
        else:
            self._buttons_section.pack(fill="both", expand=True)

    def _rebuild_button_rows(self, mapping: Dict[int, int]) -> None:
        for widget in self._button_rows_holder.winfo_children():
            widget.destroy()
        self._button_value_vars.clear()
        self._button_row_frames.clear()

        header = tk.Frame(self._button_rows_holder, bg=BORDER)
        header.pack(fill="x", pady=(0, 4))
        cols = [("BUTTON", 18), ("VALUE WHEN BUTTON IS ON", 24), ("", 10)]
        for text, width in cols:
            tk.Label(header, text=text, width=width, font=FONT_MONO, fg=SUBTLE, bg=BORDER, pady=6).pack(side="left", padx=4)

        if not mapping:
            tk.Label(self._button_rows_holder, text="No buttons assigned yet", font=FONT_HINT, fg=SUBTLE, bg=PANEL, anchor="w").pack(fill="x", pady=10)
            return

        for button_id in sorted(mapping):
            self._add_button_row(button_id, mapping[button_id])

    def _add_button_row(self, button_id: int, value: int) -> None:
        placeholder = None
        children = self._button_rows_holder.winfo_children()
        if len(children) == 2:
            maybe_placeholder = children[1]
            if isinstance(maybe_placeholder, tk.Label) and maybe_placeholder.cget("text") == "No buttons assigned yet":
                placeholder = maybe_placeholder
        if placeholder is not None:
            placeholder.destroy()

        row = tk.Frame(self._button_rows_holder, bg=ROW_ALT, highlightthickness=1, highlightbackground=BORDER)
        row.pack(fill="x", pady=3)
        self._button_row_frames[button_id] = row

        tk.Label(row, text=f"button_{button_id}", width=18, font=FONT_MONO, fg=TEXT, bg=ROW_ALT, anchor="w").pack(side="left", padx=8, pady=8)

        value_var = tk.StringVar(value=str(value))
        self._button_value_vars[button_id] = value_var
        entry = tk.Entry(row, textvariable=value_var, width=18, font=FONT_BODY, bg=BG, fg=TEXT, insertbackground=TEXT, relief="flat")
        entry.pack(side="left", padx=8, pady=8)

        delete_btn = tk.Button(
            row,
            text="DELETE",
            font=FONT_HINT,
            fg=TEXT,
            bg=BORDER,
            activebackground=PANEL,
            activeforeground=TEXT,
            bd=0,
            padx=12,
            pady=6,
            cursor="hand2",
            command=lambda bid=button_id: self._delete_button_assignment(bid),
        )
        delete_btn.pack(side="right", padx=8, pady=8)

    def _delete_button_assignment(self, button_id: int) -> None:
        if button_id in self._button_row_frames:
            self._button_row_frames[button_id].destroy()
            del self._button_row_frames[button_id]
        if button_id in self._button_value_vars:
            del self._button_value_vars[button_id]
        if not self._button_row_frames:
            self._rebuild_button_rows({})

    def _poll_live_input(self) -> None:
        if not self.reader or not self.winfo_exists():
            return

        axes = self.reader.snapshot_axes()
        buttons = self.reader.snapshot_buttons()

        axis_lines = [f"axis_{axis}: {value}" for axis, value in sorted(axes.items())]
        button_lines = [f"button_{button}: {value}" for button, value in sorted(buttons.items())]

        self._axis_live_var.set("\n".join(axis_lines[:18]) if axis_lines else "No axis data yet")
        self._button_live_var.set("\n".join(button_lines[:18]) if button_lines else "No button data yet")
        self._live_info_var.set("Live values update automatically while this page is open.")

        if self._listening_buttons:
            for button_id in self.reader.pop_button_press_events():
                if button_id not in self._button_value_vars:
                    default_text = self._default_value_var.get().strip()
                    try:
                        default_value = int(default_text)
                    except ValueError:
                        default_value = 1500
                    self._add_button_row(button_id, default_value)
                    self._button_status_var.set(f"Added button_{button_id}")

        self.after(POLL_MS, self._poll_live_input)

    def _detect_axis_for_current_channel(self) -> None:
        if not self.reader or self._detecting_axis:
            return

        self._input_error_var.set("")
        self._detecting_axis = True
        self._detect_axis_btn.configure(state="disabled")
        self._axis_status_var.set("Detecting... move only the axis for this channel")
        self._axis_hint_var.set("Recording axis movement now.")

        baseline = self.reader.snapshot_axes()
        samples: List[Dict[int, int]] = []
        start = time.monotonic()

        def collect() -> None:
            if not self.reader:
                self._finish_axis_detection(None, 0.0)
                return

            elapsed = time.monotonic() - start
            samples.append(self.reader.snapshot_axes())
            if elapsed < AXIS_DETECTION_SECONDS:
                self.after(POLL_MS, collect)
                return

            axis, peak = self._find_moved_axis(baseline, samples)
            self._finish_axis_detection(axis, peak)

        collect()

    def _find_moved_axis(self, baseline: Dict[int, int], samples: List[Dict[int, int]]) -> Tuple[Optional[int], float]:
        if not samples:
            return None, 0.0

        axes = set(baseline.keys())
        for snap in samples:
            axes.update(snap.keys())

        best_axis: Optional[int] = None
        best_peak = 0.0
        for axis in sorted(axes):
            center = baseline.get(axis)
            if center is None:
                first_seen = None
                for snap in samples:
                    if axis in snap:
                        first_seen = snap[axis]
                        break
                center = 0 if first_seen is None else first_seen

            peak = 0.0
            for snap in samples:
                value = snap.get(axis, center)
                peak = max(peak, abs(value - center))

            if peak > best_peak:
                best_peak = peak
                best_axis = axis

        return best_axis, best_peak

    def _finish_axis_detection(self, axis: Optional[int], peak: float) -> None:
        self._detecting_axis = False
        self._detect_axis_btn.configure(state="normal")

        if axis is None:
            self._axis_status_var.set("Could not identify a moved axis")
            self._axis_hint_var.set("Retry and move only the intended axis.")
            return

        if peak < MIN_AXIS_MOVE_THRESHOLD:
            self._axis_status_var.set(f"Movement too small ({int(peak)})")
            self._axis_hint_var.set("Retry and move the control through a larger range.")
            return

        self.channel_configs[self.current_channel_idx].axis_number = axis
        self._axis_status_var.set(f"Assigned axis_number: {axis}")
        self._axis_hint_var.set(f"Detected successfully. Peak movement: {int(peak)}")

    def _toggle_button_listen(self) -> None:
        self._listening_buttons = not self._listening_buttons
        if self._listening_buttons:
            if self.reader:
                self.reader.pop_button_press_events()
            self._button_status_var.set("Listening is ON — press buttons now")
            self._listen_buttons_btn.configure(text="STOP LISTENING")
        else:
            self._button_status_var.set("Listening is OFF")
            self._listen_buttons_btn.configure(text="LISTEN FOR BUTTONS")

    def _read_default_value(self, *, required: bool = True) -> Tuple[Optional[int], Optional[str]]:
        raw = self._default_value_var.get().strip()
        if not raw:
            return (None, None) if not required else (None, "Default value is required.")
        try:
            return int(raw), None
        except ValueError:
            return None, "Default value must be an integer."

    def _read_axis_range(self, *, required: bool = True) -> Tuple[Optional[int], Optional[int], Optional[str]]:
        raw_min = self._axis_min_var.get().strip()
        raw_max = self._axis_max_var.get().strip()
        if not raw_min or not raw_max:
            if required:
                return None, None, "Axis channel requires both min and max values."
            return None, None, None
        try:
            axis_min = int(raw_min)
            axis_max = int(raw_max)
        except ValueError:
            return None, None, "Axis min and max must be integers."
        if axis_min >= axis_max:
            return None, None, "Axis min must be smaller than axis max."
        return axis_min, axis_max, None

    def _gather_button_mapping(self, *, required: bool = True) -> Tuple[Optional[Dict[int, int]], Optional[str]]:
        mapping: Dict[int, int] = {}
        for button_id, value_var in sorted(self._button_value_vars.items()):
            raw = value_var.get().strip()
            if not raw:
                return (None, None) if not required else (None, f"button_{button_id} is missing its value.")
            try:
                mapping[button_id] = int(raw)
            except ValueError:
                return None, f"button_{button_id} must have an integer value."
        return mapping, None

    def _save_current_channel(self, *, validate: bool = True) -> bool:
        default_value, err = self._read_default_value(required=validate)
        if err:
            self._input_error_var.set(err)
            return False

        config = self.channel_configs[self.current_channel_idx]
        if default_value is not None:
            config.default_value = default_value
        config.input_type = self._input_type_var.get()

        if config.input_type == "axis":
            axis_min, axis_max, err = self._read_axis_range(required=validate)
            if err:
                self._input_error_var.set(err)
                return False
            if axis_min is not None:
                config.axis_min = axis_min
            if axis_max is not None:
                config.axis_max = axis_max

            if validate and config.axis_number is None:
                self._input_error_var.set("Axis channel requires a detected axis_number.")
                return False
            config.buttons = {}
        else:
            buttons, err = self._gather_button_mapping(required=validate)
            if err:
                self._input_error_var.set(err)
                return False
            if validate and not buttons:
                self._input_error_var.set("Button channel requires at least one assigned button.")
                return False
            if buttons is not None:
                config.buttons = buttons
            config.axis_number = None

        self._input_error_var.set("")
        return True

    def _go_prev_channel(self) -> None:
        if not self._save_current_channel(validate=False):
            return
        if self.current_channel_idx <= 0:
            return
        self.current_channel_idx -= 1
        self._show_channel_screen()

    def _save_and_next(self) -> None:
        if not self._save_current_channel():
            return
        if self.current_channel_idx >= self.channel_count - 1:
            return
        self.current_channel_idx += 1
        self._show_channel_screen()

    def _save_and_finish(self) -> None:
        if not self._save_current_channel():
            return
        self._write_yaml_file()
        self._show_done_screen()

    def _write_yaml_file(self) -> None:
        out = Path(OUTPUT_FILE)
        lines: List[str] = []
        for index, config in enumerate(self.channel_configs, start=1):
            lines.append(f"channel_{index}:")
            lines.append(f"  default_value: {config.default_value}")
            lines.append(f"  input_type: {config.input_type}")
            if config.input_type == "axis":
                lines.append(f"  rc_min: {config.axis_min}")
                lines.append(f"  rc_max: {config.axis_max}")
                axis_number = 0 if config.axis_number is None else config.axis_number
                lines.append(f"  axis_number: {axis_number}")
            else:
                lines.append("  buttons:")
                for button_id, value in sorted(config.buttons.items()):
                    lines.append(f"    {button_id}: {value}")
        out.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def _show_done_screen(self) -> None:
        self._clear()
        out = Path(OUTPUT_FILE)
        self._header("CHANNEL PROFILE SAVED", f"Saved -> {out.resolve()}", title_fg=SUCCESS)

        card = self._card(self, padx=26, pady=12)
        tk.Label(card, text="SUMMARY", font=FONT_BIG, fg=TEXT, bg=PANEL).pack(anchor="w", padx=16, pady=(14, 8))

        table = tk.Frame(card, bg=PANEL)
        table.pack(fill="x", padx=16, pady=(0, 14))

        headers = ["CHANNEL", "DEFAULT", "TYPE", "AXIS / BUTTONS"]
        widths = [12, 12, 12, 46]
        for col, (header, width) in enumerate(zip(headers, widths)):
            tk.Label(table, text=header, width=width, font=FONT_MONO, fg=SUBTLE, bg=BORDER, pady=6).grid(row=0, column=col, sticky="nsew", padx=1, pady=1)

        for row_idx, config in enumerate(self.channel_configs, start=1):
            detail = f"axis_number={config.axis_number}, min={config.axis_min}, max={config.axis_max}" if config.input_type == "axis" else ", ".join(
                f"button_{button}->{value}" for button, value in sorted(config.buttons.items())
            )
            values = [f"channel_{row_idx}", str(config.default_value), config.input_type, detail]
            for col_idx, (value, width) in enumerate(zip(values, widths)):
                tk.Label(table, text=value, width=width, font=FONT_MONO, fg=TEXT, bg=PANEL, pady=5, anchor="w").grid(row=row_idx, column=col_idx, sticky="nsew", padx=1, pady=1)

        actions = tk.Frame(self, bg=BG)
        actions.pack(pady=(6, 22))
        self._make_button(actions, "CLOSE", self._on_close, primary=True, side="left")
        self._make_button(actions, "CREATE ANOTHER PROFILE", self._restart, primary=False, side="left")

    def _restart(self) -> None:
        if self.reader:
            self.reader.stop()
            self.reader = None
        self.selected_device = None
        self.channel_count = 0
        self.channel_configs = []
        self.current_channel_idx = 0
        self._show_start_screen()


if __name__ == "__main__":
    if sys.platform != "linux":
        print("This tool is Linux-only.")
        sys.exit(1)

    App().mainloop()