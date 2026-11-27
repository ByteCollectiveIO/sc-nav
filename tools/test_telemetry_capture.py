"""Tests for telemetry_capture.py (run: python tools/test_telemetry_capture.py)."""

import json
import os
import struct
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import telemetry_capture as tc  # noqa: E402


# --- tiny protobuf encoder (the tool only decodes) -------------------------

def varint(v):
    v &= (1 << 64) - 1
    out = bytearray()
    while True:
        b = v & 0x7F
        v >>= 7
        out.append(b | (0x80 if v else 0))
        if not v:
            return bytes(out)


def fv(num, v):
    return varint(num << 3) + varint(v)


def fl(num, payload):
    if isinstance(payload, str):
        payload = payload.encode()
    return varint((num << 3) | 2) + varint(len(payload)) + payload


def f32(num, x):
    return varint((num << 3) | 5) + struct.pack("<f", x)


def field(name, number, ftype, label=1, type_name=""):
    out = fl(1, name) + fv(3, number) + fv(4, label) + fv(5, ftype)
    return out + (fl(6, type_name) if type_name else b"")


# A miniature telemetry .proto, serialized the way protoc embeds it:
#   package sc.game.telemetry.v1;
#   enum Loco { UNSET = 0; SEATED = 3; }
#   message State { Loco locomotion = 1; int64 uec_balance = 4; int32 level = 5; }
#   message Shields { repeated float faces = 2; }
#   message Frame { State state = 2; Shields shields = 3; repeated string tags = 4; }
#   service GameTelemetryService { rpc TelemetryStream (State) returns (stream Frame); }
PKG = ".sc.game.telemetry.v1"
DESCRIPTOR = (
    fl(1, "sc/game/telemetry/v1/game_telemetry.proto")
    + fl(2, "sc.game.telemetry.v1")
    + fl(4, fl(1, "State") + fl(2, field("locomotion", 1, 14, type_name=f"{PKG}.Loco"))
         + fl(2, field("uec_balance", 4, 3)) + fl(2, field("level", 5, 5)))
    + fl(4, fl(1, "Shields") + fl(2, field("faces", 2, 2, label=3)))
    + fl(4, fl(1, "Frame") + fl(2, field("state", 2, 11, type_name=f"{PKG}.State"))
         + fl(2, field("shields", 3, 11, type_name=f"{PKG}.Shields"))
         + fl(2, field("tags", 4, 9, label=3)))
    + fl(5, fl(1, "Loco") + fl(2, fl(1, "UNSET") + fv(2, 0)) + fl(2, fl(1, "SEATED") + fv(2, 3)))
    + fl(6, fl(1, "GameTelemetryService") + fl(2, fl(1, "TelemetryStream")
         + fl(2, f"{PKG}.State") + fl(3, f"{PKG}.Frame") + fv(6, 1)))
    + fl(12, "proto3"))


def frame(balance, level=-2, loco=3):
    state = (fv(1, loco) if loco else b"") + fv(4, balance) + fv(5, level)  # proto3 omits 0
    faces = b"".join(struct.pack("<f", x) for x in (1.0, 0.5))
    return fl(2, state) + fl(3, fl(2, faces)) + fl(4, "a") + fl(4, "b")


class TestHpack(unittest.TestCase):
    def test_huffman_rfc7541_c41(self):
        self.assertEqual(tc.huffman_decode(bytes.fromhex("f1e3c2e5f23a6ba0ab90f4ff")),
                         b"www.example.com")

    def test_decode_rfc7541_c41_request(self):
        block = bytes.fromhex("828684418cf1e3c2e5f23a6ba0ab90f4ff")
        self.assertEqual(tc.HpackDecoder().decode(block), [
            (":method", "GET"), (":scheme", "http"), (":path", "/"),
            (":authority", "www.example.com")])

    def test_dynamic_table_carries_across_blocks(self):
        d = tc.HpackDecoder()
        d.decode(bytes.fromhex("828684418cf1e3c2e5f23a6ba0ab90f4ff"))
        # RFC 7541 C.4.2: index 62 is the :authority the first block added.
        self.assertEqual(d.decode(bytes.fromhex("828684be5886a8eb10649cbf"))[-2:],
                         [(":authority", "www.example.com"), ("cache-control", "no-cache")])

    def test_encode_round_trip(self):
        pairs = [(":path", "/sc.game.telemetry.v1.GameTelemetryService/TelemetryStream"),
                 ("te", "trailers"), ("x-long", "y" * 300)]
        self.assertEqual(tc.HpackDecoder().decode(tc.hpack_encode(pairs)), pairs)


