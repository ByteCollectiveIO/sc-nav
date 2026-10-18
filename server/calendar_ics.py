"""iCalendar (RFC 5545) serialization for the event planner.

Two consumers share this one serializer: the per-event "Add to calendar" .ics
download (a one-time copy) and a member's subscribed calendar feed (re-fetched
by Google / Apple / Outlook on their own schedule). Pure functions, no app
state, so the feed's whole wire format is unit-testable.

Event times are stored in UTC, so every DTSTART/DTEND is a UTC `Z` stamp and
the calendar carries no VTIMEZONE — the client renders local time itself.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

PRODID = "-//ByteCollective//Org Navigator Events//EN"
# Default length for an event saved without a duration — a calendar entry needs
# an end, and an hour reads as "about an hour" rather than claiming precision.
DEFAULT_DURATION_MIN = 60
# How often the subscribing client should re-fetch. Apple Calendar and Outlook
# honor these hints; Google Calendar ignores them and polls on its own schedule
# (observed ~12–24 h), which the subscribe UI says out loud.
REFRESH_INTERVAL = "PT1H"


def escape_text(s: str) -> str:
    """TEXT value escaping (RFC 5545 §3.3.11): backslash, semicolon, comma,
    newline. Without it a rally point like "Port Olisar, Stanton" splits the
    property, and a newline in a title ends it early (a property injection)."""
    return (str(s).replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,")
            .replace("\r\n", "\\n").replace("\r", "\\n").replace("\n", "\\n"))


def fold(line: str) -> str:
    """Fold a content line at 75 octets (RFC 5545 §3.1), never inside a UTF-8
    sequence — a split multi-byte character corrupts the title in every client."""
    out, cur, size = [], "", 0
    for ch in line:
        n = len(ch.encode("utf-8"))
        if size + n > 75:
            out.append(cur)
            cur, size = " ", 1        # continuation lines start with one space
        cur += ch
        size += n
    out.append(cur)
    return "\r\n".join(out)


def _parse(iso: str | None) -> datetime | None:
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _stamp(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def event_window(ev: dict) -> tuple[datetime, datetime] | None:
    """(start, end) in UTC for an event, or None when it has no usable start."""
    start = _parse(ev.get("start_at"))
    if start is None:
        return None
    minutes = ev.get("duration_min") or DEFAULT_DURATION_MIN
    return start, start + timedelta(minutes=int(minutes))


def description(ev: dict, url: str | None) -> str:
    """The basics, and nothing a forwarded link shouldn't carry: where to meet,
    where it happens, and a link back to the full event (roster, notes)."""
    lines = []
    if ev.get("location"):
        lines.append(f"Rally: {ev['location']}")
    if ev.get("event_location"):
        lines.append(f"Event: {ev['event_location']}")
    if url:
        lines.append(url)
    return "\n".join(lines)


def vevent(ev: dict, *, uid_host: str, url: str | None, now: datetime) -> list[str]:
    """One VEVENT's content lines (unfolded), or [] for an event with no start.

    The UID is stable per event so a re-fetched feed (or a re-downloaded .ics)
    UPDATES the entry instead of duplicating it. A cancelled event stays in the
    feed as STATUS:CANCELLED — clients that honor it strike or drop it, and the
    "Cancelled:" prefix covers the ones that don't — rather than vanishing,
    which a subscriber can't tell apart from a sync glitch."""
    win = event_window(ev)
    if win is None:
        return []
    start, end = win
    cancelled = ev.get("status") == "cancelled"
    title = (ev.get("title") or "Event").strip() or "Event"
    if cancelled:
        title = f"Cancelled: {title}"
    lines = [
        "BEGIN:VEVENT",
        f"UID:event-{int(ev['id'])}@{uid_host}",
        f"DTSTAMP:{_stamp(now)}",
        f"DTSTART:{_stamp(start)}",
        f"DTEND:{_stamp(end)}",
        f"SUMMARY:{escape_text(title)}",
        f"STATUS:{'CANCELLED' if cancelled else 'CONFIRMED'}",
    ]
    updated = _parse(ev.get("updated_at"))
    if updated:
        lines.append(f"LAST-MODIFIED:{_stamp(updated)}")
    where = ev.get("location") or ev.get("event_location")
    if where:
        lines.append(f"LOCATION:{escape_text(where)}")
    desc = description(ev, url)
    if desc:
        lines.append(f"DESCRIPTION:{escape_text(desc)}")
    if url:
        lines.append(f"URL:{url}")
    lines.append("END:VEVENT")
    return lines


def calendar(events: list[dict], *, uid_host: str, url_for,
             feed_name: str | None = None, now: datetime | None = None) -> str:
    """A whole VCALENDAR document (CRLF line endings, folded). `url_for(ev)`
    returns the event's link back into the app, or None.

    `feed_name` marks a SUBSCRIBED feed: it names the calendar and carries the
    refresh hint. A one-off .ics download leaves it None — a calendar name there
    makes some clients offer to create a whole new calendar for one event."""
    now = now or datetime.now(timezone.utc)
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        f"PRODID:{PRODID}",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
    ]
    if feed_name:
        lines += [
            f"X-WR-CALNAME:{escape_text(feed_name)}",
            f"REFRESH-INTERVAL;VALUE=DURATION:{REFRESH_INTERVAL}",
            f"X-PUBLISHED-TTL:{REFRESH_INTERVAL}",
        ]
    for ev in events:
        lines.extend(vevent(ev, uid_host=uid_host, url=url_for(ev), now=now))
    lines.append("END:VCALENDAR")
    return "\r\n".join(fold(l) for l in lines) + "\r\n"
