"""HTTP-level tests for the app shell's CSP nonce backstop (backlog #9).

Unlike test_nav_core.py (pure stdlib), this imports `app` and drives it through
a TestClient, so it needs the runtime deps (fastapi/starlette/httpx). It runs as
its own CI step. Importing `app` boots offline: the feed loaders fall back to the
committed poi/ cache when the live fetch fails, so no network is required.

What it pins down — the things a pure unit test can't:
  * the per-request nonce stamped into the inline <script> matches the nonce in
    that response's Content-Security-Policy header (middleware-before-route
    ordering — the documented failure mode in the design),
  * script-src carries a nonce and NOT 'unsafe-inline' (the whole point: an
    injected inline script won't execute even if an esc() is ever missed),
  * the shell is served no-store so a cached document can't pin a stale nonce.
"""

import asyncio
import re
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi.testclient import TestClient

import app
import db
import notify

_NONCE_IN_SCRIPT = re.compile(r'<script nonce="([A-Za-z0-9_-]+)">')


def _script_src(csp: str) -> str:
    """Return just the script-src directive from a CSP header string."""
    for directive in csp.split(";"):
        directive = directive.strip()
        if directive.startswith("script-src"):
            return directive
    return ""


class CspNonceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(app.app)

    def _assert_shell(self, path):
        r = self.client.get(path)
        self.assertEqual(r.status_code, 200)

        # Exactly one inline <script>, and it carries a nonce (guards a future
        # dev adding an un-nonced inline script that the CSP would then block).
        scripts = re.findall(r"<script\b[^>]*>", r.text)
        self.assertEqual(len(scripts), 1, f"expected one inline <script>, got {scripts}")
        m = _NONCE_IN_SCRIPT.search(r.text)
        self.assertIsNotNone(m, "inline <script> is missing a nonce attribute")
        body_nonce = m.group(1)

        csp = r.headers.get("content-security-policy", "")
        script_src = _script_src(csp)
        self.assertIn(f"'nonce-{body_nonce}'", script_src,
                      "CSP script-src nonce must match the one stamped in the body")
        self.assertNotIn("'unsafe-inline'", script_src,
                         "script-src must not allow 'unsafe-inline' (defeats the nonce)")

        # A cached shell would pin a stale nonce and dead-script the app.
        self.assertEqual(r.headers.get("cache-control"), "no-store")
        return body_nonce

    def test_root_shell(self):
        self._assert_shell("/")

    def test_index_html_shell(self):
        self._assert_shell("/index.html")

    def test_nonce_is_fresh_per_request(self):
        # Different nonce each load proves the middleware runs per request and
        # the route reads the value the middleware just set (ordering holds).
        first = self._assert_shell("/")
        second = self._assert_shell("/")
        self.assertNotEqual(first, second)

    def test_csp_directive_shape(self):
        csp = app._csp("TESTNONCE")
        self.assertIn("script-src 'self' 'nonce-TESTNONCE'", csp)
        self.assertNotIn("'unsafe-inline'", _script_src(csp))
        self.assertIn("object-src 'none'", csp)
        self.assertIn("frame-ancestors 'none'", csp)
        self.assertIn("base-uri 'self'", csp)


_GOOD_WEBHOOK = "https://discord.com/api/webhooks/123456789012345678/AbCd-eF_gh1234"


class NotifyValidationTests(unittest.TestCase):
    """The webhook URL is a credential the admin form writes; validation is the
    anti-SSRF / open-relay guard, and masking must never leak the token."""

    def test_accepts_real_discord_hosts(self):
        for url in (
            _GOOD_WEBHOOK,
            "https://discordapp.com/api/webhooks/1/abcd",
            "https://canary.discord.com/api/webhooks/1/abcd",
            "https://ptb.discord.com/api/webhooks/1/abcd",
        ):
            self.assertTrue(notify.is_valid_webhook_url(url), url)

    def test_rejects_ssrf_and_malformed(self):
        for url in (
            "http://discord.com/api/webhooks/1/abcd",       # not https
            "https://evil.com/api/webhooks/1/abcd",          # wrong host
            "https://discord.com.evil.com/api/webhooks/1/x", # host-suffix trick
            "https://discord.com/api/webhooks/",             # no id/token
            "https://internal/api/webhooks/1/x",             # internal host
            "", "not a url",
        ):
            self.assertFalse(notify.is_valid_webhook_url(url), url)

    def test_mask_never_reveals_url(self):
        masked = notify.mask(_GOOD_WEBHOOK)
        self.assertNotIn("discord", masked)
        self.assertNotIn("123456789", masked)
        self.assertTrue(masked.startswith("…"))
        self.assertEqual(notify.mask(""), "")


