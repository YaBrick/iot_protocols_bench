"""MQTT bench (app/esp32_mqtt_bench) from a capture taken on the broker host.

Sources, all inside the capture:
  bench/data     every PUBLISH, its QoS handshake on both links, the
                 "<run>:<seq>:<tx_tick>|" payload header
  bench/sub/log  the subscriber's rx_tick per message (sent after "sub done")
  bench/status   run parameters and the boards' own summaries
  bench/ctrl     the command, if the capture started before it was sent

Times:
  tick           the 100 kHz reference clock both boards count (1 tick = 10 us)
  capture        the RPi clock dumpcap stamps frames with. Ticks are put on it
                 with the clock_button.py log (start_ns) when there is one,
                 otherwise with an offset estimated from the capture itself:
                 every message reached the RPi after tx_tick and left it before
                 rx_tick, which bounds the offset from both sides.
Per packet, the capture time of its last byte is used: when an incoming packet
was complete at the RPi, or an outgoing one had fully left.
"""

import bisect
import re
import statistics
import struct
from collections import defaultdict
from dataclasses import dataclass

from pcap import tcp_streams

BROKER_PORT = 1883
T_CTRL, T_STATUS, T_DATA, T_LOG = "bench/ctrl", "bench/status", "bench/data", "bench/sub/log"
CONNECT, PUBLISH, PUBACK, PUBREC, PUBREL, PUBCOMP = 1, 3, 4, 5, 6, 7
SUBSCRIBE, SUBACK, UNSUBSCRIBE, UNSUBACK, PINGREQ, PINGRESP, DISCONNECT, AUTH = 8, 9, 10, 11, 12, 13, 14, 15
HDR_RE = re.compile(rb"(\d+):(\d+):(\d+)\|")
DEFAULT_CLOCK_HZ = 100_000


# ---------------------------------------------------------------- MQTT framing

@dataclass(slots=True)
class MqttPacket:
    type: int
    flags: int
    ts: int                # capture time of the last byte
    pid: int | None = None
    topic: str | None = None
    payload: bytes = b""

    @property
    def qos(self):
        return (self.flags >> 1) & 3

    @property
    def dup(self):
        return bool(self.flags & 8)


_SHORT = object()  # the packet runs past the end of the available bytes
_FIXED_FLAGS = {PUBREL: 2, SUBSCRIBE: 2, UNSUBSCRIBE: 2}
_PID_TYPES = (PUBACK, PUBREC, PUBREL, PUBCOMP, SUBSCRIBE, SUBACK, UNSUBSCRIBE, UNSUBACK)


def _varint(buf, j):
    """MQTT variable byte integer at buf[j]: (value, next index), None if invalid, _SHORT."""
    value, shift = 0, 0
    while True:
        if j >= len(buf):
            return _SHORT
        b = buf[j]
        j += 1
        value |= (b & 0x7F) << shift
        if not b & 0x80:
            return value, j
        shift += 7
        if shift > 21:
            return None


def _parse_at(buf, i, v5):
    """(type, flags, end, pid, topic, payload) of the packet at buf[i];
    None if these bytes cannot start one, _SHORT if it is cut off."""
    ptype, flags = buf[i] >> 4, buf[i] & 0x0F
    if ptype == 0 or (ptype == AUTH and not v5):
        return None
    if ptype == PUBLISH:
        if flags & 6 == 6:  # QoS 3
            return None
    elif flags != _FIXED_FLAGS.get(ptype, 0):
        return None
    r = _varint(buf, i + 1)
    if r is None or r is _SHORT:
        return r
    rl, j = r
    end = j + rl
    # Strict sizes keep resynchronisation from locking onto payload bytes
    if ptype in (PUBACK, PUBREC, PUBREL, PUBCOMP) and (rl < 2 or (rl != 2 and not v5)):
        return None
    if ptype in (PINGREQ, PINGRESP) and rl != 0:
        return None
    if ptype == DISCONNECT and rl != 0 and not v5:
        return None
    if end > len(buf):
        return _SHORT

    pid, topic, payload = None, None, b""
    if ptype == PUBLISH:
        if rl < 2:
            return None
        k = j + 2 + struct.unpack_from(">H", buf, j)[0]
        if k > end:
            return None
        try:
            topic = bytes(buf[j + 2:k]).decode("utf-8")
        except UnicodeDecodeError:
            return None
        if flags & 6:
            if k + 2 > end:
                return None
            pid = struct.unpack_from(">H", buf, k)[0]
            k += 2
        if v5:
            r = _varint(buf, k)
            if r is None or r is _SHORT or r[1] + r[0] > end:
                return None
            k = r[1] + r[0]
        payload = bytes(buf[k:end])
    elif ptype in _PID_TYPES:
        pid = struct.unpack_from(">H", buf, j)[0]
    elif ptype == CONNECT:
        payload = bytes(buf[j:end])
    return ptype, flags, end, pid, topic, payload


