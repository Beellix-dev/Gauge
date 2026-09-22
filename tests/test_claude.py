from contextlib import ExitStack
import io
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
import urllib.error
from unittest.mock import Mock, patch

from src import claude_provider as c
from src import main as s
from tests import test_login


def credential_bytes(expired=False):
    return json.dumps({"other": {"keep": True}, "claudeAiOauth": {
        "accessToken": "TEST-ONLY-ACCESS", "refreshToken": "TEST-ONLY-REFRESH",
        "expiresAt": 1 if expired else (time.time() + 3600) * 1000,
        "scopes": ["user:profile", "user:inference"], "subscriptionType": "pro",
    }}).encode()


USAGE = {"five_hour": {"utilization": 30, "resets_at": "2026-09-19T20:00:00Z"},
         "seven_day": {"utilization": 9, "resets_at": "2026-09-25T20:00:00+00:00"}}


class ClaudeTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.home = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.file = self.home / ".credentials.json"
        self.file.write_bytes(credential_bytes())
        (self.home / ".claude.json").write_text(json.dumps({"oauthAccount": {"accountUuid": "TEST-USER", "organizationUuid": "TEST-ORG"}}))
        c._cache.clear()
        c._retry_after.clear()
        c._last_token.clear()

    def test_usage_converts_remaining_and_caches_without_extra_requests(self):
        with patch.object(c, "_request", return_value=USAGE) as request:
            quota = s.read_claude_quota(str(self.home))
            self.assertEqual(s.remaining(quota.five), "70%")
            self.assertEqual(s.remaining(quota.week), "91%")
            self.assertEqual(s.read_claude_quota(str(self.home)), quota)
            request.assert_called_once_with(c.USAGE_URL, token="TEST-ONLY-ACCESS")

    def test_missing_window_does_not_become_zero_or_full(self):
        with patch.object(c, "_request", return_value={"five_hour": None, "seven_day": USAGE["seven_day"]}):
            self.assertIsNone(s.read_claude_quota(str(self.home)).five)
        for value in [float("nan"), float("inf"), True, -1, 101, "12"]:
            with self.assertRaises(c.ClaudeError):
                c._window({"utilization": value}, 300)

    def test_refresh_preserves_other_fields_and_saves_before_using_new_token(self):
        self.file.write_bytes(credential_bytes(expired=True))

        def request(url, **kwargs):
            if url == c.TOKEN_URL:
                self.assertEqual(kwargs["payload"]["client_id"], c.CLIENT_ID)
                self.assertTrue((self.home / ".oauth_refresh.lock").is_dir())
                return {"access_token": "TEST-ONLY-NEW", "refresh_token": "TEST-ONLY-ROTATED", "expires_in": 3600}
            document = json.loads(self.file.read_bytes())
            self.assertEqual(document["other"], {"keep": True})
            self.assertEqual(document["claudeAiOauth"]["refreshToken"], "TEST-ONLY-ROTATED")
            self.assertEqual(kwargs["token"], "TEST-ONLY-NEW")
            return USAGE

        with patch.object(c, "_request", side_effect=request):
            c.read_usage(str(self.home))
        self.assertFalse((self.home / ".oauth_refresh.lock").exists())
        self.assertEqual(list(self.home.glob(".gauge-*.tmp")), [])

    def test_concurrent_authorization_is_not_overwritten(self):
        self.file.write_bytes(credential_bytes(expired=True))
        changed = credential_bytes()

        def refresh(*args, **kwargs):
            self.file.write_bytes(changed)
            return {"access_token": "TEST-ONLY-NEW", "expires_in": 3600}

        with patch.object(c, "_request", side_effect=refresh), self.assertRaises(c.ClaudeError):
            c.read_usage(str(self.home))
        self.assertEqual(self.file.read_bytes(), changed)

    def test_cli_refresh_lock_is_respected(self):
        self.file.write_bytes(credential_bytes(expired=True))
        (self.home / ".oauth_refresh.lock").mkdir()
        with patch.object(c, "_request") as request, self.assertRaisesRegex(c.ClaudeError, "正在刷新"):
            c.read_usage(str(self.home))
        request.assert_not_called()
        self.assertTrue((self.home / ".oauth_refresh.lock").exists())

    def test_abandoned_empty_refresh_lock_can_be_recovered(self):
        lock = self.home / ".oauth_refresh.lock"
        lock.mkdir()
        old = time.time() - 120
        os.utime(lock, (old, old))
        with c._refresh_lock(self.home) as check:
            check()
            self.assertTrue(lock.is_dir())
        self.assertFalse(lock.exists())

    def test_rate_limit_backs_off(self):
        with patch.object(c, "_request", side_effect=c.RateLimited(600)) as request:
            for _ in range(2):
                with self.assertRaises(c.ClaudeError):
                    c.read_usage(str(self.home))
            request.assert_called_once()

    def test_new_login_clears_previous_authorization_cooldown(self):
        with patch.object(c, "_request", side_effect=c.ClaudeError("expired")):
            with self.assertRaises(c.ClaudeError):
                c.read_usage(str(self.home))
        document = json.loads(self.file.read_bytes())
        document["claudeAiOauth"]["accessToken"] = "TEST-ONLY-NEW-LOGIN"
        self.file.write_text(json.dumps(document))
        with patch.object(c, "_request", return_value=USAGE) as request:
            c.read_usage(str(self.home))
            request.assert_called_once()

    def test_request_rejects_unapproved_hosts_and_redirects(self):
        with self.assertRaises(c.ClaudeError):
            c._request("https://example.com", token="TEST-ONLY-ACCESS")
        handler = c._NoRedirect()
        self.assertIsNone(handler.redirect_request(None, None, 302, None, {}, "https://example.com"))

    def test_http_errors_never_expose_response_tokens(self):
        error = urllib.error.HTTPError(c.USAGE_URL, 401, "TEST-ONLY-SECRET", {}, io.BytesIO(b"TEST-ONLY-SECRET"))
        opener = Mock()
        opener.open.side_effect = error
        with patch.object(c.urllib.request, "build_opener", return_value=opener):
            with self.assertRaises(c.ClaudeError) as raised:
                c._request(c.USAGE_URL, token="TEST-ONLY-ACCESS")
        self.assertNotIn("TEST-ONLY", str(raised.exception))

    def test_identity_is_stable_after_refresh_and_provider_env_is_isolated(self):
        before = c.identity(self.home)
        self.assertIsNotNone(before)
        document = json.loads(self.file.read_bytes())
        document["claudeAiOauth"]["accessToken"] = "TEST-ONLY-ROTATED"
        self.file.write_text(json.dumps(document))
        self.assertEqual(c.identity(self.home), before)
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "TEST-ONLY", "CLAUDE_CODE_USE_BEDROCK": "1"}):
            env = c.login_environment(self.home)
        self.assertNotIn("ANTHROPIC_API_KEY", env)
        self.assertNotIn("CLAUDE_CODE_USE_BEDROCK", env)
        self.assertEqual(env["CLAUDE_CONFIG_DIR"], str(self.home))

    def test_profiles_migrate_codex_and_detect_current_claude(self):
        metadata = self.home / "profiles.json"
        metadata.write_text(json.dumps({"profiles": [{"id": "current", "name": "Codex", "home": str(self.home / "codex"), "managed": False}]}))
        with patch.multiple(s, APP_DIR=self.home, DEFAULT_HOME=self.home / "codex", ACCOUNTS_DIR=self.home / "accounts", WORK_DIR=self.home / "work", PROFILES_FILE=metadata), patch.object(c, "DEFAULT_HOME", self.home), patch.object(s.account_switch, "identity_or_none", return_value="test"):
            profiles = s.load_profiles()
            self.assertEqual([p.provider for p in profiles], ["codex", "claude"])
            s.save_profiles(profiles)
            self.assertEqual(s.load_profiles(), profiles)
        self.assertNotIn("TEST-ONLY", metadata.read_text())

    def test_expired_local_claude_is_not_an_automatic_placeholder(self):
        self.file.write_bytes(credential_bytes(expired=True))
        metadata = self.home / "profiles.json"
        metadata.write_text(json.dumps({"profiles": [
            {"id": "claude-current", "name": "Claude", "home": str(self.home), "managed": False, "provider": "claude"},
            {"id": "saved", "name": "User-added", "home": str(self.home), "managed": True, "provider": "claude"},
        ]}))
        before = self.file.read_bytes()
        with patch.multiple(s, APP_DIR=self.home, DEFAULT_HOME=self.home / "codex", ACCOUNTS_DIR=self.home / "accounts", WORK_DIR=self.home / "work", PROFILES_FILE=metadata), patch.object(c, "DEFAULT_HOME", self.home), patch.object(s.account_switch, "identity_or_none", return_value=None):
            profiles = s.load_profiles()
        self.assertEqual([p.id for p in profiles], ["saved"])
        self.assertEqual(self.file.read_bytes(), before)

    def test_no_local_login_means_no_default_accounts(self):
        with patch.multiple(s, APP_DIR=self.home, DEFAULT_HOME=self.home / "codex", ACCOUNTS_DIR=self.home / "accounts", WORK_DIR=self.home / "work", PROFILES_FILE=self.home / "profiles.json"), patch.object(c, "DEFAULT_HOME", self.home / "absent"), patch.object(s.account_switch, "identity_or_none", return_value=None):
            self.assertEqual(s.load_profiles(), [])

    def test_claude_login_uses_official_cli_in_separate_home(self):
        key = "11111111-1111-4111-8111-111111111111"
        home = self.home / "accounts" / key
        process = Mock()
        process.poll.return_value = 0
        process.returncode = 0

        def launch(command, **kwargs):
            self.assertEqual(command, ["test-claude.exe", "auth", "login", "--claudeai"])
            self.assertEqual(kwargs["env"]["CLAUDE_CONFIG_DIR"], str(home))
            (home / ".credentials.json").write_bytes(credential_bytes())
            return process

        with patch.object(s, "ACCOUNTS_DIR", self.home / "accounts"), patch.object(c, "binary", return_value="test-claude.exe"), patch.object(s.subprocess, "Popen", side_effect=launch):
            result = s.login_new_account(key, threading.Event(), time.monotonic() + 60, "claude")
        self.assertEqual(result.provider, "claude")
        self.assertFalse((home / "config.toml").exists())