class DiscordSettingsTests(unittest.TestCase):
    """The settings round-trip must store the webhook but never echo it back."""

    @classmethod
    def setUpClass(cls):
        # Point the DB at a throwaway file so the test never writes the real
        # meta table (import app already ran db.init on the real db).
        cls._tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        cls._tmp.close()
        db.init(Path(cls._tmp.name))
        cls._admin = {"id": "1", "username": "tester", "is_admin": True}
        # require_session reads request.session — bypass it with an override.
        app.app.dependency_overrides[app.require_session] = lambda: cls._admin
        # auth_gate is middleware (runs before DI), so it can't see the override;
        # it accepts a bearer-token user, so stub token_user to satisfy the gate.
        cls._orig_token_user = app.token_user
        app.token_user = lambda request: cls._admin
        cls.client = TestClient(app.app)

    @classmethod
    def tearDownClass(cls):
        app.app.dependency_overrides.clear()
        app.token_user = cls._orig_token_user
        Path(cls._tmp.name).unlink(missing_ok=True)

    def setUp(self):
        # Clean webhook state per test (the shared temp DB carries over otherwise).
        for c in notify.CATEGORIES:
            db.set_setting(notify._webhook_key(c), "")
        db.set_setting(notify._LEGACY_WEBHOOK_KEY, "")

    def test_per_category_store_mask_validate_roundtrip(self):
        # Reject a non-Discord URL (anti-SSRF) ...
        bad = self.client.post("/api/settings",
                               json={"discord_webhooks": {"events": "https://evil.com/x"}})
        self.assertEqual(bad.status_code, 400)
        # ... and an unknown category.
        self.assertEqual(self.client.post(
            "/api/settings", json={"discord_webhooks": {"bogus": _GOOD_WEBHOOK}}).status_code, 400)

        # Route events to one channel; leave the others off.
        ok = self.client.post("/api/settings",
                              json={"discord_webhooks": {"events": _GOOD_WEBHOOK}})
        self.assertEqual(ok.status_code, 200)

        s = self.client.get("/api/settings").json()
        wh = s["discord_webhooks"]
        self.assertTrue(wh["events"]["set"])
        self.assertFalse(wh["marketplace"]["set"])
        # The raw secret must NEVER appear in any response.
        self.assertNotIn(_GOOD_WEBHOOK, ok.text)
        self.assertNotIn(_GOOD_WEBHOOK, self.client.get("/api/settings").text)
        self.assertTrue(wh["events"]["tail"].startswith("…"))

        # A blank patch for one category leaves others untouched; "" clears just it.
        self.client.post("/api/settings", json={"discord_webhooks": {"events": ""}})
        self.assertFalse(self.client.get("/api/settings").json()["discord_webhooks"]["events"]["set"])

    def test_test_send_requires_category_and_webhook(self):
        self.client.post("/api/settings", json={"discord_webhooks": {"events": ""}})
        # No webhook for the category -> 400.
        r = self.client.post("/api/settings/discord/test", json={"category": "events"})
        self.assertEqual(r.status_code, 400)
        # Unknown category -> 400.
        r = self.client.post("/api/settings/discord/test", json={"category": "bogus"})
        self.assertEqual(r.status_code, 400)

    def test_legacy_webhook_migrates_to_all_categories(self):
        # Simulate a v0.13.0 install: one shared key, no per-category keys.
        for c in notify.CATEGORIES:
            db.set_setting(notify._webhook_key(c), "")
        db.set_setting(notify._LEGACY_WEBHOOK_KEY, _GOOD_WEBHOOK)
        notify.migrate_legacy_webhook()
        wh = self.client.get("/api/settings").json()["discord_webhooks"]
        self.assertTrue(all(wh[c]["set"] for c in notify.CATEGORIES))
        # Legacy key consumed -> a second run is a no-op.
        self.assertEqual(db.get_setting(notify._LEGACY_WEBHOOK_KEY), "")


class EventNotifyTests(unittest.TestCase):
    """The inline event notifications: fire when the toggle is on, stay silent
    when off / no webhook, and format a member-friendly, timezone-aware message."""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        cls._tmp.close()
        db.init(Path(cls._tmp.name))
        # The events builder gates on a valid webhook for the 'events' category;
        # set one so the enabled path fires. We flip it off in the disabled test.
        db.set_setting(notify._webhook_key("events"), _GOOD_WEBHOOK)
        cls._orig_send = notify.send

    @classmethod
    def tearDownClass(cls):
        notify.send = cls._orig_send
        Path(cls._tmp.name).unlink(missing_ok=True)

    def setUp(self):
        self.sent = []

        async def _capture(category, text, *, mentions=None, dedup_key=None):
            self.sent.append({"category": category, "text": text,
                              "mentions": mentions, "dedup_key": dedup_key})
            return True
        notify.send = _capture

    _EV = {"id": 7, "title": "Xenothreat Push", "start_at": "2026-07-01T18:00:00+00:00",
           "event_location": "Pyro Gateway", "location": "Everus Harbor"}

    def test_created_fires_when_configured(self):
        db.set_setting(notify._webhook_key("events"), _GOOD_WEBHOOK)
        asyncio.run(app._notify_event_created(self._EV))
        self.assertEqual(len(self.sent), 1)
        msg = self.sent[0]
        self.assertEqual(msg["category"], "events")       # routed to the events channel
        self.assertIn("Xenothreat Push", msg["text"])
        self.assertIn("Pyro Gateway", msg["text"])       # event_location preferred
        self.assertIn("<t:", msg["text"])                 # timezone-aware Discord timestamp
        self.assertEqual(msg["dedup_key"], "event-created:7")
        self.assertIsNone(msg["mentions"])                # a broadcast, pings nobody

    def test_suppressed_when_no_webhook(self):
        db.set_setting(notify._webhook_key("events"), "")
        asyncio.run(app._notify_event_created(self._EV))
        asyncio.run(app._notify_event_cancelled(self._EV))
        self.assertEqual(self.sent, [])

    def test_cancelled_message(self):
        db.set_setting(notify._webhook_key("events"), _GOOD_WEBHOOK)
        asyncio.run(app._notify_event_cancelled(self._EV))
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.sent[0]["category"], "events")
        self.assertIn("cancelled", self.sent[0]["text"].lower())
        self.assertEqual(self.sent[0]["dedup_key"], "event-cancelled:7")

    def test_discord_ts_and_deep_link(self):
        self.assertTrue(app._discord_ts("2026-07-01T18:00:00+00:00").startswith("<t:"))
        self.assertEqual(app._discord_ts("not-a-date"), "not-a-date")
        # No SC_NAV_PUBLIC_URL configured in the test env -> no link fragment.
        self.assertEqual(app._deep_link("#/events"), "")


