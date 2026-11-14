"""Capture reading for the bench analysis: pcap/pcapng files, IPv4 TCP/UDP
decoding and TCP stream reassembly. Standard library only.

Timestamps stay integer nanoseconds since the epoch, the same value Wireshark
shows as frame.time_epoch, so rows can be looked up in the GUI.
"""

import bisect
import struct
from dataclasses import dataclass, field

TCP, UDP = 6, 17
SYN = 0x02

# Link-layer types (LINKTYPE_*) that are decoded
LINK_ETHERNET = 1
LINK_RAW = 101
LINK_LINUX_SLL = 113
LINK_LINUX_SLL2 = 276


@dataclass(slots=True)
class Packet:
    n: int          # frame number, 1-based (Wireshark's frame.number)
    ts: int         # capture time, ns since the epoch
    wire_len: int   # original frame length, bytes (frame.len), link header included
    src: str
    dst: str
    proto: int      # TCP or UDP
    sport: int
    dport: int
    seq: int = 0
    ack: int = 0
    flags: int = 0
    payload: bytes = b""


# ---------------------------------------------------------------- file formats

def _pcapng_blocks(data):
    """Yields (ts_ns, linktype, orig_len, frame) from a pcapng file."""
    off = 0
    endian = "<"
    ifaces = []  # per interface: (linktype, ts units per second, ts offset in s)
    while off + 12 <= len(data):
        btype, blen = struct.unpack_from(endian + "II", data, off)
        if btype == 0x0A0D0D0A:  # section header: byte order may change, interfaces restart
            endian = "<" if data[off + 8:off + 12] == b"\x4d\x3c\x2b\x1a" else ">"
            btype, blen = struct.unpack_from(endian + "II", data, off)
            ifaces = []
        if blen < 12:
            raise ValueError(f"corrupt pcapng block at offset {off}")

        if btype == 1:  # interface description
            linktype = struct.unpack_from(endian + "H", data, off + 8)[0]
            units, ts_off = 1_000_000, 0
            o, end = off + 16, off + blen - 4
            while o + 4 <= end:
                code, olen = struct.unpack_from(endian + "HH", data, o)
                if code == 0:
                    break
                if code == 9:  # if_tsresol
                    v = data[o + 4]
                    units = 2 ** (v & 0x7F) if v & 0x80 else 10 ** v
                elif code == 14:  # if_tsoffset
                    ts_off = struct.unpack_from(endian + "q", data, o + 4)[0]
                o += 4 + ((olen + 3) & ~3)
            ifaces.append((linktype, units, ts_off))
        elif btype in (6, 2):  # enhanced packet, obsolete packet
            if btype == 6:
                ifid, th, tl, caplen, orig = struct.unpack_from(endian + "IIIII", data, off + 8)
            else:
                ifid, _drops, th, tl, caplen, orig = struct.unpack_from(endian + "HHIIII", data, off + 8)
            linktype, units, ts_off = ifaces[ifid]
            raw = (th << 32) | tl
            ts = raw * 1_000_000_000 // units + ts_off * 1_000_000_000
            yield ts, linktype, orig, data[off + 28:off + 28 + caplen]
        off += blen


def _pcap_records(data):
    """Yields (ts_ns, linktype, orig_len, frame) from a classic pcap file."""
    magic = data[:4]
    endian = "<" if magic in (b"\xd4\xc3\xb2\xa1", b"\x4d\x3c\xb2\xa1") else ">"
    nano = magic in (b"\x4d\x3c\xb2\xa1", b"\xa1\xb2\x3c\x4d")
    linktype = struct.unpack_from(endian + "I", data, 20)[0] & 0x0FFFFFFF
    off = 24
    while off + 16 <= len(data):
        sec, frac, caplen, orig = struct.unpack_from(endian + "IIII", data, off)
        ts = sec * 1_000_000_000 + (frac if nano else frac * 1000)
        yield ts, linktype, orig, data[off + 16:off + 16 + caplen]
        off += 16 + caplen


def _l3_offset(linktype, frame):
    """Returns (offset of the IPv4 header, ethertype) or None for anything else."""
    if linktype == LINK_ETHERNET:
        off, etype = 14, struct.unpack_from(">H", frame, 12)[0]
        while etype in (0x8100, 0x88A8):  # VLAN tags
            etype = struct.unpack_from(">H", frame, off + 2)[0]
            off += 4
    elif linktype == LINK_LINUX_SLL:
        off, etype = 16, struct.unpack_from(">H", frame, 14)[0]
    elif linktype == LINK_LINUX_SLL2:
        off, etype = 20, struct.unpack_from(">H", frame, 0)[0]
    elif linktype == LINK_RAW:
        off, etype = 0, 0x0800 if frame and frame[0] >> 4 == 4 else 0
    else:
        raise ValueError(f"unsupported link type {linktype}")
    return off, etype


