#!/usr/bin/env python3
"""Modbus TCP throughput bench, client side (RPi). The ESP32 is the server.

A "message" is `size` bytes of holding registers (FC03). Modbus returns at most
125 registers (250 bytes) per request, so larger messages take several
transactions. `inflight` caps outstanding transactions, `rate` paces messages.

Every run uses its own TCP connection, so in Wireshark each run is a separate
stream (Statistics -> Conversations, filter tcp.stream == N).

Examples:
  ./modbus_bench.py 192.168.1.50 --size 250 --count 2000
  ./modbus_bench.py 192.168.1.50 --size 16,64,250,1000 --inflight 1,4,16 --csv results.csv
"""

import argparse
import asyncio
import csv
import os
import socket
import struct
import sys
import time

FC_READ_HOLDING = 3
FC_READ_INPUT = 4
MAX_REGS = 125
MBAP = struct.Struct(">HHHB")  # transaction id, protocol id, length, unit id
STATS_REGS = 11
STATS_SETTLE_S = 0.3  # the ESP refreshes its stats registers every 100 ms


class BenchError(Exception):
    pass


def build_read(tid, unit, fc, addr, count):
    return MBAP.pack(tid, 0, 6, unit) + struct.pack(">BHH", fc, addr, count)


async def read_frame(reader):
    tid, proto, length, _unit = MBAP.unpack(await reader.readexactly(MBAP.size))
    if proto != 0 or length < 2:
        raise BenchError(f"bad MBAP header: proto={proto} length={length}")
    return tid, await reader.readexactly(length - 1)


def check_pdu(pdu, fc, n_regs):
    """Returns the Modbus exception code, or None for a valid response."""
    if pdu[0] == fc | 0x80:
        return pdu[1]
    if pdu[0] != fc or pdu[1] != 2 * n_regs or len(pdu) != 2 + 2 * n_regs:
        raise BenchError(f"malformed response: {pdu[:4].hex()}... ({len(pdu)} bytes)")
    return None


class Connection:
    def __init__(self, reader, writer, unit, timeout):
        self.reader = reader
        self.writer = writer
        self.unit = unit
        self.timeout = timeout
        self._tid = 0

    def next_tid(self):
        self._tid = (self._tid + 1) & 0xFFFF
        return self._tid

    async def read_regs(self, fc, addr, count):
        """Single request/response, outside the measured part of a run."""
        tid = self.next_tid()
        self.writer.write(build_read(tid, self.unit, fc, addr, count))
        await self.writer.drain()
        rtid, pdu = await asyncio.wait_for(read_frame(self.reader), self.timeout)
        if rtid != tid:
            raise BenchError(f"TID mismatch: sent {tid}, got {rtid}")
        exc = check_pdu(pdu, fc, count)
        if exc is not None:
            raise BenchError(f"exception {exc} reading fc={fc} addr={addr} count={count}")
        return struct.unpack(f">{count}H", pdu[2:])

    async def read_stats(self):
        r = await self.read_regs(FC_READ_INPUT, 0, STATS_REGS)
        u32 = lambda i: (r[i] << 16) | r[i + 1]
        return {"uptime_s": u32(0), "read_reqs": u32(2), "read_regs": u32(4),
                "free_heap": u32(6), "min_heap": u32(8), "data_regs": r[10]}


def plan_chunks(size, data_regs):
    """(address, n_regs) per transaction of one message; consecutive registers,
    wrapping to 0 when the next chunk would run past the data area."""
    regs_total = (size + 1) // 2
    plan, addr = [], 0
    for off in range(0, regs_total, MAX_REGS):
        n = min(MAX_REGS, regs_total - off)
        if addr + n > data_regs:
            addr = 0
        plan.append((addr, n))
        addr += n
    return plan


def percentile(sorted_vals, q):
    return sorted_vals[min(len(sorted_vals) - 1, int(q * len(sorted_vals)))]


