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
import unittest

from fastapi.testclient import TestClient

import app

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


if __name__ == "__main__":
    unittest.main(verbosity=1)
