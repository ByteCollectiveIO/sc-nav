#!/usr/bin/env python3
"""Game.log research spike (#40.2 §9): inventory what the game actually logs.

Feed it one or more Star Citizen Game.log files captured across a play session
(ideally a mining loop: scan → fracture → extract → refine → QT between
markers). It produces a markdown report answering the spike's question — which
in-flight events reach the log at all — so widget ideas can be judged on
evidence instead of hope.

Usage:
    python tools/gamelog_spike.py Game.log [more.log ...] [--out report.md]
                                  [--keep-identity] [--max-samples N]

Where to find the log (player's Windows box):
    C:\\Program Files\\Roberts Space Industries\\StarCitizen\\LIVE\\Game.log
    ...\\LIVE\\logbackups\\        <- previous sessions, one file per launch

The log is rewritten on every client launch, so copy it AFTER quitting the
game. Nothing here needs the game running or modifies anything: this reads a
file the game already wrote — the same class-of-citizen rule as the watcher.

Privacy: by default the report scrubs the player's own handle / account id /
geid (extracted from the log's own character-status line) and IPv4 addresses,
and drops chat lines from samples entirely (they can quote other players).
`--keep-identity` disables the scrub for local-only reading.

Line grammar (observed; matches what the watcher's shard reader assumes):
    <ISO-8601 timestamp>  optional
    [Severity]            optional: Notice/Warning/Error/Trace/Debug/Verbose
    <EventTag>            optional angle-bracket event name
    free-text message
    [Tag][Tag]...         optional trailing subsystem tags
"""

from __future__ import annotations

import argparse
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

TIMESTAMP_RE = re.compile(r"^<(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z)>\s*")
SEVERITY_RE = re.compile(r"^\[(Notice|Warning|Error|Trace|Debug|Verbose)\]\s*")
EVENT_TAG_RE = re.compile(r"^<([^>]+)>\s*")
TRAILING_TAGS_RE = re.compile(r"\s*((?:\[[^\]]*\])+)\s*$")

# The player's own identifiers, from the log's character-status / login lines.
IDENT_CHAR_RE = re.compile(r"geid (\d+) - accountId (\d+) - name (\S+) - state STATE_CURRENT")
IDENT_HANDLE_RE = re.compile(r"Handle\[([^\]]+)\]")
IPV4_RE = re.compile(r"\b(?:(?:25[0-5]|2[0-4]\d|1?\d?\d)\.){3}(?:25[0-5]|2[0-4]\d|1?\d?\d)\b")
CHAT_RE = re.compile(r"chat", re.IGNORECASE)

MAX_SAMPLE_CHARS = 240

# The spike's actual questions, as keyword buckets. A bucket hit means "the log
# says something in this territory — go read those lines"; emptiness after a
# real mining session is itself the finding (it is what pushed sc-overlay to
# OCR for scan signatures).
BUCKETS = {
    "mining": r"minin[g]?|mineabl|fractur|extract\w*|prospect|regolith|MiningModifier|laser",
    "scanning": r"\bscan\w*|signature|radar|\bping\b",
    "refinery": r"refin\w*",
    "salvage": r"salvag\w*|scrape|munch",
    "quantum": r"quantum|\bQT\b|spline|calibrat",
    "location/zone": r"StreamingSOC|SolarSystem|OOC_|_OC\d|\bOM-?\d\b|hangar|landing|atmosphere|planet\w*|asteroid",
    "cargo/commodity": r"cargo|SCU|commodit|manifest",
    "combat/danger": r"death|\bkill\w*|destro\w*|missile|hostil|crime",
    "economy": r"aUEC|kiosk|\bshop\b|purchas|transact",
    "mission": r"mission|contract|objective",
    "party": r"party",
    "vehicle": r"vehicle|\bship\b|\bseat\b|\benter\w*|\bexit\w*|\bspawn\w*",
}


def parse_line(raw):
    """Split one log line into (timestamp, severity, event_tag, tags, message)."""
    rest = raw.rstrip("\n")
    ts = sev = tag = None
    m = TIMESTAMP_RE.match(rest)
    if m:
        ts, rest = m.group(1), rest[m.end():]
    m = SEVERITY_RE.match(rest)
    if m:
        sev, rest = m.group(1), rest[m.end():]
    m = EVENT_TAG_RE.match(rest)
    if m:
        tag, rest = m.group(1), rest[m.end():]
    tags = []
    m = TRAILING_TAGS_RE.search(rest)
    if m:
        tags = re.findall(r"\[([^\]]*)\]", m.group(1))
        rest = rest[: m.start()].rstrip()
    return ts, sev, tag, tags, rest.strip()


def build_scrubber(text, keep_identity):
    """Return a fn folding the player's own identifiers out of sample lines."""
    if keep_identity:
        return lambda s: s
    handle = acct = geid = None
    m = IDENT_CHAR_RE.search(text)
    if m:
        geid, acct, handle = m.group(1), m.group(2), m.group(3)
    if not handle:
        m = IDENT_HANDLE_RE.search(text)
        if m:
            handle = m.group(1)

    def scrub(s):
        if handle:
            s = s.replace(handle, "<PLAYER>")
        if acct and len(acct) >= 4:
            s = re.sub(r"\b%s\b" % re.escape(acct), "<ACCT>", s)
        if geid and len(geid) >= 6:
            s = re.sub(r"\b%s\b" % re.escape(geid), "<GEID>", s)
        return IPV4_RE.sub("<IP>", s)

    return scrub