def parse_stream(stream, v5=False):
    """MQTT packets of one reassembled TCP direction. After a gap, or bytes
    that are no MQTT packet (the capture started mid-packet), parsing resumes
    at the next TCP segment boundary. Returns (packets, skipped bytes)."""
    out, skipped = [], 0
    for ch in stream.chunks:
        buf, base = ch.data, ch.offset
        bounds = [o - base for o in ch.seg_off]
        i = 0
        while i < len(buf):
            r = _parse_at(buf, i, v5)
            if r is _SHORT:
                skipped += len(buf) - i
                break
            if r is None:
                k = bisect.bisect_right(bounds, i)
                nxt = bounds[k] if k < len(bounds) else len(buf)
                skipped += nxt - i
                i = nxt
                continue
            ptype, flags, end, pid, topic, payload = r
            out.append(MqttPacket(ptype, flags, ch.ts_at(base + end - 1), pid, topic, payload))
            i = end
    return out, skipped


def _is_v5(packets):
    for p in packets:
        if p.type == CONNECT and len(p.payload) >= 7:
            return p.payload[6] == 5  # protocol name "MQTT" (2 + 4 bytes), then the level
    return False


# ---------------------------------------------------------------- QoS handshakes

@dataclass(slots=True)
class Txn:
    """One bench/data PUBLISH on one link and its QoS handshake."""
    run: int
    seq: int
    tx_tick: int
    pid: int | None
    qos: int
    t_pub: int                # PUBLISH
    t_ack: int | None = None  # PUBACK (QoS 1) or PUBREC (QoS 2)
    t_rel: int | None = None  # PUBREL
    t_comp: int | None = None  # PUBCOMP
    dups: int = 0             # resent PUBLISHes with the DUP flag

    @property
    def t_done(self):
        return self.t_ack if self.qos == 1 else self.t_comp


def transactions(sender, receiver):
    """Pairs bench/data PUBLISHes with their handshakes on one link. `sender`
    are the packets of the publishing side (PUBLISH, PUBREL), `receiver` those
    of the other side (PUBACK, PUBREC, PUBCOMP). Packet ids are reused, so each
    id keeps a FIFO of open exchanges. Returns (txns, unmatched packets)."""
    events = sorted([(p.ts, 0, p) for p in sender] + [(p.ts, 1, p) for p in receiver],
                    key=lambda e: (e[0], e[1]))
    open_ = defaultdict(list)
    txns, unmatched = [], 0

    def first(pid, cond):
        return next((t for t in open_.get(pid, ()) if cond(t)), None)

    for ts, side, p in events:
        if side == 0 and p.type == PUBLISH and p.topic == T_DATA:
            m = HDR_RE.match(p.payload)
            if not m:
                unmatched += 1
                continue
            run, seq, tick = map(int, m.groups())
            if p.dup:
                t = first(p.pid, lambda t: t.run == run and t.seq == seq)
                if t:
                    t.dups += 1
                    continue
            t = Txn(run, seq, tick, p.pid, p.qos, ts)
            txns.append(t)
            if p.qos:
                open_[p.pid].append(t)
        elif side == 1 and p.type in (PUBACK, PUBREC):
            t = first(p.pid, lambda t: t.t_ack is None)
            if t is None:
                unmatched += 1  # e.g. a second PUBREC answering a DUP PUBLISH
                continue
            t.t_ack = ts
            if p.type == PUBACK:
                open_[p.pid].remove(t)
        elif side == 0 and p.type == PUBREL:
            t = first(p.pid, lambda t: t.t_ack is not None and t.t_rel is None)
            if t is None:
                unmatched += 1
                continue
            t.t_rel = ts
        elif side == 1 and p.type == PUBCOMP:
            t = first(p.pid, lambda t: t.t_rel is not None and t.t_comp is None)
            if t is None:
                unmatched += 1
                continue
            t.t_comp = ts
            open_[p.pid].remove(t)
    return txns, unmatched


