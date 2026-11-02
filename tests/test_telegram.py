import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import requests
from fastapi.testclient import TestClient

from app import db, longhu, runner, scheduler, telegram
from app.main import app


FAKE_TOKEN = "123456:" + "test_credential_" * 3
FAKE_CHAT = "-100123456789"


class TelegramTransportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.config_path = Path(self.temp.name) / "telegram.json"
        self.env_patch = patch.dict(os.environ, {"TELEGRAM_CONFIG_FILE": str(self.config_path)})
        self.env_patch.start()
        self.config = {"enabled": True, "bot_token": FAKE_TOKEN, "chat_id": FAKE_CHAT}
        self.config_path.write_text(json.dumps(self.config), encoding="utf-8")
        self.session = Mock()
        self.session.post.return_value.status_code = 200
        self.session.post.return_value.json.return_value = {"ok": True}
        self.session_patch = patch.object(telegram.requests, "Session", return_value=self.session)
        self.session_patch.start()

    def tearDown(self):
        self.session_patch.stop()
        self.env_patch.stop()
        self.temp.cleanup()

    def test_status_exposes_only_flags(self):
        self.assertEqual(telegram.status(), {"configured": True, "enabled": True})

    def test_missing_config_and_disabled_config_make_no_request(self):
        self.config_path.unlink()
        self.assertEqual(telegram.status(), {"configured": False, "enabled": False})
        self.assertFalse(telegram.send_message("test"))
        self.config["enabled"] = False
        self.config_path.write_text(json.dumps(self.config), encoding="utf-8")
        self.assertEqual(telegram.status(), {"configured": True, "enabled": False})
        self.assertFalse(telegram.send_message("test"))
        self.session.post.assert_not_called()

    def test_bad_config_fails_safely_without_logging_contents(self):
        for contents in ("invalid " + FAKE_TOKEN, "[]", json.dumps({**self.config, "bot_token": []}),
                         json.dumps({**self.config, "chat_id": {"secret": FAKE_TOKEN}})):
            with self.subTest(contents=contents):
                self.config_path.write_text(contents, encoding="utf-8")
                with self.assertLogs("app.telegram", level="WARNING") as logs:
                    self.assertFalse(telegram.send_message("test"))
                self.assertNotIn(FAKE_TOKEN, " ".join(logs.output))
        self.session.post.assert_not_called()

    def test_sends_only_to_telegram_without_proxy_redirect_or_credentials_in_text(self):
        self.assertTrue(telegram.send_message("test " + FAKE_TOKEN + " " + "a" * 32))
        call = self.session.post.call_args
        self.assertEqual(call.args, (f"https://api.telegram.org/bot{FAKE_TOKEN}/sendMessage",))
        self.assertEqual(call.kwargs["json"]["chat_id"], FAKE_CHAT)
        self.assertNotIn(FAKE_TOKEN, call.kwargs["json"]["text"])
        self.assertNotIn("a" * 32, call.kwargs["json"]["text"])
        self.assertFalse(call.kwargs["allow_redirects"])
        self.assertEqual(call.kwargs["timeout"], (5, 15))
        self.assertFalse(self.session.trust_env)
        self.assertNotIn("parse_mode", call.kwargs["json"])
        self.session.close.assert_called_once()

    def test_rejected_response_is_not_retried_or_logged(self):
        for status, payload in ((200, {"ok": False, "description": FAKE_TOKEN}),
                                (302, {"ok": True}), (500, {"ok": True}), (200, [])):
            with self.subTest(status=status, payload=payload):
                self.session.reset_mock()
                self.session.post.return_value.status_code = status
                self.session.post.return_value.json.return_value = payload
                with self.assertLogs("app.telegram", level="WARNING") as logs:
                    self.assertFalse(telegram.send_message("test"))
                self.assertNotIn(FAKE_TOKEN, " ".join(logs.output))
                self.session.post.assert_called_once()
                self.session.close.assert_called_once()

    def test_bad_json_and_network_failure_do_not_log_secret(self):
        self.session.post.return_value.json.side_effect = ValueError(FAKE_TOKEN)
        with self.assertLogs("app.telegram", level="WARNING") as logs:
            self.assertFalse(telegram.send_message("test"))
        self.assertNotIn(FAKE_TOKEN, " ".join(logs.output))
        self.session.reset_mock()
        self.session.post.side_effect = requests.Timeout("failed URL /bot" + FAKE_TOKEN)
        with self.assertLogs("app.telegram", level="WARNING") as logs:
            self.assertFalse(telegram.send_message("test"))
        self.assertNotIn(FAKE_TOKEN, " ".join(logs.output))
        self.session.post.assert_called_once()

    def test_long_unicode_messages_split_and_preserve_content(self):
        message = "龙湖😀\n" * 1800
        self.assertTrue(telegram.send_message(message))
        chunks = [call.kwargs["json"]["text"] for call in self.session.post.call_args_list]
        self.assertGreater(len(chunks), 1)
        self.assertEqual("".join(chunks), message)
        self.assertTrue(all(len(chunk.encode("utf-16-le")) // 2 <= 3900 for chunk in chunks))

    def test_empty_message_does_not_send(self):
        self.assertFalse(telegram.send_message("  \n"))
        self.session.post.assert_not_called()


class SummaryTests(unittest.TestCase):
    def result(self, **changes):
        return {"account_id": 1, "name": "测试账号", "refreshed": True,
                "checkin_status": "ok", "checkin_message": "签到成功，获得 1 积分",
                "growth_before": 5, "profile": {"growth_value": 6, "balance": 0.5, "level_name": "V1"},
                "days_left": 30, "error": "", **changes}

    def summary(self, results, **kwargs):
        with patch.object(runner, "notify") as notify:
            runner.daily_summary(results, **kwargs)
        notify.assert_called_once()
        return notify.call_args.args[0]

    def test_success_summary_includes_expected_values(self):
        text = self.summary([self.result()])
        for value in ("每日自动", "家里 common", "北京时间", "成功 1", "异常账号：0",
                      "测试账号", "5 → 6（+1）", "珑珠余额：0.5", "等级：V1", "续期：成功", "剩余：30 天"):
            self.assertIn(value, text)

    def test_already_signed_still_notifies(self):
        text = self.summary([self.result(checkin_status="already", checkin_message="今日已签到")])
        self.assertIn("已签 1", text)
        self.assertIn("今日已签到", text)

    def test_refresh_or_query_failure_notifies_even_if_checkin_succeeds(self):
        for result in (self.result(refreshed=False, error="续期失败"),
                       self.result(profile={}, error="资料查询失败")):
            with self.subTest(result=result):
                text = self.summary([result])
                self.assertIn("成功 1", text)
                self.assertIn("异常账号：1", text)
                self.assertIn(result["error"], text)

    def test_multiple_accounts_use_one_summary_including_failures(self):
        text = self.summary([self.result(), self.result(account_id=2, name="第二账号",
                            checkin_status="fail", checkin_message="凭据失效", error="凭据失效")])
        self.assertIn("失败 1", text)
        self.assertIn("第二账号", text)
        self.assertIn("凭据失效", text)

    def test_empty_batch_does_not_notify(self):
        with patch.object(runner, "notify") as notify:
            runner.daily_summary([])
        notify.assert_not_called()

    def test_failed_telegram_does_not_prevent_optional_webhook(self):
        with patch.object(telegram, "send_message", return_value=False) as send, \
                patch.object(runner, "_notify_webhook") as webhook:
            runner.notify("error " + "x" * 32)
        send.assert_called_once()
        webhook.assert_called_once_with(send.call_args.args[0])
        self.assertNotIn("x" * 32, send.call_args.args[0])

    def test_known_token_forms_are_redacted(self):
        jwt = "eyJhbGciOiJIUzI1NiJ9.eyJ1c2VyIjoiMSJ9.signature-value"
        text = telegram.redact("test " + FAKE_TOKEN + " " + "a" * 32 + " " + jwt)
        for secret in (FAKE_TOKEN, "a" * 32, jwt):
            self.assertNotIn(secret, text)

    def test_run_account_does_not_duplicate_failure_notification(self):
        account = {"id": 1, "name": "测试账号", "token": "test", "growth_value": 5}
        with patch.object(longhu, "refresh_token", return_value=(False, "续期失败")), \
                patch.object(longhu, "checkin", return_value=("fail", "凭据失效")), \
                patch.object(longhu, "fetch_profile", side_effect=longhu.LonghuError("资料失败")), \
                patch.object(db, "add_log"), patch.object(db, "update_account"), \
                patch.object(db, "get_account", return_value={}), patch.object(runner, "notify") as notify:
            result = runner.run_account(account)
        self.assertEqual(result["checkin_status"], "fail")
        self.assertEqual(result["growth_before"], 5)
        notify.assert_not_called()

    def test_scheduler_notifies_generic_error_without_raw_exception(self):
        with patch.object(runner, "run_all", side_effect=RuntimeError(FAKE_TOKEN)), \
                patch.object(runner, "notify") as notify, patch.object(db, "save_settings") as save, \
                patch.object(scheduler.logger, "exception"):
            scheduler._job()
        notify.assert_called_once()
        self.assertIn("RuntimeError", notify.call_args.args[0])
        self.assertNotIn(FAKE_TOKEN, notify.call_args.args[0])
        save.assert_called_once()


class NotificationEndpointTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)

    def tearDown(self):
        self.client.close()

    def test_manual_run_notifies_once(self):
        result = {"account_id": 1, "checkin_status": "already"}
        account = {"id": 1, "name": "测试账号"}
        with patch.object(db, "get_account", return_value=account), \
                patch.object(runner, "run_account", return_value=result) as run, \
                patch.object(runner, "daily_summary") as summary:
            response = self.client.post("/api/accounts/1/run")
        self.assertEqual(response.status_code, 200)
        run.assert_called_once_with(account, do_checkin=True)
        summary.assert_called_once_with([result], source="手动执行")

    def test_manual_unexpected_failure_sends_generic_alert(self):
        with patch.object(db, "get_account", return_value={"id": 1}), \
                patch.object(runner, "run_account", side_effect=RuntimeError(FAKE_TOKEN)), \
                patch.object(runner, "notify") as notify, patch("app.main.logger.exception"):
            response = self.client.post("/api/accounts/1/run")
        self.assertEqual(response.status_code, 500)
        notify.assert_called_once()
        self.assertNotIn(FAKE_TOKEN, notify.call_args.args[0])
        self.assertNotIn(FAKE_TOKEN, response.text)

    def test_settings_endpoint_exposes_only_telegram_flags(self):
        with patch.object(db, "get_settings", return_value={}), \
                patch.object(telegram, "_config", return_value={"enabled": True, "bot_token": FAKE_TOKEN, "chat_id": FAKE_CHAT}):
            response = self.client.get("/api/settings")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["telegram"], {"configured": True, "enabled": True})
        self.assertNotIn(FAKE_TOKEN, response.text)
        self.assertNotIn(FAKE_CHAT, response.text)


if __name__ == "__main__":
    unittest.main()
