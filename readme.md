# RC Controller Project

This project turns a Linux joystick/controller into RC channel output using a calibrated profile and can stream it either over UDP or CRSF.

## What it does

The project has 3 stages:

1. **Calibrate controller axes** and save the raw joystick ranges.
2. **Map joystick inputs to RC channels** and choose each channel’s behavior.
3. **Run a live controller** that reads the joystick and outputs RC values in real time.

At runtime, the system reads `/dev/input/js*`, applies the saved calibration and channel mapping, and produces standard RC-style values such as `1000` to `2000`.

## Files

### `rc_controller_axis_calibration.py`
GUI calibration tool.

- Detects joystick axes.
- Calibrates `roll`, `pitch`, `throttle`, and `yaw` first.
- Can also calibrate extra inputs such as sliders, knobs, or switches.
- Saves:
  - `rc_controller_axis_calibration.json`

### `rc_controller_channel_function_mapping.py`
GUI channel profile tool.

- Lets you choose how many RC channels you want.
- For each channel, assigns either:
  - an **axis**, or
  - one or more **buttons**
- Sets default value and RC output range / button output values.
- Saves:
  - `rc_controller_channel_function_mapping.yaml`

### `simulate_rc_controller.py`
Core helper module.

- Loads the calibration JSON and mapping YAML.
- Reads live joystick events.
- Converts joystick input into RC channel values.
- This is a helper module only. It does **not** run by itself.

### `simulate_rc_controller_udp.py`
UDP sender.

- Uses `simulate_rc_controller.py`.
- Sends 16-channel RC packets over UDP.
- Good for simulators that accept RC input over UDP.

### `simulate_rc_controller_crsf.py`
CRSF sender over serial.

- Uses `simulate_rc_controller.py`.
- Sends CRSF RC frames through a serial port such as `/dev/ttyUSB0`.
- Requires `pyserial`.

## How it works at a high level

- `rc_controller_axis_calibration.json` stores how your joystick axes behave physically.
- `rc_controller_channel_function_mapping.yaml` stores what each RC channel should do.
- `simulate_rc_controller.py` combines both files and resolves the final channel values.
- The UDP or CRSF script sends those resolved channel values to the target system.

## Correct run order

Run the project in this order from the project folder:

### 1) Calibrate the joystick

```bash
python3 rc_controller_axis_calibration.py
```

Or with a specific device:

```bash
python3 rc_controller_axis_calibration.py /dev/input/js0
```

This creates:

- `rc_controller_axis_calibration.json`

### 2) Create the channel mapping profile

```bash
python3 rc_controller_channel_function_mapping.py
```

Or with a specific joystick:

```bash
python3 rc_controller_channel_function_mapping.py /dev/input/js0
```

This creates:

- `rc_controller_channel_function_mapping.yaml`

### 3A) Run UDP output

```bash
python3 simulate_rc_controller_udp.py
```

Common examples:

```bash
python3 simulate_rc_controller_udp.py --js /dev/input/js0
python3 simulate_rc_controller_udp.py --host 127.0.0.1 --port 9004
python3 simulate_rc_controller_udp.py --rate 100
```

### 3B) Run CRSF output

```bash
python3 simulate_rc_controller_crsf.py /dev/ttyUSB0
```

Common examples:

```bash
python3 simulate_rc_controller_crsf.py /dev/ttyUSB0 --js /dev/input/js0
python3 simulate_rc_controller_crsf.py /dev/ttyUSB0 --rate 50
```

## Requirements

- Linux
- A joystick/controller exposed as `/dev/input/js*`
- Python 3
- For CRSF only:

```bash
pip install pyserial
```

## Important notes

- `simulate_rc_controller.py` depends on both saved files being present:
  - `rc_controller_axis_calibration.json`
  - `rc_controller_channel_function_mapping.yaml`
- If either file is missing, the runtime scripts will fail.
- The tools support up to 16 channels.