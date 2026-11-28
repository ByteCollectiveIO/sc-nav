#!/usr/bin/env python3
"""Record Star Citizen's local telemetry stream (gRPC on 127.0.0.1:2942) while
you play, so we can see what it really carries before designing anything on it.

Research notes: CIG ships an undocumented local gRPC server in the game client,
`sc.game.telemetry.v1.GameTelemetryService` (4.10 LIVE = read-only stream, 5.0
PTU adds actions). Third-party write-ups disagree on what's in it per build
(e.g. location/jurisdiction/quantum-travel fields reportedly appeared in PTU
CL 13273298), and none of them answer our questions: does `uec_balance` move by
exactly the price + auto-load fee around a kiosk transaction? Is there any
position or location name? This tool records the evidence.

READ-ONLY. It calls `TelemetryStream` and `ListTriggerableActions` (a list,
nothing is pressed). It never calls `TriggerGameAction`; that code path doesn't
exist here. No third-party packages: gRPC is spoken over plain HTTP/2 by hand,
like the watcher's no-pip-install rule.

Enable the server first (game fully closed):
    LIVE 4.10.x : PowerShell `setx GRPC_CLIENT_SERVER_ENABLED true`, then quit
                  the RSI Launcher completely (tray icon too) and start it again.
    PTU 5.0     : add to user.cfg in the channel folder (…\\StarCitizen\\PTU\\):
                      grpc.client.server.enabled = 1
                      grpc.client.server.port = 2942
                      server.game.telemetry.enabled = 1
Game.log then says `<GameTelemetry> Local services server is running on
127.0.0.1:2942`. PTU 5.0 builds from CL 13273298 (2026-11-24; still so on
13284307) read those keys and then log `<GRPC Server> No services configured`:
the client does not start telemetry at all, and no setting brings it back. The stream only produces frames once your character spawns.

Usage (on the gaming PC):
    python tools/telemetry_capture.py [--game-log PATH] [--exe PATH] [--port N]
                                      [--out DIR] [--duration SECONDS]
    python tools/telemetry_capture.py --schema-only [--exe PATH]
    python tools/telemetry_capture.py --replay SESSION_DIR [--schema-dir DIR]

While it runs, type a note + Enter to drop a timestamped marker ("bought 32
SCU agri, auto-load ON"), or `q` + Enter / Ctrl+C to stop and write the report.
It waits for the game and reconnects on its own, so start it whenever.

The schema (field names) is read out of StarCitizen.exe: protoc-generated C++
embeds each .proto as a serialized FileDescriptorProto. If that fails, frames
are still recorded and decoded by field NUMBER, and `--replay` can re-decode
them later with a schema from another build.

Output (default `../game_logs/telemetry/session-<utc>/`, outside the repo like
the other captured logs; it holds your aUEC balance and player name, so it is
never committed):
    frames.bin      every raw frame, timestamped (re-decodable with --replay)
    changes.jsonl   one line per frame that changed something (first = snapshot);
                    null = field went absent (proto3 omits zero/default values).
                    Float fields are written at most once per --float-interval
                    (default 1 s) so flight doesn't bury everything else
    events.jsonl    markers, aUEC balance changes, the stream's own one-frame
                    game events, connects/errors, and the Game.log lines worth
                    correlating (kiosk buy/sell, HUD notifications, QT)
    gamelog.txt     every Game.log line written while recording, verbatim
                    (the game replaces Game.log on its next launch)
    actions.json    ListTriggerableActions result, when the build has it
    schema/         extracted .proto text + raw .pb descriptors
    summary.md      every field path seen (presence, change count, sample),
                    schema fields that never showed up, balance deltas next to
                    the Game.log lines that explain them, game events, markers

First session (LIVE sc-alpha-4.10.0 CL 13220821, 2026-11-27, on foot + seated,
never flew): 15 frames/s from character spawn; the schema has NO position,
location, jurisdiction or quantum-travel field at all; `actor.state.uec_balance`
followed a contract fee 0.23 s after Game.log's "Contract Accepted";
ListTriggerableActions = UNIMPLEMENTED. Still unrecorded: a kiosk buy/sell.

Run tools/gamelog_live.py alongside it for the full Game.log timeline.
"""

import argparse
import json
import math
import mmap
import os
import re
import socket
import struct
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DEFAULT_OUT = REPO.parent / "game_logs" / "telemetry"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 2942
FALLBACK_SERVICE = "sc.game.telemetry.v1.GameTelemetryService"

_LOG_CANDIDATES = (
    r"C:\Program Files\Roberts Space Industries\StarCitizen\LIVE\Game.log",
    r"C:\Program Files\Roberts Space Industries\StarCitizen\PTU\Game.log",
    r"C:\Program Files\Roberts Space Industries\StarCitizen\EPTU\Game.log",
)


def utc_iso(t):
    return datetime.fromtimestamp(t, timezone.utc).isoformat(timespec="milliseconds")


def local_hms(t):
    return datetime.fromtimestamp(t).strftime("%H:%M:%S")


# ---------------------------------------------------------------------------
# Protobuf wire format
# ---------------------------------------------------------------------------

def read_varint(buf, i):
    result = shift = 0
    while True:
        if i >= len(buf):
            raise ValueError("truncated varint")
        b = buf[i]
        i += 1
        result |= (b & 0x7F) << shift
        if not b & 0x80:
            return result, i
        shift += 7
        if shift > 63:
            raise ValueError("varint too long")


def iter_fields(buf):
    """Yield (field_number, wire_type, value): int for varints, bytes otherwise.
    Raises ValueError on anything that isn't well-formed wire format."""
    i, end = 0, len(buf)
    while i < end:
        key, i = read_varint(buf, i)
        num, wt = key >> 3, key & 7
        if num == 0:
            raise ValueError("field number 0")
        if wt == 0:
            v, i = read_varint(buf, i)
        elif wt == 1:
            v, i = bytes(buf[i:i + 8]), i + 8
        elif wt == 5:
            v, i = bytes(buf[i:i + 4]), i + 4
        elif wt == 2:
            n, i = read_varint(buf, i)
            v, i = bytes(buf[i:i + n]), i + n
        else:
            raise ValueError(f"unsupported wire type {wt}")
        if i > end:
            raise ValueError("truncated field")
        yield num, wt, v


def parse_fields(buf):
    return list(iter_fields(buf))


def signed64(v):
    return v - (1 << 64) if v >= 1 << 63 else v


def signed32(v):
    v &= 0xFFFFFFFF
    return v - (1 << 32) if v >= 1 << 31 else v


def zigzag(v):
    return (v >> 1) ^ -(v & 1)


def clean_float(x):
    if math.isnan(x) or math.isinf(x):
        return str(x)
    return round(x, 4)


# ---------------------------------------------------------------------------
# Schema extraction (FileDescriptorProto embedded in StarCitizen.exe)
# ---------------------------------------------------------------------------

# FileDescriptorProto field -> wire type. protoc writes fields in number order,
# which is what lets the walker below find where one descriptor ends.
_FDP_WIRE = {1: 2, 2: 2, 3: 2, 4: 2, 5: 2, 6: 2, 7: 2, 8: 2, 9: 2,
             10: 0, 11: 0, 12: 2, 13: 2, 14: 0}
_FDP_SINGLE = {1, 2, 8, 9, 12, 13, 14}
_FDP_SUBMSG = {4, 5, 6, 7, 8, 9}
_PROTO_NAME_RE = re.compile(rb"\x0a([\x05-\x7f])([A-Za-z0-9_./-]{4,126}\.proto)")