class EventReminderTests(unittest.TestCase):
    """Step 3 — the scheduled 'starting soon' reminder: its due-window query, the
    atomic reminded_at claim (idempotency), and the attendee-pinging message."""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        cls._tmp.close()
        db.init(Path(cls._tmp.name))
        db.set_setting(notify._webhook_key("events"), _GOOD_WEBHOOK)
        cls._orig_send = notify.send

    @classmethod
    def tearDownClass(cls):
        notify.send = cls._orig_send
        Path(cls._tmp.name).unlink(missing_ok=True)

    def setUp(self):
        self.sent = []

        async def _capture(category, text, *, mentions=None, dedup_key=None):
            self.sent.append({"category": category, "text": text,
                              "mentions": mentions, "dedup_key": dedup_key})
            return True
        notify.send = _capture

    def _mk_event(self, start_at, **extra):
        now = datetime.now(timezone.utc).isoformat()
        return db.create_event({"organizer_id": "1", "title": extra.pop("title", "Op"),
                                "start_at": start_at, "status": "scheduled",
                                "created_at": now, "updated_at": now, **extra})

    def test_due_window_selects_only_in_window_scheduled(self):
        now = datetime.now(timezone.utc)
        soon_id = self._mk_event((now + timedelta(minutes=10)).isoformat(), title="Soon")
        self._mk_event((now + timedelta(hours=5)).isoformat(), title="Far")   # outside lead
        self._mk_event((now - timedelta(minutes=10)).isoformat(), title="Past")  # already started
        until = (now + timedelta(minutes=30)).isoformat()
        due = db.events_due_for_reminder(now.isoformat(), until)
        self.assertEqual([e["id"] for e in due], [soon_id])

    def test_mark_reminded_is_an_atomic_claim(self):
        now = datetime.now(timezone.utc)
        eid = self._mk_event((now + timedelta(minutes=5)).isoformat())
        self.assertTrue(db.mark_event_reminded(eid, now.isoformat()))    # first claim wins
        self.assertFalse(db.mark_event_reminded(eid, now.isoformat()))   # racing tick loses
        until = (now + timedelta(minutes=30)).isoformat()
        due_ids = [e["id"] for e in db.events_due_for_reminder(now.isoformat(), until)]
        self.assertNotIn(eid, due_ids)   # a claimed event no longer surfaces as due

    def test_reminder_pings_active_signups_only(self):
        now = datetime.now(timezone.utc)
        eid = self._mk_event((now + timedelta(minutes=5)).isoformat(),
                             title="Bounty Run", event_location="Grim HEX")
        db.upsert_signup(eid, "111", ["dps"], "going", None, now.isoformat())
        db.upsert_signup(eid, "222", ["medic"], "withdrawn", None, now.isoformat())
        asyncio.run(app._notify_event_reminder(db.get_event(eid)))
        self.assertEqual(len(self.sent), 1)
        msg = self.sent[0]
        self.assertEqual(msg["category"], "events")
        self.assertIn("Bounty Run", msg["text"])
        self.assertIn("Grim HEX", msg["text"])
        self.assertIn("<t:", msg["text"])                 # timezone-aware timestamp
        self.assertIn("<@111>", msg["text"])              # active attendee pinged
        self.assertNotIn("<@222>", msg["text"])           # withdrawn -> not pinged
        self.assertEqual(msg["mentions"], ["111"])
        self.assertEqual(msg["dedup_key"], f"event-reminder:{eid}")

    def test_reminder_suppressed_when_no_webhook(self):
        db.set_setting(notify._webhook_key("events"), "")
        try:
            now = datetime.now(timezone.utc)
            eid = self._mk_event((now + timedelta(minutes=5)).isoformat())
            asyncio.run(app._notify_event_reminder(db.get_event(eid)))
            self.assertEqual(self.sent, [])
        finally:
            db.set_setting(notify._webhook_key("events"), _GOOD_WEBHOOK)


class MarketNotifyTests(unittest.TestCase):
    """Step 4 — the inline marketplace pings: a new offer / instant buy alerts the
    seller, an accept alerts the bidder, and the dual-confirm handshake nudges the
    other party then celebrates completion. Each directs the ping with an `<@id>`
    mention, and all stay silent when the marketplace webhook is unset."""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        cls._tmp.close()
        db.init(Path(cls._tmp.name))
        db.set_setting(notify._webhook_key("marketplace"), _GOOD_WEBHOOK)
        cls._orig_send = notify.send

    @classmethod
    def tearDownClass(cls):
        notify.send = cls._orig_send
        Path(cls._tmp.name).unlink(missing_ok=True)

    def setUp(self):
        self.sent = []

        async def _capture(category, text, *, mentions=None, dedup_key=None):
            self.sent.append({"category": category, "text": text,
                              "mentions": mentions, "dedup_key": dedup_key})
            return True
        notify.send = _capture

    # seller / buyer / bidder are real Discord snowflakes so they're pingable.
    _LISTING = {"id": 5, "seller_id": "111", "buyer_id": "222",
                "item_name": "Quantanium", "final_auec": 250000}
    _OFFER = {"id": 9, "bidder_id": "222", "amount_auec": 120000}

    def test_new_offer_alerts_the_seller(self):
        asyncio.run(app._notify_market_offer(self._LISTING, self._OFFER, deal=False))
        self.assertEqual(len(self.sent), 1)
        msg = self.sent[0]
        self.assertEqual(msg["category"], "marketplace")
        self.assertIn("New offer", msg["text"])
        self.assertIn("Quantanium", msg["text"])
        self.assertIn("120,000 aUEC", msg["text"])
        self.assertIn("<@111>", msg["text"])           # the seller is pinged
        self.assertEqual(msg["mentions"], ["111"])
        self.assertEqual(msg["dedup_key"], "market-offer:9")

    def test_instant_buy_reads_as_a_sale(self):
        asyncio.run(app._notify_market_offer(self._LISTING, self._OFFER, deal=True))
        self.assertEqual(len(self.sent), 1)
        self.assertIn("sold", self.sent[0]["text"].lower())
        self.assertEqual(self.sent[0]["mentions"], ["111"])   # still the seller

    def test_barter_offer_has_no_auec(self):
        offer = {"id": 3, "bidder_id": "222", "amount_auec": None,
                 "offer_item_name": "Titanium", "offer_note": "swap?"}
        asyncio.run(app._notify_market_offer(self._LISTING, offer, deal=False))
        text = self.sent[0]["text"]
        self.assertIn("Titanium", text)
        self.assertNotIn("aUEC", text)

    def test_accept_alerts_the_bidder(self):
        asyncio.run(app._notify_market_accepted(self._LISTING, self._OFFER))
        msg = self.sent[0]
        self.assertIn("accepted", msg["text"].lower())
        self.assertIn("<@222>", msg["text"])           # the bidder, not the seller
        self.assertEqual(msg["mentions"], ["222"])
        self.assertEqual(msg["dedup_key"], "market-accept:9")

    def test_confirm_nudges_the_other_side(self):
        asyncio.run(app._notify_market_confirm_needed(self._LISTING, confirmed_by="seller"))
        msg = self.sent[0]
        self.assertIn("Confirm", msg["text"])
        self.assertEqual(msg["mentions"], ["222"])      # seller confirmed -> nudge buyer
        self.assertEqual(msg["dedup_key"], "market-confirm:5:seller")

    def test_completed_pings_both_parties(self):
        asyncio.run(app._notify_market_completed(self._LISTING))
        msg = self.sent[0]
        self.assertIn("Deal complete", msg["text"])
        self.assertIn("250,000 aUEC", msg["text"])
        self.assertIn("<@111>", msg["text"])
        self.assertIn("<@222>", msg["text"])
        self.assertEqual(msg["mentions"], ["111", "222"])
        self.assertEqual(msg["dedup_key"], "market-complete:5")

    def test_legacy_ids_are_not_pinged(self):
        # A non-numeric (legacy/synthetic) id can't be a Discord mention -> dropped.
        listing = {**self._LISTING, "seller_id": "legacy-seller"}
        asyncio.run(app._notify_market_offer(listing, self._OFFER, deal=False))
        self.assertEqual(self.sent[0]["mentions"], [])
        self.assertNotIn("<@", self.sent[0]["text"])

    def test_silent_when_no_webhook(self):
        db.set_setting(notify._webhook_key("marketplace"), "")
        try:
            asyncio.run(app._notify_market_offer(self._LISTING, self._OFFER, deal=False))
            asyncio.run(app._notify_market_accepted(self._LISTING, self._OFFER))
            asyncio.run(app._notify_market_completed(self._LISTING))
            self.assertEqual(self.sent, [])
        finally:
            db.set_setting(notify._webhook_key("marketplace"), _GOOD_WEBHOOK)


