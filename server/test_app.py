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


class ProfileTagsTests(unittest.TestCase):
    """Backlog #30 — persistent member playstyle tags: PUT /api/me normalization
    (allowlist, dedup, cap, clear-to-empty), persistence through the member
    directory cache, and the two surfaces that carry them (the admin directory
    rows and the who's-online roster, live-updated on save)."""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        cls._tmp.close()
        db.init(Path(cls._tmp.name))
        cls._member = {"id": "1", "username": "tester", "is_admin": False}
        app.app.dependency_overrides[app.require_session] = lambda: cls._member
        app.app.dependency_overrides[app.require_admin] = lambda: cls._member
        cls._orig_token_user = app.token_user   # the auth middleware's bearer path
        app.token_user = lambda request: cls._member
        # The module-global directory cache was loaded from the real DB at import;
        # park it and run against the temp DB only, restoring on teardown.
        cls._orig_members = app.members_dir.by_id
        app.members_dir.by_id = {}
        cls.client = TestClient(app.app)

    @classmethod
    def tearDownClass(cls):
        app.app.dependency_overrides.clear()
        app.token_user = cls._orig_token_user
        app.members_dir.by_id = cls._orig_members
        Path(cls._tmp.name).unlink(missing_ok=True)

    def setUp(self):
        app.hub.online.clear()
        app.members_dir.by_id.clear()
        db.set_member_playstyles("1", [])

    def test_put_normalizes_allowlist_dedup(self):
        r = self.client.put("/api/me", json={
            "playstyle_tags": ["PvP", "PvP", "not-a-real-tag", "FPS", "mining"]})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["playstyle_tags"], ["PvP", "FPS", "mining"])
        # Persisted through the directory cache — the same read /api/me serves.
        self.assertEqual(app.member_playstyles(app.members_dir.get("1")),
                         ["PvP", "FPS", "mining"])

    def test_put_rejects_oversized_list(self):
        seven = ["hauling", "mining", "salvage", "trading", "bunkers", "bounty", "PvE"]
        self.assertEqual(
            self.client.put("/api/me", json={"playstyle_tags": seven}).status_code, 422)

    def test_put_clears_to_empty_and_absent_leaves_untouched(self):
        self.client.put("/api/me", json={"playstyle_tags": ["bounty"]})
        # Absent field = untouched (the presence-toggle-only call must not wipe tags).
        self.client.put("/api/me", json={"share_presence": True})
        self.assertEqual(app.member_playstyles(app.members_dir.get("1")), ["bounty"])
        r = self.client.put("/api/me", json={"playstyle_tags": []})
        self.assertEqual(r.json()["playstyle_tags"], [])
        self.assertEqual(app.member_playstyles(app.members_dir.get("1")), [])
        self.assertIsNone((db.get_member("1") or {}).get("playstyle_tags"))

    def test_directory_rows_carry_tags(self):
        app.members_dir.upsert({"id": "1", "username": "tester",
                                "display_name": "Tester", "guild_nick": None})
        self.client.put("/api/me", json={"playstyle_tags": ["PvE", "casual"]})
        rows = self.client.get("/api/intel/directory").json()["members"]
        self.assertEqual(rows[0]["playstyle_tags"], ["PvE", "casual"])

    def test_online_roster_seeds_and_live_updates_tags(self):
        db.set_member_playstyles("1", ["FPS", "bounty"])
        app.hub.mark_online(app.Session({"id": "1", "display_name": "Me", "is_admin": False}))
        rec = next(r for r in app.hub.online_roster() if r["discord_id"] == "1")
        self.assertEqual(rec["tags"], ["FPS", "bounty"])   # seeded from prefs on arrival
        # Saving new tags while online mirrors onto the live record — no reconnect.
        self.client.put("/api/me", json={"playstyle_tags": ["mining"]})
        rec = next(r for r in app.hub.online_roster() if r["discord_id"] == "1")
        self.assertEqual(rec["tags"], ["mining"])

    def test_stored_tags_refiltered_against_live_vocabulary(self):
        # A tag retired from PLAYSTYLE_TAGS must not resurface from an old row.
        db.set_member_playstyles("1", ["FPS"])
        row = db.get_member("1")
        row["playstyle_tags"] = '["FPS", "retired-tag"]'
        self.assertEqual(app.member_playstyles(row), ["FPS"])


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


class WarningBoardTests(unittest.TestCase):
    """Backlog #24 — the pirate danger board: post (point/lane), same-danger
    supersede, community confirm/refresh, close permissions, age-off prune,
    persistence, anchor/system resolution, board ordering, and the REST surface."""

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
        # A real POI id so anchor + system resolution is exercised against nav data.
        cls._poi_id = next(iter(app.nav.pois))
        cls._poi = app.nav.pois[cls._poi_id]

    @classmethod
    def tearDownClass(cls):
        app.app.dependency_overrides.clear()
        app.token_user = cls._orig_token_user
        Path(cls._tmp.name).unlink(missing_ok=True)

    def setUp(self):
        app.hub.warnings.clear()
        db.warning_delete([w["id"] for w in db.warnings_all()])
        app.hub._warning_seq = 0
        self._member["is_admin"] = False

    def test_post_resolves_system_from_anchor(self):
        w = app.hub.post_warning("7", "point", "pvp", "active",
                                 self._poi_id, None, "near it", "2 Cutlass")
        self.assertEqual(w["system"], self._poi.system)
        self.assertEqual(w["anchor_a_poi"], self._poi_id)

    def test_same_danger_supersedes_same_poster(self):
        first = app.hub.post_warning("7", "point", "pvp", "sighted",
                                     self._poi_id, None, "here", "")
        second = app.hub.post_warning("7", "point", "pvp", "deadly",
                                      self._poi_id, None, "here", "back again")
        mine = [w for w in app.hub.warnings.values() if w["poster"] == "7"]
        self.assertEqual(len(mine), 1)
        self.assertEqual(mine[0]["id"], second["id"])
        self.assertNotIn(first["id"], app.hub.warnings)

    def test_distinct_dangers_coexist(self):
        app.hub.post_warning("7", "point", "pvp", "active", self._poi_id, None, "a", "")
        app.hub.post_warning("7", "lane", "pve", "active", None, None, "Between X and Y", "")
        self.assertEqual(len(app.hub.warnings), 2)

    def test_confirm_records_confirmer_and_refreshes(self):
        w = app.hub.post_warning("7", "point", "pvp", "active", self._poi_id, None, "x", "")
        w["created"] = time.time() - 100
        again = app.hub.confirm_warning(w["id"], "8")
        self.assertIn("8", again["confirmations"])
        self.assertGreater(again["created"], time.time() - 5)   # clock reset

    def test_confirm_by_poster_refreshes_without_self_credit(self):
        w = app.hub.post_warning("7", "point", "pvp", "active", self._poi_id, None, "x", "")
        w["created"] = time.time() - 100
        again = app.hub.confirm_warning(w["id"], "7")
        self.assertEqual(again["confirmations"], [])            # poster isn't a confirmer
        self.assertGreater(again["created"], time.time() - 5)   # but it still refreshes

    def test_confirm_is_idempotent_per_member(self):
        w = app.hub.post_warning("7", "point", "pvp", "active", self._poi_id, None, "x", "")
        app.hub.confirm_warning(w["id"], "8")
        app.hub.confirm_warning(w["id"], "8")
        self.assertEqual(app.hub.warnings[w["id"]]["confirmations"], ["8"])

    def test_close_is_poster_or_admin_only(self):
        w = app.hub.post_warning("7", "point", "pvp", "active", self._poi_id, None, "x", "")
        self.assertFalse(app.hub.close_warning(w["id"], "8", False))   # stranger
        self.assertTrue(app.hub.close_warning(w["id"], "8", True))      # admin
        self.assertNotIn(w["id"], app.hub.warnings)
        w2 = app.hub.post_warning("7", "point", "pvp", "active", self._poi_id, None, "x", "")
        self.assertTrue(app.hub.close_warning(w2["id"], "7", False))    # poster

    def test_drop_warnings_for_clears_a_member(self):
        app.hub.post_warning("7", "point", "pvp", "active", self._poi_id, None, "a", "")
        app.hub.post_warning("7", "lane", "pve", "active", None, None, "b", "")
        app.hub.post_warning("8", "point", "pvp", "active", self._poi_id, None, "c", "")
        self.assertTrue(app.hub.drop_warnings_for("7"))
        self.assertEqual([w["poster"] for w in app.hub.warnings.values()], ["8"])
        self.assertFalse(app.hub.drop_warnings_for("7"))

    def test_prune_removes_only_aged_off(self):
        live = app.hub.post_warning("7", "point", "pvp", "active", self._poi_id, None, "a", "")
        aged = app.hub.post_warning("8", "point", "pvp", "active", self._poi_id, None, "b", "")
        aged["created"] = time.time() - (app.warning_ageoff_min() * 60 + 1)
        self.assertTrue(app.hub.prune_warnings(time.time()))
        self.assertEqual(list(app.hub.warnings), [live["id"]])
        self.assertFalse(app.hub.prune_warnings(time.time()))

    def test_board_persists_and_reloads(self):
        w = app.hub.post_warning("7", "lane", "pve", "deadly",
                                 self._poi_id, None, "Between A and B", "snare + gank")
        app.hub.confirm_warning(w["id"], "8")
        app.hub.warnings.clear()
        for row in db.warnings_all():
            app.hub.warnings[row["id"]] = row
        again = app.hub.warnings[w["id"]]
        self.assertEqual(again["kind"], "lane")
        self.assertEqual(again["threat"], "pve")
        self.assertEqual(again["severity"], "deadly")
        self.assertEqual(again["location"], "Between A and B")
        self.assertEqual(again["confirmations"], ["8"])   # confirm survived the reload

    def test_public_form_resolves_anchor_and_flags_stale(self):
        w = app.hub.post_warning("7", "point", "pvp", "active", self._poi_id, None, "x", "")
        pub = app.hub._public_warning(w)
        self.assertEqual(pub["anchor_a"]["name"], self._poi.name)
        self.assertEqual(pub["anchor_a"]["system"], self._poi.system)
        self.assertFalse(pub["stale"])
        w["created"] = time.time() - (app.warning_stale_min() * 60 + 1)
        pub = app.hub._public_warning(w)
        self.assertTrue(pub["stale"])
        self.assertGreater(pub["expires_s"], 0)

    def test_public_form_unknown_anchor_is_unlabeled(self):
        w = app.hub.post_warning("7", "point", "pvp", "active", None, None, "somewhere", "")
        # Simulate an anchor id the current nav dataset doesn't know.
        w["anchor_a_poi"] = 999999999
        pub = app.hub._public_warning(w)
        self.assertIsNone(pub["anchor_a"]["name"])
        self.assertEqual(pub["anchor_a"]["id"], 999999999)

    def test_board_orders_deadliest_then_freshest(self):
        sighted = app.hub.post_warning("7", "point", "pvp", "sighted", self._poi_id, None, "a", "")
        deadly = app.hub.post_warning("8", "point", "pvp", "deadly", self._poi_id, None, "b", "")
        order = [w["id"] for w in app.hub.warnings_board()]
        self.assertEqual(order[0], deadly["id"])     # deadly outranks sighted
        self.assertEqual(order[1], sighted["id"])

    # --- REST surface -------------------------------------------------------
    def test_api_post_point_with_anchor(self):
        r = self.client.post("/api/warnings", json={
            "kind": "point", "threat": "pvp", "severity": "deadly",
            "anchor_a": self._poi_id, "note": "camped"})
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["anchor_a"]["id"], self._poi_id)
        self.assertEqual(body["severity"], "deadly")

    def test_api_post_requires_a_location(self):
        r = self.client.post("/api/warnings", json={"kind": "point", "threat": "pvp"})
        self.assertEqual(r.status_code, 400)

    def test_api_free_text_only_is_valid(self):
        r = self.client.post("/api/warnings", json={
            "kind": "lane", "location": "Between Baijini and Orison"})
        self.assertEqual(r.status_code, 200)
        self.assertIsNone(r.json()["anchor_a"])

    def test_api_bad_enums_fall_back(self):
        r = self.client.post("/api/warnings", json={
            "kind": "nonsense", "threat": "aliens", "severity": "spicy",
            "location": "somewhere"})
        body = r.json()
        self.assertEqual(body["kind"], "point")
        self.assertEqual(body["threat"], "pvp")
        self.assertEqual(body["severity"], "active")

    def test_api_snapshot_lists_warnings(self):
        app.hub.post_warning("7", "point", "pvp", "active", self._poi_id, None, "a", "")
        app.hub.post_warning("8", "lane", "pve", "active", None, None, "b", "")
        body = self.client.get("/api/warnings").json()
        self.assertEqual(body["count"], 2)

    def test_api_confirm_and_delete(self):
        w = app.hub.post_warning("7", "point", "pvp", "active", self._poi_id, None, "a", "")
        r = self.client.post(f"/api/warnings/{w['id']}/confirm")   # caller "1" != poster
        self.assertEqual(r.json()["confirm_count"], 1)
        self.assertEqual(self.client.delete(f"/api/warnings/{w['id']}").status_code, 404)  # not owner
        self._member["is_admin"] = True
        self.assertEqual(self.client.delete(f"/api/warnings/{w['id']}").status_code, 200)

    def test_api_confirm_missing_404s(self):
        self.assertEqual(self.client.post("/api/warnings/999/confirm").status_code, 404)