def walk_file_descriptor(buf, start, limit=1 << 20):
    """Byte length of the serialized FileDescriptorProto starting at `start`:
    consume fields while they fit the descriptor's shape, stop at the first
    that doesn't (that's where the next blob in the binary begins)."""
    i, end = start, min(len(buf), start + limit)
    last, seen = 0, set()
    while i < end:
        try:
            key, j = read_varint(buf, i)
            num, wt = key >> 3, key & 7
            if _FDP_WIRE.get(num) != wt or num < last or (num in _FDP_SINGLE and num in seen):
                break
            if wt == 0:
                _, j = read_varint(buf, j)
            else:
                n, j = read_varint(buf, j)
                if j + n > end:
                    break
                if num in _FDP_SUBMSG:
                    parse_fields(buf[j:j + n])
                j += n
        except ValueError:
            break
        seen.add(num)
        last, i = num, j
    return i - start


def _s(b):
    return b.decode("utf-8", "replace")


def decode_file(blob):
    f = {"name": "", "package": "", "deps": [], "messages": [], "enums": [],
         "services": [], "syntax": ""}
    for num, _, v in iter_fields(blob):
        if num == 1:
            f["name"] = _s(v)
        elif num == 2:
            f["package"] = _s(v)
        elif num == 3:
            f["deps"].append(_s(v))
        elif num == 4:
            f["messages"].append(decode_message(v))
        elif num == 5:
            f["enums"].append(decode_enum(v))
        elif num == 6:
            f["services"].append(decode_service(v))
        elif num == 12:
            f["syntax"] = _s(v)
    return f


def decode_message(b):
    m = {"name": "", "fields": [], "nested": [], "enums": [], "oneofs": [], "map_entry": False}
    for num, _, v in iter_fields(b):
        if num == 1:
            m["name"] = _s(v)
        elif num == 2:
            m["fields"].append(decode_field(v))
        elif num == 3:
            m["nested"].append(decode_message(v))
        elif num == 4:
            m["enums"].append(decode_enum(v))
        elif num == 7:
            m["map_entry"] = any(n == 7 and x for n, _, x in iter_fields(v))
        elif num == 8:
            m["oneofs"].append(next((_s(x) for n, _, x in iter_fields(v) if n == 1), ""))
    return m


def decode_field(b):
    fd = {"name": "", "number": 0, "label": 1, "type": 0, "type_name": "",
          "oneof": None, "proto3_optional": False, "packed": None}
    for num, _, v in iter_fields(b):
        if num == 1:
            fd["name"] = _s(v)
        elif num == 3:
            fd["number"] = v
        elif num == 4:
            fd["label"] = v
        elif num == 5:
            fd["type"] = v
        elif num == 6:
            fd["type_name"] = _s(v)
        elif num == 8:
            for n, _, x in iter_fields(v):
                if n == 2:
                    fd["packed"] = bool(x)
        elif num == 9:
            fd["oneof"] = v
        elif num == 17:
            fd["proto3_optional"] = bool(v)
    return fd


def decode_enum(b):
    e = {"name": "", "values": []}
    for num, _, v in iter_fields(b):
        if num == 1:
            e["name"] = _s(v)
        elif num == 2:
            name, number = "", 0
            for n, _, x in iter_fields(v):
                if n == 1:
                    name = _s(x)
                elif n == 2:
                    number = signed32(x)
            e["values"].append((name, number))
    return e


def decode_service(b):
    s = {"name": "", "methods": []}
    for num, _, v in iter_fields(b):
        if num == 1:
            s["name"] = _s(v)
        elif num == 2:
            m = {"name": "", "input": "", "output": "", "client_streaming": False,
                 "server_streaming": False}
            for n, _, x in iter_fields(v):
                if n == 1:
                    m["name"] = _s(x)
                elif n == 2:
                    m["input"] = _s(x)
                elif n == 3:
                    m["output"] = _s(x)
                elif n == 5:
                    m["client_streaming"] = bool(x)
                elif n == 6:
                    m["server_streaming"] = bool(x)
            s["methods"].append(m)
    return s


def scan_descriptors(buf):
    """Every embedded FileDescriptorProto in `buf`: {name: (blob, decoded)}."""
    found = {}
    for m in _PROTO_NAME_RE.finditer(buf):
        if m.group(1)[0] != len(m.group(2)):
            continue
        name = _s(m.group(2))
        if name in found:
            continue
        n = walk_file_descriptor(buf, m.start())
        if n <= len(m.group(0)):
            continue
        blob = bytes(buf[m.start():m.start() + n])
        try:
            found[name] = (blob, decode_file(blob))
        except ValueError:
            continue
    return found


def telemetry_files(found):
    """The telemetry protos plus whatever they import that the binary also has."""
    want = [n for n, (_, f) in found.items()
            if "telemetry" in n.lower() or "telemetry" in f["package"].lower()]
    keep, todo = [], list(want)
    while todo:
        n = todo.pop()
        if n in keep or n not in found:
            continue
        keep.append(n)
        todo.extend(found[n][1]["deps"])
    return keep


def extract_schema(exe_path):
    with open(exe_path, "rb") as fh, mmap.mmap(fh.fileno(), 0, access=mmap.ACCESS_READ) as mm:
        found = scan_descriptors(mm)
    return found, telemetry_files(found)


_TYPE_NAMES = {1: "double", 2: "float", 3: "int64", 4: "uint64", 5: "int32", 6: "fixed64",
               7: "fixed32", 8: "bool", 9: "string", 12: "bytes", 13: "uint32",
               15: "sfixed32", 16: "sfixed64", 17: "sint32", 18: "sint64"}


def proto_text(f):
    """Readable .proto reconstruction (for us to read; not guaranteed to compile)."""
    out = [f'syntax = "{f["syntax"] or "proto2"}";', ""]
    if f["package"]:
        out += [f"package {f['package']};", ""]
    out += [f'import "{d}";' for d in f["deps"]]
    if f["deps"]:
        out.append("")

    def enum(e, ind):
        out.append(f"{ind}enum {e['name']} {{")
        out.extend(f"{ind}  {n} = {v};" for n, v in e["values"])
        out.append(f"{ind}}}")

    def field_line(fd, ind):
        t = _TYPE_NAMES.get(fd["type"]) or fd["type_name"] or f"type{fd['type']}"
        label = "repeated " if fd["label"] == 3 else (
            "optional " if fd["proto3_optional"] or (f["syntax"] != "proto3" and fd["label"] == 1)
            else "required " if fd["label"] == 2 else "")
        return f"{ind}{label}{t} {fd['name']} = {fd['number']};"

    def msg(m, ind):
        out.append(f"{ind}message {m['name']} {{" + ("  // map entry" if m["map_entry"] else ""))
        for e in m["enums"]:
            enum(e, ind + "  ")
        for n in m["nested"]:
            msg(n, ind + "  ")
        groups = {}
        for fd in m["fields"]:
            if fd["oneof"] is not None and not fd["proto3_optional"]:
                groups.setdefault(fd["oneof"], []).append(fd)
            else:
                out.append(field_line(fd, ind + "  "))
        for idx, fds in groups.items():
            name = m["oneofs"][idx] if idx < len(m["oneofs"]) else f"oneof_{idx}"
            out.append(f"{ind}  oneof {name} {{")
            out.extend(field_line(fd, ind + "    ") for fd in fds)
            out.append(f"{ind}  }}")
        out.append(f"{ind}}}")

    for e in f["enums"]:
        enum(e, "")
        out.append("")
    for m in f["messages"]:
        msg(m, "")
        out.append("")
    for s in f["services"]:
        out.append(f"service {s['name']} {{")
        for m in s["methods"]:
            cs = "stream " if m["client_streaming"] else ""
            ss = "stream " if m["server_streaming"] else ""
            out.append(f"  rpc {m['name']} ({cs}{m['input']}) returns ({ss}{m['output']});")
        out += ["}", ""]
    return "\n".join(out)