class GoalRecordNotifyTests(unittest.TestCase):
    """Step 5 — the goals-100% and org-hauling-record pings: each fires only when
    its category webhook is set, directs the ping with an `<@id>` mention, and
    stays silent otherwise."""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        cls._tmp.close()
        db.init(Path(cls._tmp.name))
        db.set_setting(notify._webhook_key("goals"), _GOOD_WEBHOOK)
        db.set_setting(notify._webhook_key("records"), _GOOD_WEBHOOK)
        cls._orig_send = notify.send

    @classmethod
    def tearDownClass(cls):
        notify.send = cls._orig_send
        Path(cls._tmp.name).unlink(missing_ok=True)

    def setUp(self):
        self.sent = []

        async def _capture(category, text, *, mentions=None, dedup_key=None):
            self.sent.append({"category": category, "text": text,
                              "mentions": mentions, "dedup_key": dedup_key})
            return True
        notify.send = _capture

    _GOAL = {"id": 4, "creator_id": "111", "title": "Fund the Idris"}
    _CONTRIB = [{"owner_id": "111", "qty": 5}, {"owner_id": "222", "qty": 3}]

    def test_goal_met_pings_the_creator(self):
        asyncio.run(app._notify_goal_met(self._GOAL, self._CONTRIB))
        self.assertEqual(len(self.sent), 1)
        msg = self.sent[0]
        self.assertEqual(msg["category"], "goals")
        self.assertIn("Fund the Idris", msg["text"])
        self.assertIn("2 contributors", msg["text"])
        self.assertIn("<@111>", msg["text"])            # the creator is pinged
        self.assertEqual(msg["mentions"], ["111"])
        self.assertEqual(msg["dedup_key"], "goal-met:4")

    def test_goal_met_silent_when_no_webhook(self):
        db.set_setting(notify._webhook_key("goals"), "")
        try:
            asyncio.run(app._notify_goal_met(self._GOAL, self._CONTRIB))
            self.assertEqual(self.sent, [])
        finally:
            db.set_setting(notify._webhook_key("goals"), _GOOD_WEBHOOK)

    def test_record_brags_both_metrics(self):
        run = {"id": 8, "total_reward": 900000, "total_time_s": 1800}
        asyncio.run(app._notify_hauling_record(
            "111", run, {"total": 900000, "rate": 1800000}))
        msg = self.sent[0]
        self.assertEqual(msg["category"], "records")
        self.assertIn("record", msg["text"].lower())
        self.assertIn("900,000 aUEC", msg["text"])       # single-run total
        self.assertIn("1,800,000 aUEC/hr", msg["text"])  # efficiency
        self.assertIn("<@111>", msg["text"])             # the hauler is pinged
        self.assertEqual(msg["mentions"], ["111"])
        self.assertEqual(msg["dedup_key"], "hauling-record:8")

    def test_record_only_the_broken_metric_shows(self):
        run = {"id": 9, "total_reward": 500000}
        asyncio.run(app._notify_hauling_record("111", run, {"total": 500000}))
        text = self.sent[0]["text"]
        self.assertIn("single-run", text)
        self.assertNotIn("efficiency", text)

    def test_record_silent_when_no_webhook(self):
        db.set_setting(notify._webhook_key("records"), "")
        try:
            asyncio.run(app._notify_hauling_record(
                "111", {"id": 1}, {"total": 1}))
            self.assertEqual(self.sent, [])
        finally:
            db.set_setting(notify._webhook_key("records"), _GOOD_WEBHOOK)