def read_packets(path):
    """All IPv4 TCP and UDP packets of a capture, in file order."""
    with open(path, "rb") as f:
        data = f.read()
    if data[:4] == b"\x0a\x0d\x0d\x0a":
        records = _pcapng_blocks(data)
    elif data[:4] in (b"\xd4\xc3\xb2\xa1", b"\xa1\xb2\xc3\xd4", b"\x4d\x3c\xb2\xa1", b"\xa1\xb2\x3c\x4d"):
        records = _pcap_records(data)
    else:
        raise ValueError(f"{path}: not a pcap or pcapng file")

    packets = []
    for n, (ts, linktype, orig, frame) in enumerate(records, 1):
        off, etype = _l3_offset(linktype, frame)
        if etype != 0x0800 or len(frame) < off + 20:
            continue
        ip = frame[off:]
        ihl = (ip[0] & 0x0F) * 4
        total = struct.unpack_from(">H", ip, 2)[0]
        frag = struct.unpack_from(">H", ip, 6)[0]
        proto = ip[9]
        if frag & 0x1FFF or proto not in (TCP, UDP):  # non-first fragments carry no L4 header
            continue
        l4 = ip[ihl:total]
        src = ".".join(map(str, ip[12:16]))
        dst = ".".join(map(str, ip[16:20]))
        if proto == TCP:
            sport, dport, seq, ack = struct.unpack_from(">HHII", l4)
            doff = (l4[12] >> 4) * 4
            packets.append(Packet(n, ts, orig, src, dst, TCP, sport, dport, seq, ack, l4[13], bytes(l4[doff:])))
        else:
            sport, dport = struct.unpack_from(">HH", l4)
            packets.append(Packet(n, ts, orig, src, dst, UDP, sport, dport, payload=bytes(l4[8:])))
    return packets


# ---------------------------------------------------------------- TCP reassembly

@dataclass(slots=True)
class Chunk:
    """A gap-free piece of a TCP byte stream."""
    offset: int                                   # stream offset of data[0]
    data: bytearray = field(default_factory=bytearray)
    seg_off: list = field(default_factory=list)   # stream offset where each segment's new bytes start
    seg_ts: list = field(default_factory=list)    # capture time of that segment

    def ts_at(self, stream_off):
        """Capture time of the segment that first carried this byte."""
        return self.seg_ts[bisect.bisect_right(self.seg_off, stream_off) - 1]


@dataclass(slots=True)
class Stream:
    """One direction of one TCP connection."""
    conn: int        # connection index; both directions share it
    src: str
    sport: int
    dst: str
    dport: int
    packets: list    # every segment of this direction, pure ACKs included, file order
    chunks: list = field(default_factory=list)
    retrans_ts: list = field(default_factory=list)  # capture times of retransmitted segments
    gaps: int = 0    # holes in the stream: bytes never captured


class _Coverage:
    """Byte ranges of a stream seen so far, as sorted disjoint intervals."""

    def __init__(self):
        self.starts = []
        self.ends = []

    def add(self, s, e):
        """Marks [s, e) as seen and returns the sub-ranges not seen before."""
        i = bisect.bisect_left(self.ends, s)
        new, pos, j = [], s, i
        while j < len(self.starts) and self.starts[j] <= e:
            if self.starts[j] > pos:
                new.append((pos, self.starts[j]))
            pos = max(pos, self.ends[j])
            j += 1
        if pos < e:
            new.append((pos, e))
        if i < j:
            s, e = min(s, self.starts[i]), max(e, self.ends[j - 1])
        self.starts[i:j] = [s]
        self.ends[i:j] = [e]
        return new


def _reassemble(stream):
    segs = [p for p in stream.packets if p.payload]
    if not segs:
        return
    syn = next((p for p in stream.packets if p.flags & SYN), None)
    base = (syn.seq + 1) if syn else segs[0].seq

    # Walk in capture order, so every byte keeps the time it was first seen at
    cover = _Coverage()
    pieces = []
    top = None  # highest stream offset seen so far
    for p in segs:
        s = ((p.seq - base + 2**31) % 2**32) - 2**31  # signed distance, survives wraparound
        e = s + len(p.payload)
        new = cover.add(s, e)
        # Repeats bytes already captured (lost after the capture point), or fills
        # a hole (the original was lost before it, e.g. on the air into the RPi)
        if new != [(s, e)] or (top is not None and s < top):
            stream.retrans_ts.append(p.ts)
        top = e if top is None else max(top, e)
        for a, b in new:
            pieces.append((a, p.payload[a - s:b - s], p.ts))

    pieces.sort(key=lambda x: x[0])
    chunk = None
    for off, data, ts in pieces:
        if chunk is None or off != chunk.offset + len(chunk.data):
            if chunk is not None:
                stream.gaps += 1
            chunk = Chunk(off)
            stream.chunks.append(chunk)
        chunk.seg_off.append(off)
        chunk.seg_ts.append(ts)
        chunk.data += data


def tcp_streams(packets, port=None):
    """Reassembled TCP streams, two per connection. With `port`, only
    connections that have it on one side."""
    by_tuple = {}
    conns = []  # per connection: {direction tuple: Stream}
    for p in packets:
        if p.proto != TCP or (port is not None and port not in (p.sport, p.dport)):
            continue
        key = (p.src, p.sport, p.dst, p.dport)
        rkey = (p.dst, p.dport, p.src, p.sport)
        idx = by_tuple.get(key)
        # A bare SYN on a known tuple opens a new connection (port reuse),
        # unless it repeats the SYN that opened the current one
        prev = conns[idx][key].packets if idx is not None else None
        if idx is None or (p.flags & SYN and not p.flags & 0x10 and prev and prev[0].seq != p.seq):
            idx = len(conns)
            conns.append({key: Stream(idx, *key, []), rkey: Stream(idx, *rkey, [])})
            by_tuple[key] = by_tuple[rkey] = idx
        conns[idx][key].packets.append(p)

    streams = [s for c in conns for s in c.values()]
    for s in streams:
        _reassemble(s)
    return streams
