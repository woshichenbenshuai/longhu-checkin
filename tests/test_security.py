import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient

from app import db, longhu, runner, security
from app.main import app


class DestinationPolicyTests(unittest.TestCase):
    def test_official_https_notification_destinations(self):
        with patch.dict(os.environ, {}, clear=True):
            for url in (
                "https://api.day.app/test-device",
                "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=test",
                "https://oapi.dingtalk.com/robot/send?access_token=test",
            ):
                self.assertEqual(security.validate_webhook_url(url), url)

    def test_local_http_and_lookalike_hosts_are_rejected(self):
        with patch.dict(os.environ, {}, clear=True):
            for url in (
                "http://api.day.app/test", "https://127.0.0.1/test",
                "https://10.10.10.1/test", "https://api.day.app.attacker.invalid/test",
                "https://api.day.app@attacker.invalid/test",
                "https://user:pass@api.day.app/test", "https://api.day.app:8443/test",
                "https://api.day.app/test#fragment", "https://api.day.app/\ntest",
            ):
                with self.subTest(url=url), self.assertRaises(ValueError):
                    security.validate_webhook_url(url)

    def test_custom_bark_requires_operator_allowlist(self):
        url = "https://bark.example.com/test"
        with patch.dict(os.environ, {"WEBHOOK_ALLOWED_HOSTS": "bark.example.com"}):
            self.assertEqual(security.validate_webhook_url(url), url)

    def test_notification_rejects_legacy_untrusted_database_value(self):
        with patch.object(db, "get_settings", return_value={"webhook_url": "http://127.0.0.1/test"}), \
                patch.object(runner.requests, "Session") as session:
            runner.notify("test")
            session.assert_not_called()


class BrowserProtectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_patch = patch.object(db, "DB_PATH", str(Path(self.temp.name) / "test.db"))
        self.db_patch.start()
        db.init()
        self.client = TestClient(app)

    def tearDown(self):
        self.client.close()
        self.db_patch.stop()
        self.temp.cleanup()

    def test_cross_origin_form_cannot_change_notification_destination(self):
        response = self.client.post(
            "/api/settings", headers={"Origin": "https://attacker.invalid"},
            data={"checkin_time": "08:00", "webhook_url": "https://api.day.app/stolen"},
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(db.get_settings()["webhook_url"], "")

    def test_cross_site_run_is_blocked_before_execution(self):
        with patch.object(runner, "run_account") as run:
            response = self.client.post("/api/accounts/1/run", headers={"Sec-Fetch-Site": "cross-site"})
        self.assertEqual(response.status_code, 403)
        run.assert_not_called()

    def test_same_origin_settings_and_blocked_destination(self):
        with patch("app.main.scheduler.reschedule"):
            valid = self.client.post(
                "/api/settings", headers={"Origin": "http://testserver"},
                data={"checkin_time": "08:00", "webhook_url": "https://api.day.app/test"},
            )
            invalid = self.client.post(
                "/api/settings", headers={"Origin": "http://testserver"},
                data={"checkin_time": "08:00", "webhook_url": "http://10.10.10.1/test"},
            )
        self.assertEqual(valid.status_code, 200)
        self.assertEqual(invalid.status_code, 400)
        self.assertEqual(db.get_settings()["webhook_url"], "https://api.day.app/test")

    def test_account_response_removes_credentials_and_disables_caching(self):
        db.create_account({"name": "test", "token": "a" * 32, "user_agent": "private-ua", "api_key": "private-key"})
        response = self.client.get("/api/accounts")
        self.assertEqual(response.status_code, 200)
        for field in ("token", "user_agent", "api_key"):
            self.assertNotIn(field, response.json()[0])
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertIn("frame-ancestors 'none'", response.headers["Content-Security-Policy"])


class CredentialTransportTests(unittest.TestCase):
    def test_longhu_refuses_redirect_without_following_it(self):
        session = Mock()
        session.get.return_value.status_code = 302
        with patch.object(longhu.requests, "Session", return_value=session):
            with self.assertRaises(longhu.LonghuError):
                longhu._request("GET", longhu.BASE + "/test", {"token": "fake-test-token"})
        self.assertFalse(session.get.call_args.kwargs["allow_redirects"])
        self.assertFalse(session.trust_env)
        session.close.assert_called_once()

    def test_invalid_response_does_not_echo_raw_sensitive_content(self):
        result = longhu._decode(Mock(text="unexpected response with fake-secret-token"))
        self.assertNotIn("fake-secret-token", result["message"])

    def test_notification_does_not_follow_redirects_or_log_provider_body(self):
        session = Mock()
        session.post.return_value.status_code = 302
        session.post.return_value.json.return_value = {"code": 500, "secret": "fake-secret"}
        with patch.object(db, "get_settings", return_value={"webhook_url": "https://api.day.app/test"}), \
                patch.object(runner.requests, "Session", return_value=session), \
                self.assertLogs("app.runner", level="WARNING") as logs:
            runner.notify("test")
        self.assertFalse(session.post.call_args.kwargs["allow_redirects"])
        self.assertNotIn("fake-secret", " ".join(logs.output))


if __name__ == "__main__":
    unittest.main()
