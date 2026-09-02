"""Administrator authentication must be fail-closed.

The repository ships no default account. A deployment that forgets to set
SAFENEST_ADMIN_ID / SAFENEST_ADMIN_PASSWORD must not be reachable with a
publicly known credential, and must not accept an empty submission either.
"""

from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from backend.portal import PortalAuth


ENV_KEYS = {"SAFENEST_ADMIN_ID": "", "SAFENEST_ADMIN_PASSWORD": "", "SAFENEST_AUTH_SECRET": ""}


class PortalAuthFailClosedTests(unittest.TestCase):
    def test_unconfigured_auth_reports_not_configured(self) -> None:
        with patch.dict(os.environ, ENV_KEYS, clear=False):
            auth = PortalAuth()
            self.assertFalse(auth.configured)

    def test_unconfigured_auth_rejects_every_login(self) -> None:
        with patch.dict(os.environ, ENV_KEYS, clear=False):
            auth = PortalAuth()
            for admin_id, password in (
                ("", ""),
                ("admin", ""),
                ("", "anything"),
                ("admin", "admin"),
                ("admin", "password"),
                (None, None),
            ):
                with self.subTest(admin_id=admin_id):
                    self.assertIsNone(auth.login(admin_id, password))

    def test_unconfigured_auth_rejects_every_token(self) -> None:
        with patch.dict(os.environ, ENV_KEYS, clear=False):
            auth = PortalAuth()
            self.assertFalse(auth.verify(""))
            self.assertFalse(auth.verify("anything"))

    def test_no_default_credential_is_compiled_into_the_source(self) -> None:
        """A configured account must never be satisfied by a shipped default."""
        with patch.dict(os.environ, ENV_KEYS, clear=False):
            auth = PortalAuth()
            self.assertEqual(auth.admin_id, "")
            self.assertEqual(auth.password, "")

    def test_configured_auth_accepts_only_the_configured_pair(self) -> None:
        env = {
            "SAFENEST_ADMIN_ID": "operator",
            "SAFENEST_ADMIN_PASSWORD": "correct horse battery staple",
            "SAFENEST_AUTH_SECRET": "unit-test-secret",
        }
        with patch.dict(os.environ, env, clear=False):
            auth = PortalAuth()
            self.assertTrue(auth.configured)
            self.assertIsNone(auth.login("operator", "wrong"))
            self.assertIsNone(auth.login("someone", "correct horse battery staple"))
            self.assertIsNone(auth.login("", ""))
            token = auth.login("operator", "correct horse battery staple")
            self.assertIsInstance(token, str)
            self.assertTrue(auth.verify(token))

    def test_token_from_a_configured_auth_is_rejected_once_unconfigured(self) -> None:
        env = {
            "SAFENEST_ADMIN_ID": "operator",
            "SAFENEST_ADMIN_PASSWORD": "correct horse battery staple",
            "SAFENEST_AUTH_SECRET": "unit-test-secret",
        }
        with patch.dict(os.environ, env, clear=False):
            token = PortalAuth().login("operator", "correct horse battery staple")
        self.assertIsNotNone(token)
        with patch.dict(os.environ, ENV_KEYS, clear=False):
            self.assertFalse(PortalAuth(secret="unit-test-secret").verify(str(token)))


class LoginRouteFailClosedTests(unittest.TestCase):
    def setUp(self) -> None:
        try:
            from fastapi.testclient import TestClient  # noqa: F401
        except ImportError:  # pragma: no cover - optional dependency
            self.skipTest("fastapi is not installed")

    def _client(self):
        from fastapi.testclient import TestClient
        from backend.app import create_app

        return TestClient(create_app(start_runtime=False))

    def test_login_route_refuses_when_credentials_are_absent(self) -> None:
        with patch.dict(os.environ, ENV_KEYS, clear=False):
            client = self._client()
            response = client.post("/api/auth/login", json={"id": "admin", "password": "admin"})
            self.assertEqual(response.status_code, 503)
            self.assertIn("SAFENEST_ADMIN_ID", response.json()["error"])

    def test_health_reports_admin_configuration_state(self) -> None:
        with patch.dict(os.environ, ENV_KEYS, clear=False):
            client = self._client()
            self.assertFalse(client.get("/health").json()["admin_auth_configured"])

    def test_protected_routes_stay_unauthorised_without_configuration(self) -> None:
        with patch.dict(os.environ, ENV_KEYS, clear=False):
            client = self._client()
            self.assertEqual(client.get("/api/spaces").status_code, 401)


if __name__ == "__main__":
    unittest.main()