class Schema:
    _PACKABLE = {1, 2, 3, 4, 5, 6, 7, 8, 13, 14, 15, 16, 17, 18}
    _FIXED32 = {2, 7, 15}
    _FIXED64 = {1, 6, 16}

    def __init__(self, files):
        self.files = files
        self.messages, self.enums, self.methods = {}, {}, {}
        for f in files:
            prefix = "." + f["package"] if f["package"] else ""
            for m in f["messages"]:
                self._add_message(prefix, m)
            for e in f["enums"]:
                self.enums[f"{prefix}.{e['name']}"] = {v: n for n, v in e["values"]}
            for s in f["services"]:
                svc = f"{prefix}.{s['name']}".lstrip(".")
                for m in s["methods"]:
                    self.methods.setdefault(m["name"], dict(m, path=f"/{svc}/{m['name']}"))

    def _add_message(self, prefix, m):
        full = f"{prefix}.{m['name']}"
        self.messages[full] = {"fields": {fd["number"]: fd for fd in m["fields"]},
                               "map_entry": m["map_entry"]}
        for n in m["nested"]:
            self._add_message(full, n)
        for e in m["enums"]:
            self.enums[f"{full}.{e['name']}"] = {v: n for n, v in e["values"]}

    def leaf_paths(self, type_name, prefix="", _seen=()):
        """Every flattened field path the schema allows under `type_name`, in
        the form Recorder.stats keys take (`[]` after a repeated message)."""
        msg = self.messages.get(type_name)
        if msg is None or type_name in _seen:
            return [prefix] if prefix else []
        out = []
        for _, fd in sorted(msg["fields"].items()):
            p = f"{prefix}.{fd['name']}" if prefix else fd["name"]
            if fd["type"] == 11 and fd["type_name"] in self.messages:
                out += self.leaf_paths(fd["type_name"], p + ("[]" if fd["label"] == 3 else ""),
                                       _seen + (type_name,))
            else:
                out.append(p)
        return out

    def _scalar(self, fd, wt, v):
        t = fd["type"]
        if t == 11:
            return self.decode(v, fd["type_name"]) if wt == 2 else raw_value(wt, v)
        if t == 14:
            n = signed32(v) if wt == 0 else v
            return self.enums.get(fd["type_name"], {}).get(n, n)
        if t == 9:
            return _s(v) if wt == 2 else raw_value(wt, v)
        if t == 12:
            return v.hex() if wt == 2 else raw_value(wt, v)
        if t == 8:
            return bool(v)
        if t == 5:
            return signed32(v)
        if t == 3:
            return signed64(v)
        if t in (17, 18):
            return zigzag(v)
        if t in (4, 13):
            return v
        if t in self._FIXED32 and wt == 5:
            return clean_float(struct.unpack("<f", v)[0]) if t == 2 else struct.unpack(
                "<I" if t == 7 else "<i", v)[0]
        if t in self._FIXED64 and wt == 1:
            return clean_float(struct.unpack("<d", v)[0]) if t == 1 else struct.unpack(
                "<Q" if t == 6 else "<q", v)[0]
        return raw_value(wt, v)

    def _packed(self, fd, v):
        t, vals = fd["type"], []
        if t in self._FIXED32:
            for k in range(0, len(v) - 3, 4):
                vals.append(self._scalar(fd, 5, v[k:k + 4]))
        elif t in self._FIXED64:
            for k in range(0, len(v) - 7, 8):
                vals.append(self._scalar(fd, 1, v[k:k + 8]))
        else:
            i = 0
            while i < len(v):
                x, i = read_varint(v, i)
                vals.append(self._scalar(fd, 0, x))
        return vals

    def decode(self, buf, type_name):
        msg = self.messages.get(type_name)
        if msg is None:
            return raw_decode(buf)
        out, maps, lists = {}, set(), set()
        for num, wt, v in iter_fields(buf):
            fd = msg["fields"].get(num)
            if fd is None:
                _put(out, f"#{num}", raw_value(wt, v), lists)
                continue
            key = fd["name"]
            if wt == 2 and fd["type"] in self._PACKABLE:
                out.setdefault(key, []).extend(self._packed(fd, v))
                continue
            val = self._scalar(fd, wt, v)
            if fd["label"] == 3:
                out.setdefault(key, []).append(val)
                target = self.messages.get(fd["type_name"])
                if target and target["map_entry"]:
                    maps.add(key)
            else:
                out[key] = val
        for key in maps:
            out[key] = {str(e.get("key", "")): e.get("value") for e in out[key]
                        if isinstance(e, dict)}
        return out


def _put(out, key, val, lists):
    """Add a field; a number seen twice becomes a list (repeated field)."""
    if key in lists:
        out[key].append(val)
    elif key in out:
        out[key] = [out[key], val]
        lists.add(key)
    else:
        out[key] = val


def raw_value(wt, v):
    if wt == 0:
        return v
    if wt == 5:
        f = struct.unpack("<f", v)[0]
        if math.isfinite(f) and (f == 0 or 1e-6 < abs(f) < 1e9):
            return clean_float(f)
        return struct.unpack("<I", v)[0]
    if wt == 1:
        d = struct.unpack("<d", v)[0]
        if math.isfinite(d) and (d == 0 or 1e-9 < abs(d) < 1e15):
            return clean_float(d)
        return struct.unpack("<Q", v)[0]
    try:
        text = v.decode("utf-8")
        if text and all(c.isprintable() for c in text):
            return text
    except UnicodeDecodeError:
        pass
    try:
        return raw_decode(v) if v else ""
    except ValueError:
        return "0x" + v.hex()


def raw_decode(buf):
    """Schema-less decode: keys are field numbers."""
    out, lists = {}, set()
    for num, wt, v in iter_fields(buf):
        _put(out, str(num), raw_value(wt, v), lists)
    return out


def flatten(obj, prefix="", out=None):
    out = {} if out is None else out
    if isinstance(obj, dict):
        for k, v in obj.items():
            flatten(v, f"{prefix}.{k}" if prefix else str(k), out)
    elif isinstance(obj, list) and any(isinstance(v, (dict, list)) for v in obj):
        for i, v in enumerate(obj):
            flatten(v, f"{prefix}[{i}]", out)
    else:
        out[prefix] = obj
    return out


# ---------------------------------------------------------------------------
# HPACK (RFC 7541): just enough for a gRPC client
# ---------------------------------------------------------------------------

# Canonical Huffman code lengths for symbols 0..256 (RFC 7541 Appendix B);
# the code is canonical, so lengths alone rebuild it.
_HUFF_LENS = bytes.fromhex(
    "0d171c1c1c1c1c1c1c181e1c1c1e1c1c1c1c1c1c1c1c1e1c1c1c1c1c1c1c1c1c060a0a0c0d06080b"
    "0a0a080b080606060505050606060606060607080f060c0a0d060707070707070707070707070707"
    "07070707070707070807080d130d0e060f0506050605060606050707060606050607060505060707"
    "0707070f0b0e0d1c1416141416161617161717171717181718181617181717171715161716171718"
    "16151416161717151716161815161717151516151716171714161616171616171a1a141316171619"
    "1a1a1a1b1b1a181913151a1b1b1a1b1815151a1a1c1b1b1b14181415161515171616191918181a17"
    "1a1b1a1a1b1b1b1b1b1c1b1b1b1b1b1a1e")


