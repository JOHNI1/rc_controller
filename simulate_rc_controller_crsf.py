#!/usr/bin/env python3
"""
simulate_rc_controller_crsf.py

Thin CRSF forwarder built on top of simulate_rc_controller.py.

Usage:
    python3 simulate_rc_controller_crsf.py /dev/ttyUSB0
    python3 simulate_rc_controller_crsf.py /dev/ttyUSB0 --js /dev/input/js1
    python3 simulate_rc_controller_crsf.py /dev/ttyUSB0 --rate 50
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from typing import List

try:
    import serial  # type: ignore
except ImportError:
    print("Missing dependency: pyserial")
    print("Install with: pip install pyserial")
    raise

from simulate_rc_controller import RcJoystickController

DEFAULT_BAUD = 416666
DEFAULT_RATE_HZ = 50
CRSF_CHANNEL_COUNT_LIMIT = 16
CRSF_RAW_MIN = 172
CRSF_RAW_MAX = 1811


def clamp_int(value: float, lo: int, hi: int) -> int:
    return max(lo, min(hi, int(round(value))))


def crc8_dvb_s2(data: bytes) -> int:
    crc = 0
    for b in data:
        crc ^= b
        for _ in range(8):
            if crc & 0x80:
                crc = ((crc << 1) ^ 0xD5) & 0xFF
            else:
                crc = (crc << 1) & 0xFF
    return crc


def pack_crsf_channels(channels: List[int]) -> bytearray:
    payload = bytearray()
    bit_buffer = 0
    bit_count = 0

    for ch in channels:
        bit_buffer |= (ch & 0x7FF) << bit_count
        bit_count += 11

        while bit_count >= 8:
            payload.append(bit_buffer & 0xFF)
            bit_buffer >>= 8
            bit_count -= 8

    if bit_count > 0:
        payload.append(bit_buffer & 0xFF)

    return payload


def build_crsf_rc_frame(channels: List[int]) -> bytearray:
    sync_byte = 0xC8
    type_byte = 0x16

    payload = pack_crsf_channels(channels)
    length = 1 + len(payload) + 1

    frame = bytearray([sync_byte, length, type_byte]) + payload
    frame.append(crc8_dvb_s2(frame[2:]))
    return frame


def channel_to_crsf_raw(value_us: int) -> int:
    if value_us <= 1000:
        return CRSF_RAW_MIN
    if value_us >= 2000:
        return CRSF_RAW_MAX

    span_in = 1000.0
    span_out = float(CRSF_RAW_MAX - CRSF_RAW_MIN)
    raw = CRSF_RAW_MIN + (float(value_us - 1000) / span_in) * span_out
    return clamp_int(raw, CRSF_RAW_MIN, CRSF_RAW_MAX)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Forward local joystick channels as CRSF over UART.")
    parser.add_argument("serial_port", help="UART device for the USB-to-TTL adapter, e.g. /dev/ttyUSB0")
    parser.add_argument("--js", dest="js_path", default=None, help="Joystick device, e.g. /dev/input/js0")
    parser.add_argument("--baud", type=int, default=DEFAULT_BAUD, help=f"UART baud rate (default: {DEFAULT_BAUD})")
    parser.add_argument("--rate", type=float, default=DEFAULT_RATE_HZ, help=f"Send rate in Hz (default: {DEFAULT_RATE_HZ})")
    return parser.parse_args()


def main() -> None:
    if sys.platform != "linux":
        print("This script is Linux-only.")
        sys.exit(1)

    args = parse_args()

    if args.js_path and not os.path.exists(args.js_path):
        raise FileNotFoundError(f"Joystick device not found: {args.js_path}")

    controller = RcJoystickController(js_path=args.js_path)

    print(f"Serial port: {args.serial_port}")
    print(f"Joystick:    {controller.js_path}")
    print(f"Baud rate:   {args.baud}")
    print(f"Send rate:   {args.rate} Hz")
    controller.print_channel_summary()

    controller.start()
    period = 1.0 / max(args.rate, 1.0)

    try:
        with serial.Serial(args.serial_port, args.baud, timeout=0.02) as ser:
            print("Streaming CRSF frames. Press Ctrl+C to stop.")
            last_print = 0.0

            while True:
                loop_start = time.monotonic()

                rc_us_values = controller.get_channel_values()
                crsf_raw_values = [channel_to_crsf_raw(value) for value in rc_us_values]

                while len(crsf_raw_values) < CRSF_CHANNEL_COUNT_LIMIT:
                    crsf_raw_values.append(channel_to_crsf_raw(1500))

                ser.write(build_crsf_rc_frame(crsf_raw_values))

                now = time.monotonic()
                if now - last_print >= 1.0:
                    pretty = ", ".join(f"ch{i+1}={value}" for i, value in enumerate(rc_us_values))
                    print(pretty)
                    last_print = now

                elapsed = time.monotonic() - loop_start
                sleep_time = period - elapsed
                if sleep_time > 0:
                    time.sleep(sleep_time)

    except KeyboardInterrupt:
        print("\\nStopped by user.")
    finally:
        controller.stop()


if __name__ == "__main__":
    main()