class OnlineRosterTests(unittest.TestCase):
    """Backlog #19 step 1 — the who's-online roster: join/heartbeat/leave
    lifecycle, visibility gating (the seam "appear offline" plugs into), and the
    available-first ordering, plus the GET /api/online snapshot."""

    @classmethod
    def setUpClass(cls):
        # Temp DB: step 2 persists online prefs, so keep it off the real members table.
        cls._tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        cls._tmp.close()
        db.init(Path(cls._tmp.name))
        cls._member = {"id": "1", "username": "tester", "is_admin": False}
        app.app.dependency_overrides[app.require_session] = lambda: cls._member
        cls._orig_token_user = app.token_user
        app.token_user = lambda request: cls._member
        cls.client = TestClient(app.app)

    @classmethod
    def tearDownClass(cls):
        app.app.dependency_overrides.clear()
        app.token_user = cls._orig_token_user
        Path(cls._tmp.name).unlink(missing_ok=True)

    def setUp(self):
        app.hub.online.clear()   # isolate from any live connections
        db.set_online_prefs("1", "available", None, False)   # reset member 1's prefs

    @staticmethod
    def _sess(uid, name=None):
        return app.Session({"id": uid, "display_name": name, "is_admin": False})

    def test_mark_add_then_heartbeat(self):
        s = self._sess("7", "Zoe")
        self.assertTrue(app.hub.mark_online(s))    # first call = arrival
        self.assertFalse(app.hub.mark_online(s))   # second = heartbeat, not a re-add
        self.assertEqual(app.hub.online_count(), 1)

    def test_drop_removes_and_reports(self):
        app.hub.mark_online(self._sess("7"))
        self.assertTrue(app.hub.drop_online("7"))
        self.assertFalse(app.hub.drop_online("7"))  # already gone
        self.assertEqual(app.hub.online_count(), 0)

    def test_invisible_member_excluded_from_roster_and_count(self):
        app.hub.mark_online(self._sess("7", "Zoe"))
        app.hub.mark_online(self._sess("8", "Amy"))
        app.hub.online["8"]["visible"] = False     # the "appear offline" seam
        roster = app.hub.online_roster()
        self.assertEqual([r["discord_id"] for r in roster], ["7"])
        self.assertEqual(app.hub.online_count(), 1)

    def test_roster_orders_available_before_busy_before_afk(self):
        for uid in ("2", "3", "4"):
            app.hub.mark_online(self._sess(uid))
        app.hub.online["2"]["status"] = "afk"
        app.hub.online["3"]["status"] = "busy"
        app.hub.online["4"]["status"] = "available"
        order = [r["discord_id"] for r in app.hub.online_roster()]
        self.assertEqual(order, ["4", "3", "2"])

    def test_location_only_when_sharing_presence(self):
        app.hub.mark_online(self._sess("7", "Zoe"))
        self.assertNotIn("location", app.hub.online_roster()[0])
        app.hub.presence["7"] = {"system": "Stanton", "body": "Daymar",
                                 "shard": "abc", "lat": 0, "lon": 0,
                                 "heading": None, "last_update": 0}
        loc = app.hub.online_roster()[0]["location"]
        self.assertEqual(loc["body"], "Daymar")
        app.hub.presence.pop("7", None)

    def test_api_online_snapshot(self):
        app.hub.mark_online(self._sess("7", "Zoe"))
        r = self.client.get("/api/online")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["count"], 1)
        self.assertEqual(body["users"][0]["discord_id"], "7")
        self.assertEqual(body["me"], {"status": "available", "activity": None,
                                      "appear_offline": False})

    # --- step 2: manual status / activity / appear-offline ------------------
    def test_status_persists_and_applies_to_live_record(self):
        app.hub.mark_online(self._sess("1", "Me"))   # caller is member "1"
        r = self.client.post("/api/online/status",
                             json={"status": "busy", "activity": "hauling to A18"})
        self.assertEqual(r.status_code, 200)
        # Applied to the live roster record ...
        rec = app.hub.online["1"]
        self.assertEqual((rec["status"], rec["activity"]), ("busy", "hauling to A18"))
        # ... and persisted so a reconnect (fresh mark_online) restores it.
        app.hub.online.clear()
        app.hub.mark_online(self._sess("1", "Me"))
        self.assertEqual(app.hub.online["1"]["status"], "busy")
        self.assertEqual(app.hub.online["1"]["activity"], "hauling to A18")

    def test_bad_status_falls_back_to_available(self):
        self.client.post("/api/online/status", json={"status": "party"})
        self.assertEqual(self._my_prefs()["status"], "available")

    def test_blank_activity_stored_as_null(self):
        self.client.post("/api/online/status", json={"status": "afk", "activity": "   "})
        self.assertIsNone(self._my_prefs()["activity"])

    def test_appear_offline_hides_from_roster_and_count(self):
        app.hub.mark_online(self._sess("1", "Me"))
        app.hub.mark_online(self._sess("9", "Bo"))
        self.client.post("/api/online/status",
                        json={"status": "available", "appear_offline": True})
        ids = [u["discord_id"] for u in app.hub.online_roster()]
        self.assertEqual(ids, ["9"])              # caller "1" is hidden
        self.assertEqual(app.hub.online_count(), 1)
        # The choice sticks across reconnect (visible stays False).
        app.hub.online.pop("1", None)
        app.hub.mark_online(self._sess("1", "Me"))
        self.assertFalse(app.hub.online["1"]["visible"])

    def test_playstyles_served(self):
        tags = self.client.get("/api/playstyles").json()["tags"]
        self.assertIn("mining", tags)
        self.assertIn("PvP", tags)

    def _my_prefs(self):
        return self.client.get("/api/online").json()["me"]