def clip(s):
    return s if len(s) <= MAX_SAMPLE_CHARS else s[: MAX_SAMPLE_CHARS - 3] + "..."


def analyze(paths, keep_identity=False, max_samples=3):
    tag_counts = Counter()
    tag_samples = defaultdict(list)      # tag -> [line, ...] (diverse-ish: keep firsts)
    bucket_res = {k: re.compile(v, re.IGNORECASE) for k, v in BUCKETS.items()}
    bucket_counts = Counter()
    bucket_tags = defaultdict(Counter)   # bucket -> tag counter
    bucket_samples = defaultdict(list)
    total = untagged = chat = 0
    first_ts = last_ts = None

    texts = [p.read_text(encoding="utf-8", errors="replace") for p in paths]
    scrub = build_scrubber("\n".join(t[:200_000] for t in texts), keep_identity)

    for text in texts:
        for raw in text.splitlines():
            if not raw.strip():
                continue
            total += 1
            ts, sev, tag, tags, msg = parse_line(raw)
            if ts:
                first_ts = first_ts or ts
                last_ts = ts
            is_chat = bool(CHAT_RE.search(raw))
            if is_chat:
                chat += 1
            key = tag or "(untagged)"
            if not tag:
                untagged += 1
            tag_counts[key] += 1
            if not is_chat and len(tag_samples[key]) < max_samples:
                sample = clip(scrub(raw.strip()))
                if sample not in tag_samples[key]:
                    tag_samples[key].append(sample)
            for bucket, rx in bucket_res.items():
                if rx.search(raw):
                    bucket_counts[bucket] += 1
                    bucket_tags[bucket][key] += 1
                    if not is_chat and len(bucket_samples[bucket]) < max_samples * 4:
                        sample = clip(scrub(raw.strip()))
                        if sample not in bucket_samples[bucket]:
                            bucket_samples[bucket].append(sample)

    return {
        "files": [str(p) for p in paths],
        "total": total, "untagged": untagged, "chat": chat,
        "first_ts": first_ts, "last_ts": last_ts,
        "tag_counts": tag_counts, "tag_samples": tag_samples,
        "bucket_counts": bucket_counts, "bucket_tags": bucket_tags,
        "bucket_samples": bucket_samples,
    }


def report(a):
    out = []
    w = out.append
    w("# Game.log spike report (#40.2 §9)\n")
    w(f"Files: {', '.join('`%s`' % f for f in a['files'])}  ")
    w(f"Lines: **{a['total']:,}** ({a['untagged']:,} untagged, {a['chat']:,} chat — chat excluded from samples)  ")
    if a["first_ts"]:
        w(f"Span: `{a['first_ts']}` → `{a['last_ts']}`\n")

    w("\n## Question buckets — is there signal here at all?\n")
    w("An empty bucket after a real mining session is a finding, not a failure:")
    w("it means that activity never reaches the log (sc-overlay hit this for scan")
    w("signatures and fell back to OCR).\n")
    for bucket in BUCKETS:
        n = a["bucket_counts"].get(bucket, 0)
        w(f"### {bucket} — {n:,} lines")
        if not n:
            w("_nothing_\n")
            continue
        top = ", ".join(f"`{t}` ×{c:,}" for t, c in a["bucket_tags"][bucket].most_common(6))
        w(f"tags: {top}\n")
        for s in a["bucket_samples"][bucket][:8]:
            w(f"    {s}")
        w("")

    w("\n## Event-tag inventory (full)\n")
    w("| count | tag |")
    w("|---:|---|")
    for tag, c in a["tag_counts"].most_common():
        w(f"| {c:,} | `{tag}` |")
    w("\n### Samples per tag (up to 3, scrubbed)\n")
    for tag, _ in a["tag_counts"].most_common():
        samples = a["tag_samples"].get(tag) or []
        if not samples:
            continue
        w(f"**`{tag}`**")
        for s in samples:
            w(f"    {s}")
        w("")
    return "\n".join(out) + "\n"


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("logs", nargs="+", type=Path, help="Game.log file(s)")
    ap.add_argument("--out", type=Path, help="write the markdown report here (default: stdout)")
    ap.add_argument("--keep-identity", action="store_true",
                    help="skip scrubbing the player's handle/ids from samples")
    ap.add_argument("--max-samples", type=int, default=3)
    args = ap.parse_args(argv)

    missing = [p for p in args.logs if not p.is_file()]
    if missing:
        ap.error("not a file: " + ", ".join(map(str, missing)))
    text = report(analyze(args.logs, args.keep_identity, args.max_samples))
    if args.out:
        args.out.write_text(text, encoding="utf-8")
        print(f"wrote {args.out} ({len(text):,} chars)")
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