class WarningAnnounceTests(unittest.TestCase):
    """Backlog #24 — opt-in 'announce to Discord' for a danger warning: a channel
    broadcast (no @mentions), rate-limited per member, silent when the pirates webhook
    is unset, and surfaced to the composer via `announce_available`."""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        cls._tmp.close()
        db.init(Path(cls._tmp.name))
        db.set_setting(notify._webhook_key("pirates"), _GOOD_WEBHOOK)
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
        app.hub.warnings.clear()
        app.hub._warning_seq = 0
        app._warning_announce_at.clear()
        db.set_setting(notify._webhook_key("pirates"), _GOOD_WEBHOOK)
        self.sent = []

        async def _capture(category, text, *, mentions=None, dedup_key=None):
            self.sent.append({"category": category, "text": text,
                              "mentions": mentions, "dedup_key": dedup_key})
            return True
        notify.send = _capture

    _LANE = {"id": 3, "poster_id": "7", "poster": "Ace", "kind": "lane",
             "threat": "pvp", "severity": "deadly",
             "anchor_a": {"id": 1, "name": "Baijini Point", "system": "Stanton"},
             "anchor_b": {"id": 2, "name": "Orison", "system": "Stanton"},
             "location": "", "note": "2 Cutlass + a snare"}
    _POINT = {"id": 4, "poster_id": "8", "poster": "Nova", "kind": "point",
              "threat": "pve", "severity": "active",
              "anchor_a": {"id": 5, "name": "CRU-L1", "system": "Stanton"},
              "anchor_b": None, "location": "", "note": ""}

    def test_lane_announce_names_both_endpoints(self):
        asyncio.run(app._notify_warning_posted(self._LANE))
        self.assertEqual(len(self.sent), 1)
        msg = self.sent[0]
        self.assertEqual(msg["category"], "pirates")
        self.assertIsNone(msg["mentions"])
        self.assertIn("Baijini Point", msg["text"])
        self.assertIn("Orison", msg["text"])
        self.assertIn("DEADLY", msg["text"])
        self.assertIn("players (PvP)", msg["text"])
        self.assertEqual(msg["dedup_key"], "warning-posted:3")

    def test_point_announce_reads_as_danger_near(self):
        asyncio.run(app._notify_warning_posted(self._POINT))
        self.assertIn("Danger near CRU-L1", self.sent[0]["text"])
        self.assertIn("NPC pirates (PvE)", self.sent[0]["text"])

    def test_announce_silent_when_no_webhook(self):
        db.set_setting(notify._webhook_key("pirates"), "")
        asyncio.run(app._notify_warning_posted(self._LANE))
        self.assertEqual(self.sent, [])

    def test_announce_is_rate_limited_per_member(self):
        self.assertTrue(app._warning_announce_ok("7"))
        self.assertFalse(app._warning_announce_ok("7"))
        self.assertTrue(app._warning_announce_ok("8"))

    def test_snapshot_exposes_announce_available(self):
        self.assertTrue(self.client.get("/api/warnings").json()["announce_available"])
        db.set_setting(notify._webhook_key("pirates"), "")
        self.assertFalse(self.client.get("/api/warnings").json()["announce_available"])

    def test_api_announce_arms_the_rate_limit(self):
        r = self.client.post("/api/warnings", json={
            "kind": "lane", "location": "Between A and B", "announce": True})
        self.assertEqual(r.status_code, 200)
        self.assertIn("1", app._warning_announce_at)


class TradeDangerWiringTests(unittest.TestCase):
    """#24 trade-planner glue in app.py: avoid_mode normalization, the leg-warning
    serializer, and leg annotation. The nav_core avoid/annotate logic itself is
    covered in test_nav_core.TradeDangerAvoidTests."""

    @classmethod
    def setUpClass(cls):
        cls._poi_id = next(iter(app.nav.pois))
        cls._poi = app.nav.pois[cls._poi_id]

    def test_norm_avoid_mode(self):
        self.assertEqual(app._norm_avoid_mode("avoid"), "avoid")
        self.assertEqual(app._norm_avoid_mode("warn"), "warn")
        self.assertEqual(app._norm_avoid_mode("bogus"), "ignore")
        self.assertEqual(app._norm_avoid_mode(None), "ignore")

    def test_leg_warning_view_resolves_poi_name(self):
        w = {"id": 5, "kind": "point", "anchor_a_poi": self._poi_id, "anchor_b_poi": None,
             "threat": "pvp", "severity": "deadly", "location": ""}
        v = app._leg_warning_view(w)
        self.assertEqual(v["where"], self._poi.name)
        self.assertEqual((v["id"], v["severity"], v["threat"]), (5, "deadly", "pvp"))

    def test_leg_warning_view_falls_back_to_location(self):
        w = {"id": 6, "kind": "lane", "anchor_a_poi": None, "anchor_b_poi": None,
             "threat": "pve", "severity": "active", "location": "Between A and B"}
        self.assertEqual(app._leg_warning_view(w)["where"], "Between A and B")

    def test_annotate_ignore_is_noop(self):
        plan = {"legs": [{"buy_poi_id": self._poi_id, "sell_poi_id": None}]}
        w = {"id": 1, "kind": "point", "anchor_a_poi": self._poi_id, "severity": "deadly"}
        out = app._annotate_trade_legs(plan, [w], "ignore")
        self.assertNotIn("warnings", out["legs"][0])

    def test_annotate_warn_tags_touched_leg(self):
        plan = {"legs": [{"buy_poi_id": self._poi_id, "sell_poi_id": None}]}
        w = {"id": 1, "kind": "point", "anchor_a_poi": self._poi_id, "anchor_b_poi": None,
             "threat": "pvp", "severity": "deadly", "location": ""}
        out = app._annotate_trade_legs(plan, [w], "warn")
        self.assertEqual([x["id"] for x in out["legs"][0]["warnings"]], [1])

    def test_active_trade_warnings_snapshot(self):
        app.hub.warnings.clear()
        app.hub.warnings[1] = {"id": 1, "kind": "point", "anchor_a_poi": self._poi_id,
                               "anchor_b_poi": None, "severity": "active", "threat": "pvp",
                               "location": "", "note": "", "confirmations": [], "created": 0.0}
        try:
            snap = app.hub.active_trade_warnings()
            self.assertEqual([w["id"] for w in snap], [1])
        finally:
            app.hub.warnings.clear()