def _build_huffman():
    order = sorted(range(257), key=lambda s: (_HUFF_LENS[s], s))
    table, code, length = {}, 0, _HUFF_LENS[order[0]]
    for k, sym in enumerate(order):
        if k:
            code = (code + 1) << (_HUFF_LENS[sym] - length)
            length = _HUFF_LENS[sym]
        table[(length, code)] = sym
    return table


_HUFF = _build_huffman()


def huffman_decode(data):
    out, code, n = bytearray(), 0, 0
    for byte in data:
        for bit in range(7, -1, -1):
            code, n = (code << 1) | ((byte >> bit) & 1), n + 1
            sym = _HUFF.get((n, code))
            if sym is not None:
                if sym == 256:
                    raise ValueError("EOS in Huffman string")
                out.append(sym)
                code = n = 0
    return bytes(out)


_STATIC = [(":authority", ""), (":method", "GET"), (":method", "POST"), (":path", "/"),
           (":path", "/index.html"), (":scheme", "http"), (":scheme", "https"),
           (":status", "200"), (":status", "204"), (":status", "206"), (":status", "304"),
           (":status", "400"), (":status", "404"), (":status", "500")] + [
    (n, "gzip, deflate" if n == "accept-encoding" else "") for n in (
        "accept-charset accept-encoding accept-language accept-ranges accept "
        "access-control-allow-origin age allow authorization cache-control "
        "content-disposition content-encoding content-language content-length "
        "content-location content-range content-type cookie date etag expect expires "
        "from host if-match if-modified-since if-none-match if-range "
        "if-unmodified-since last-modified link location max-forwards "
        "proxy-authenticate proxy-authorization range referer refresh retry-after "
        "server set-cookie strict-transport-security transfer-encoding user-agent "
        "vary via www-authenticate").split()]


def hpack_int(value, prefix_bits, first=0):
    cap = (1 << prefix_bits) - 1
    if value < cap:
        return bytes([first | value])
    out = bytearray([first | cap])
    value -= cap
    while value >= 128:
        out.append((value & 0x7F) | 0x80)
        value >>= 7
    out.append(value)
    return bytes(out)


def hpack_encode(pairs):
    """Literal header fields without indexing, new name, no Huffman."""
    out = bytearray()
    for k, v in pairs:
        kb, vb = k.encode(), v.encode()
        out += b"\x00" + hpack_int(len(kb), 7) + kb + hpack_int(len(vb), 7) + vb
    return bytes(out)


class HpackDecoder:
    def __init__(self):
        self.dyn, self.size, self.max = [], 0, 4096

    @staticmethod
    def _int(data, i, prefix_bits):
        cap = (1 << prefix_bits) - 1
        v = data[i] & cap
        i += 1
        if v < cap:
            return v, i
        shift = 0
        while True:
            b = data[i]
            i += 1
            v += (b & 0x7F) << shift
            shift += 7
            if not b & 0x80:
                return v, i

    def _str(self, data, i):
        huff = data[i] & 0x80
        n, i = self._int(data, i, 7)
        raw = bytes(data[i:i + n])
        return (huffman_decode(raw) if huff else raw).decode("latin-1"), i + n

    def _get(self, idx):
        if 1 <= idx <= len(_STATIC):
            return _STATIC[idx - 1]
        k = idx - len(_STATIC) - 1
        if 0 <= k < len(self.dyn):
            return self.dyn[k]
        raise ValueError(f"HPACK index {idx} out of range")

    def _evict(self):
        while self.size > self.max and self.dyn:
            n, v = self.dyn.pop()
            self.size -= len(n) + len(v) + 32

    def _add(self, name, value):
        self.dyn.insert(0, (name, value))
        self.size += len(name) + len(value) + 32
        self._evict()

    def decode(self, data):
        out, i = [], 0
        while i < len(data):
            b = data[i]
            if b & 0x80:
                idx, i = self._int(data, i, 7)
                out.append(self._get(idx))
            elif b & 0xE0 == 0x20:
                self.max, i = self._int(data, i, 5)
                self._evict()
            else:
                incremental = bool(b & 0x40)
                idx, i = self._int(data, i, 6 if incremental else 4)
                if idx:
                    name = self._get(idx)[0]
                else:
                    name, i = self._str(data, i)
                value, i = self._str(data, i)
                if incremental:
                    self._add(name, value)
                out.append((name, value))
        return out


# ---------------------------------------------------------------------------
# gRPC over cleartext HTTP/2 (h2c), one call per connection
# ---------------------------------------------------------------------------

_PREFACE = b"PRI * HTTP/2.0\r\n\r\nSM\r\n\r\n"
_DATA, _HEADERS, _RST, _SETTINGS, _PING, _GOAWAY, _WINDOW, _CONT = 0, 1, 3, 4, 6, 7, 8, 9
_END_STREAM, _ACK, _END_HEADERS, _PADDED, _PRIORITY = 0x1, 0x1, 0x4, 0x8, 0x20
_WINDOW_SIZE = 16 * 1024 * 1024
_GRPC_STATUS = {0: "OK", 1: "CANCELLED", 2: "UNKNOWN", 3: "INVALID_ARGUMENT",
                4: "DEADLINE_EXCEEDED", 5: "NOT_FOUND", 7: "PERMISSION_DENIED",
                8: "RESOURCE_EXHAUSTED", 9: "FAILED_PRECONDITION", 12: "UNIMPLEMENTED",
                13: "INTERNAL", 14: "UNAVAILABLE"}


class Stopped(Exception):
    pass


class H2Error(Exception):
    pass


