"""server/notify_templates.py — the wording of Discord announcements, as data.

docs/discord-notification-customization.md §4 (slice 4). Each announcement is a
template with a CLOSED set of variables; app.py builders compute the values and
call render(). The shipped wording lives here and only here, and with no admin
override, render() reproduces the pre-registry hand-written posts byte for byte
(server/testdata/notify_golden.json pins that).

Template language (deliberately tiny — no logic):
- `{name}` substitutes a declared variable. Nothing else is evaluated: this is a
  regex, never str.format (which allows `{x.__class__}` attribute access).
- `{{` and `}}` are literal braces.
- The drop rule: any PART of the template that names variables, all of which
  came out empty, is left out. A part is a ` · `-separated piece of a line, a
  line, or a blank-line-separated paragraph. So "Starts {start} · {length}"
  drops just " · {length}" when there's no length, and a "Mission briefing"
  paragraph vanishes when no mission detail is set. Text-only parts always stay.
- Escaping: a variable marked `md` holds member-typed text. It is markdown-
  escaped in the DESCRIPTION slot (the only embed slot Discord renders markdown
  in) and inserted as-is in the title and footer, where an escape would show as
  a literal backslash. A variable also marked `fmt` is prose the member WROTE
  to be formatted (an event description): its **bold** / *italics* / lists
  render, and only masked links are defused — `[text](url)` shows literally,
  so a description can't hide where a link goes (user's call, 2026-10-06).
- Every variable that carries text a MEMBER typed (names, places, notes, item
  and goal names) is `md`. Security sweep 2026-10-06: a Discord nickname like
  `[patch notes](https://…)` in an unescaped slot rendered as a masked link in
  the org channel. Only fixed vocabulary (icons, headings, mode words,
  computed numbers, <t:> stamps) stays unescaped.
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field

VAR_RE = re.compile(r"\{([a-z_]+)\}")
_LBRACE, _RBRACE = "\x00", "\x01"     # placeholders for {{ and }} while parsing
SEG_SEP = " · "
SLOTS = ("title", "description", "footer")


@dataclass(frozen=True)
class Var:
    label: str            # what the editor shows next to the chip
    sample: str           # used by the preview + test send (slice 5)
    md: bool = False      # member-typed text: escape in the description slot
    fmt: bool = False     # with md: keep the member's formatting, defuse masked links only
    group: str = ""       # editor chip grouping (When / Where / Crew …); "" = ungrouped


@dataclass(frozen=True)
class Template:
    category: str                 # notify category (webhook channel)
    label: str                    # human name in the editor
    title: str
    description: str | None       # None = built in code, not editable (op record)
    vars: dict = field(default_factory=dict)
    footer: str = ""
    color: int = 0x4FC3F7         # the usual colour (preview); builders may vary it
    color_note: str = ""          # when the builder varies it, say how


def _protect(text: str) -> str:
    return text.replace("{{", _LBRACE).replace("}}", _RBRACE)


def _restore(text: str) -> str:
    return text.replace(_LBRACE, "{").replace(_RBRACE, "}")


def template_vars(text: str) -> set[str]:
    """Variable names a template text uses ({{…}} literals excluded)."""
    return set(VAR_RE.findall(_protect(text or "")))


def _blank(part: str, values: dict) -> bool:
    names = VAR_RE.findall(part)
    return bool(names) and all(not values.get(n) for n in names)


def _sub(part: str, values: dict) -> str:
    return VAR_RE.sub(lambda m: values.get(m.group(1), ""), part)


def render_text(text: str, values: dict) -> str:
    """Apply substitution + the drop rule to one slot's text. `values` must
    already be escaped for this slot (see render)."""
    paragraphs = []
    for par in _protect(text or "").split("\n\n"):
        if _blank(par, values):
            continue
        lines = []
        for line in par.split("\n"):
            if _blank(line, values):
                continue
            kept = [seg for seg in line.split(SEG_SEP) if not _blank(seg, values)]
            if kept:
                lines.append(SEG_SEP.join(_sub(seg, values) for seg in kept))
        if lines:
            paragraphs.append("\n".join(lines))
    return _restore("\n\n".join(p for p in paragraphs if p.strip()))


# Admin overrides (slice 5): caps per slot, checked on save.
OVERRIDE_CAPS = {"title": 200, "description": 1500, "footer": 200}
_BRACED_RE = re.compile(r"\{([^{}]*)\}")
_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")


def validate_override(key: str, draft: dict) -> dict:
    """Clean an admin's override for `key`, or raise ValueError with a message
    an admin can act on. Unknown fields fail HERE, at save time — not at 2 a.m.
    when the reminder fires. Empty slots are dropped (= shipped text)."""
    t = TEMPLATES[key]
    out: dict = {}
    for slot, cap in OVERRIDE_CAPS.items():
        text = (draft.get(slot) or "").strip("\n")
        if not text.strip():
            continue
        if slot == "description" and t.description is None:
            raise ValueError("this message's body is built from the record itself and can't be edited")
        if len(text) > cap:
            raise ValueError(f"the {slot} is {len(text)} characters; the limit is {cap}")
        unknown = sorted({m for m in _BRACED_RE.findall(_protect(text))} - set(t.vars))
        if unknown:
            # Name the box and the likely intended field; list them all only
            # when nothing is close (the editor shows them as chips anyway).
            where = {"title": "the title", "description": "the message body",
                     "footer": "the footer"}[slot]
            close = difflib.get_close_matches(unknown[0], list(t.vars), n=1, cutoff=0.6)
            msg = ("Unknown field" + ("s " if len(unknown) > 1 else " ")
                   + ", ".join("{" + u + "}" for u in unknown) + f" in {where}.")
            if close:
                msg += " Did you mean {" + close[0] + "}?"
            else:
                msg += " Fields you can use here: " + ", ".join("{" + v + "}" for v in t.vars) + "."
            raise ValueError(msg + " For a literal brace, type {{ or }}.")
        out[slot] = text
    color = (draft.get("color") or "").strip()
    if color:
        if not _COLOR_RE.match(color):
            raise ValueError("colour must look like #4FC3F7")
        out["color"] = color.upper()
    return out


def sample_values(key: str) -> dict:
    return {n: v.sample for n, v in TEMPLATES[key].vars.items()}


_MASKED_LINK_RE = re.compile(r"([\[\]])")


def defuse_masked_links(text: str) -> str:
    """Escape only the brackets, so Discord renders the member's formatting but
    shows `[text](url)` as typed instead of a link whose URL is hidden."""
    return _MASKED_LINK_RE.sub(r"\\\1", text)


def render(key: str, values: dict, *, escape, overrides: dict | None = None) -> dict:
    """{title, description, footer} for announcement `key`. `values` are RAW;
    md variables are escaped here, in the description slot only. `overrides`
    = an admin's slot texts (empty/absent slot = shipped text). A variable the
    caller didn't supply renders empty — a notification must never fail."""
    t = TEMPLATES[key]
    over = overrides or {}
    out = {}
    for slot in SLOTS:
        text = over.get(slot) or getattr(t, slot)
        if text is None:
            continue
        vals = {n: ("" if values.get(n) is None else str(values.get(n))) for n in t.vars}
        if slot == "description":
            vals = {n: (defuse_masked_links(v) if t.vars[n].fmt else escape(v))
                    if t.vars[n].md else v for n, v in vals.items()}
        out[slot] = render_text(text, vals)
    return out