class SnareDetourWiringTests(unittest.TestCase):
    """#24 v2 snare-detour glue in app.py: default flip to avoid, avoid_poi_ids
    model caps, hazard-volume build, and the detour/fly-past annotate passes. The
    geometry + solver behavior itself lives in test_nav_core."""

    @classmethod
    def setUpClass(cls):
        cls._poi_id = next(iter(app.nav.pois))
        cls._poi = app.nav.pois[cls._poi_id]

    # --- model defaults + caps ---------------------------------------------
    def test_avoid_mode_defaults_to_avoid(self):
        self.assertEqual(app.TradePlanIn(usable_scu=100).avoid_mode, "avoid")
        self.assertEqual(app.TradePlanIn(usable_scu=100).avoid_poi_ids, [])
        self.assertEqual(app.RoutePlanIn(packages=[], usable_scu=100).avoid_mode, "avoid")

    def test_avoid_poi_ids_cap(self):
        import pydantic
        app.TradePlanIn(usable_scu=100, avoid_poi_ids=list(range(50)))   # 50 ok
        with self.assertRaises(pydantic.ValidationError):
            app.TradePlanIn(usable_scu=100, avoid_poi_ids=list(range(51)))

    def test_norm_avoid_mode_default_override(self):
        self.assertEqual(app._norm_avoid_mode(None, default="avoid"), "avoid")
        self.assertEqual(app._norm_avoid_mode("ignore", default="avoid"), "ignore")

    # --- hazard-volume build ------------------------------------------------
    def test_build_volumes_none_when_nothing(self):
        self.assertIsNone(app._build_hazard_volumes([], [], None))

    def test_build_volumes_from_warning(self):
        w = {"id": 1, "kind": "point", "severity": "active",
             "anchor_a_poi": self._poi_id, "anchor_b_poi": None}
        vols = app._build_hazard_volumes([w], [], None)
        self.assertEqual(len(vols), 1)
        self.assertEqual(vols[0]["kind"], "sphere")
        self.assertEqual(vols[0]["warning_id"], 1)

    def test_build_volumes_unknown_blacklist_id_is_safe(self):
        # A blacklist id that isn't a known POI contributes nothing (no crash).
        self.assertIsNone(app._build_hazard_volumes([], [999_999_999], None))

    # --- annotate: detour outcomes -> named views --------------------------
    def test_annotate_avoid_resolves_dodged_and_blocked(self):
        warnings = [
            {"id": 42, "kind": "lane", "anchor_a_poi": self._poi_id, "anchor_b_poi": None,
             "threat": "pvp", "severity": "deadly", "location": "the lane"},
            {"id": 5, "kind": "point", "anchor_a_poi": self._poi_id, "anchor_b_poi": None,
             "threat": "pve", "severity": "active", "location": ""},
        ]
        plan = {"legs": [{"buy_poi_id": self._poi_id, "sell_poi_id": None,
                          "haul": {"dodged": [42], "blocked": [5]}}]}
        out = app._annotate_trade_legs(plan, warnings, "avoid")
        lg = out["legs"][0]
        self.assertEqual([v["id"] for v in lg["dodged"]], [42])
        self.assertEqual([v["id"] for v in lg["blocked"]], [5])

    def test_annotate_warn_adds_flypast_via_volumes(self):
        # Endpoint match finds nothing (the warned POI isn't this leg's buy/sell),
        # but a hazard volume the leg crosses is surfaced via leg_hazards.
        spc = [p for p in app.nav.pois.values() if p.system == "Stanton" and p.global_m][:3]
        a, mid, b = spc
        w = {"id": 7, "kind": "point", "anchor_a_poi": mid.id, "anchor_b_poi": None,
             "threat": "pvp", "severity": "deadly", "location": ""}
        vols = app.nav_core.hazard_volumes(app.nav, [w], None, radius_m=5e14)  # huge -> guaranteed cross
        plan = {"legs": [{"buy_poi_id": a.id, "sell_poi_id": b.id}]}
        out = app._annotate_trade_legs(plan, [w], "warn", vols, None)
        self.assertEqual([v["id"] for v in out["legs"][0]["warnings"]], [7])

    def test_annotate_cargo_warn_badges_camped_stop(self):
        warnings = [{"id": 3, "kind": "point", "anchor_a_poi": self._poi_id,
                     "anchor_b_poi": None, "threat": "pvp", "severity": "deadly",
                     "location": ""}]
        plan = {"stops": [{"stop_id": self._poi_id, "leg": {}}]}
        out = app._annotate_cargo_stops(plan, warnings, "warn")
        self.assertEqual([v["id"] for v in out["stops"][0]["warnings"]], [3])

    def test_annotate_cargo_avoid_resolves_leg_outcomes(self):
        warnings = [{"id": 9, "kind": "lane", "anchor_a_poi": self._poi_id,
                     "anchor_b_poi": None, "threat": "pvp", "severity": "active",
                     "location": "x"}]
        plan = {"stops": [{"stop_id": self._poi_id, "leg": {"dodged": [9]}}]}
        out = app._annotate_cargo_stops(plan, warnings, "avoid")
        self.assertEqual([v["id"] for v in out["stops"][0]["dodged"]], [9])


class HazardRadiusSettingTests(unittest.TestCase):
    """#24 v2: the admin-editable hazard_radius_km org setting round-trips through
    /api/settings and defaults sanely."""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        cls._tmp.close()
        db.init(Path(cls._tmp.name))
        cls._admin = {"id": "1", "username": "tester", "is_admin": True}
        app.app.dependency_overrides[app.require_session] = lambda: cls._admin
        cls._orig_token_user = app.token_user
        app.token_user = lambda request: cls._admin
        cls.client = TestClient(app.app)

    @classmethod
    def tearDownClass(cls):
        app.app.dependency_overrides.clear()
        app.token_user = cls._orig_token_user
        Path(cls._tmp.name).unlink(missing_ok=True)

    def test_default_is_5000(self):
        db.set_setting("hazard_radius_km", "")           # unset -> default
        self.assertEqual(app.hazard_radius_km(), 5000)
        self.assertEqual(self.client.get("/api/settings").json()["hazard_radius_km"], 5000)

    def test_round_trip(self):
        r = self.client.post("/api/settings", json={"hazard_radius_km": 8000})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(app.hazard_radius_km(), 8000)
        self.assertEqual(self.client.get("/api/settings").json()["hazard_radius_km"], 8000)

    def test_out_of_range_rejected(self):
        self.assertEqual(
            self.client.post("/api/settings", json={"hazard_radius_km": 50}).status_code, 422)
        self.assertEqual(
            self.client.post("/api/settings", json={"hazard_radius_km": 999_999}).status_code, 422)


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


class TradeRunStateTests(unittest.TestCase):
    """The trade-run leg/phase state machine (#21 step 5): guidance points at the
    active leg's buy terminal until the buy is confirmed, then its sell terminal;
    the cursor advances leg-by-leg and the run finishes on the last sell."""

    def _run(self):
        legs = [
            {"commodity": "Gold", "buy_poi_id": 11, "sell_poi_id": 12,
             "buy_terminal": "A", "sell_terminal": "B", "buy_price": 100,
             "sell_price": 300, "scu": 40, "profit": 8000, "held": False},
            {"commodity": "Iron", "buy_poi_id": 12, "sell_poi_id": 13,
             "buy_terminal": "B", "sell_terminal": "C", "buy_price": 50,
             "sell_price": 200, "scu": 40, "profit": 6000, "held": False},
        ]
        return {"id": 1, "ship": "C2", "usable_scu": 64, "legs": legs,
                "leg_states": app._initial_trade_states(legs), "active": 0,
                "summary": {"total_profit": 14000}}

    def _sess(self):
        s = app.Session({"id": "1", "display_name": "Trader"})
        s.trade_run = self._run()
        return s

    def test_guidance_points_at_buy_then_sell(self):
        s = self._sess()
        app._point_at_active_trade_leg(s)
        self.assertEqual(s.destination_id, 11)          # active leg's buy terminal
        s.trade_run["leg_states"][0] = "bought"
        app._point_at_active_trade_leg(s)
        self.assertEqual(s.destination_id, 12)          # flips to the sell terminal

    def test_view_reports_phase_and_onboard(self):
        s = self._sess()
        v = s.trade_run_view()
        self.assertEqual(v["phase"], "buy")
        self.assertEqual(v["onboard_scu"], 0)
        s.trade_run["leg_states"][0] = "bought"
        v = s.trade_run_view()
        self.assertEqual(v["phase"], "sell")
        self.assertEqual(v["onboard_scu"], 40)          # holding the bought leg

    def test_advance_skips_sold_legs_and_completes(self):
        s = self._sess()
        s.trade_run["leg_states"][0] = "sold"
        self.assertFalse(app._advance_trade_run(s))
        self.assertEqual(s.trade_run["active"], 1)
        self.assertEqual(s.destination_id, 12)          # leg 1's buy terminal
        s.trade_run["leg_states"][1] = "sold"
        self.assertTrue(app._advance_trade_run(s))
        self.assertIsNone(s.destination_id)

    def test_realized_profit_sums_sold_legs(self):
        s = self._sess()
        s.trade_run["leg_states"][0] = "sold"
        self.assertEqual(s.trade_run_view()["realized_profit"], 8000)

    def test_held_leg_starts_in_sell_phase(self):
        s = self._sess()
        s.trade_run["legs"][0]["held"] = True
        s.trade_run["leg_states"] = app._initial_trade_states(s.trade_run["legs"])
        self.assertEqual(s.trade_run["leg_states"][0], "bought")
        self.assertEqual(s.trade_run_view()["phase"], "sell")

    def test_actuals_drive_realized_profit(self):
        # Enter real figures on leg 0: bought 40 @120, sold 40 @350.
        s = self._sess()
        lg = s.trade_run["legs"][0]
        lg["actual_buy_price"] = 120; lg["actual_buy_scu"] = 40
        lg["actual_sell_price"] = 350; lg["actual_sell_scu"] = 40
        s.trade_run["leg_states"][0] = "sold"
        v = s.trade_run_view()
        self.assertEqual(v["realized_profit"], (350 - 120) * 40)     # not the 8000 plan
        self.assertEqual(v["legs"][0]["realized"], (350 - 120) * 40)

    def test_onboard_uses_actual_bought_scu(self):
        s = self._sess()
        lg = s.trade_run["legs"][0]
        lg["actual_buy_scu"] = 25                                    # short fill
        s.trade_run["leg_states"][0] = "bought"
        self.assertEqual(s.trade_run_view()["onboard_scu"], 25)

    def test_realized_falls_back_to_plan_without_actuals(self):
        s = self._sess()
        s.trade_run["leg_states"][0] = "sold"
        self.assertEqual(s.trade_run_view()["realized_profit"], 8000)   # planned profit