class ClaudeIntegrationTests(unittest.TestCase):
    def test_mixed_accounts_have_separate_queries_lights_and_quota_rows(self):
        test_login.LoginUiTests.setUpClass()
        harness = test_login.LoginUiTests()
        harness.setUp()
        try:
            window = harness.window
            window.profiles = [s.Profile("current", "Codex", "codex", False),
                               s.Profile("claude-current", "Claude", "claude", False, "claude"),
                               s.Profile("other", "Claude 2", "other", True, "claude")]
            with patch.object(c, "identity", side_effect=lambda home: "other" if str(home) == "other" else "same"), patch.object(s.account_switch, "identity_or_none", return_value="same"), patch.object(s, "load_profiles", side_effect=lambda: window.profiles), patch.object(c, "local_login_available", return_value=True):
                window._build_cards()
                # Restore the real implementation; the UI harness disables automatic queries.
                REAL_REFRESH(window)
            tasks = {call.args[0].key: call.args[0] for call in window.pool.start.call_args_list}
            self.assertEqual(set(tasks), {"current", "claude-current", "other"})
            self.assertIs(tasks["current"].action, s.read_quota)
            self.assertIs(tasks["claude-current"].action, s.read_claude_quota)
            self.assertEqual(len(window.cards["current"].rows), 1)
            self.assertEqual(len(window.cards["claude-current"].rows), 2)
            self.assertIn("正在使用", window.cards["claude-current"].active_light.toolTip())
            self.assertIn("未用于", window.cards["other"].active_light.toolTip())
            window._task_done("claude-current", s.Quota(s.Window(30, 300, None), s.Window(9, 10080, None), time.time()), "")
            self.assertEqual(window.cards["claude-current"].rows[0][0].text(), "70%")
        finally:
            harness.tearDown()


REAL_REFRESH = s.Monitor.refresh_all
