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

import re
import tempfile
import unittest
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

    def test_store_mask_validate_roundtrip(self):
        # Reject a non-Discord URL (anti-SSRF) ...
        bad = self.client.post("/api/settings",
                               json={"discord_webhook_url": "https://evil.com/x"})
        self.assertEqual(bad.status_code, 400)

        # ... accept a real one and flip the toggles.
        ok = self.client.post("/api/settings", json={
            "discord_webhook_url": _GOOD_WEBHOOK,
            "discord_notify": {"events": True, "goals": False},
        })
        self.assertEqual(ok.status_code, 200)

        s = self.client.get("/api/settings").json()
        self.assertTrue(s["discord_webhook_set"])
        # The raw secret must NEVER appear in any response.
        self.assertNotIn(_GOOD_WEBHOOK, ok.text)
        self.assertNotIn(_GOOD_WEBHOOK, self.client.get("/api/settings").text)
        self.assertTrue(s["discord_webhook_tail"].startswith("…"))
        self.assertTrue(s["discord_notify"]["events"])
        self.assertFalse(s["discord_notify"]["goals"])

        # Clearing it ("") turns the feature off.
        self.client.post("/api/settings", json={"discord_webhook_url": ""})
        self.assertFalse(self.client.get("/api/settings").json()["discord_webhook_set"])

    def test_test_send_blocked_without_webhook(self):
        self.client.post("/api/settings", json={"discord_webhook_url": ""})
        r = self.client.post("/api/settings/discord/test")
        self.assertEqual(r.status_code, 400)


if __name__ == "__main__":
    unittest.main(verbosity=1)