class TradeRunSummaryTests(unittest.TestCase):
    """app._trade_run_summary — the compact completed-run record the history list
    and 'run again' shortcut consume (#21 step 6)."""

    def _run(self):
        legs = [
            {"commodity": "Gold", "buy_terminal_id": 1, "buy_terminal": "A",
             "buy_poi_id": 11, "buy_system": "Stanton",
             "sell_terminal_id": 2, "sell_terminal": "B", "sell_poi_id": 12,
             "sell_system": "Stanton", "buy_price": 100, "sell_price": 300,
             "scu": 40, "profit": 8000},
            {"commodity": "Iron", "buy_terminal_id": 2, "buy_terminal": "B",
             "buy_poi_id": 12, "sell_terminal_id": 3, "sell_terminal": "C",
             "sell_poi_id": 13, "buy_price": 50, "sell_price": 200,
             "scu": 40, "profit": 6000},
        ]
        return {"id": 7, "ship": "C2", "usable_scu": 64, "legs": legs,
                "leg_states": ["sold", "sold"], "completed_at": "2026-07-04T00:00:00+00:00",
                "summary": {"total_distance_m": 5000.0, "total_time_s": 3600.0}}

    def test_summary_headline_and_legs(self):
        s = app._trade_run_summary(self._run())
        self.assertEqual(s["id"], 7)
        self.assertEqual(s["ship"], "C2")
        self.assertEqual(s["num_legs"], 2)
        self.assertEqual(s["total_scu"], 80.0)
        self.assertEqual(s["profit"], 14000)               # 8000 + 6000
        self.assertEqual(s["auec_per_hour"], 14000.0)      # over 1h
        self.assertEqual([l["commodity"] for l in s["legs"]], ["Gold", "Iron"])
        self.assertEqual(s["legs"][0]["realized"], 8000)

    def test_summary_only_lists_sold_legs(self):
        run = self._run()
        run["leg_states"] = ["sold", "pending"]
        s = app._trade_run_summary(run)
        self.assertEqual(s["num_legs"], 1)
        self.assertEqual(s["profit"], 8000)


class TradeStockReportTests(unittest.TestCase):
    """Stock reports (#21): the run-mode no-stock skip files a shared 'out'
    report, a well-short buy files a 'low' one, the board endpoint serves fresh
    reports with age-off pruning, and skipped legs stay out of realized stats."""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        cls._tmp.close()
        db.init(Path(cls._tmp.name))
        cls._member = {"id": "9", "display_name": "Trader", "is_admin": False}
        app.app.dependency_overrides[app.require_session] = lambda: cls._member
        cls._orig_token_user = app.token_user
        app.token_user = lambda request: cls._member
        cls.client = TestClient(app.app)

    @classmethod
    def tearDownClass(cls):
        app.app.dependency_overrides.clear()
        app.token_user = cls._orig_token_user
        app.hub.sessions.pop("9", None)
        Path(cls._tmp.name).unlink(missing_ok=True)

    def setUp(self):
        db.stock_reports_clear()
        legs = [
            {"commodity": "Gold", "buy_poi_id": 11, "sell_poi_id": 12,
             "buy_terminal": "A", "sell_terminal": "B", "buy_price": 100,
             "sell_price": 300, "scu": 40, "profit": 8000, "held": False},
            {"commodity": "Iron", "buy_poi_id": 12, "sell_poi_id": 13,
             "buy_terminal": "B", "sell_terminal": "C", "buy_price": 50,
             "sell_price": 200, "scu": 40, "profit": 6000, "held": False},
        ]
        sess = app.hub.get(self._member)
        sess.trade_run = {"id": 1, "ship": "C2", "usable_scu": 64, "legs": legs,
                          "leg_states": app._initial_trade_states(legs), "active": 0,
                          "summary": {"total_profit": 14000}}

    # --- the stockout action ------------------------------------------------
    def test_stockout_skips_leg_and_files_out_report(self):
        r = self.client.patch("/api/trade/run", json={"action": "stockout", "leg": 0})
        self.assertEqual(r.status_code, 200)
        v = r.json()["trade_run"]
        self.assertEqual(v["active"], 1)                    # cursor moved on
        self.assertTrue(v["legs"][0]["skipped"])
        self.assertTrue(v["legs"][0]["stockout"])
        self.assertEqual(v["realized_profit"], 0)           # no phantom 8000
        self.assertIsNone(v["legs"][0]["realized"])
        board = self.client.get("/api/trade/stock").json()
        self.assertEqual(len(board["reports"]), 1)
        rep = board["reports"][0]
        self.assertEqual((rep["kind"], rep["poi_id"], rep["commodity"]),
                         ("out", 11, "Gold"))
        self.assertEqual(rep["poster_name"], "Trader")
        self.assertIn("age_s", rep)

    def test_stockout_only_valid_before_the_buy(self):
        self.client.patch("/api/trade/run", json={"action": "buy", "leg": 0})
        r = self.client.patch("/api/trade/run", json={"action": "stockout", "leg": 0})
        self.assertEqual(r.status_code, 409)

    def test_plain_skip_files_nothing_but_stays_unrealized(self):
        r = self.client.patch("/api/trade/run", json={"action": "advance", "leg": 0})
        v = r.json()["trade_run"]
        self.assertTrue(v["legs"][0]["skipped"])
        self.assertNotIn("stockout", v["legs"][0])
        self.assertEqual(v["realized_profit"], 0)           # the regression fix
        self.assertEqual(self.client.get("/api/trade/stock").json()["reports"], [])

    # --- the low-stock auto-report ------------------------------------------
    def test_short_buy_files_low_report(self):
        r = self.client.patch("/api/trade/run",
                              json={"action": "buy", "leg": 0, "scu": 10})
        self.assertEqual(r.status_code, 200)                # 10 < 40 * 0.5
        rep = self.client.get("/api/trade/stock").json()["reports"][0]
        self.assertEqual((rep["kind"], rep["scu"]), ("low", 10))

    def test_modest_short_buy_files_nothing(self):
        self.client.patch("/api/trade/run", json={"action": "buy", "leg": 0, "scu": 30})
        self.assertEqual(self.client.get("/api/trade/stock").json()["reports"], [])

    # --- board lifecycle ----------------------------------------------------
    def test_new_report_replaces_same_terminal_and_commodity(self):
        now = time.time()
        db.stock_report_save({"poi_id": 11, "terminal": "A", "commodity": "Gold",
                              "kind": "low", "scu": 5, "poster": "9",
                              "poster_name": "Trader", "created": now - 60})
        db.stock_report_save({"poi_id": 11, "terminal": "A", "commodity": "gold",
                              "kind": "out", "scu": None, "poster": "8",
                              "poster_name": "Other", "created": now})
        reports = app.active_stock_reports()
        self.assertEqual(len(reports), 1)
        self.assertEqual(reports[0]["kind"], "out")         # newest observation wins

    def test_ageoff_prunes_expired_reports(self):
        now = time.time()
        db.stock_report_save({"poi_id": 11, "terminal": "A", "commodity": "Gold",
                              "kind": "out", "scu": None, "poster": "9",
                              "poster_name": "T", "created": now})
        db.stock_report_save({"poi_id": 12, "terminal": "B", "commodity": "Iron",
                              "kind": "out", "scu": None, "poster": "9",
                              "poster_name": "T",
                              "created": now - (app.stock_ageoff_min() * 60 + 5)})
        reports = app.active_stock_reports()
        self.assertEqual([r["commodity"] for r in reports], ["Gold"])
        # the expired row was pruned, not just filtered
        self.assertEqual(len(db.stock_reports_since(0)), 1)

    def test_ageoff_window_is_the_org_setting(self):
        db.set_setting("stock_ageoff_min", "1")
        try:
            self.assertEqual(app.stock_ageoff_min(), 1)
            now = time.time()
            db.stock_report_save({"poi_id": 11, "terminal": "A", "commodity": "Gold",
                                  "kind": "out", "scu": None, "poster": "9",
                                  "poster_name": "T", "created": now - 90})
            self.assertEqual(app.active_stock_reports(), [])   # 90s > 1min window
        finally:
            db.set_setting("stock_ageoff_min", "")
        self.assertEqual(app.stock_ageoff_min(), 180)          # default restored

    def test_settings_surface_carries_the_knob(self):
        s = self.client.get("/api/settings").json()
        self.assertEqual(s["stock_ageoff_min"], app.stock_ageoff_min())

    # --- end-to-end: the plan endpoint honors + badges reports ---------------
    def test_plan_respects_out_reports_and_badges_low(self):
        pois = [p for p in app.nav.pois.values()
                if p.system == "Stanton" and p.global_m][:3]
        A, B, C = (p.id for p in pois)

        def pt(commodity, tid, poi, buy=None, sell=None):
            return {"commodity": commodity, "terminal_id": tid, "terminal": f"T{tid}",
                    "system": "Stanton", "poi_id": poi, "buy": buy, "sell": sell,
                    "scu_buy": 500 if buy else 0,
                    "scu_sell_stock": 500 if sell else 0, "updated_at": None}

        orig = app.trade_price_points
        app.trade_price_points = [pt("Gold", 1, A, buy=100), pt("Gold", 2, B, sell=300),
                                  pt("Iron", 3, B, buy=50), pt("Iron", 4, C, sell=200)]
        try:
            now = time.time()
            db.stock_report_save({"poi_id": A, "terminal": "T1", "commodity": "Gold",
                                  "kind": "out", "scu": None, "poster": "9",
                                  "poster_name": "T", "created": now})
            db.stock_report_save({"poi_id": B, "terminal": "T3", "commodity": "Iron",
                                  "kind": "low", "scu": 12, "poster": "9",
                                  "poster_name": "T", "created": now})
            r = self.client.post("/api/trade/plan", json={
                "usable_scu": 100, "start_id": A, "sort": "profit",
                "system": "Stanton"})
            self.assertEqual(r.status_code, 200)
            legs = r.json()["legs"]
            # Gold's buy is reported out -> excluded; Iron survives with its
            # low-stock badge attached to the buy side.
            self.assertEqual({l["commodity"] for l in legs}, {"Iron"})
            self.assertEqual(legs[0]["stock"][0]["kind"], "low")
            self.assertEqual(legs[0]["stock"][0]["scu"], 12)
        finally:
            app.trade_price_points = orig

    # --- the demand side (sell end) -------------------------------------------
    def test_demandout_reports_without_moving_the_cursor(self):
        self.client.patch("/api/trade/run", json={"action": "buy", "leg": 0})
        r = self.client.patch("/api/trade/run", json={"action": "demandout", "leg": 0})
        self.assertEqual(r.status_code, 200)
        v = r.json()["trade_run"]
        self.assertEqual(v["active"], 0)                    # cargo still aboard —
        self.assertEqual(v["legs"][0]["state"], "bought")   # nothing advanced
        self.assertTrue(v["legs"][0]["demand_reported"])
        rep = self.client.get("/api/trade/stock").json()["reports"][0]
        self.assertEqual((rep["side"], rep["kind"], rep["poi_id"], rep["commodity"]),
                         ("demand", "out", 12, "Gold"))     # anchored to the SELL end
        self.assertEqual(rep["terminal"], "B")

    def test_demandout_only_valid_at_the_sell(self):
        r = self.client.patch("/api/trade/run", json={"action": "demandout", "leg": 0})
        self.assertEqual(r.status_code, 409)                # still awaiting the buy

    def test_short_sell_files_demand_low(self):
        self.client.patch("/api/trade/run", json={"action": "buy", "leg": 0})
        self.client.patch("/api/trade/run", json={"action": "sell", "leg": 0, "scu": 10})
        rep = self.client.get("/api/trade/stock").json()["reports"][0]
        self.assertEqual((rep["side"], rep["kind"], rep["scu"], rep["poi_id"]),
                         ("demand", "low", 10, 12))

    def test_modest_short_sell_files_nothing(self):
        self.client.patch("/api/trade/run", json={"action": "buy", "leg": 0})
        self.client.patch("/api/trade/run", json={"action": "sell", "leg": 0, "scu": 30})
        self.assertEqual(self.client.get("/api/trade/stock").json()["reports"], [])

    def test_supply_and_demand_reports_coexist_per_terminal(self):
        now = time.time()
        base = {"poi_id": 11, "terminal": "A", "commodity": "Gold", "kind": "out",
                "scu": None, "poster": "9", "poster_name": "T", "created": now}
        db.stock_report_save(base)
        db.stock_report_save({**base, "side": "demand"})
        reports = app.active_stock_reports()
        self.assertEqual({r["side"] for r in reports}, {"supply", "demand"})
        # ...but a newer report on the SAME side still replaces its predecessor.
        db.stock_report_save({**base, "side": "demand", "kind": "low", "scu": 5})
        reports = app.active_stock_reports()
        self.assertEqual(len(reports), 2)
        demand = next(r for r in reports if r["side"] == "demand")
        self.assertEqual(demand["kind"], "low")

    def test_plan_respects_demand_out_and_badges_low_demand(self):
        pois = [p for p in app.nav.pois.values()
                if p.system == "Stanton" and p.global_m][:3]
        A, B, C = (p.id for p in pois)

        def pt(commodity, tid, poi, buy=None, sell=None):
            return {"commodity": commodity, "terminal_id": tid, "terminal": f"T{tid}",
                    "system": "Stanton", "poi_id": poi, "buy": buy, "sell": sell,
                    "scu_buy": 500 if buy else 0,
                    "scu_sell_stock": 500 if sell else 0, "updated_at": None}

        orig = app.trade_price_points
        app.trade_price_points = [pt("Gold", 1, A, buy=100), pt("Gold", 2, B, sell=300),
                                  pt("Iron", 3, B, buy=50), pt("Iron", 4, C, sell=200)]
        try:
            now = time.time()
            db.stock_report_save({"poi_id": B, "terminal": "T2", "commodity": "Gold",
                                  "side": "demand", "kind": "out", "scu": None,
                                  "poster": "9", "poster_name": "T", "created": now})
            db.stock_report_save({"poi_id": C, "terminal": "T4", "commodity": "Iron",
                                  "side": "demand", "kind": "low", "scu": 20,
                                  "poster": "9", "poster_name": "T", "created": now})
            r = self.client.post("/api/trade/plan", json={
                "usable_scu": 100, "start_id": A, "sort": "profit",
                "system": "Stanton"})
            self.assertEqual(r.status_code, 200)
            legs = r.json()["legs"]
            # Gold's sell is reported not buying -> excluded; Iron survives with
            # its low-demand badge attached to the sell side.
            self.assertEqual({l["commodity"] for l in legs}, {"Iron"})
            self.assertEqual((legs[0]["stock"][0]["side"], legs[0]["stock"][0]["kind"]),
                             ("demand", "low"))
        finally:
            app.trade_price_points = orig