# ---- the registry ------------------------------------------------------------
# Shared variables, so the same name means the same thing in every template.
_TITLE = Var("Event title", "Salvage night", md=True)
_START = Var("Start (each member's own time zone)", "<t:1792726500:F>")
_START_R = Var("Start, relative ('in 2 weeks')", "<t:1792726500:R>")
_TITLE_EV = Var("Event title", "Salvage night", md=True, group="Event")
_START_W = Var("Start (each member's own time zone)", "<t:1792726500:F>", group="When")
_START_RW = Var("Start, relative ('in 2 weeks')", "<t:1792726500:R>", group="When")

TEMPLATES: dict[str, Template] = {
    "event_created": Template(
        "events", "New event",
        title="📅 New event: {title}",
        description=(
            "{description}\n{read_more}\n\n"
            "**Starts** {start} ({start_relative}) · {length}\n"
            "**Rally point** {rally_point} · **Location** {event_location}\n"
            "**Crew** {crew}\n"
            "**Roles** {roles}\n"
            "**Signups close** {signups_close}\n"
            "**Organizer** {organizer}\n"
            "**Type** {type}\n\n"
            "**Mission briefing**\n"
            "**Mission** {mission}\n"
            "**ROE** {roe}\n"
            "**Comms** {comms}\n"
            "**Loadout** {loadout}\n"
            "**Medical** {medical}\n"
            "**Prerequisites** {prereqs}"),
        vars={
            "title": _TITLE_EV,
            "description": Var("Event description (first 1,500 characters; its formatting is kept)",
                               "Bring a **Vulture**.", md=True, fmt=True, group="Event"),
            "read_more": Var("'Read the rest' link, when the description was cut", "", group="Event"),
            "start": _START_W, "start_relative": _START_RW,
            "length": Var("Length", "1 h 30 min", group="When"),
            "rally_point": Var("Rally point", "Port Tressler", md=True, group="Where"),
            "event_location": Var("Event location", "Yela belt", md=True, group="Where"),
            "crew": Var("Crew (going / max, min)", "0 / 7 going (min 5)", group="Crew"),
            "roles": Var("Roles with fill", "Salvage 0/7 · Escort 0/2", md=True, group="Crew"),
            "signups_close": Var("Signups close", "<t:1792028040:f>", group="When"),
            "organizer": Var("Organizer", "Bolvangar", md=True, group="Event"),
            "type": Var("Type · category", "Salvage Op · PvE, Social", md=True, group="Event"),
            "mission": Var("Mission", "Clear the wreck", md=True, group="Briefing"),
            "roe": Var("Rules of engagement", "PvE only", group="Briefing"),
            "comms": Var("Comms", "Org TS, channel 2", md=True, group="Briefing"),
            "loadout": Var("Loadout", "Medium armor", md=True, group="Briefing"),
            "medical": Var("Medical", "Cutty Red on call", md=True, group="Briefing"),
            "prereqs": Var("Prerequisites", "Own a salvage ship", md=True, group="Briefing"),
        }),
    "event_reminder": Template(
        "events", "Event reminder",
        title="⏰ Starting soon: {title}",
        description=("Begins {start} ({start_relative})\n📍 {place}\n\n"
                     "**Crew** {crew}\n**Roles** {roles}\n**Still short** {short}"),
        vars={"title": _TITLE, "start": _START, "start_relative": _START_R,
              "place": Var("Where (event location, else rally point)", "Yela belt", md=True),
              # The reminder is sent minutes before start, so its count is the
              # one members act on (user's call 2026-10-06).
              "crew": Var("Crew right now (going / max, min)", "5 / 7 going (min 5)"),
              "roles": Var("Roles with fill right now", "Salvage 5/7 · Escort 0/2", md=True),
              # Blank when every role is filled, so the line drops. With role
              # pings on, these are the roles the reminder @-mentions.
              "short": Var("Roles still short, and by how many", "Escort ×2 · Salvage ×2", md=True)},
        color=0xFFB74D),
    "event_rescheduled": Template(
        "events", "Event changed",
        title="📌 Event updated: {title}",
        description=("Now starts {start} ({start_relative}) — was {old_start}\n"
                     "📍 Now at {new_place}"),
        vars={"title": _TITLE,
              "start": Var("New start (empty if the time didn't change)", "<t:1792726500:F>"),
              "start_relative": Var("New start, relative", "<t:1792726500:R>"),
              "old_start": Var("Old start", "<t:1792640100:F>"),
              "new_place": Var("New place (empty if it didn't change)", "Yela belt", md=True)},
        color=0xFFB74D),
    "event_cancelled": Template(
        "events", "Event cancelled",
        title="🚫 Event cancelled: {title}",
        description="Was set for {start}.",
        vars={"title": _TITLE, "start": _START},
        color=0xEF5350),
    "listing_posted": Template(
        "marketplace", "New marketplace listing",
        title="{icon} {headline}: {item}",
        description="{terms}\nPosted by {poster}. {call_to_action}",
        vars={"icon": Var("Mode icon", "🏷️"),
              "headline": Var("Mode headline (FOR SALE, AUCTION…)", "FOR SALE"),
              "item": Var("Item", "Quantanium", md=True),
              "terms": Var("Terms (price, quantity, deadline…)", "×3 · 250,000 aUEC", md=True),
              "poster": Var("Posted by", "Bolvangar", md=True),
              "call_to_action": Var("What to do next", "Make an offer on the board.")}),
    "lfg_posted": Template(
        "lfg", "Group Finder post",
        title="{icon} {heading}: {poster}",
        description=("{note}\n\n"
                     "**Needs** {needs}\n"
                     "**Playstyles** {playstyles}\n"
                     "**Rally point** {rally_point}\n"
                     "**Comms** {comms}"),
        vars={"icon": Var("🔎 or 🙋", "🔎"),
              "heading": Var("Looking for members / Looking to join", "Looking for members"),
              "poster": Var("Posted by", "Ace", md=True),
              "note": Var("Their note", "need 2 for a bunker", md=True),
              "needs": Var("Open seats", "2 (filled 1/2)"),
              "playstyles": Var("Playstyles", "bunkers · PvE", md=True),
              "rally_point": Var("Rally point", "Daymar", md=True),
              "comms": Var("'Voice comms' when on", "Voice comms")}),
    "warning_posted": Template(
        "pirates", "Danger warning",
        title="{icon} {kind_label} {where}",
        description=("{note}\n\n"
                     "**Severity** {severity}\n"
                     "**Threat** {threat}\n"
                     "**Location** {location}\n"
                     "**Reported by** {poster}"),
        vars={"icon": Var("☠️ (players) or 🤖 (NPCs)", "☠️"),
              "kind_label": Var("'Pirate snare:' or 'Danger near'", "Pirate snare:"),
              "where": Var("Where", "Baijini Point ↔ Orison", md=True),
              "note": Var("Their note", "2 Cutlass + a snare", md=True),
              "severity": Var("Severity", "DEADLY"),
              "threat": Var("Threat", "Players (PvP)"),
              "location": Var("Extra location detail", "near the comm array", md=True),
              "poster": Var("Reported by", "Ace", md=True)},
        color=0xFFB74D, color_note="Red for a deadly report, amber otherwise."),
    "goal_posted": Template(
        "goals", "Org goal post",
        title="{icon} {heading}: {title}",
        description=("{progress}\n{lines}\n\n"
                     "{description}\n\n"
                     "{posted} by {poster}. {call_to_action}"),
        vars={"icon": Var("🎯 / 🔓 / ⛏", "🎯"),
              "heading": Var("New org goal / Goal update", "New org goal"),
              "title": Var("Goal title", "Build an Idris", md=True),
              "progress": Var("Progress line (% filled, target, due)", "41% filled · due <t:1796083200:D>"),
              "lines": Var("Line items with fill", "▫️ Iron (≥Q500) — 10/40 SCU", md=True),
              "description": Var("Goal description (its formatting is kept)", "Org priority.", md=True, fmt=True),
              "posted": Var("Posted / Re-posted", "Posted"),
              "poster": Var("Posted by", "Bolvangar", md=True),
              "call_to_action": Var("What to do next", "Log what you're holding to contribute.")},
        color=0x4FC3F7, color_note="Green once the goal is met."),
    "op_closed": Template(
        "ops", "Op record",
        title="📜 {heading}: {name}",
        description=None,          # attendance / money / loot are built in code
        vars={"heading": Var("Op record / Op record updated", "Op record"),
              "name": Var("Op name", "Rockbreaker", md=True)},
        color=0x4FC3F7, color_note="Amber when an updated record is posted."),
}