class TestDescriptorExtraction(unittest.TestCase):
    def test_finds_descriptor_between_garbage(self):
        blob = os.urandom(5000) + b"\x0a\x0bjunk.proto \xff\xff" + DESCRIPTOR + b"\x00\xff" + os.urandom(5000)
        found = tc.scan_descriptors(blob)
        name = "sc/game/telemetry/v1/game_telemetry.proto"
        self.assertIn(name, found)
        self.assertEqual(found[name][0], DESCRIPTOR)
        self.assertNotIn("junk.proto", found)
        self.assertEqual(tc.telemetry_files(found), [name])

    def test_walk_stops_at_out_of_order_field(self):
        # protoc writes fields in number order; a lower number after syntax(12)
        # is the next blob, not part of this descriptor.
        tail = fl(1, "next.proto")
        self.assertEqual(tc.walk_file_descriptor(DESCRIPTOR + tail, 0), len(DESCRIPTOR))

    def test_proto_text_mentions_everything(self):
        text = tc.proto_text(tc.decode_file(DESCRIPTOR))
        for bit in ("package sc.game.telemetry.v1;", "SEATED = 3;", "int64 uec_balance = 4;",
                    "repeated float faces = 2;",
                    "rpc TelemetryStream (.sc.game.telemetry.v1.State) returns (stream "):
            self.assertIn(bit, text)


class TestDecoding(unittest.TestCase):
    def setUp(self):
        self.schema = tc.Schema([tc.decode_file(DESCRIPTOR)])

    def test_method_path(self):
        path, out_type = tc.stream_method(self.schema)
        self.assertEqual(path, "/sc.game.telemetry.v1.GameTelemetryService/TelemetryStream")
        self.assertEqual(out_type, f"{PKG}.Frame")

    def test_schema_decode(self):
        got = self.schema.decode(frame(1_250_000), f"{PKG}.Frame")
        self.assertEqual(got, {"state": {"locomotion": "SEATED", "uec_balance": 1_250_000,
                                         "level": -2},
                               "shields": {"faces": [1.0, 0.5]}, "tags": ["a", "b"]})

    def test_unknown_enum_value_and_field_survive(self):
        raw = fl(2, fv(1, 9) + f32(7, 2.5))
        got = self.schema.decode(raw, f"{PKG}.Frame")
        self.assertEqual(got, {"state": {"locomotion": 9, "#7": 2.5}})

    def test_raw_decode_without_schema(self):
        got = tc.raw_decode(frame(42))
        self.assertEqual(got["2"]["4"], 42)
        self.assertEqual(got["4"], ["a", "b"])

    def test_flatten(self):
        self.assertEqual(tc.flatten({"a": {"b": 1, "c": [1, 2]}, "e": [{"x": 1}]}),
                         {"a.b": 1, "a.c": [1, 2], "e[0].x": 1})


class TestRecorder(unittest.TestCase):
    def test_balance_deltas_and_changes(self):
        schema = tc.Schema([tc.decode_file(DESCRIPTOR)])
        lines = []
        with tempfile.TemporaryDirectory() as d:
            rec = tc.Recorder(d, schema, f"{PKG}.Frame", say=lines.append)
            for t, bal in ((100.0, 1000), (100.1, 1000), (100.2, 900), (100.3, 880)):
                rec.on_frame(t, frame(bal))
            rec.on_frame(100.4, frame(880, loco=0))  # zero value = field omitted
            rec.close()
            changes = [json.loads(x) for x in Path(d, "changes.jsonl").read_text().splitlines()]
            summary = Path(d, "summary.md").read_text()
            raw = Path(d, "frames.bin").read_bytes()
        self.assertIn("snapshot", changes[0])
        self.assertEqual([c["set"] for c in changes[1:]], [
            {"state.uec_balance": 900}, {"state.uec_balance": 880},
            {"state.locomotion": None}])
        self.assertEqual([(o, n) for _, o, n in rec.balances], [(1000, 900), (900, 880)])
        self.assertTrue(any("(-100)" in x for x in lines))
        self.assertIn("| -100 |", summary)
        t, n = struct.unpack("<dI", raw[:12])
        self.assertEqual((t, raw[12:12 + n]), (100.0, frame(1000)))

    def test_fresh_dir_never_reuses(self):
        with tempfile.TemporaryDirectory() as d:
            a = tc.fresh_dir(d, "replay")
            a.mkdir()
            self.assertNotEqual(a, tc.fresh_dir(d, "replay"))


if __name__ == "__main__":
    unittest.main()
