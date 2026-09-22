import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src import main as gauge
from tests.test_account_switch import auth


class ProfileStorageTests(unittest.TestCase):
    def test_local_accounts_survive_external_switch_logout_and_reload(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            home = root / "codex"
            home.mkdir()
            with patch.multiple(gauge, APP_DIR=root / "app", ACCOUNTS_DIR=root / "app/accounts",
                                WORK_DIR=root / "app/work", PROFILES_FILE=root / "app/profiles.json",
                                DEFAULT_HOME=home), patch.object(gauge.claude_provider, "DEFAULT_HOME", root / "claude"):
                (home / "auth.json").write_bytes(auth("one"))
                first = gauge.load_profiles()
                original = next(p for p in first if p.managed)
                original.name = "Original"
                gauge.save_profiles(first)
                (home / "auth.json").write_bytes(auth("two"))
                second = gauge.load_profiles()
                self.assertEqual(len([p for p in second if p.managed]), 2)
                (home / "auth.json").unlink()
                after_logout = gauge.load_profiles()
                gauge.save_profiles(after_logout)
                after_restart = gauge.load_profiles()
                self.assertEqual(len(after_restart), 2)
                self.assertEqual(next(p.name for p in after_restart if p.id == original.id), "Original")
                self.assertEqual((Path(original.home) / "auth.json").read_bytes(), auth("one"))
                contents = gauge.PROFILES_FILE.read_text()
                self.assertNotIn("TEST-ONLY", contents)
    def test_fresh_install_uses_gauge_data_directory(self):
        with tempfile.TemporaryDirectory() as root, patch.dict(os.environ, {"LOCALAPPDATA": root}):
            self.assertEqual(gauge.app_data_directory(), Path(root) / "Gauge")

    def test_upgrade_reuses_legacy_store_without_changing_it(self):
        with tempfile.TemporaryDirectory() as root, patch.dict(os.environ, {"LOCALAPPDATA": root}):
            legacy = Path(root) / "SafeQuota"
            legacy.mkdir()
            marker = legacy / "profiles.json"
            marker.write_text('[]', encoding="utf-8")
            (Path(root) / "Gauge").mkdir()
            self.assertEqual(gauge.app_data_directory(), legacy)
            self.assertEqual(marker.read_text(encoding="utf-8"), '[]')

    def test_only_metadata_is_saved(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "profiles.json"
            with patch.multiple(
                gauge,
                APP_DIR=Path(root),
                PROFILES_FILE=path,
                ACCOUNTS_DIR=Path(root) / "accounts",
                WORK_DIR=Path(root) / "empty-workdir",
                DEFAULT_HOME=Path(root) / "codex",
            ), patch.object(gauge.claude_provider, "DEFAULT_HOME", Path(root) / "claude"), patch.object(gauge.account_switch, "identity_or_none", return_value="test"):
                gauge.save_profiles([gauge.Profile("current", "当前账号", root, False)])
                contents = path.read_text(encoding="utf-8")
                self.assertNotIn("access_token", contents)
                self.assertNotIn("refresh_token", contents)
                self.assertEqual(len(gauge.load_profiles()), 1)


if __name__ == "__main__":
    unittest.main()
