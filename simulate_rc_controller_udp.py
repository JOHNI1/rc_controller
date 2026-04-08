#!/usr/bin/env python3
"""
simulate_rc_controller_udp.py

UDP forwarder built on top of simulate_rc_controller.py.

It reads live RC channel values from RcJoystickController and sends them to a
simulator using the Betaflight-style UDP RC packet:

    struct {
        double   timestamp;
        uint16_t channels[16];
    }

Usage:
    python3 simulate_rc_controller_udp.py
    python3 simulate_rc_controller_udp.py --js /dev/input/js1
    python3 simulate_rc_controller_udp.py --host 127.0.0.1 --port 9004
    python3 simulate_rc_controller_udp.py --rate 100
"""

from __future__ import annotations

import argparse
import os
import socket
import struct
import sys
import time
from typing import List

from simulate_rc_controller import RcJoystickController

UDP_PACKET_CHANNEL_COUNT = 16
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 9004
DEFAULT_RATE_HZ = 100.0
RC_VALUE_MIN = 1000


def build_rc_udp_packet(channels: List[int]) -> bytes:
    if len(channels) != UDP_PACKET_CHANNEL_COUNT:
        raise ValueError(f"Expected exactly {UDP_PACKET_CHANNEL_COUNT} channels")
    timestamp = time.monotonic()
    return struct.pack("<d16H", timestamp, *channels)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Forward local joystick channels over UDP using simulate_rc_controller.py"
    )
    parser.add_argument("--js", dest="js_path", default=None, help="Joystick device, e.g. /dev/input/js0")
    parser.add_argument("--host", default=DEFAULT_HOST, help=f"UDP destination host (default: {DEFAULT_HOST})")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"UDP destination port (default: {DEFAULT_PORT})")
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

    print(f"Joystick:    {controller.js_path}")
    print(f"UDP target:  {args.host}:{args.port}")
    print(f"Send rate:   {args.rate} Hz")
    controller.print_channel_summary()

    controller.start()
    period = 1.0 / max(args.rate, 1.0)

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.connect((args.host, args.port))

    try:
        print("Streaming UDP RC packets. Press Ctrl+C to stop.")
        last_print = 0.0

        while True:
            loop_start = time.monotonic()

            channel_values = controller.get_channel_values()

            while len(channel_values) < UDP_PACKET_CHANNEL_COUNT:
                channel_values.append(RC_VALUE_MIN)

            packet = build_rc_udp_packet(channel_values[:UDP_PACKET_CHANNEL_COUNT])
            sock.send(packet)

            now = time.monotonic()
            if now - last_print >= 1.0:
                pretty = ", ".join(f"ch{i+1}={value}" for i, value in enumerate(channel_values[:UDP_PACKET_CHANNEL_COUNT]))
                print(pretty)
                last_print = now

            elapsed = time.monotonic() - loop_start
            sleep_time = period - elapsed
            if sleep_time > 0:
                time.sleep(sleep_time)

    except KeyboardInterrupt:
        print("\nStopped by user.")
    finally:
        controller.stop()
        sock.close()


if __name__ == "__main__":
    main()