class TradeFavoritesTests(unittest.TestCase):
    """Saved trade-route favorites (#21): persist a plan *config* (not resolved
    legs), list freshest-first, overwrite by name, and delete — scoped per member."""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        cls._tmp.close()
        db.init(Path(cls._tmp.name))
        cls._user = {"id": "42", "username": "trader", "is_admin": False}
        app.app.dependency_overrides[app.require_session] = lambda: cls._user
        cls._orig_token_user = app.token_user
        app.token_user = lambda request: cls._user
        cls.client = TestClient(app.app)

    @classmethod
    def tearDownClass(cls):
        app.app.dependency_overrides.clear()
        app.token_user = cls._orig_token_user
        Path(cls._tmp.name).unlink(missing_ok=True)

    def setUp(self):
        for f in self.client.get("/api/trade/favorites").json()["favorites"]:
            self.client.delete(f"/api/trade/favorites/{f['id']}")

    def _save(self, name, config, **extra):
        return self.client.post("/api/trade/favorites",
                                json={"name": name, "config": config, **extra})

    def test_save_list_roundtrip_keeps_config(self):
        cfg = {"mode": "filtered", "usable_scu": 64, "ship": "C2 Hercules",
               "commodities": ["Gold"], "system": "Stanton", "max_stops": 5}
        r = self._save("Gold loop", cfg, start_label="Everus Harbor")
        self.assertEqual(r.status_code, 200, r.text)
        favs = self.client.get("/api/trade/favorites").json()["favorites"]
        self.assertEqual(len(favs), 1)
        got = favs[0]
        self.assertEqual(got["name"], "Gold loop")
        self.assertEqual(got["config"]["mode"], "filtered")
        self.assertEqual(got["config"]["commodities"], ["Gold"])
        self.assertEqual(got["config"]["ship"], "C2 Hercules")
        self.assertEqual(got["config"]["start_label"], "Everus Harbor")

    def test_resave_same_name_overwrites_in_place(self):
        self._save("My route", {"mode": "auto", "usable_scu": 32})
        self._save("My route", {"mode": "auto", "usable_scu": 96})
        favs = self.client.get("/api/trade/favorites").json()["favorites"]
        self.assertEqual(len(favs), 1)                       # not duplicated
        self.assertEqual(favs[0]["config"]["usable_scu"], 96)

    def test_delete_removes_only_that_favorite(self):
        a = self._save("A", {"mode": "auto", "usable_scu": 32}).json()["id"]
        self._save("B", {"mode": "auto", "usable_scu": 32})
        r = self.client.delete(f"/api/trade/favorites/{a}")
        self.assertEqual(r.status_code, 200)
        names = [f["name"] for f in self.client.get("/api/trade/favorites").json()["favorites"]]
        self.assertEqual(names, ["B"])
        self.assertEqual(self.client.delete(f"/api/trade/favorites/{a}").status_code, 404)

    def test_favorites_are_scoped_to_the_member(self):
        self._save("Mine", {"mode": "auto", "usable_scu": 32})
        # A different member sees none of it.
        other = {"id": "99", "username": "other", "is_admin": False}
        app.app.dependency_overrides[app.require_session] = lambda: other
        try:
            self.assertEqual(self.client.get("/api/trade/favorites").json()["favorites"], [])
        finally:
            app.app.dependency_overrides[app.require_session] = lambda: self._user

    def test_invalid_config_is_rejected(self):
        r = self._save("Bad", {"mode": "auto", "usable_scu": 0})   # SCU must be > 0
        self.assertEqual(r.status_code, 422)


class QuantumEnrichmentTests(unittest.TestCase):
    """#27 — /api/ships quantum enrichment + drive resolution wiring."""

    @classmethod
    def setUpClass(cls):
        # auth_gate is middleware (runs before DI): satisfy it with a stub token user.
        cls._member = {"id": "1", "username": "tester", "is_admin": False}
        cls._orig_token_user = app.token_user
        app.token_user = lambda request: cls._member
        cls.client = TestClient(app.app)

    @classmethod
    def tearDownClass(cls):
        app.token_user = cls._orig_token_user

    def test_ships_endpoint_carries_quantum_for_matched(self):
        rows = self.client.get("/api/ships").json()
        matched = [s for s in rows if "quantum" in s]
        self.assertTrue(matched, "expected some ships enriched with quantum data")
        q = matched[0]["quantum"]
        for k in ("fuel_scu", "default_range_m", "max_range_m", "drives"):
            self.assertIn(k, q)
        self.assertTrue(q["drives"])
        self.assertTrue(any(d["is_default"] for d in q["drives"]))

    def test_unmatched_ships_have_no_quantum_key(self):
        # Never fabricate: an unmatched ship simply omits the key.
        rows = self.client.get("/api/ships").json()
        self.assertTrue(any("quantum" not in s for s in rows))

    def test_resolve_drive_default_and_override(self):
        row = next((s for s in self.client.get("/api/ships").json() if "quantum" in s), None)
        self.assertIsNotNone(row)
        name = row["name"]
        fuel_req, max_range_m, qd = app._resolve_drive(name, None)
        self.assertIsNotNone(fuel_req)
        self.assertEqual(qd, row["quantum"]["default_qd"])
        # an override selects that specific drive
        alt = next((d for d in row["quantum"]["drives"] if not d["is_default"]), None)
        if alt:
            fr2, mr2, qd2 = app._resolve_drive(name, alt["qd"])
            self.assertEqual(qd2, alt["qd"])
            self.assertEqual(fr2, alt["fuel_req"])
        # unmatched ship -> all None (no fabricated numbers)
        self.assertEqual(app._resolve_drive("Definitely Not A Ship", None), (None, None, None))