class PresenceTrailTests(unittest.TestCase):
    """Teammate presence now carries the member's current-body breadcrumb trail so
    everyone can see where the org has already scouted (avoids duplicate mapping)."""

    @staticmethod
    def _sess(uid):
        s = app.Session({"id": uid, "display_name": "Scout", "is_admin": False})
        s.nav_state = {
            "system": "Stanton",
            "container": {"name": "Daymar", "is_body": True, "body_radius_m": 295000.0},
            "latitude": 1.0, "longitude": 2.0,
        }
        s.shard = "shard-1"
        return s

    def setUp(self):
        app.hub.presence.clear()
        app.hub._dirty.clear()
        app.hub._removed.clear()

    def test_shares_only_current_body_crumbs(self):
        s = self._sess("9")
        s.path = [
            {"lat": 1.0, "lon": 2.0, "container": "Daymar"},
            {"lat": 1.1, "lon": 2.1, "container": "Daymar"},
            {"lat": 5.0, "lon": 5.0, "container": "Yela"},   # other body — excluded
        ]
        rec = app.hub._presence_record(s)
        self.assertEqual(rec["path"], [{"lat": 1.0, "lon": 2.0}, {"lat": 1.1, "lon": 2.1}])
        # The wire form (what tabs actually receive) carries the trail through.
        self.assertEqual(app.hub._public_presence(rec)["path"], rec["path"])

    def test_trail_capped_to_most_recent(self):
        s = self._sess("9")
        n = app.SHARED_PATH_MAX + 50
        s.path = [{"lat": i, "lon": i, "container": "Daymar"} for i in range(n)]
        rec = app.hub._presence_record(s)
        self.assertEqual(len(rec["path"]), app.SHARED_PATH_MAX)
        self.assertEqual(rec["path"][-1], {"lat": n - 1, "lon": n - 1})  # keeps the tail


