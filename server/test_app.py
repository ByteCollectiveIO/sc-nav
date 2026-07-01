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


if __name__ == "__main__":
    unittest.main(verbosity=1)