class CommissionModeTests(unittest.TestCase):
    """#25 — the blueprint feed endpoints + the commission listing mode:
    create/validate, the quote flow (never an instant deal), accept →
    withdraw-after-accept reopening the job, dual-confirm freezing the quote,
    lazy needed-by expiry, and the WANTED announce copy."""

    _BP = {
        "uuid": "u-1", "name": "Omnisky III Cannon", "cat": "Weapon Gun",
        "type": "WeaponGun", "cls": "amrs_lasercannon_s1", "time_s": 540,
        "default": False,
        "aspects": [
            {"slot": "Frame", "kind": "resource", "input": "Agricium",
             "scu": 0.36, "min_q": 1,
             "mods": [{"prop": "Integrity", "dir": "higher", "mode": "multiplier",
                       "ranges": [{"q0": 0, "q1": 1000, "v0": 0.9, "v1": 1.1}]}]},
            {"slot": "Emitter", "kind": "item", "input": "Hadanite", "qty": 7,
             "mods": [{"prop": "Impact Force", "dir": "higher", "mode": "multiplier",
                       "ranges": [{"q0": 0, "q1": 1000, "v0": 0.95, "v1": 1.05}]}]},
        ],
    }

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        cls._tmp.close()
        db.init(Path(cls._tmp.name))
        cls._requester = {"id": "111", "username": "req", "is_admin": False}
        cls._crafter = {"id": "222", "username": "crafty", "is_admin": False}
        cls._other = {"id": "333", "username": "third", "is_admin": False}
        cls._current = cls._requester
        app.app.dependency_overrides[app.require_session] = lambda: cls._current
        cls._orig_token_user = app.token_user
        app.token_user = lambda request: cls._current
        cls._orig_feed = app.blueprints_feed
        app.blueprints_feed = {"BP_CRAFT_AMRS_LaserCannon_S1": cls._BP}
        cls._orig_catalog_by_id = app.item_catalog_by_id
        # A resolvable NON-blueprint item, to prove the mode check itself rejects it.
        app.item_catalog_by_id = {**app.item_catalog_by_id, "commodity:TestOre": {
            "item_id": "commodity:TestOre", "name": "Test Ore",
            "kind": "commodity", "unit": "SCU"}}
        cls.client = TestClient(app.app)

    @classmethod
    def tearDownClass(cls):
        app.app.dependency_overrides.clear()
        app.token_user = cls._orig_token_user
        app.blueprints_feed = cls._orig_feed
        app.item_catalog_by_id = cls._orig_catalog_by_id
        Path(cls._tmp.name).unlink(missing_ok=True)

    def _as(self, user):
        type(self)._current = user

    def setUp(self):
        self._as(self._requester)

    def _post_commission(self, **over):
        body = {"item_id": "blueprint:BP_CRAFT_AMRS_LaserCannon_S1",
                "mode": "commission", "qty": 1, "price_auec": 45000,
                "materials": "crafter",
                "crafted": {"quality": 700,
                            "stats": [{"name": "Impact Force", "value": "≥ 1.05x"}]}}
        body.update(over)
        return self.client.post("/api/market", json=body)

    # -- blueprint feed endpoints --

    def test_blueprint_index_and_search(self):
        r = self.client.get("/api/blueprints").json()
        self.assertEqual(r["total"], 1)
        row = r["blueprints"][0]
        self.assertEqual(row["key"], "BP_CRAFT_AMRS_LaserCannon_S1")
        self.assertIn("Agricium", row["inputs"])
        self.assertIn("Weapon Gun", r["categories"])
        # search matches name/input substrings; a miss returns nothing
        self.assertEqual(self.client.get("/api/blueprints?q=omnisky").json()["total"], 1)
        self.assertEqual(self.client.get("/api/blueprints?q=hadanite").json()["total"], 1)
        self.assertEqual(self.client.get("/api/blueprints?q=nope").json()["total"], 0)
        self.assertEqual(
            self.client.get("/api/blueprints?category=Helmet%20(Armor)").json()["total"], 0)

    def test_blueprint_detail_carries_derived_views(self):
        r = self.client.get("/api/blueprints/BP_CRAFT_AMRS_LaserCannon_S1")
        self.assertEqual(r.status_code, 200)
        d = r.json()
        self.assertEqual(d["manifest"]["items"][0]["input"], "Hadanite")
        props = {s["prop"] for s in d["stat_drivers"]}
        self.assertEqual(props, {"Integrity", "Impact Force"})
        self.assertEqual(self.client.get("/api/blueprints/BP_NOPE").status_code, 404)

    # -- create + validation --

    def test_create_commission(self):
        r = self._post_commission()
        self.assertEqual(r.status_code, 200, r.text)
        v = r.json()
        self.assertEqual(v["mode"], "commission")
        self.assertEqual(v["item_name"], "Omnisky III Cannon")   # stamped from the feed
        self.assertEqual(v["blueprint_key"], "BP_CRAFT_AMRS_LaserCannon_S1")
        self.assertEqual(v["materials"], "crafter")
        self.assertEqual(v["attributes"]["spec"]["quality"], 700)
        self.assertEqual(v["commission"]["budget"], 45000)
        self.assertEqual(v["commission"]["quote_count"], 0)

    def test_budget_is_optional(self):
        r = self._post_commission(price_auec=None)
        self.assertEqual(r.status_code, 200)
        self.assertIsNone(r.json()["commission"]["budget"])

    def test_spec_inputs_persist_per_slot_material_minimums(self):
        r = self._post_commission(crafted={
            "quality": 800,
            "inputs": [{"slot": "Emitter", "input": "Hadanite", "min_q": 800},
                       {"slot": "Frame", "input": "Agricium", "min_q": 500},
                       {"slot": " ", "input": "Blank", "min_q": 5}]})   # blank slot dropped
        self.assertEqual(r.status_code, 200, r.text)
        spec = r.json()["attributes"]["spec"]
        self.assertEqual(spec["inputs"], [
            {"slot": "Emitter", "input": "Hadanite", "min_q": 800},
            {"slot": "Frame", "input": "Agricium", "min_q": 500}])
        # out-of-range min_q is rejected at the model layer
        bad = self._post_commission(crafted={
            "inputs": [{"slot": "Emitter", "input": "Hadanite", "min_q": 2000}]})
        self.assertEqual(bad.status_code, 422)

    def test_rejects_non_blueprint_item(self):
        r = self._post_commission(item_id="commodity:TestOre")
        self.assertEqual(r.status_code, 400)
        self.assertIn("blueprint", r.json()["detail"])

    def test_rejects_bad_materials_and_past_needed_by(self):
        self.assertEqual(self._post_commission(materials="magic").status_code, 400)
        past = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        self.assertEqual(self._post_commission(ends_at=past).status_code, 400)

    # -- quote flow --

    def test_quote_never_strikes_instantly_and_best_quote_derives(self):
        lid = self._post_commission().json()["id"]
        self._as(self._crafter)
        r = self.client.post(f"/api/market/{lid}/offer",
                             json={"amount_auec": 40000, "offer_note": "2 days"})
        self.assertEqual(r.status_code, 200)
        v = r.json()
        # at/below budget still does NOT auto-strike — the requester picks
        self.assertEqual(v["status"], "open")
        self._as(self._other)
        self.client.post(f"/api/market/{lid}/offer", json={"amount_auec": 38000})
        v = self.client.get(f"/api/market/{lid}").json()
        self.assertEqual(v["commission"]["quote_count"], 2)
        self.assertEqual(v["commission"]["best_quote"], 38000)
        # a quote without an amount is rejected
        self.assertEqual(
            self.client.post(f"/api/market/{lid}/offer",
                             json={"offer_note": "no price"}).status_code, 400)

    def test_board_card_and_filters(self):
        lid = self._post_commission().json()["id"]
        board = self.client.get("/api/market?mode=commission").json()
        card = next(c for c in board["listings"] if c["id"] == lid)
        self.assertEqual(card["commission"]["budget"], 45000)
        self.assertIsNone(card["commission"]["best_quote"])   # no quotes yet
        self.assertIn("announce_available", board)
        kinds = self.client.get("/api/market?kind=blueprint").json()
        self.assertIn(lid, [c["id"] for c in kinds["listings"]])

    # -- accept, withdraw-after-accept, dual-confirm --

    def _accept_flow(self):
        lid = self._post_commission().json()["id"]
        self._as(self._crafter)
        v = self.client.post(f"/api/market/{lid}/offer",
                             json={"amount_auec": 40000}).json()
        oid = v["my_offer"]["id"]
        self._as(self._other)
        self.client.post(f"/api/market/{lid}/offer", json={"amount_auec": 50000})
        self._as(self._requester)
        v = self.client.patch(f"/api/market/{lid}/offer/{oid}",
                              json={"action": "accept"}).json()
        return lid, oid, v

    def test_accept_goes_pending_with_the_crafter(self):
        lid, oid, v = self._accept_flow()
        self.assertEqual(v["status"], "pending")
        self.assertEqual(v["buyer_id"], "222")               # crafter rides buyer_id
        lost = [o for o in v["offers"] if o["status"] == "lost"]
        self.assertEqual(len(lost), 1)

    def test_accepted_crafter_can_withdraw_and_job_reopens(self):
        lid, oid, _ = self._accept_flow()
        self._as(self._crafter)
        v = self.client.patch(f"/api/market/{lid}/offer/{oid}",
                              json={"action": "withdraw"}).json()
        self.assertEqual(v["status"], "open")                 # back on the board
        self.assertIsNone(v["buyer_id"])
        statuses = {o["id"]: o["status"] for o in v["offers"]}
        self.assertEqual(statuses[oid], "withdrawn")
        self.assertIn("lost", statuses.values())              # lost offers stay lost

    def test_dual_confirm_freezes_the_quote(self):
        lid, oid, _ = self._accept_flow()
        self._as(self._requester)
        self.client.post(f"/api/market/{lid}/confirm")
        self._as(self._crafter)
        v = self.client.post(f"/api/market/{lid}/confirm").json()
        self.assertEqual(v["status"], "completed")
        self.assertEqual(v["attributes"]["spec"]["quality"], 700)
        listing = db.get_listing(lid)
        self.assertEqual(listing["final_auec"], 40000)        # the accepted quote

    # -- lazy needed-by expiry --

    def test_lapsed_needed_by_expires_without_a_winner(self):
        future = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
        lid = self._post_commission(ends_at=future).json()["id"]
        self._as(self._crafter)
        self.client.post(f"/api/market/{lid}/offer", json={"amount_auec": 40000})
        past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        db.update_listing(lid, {"ends_at": past}, past)       # force the lapse
        self._as(self._requester)
        v = self.client.get(f"/api/market/{lid}").json()
        self.assertEqual(v["status"], "expired")              # no auction-style winner
        self.assertIsNone(v["buyer_id"])

    # -- edit --

    def test_edit_budget_materials_and_spec(self):
        lid = self._post_commission().json()["id"]
        r = self.client.patch(
            f"/api/market/{lid}",
            json={"price_auec": 60000, "materials": "split",
                  "crafted": {"quality": 900}})
        v = r.json()
        self.assertEqual(v["price_auec"], 60000)
        self.assertEqual(v["materials"], "split")
        self.assertEqual(v["attributes"]["spec"]["quality"], 900)
        self.assertEqual(
            self.client.patch(f"/api/market/{lid}",
                              json={"materials": "magic"}).status_code, 400)


