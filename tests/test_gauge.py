import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src import main as gauge


class ProfileStorageTests(unittest.TestCase):
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