# ---------------------------------------------------------------- bench text

def parse_kv(text):
    """'pub done run=4 sent=2000' -> ('pub', 'done', {'run': 4, 'sent': 2000})"""
    words = text.split()
    kv = {}
    for w in words:
        k, eq, v = w.partition("=")
        if eq:
            kv[k] = int(v) if v.isdigit() else v
    plain = [w for w in words if "=" not in w]
    return (plain + ["", ""])[0], (plain + ["", ""])[1], kv


def parse_sub_log(payloads):
    """bench/sub/log messages, in order -> {run: {'clock_hz', 'rows', 'complete'}}.
    Each message is a CSV fragment without the trailing newline."""
    runs, cur = {}, None
    for line in "\n".join(p.decode("ascii", "replace") for p in payloads).splitlines():
        if line.startswith("# run="):
            _, _, kv = parse_kv(line[2:])
            cur = {"clock_hz": kv.get("clock_hz", DEFAULT_CLOCK_HZ), "rows": [], "complete": False}
            runs[kv["run"]] = cur
        elif line.startswith("# end") and cur is not None:
            cur["complete"] = parse_kv(line[2:])[2].get("lines") == len(cur["rows"])
            cur = None
        elif line[:1].isdigit() and cur is not None:
            try:
                seq, tx, rx = map(int, line.split(","))
            except ValueError:
                continue  # cut by a capture gap
            cur["rows"].append((seq, tx, rx))
    return runs


# ---------------------------------------------------------------- statistics

def dist(prefix, values, scale=1e-3, ndigits=3):
    """min/p50/p90/p99/max/mean/std of `values` (us, in message order) and
    jitter: the mean |difference| between consecutive values, i.e. the
    RFC 3550 interarrival jitter without its 1/16 smoothing. Scaled to ms."""
    keys = ("min", "p50", "p90", "p99", "max", "mean", "std", "jitter")
    if not values:
        return {f"{prefix}_{k}_ms": None for k in keys}
    q = statistics.quantiles(values, n=100, method="inclusive") if len(values) > 1 else [values[0]] * 99
    diffs = [abs(b - a) for a, b in zip(values, values[1:])]
    vals = (min(values), q[49], q[89], q[98], max(values), statistics.fmean(values),
            statistics.pstdev(values), statistics.fmean(diffs) if diffs else 0.0)
    return {f"{prefix}_{k}_ms": round(v * scale, ndigits) for k, v in zip(keys, vals)}


def max_concurrent(intervals):
    """Largest number of (start, end) intervals open at once."""
    ev = sorted([(s, 1) for s, e in intervals] + [(e, -1) for s, e in intervals])
    cur = best = 0
    for _, d in ev:
        cur += d
        best = max(best, cur)
    return best


# ---------------------------------------------------------------- analysis

def _offset(rows, clock_rows, t_first):
    """Capture time of tick 0, in ns: (offset, error, source, consistency note)."""
    lo = max((r["t_out"] - r["rx_ns"] for r in rows if r["t_out"] and r["rx_ns"] is not None), default=None)
    hi = min((r["t_in"] - r["tx_ns"] for r in rows if r["t_in"]), default=None)
    note = ""
    if lo is not None and hi is not None and lo > hi:
        note = f"capture contradicts ticks by {(lo - hi) / 1e3:.0f} us"

    clock = next((c for c in clock_rows if c["start_ns"] <= t_first <= c.get("stop_ns", t_first)), None)
    if clock:
        err = clock["start_err_ns"] + 1_000_000_000 // clock["freq_hz"]
        off = clock["start_ns"]
        if (lo is not None and off < lo - err) or (hi is not None and off > hi + err):
            note = (note + "; " if note else "") + "clock log outside the capture bounds"
        return off, err, "clock_log", note
    if lo is None or hi is None:
        return None, None, "none", note
    return (lo + hi) // 2, abs(hi - lo) // 2, "estimated", note