class CommissionNotifyTests(unittest.TestCase):
    """#25 step 4 — the WANTED announce (terms in the headline, per-member
    cooldown) and the commission-flavored quote/accept/complete copy."""

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
        app._commission_announce_at.clear()

    _LISTING = {"id": 7, "seller_id": "111", "buyer_id": "222",
                "mode": "commission", "item_name": "Omnisky III Cannon",
                "qty": 1, "price_auec": 45000, "materials": "crafter",
                "ends_at": "2027-01-15T00:00:00+00:00", "final_auec": 40000,
                "attributes": {"spec": {"quality": 700}}}
    _OFFER = {"id": 4, "bidder_id": "222", "amount_auec": 40000}

    def test_wanted_announce_carries_the_terms(self):
        asyncio.run(app._notify_commission_posted(self._LISTING))
        msg = self.sent[0]
        self.assertIn("WANTED: Omnisky III Cannon", msg["text"])
        self.assertIn("Q700+", msg["text"])
        self.assertIn("45,000 aUEC", msg["text"])
        self.assertIn("crafter sources mats", msg["text"])
        self.assertIn("needed by 2027-01-15", msg["text"])
        self.assertEqual(msg["dedup_key"], "commission-posted:7")

    def test_announce_cooldown_gates_per_member(self):
        self.assertTrue(app._commission_announce_ok("111"))
        self.assertFalse(app._commission_announce_ok("111"))   # inside the cooldown
        self.assertTrue(app._commission_announce_ok("999"))    # other members unaffected

    def test_wanted_announce_mentions_capable_crafters(self):
        now = "2026-07-05T00:00:00+00:00"
        db.add_member_blueprint("222", "TEST_BP", now)
        db.add_member_blueprint("333", "TEST_BP", now)
        db.add_member_blueprint("111", "TEST_BP", now)   # the poster — never self-pinged
        asyncio.run(app._notify_commission_posted(
            {**self._LISTING, "blueprint_key": "TEST_BP"}))
        msg = self.sent[0]
        self.assertEqual(msg["mentions"], ["222", "333"])
        self.assertIn("Can craft: <@222> <@333>", msg["text"])
        self.assertNotIn("<@111>", msg["text"])

    def test_wanted_announce_without_recipe_pings_nobody(self):
        asyncio.run(app._notify_commission_posted(self._LISTING))
        self.assertEqual(self.sent[0]["mentions"], [])
        self.assertNotIn("Can craft", self.sent[0]["text"])

    def test_quote_and_accept_read_commission_flavored(self):
        asyncio.run(app._notify_market_offer(self._LISTING, self._OFFER, deal=False))
        self.assertIn("New quote", self.sent[0]["text"])
        asyncio.run(app._notify_market_accepted(self._LISTING, self._OFFER))
        self.assertIn("You got the job", self.sent[1]["text"])
        asyncio.run(app._notify_market_completed(self._LISTING))
        self.assertIn("Commission complete", self.sent[2]["text"])


