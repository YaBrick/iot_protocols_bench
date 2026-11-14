#!/usr/bin/env python3
"""Turns bench captures (pcap/pcapng from dumpcap on the RPi) into tables.

For every capture <name>.pcapng it writes <name>.msgs.csv, one row per
message, and prints one summary per run. Given a directory, it processes every
capture below it and also writes <dir>/summary.csv, one row per run, for
comparing protocols. Python 3.10+, standard library only.

Protocol is chosen by port: MQTT 1883 (app/esp32_mqtt_bench). Modbus TCP and
Micro XRCE-DDS are not parsed yet.

Clock log: clock_button.py --log writes start_ns per clock start. Any
clock*.csv next to the capture (or --clock-log) is matched to runs by time and
puts ESP ticks on the capture clock with ~0.1 ms error; without it the offset
is estimated from the capture, with an error of several ms that shows up in
the per-hop columns only (end-to-end latency is tick-based either way).

Examples:
  ./parse_runs.py ../runs
  ./parse_runs.py ../runs/mqtt/mqtt_run1.pcapng
  ./parse_runs.py run.pcapng --clock-log clock_log.csv
"""

import argparse
import csv
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import mqtt  # noqa: E402
from pcap import read_packets  # noqa: E402

CAPTURE_EXT = (".pcapng", ".pcap")


def read_clock_logs(paths):
    rows = []
    for path in paths:
        with open(path, newline="") as f:
            for r in csv.DictReader(f):
                rows.append({k: int(v) for k, v in r.items() if v not in (None, "")})
    return rows


def find_captures(path):
    if os.path.isfile(path):
        return [path]
    found = []
    for root, _dirs, files in os.walk(path):
        found += [os.path.join(root, f) for f in sorted(files) if f.endswith(CAPTURE_EXT)]
    return sorted(found)


def write_csv(path, rows):
    if not rows:
        return
    fields = list(dict.fromkeys(k for r in rows for k in r))
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)


def kB(v):
    return "-" if v is None else f"{v / 1000:.1f}"


def ms_line(s, prefix):
    keys = ("min", "p50", "p90", "p99", "max", "mean", "std", "jitter")
    if s[f"{prefix}_p50_ms"] is None:
        return "-"
    return "  ".join(f"{k} {s[f'{prefix}_{k}_ms']:.1f}" for k in keys)


def print_summary(path, s):
    print(f"\n{path}: MQTT run {s['run']}  size={s['size']} count={s['count']} qos={s['qos']} "
          f"inflight={s['inflight'] if s['inflight'] is not None else '?'} "
          f"(seen at most {s['inflight_obs_max']})  pub {s['pub_ip']} -> sub {s['sub_ip']}")
    print(f"  delivered   {s['delivered']}/{s['count']}, lost {s['lost']}, end={s['end']}, "
          f"MQTT DUP resends {s['pub_dup_publish']}")
    print(f"  TCP retx    pub up {s['retrans_pub_up']} down {s['retrans_pub_down']} | "
          f"sub down {s['retrans_sub_down']} up {s['retrans_sub_up']}")
    print(f"  goodput     {kB(s['goodput_Bps'])} kB/s over {s['duration_s']:.2f} s "
          f"(sub reports {kB(s['esp_goodput_Bps'])})")
    print(f"  wire, L2    pub up {kB(s['thr_pub_up_Bps'])} down {kB(s['thr_pub_down_Bps'])} | "
          f"sub down {kB(s['thr_sub_down_Bps'])} up {kB(s['thr_sub_up_Bps'])} kB/s")
    print(f"  latency ms  {ms_line(s, 'lat')}")
    print(f"  hops p50    up {s['up_p50_ms']} | broker {s['broker_p50_ms']} | down {s['down_p50_ms']} ms "
          f"(offset {s['offset_source']}, +-{s['offset_err_ms']} ms)")
    print(f"  rtt sub ms  {ms_line(s, 'rtt_sub')}")
    print(f"  rtt pub ms  {ms_line(s, 'rtt_pub')}")
    print(f"  ESP ack ms  min {s['esp_ack_min_ms']}  avg {s['esp_ack_avg_ms']}  max {s['esp_ack_max_ms']}")
    if s["notes"]:
        print(f"  notes       {s['notes']}")


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("paths", nargs="+", help="capture files or directories with captures")
    p.add_argument("--clock-log", action="append", default=[], help="clock_button.py CSV (repeatable)")
    p.add_argument("--summary", help="write the per-run summary here (default: <dir>/summary.csv for a directory)")
    args = p.parse_args()

    summaries = []
    for root in args.paths:
        for path in find_captures(root):
            folder = os.path.dirname(path) or "."
            logs = args.clock_log or [os.path.join(folder, f) for f in sorted(os.listdir(folder))
                                      if f.startswith("clock") and f.endswith(".csv")]
            packets = read_packets(path)
            name = os.path.relpath(path, root if os.path.isdir(root) else folder)
            results = mqtt.analyze(packets, name, read_clock_logs(logs))
            if not results:
                print(f"\n{path}: no MQTT bench traffic (Modbus/XRCE-DDS parsers not written yet)")
                continue
            write_csv(os.path.splitext(path)[0] + ".msgs.csv", [m for _, msgs in results for m in msgs])
            for s, _ in results:
                print_summary(path, s)
                summaries.append(s)

    out = args.summary or (os.path.join(args.paths[0], "summary.csv")
                           if len(args.paths) == 1 and os.path.isdir(args.paths[0]) else None)
    if out and summaries:
        write_csv(out, summaries)
        print(f"\nsummary: {out}")


if __name__ == "__main__":
    main()