def analyze(packets, capture, clock_rows=()):
    """Per run: (summary dict, per-message rows). Empty if the capture has no
    bench/data traffic."""
    streams = tcp_streams(packets, BROKER_PORT)
    conns = defaultdict(dict)  # conn -> {"up": client->broker, "down": broker->client}
    for s in streams:
        conns[s.conn]["up" if s.dport == BROKER_PORT else "down"] = s

    parsed, skipped = {}, 0
    for c, d in conns.items():
        up, sk_up = parse_stream(d["up"])
        v5 = _is_v5(up)
        if v5:
            up, sk_up = parse_stream(d["up"], v5)
        down, sk_down = parse_stream(d["down"], v5)
        parsed[c] = (up, down)
        skipped += sk_up + sk_down

    def published(pkts, topic):
        return [p for p in pkts if p.type == PUBLISH and p.topic == topic]

    pub_conns = [c for c, (up, _) in parsed.items() if published(up, T_DATA)]
    sub_conns = [c for c, (_, down) in parsed.items() if published(down, T_DATA)]
    if not pub_conns:
        return []

    status, ctrl, log_payloads = [], [], []
    for c, (up, _) in parsed.items():
        status += [(p.ts, *parse_kv(p.payload.decode("ascii", "replace"))) for p in published(up, T_STATUS)]
        ctrl += [(p.ts, p.payload.decode("ascii", "replace")) for p in published(up, T_CTRL)]
    for c in sub_conns:
        log_payloads += [p.payload for p in published(parsed[c][0], T_LOG)]
    status.sort(key=lambda s: s[0])
    ctrl.sort()
    sub_logs = parse_sub_log(log_payloads)

    pub_txns, sub_txns, unmatched = {}, {}, 0
    for conns_, store, sender_is_client in ((pub_conns, pub_txns, True), (sub_conns, sub_txns, False)):
        for c in conns_:
            up, down = parsed[c]
            txns, um = transactions(up, down) if sender_is_client else transactions(down, up)
            unmatched += um
            for t in txns:
                prev = store.get((t.run, t.seq))
                if prev:  # resent on a new connection after a reconnect
                    prev.dups += 1 + t.dups
                else:
                    store[(t.run, t.seq)] = t

    results = []
    for run in sorted({r for r, _ in pub_txns}):
        results.append(_analyze_run(run, capture, conns, parsed, pub_conns, sub_conns, pub_txns,
                                    sub_txns, status, ctrl, sub_logs.get(run), clock_rows, skipped))
    return results


