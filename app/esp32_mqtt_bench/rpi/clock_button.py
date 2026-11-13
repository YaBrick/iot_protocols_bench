#!/usr/bin/env python3
"""Reference clock for the MQTT bench (RPi 5): a button starts and stops a
100 kHz square wave, and both ESP32 boards count its rising edges.

Press 1 starts the clock. Both boards count from the same first edge, so a
tick value means the same instant on each of them, and an armed publisher
takes the first edge as the signal to start sending.
Press 2 stops the clock. Stop it before the next bench/ctrl command: the
boards refuse to arm while it runs (error reason=clock_running).

The clock is the RP1 hardware PWM (push-pull, 3.3 V), so its period does not
depend on CPU load. It needs this line in /boot/firmware/config.txt and a reboot:
  dtoverlay=pwm-2chan,pin=12,func=4,pin2=13,func2=4
GPIO12 is then PWM0 channel 0, the clock output; GPIO13 (channel 1) stays unused.

Every start and stop is logged against the RPi wall clock (CLOCK_REALTIME, the
clock dumpcap stamps packets with), so tick N of a run happened at
  start_ns + N * 1e9 / freq   (+- start_err_ns)
which puts ESP-side events onto the Wireshark timeline.

Wiring: see ../README.md.

Examples:
  ./clock_button.py
  ./clock_button.py --duration 30 --log clock_log.csv
"""

import argparse
import csv
import datetime
import glob
import os
import sys
import time

try:
    from gpiozero import Button
except ImportError:
    sys.exit("gpiozero is missing: sudo apt install python3-gpiozero")

PWM_ROOT = "/sys/class/pwm"
RP1_PWM0 = "1f00098000.pwm"  # RP1 PWM0: channels 0..3 on GPIO12, 13, 18, 19
SYSFS_WAIT_S = 2.0


def read(path):
    with open(path) as f:
        return f.read().strip()


def write(path, value):
    # udev hands freshly exported files to the gpio group a moment later
    deadline = time.monotonic() + SYSFS_WAIT_S
    while True:
        try:
            with open(path, "w") as f:
                f.write(str(value))
            return
        except PermissionError:
            if time.monotonic() > deadline:
                raise
            time.sleep(0.05)


def find_chip(channel):
    chips = []
    for chip in sorted(glob.glob(os.path.join(PWM_ROOT, "pwmchip*"))):
        device = os.path.basename(os.path.realpath(os.path.join(chip, "device")))
        chips.append((chip, device, int(read(os.path.join(chip, "npwm")))))

    for chip, device, npwm in chips:
        if device == RP1_PWM0 and channel < npwm:
            return chip

    listing = "\n".join(f"  {c}: {d}, {n} channels" for c, d, n in chips) or "  (none)"
    sys.exit(f"RP1 PWM0 ({RP1_PWM0}) not found. PWM chips:\n{listing}\n"
             "Check the dtoverlay line in /boot/firmware/config.txt (see --help), "
             "or pass the chip with --chip.")


class HwPwm:
    """One channel of a sysfs PWM chip."""

    def __init__(self, chip, channel):
        self.chip = chip
        self.channel = channel
        self.path = os.path.join(chip, f"pwm{channel}")

    def open(self, freq_hz):
        if not os.path.isdir(self.path):
            write(os.path.join(self.chip, "export"), self.channel)
            deadline = time.monotonic() + SYSFS_WAIT_S
            while not os.path.isdir(self.path):
                if time.monotonic() > deadline:
                    sys.exit(f"{self.path} did not appear after export")
                time.sleep(0.05)

        period_ns = round(1e9 / freq_hz)
        self.enable(False)
        # The duty cycle may never exceed the period, whatever was set before
        write(os.path.join(self.path, "duty_cycle"), 0)
        write(os.path.join(self.path, "period"), period_ns)
        write(os.path.join(self.path, "duty_cycle"), period_ns // 2)
        return period_ns

    def enable(self, on):
        write(os.path.join(self.path, "enable"), 1 if on else 0)

    def close(self):
        try:
            self.enable(False)
            write(os.path.join(self.chip, "unexport"), self.channel)
        except OSError:
            pass


def wall(ns):
    return datetime.datetime.fromtimestamp(ns / 1e9).isoformat(sep=" ", timespec="microseconds")


def parse_args():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--button", type=int, default=17, help="button GPIO (BCM), to GND, internal pull-up")
    p.add_argument("--freq", type=int, default=100_000, help="clock frequency, Hz (CONFIG_BENCH_CLOCK_HZ)")
    p.add_argument("--chip", help="PWM chip directory, e.g. /sys/class/pwm/pwmchip0 (default: find RP1 PWM0)")
    p.add_argument("--channel", type=int, default=0, help="PWM channel: 0 = GPIO12, 2 = GPIO18 on RPi 5")
    p.add_argument("--duration", type=float, help="stop the clock after this many seconds without a second press")
    p.add_argument("--log", help="append one row per run to this CSV file")
    return p.parse_args()


def main():
    args = parse_args()
    chip = args.chip or find_chip(args.channel)
    pwm = HwPwm(chip, args.channel)
    period_ns = pwm.open(args.freq)
    freq = 1e9 / period_ns
    button = Button(args.button, pull_up=True, bounce_time=0.05)

    log_file = None
    if args.log:
        new = not os.path.exists(args.log)
        log_file = open(args.log, "a", newline="")
        writer = csv.writer(log_file)
        if new:
            writer.writerow(["run", "start_ns", "start_err_ns", "stop_ns", "freq_hz"])

    print(f"clock: {chip} channel {args.channel}, {freq:.0f} Hz (period {period_ns} ns); "
          f"button: GPIO{args.button}. Ctrl+C to quit.")
    run = 0
    try:
        while True:
            print("press the button to start the clock")
            button.wait_for_press()
            # The first edge leaves somewhere between t0 and t1
            t0 = time.time_ns()
            pwm.enable(True)
            t1 = time.time_ns()
            run += 1
            start_ns, start_err_ns = (t0 + t1) // 2, (t1 - t0) // 2
            print(f"[{run}] clock ON   {wall(start_ns)}  start_ns={start_ns} +-{start_err_ns / 1000:.0f} us")
            button.wait_for_release()

            pressed = button.wait_for_press(timeout=args.duration)
            stop_ns = time.time_ns()
            pwm.enable(False)
            duration = (stop_ns - start_ns) / 1e9
            why = "" if pressed else " (--duration elapsed)"
            print(f"[{run}] clock OFF  {wall(stop_ns)}  {duration:.3f} s, ~{round(duration * freq)} ticks{why}")
            if log_file:
                writer.writerow([run, start_ns, start_err_ns, stop_ns, round(freq)])
                log_file.flush()
            if pressed:
                button.wait_for_release()
    except KeyboardInterrupt:
        print()
    finally:
        pwm.close()
        if log_file:
            log_file.close()


if __name__ == "__main__":
    main()