async def run_once(args, size, inflight):
    reader, writer = await asyncio.wait_for(
        asyncio.open_connection(args.host, args.port), args.timeout)
    writer.get_extra_info("socket").setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    conn = Connection(reader, writer, args.unit, args.timeout)

    try:
        before = await conn.read_stats()
        plan = plan_chunks(size, before["data_regs"])
        total_tx = len(plan) * args.count

        window = asyncio.Semaphore(inflight)
        pending = {}  # tid -> (t_send_ns, n_regs)
        latencies_ns = []
        exceptions = 0
        payload_bytes = 0

        async def sender():
            t0 = time.perf_counter()
            for m in range(args.count):
                if args.rate:
                    delay = t0 + m / args.rate - time.perf_counter()
                    if delay > 0:
                        await asyncio.sleep(delay)
                for addr, n in plan:
                    await window.acquire()
                    tid = conn.next_tid()
                    pending[tid] = (time.perf_counter_ns(), n)
                    writer.write(build_read(tid, args.unit, FC_READ_HOLDING, addr, n))
                await writer.drain()

        # The receiver may also be idle while the sender waits for the next rate slot
        recv_timeout = args.timeout + (1 / args.rate if args.rate else 0)

        async def receiver():
            nonlocal exceptions, payload_bytes
            for _ in range(total_tx):
                tid, pdu = await asyncio.wait_for(read_frame(reader), recv_timeout)
                t_recv = time.perf_counter_ns()
                entry = pending.pop(tid, None)
                if entry is None:
                    raise BenchError(f"response with unknown TID {tid}")
                t_send, n = entry
                if check_pdu(pdu, FC_READ_HOLDING, n) is None:
                    payload_bytes += 2 * n
                else:
                    exceptions += 1
                latencies_ns.append(t_recv - t_send)
                window.release()

        t_start = time.perf_counter()
        try:
            async with asyncio.TaskGroup() as tg:
                tg.create_task(sender())
                tg.create_task(receiver())
        except* (BenchError, asyncio.TimeoutError, asyncio.IncompleteReadError, ConnectionError) as eg:
            err = eg.exceptions[0]
            raise BenchError(f"{type(err).__name__}: {err} after {len(latencies_ns)}/{total_tx} responses")
        elapsed = time.perf_counter() - t_start

        await asyncio.sleep(STATS_SETTLE_S)
        after = await conn.read_stats()
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except ConnectionError:
            pass

    lat_ms = sorted(ns / 1e6 for ns in latencies_ns)
    return {
        "size": size,
        "regs_per_msg": sum(n for _, n in plan),
        "tx_per_msg": len(plan),
        "inflight": inflight,
        "rate": args.rate,
        "count": args.count,
        "elapsed_s": round(elapsed, 4),
        "msg_per_s": round(args.count / elapsed, 1),
        "tx_per_s": round(total_tx / elapsed, 1),
        "payload_Bps": round(payload_bytes / elapsed),
        "rtt_min_ms": round(lat_ms[0], 3),
        "rtt_avg_ms": round(sum(lat_ms) / len(lat_ms), 3),
        "rtt_p50_ms": round(percentile(lat_ms, 0.50), 3),
        "rtt_p99_ms": round(percentile(lat_ms, 0.99), 3),
        "rtt_max_ms": round(lat_ms[-1], 3),
        "exceptions": exceptions,
        "esp_reqs": after["read_reqs"] - before["read_reqs"],
        "esp_free_heap": after["free_heap"],
        "esp_min_heap": after["min_heap"],
    }


def int_list(text):
    values = [int(v) for v in text.split(",")]
    if any(v < 1 for v in values):
        raise argparse.ArgumentTypeError("values must be >= 1")
    return values


def parse_args():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("host", help="ESP32 IP address")
    p.add_argument("--port", type=int, default=502)
    p.add_argument("--unit", type=int, default=1, help="unit identifier")
    p.add_argument("--size", type=int_list, default=[250],
                   help="message size in bytes, comma-separated for a sweep (odd sizes round up to whole registers)")
    p.add_argument("--inflight", type=int_list, default=[1],
                   help="outstanding transactions, comma-separated for a sweep")
    p.add_argument("--count", type=int, default=1000, help="messages per run")
    p.add_argument("--rate", type=float, default=0, help="messages per second, 0 = as fast as the window allows")
    p.add_argument("--timeout", type=float, default=5.0, help="seconds to wait for any single response")
    p.add_argument("--gap", type=float, default=1.0, help="pause between runs, s (separates runs in IO graphs)")
    p.add_argument("--csv", help="append one row per run to this file")
    return p.parse_args()


async def main():
    args = parse_args()
    runs = [(s, w) for s in args.size for w in args.inflight]
    failed = 0

    for i, (size, inflight) in enumerate(runs):
        if i:
            await asyncio.sleep(args.gap)
        try:
            res = await run_once(args, size, inflight)
        except (BenchError, OSError, asyncio.TimeoutError, asyncio.IncompleteReadError) as e:
            failed += 1
            print(f"size={size} inflight={inflight} FAILED: {e}", file=sys.stderr)
            continue

        print(" ".join(f"{k}={v}" for k, v in res.items()), flush=True)
        if args.csv:
            new_file = not os.path.exists(args.csv)
            with open(args.csv, "a", newline="") as f:
                w = csv.DictWriter(f, fieldnames=list(res))
                if new_file:
                    w.writeheader()
                w.writerow(res)

    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
