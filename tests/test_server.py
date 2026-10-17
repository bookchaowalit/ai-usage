"""Offline service authorization and actual loopback HTTP transport tests."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from service_auth import AuthPolicy

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from server import AiUsageApi, AiUsageRequestHandler  # noqa: E402


class AiUsageServerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name)
        with patch.dict(os.environ, {}, clear=True):
            self.api = AiUsageApi(self.path, environment="development", token="")

    def tearDown(self):
        self.tmp.cleanup()

    def test_read_only_routes_and_unknown_providers(self):
        response = self.api.handle("GET", "/api/v1/ai-usage")
        self.assertEqual(response.status, 200)
        self.assertEqual(response.payload["schema_version"], "solo-empire.ai-usage.v1")
        self.assertEqual(response.payload["summary"]["connected"], 0)
        self.assertEqual(self.api.handle("POST", "/api/v1/ai-usage").status, 405)
        self.assertEqual(self.api.handle("GET", "/wrong").status, 404)
        self.assertEqual(self.api.handle("OPTIONS", "/api/v1/ai-usage").status, 204)
        self.assertEqual(self.api.handle("GET", "/health").status, 200)
        self.assertEqual(
            self.api.handle("GET", "/ready").payload["auth"], "development"
        )
        self.assertEqual(self.api.handle("GET", "/ready").status, 503)

    def test_production_static_token_and_partial_policy_fail_closed(self):
        with patch.dict(os.environ, {}, clear=True):
            production = AiUsageApi(
                self.path, environment="production", token="fixture-dev-token"
            )
        response = production.handle(
            "GET",
            "/api/v1/ai-usage",
            headers={"Authorization": "Bearer fixture-dev-token"},
        )
        self.assertEqual(response.status, 503)
        self.assertEqual(production.handle("GET", "/ready").payload["auth"], "disabled")
        policy = AuthPolicy(
            "https://solo.example", "ai-usage-api", "", frozenset(), "ai-usage:read"
        )
        partial = AiUsageApi(self.path, environment="development", auth_policy=policy)
        self.assertEqual(partial.handle("GET", "/api/v1/ai-usage").status, 503)

    def test_owner_jwt_audience_scope_and_claims(self):
        signing_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        public = (
            signing_key.public_key()
            .public_bytes(
                serialization.Encoding.PEM,
                serialization.PublicFormat.SubjectPublicKeyInfo,
            )
            .decode()
        )
        policy = AuthPolicy(
            "https://solo.example",
            "ai-usage-api",
            public,
            frozenset({"fixture-owner"}),
            "ai-usage:read",
        )
        api = AiUsageApi(self.path, environment="production", auth_policy=policy)
        now = int(time.time())
        claims = dict(
            iss="https://solo.example",
            aud="ai-usage-api",
            sub="fixture-owner",
            tenant_id="owner",
            scope="ai-usage:read",
            iat=now,
            nbf=now,
            exp=now + 120,
            jti="fixture-request",
        )

        def headers(changes=None):
            return {
                "Authorization": "Bearer "
                + jwt.encode(
                    {**claims, **(changes or {})}, signing_key, algorithm="RS256"
                )
            }

        self.assertEqual(api.handle("GET", "/api/v1/ai-usage").status, 401)
        self.assertEqual(
            api.handle("GET", "/api/v1/ai-usage", headers=headers()).status, 200
        )
        self.assertEqual(api.handle("GET", "/ready").status, 200)
        for changes, status in (
            ({"aud": "accounts-api"}, 401),
            ({"scope": "accounts:read"}, 403),
            ({"tenant_id": "other"}, 403),
            ({"sub": "other-owner"}, 403),
            ({"exp": now - 300}, 401),
            ({"nbf": now + 300}, 401),
        ):
            with self.subTest(changes=changes):
                response = api.handle(
                    "GET", "/api/v1/ai-usage", headers=headers(changes)
                )
                self.assertEqual(response.status, status)
                self.assertEqual(set(response.payload), {"error"})
                self.assertNotIn("Bearer", json.dumps(response.payload))

    def test_remote_bind_requires_owner_auth_even_in_development(self):
        env = {
            "PATH": os.environ.get("PATH", ""),
            "PYTHONPATH": os.environ.get("PYTHONPATH", ""),
            "AI_USAGE_API_ENV": "development",
            "AI_USAGE_API_TOKEN": "fixture-dev-token",
        }
        result = subprocess.run(
            [
                sys.executable,
                str(ROOT / "server.py"),
                "--host",
                "0.0.0.0",
                "--state-dir",
                str(self.path),
            ],
            env=env,
            capture_output=True,
            text=True,
            timeout=5,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("AI_USAGE_API_AUTH_*", result.stderr)

    def test_actual_http_cors_cache_and_no_snapshot_path_exposure(self):
        class FixtureHandler(AiUsageRequestHandler):
            api = self.api

        server = ThreadingHTTPServer(("127.0.0.1", 0), FixtureHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_port}"
        try:
            req = urllib.request.Request(
                base + "/api/v1/ai-usage", headers={"Origin": "http://localhost:3600"}
            )
            with urllib.request.urlopen(req, timeout=3) as response:
                self.assertEqual(response.headers["Cache-Control"], "no-store")
                self.assertEqual(response.headers["X-Content-Type-Options"], "nosniff")
                self.assertEqual(
                    response.headers["Access-Control-Allow-Origin"],
                    "http://localhost:3600",
                )
                self.assertNotIn(str(self.path), response.read().decode())
            req = urllib.request.Request(
                base + "/api/v1/ai-usage",
                headers={"Origin": "https://untrusted.example"},
            )
            with self.assertRaises(urllib.error.HTTPError) as caught:
                urllib.request.urlopen(req, timeout=3)
            self.assertEqual(caught.exception.code, 403)
            preflight = urllib.request.Request(
                base + "/api/v1/ai-usage",
                method="OPTIONS",
                headers={"Origin": "http://localhost:3600"},
            )
            with urllib.request.urlopen(preflight, timeout=3) as response:
                self.assertEqual(response.status, 204)
                self.assertEqual(
                    response.headers["Access-Control-Allow-Methods"], "GET, OPTIONS"
                )
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)

    def test_expected_source_errors_never_disclose_exception_text(self):
        (self.path / "claude.json").write_text("PRIVATE_MARKER", encoding="utf-8")
        response = self.api.handle("GET", "/api/v1/ai-usage")
        self.assertEqual(response.status, 200)
        self.assertEqual(response.payload["providers"][0]["status"], "error")
        self.assertNotIn("PRIVATE_MARKER", json.dumps(response.payload))
        with patch.object(
            self.api.store, "snapshot", side_effect=RuntimeError("PRIVATE_MARKER")
        ):
            error = self.api.handle("GET", "/api/v1/ai-usage")
        self.assertEqual(error.status, 500)
        self.assertNotIn("PRIVATE_MARKER", json.dumps(error.payload))


if __name__ == "__main__":
    unittest.main()