class GrpcCall:
    def __init__(self, host, port, stop=lambda: False, connect_timeout=3.0):
        self.host, self.port, self.stop = host, port, stop
        self.sock = socket.create_connection((host, port), timeout=connect_timeout)
        self.sock.settimeout(1.0)
        self.buf = bytearray()
        self.hdec = HpackDecoder()
        self._send(_PREFACE)
        # ENABLE_PUSH=0, INITIAL_WINDOW_SIZE=16 MiB, then open the connection window too.
        self._frame(_SETTINGS, 0, 0, struct.pack(">HI", 0x2, 0) + struct.pack(">HI", 0x4, _WINDOW_SIZE))
        self._frame(_WINDOW, 0, 0, struct.pack(">I", _WINDOW_SIZE - 65535))

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass

    def _send(self, data):
        self.sock.sendall(data)

    def _frame(self, typ, flags, sid, payload=b""):
        self._send(len(payload).to_bytes(3, "big") + bytes([typ, flags]) +
                   struct.pack(">I", sid) + payload)

    def _recv_exact(self, n):
        while len(self.buf) < n:
            if self.stop():
                raise Stopped()
            try:
                chunk = self.sock.recv(65536)
            except socket.timeout:
                continue
            if not chunk:
                raise ConnectionError("server closed the connection")
            self.buf += chunk
        out = bytes(self.buf[:n])
        del self.buf[:n]
        return out

    def _read_frame(self):
        h = self._recv_exact(9)
        length = int.from_bytes(h[:3], "big")
        return h[3], h[4], struct.unpack(">I", h[5:9])[0] & 0x7FFFFFFF, self._recv_exact(length)

    def call(self, path, request=b""):
        """Yield ("headers", list) / ("message", bytes) / ("end", dict of trailers)."""
        sid = 1
        self._frame(_HEADERS, _END_HEADERS, sid, hpack_encode([
            (":method", "POST"), (":scheme", "http"), (":path", path),
            (":authority", f"{self.host}:{self.port}"), ("content-type", "application/grpc"),
            ("te", "trailers"), ("user-agent", "sc-nav-telemetry-capture/1")]))
        self._frame(_DATA, _END_STREAM, sid, b"\x00" + len(request).to_bytes(4, "big") + request)
        grpc_buf, block, unacked = bytearray(), bytearray(), 0
        got_headers, block_ends_stream, last_headers = False, False, []
        while True:
            typ, flags, fsid, payload = self._read_frame()
            if typ == _SETTINGS:
                if not flags & _ACK:
                    self._frame(_SETTINGS, _ACK, 0)
                continue
            if typ == _PING:
                if not flags & _ACK:
                    self._frame(_PING, _ACK, 0, payload)
                continue
            if typ == _GOAWAY:
                code = struct.unpack(">I", payload[4:8])[0] if len(payload) >= 8 else -1
                raise H2Error(f"GOAWAY code {code} {payload[8:].decode('latin-1', 'replace')}")
            if fsid != sid:
                continue
            if typ == _RST:
                raise H2Error(f"stream reset, HTTP/2 error code {struct.unpack('>I', payload)[0]}")
            if typ == _DATA:
                if flags & _PADDED:
                    payload = payload[1:len(payload) - payload[0]]
                unacked += len(payload)
                if unacked > _WINDOW_SIZE // 4:
                    self._frame(_WINDOW, 0, 0, struct.pack(">I", unacked))
                    self._frame(_WINDOW, 0, sid, struct.pack(">I", unacked))
                    unacked = 0
                grpc_buf += payload
                while len(grpc_buf) >= 5:
                    mlen = int.from_bytes(grpc_buf[1:5], "big")
                    if len(grpc_buf) < 5 + mlen:
                        break
                    if grpc_buf[0]:
                        raise H2Error("server sent a compressed message (not requested)")
                    msg = bytes(grpc_buf[5:5 + mlen])
                    del grpc_buf[:5 + mlen]
                    yield ("message", msg)
                if flags & _END_STREAM:
                    yield ("end", dict(last_headers))
                    return
            elif typ in (_HEADERS, _CONT):
                if typ == _HEADERS:
                    if flags & _PADDED:
                        payload = payload[1:len(payload) - payload[0]]
                    if flags & _PRIORITY:
                        payload = payload[5:]
                    block_ends_stream = bool(flags & _END_STREAM)
                block += payload
                if flags & _END_HEADERS:
                    last_headers = self.hdec.decode(bytes(block))
                    block.clear()
                    if not got_headers:
                        got_headers = True
                        yield ("headers", last_headers)
                    if block_ends_stream:
                        yield ("end", dict(last_headers))
                        return


def grpc_status_text(trailers):
    code = trailers.get("grpc-status", "?")
    try:
        name = _GRPC_STATUS.get(int(code), code)
    except ValueError:
        name = code
    msg = trailers.get("grpc-message", "")
    return f"{name}" + (f" ({msg})" if msg else "")


# ---------------------------------------------------------------------------
# Game.log
# ---------------------------------------------------------------------------

_LOG_KEEP_RE = re.compile(
    r"SendCommodityBuyRequest|SendCommoditySellRequest|<GameTelemetry>|"
    r"SendShopBuyRequest|SendShopSellRequest|ShopFlowResponse|"
    r"Quantum Drive Arrived|Player Selected Quantum Target|Quantum Travel - |"
    r"Projected Start Location|<SHUDEvent_OnNotification>|<Join PU>|New Shard Id|"
    r"AddingCommodityBox|Fatal|ActorState Dead|Corpse")
# One line per HUD notification (contract accepted/complete, fines, zones...);
# the queue dumps and UpdateNotificationItem lines that repeat it are not kept.
_NOTIFY_RE = re.compile(r'<SHUDEvent_OnNotification> Added notification "([^"]*)')
_TELEMETRY_PORT_RE = re.compile(r"Local services server is running on ([\d.]+):(\d+)")
# The game read the settings but started its local server with nothing on it:
# PTU 5.0 builds from CL 13273298 (2026-11-24) do this whatever is configured.
_NO_SERVICES = "<GRPC Server> No services configured"
_BRANCH_RE = re.compile(r"Branch:\s+([\w.-]+)")
_CL_RE = re.compile(r"Changelist:\s+(\d+)")
_TXN_RE = re.compile(r"Send(?:Commodity|Shop)(Buy|Sell)Request")  # cargo + item kiosks


def fresh_dir(parent, prefix):
    """parent/<prefix>-<utc stamp>, suffixed when that already exists (the
    recorder appends, so two runs must never share a folder)."""
    base = Path(parent) / f"{prefix}-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
    out, k = base, 1
    while out.exists():
        k += 1
        out = Path(f"{base}-{k}")
    return out


def default_game_log():
    """The most recently written Game.log among the channels: with LIVE and PTU
    both installed, that is the one being played (LIVE-first would tail a stale
    log and read the schema out of the wrong exe)."""
    found = [p for p in _LOG_CANDIDATES if os.path.isfile(p)]
    return max(found, key=os.path.getmtime, default=None)


def read_log_head(path, max_lines=4000):
    info = {}
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            for k, line in enumerate(fh):
                if k > max_lines:
                    break
                if "branch" not in info and (m := _BRANCH_RE.search(line)):
                    info["branch"] = m.group(1)
                if "changelist" not in info and (m := _CL_RE.search(line)):
                    info["changelist"] = m.group(1)
                if m := _TELEMETRY_PORT_RE.search(line):
                    info["telemetry"] = (m.group(1), int(m.group(2)))
                if _NO_SERVICES in line:
                    info["no_services"] = True
    except OSError:
        pass
    return info


class LogTail(threading.Thread):
    def __init__(self, path, on_line, stop):
        super().__init__(daemon=True)
        self.path, self.on_line, self.stop = path, on_line, stop

    def run(self):
        fh = None
        while not self.stop.is_set():
            try:
                if fh is None:
                    fh = open(self.path, "rb")
                    fh.seek(0, 2)
                if os.path.getsize(self.path) < fh.tell():  # game restarted: new log
                    fh.close()
                    fh = open(self.path, "rb")
                line = fh.readline()
                if not line or not line.endswith(b"\n"):
                    if line:
                        fh.seek(-len(line), 1)
                    time.sleep(0.25)
                    continue
                self.on_line(line.decode("utf-8", "replace").rstrip())
            except OSError:
                fh = None
                time.sleep(2)


# ---------------------------------------------------------------------------
# Recorder
# ---------------------------------------------------------------------------

_BALANCE_PATHS = re.compile(r"(^|\.)uec_balance$|^6\.2\.4$")
DEFAULT_ECHO = (r"location|jurisdiction|crime|armistice|quantum|mission|objective|"
                r"class_name|manufacturer|^vehicle\.state\.name|is_landed|locomotion|"
                r"seat_name|crew_role|^events|player|zone|mode")
# 5.0 adds a per-frame counter/clock (header.*) and perf counters (game.debug.*)
# that change on every frame; the shard fields under game.debug are kept.
DEFAULT_QUIET = r"^force_feedback|^7\.|heart_rate|^header\.|^game\.debug\.(?!shard)"