class LFGBoardTests(unittest.TestCase):
    """Backlog #19 step 3 — the looking-for-group board: the two directions
    (LFM/LFJ), post-supersede, join/ping toggle + slot cap, close permissions,
    offline + expiry cleanup, and the REST surface."""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        cls._tmp.close()
        db.init(Path(cls._tmp.name))
        cls._member = {"id": "1", "username": "tester", "is_admin": False}
        app.app.dependency_overrides[app.require_session] = lambda: cls._member
        cls._orig_token_user = app.token_user
        app.token_user = lambda request: cls._member
        cls.client = TestClient(app.app)

    @classmethod
    def tearDownClass(cls):
        app.app.dependency_overrides.clear()
        app.token_user = cls._orig_token_user
        Path(cls._tmp.name).unlink(missing_ok=True)

    def setUp(self):
        app.hub.lfg.clear()
        db.lfg_delete([e["id"] for e in db.lfg_all()])   # wipe the persisted board too
        app.hub._lfg_seq = 0
        self._member["is_admin"] = False

    def test_post_lfm_and_lfj_coexist_per_member(self):
        app.hub.post_lfg("7", "lfm", ["bunkers"], 2, "need 2", None, False)
        app.hub.post_lfg("7", "lfj", ["mining"], None, "solo", None, False)
        dirs = sorted(e["direction"] for e in app.hub.lfg.values())
        self.assertEqual(dirs, ["lfj", "lfm"])   # one of each survives

    def test_repost_same_direction_supersedes(self):
        first = app.hub.post_lfg("7", "lfm", [], 1, "v1", None, False)
        second = app.hub.post_lfg("7", "lfm", [], 3, "v2", None, False)
        entries = [e for e in app.hub.lfg.values() if e["poster"] == "7"
                   and e["direction"] == "lfm"]
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["id"], second["id"])
        self.assertNotIn(first["id"], app.hub.lfg)

    def test_lfj_never_carries_slots(self):
        e = app.hub.post_lfg("7", "lfj", [], 5, "", None, False)
        self.assertIsNone(e["slots"])

    def test_join_toggles_and_caps_at_slots(self):
        e = app.hub.post_lfg("7", "lfm", [], 1, "", None, False)
        app.hub.join_lfg(e["id"], "8")
        self.assertEqual(e["responders"], ["8"])
        app.hub.join_lfg(e["id"], "9")            # full (slots=1) — ignored
        self.assertEqual(e["responders"], ["8"])
        app.hub.join_lfg(e["id"], "8")            # toggle off = leave
        self.assertEqual(e["responders"], [])

    def test_cannot_join_own_post(self):
        e = app.hub.post_lfg("7", "lfm", [], 3, "", None, False)
        app.hub.join_lfg(e["id"], "7")
        self.assertEqual(e["responders"], [])

    def test_lfj_ping_has_no_cap(self):
        e = app.hub.post_lfg("7", "lfj", [], None, "", None, False)
        for uid in ("8", "9", "10"):
            app.hub.join_lfg(e["id"], uid)
        self.assertEqual(e["responders"], ["8", "9", "10"])

    def test_close_is_poster_or_admin_only(self):
        e = app.hub.post_lfg("7", "lfm", [], 1, "", None, False)
        self.assertFalse(app.hub.close_lfg(e["id"], "8", False))   # stranger
        self.assertTrue(app.hub.close_lfg(e["id"], "8", True))      # admin
        self.assertNotIn(e["id"], app.hub.lfg)
        e2 = app.hub.post_lfg("7", "lfm", [], 1, "", None, False)
        self.assertTrue(app.hub.close_lfg(e2["id"], "7", False))    # poster

    def test_drop_lfg_for_clears_all_of_a_members_posts(self):
        app.hub.post_lfg("7", "lfm", [], 1, "", None, False)
        app.hub.post_lfg("7", "lfj", [], None, "", None, False)
        app.hub.post_lfg("8", "lfm", [], 1, "", None, False)
        self.assertTrue(app.hub.drop_lfg_for("7"))
        self.assertEqual([e["poster"] for e in app.hub.lfg.values()], ["8"])
        self.assertFalse(app.hub.drop_lfg_for("7"))   # nothing left

    def test_prune_removes_only_aged_off(self):
        live = app.hub.post_lfg("7", "lfm", [], 1, "", None, False)
        aged = app.hub.post_lfg("8", "lfm", [], 1, "", None, False)
        aged["created"] = time.time() - (app.lfg_ageoff_min() * 60 + 1)   # past age-off
        self.assertTrue(app.hub.prune_lfg(time.time()))
        self.assertEqual(list(app.hub.lfg), [live["id"]])
        self.assertFalse(app.hub.prune_lfg(time.time()))

    def test_board_persists_and_reloads(self):
        e = app.hub.post_lfg("7", "lfm", ["bunkers"], 2, "need 2", "Daymar", True)
        app.hub.join_lfg(e["id"], "8")
        # Simulate a restart: drop the in-memory board, re-seed from the DB.
        app.hub.lfg.clear()
        for row in db.lfg_all():
            app.hub.lfg[row["id"]] = row
        again = app.hub.lfg[e["id"]]
        self.assertEqual(again["poster"], "7")
        self.assertEqual(again["tags"], ["bunkers"])
        self.assertEqual(again["responders"], ["8"])   # join survived the reload
        self.assertTrue(again["comms"])
        self.assertEqual(again["rally"], "Daymar")

    def test_close_and_supersede_clear_the_db(self):
        e = app.hub.post_lfg("7", "lfm", [], 1, "", None, False)
        app.hub.post_lfg("7", "lfm", [], 3, "", None, False)   # supersedes e
        self.assertNotIn(e["id"], [r["id"] for r in db.lfg_all()])
        [row] = db.lfg_all()
        app.hub.close_lfg(row["id"], "7", False)
        self.assertEqual(db.lfg_all(), [])

    def test_stale_setting_is_clamped_below_ageoff(self):
        db.set_setting("lfg_ageoff_min", "60")
        db.set_setting("lfg_stale_min", "90")     # nonsensical: stale after age-off
        try:
            self.assertEqual(app.lfg_ageoff_min(), 60)
            self.assertLess(app.lfg_stale_min(), 60)   # forced below so green exists
        finally:
            db.set_setting("lfg_ageoff_min", "180")
            db.set_setting("lfg_stale_min", "120")

    def test_public_form_flags_stale_by_age(self):
        e = app.hub.post_lfg("7", "lfm", [], 1, "", None, False)
        self.assertFalse(app.hub._public_lfg(e)["stale"])       # fresh at post time
        e["created"] = time.time() - (app.lfg_stale_min() * 60 + 1)
        pub = app.hub._public_lfg(e)
        self.assertTrue(pub["stale"])                            # past the stale window
        self.assertGreater(pub["expires_s"], 0)                 # but not yet aged off

    def test_public_form_resolves_and_counts(self):
        e = app.hub.post_lfg("7", "lfm", ["bunkers"], 2, "run", "Daymar", True)
        app.hub.join_lfg(e["id"], "8")
        pub = app.hub._public_lfg(e)
        self.assertEqual(pub["direction"], "lfm")
        self.assertEqual(pub["filled"], 1)
        self.assertEqual(pub["slots"], 2)
        self.assertTrue(pub["comms"])
        self.assertEqual(pub["rally"], "Daymar")
        self.assertEqual([r["id"] for r in pub["responders"]], ["8"])

    # --- REST surface -------------------------------------------------------
    def test_api_post_filters_tags_to_vocabulary(self):
        r = self.client.post("/api/lfg", json={
            "direction": "lfm", "tags": ["mining", "not-a-real-tag", "PvP"],
            "slots": 2, "note": "come along"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["tags"], ["mining", "PvP"])

    def test_api_bad_direction_falls_back_to_lfm(self):
        r = self.client.post("/api/lfg", json={"direction": "party"})
        self.assertEqual(r.json()["direction"], "lfm")

    def test_api_snapshot_lists_open_entries(self):
        app.hub.post_lfg("7", "lfm", [], 1, "", None, False)
        app.hub.post_lfg("8", "lfj", [], None, "", None, False)
        body = self.client.get("/api/lfg").json()
        self.assertEqual(body["count"], 2)
        self.assertEqual(len(body["entries"]), 2)

    def test_api_join_toggles_via_endpoint(self):
        e = app.hub.post_lfg("7", "lfm", [], 3, "", None, False)   # caller "1" != poster
        r = self.client.post(f"/api/lfg/{e['id']}/join")
        self.assertEqual(r.json()["filled"], 1)
        r = self.client.post(f"/api/lfg/{e['id']}/join")           # toggle off
        self.assertEqual(r.json()["filled"], 0)

    def test_api_join_missing_entry_404s(self):
        self.assertEqual(self.client.post("/api/lfg/999/join").status_code, 404)

    def test_api_delete_requires_ownership(self):
        e = app.hub.post_lfg("7", "lfm", [], 1, "", None, False)   # someone else's
        self.assertEqual(self.client.delete(f"/api/lfg/{e['id']}").status_code, 404)
        self._member["is_admin"] = True
        self.assertEqual(self.client.delete(f"/api/lfg/{e['id']}").status_code, 200)

    def test_api_delete_own_post(self):
        r = self.client.post("/api/lfg", json={"direction": "lfj"})
        eid = r.json()["id"]
        self.assertEqual(self.client.delete(f"/api/lfg/{eid}").status_code, 200)
        self.assertNotIn(eid, app.hub.lfg)


class LFGAnnounceTests(unittest.TestCase):
    """Backlog #19 step 4 — opt-in 'announce to Discord' for a new LFG post: a channel
    broadcast (no @mentions), rate-limited per member, silent when the webhook is unset,
    and surfaced to the composer via `announce_available` on the snapshot."""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        cls._tmp.close()
        db.init(Path(cls._tmp.name))
        db.set_setting(notify._webhook_key("lfg"), _GOOD_WEBHOOK)
        cls._member = {"id": "1", "username": "tester", "is_admin": False}
        app.app.dependency_overrides[app.require_session] = lambda: cls._member
        cls._orig_token_user = app.token_user
        app.token_user = lambda request: cls._member
        cls.client = TestClient(app.app)
        cls._orig_send = notify.send

    @classmethod
    def tearDownClass(cls):
        app.app.dependency_overrides.clear()
        app.token_user = cls._orig_token_user
        notify.send = cls._orig_send
        Path(cls._tmp.name).unlink(missing_ok=True)

    def setUp(self):
        app.hub.lfg.clear()
        app.hub._lfg_seq = 0
        app._lfg_announce_at.clear()
        db.set_setting(notify._webhook_key("lfg"), _GOOD_WEBHOOK)   # restore after silent test
        self.sent = []

        async def _capture(category, text, *, mentions=None, dedup_key=None):
            self.sent.append({"category": category, "text": text,
                              "mentions": mentions, "dedup_key": dedup_key})
            return True
        notify.send = _capture

    _LFM = {"id": 3, "poster_id": "7", "poster": "Ace", "direction": "lfm",
            "tags": ["bunkers", "PvE"], "slots": 2, "filled": 1,
            "note": "need 2 for a bunker", "rally": "Daymar", "comms": True}
    _LFJ = {"id": 4, "poster_id": "8", "poster": "Nova", "direction": "lfj",
            "tags": ["mining"], "slots": None, "filled": 0,
            "note": "solo, down for anything", "rally": None, "comms": False}

    def test_announce_broadcasts_with_no_mentions(self):
        asyncio.run(app._notify_lfg_posted(self._LFM))
        self.assertEqual(len(self.sent), 1)
        msg = self.sent[0]
        self.assertEqual(msg["category"], "lfg")
        self.assertIsNone(msg["mentions"])          # an open call, not a directed ping
        self.assertIn("Looking for members", msg["text"])
        self.assertIn("Ace", msg["text"])
        self.assertIn("bunkers", msg["text"])
        self.assertIn("need 2 for a bunker", msg["text"])
        self.assertIn("Daymar", msg["text"])
        self.assertEqual(msg["dedup_key"], "lfg-posted:3")

    def test_lfj_announce_reads_as_looking_to_join(self):
        asyncio.run(app._notify_lfg_posted(self._LFJ))
        self.assertIn("Looking to join", self.sent[0]["text"])
        self.assertNotIn("Needs", self.sent[0]["text"])   # slots line is LFM-only

    def test_announce_silent_when_no_webhook(self):
        db.set_setting(notify._webhook_key("lfg"), "")
        asyncio.run(app._notify_lfg_posted(self._LFM))
        self.assertEqual(self.sent, [])

    def test_announce_is_rate_limited_per_member(self):
        self.assertTrue(app._lfg_announce_ok("7"))    # first arms the cooldown
        self.assertFalse(app._lfg_announce_ok("7"))   # second within cooldown is blocked
        self.assertTrue(app._lfg_announce_ok("8"))    # a different member is independent

    def test_snapshot_exposes_announce_available(self):
        self.assertTrue(self.client.get("/api/lfg").json()["announce_available"])
        db.set_setting(notify._webhook_key("lfg"), "")
        self.assertFalse(self.client.get("/api/lfg").json()["announce_available"])

    def test_api_announce_arms_the_rate_limit(self):
        r = self.client.post("/api/lfg", json={"direction": "lfj", "announce": True})
        self.assertEqual(r.status_code, 200)
        self.assertIn("1", app._lfg_announce_at)   # caller "1" armed via the route

    def test_api_no_announce_leaves_rate_limit_untouched(self):
        self.client.post("/api/lfg", json={"direction": "lfj", "announce": False})
        self.assertNotIn("1", app._lfg_announce_at)


class FleetTemplateTests(unittest.TestCase):
    """#20 v1.1: the ship seat-template feed + the saved-group-template lifecycle
    (snapshot an event's units → apply onto another event → delete)."""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        cls._tmp.close()
        db.init(Path(cls._tmp.name))
        cls._user = {"id": "1", "username": "organizer", "is_admin": False}
        app.app.dependency_overrides[app.require_session] = lambda: cls._user
        cls._orig_token_user = app.token_user
        app.token_user = lambda request: cls._user
        cls.client = TestClient(app.app)

    @classmethod
    def tearDownClass(cls):
        app.app.dependency_overrides.clear()
        app.token_user = cls._orig_token_user
        Path(cls._tmp.name).unlink(missing_ok=True)

    def _make_event(self, title):
        start = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
        r = self.client.post("/api/events",
                             json={"title": title, "start_at": start,
                                   "types": ["Raid"], "categories": ["PvE"]})
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()["id"]

    def test_fleet_ships_feed_carries_crew_and_seats(self):
        r = self.client.get("/api/fleet/ships")
        self.assertEqual(r.status_code, 200)
        ships = r.json()
        self.assertTrue(ships, "expected ships from the committed cache")
        s = ships[0]
        self.assertEqual(set(s), {"name", "crew", "seats"})
        self.assertEqual(len(s["seats"]), s["crew"])   # one label per seat
        self.assertEqual(s["seats"][0], "Pilot")

    def test_template_snapshot_apply_and_delete(self):
        src = self._make_event("Source Op")
        for name, kind, cap in [("Alpha", "squad", 4), ("Gold", "squadron", 3)]:
            r = self.client.post(f"/api/events/{src}/groups",
                                 json={"name": name, "kind": kind, "capacity": cap})
            self.assertEqual(r.status_code, 200, r.text)

        # Snapshot the two units into a named, reusable template.
        r = self.client.post("/api/group-templates",
                             json={"name": "Standard Wing", "event_id": src})
        self.assertEqual(r.status_code, 200, r.text)
        tpls = r.json()
        tpl = next(t for t in tpls if t["name"] == "Standard Wing")
        self.assertEqual(tpl["group_count"], 2)
        self.assertTrue(tpl["can_delete"])          # author may delete

        # Apply it onto a fresh event: two units appear, members not carried.
        dst = self._make_event("Target Op")
        r = self.client.post(f"/api/events/{dst}/groups/apply-template",
                             json={"template_id": tpl["id"]})
        self.assertEqual(r.status_code, 200, r.text)
        board = r.json()
        names = sorted(g["name"] for g in board["groups"])
        self.assertEqual(names, ["Alpha", "Gold"])
        caps = {g["name"]: g["capacity"] for g in board["groups"]}
        self.assertEqual(caps["Alpha"], 4)
        self.assertEqual(board["assigned_count"], 0)

        # Delete the template; it leaves the list.
        r = self.client.delete(f"/api/group-templates/{tpl['id']}")
        self.assertEqual(r.status_code, 200)
        self.assertNotIn(tpl["id"], [t["id"] for t in r.json()])

    def test_save_template_rejects_empty_plan(self):
        empty = self._make_event("No Units Op")
        r = self.client.post("/api/group-templates",
                             json={"name": "Nope", "event_id": empty})
        self.assertEqual(r.status_code, 400)


if __name__ == "__main__":
    unittest.main(verbosity=1)
