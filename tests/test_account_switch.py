import base64
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from src import account_switch as a


def auth(user="one", version="initial", workspace="workspace"):
    claims = base64.urlsafe_b64encode(json.dumps({"sub": user}).encode()).decode().rstrip("=")
    return json.dumps({"auth_mode": "chatgpt", "OPENAI_API_KEY": None,
                       "tokens": {"id_token": f"fake.{claims}.test", "account_id": workspace,
                                  "access_token": f"TEST-ONLY-{user}-{version}", "refresh_token": "TEST-ONLY-REFRESH"}}).encode()


class AccountSwitchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.home, self.app = root / "codex", root / "app"
        self.home.mkdir()
        key = "11111111-1111-4111-8111-111111111111"
        self.target_home = self.app / "accounts" / key
        self.target_home.mkdir(parents=True)
        self.old = auth()
        (self.home / "auth.json").write_bytes(self.old)
        (self.target_home / "auth.json").write_bytes(auth("two"))
        (self.home / "config.toml").write_text('cli_auth_credentials_store = "file"\n')
        (self.home / "history.jsonl").write_bytes(b"shared history")
        (self.home / "state.sqlite").write_bytes(b"shared cache")
        self.target = {"id": key, "name": "Second", "home": str(self.target_home), "managed": True}
        self.profiles = [{"id": "current", "name": "当前账号", "home": str(self.home), "managed": False}, self.target]
        self.processes = patch.object(a, "codex_processes", return_value=[]).start()
        self.addCleanup(patch.stopall)

    def switch(self, validate=lambda home: None):
        return a.switch_account(self.target, self.profiles, self.home, self.app, validate)

    def test_switch_preserves_shared_state_and_keeps_original_account(self):
        result = self.switch()
        self.assertEqual((self.home / "auth.json").read_bytes(), auth("two"))
        self.assertEqual((self.home / "history.jsonl").read_bytes(), b"shared history")
        self.assertEqual((self.home / "state.sqlite").read_bytes(), b"shared cache")
        self.assertEqual((self.home / "config.toml").read_text(), 'cli_auth_credentials_store = "file"\n')
        self.assertEqual(len(result), 3)
        self.assertEqual((Path(result[-1]["home"]) / "auth.json").read_bytes(), self.old)
        self.assertNotIn(b"TEST-ONLY", (self.app / "last-switch.dpapi").read_bytes())
        self.assertNotIn(b"TEST-ONLY", (self.app / "profiles.json").read_bytes())
        self.assertEqual(list(self.home.glob(".gauge-*.tmp")), [])

    def test_external_switch_retains_each_observed_account_and_custom_name(self):
        first = a.preserve_local_account(self.profiles, self.home, self.app / "accounts")
        original = first[-1]
        original["name"] = "My original account"
        (self.home / "auth.json").write_bytes(auth("two", "new-login"))
        second = a.preserve_local_account(first, self.home, self.app / "accounts")
        self.assertEqual(len(second), 3)
        self.assertEqual((Path(original["home"]) / "auth.json").read_bytes(), self.old)
        self.assertEqual((self.target_home / "auth.json").read_bytes(), auth("two", "new-login"))
        (self.home / "auth.json").write_bytes(auth("one", "renewed"))
        third = a.preserve_local_account(second, self.home, self.app / "accounts")
        self.assertEqual(len(third), 3)
        self.assertEqual(third[-1]["name"], "My original account")
        self.assertEqual((Path(original["home"]) / "auth.json").read_bytes(), auth("one", "renewed"))

    def test_preserve_rejects_changed_source_before_commit(self):
        atomic = a.atomic_private_write
        def race(path, raw, before_replace=None):
            (self.home / "auth.json").write_bytes(auth("external"))
            atomic(path, raw, before_replace)
        with patch.object(a, "atomic_private_write", side_effect=race), self.assertRaises(a.SwitchError):
            a.preserve_local_account(self.profiles, self.home, self.app / "accounts")
        self.assertEqual((self.home / "auth.json").read_bytes(), auth("external"))
        self.assertEqual((self.target_home / "auth.json").read_bytes(), auth("two"))

    def test_preserve_does_not_overwrite_concurrently_renewed_saved_tokens(self):
        (self.home / "auth.json").write_bytes(auth("two", "shared"))
        atomic = a.atomic_private_write
        def race(path, raw, before_replace=None):
            (self.target_home / "auth.json").write_bytes(auth("two", "concurrent"))
            atomic(path, raw, before_replace)
        with patch.object(a, "atomic_private_write", side_effect=race), self.assertRaises(a.SwitchError):
            a.preserve_local_account(self.profiles, self.home, self.app / "accounts")
        self.assertEqual((self.target_home / "auth.json").read_bytes(), auth("two", "concurrent"))

    def test_unchanged_snapshot_does_not_rewrite_credentials(self):
        result = a.preserve_local_account(self.profiles, self.home, self.app / "accounts")
        with patch.object(a, "atomic_private_write") as write:
            self.assertEqual(a.preserve_local_account(result, self.home, self.app / "accounts"), result)
        write.assert_not_called()

    def test_switch_back_keeps_latest_refresh_and_reuses_existing_profile(self):
        self.profiles = self.switch()
        (self.home / "auth.json").write_bytes(auth("two", "rotated"))
        self.target = self.profiles[-1]

        def refresh(home):
            (Path(home) / "auth.json").write_bytes(auth("one", "refreshed-by-cli"))

        result = self.switch(refresh)
        self.assertEqual(len(result), 3)
        self.assertEqual((self.home / "auth.json").read_bytes(), auth("one", "refreshed-by-cli"))
        self.assertEqual((self.target_home / "auth.json").read_bytes(), auth("two", "rotated"))

    def test_running_codex_blocks_any_changes(self):
        self.processes.return_value = [(1, "Codex.exe")]
        with self.assertRaises(a.SwitchError):
            self.switch()
        self.assertEqual((self.home / "auth.json").read_bytes(), self.old)
        self.assertFalse((self.app / "profiles.json").exists())

    def test_validation_failure_leaves_current_login_intact_and_hides_raw_error(self):
        with self.assertRaises(a.SwitchError) as error:
            self.switch(lambda home: (_ for _ in ()).throw(RuntimeError("secret-test")))
        self.assertNotIn("secret-test", str(error.exception))
        self.assertEqual((self.home / "auth.json").read_bytes(), self.old)
        self.assertFalse((self.app / "profiles.json").exists())

    def test_codex_reopening_before_commit_cancels_switch(self):
        self.processes.side_effect = [[], [], [(1, "Codex.exe")]]
        with self.assertRaises(a.SwitchError):
            self.switch()
        self.assertEqual((self.home / "auth.json").read_bytes(), self.old)

    def test_external_login_change_is_not_overwritten(self):
        external = auth("external")
        with self.assertRaises(a.SwitchError):
            self.switch(lambda home: (self.home / "auth.json").write_bytes(external))
        self.assertEqual((self.home / "auth.json").read_bytes(), external)

    def test_commit_failure_leaves_original_and_cleans_plaintext_staging(self):
        replace = os.replace

        def fail_commit(source, destination):
            if Path(destination) == self.home / "auth.json":
                raise PermissionError("test")
            replace(source, destination)

        with patch.object(a.os, "replace", fail_commit), self.assertRaises(a.SwitchError):
            self.switch()
        self.assertEqual((self.home / "auth.json").read_bytes(), self.old)
        self.assertEqual(list(self.home.glob(".gauge-*.tmp")), [])

    def test_rejects_non_file_storage_and_workspace_restrictions(self):
        for setting in ('cli_auth_credentials_store = "keyring"', 'forced_chatgpt_workspace_id = "other"'):
            (self.home / "config.toml").write_text(setting)
            with self.assertRaises(a.SwitchError):
                self.switch()
            self.assertEqual((self.home / "auth.json").read_bytes(), self.old)

    def test_same_workspace_different_users_are_distinct(self):
        self.assertNotEqual(a.read_auth(self.home).identity, a.read_auth(self.target_home).identity)

    def test_rejects_external_managed_directory(self):
        self.target["home"] = str(self.home)
        with self.assertRaises(a.SwitchError):
            self.switch()
        self.assertEqual((self.home / "auth.json").read_bytes(), self.old)

    def test_backup_protection_failure_happens_before_credential_commit(self):
        with patch.object(a, "protect", side_effect=a.SwitchError("test")), self.assertRaises(a.SwitchError):
            self.switch()
        self.assertEqual((self.home / "auth.json").read_bytes(), self.old)


if __name__ == "__main__":
    unittest.main()