class CraftGoalTests(unittest.TestCase):
    """Personal vs org goals + blueprint-seeded craft goals (#14.2) and the member
    blueprint library / commission crafter-matching (#25.1). Drives the real
    endpoints over a TestClient with a controlled blueprint feed."""

    # A controlled feed: real commodity names so the seed maps to catalog items,
    # plus one bogus input to exercise the unmapped path.
    _FEED = {"TEST_BP": {
        "name": "Test Cannon", "cat": "Weapon Gun", "time_s": 300, "default": False,
        "unlocks": [], "aspects": [
            {"slot": "Frame", "kind": "resource", "input": "Agricium",
             "scu": 0.5, "min_q": 1,
             "mods": [{"prop": "Integrity", "dir": "higher", "mode": "multiplier",
                       "ranges": [{"q0": 0, "q1": 1000, "v0": 0.9, "v1": 1.1}]}]},
            {"slot": "Emitter", "kind": "item", "input": "Hadanite",
             "qty": 4, "min_q": 500, "mods": []},
            {"slot": "Weird", "kind": "resource", "input": "Nonexistanium",
             "scu": 1.0, "min_q": 0, "mods": []}]}}

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        cls._tmp.close()
        db.init(Path(cls._tmp.name))
        db.set_setting(notify._webhook_key("goals"), _GOOD_WEBHOOK)
        cls._a = {"id": "111", "username": "ana", "is_admin": False}
        cls._b = {"id": "222", "username": "bo", "is_admin": False}
        app.app.dependency_overrides[app.require_session] = lambda: cls._a
        # The auth-gate middleware runs before the route dependency, so token_user
        # must also resolve to *some* member for the request to reach the route.
        cls._orig_token_user = app.token_user
        app.token_user = lambda request: cls._a
        cls._orig_feed = app.blueprints_feed
        app.blueprints_feed = cls._FEED
        # A controlled price map so the materials-cost estimate is deterministic
        # (Agricium priced, everything else unpriced).
        cls._orig_prices = app.item_prices
        app.item_prices = {"commodity:agricium": {"buy": 100, "sell": 120}}
        cls._orig_send = notify.send
        cls.client = TestClient(app.app)

    @classmethod
    def tearDownClass(cls):
        app.app.dependency_overrides.clear()
        app.token_user = cls._orig_token_user
        app.blueprints_feed = cls._orig_feed
        app.item_prices = cls._orig_prices
        notify.send = cls._orig_send
        Path(cls._tmp.name).unlink(missing_ok=True)

    def setUp(self):
        self._as(self._a)
        self.sent = []

        async def _capture(category, text, *, mentions=None, dedup_key=None):
            self.sent.append({"category": category, "text": text})
            return True
        notify.send = _capture

    def _as(self, u):
        app.app.dependency_overrides[app.require_session] = lambda: u

    # -- blueprint seeding --

    def test_seed_expands_manifest_into_line_items(self):
        r = self.client.post("/api/goals", json={
            "title": "Craft a cannon", "blueprint_key": "TEST_BP", "blueprint_qty": 2})
        self.assertEqual(r.status_code, 200, r.text)
        g = r.json()
        lines = {l["item_id"]: l for l in g["line_items"]}
        self.assertAlmostEqual(lines["commodity:agricium"]["qty_needed"], 1.0)  # 0.5×2
        self.assertEqual(lines["commodity:agricium"]["unit"], "SCU")
        self.assertAlmostEqual(lines["commodity:hadanite"]["qty_needed"], 8)    # 4×2
        self.assertEqual(lines["commodity:hadanite"]["unit"], "each")
        self.assertEqual(g["blueprint_key"], "TEST_BP")
        self.assertEqual(g["craft"]["name"], "Test Cannon")
        self.assertEqual(g["craft"]["min_q"]["commodity:hadanite"], 500)
        self.assertIn("Nonexistanium", g.get("seed_unmapped", []))

    def test_unknown_blueprint_seed_is_404(self):
        r = self.client.post("/api/goals", json={"title": "x", "blueprint_key": "NOPE"})
        self.assertEqual(r.status_code, 404)

    # -- crafted-sale identity + expected stats (#25.1 §11.3 / §11.4) --

    def test_sale_of_blueprint_item_carries_recipe_identity(self):
        r = self.client.post("/api/market", json={
            "item_id": "blueprint:TEST_BP", "qty": 1, "mode": "sale",
            "price_auec": 50000, "crafted": {"quality": 800}})
        self.assertEqual(r.status_code, 200, r.text)
        v = r.json()
        self.assertEqual(v["blueprint_key"], "TEST_BP")   # identity, not commission-only
        self.assertEqual(v["item_name"], "Test Cannon")
        # kind filter = catalog-id prefix, so kind=blueprint finds crafted goods.
        cards = self.client.get("/api/market?kind=blueprint").json()["listings"]
        self.assertIn(v["id"], [c["id"] for c in cards])
        # Detail: uniform expected stats at the advertised Q800 + the mats anchor.
        d = self.client.get(f"/api/market/{v['id']}").json()
        ex = d["expected_stats"]
        self.assertEqual(ex["basis"], "uniform")
        self.assertEqual(ex["quality"], 800)
        stats = {s["prop"]: s["value"] for s in ex["stats"]}
        self.assertAlmostEqual(stats["Integrity"], 1.06)   # 0.9 + 0.2×0.8
        self.assertEqual(d["mats_est"], 50)                # 0.5 SCU × 100 buy

    def test_sale_without_quality_has_no_stat_preview(self):
        lid = self.client.post("/api/market", json={
            "item_id": "blueprint:TEST_BP", "mode": "sale",
            "price_auec": 1000}).json()["id"]
        self.assertIsNone(self.client.get(f"/api/market/{lid}").json()["expected_stats"])

    def test_commission_expected_stats_use_slot_asks(self):
        r = self.client.post("/api/market", json={
            "item_id": "blueprint:TEST_BP", "mode": "commission",
            "crafted": {"inputs": [
                {"slot": "Frame", "input": "Agricium", "min_q": 750}]}})
        self.assertEqual(r.status_code, 200, r.text)
        d = self.client.get(f"/api/market/{r.json()['id']}").json()
        ex = d["expected_stats"]
        self.assertEqual(ex["basis"], "inputs")
        stats = {s["prop"]: s["value"] for s in ex["stats"]}
        self.assertAlmostEqual(stats["Integrity"], 1.05)   # Frame at Q750

    def test_catalog_bp_flag_offers_blueprints(self):
        items = self.client.get("/api/catalog?q=test cannon&bp=1").json()["items"]
        self.assertIn("blueprint:TEST_BP", [i["item_id"] for i in items])
        # Without the flag (inventory/goals pickers) recipes stay out.
        items = self.client.get("/api/catalog?q=test cannon").json()["items"]
        self.assertNotIn("blueprint:TEST_BP", [i["item_id"] for i in items])

    # -- estimated material cost + stat vocabulary (#25.1 §12 / §11.2) --

    def test_blueprint_detail_carries_est_cost(self):
        d = self.client.get("/api/blueprints/TEST_BP").json()
        self.assertEqual(d["est_cost"]["total"], 50)         # 0.5 SCU × 100 buy
        # Unknown resource + the gem count both degrade to unpriced.
        self.assertEqual(sorted(d["est_cost"]["unpriced"]),
                         ["Hadanite", "Nonexistanium"])

    def test_stat_names_endpoint_lists_canonical_vocabulary(self):
        r = self.client.get("/api/blueprints/stat-names")
        self.assertEqual(r.status_code, 200)                 # not eaten by /{bp_key}
        self.assertEqual(r.json()["stats"], ["Integrity"])

    def test_goal_detail_carries_est_cost(self):
        gid = self.client.post("/api/goals", json={
            "title": "cannon", "blueprint_key": "TEST_BP",
            "blueprint_qty": 2}).json()["id"]
        g = self.client.get(f"/api/goals/{gid}").json()
        # Per single craft — the UI scales by the goal's craft count.
        self.assertEqual(g["craft"]["est_cost"]["total"], 50)

    # -- spec-builder quality asks (#14.2 follow-up) --

    def test_seed_with_inputs_raises_line_target_quality(self):
        r = self.client.post("/api/goals", json={
            "title": "high-spec cannon", "blueprint_key": "TEST_BP",
            "blueprint_qty": 2,
            "blueprint_inputs": [
                {"slot": "Frame", "input": "Agricium", "min_q": 800},
                {"slot": "Bogus", "input": "Agricium", "min_q": 999}]})  # unknown slot dropped
        self.assertEqual(r.status_code, 200, r.text)
        g = r.json()
        lines = {l["item_id"]: l for l in g["line_items"]}
        self.assertEqual(lines["commodity:agricium"]["min_q"], 800)     # ask > recipe's 1
        self.assertEqual(lines["commodity:hadanite"]["min_q"], 500)     # recipe minimum kept
        # The saved spec rides on the craft block (edit restore + detail display).
        self.assertEqual(g["craft"]["qty"], 2)
        self.assertEqual(g["craft"]["inputs"],
                         [{"slot": "Frame", "input": "Agricium", "min_q": 800}])
        # min_q map + progress lines both badge the stored targets.
        self.assertEqual(g["craft"]["min_q"]["commodity:agricium"], 800)
        pline = next(l for l in g["progress"]["lines"]
                     if l["item_id"] == "commodity:agricium")
        self.assertEqual(pline["min_q"], 800)
        # Expected stats at the asked qualities: Frame Q800 → 0.9 + 0.2×0.8.
        prev = {s["prop"]: s["value"] for s in g["craft"]["stat_preview"]}
        self.assertAlmostEqual(prev["Integrity"], 1.06)

    def test_edit_reseed_rescales_lines_and_keeps_spec(self):
        gid = self.client.post("/api/goals", json={
            "title": "cannon", "blueprint_key": "TEST_BP",
            "blueprint_inputs": [
                {"slot": "Frame", "input": "Agricium", "min_q": 600}]}).json()["id"]
        # Re-seed on edit: new craft count + qualities, no line items → the recipe
        # re-expands server-side.
        r = self.client.patch(f"/api/goals/{gid}", json={
            "title": "cannon", "blueprint_key": "TEST_BP", "blueprint_qty": 3,
            "blueprint_inputs": [
                {"slot": "Frame", "input": "Agricium", "min_q": 900}]})
        self.assertEqual(r.status_code, 200, r.text)
        g = r.json()
        lines = {l["item_id"]: l for l in g["line_items"]}
        self.assertAlmostEqual(lines["commodity:agricium"]["qty_needed"], 1.5)  # 0.5×3
        self.assertEqual(lines["commodity:agricium"]["min_q"], 900)
        self.assertEqual(g["craft"]["qty"], 3)

    def test_manual_line_edit_preserves_the_craft_spec(self):
        gid = self.client.post("/api/goals", json={
            "title": "cannon", "blueprint_key": "TEST_BP",
            "blueprint_inputs": [
                {"slot": "Frame", "input": "Agricium", "min_q": 700}]}).json()["id"]
        # A hand edit of the rows (no blueprint fields sent) must not drop the
        # goal's craft tag or its saved spec.
        r = self.client.patch(f"/api/goals/{gid}", json={
            "title": "cannon", "line_items": [
                {"item_id": "commodity:agricium", "qty_needed": 9}]})
        self.assertEqual(r.status_code, 200, r.text)
        g = r.json()
        self.assertEqual(g["blueprint_key"], "TEST_BP")
        self.assertEqual(g["craft"]["inputs"][0]["min_q"], 700)

    # -- personal visibility --

    def test_personal_goal_hidden_from_others(self):
        gid = self.client.post("/api/goals", json={
            "title": "my private stash", "visibility": "personal",
            "blueprint_key": "TEST_BP"}).json()["id"]
        # creator sees it on the board + can open it
        board = self.client.get("/api/goals").json()["goals"]
        self.assertIn(gid, [g["id"] for g in board])
        self.assertEqual(self.client.get(f"/api/goals/{gid}").status_code, 200)
        # another member does not
        self._as(self._b)
        board_b = self.client.get("/api/goals").json()["goals"]
        self.assertNotIn(gid, [g["id"] for g in board_b])
        self.assertEqual(self.client.get(f"/api/goals/{gid}").status_code, 404)

    def test_edit_without_visibility_preserves_personal_scope(self):
        gid = self.client.post("/api/goals", json={
            "title": "keep me private", "visibility": "personal",
            "blueprint_key": "TEST_BP"}).json()["id"]
        # a PATCH that omits visibility must not silently flip it back to org
        r = self.client.patch(f"/api/goals/{gid}", json={
            "title": "renamed", "line_items": [
                {"item_id": "commodity:agricium", "qty_needed": 3}]})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["visibility"], "personal")

    def test_personal_goal_met_does_not_broadcast(self):
        # a one-line personal goal, filled to 100% — the org-wide ping must stay silent
        gid = self.client.post("/api/goals", json={
            "title": "solo", "visibility": "personal",
            "line_items": [{"item_id": "commodity:agricium", "qty_needed": 5}]}).json()["id"]
        self.client.post(f"/api/goals/{gid}/contribute", json={
            "item_id": "commodity:agricium", "qty": 5, "location": "Area18"})
        self.assertEqual(self.sent, [])   # no goals-category broadcast

    def test_detail_groups_my_materials_by_location(self):
        gid = self.client.post("/api/goals", json={
            "title": "stage it", "visibility": "personal",
            "line_items": [{"item_id": "commodity:agricium", "qty_needed": 20}]}).json()["id"]
        self.client.post(f"/api/goals/{gid}/contribute", json={
            "item_id": "commodity:agricium", "qty": 8, "location": "Area18"})
        g = self.client.get(f"/api/goals/{gid}").json()
        locs = {grp["location"]: grp for grp in g["my_locations"]}
        self.assertIn("Area18", locs)
        self.assertAlmostEqual(locs["Area18"]["items"][0]["qty"], 8)

    def test_org_goal_met_still_broadcasts(self):
        gid = self.client.post("/api/goals", json={
            "title": "shared", "visibility": "org",
            "line_items": [{"item_id": "commodity:agricium", "qty_needed": 5}]}).json()["id"]
        self.client.post(f"/api/goals/{gid}/contribute", json={
            "item_id": "commodity:agricium", "qty": 5, "location": "Area18"})
        self.assertTrue(any(m["category"] == "goals" for m in self.sent))

    # -- member blueprint library --

    def test_blueprint_library_crud(self):
        self.assertEqual(self.client.get("/api/me/blueprints").json()["blueprints"], [])
        r = self.client.post("/api/me/blueprints", json={"blueprint_key": "TEST_BP"})
        self.assertEqual(r.status_code, 200, r.text)
        lib = r.json()["blueprints"]
        self.assertEqual(lib[0]["blueprint_key"], "TEST_BP")
        self.assertEqual(lib[0]["name"], "Test Cannon")
        # idempotent add, unknown-key reject, then delete
        self.client.post("/api/me/blueprints", json={"blueprint_key": "TEST_BP"})
        self.assertEqual(len(self.client.get("/api/me/blueprints").json()["blueprints"]), 1)
        self.assertEqual(self.client.post(
            "/api/me/blueprints", json={"blueprint_key": "NOPE"}).status_code, 404)
        d = self.client.request("DELETE", "/api/me/blueprints", params={"key": "TEST_BP"})
        self.assertEqual(d.status_code, 200)
        self.assertEqual(self.client.get("/api/me/blueprints").json()["blueprints"], [])

    def test_commission_card_reports_crafter_matching(self):
        now = datetime.now(timezone.utc).isoformat()
        db.create_listing({"seller_id": "999", "item_id": "blueprint:TEST_BP",
                           "item_name": "Test Cannon", "unit": "each", "qty": 1,
                           "mode": "commission", "price_auec": 40000,
                           "blueprint_key": "TEST_BP", "status": "open",
                           "created_at": now, "updated_at": now})
        # ana can't craft it yet
        cards = self.client.get("/api/market", params={"mode": "commission"}).json()["listings"]
        card = next(c for c in cards if c.get("blueprint_key") == "TEST_BP")
        self.assertEqual(card["commission"]["can_craft_count"], 0)
        self.assertFalse(card["commission"]["i_can_craft"])
        # after adding it to her library, the card matches
        self.client.post("/api/me/blueprints", json={"blueprint_key": "TEST_BP"})
        cards = self.client.get("/api/market", params={"mode": "commission"}).json()["listings"]
        card = next(c for c in cards if c.get("blueprint_key") == "TEST_BP")
        self.assertEqual(card["commission"]["can_craft_count"], 1)
        self.assertTrue(card["commission"]["i_can_craft"])


if __name__ == "__main__":
    unittest.main(verbosity=1)