def _analyze_run(run, capture, conns, parsed, pub_conns, sub_conns, pub_txns, sub_txns,
                 status, ctrl, log, clock_rows, skipped):
    pt = {s: t for (r, s), t in pub_txns.items() if r == run}
    st = {s: t for (r, s), t in sub_txns.items() if r == run}
    t_first = min(t.t_pub for t in pt.values())

    def find(role, kind):
        return next((kv for _, r, k, kv in status if r == role and k == kind and kv.get("run") == run), {})

    def before(items):
        prior = [i for i in items if i[0] <= t_first]
        return prior[-1] if prior else None

    pub_armed, pub_done, sub_done = find("pub", "armed"), find("pub", "done"), find("sub", "done")
    cmd = before([c for c in ctrl if not c[1].startswith("stop")])
    cmd_kv = parse_kv(cmd[1])[2] if cmd else {}
    sub_armed = before([(ts, kv) for ts, r, k, kv in status if r == "sub" and k == "armed"])
    sub_armed = sub_armed[1] if sub_armed else {}

    def param(key, fallback=None):
        for src in (pub_armed, cmd_kv, sub_armed):
            if key in src:
                return src[key]
        return fallback

    # Payload size is not in the header; take it from the PUBLISHes themselves
    sizes = defaultdict(int)
    for c in pub_conns:
        for p in parsed[c][0]:
            if p.type == PUBLISH and p.topic == T_DATA and p.payload.startswith(f"{run}:".encode()):
                sizes[len(p.payload)] += 1
    size = param("size", max(sizes, key=sizes.get))
    qos = param("qos", max(t.qos for t in pt.values()))
    count = param("count", (sub_done["unique"] + sub_done["lost"]) if "lost" in sub_done else max(pt) + 1)

    clock_hz = log["clock_hz"] if log else DEFAULT_CLOCK_HZ
    to_ns = lambda tick: tick * 1_000_000_000 // clock_hz
    rx = defaultdict(list)
    for seq, tx_tick, rx_tick in (log["rows"] if log else []):
        rx[seq].append(rx_tick)

    rows = []
    for seq in sorted(set(pt) | set(st) | set(rx)):
        p, s = pt.get(seq), st.get(seq)
        tx_tick = p.tx_tick if p else (s.tx_tick if s else None)
        rx_tick = min(rx[seq]) if rx[seq] else None
        rows.append({
            "seq": seq, "tx_tick": tx_tick, "rx_tick": rx_tick,
            "tx_ns": to_ns(tx_tick) if tx_tick is not None else None,
            "rx_ns": to_ns(rx_tick) if rx_tick is not None else None,
            "t_in": p.t_pub if p else None, "t_out": s.t_pub if s else None,
            "p": p, "s": s, "rx_count": len(rx[seq]),
        })

    off, off_err, off_src, off_note = _offset(rows, clock_rows, t_first)
    us = lambda ns: None if ns is None else round(ns / 1000)
    msgs = []
    for r in rows:
        p, s = r["p"], r["s"]
        lat = r["rx_ns"] - r["tx_ns"] if r["rx_ns"] is not None and r["tx_ns"] is not None else None
        have_off = off is not None
        msgs.append({
            "run": run, "seq": r["seq"], "tx_tick": r["tx_tick"], "rx_tick": r["rx_tick"],
            "lat_us": us(lat),
            "up_us": us(r["t_in"] - off - r["tx_ns"]) if have_off and r["t_in"] else None,
            "broker_us": us(r["t_out"] - r["t_in"]) if r["t_out"] and r["t_in"] else None,
            "down_us": us(off + r["rx_ns"] - r["t_out"]) if have_off and r["t_out"] and r["rx_ns"] is not None else None,
            "rtt_pub_us": us(p.t_rel - p.t_ack) if p and p.t_rel and p.t_ack else None,
            "rtt_sub_us": us(s.t_ack - s.t_pub) if s and s.t_ack else None,
            "pub_dups": p.dups if p else None,
            "sub_dups": s.dups if s else None,
            "rx_count": r["rx_count"],
            "t_in_ns": r["t_in"], "t_out_ns": r["t_out"],
        })

    # Window for the wire counters: first PUBLISH in to the last handshake packet of the run
    ends = [x for t in list(pt.values()) + list(st.values()) for x in (t.t_pub, t.t_ack, t.t_rel, t.t_comp) if x]
    w0, w1 = t_first, max(ends)
    dur = max(w1 - w0, 1)

    def wire(conn_ids, key):
        return sum(p.wire_len for c in conn_ids for p in conns[c][key].packets if w0 <= p.ts <= w1)

    def retrans(conn_ids, key):
        return sum(1 for c in conn_ids for t in conns[c][key].retrans_ts if w0 <= t <= w1)

    # The subscriber's own count is authoritative: its log may be cut short
    # (log_dropped), and the broker may forward messages after the sub stopped counting
    if "unique" in sub_done:
        delivered = sub_done["unique"]
    else:
        delivered = len([m for m in msgs if m["rx_count"]]) if log else len(st)
    pub_error = next((kv for _, r, k, kv in status if r == "pub" and k == "error" and kv.get("run") == run), {})
    lat = [m["lat_us"] for m in msgs if m["lat_us"] is not None]
    if lat:
        tx_first = min(r["tx_ns"] for r in rows if r["rx_ns"] is not None)
        rx_last = max(r["rx_ns"] for r in rows if r["rx_ns"] is not None)
        span = rx_last - tx_first
    else:  # no subscriber log: first PUBLISH in to last PUBLISH out, both at the RPi
        outs = [r["t_out"] for r in rows if r["t_out"]]
        span = (max(outs) - t_first) if outs else 0
    median = lambda key: (round(statistics.median(v) / 1000, 3)
                          if (v := [m[key] for m in msgs if m[key] is not None]) else None)

    summary = {
        "capture": capture, "protocol": "mqtt", "run": run,
        "size": size, "count": count, "qos": qos,
        "inflight": param("inflight"), "rate": param("rate"),
        "pub_ip": ",".join(sorted({conns[c]["up"].src for c in pub_conns})),
        "sub_ip": ",".join(sorted({conns[c]["down"].dst for c in sub_conns})),
        "end": sub_done.get("end"),
        "sent": len(pt), "forwarded": len(st), "delivered": delivered, "lost": count - delivered,
        "pub_dup_publish": sum(t.dups for t in pt.values()),
        "sub_dup_rx": sum(max(m["rx_count"] - 1, 0) for m in msgs),
        "retrans_pub_up": retrans(pub_conns, "up"), "retrans_pub_down": retrans(pub_conns, "down"),
        "retrans_sub_down": retrans(sub_conns, "down"), "retrans_sub_up": retrans(sub_conns, "up"),
        "duration_s": round(span / 1e9, 4),
        "goodput_Bps": round(delivered * size / (span / 1e9)) if span > 0 else None,
        "thr_pub_up_Bps": round(wire(pub_conns, "up") / (dur / 1e9)),
        "thr_pub_down_Bps": round(wire(pub_conns, "down") / (dur / 1e9)),
        "thr_sub_down_Bps": round(wire(sub_conns, "down") / (dur / 1e9)),
        "thr_sub_up_Bps": round(wire(sub_conns, "up") / (dur / 1e9)),
        **dist("lat", lat),
        "up_p50_ms": median("up_us"), "broker_p50_ms": median("broker_us"), "down_p50_ms": median("down_us"),
        **dist("rtt_sub", [m["rtt_sub_us"] for m in msgs if m["rtt_sub_us"] is not None]),
        **dist("rtt_pub", [m["rtt_pub_us"] for m in msgs if m["rtt_pub_us"] is not None]),
        "esp_ack_min_ms": _ms(pub_done.get("ack_min_us")) if qos else None,
        "esp_ack_avg_ms": _ms(pub_done.get("ack_avg_us")) if qos else None,
        "esp_ack_max_ms": _ms(pub_done.get("ack_max_us")) if qos else None,
        "esp_goodput_Bps": sub_done.get("payload_Bps"),
        "inflight_obs_max": max_concurrent([(t.t_pub, t.t_done) for t in pt.values() if t.t_done]),
        "offset_source": off_src,
        "offset_err_ms": round(off_err / 1e6, 3) if off_err is not None else None,
        "notes": "; ".join(n for n in (
            f"sub ended early (end={sub_done['end']}), broker forwarded {len(st)}"
            if sub_done.get("end") not in (None, "complete") else "",
            "no sub done in the capture" if not sub_done else "",
            f"pub error reason={pub_error.get('reason')}" if pub_error else
            ("no pub done in the capture" if not pub_done else ""),
            f"sub log dropped {sub_done['log_dropped']} records" if sub_done.get("log_dropped") else "",
            off_note,
            "" if log is None or log["complete"] else "subscriber log incomplete",
            "no subscriber log" if log is None else "",
            f"{skipped} stream bytes skipped" if skipped else "",
            "params inferred (capture started after the command)" if not pub_armed and not cmd_kv else "",
        ) if n),
    }
    return summary, msgs


def _ms(v):
    return round(v / 1000, 3) if isinstance(v, int) else None