class Recorder:
    def __init__(self, out_dir, schema=None, out_type=None, echo=DEFAULT_ECHO,
                 quiet=DEFAULT_QUIET, write_raw=True, say=print, float_interval=1.0):
        self.dir = Path(out_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.schema, self.out_type, self.say = schema, out_type, say
        self.echo = re.compile(echo) if echo else None
        self.quiet = re.compile(quiet) if quiet else None
        self.lock = threading.Lock()
        self.raw = open(self.dir / "frames.bin", "ab") if write_raw else None
        self.changes = open(self.dir / "changes.jsonl", "a", encoding="utf-8")
        self.events_fh = open(self.dir / "events.jsonl", "a", encoding="utf-8")
        self.prev = None
        self.stats = {}
        self.events, self.markers, self.balances = [], [], []
        self.frames = self.decode_errors = 0
        self.first_t = self.last_t = None
        self.last_echo = {}
        self.float_interval = float_interval
        self.float_written, self.float_pending = {}, set()
        self.game_events = []
        self.gamelog_fh = None
        self.closed = False
        self.started = time.time()

    def event(self, kind, t=None, **data):
        t = time.time() if t is None else t
        rec = {"t": utc_iso(t), "kind": kind, **data}
        with self.lock:
            self.events.append((t, rec))
            self.events_fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
            self.events_fh.flush()
        return rec

    def marker(self, text):
        t = time.time()
        self.markers.append((t, text))
        self.event("marker", t, text=text)
        self.say(f"[{local_hms(t)}] MARK #{len(self.markers)}: {text}")

    def log_line(self, line):
        with self.lock:
            if self.closed:
                return
            if self.gamelog_fh is None:
                self.gamelog_fh = open(self.dir / "gamelog.txt", "a", encoding="utf-8")
            self.gamelog_fh.write(line + "\n")
        if _LOG_KEEP_RE.search(line):
            self.event("gamelog", line=line[:600])
            if _TXN_RE.search(line):
                self.say(f"[{local_hms(time.time())}] Game.log: kiosk {_TXN_RE.search(line).group(1).lower()} request")

    def reset_stream(self):
        """New connection: next frame is written as a full snapshot."""
        self.prev = None

    def decode(self, raw):
        if self.schema and self.out_type:
            try:
                return self.schema.decode(raw, self.out_type)
            except ValueError as exc:
                self.decode_errors += 1
                return {"_decode_error": str(exc)}
        try:
            return raw_decode(raw)
        except ValueError as exc:
            self.decode_errors += 1
            return {"_decode_error": str(exc)}

    def on_frame(self, t, raw):
        if self.raw:
            self.raw.write(struct.pack("<dI", t, len(raw)) + raw)
        self.frames += 1
        self.first_t = self.first_t or t
        self.last_t = t
        decoded = self.decode(raw)
        flat = flatten(decoded)
        snapshot = self.prev is None
        prev = self.prev or {}
        changed = {p: v for p, v in flat.items() if p not in prev or prev[p] != v}
        changed.update({p: None for p in prev if p not in flat})
        self.prev = flat

        counted = set()
        for p, v in flat.items():
            key = re.sub(r"\[\d+\]", "[]", p)
            s = self.stats.get(key)
            if s is None:
                s = self.stats[key] = {"first": t, "present": 0, "changes": 0,
                                       "values": set(), "last": v}
            if key not in counted:  # a repeated field counts once per frame
                counted.add(key)
                s["present"] += 1
            s["last"] = v
            if len(s["values"]) < 12:
                s["values"].add(json.dumps(v, ensure_ascii=False)[:80])
        for p in changed:
            if not snapshot:
                key = re.sub(r"\[\d+\]", "[]", p)
                if key in self.stats:
                    self.stats[key]["changes"] += 1

        if snapshot:
            self.changes.write(json.dumps({"t": utc_iso(t), "snapshot": decoded},
                                          ensure_ascii=False) + "\n")
        else:
            shown = {}
            for p, v in changed.items():
                if self.quiet and self.quiet.search(p):
                    continue
                if isinstance(v, float) and self.float_interval > 0:
                    self.float_pending.add(p)
                else:
                    shown[p] = v
                    self.float_pending.discard(p)
            # A throttled float is written once its interval is up, so the
            # value it settles on always lands even if it then stops changing.
            for p in sorted(self.float_pending):
                if t - self.float_written.get(p, 0) >= self.float_interval:
                    self.float_written[p] = t
                    self.float_pending.discard(p)
                    shown[p] = flat.get(p)
            if shown:
                self.changes.write(json.dumps({"t": utc_iso(t), "set": shown},
                                              ensure_ascii=False) + "\n")

        # GameEvents live for ONE frame; two alike in a row would not even show
        # as a change, so each is logged where it occurs.
        evs = decoded.get("events") if isinstance(decoded, dict) else None
        evs = evs.get("events") if isinstance(evs, dict) else None
        for ev in evs if isinstance(evs, list) else []:
            name = ev.get("type", ev) if isinstance(ev, dict) else ev
            self.game_events.append((t, name))
            self.event("game_event", t, type=name)
            self.say(f"[{local_hms(t)}] game event {name}")

        for p, v in changed.items():
            if _BALANCE_PATHS.search(p):
                old = prev.get(p)
                if isinstance(v, int) and isinstance(old, int):
                    delta = v - old
                    self.balances.append((t, old, v))
                    self.event("balance", t, path=p, old=old, new=v, delta=delta)
                    self.say(f"[{local_hms(t)}] aUEC {old:,} -> {v:,}  ({delta:+,})")
                elif isinstance(v, int):
                    self.event("balance", t, path=p, new=v, initial=True)
                    self.say(f"[{local_hms(t)}] aUEC balance {v:,}")
            elif (not snapshot and self.echo and self.echo.search(p)
                  and not p.startswith("events.") and not isinstance(v, float) and t - self.last_echo.get(p, 0) > 5):
                self.last_echo[p] = t
                self.say(f"[{local_hms(t)}] {p} = {json.dumps(v)[:120]}")

    def flush(self):
        with self.lock:
            for fh in (self.raw, self.changes, self.events_fh, self.gamelog_fh):
                if fh:
                    fh.flush()

    def close(self, info=None):
        self.flush()
        with self.lock:
            self.closed = True
            for fh in (self.raw, self.changes, self.events_fh, self.gamelog_fh):
                if fh:
                    fh.close()
        (self.dir / "summary.md").write_text(self.summary(info or {}), encoding="utf-8")

    def summary(self, info):
        w = []
        span = (self.last_t - self.first_t) if self.frames > 1 else 0
        w.append(f"# Telemetry capture {utc_iso(self.started)}\n")
        for k in ("branch", "changelist", "exe", "game_log", "schema", "endpoint"):
            if info.get(k):
                w.append(f"- **{k}:** {info[k]}")
        w.append(f"- **frames:** {self.frames}" + (
            f" over {span:.0f} s ({self.frames / span:.1f}/s)" if span else ""))
        if self.decode_errors:
            w.append(f"- **decode errors:** {self.decode_errors}")
        w.append("")

        if self.markers:
            w.append("## Markers\n")
            w.extend(f"{k}. {local_hms(t)} {text}" for k, (t, text) in enumerate(self.markers, 1))
            w.append("")

        w.append("## aUEC balance changes\n")
        if not self.balances:
            w.append("None seen (no balance field, or it never changed).\n")
        else:
            txns = [(t, r["line"]) for t, r in self.events
                    if r["kind"] == "gamelog" and _TXN_RE.search(r["line"])]
            notes = [(t, m.group(1).strip(" :")) for t, r in self.events
                     if r["kind"] == "gamelog" and (m := _NOTIFY_RE.search(r["line"]))]
            w.append("| time | old | new | delta | Game.log: kiosk request (±30 s), "
                     "HUD notifications (±5 s) |")
            w.append("|---|---:|---:|---:|---|")
            for t, old, new in self.balances:
                near = min(txns, key=lambda x: abs(x[0] - t), default=None)
                note = ""
                if near and abs(near[0] - t) <= 30:
                    m = re.search(r"(?:price|amount)\[([\d.]+)\]", near[1])
                    side = _TXN_RE.search(near[1]).group(1).lower()
                    note = (f"{side} {near[0] - t:+.1f}s"
                            + (f", logged {m.group(0)}" if m else ""))
                    if a := re.search(r"autoLoading\[(\d)\]", near[1]):
                        note += f", auto-load {'ON' if a.group(1) == '1' else 'OFF'}"
                    if m:
                        # What the balance moved by beyond the logged price:
                        # the auto-load fee, when there is one.
                        logged = float(m.group(1))
                        extra = (new - old) - (logged if side == "sell" else -logged)
                        note += f", beyond logged price: {extra:+,.0f}"
                heard = [f'{nt - t:+.1f}s "{text}"' for nt, text in notes if abs(nt - t) <= 5]
                note = "; ".join(filter(None, [note] + heard[:4])).replace("|", "\\|")
                w.append(f"| {local_hms(t)} | {old:,} | {new:,} | {new - old:+,} | {note} |")
            w.append("")

        if self.game_events:
            w.append("## Game events (stream)\n")
            w.extend(f"- {local_hms(t)} {name}" for t, name in self.game_events)
            w.append("")

        w.append("## Field paths seen\n")
        w.append("`[]` = any index. Present = share of frames carrying the field "
                 "(proto3 omits zero values, so <100% can just mean 'was zero').\n")
        w.append("| path | first seen | present | changes | values (up to 12) |")
        w.append("|---|---|---:|---:|---|")
        n = max(self.frames, 1)
        for key in sorted(self.stats):
            s = self.stats[key]
            vals = ", ".join(sorted(s["values"]))
            if len(vals) > 160:
                vals = vals[:157] + "..."
            vals = vals.replace("|", "\\|")
            w.append(f"| `{key}` | {local_hms(s['first'])} | {100 * s['present'] / n:.0f}% "
                     f"| {s['changes']} | {vals} |")
        w.append("")

        if self.schema and self.out_type:
            never = [p for p in self.schema.leaf_paths(self.out_type) if p not in self.stats]
            if never:
                w.append("## Schema fields never seen\n")
                w.append("In the schema but never sent with a non-zero value (proto3 omits "
                         "zeros, so: unimplemented, or you never did the thing).\n")
                w.extend(f"- `{p}`" for p in never)
                w.append("")

        others = [r for _, r in self.events
                  if r["kind"] not in ("gamelog", "marker", "balance", "game_event")]
        if others:
            w.append("## Connection log\n")
            for r in others:
                extra = {k: v for k, v in r.items() if k not in ("t", "kind")}
                w.append(f"- {r['t']} **{r['kind']}** {json.dumps(extra, ensure_ascii=False)}")
            w.append("")
        return "\n".join(w)


# ---------------------------------------------------------------------------
# Schema loading
# ---------------------------------------------------------------------------

def save_schema(found, names, schema_dir):
    schema_dir = Path(schema_dir)
    schema_dir.mkdir(parents=True, exist_ok=True)
    for name in names:
        blob, f = found[name]
        stem = name.replace("/", "__")
        (schema_dir / f"{stem}.pb").write_bytes(blob)
        (schema_dir / f"{stem}.txt").write_text(proto_text(f), encoding="utf-8")
    others = sorted(n for n in found if n not in names)
    (schema_dir / "other_protos_in_exe.txt").write_text("\n".join(others) + "\n", encoding="utf-8")


def load_schema_dir(path):
    files = []
    for pb in sorted(Path(path).glob("*.pb")):
        files.append(decode_file(pb.read_bytes()))
    return files


def resolve_exe(args, game_log):
    if args.exe:
        return args.exe
    if game_log:
        cand = Path(game_log).parent / "Bin64" / "StarCitizen.exe"
        if cand.is_file():
            return str(cand)
    return None


def load_schema(args, game_log, schema_out, say=print):
    """(Schema or None, description)."""
    if args.schema_dir:
        files = load_schema_dir(args.schema_dir)
        if files:
            return Schema(files), f"from {args.schema_dir}"
        say(f"No .pb files in {args.schema_dir}.")
    exe = resolve_exe(args, game_log)
    if not exe:
        say("StarCitizen.exe not found (use --exe); frames will be decoded by field number.")
        return None, "none (exe not found)"
    say(f"Reading the telemetry schema out of {exe} ...")
    t0 = time.time()
    try:
        found, names = extract_schema(exe)
    except OSError as exc:
        say(f"Couldn't read the exe: {exc}; frames will be decoded by field number.")
        return None, f"none ({exc})"
    say(f"  {len(found)} embedded .proto files, {len(names)} telemetry-related "
        f"({time.time() - t0:.1f} s): {', '.join(names) or '-'}")
    if schema_out:
        save_schema(found, names, schema_out)
    if not names:
        return None, f"none found in {exe}"
    return Schema([found[n][1] for n in names]), f"{', '.join(names)} from {exe}"


def stream_method(schema):
    m = schema.methods.get("TelemetryStream") if schema else None
    if m:
        return m["path"], m["output"]
    return f"/{FALLBACK_SERVICE}/TelemetryStream", None


def actions_method(schema):
    m = schema.methods.get("ListTriggerableActions") if schema else None
    if m:
        return m["path"], m["output"]
    return f"/{FALLBACK_SERVICE}/ListTriggerableActions", None


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def enable_hints(game_log, say):
    say("  Nothing is listening. Is the telemetry server on?")
    say("   LIVE 4.10: `setx GRPC_CLIENT_SERVER_ENABLED true`, quit the RSI Launcher fully, restart.")
    say("   PTU 5.0:   user.cfg -> grpc.client.server.enabled = 1 / grpc.client.server.port = 2942 /"
        " server.game.telemetry.enabled = 1")
    if game_log:
        cfg = Path(game_log).parent / "user.cfg"
        if cfg.is_file():
            text = cfg.read_text(encoding="utf-8", errors="replace").lower()
            has = "server.game.telemetry.enabled" in text
            say(f"   {cfg}: telemetry key {'present' if has else 'NOT present'}")
    if game_log and read_log_head(game_log).get("no_services"):
        say("  Game.log says '<GRPC Server> No services configured': this game build read the")
        say("  settings and still did not start telemetry. Nothing to fix on this PC (seen on")
        say("  5.0 PTU from CL 13273298). Markers and gamelog.txt are still being recorded.")
    say("  (Frames only start once your character has spawned. Retrying every 3 s.)")


def fetch_actions(host, port, schema, rec, stop, say):
    path, out_type = actions_method(schema)
    call = None
    try:
        call = GrpcCall(host, port, stop=stop)
        msgs, end = [], {}
        for kind, payload in call.call(path):
            if kind == "message":
                msgs.append(payload)
            elif kind == "end":
                end = payload
        status = grpc_status_text(end)
        if not msgs:
            say(f"ListTriggerableActions: {status} (no list on this build)")
            rec.event("actions", status=status)
            return
        decoded = [(schema.decode(m, out_type) if schema and out_type
                    else raw_decode(m)) for m in msgs]
        (rec.dir / "actions.json").write_text(
            json.dumps(decoded if len(decoded) > 1 else decoded[0], indent=2, ensure_ascii=False),
            encoding="utf-8")
        size = len(json.dumps(decoded))
        say(f"ListTriggerableActions: {status}, saved actions.json ({size:,} bytes)")
        rec.event("actions", status=status, bytes=size)
    except (OSError, H2Error, ValueError) as exc:
        say(f"ListTriggerableActions failed: {exc}")
        rec.event("actions_error", error=str(exc))
    finally:
        if call:
            call.close()


def run_capture(args):
    say = print
    stop = threading.Event()
    game_log = args.game_log or default_game_log()
    head = read_log_head(game_log) if game_log else {}
    host = args.host
    port = args.port or (head.get("telemetry", (None, None))[1]) or DEFAULT_PORT

    out = fresh_dir(args.out, "session")
    schema, schema_desc = load_schema(args, game_log, out / "schema", say)
    path, out_type = stream_method(schema)
    rec = Recorder(out, schema, out_type, echo=args.echo, quiet=args.quiet, say=say,
                   float_interval=args.float_interval)
    info = {"branch": head.get("branch"), "changelist": head.get("changelist"),
            "exe": resolve_exe(args, game_log), "game_log": game_log, "schema": schema_desc,
            "endpoint": f"{host}:{port}{path}"}
    rec.event("start", **{k: v for k, v in info.items() if v})

    say(f"Recording to {out}")
    say(f"Game build: {head.get('branch', '?')} CL {head.get('changelist', '?')}"
        f"   endpoint {host}:{port}{path}")
    if game_log:
        def on_line(line):
            nonlocal port
            m = _TELEMETRY_PORT_RE.search(line)
            if m and not args.port and int(m.group(2)) != port:
                port = int(m.group(2))
                say(f"Game.log says the telemetry server is on port {port}")
            rec.log_line(line)
        LogTail(game_log, on_line, stop).start()
    else:
        say("Game.log not found (use --game-log); kiosk transactions won't be correlated.")

    def stdin_loop():
        for line in sys.stdin:
            text = line.strip()
            if text.lower() in ("q", "quit", "exit"):
                stop.set()
                return
            if text:
                rec.marker(text)
    threading.Thread(target=stdin_loop, daemon=True).start()
    if args.duration:
        threading.Timer(args.duration, stop.set).start()
    say("Type a note + Enter to drop a marker; q + Enter or Ctrl+C to stop.\n")

    hinted, actions_done, last_status = False, args.no_actions, time.time()
    try:
        while not stop.is_set():
            call = None
            try:
                call = GrpcCall(host, port, stop=stop.is_set)
                if not actions_done:
                    actions_done = True
                    fetch_actions(host, port, schema, rec, stop.is_set, say)
                rec.reset_stream()
                rec.event("connect", port=port)
                say(f"[{local_hms(time.time())}] connected to {host}:{port}, waiting for frames")
                hinted, got = False, 0
                for kind, payload in call.call(path):
                    if kind == "message":
                        if not got:
                            say(f"[{local_hms(time.time())}] frames arriving")
                        got += 1
                        rec.on_frame(time.time(), payload)
                        if time.time() - last_status > 60:
                            last_status = time.time()
                            rec.flush()
                            say(f"[{local_hms(last_status)}] {rec.frames:,} frames so far")
                    elif kind == "headers":
                        status = dict(payload).get(":status")
                        if status and status != "200":
                            say(f"HTTP status {status}")
                    elif kind == "end":
                        status = grpc_status_text(payload)
                        rec.event("stream_end", status=status, frames=got)
                        say(f"[{local_hms(time.time())}] stream ended: {status} after {got} frames")
                        break
            except ConnectionRefusedError:
                if not hinted:
                    rec.event("refused", port=port)
                    enable_hints(game_log, say)
                    hinted = True
            except Stopped:
                break
            except (OSError, H2Error, ValueError) as exc:
                rec.event("error", error=f"{type(exc).__name__}: {exc}")
                say(f"[{local_hms(time.time())}] {type(exc).__name__}: {exc} (retrying)")
            finally:
                if call:
                    call.close()
            stop.wait(3)
    except KeyboardInterrupt:
        pass
    stop.set()
    rec.event("stop", frames=rec.frames)
    rec.close(info)
    say(f"\n{rec.frames:,} frames. Report: {out / 'summary.md'}")


def run_replay(args):
    src = Path(args.replay)
    schema, desc = None, "none"
    if args.schema_dir or (src / "schema").is_dir():
        files = load_schema_dir(args.schema_dir or src / "schema")
        if files:
            schema, desc = Schema(files), str(args.schema_dir or src / "schema")
    _, out_type = stream_method(schema)
    out = fresh_dir(src, "replay")
    rec = Recorder(out, schema, out_type, echo=None, quiet=args.quiet, write_raw=False,
                   say=lambda *_: None, float_interval=args.float_interval)
    data = (src / "frames.bin").read_bytes()
    i = 0
    while i + 12 <= len(data):
        t, n = struct.unpack("<dI", data[i:i + 12])
        rec.on_frame(t, data[i + 12:i + 12 + n])
        i += 12 + n
    for line in (src / "events.jsonl").read_text(encoding="utf-8").splitlines() if (
            src / "events.jsonl").is_file() else []:
        r = json.loads(line)
        if r.get("kind") in ("marker", "gamelog"):
            t = datetime.fromisoformat(r["t"]).timestamp()
            rec.events.append((t, r))
            if r["kind"] == "marker":
                rec.markers.append((t, r["text"]))
    rec.close({"schema": desc})
    print(f"{rec.frames:,} frames re-decoded into {out}")


def run_schema_only(args):
    game_log = args.game_log or default_game_log()
    out = fresh_dir(args.out, "schema")
    schema, desc = load_schema(args, game_log, out)
    if schema:
        for name, m in schema.methods.items():
            print(f"  rpc {m['path']}  ({m['input']} -> {'stream ' if m['server_streaming'] else ''}{m['output']})")
    print(f"Schema written to {out}")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default=DEFAULT_HOST)
    ap.add_argument("--port", type=int, help=f"default: from Game.log, else {DEFAULT_PORT}")
    ap.add_argument("--game-log", help="default: the most recently written of LIVE/PTU/EPTU under Program Files")
    ap.add_argument("--exe", help="StarCitizen.exe for the schema (default: <channel>/Bin64/)")
    ap.add_argument("--schema-dir", help="use .pb descriptors saved by an earlier run instead")
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    ap.add_argument("--duration", type=float, help="stop after this many seconds")
    ap.add_argument("--no-actions", action="store_true", help="skip ListTriggerableActions")
    ap.add_argument("--echo", default=DEFAULT_ECHO,
                    help="regex of field paths to print when they change ('' = none)")
    ap.add_argument("--quiet", default=DEFAULT_QUIET,
                    help="regex of field paths left out of changes.jsonl (still in frames.bin)")
    ap.add_argument("--float-interval", type=float, default=1.0,
                    help="min seconds between changes.jsonl writes of one float field (0 = all)")
    ap.add_argument("--schema-only", action="store_true", help="extract the schema and exit")
    ap.add_argument("--replay", metavar="SESSION_DIR", help="re-decode a session's frames.bin")
    args = ap.parse_args(argv)
    if args.replay:
        run_replay(args)
    elif args.schema_only:
        run_schema_only(args)
    else:
        run_capture(args)


if __name__ == "__main__":
    main()
