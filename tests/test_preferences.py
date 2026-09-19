import json
import os
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from src import preferences
from src import main as gauge
from src.i18n import set_language, tr


class PreferenceTests(unittest.TestCase):
    def test_finds_bundled_cli_without_terminal_path(self):
        with tempfile.TemporaryDirectory() as folder:
            binary = Path(folder) / "OpenAI" / "Codex" / "bin" / "version" / "codex.exe"
            binary.parent.mkdir(parents=True)
            binary.touch()
            with patch.dict(os.environ, {"LOCALAPPDATA": folder}), patch.object(gauge.shutil, "which", return_value=None):
                self.assertEqual(gauge.codex_binary(), str(binary))

    def test_invalid_file_and_values_use_safe_defaults(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "settings.json"
            for payload in ("broken", "[]", '{"theme": [], "accent": {}, "opacity": true, "topmost": "false", "refresh_minutes": -1}'):
                path.write_text(payload)
                self.assertEqual(preferences.Preferences.load(path), preferences.Preferences())

    def test_round_trip_and_opacity_bounds(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "settings.json"
            expected = preferences.Preferences("light", "purple", 80, False, 10)
            expected.save(path)
            self.assertEqual(preferences.Preferences.load(path), expected)
            path.write_text('{"opacity": 0}')
            self.assertEqual(preferences.Preferences.load(path).opacity, 70)

    def test_startup_quotes_executable_path_with_spaces(self):
        with patch.object(preferences.sys, "frozen", True, create=True), patch.object(preferences.sys, "executable", r"C:\My Apps\Gauge.exe"):
            self.assertEqual(preferences.startup_command(), '"C:\\My Apps\\Gauge.exe"')

    def test_source_startup_uses_root_launcher_after_package_move(self):
        import subprocess
        with patch.object(preferences.sys, "frozen", False, create=True):
            executable = Path(preferences.sys.executable).resolve()
            windowed = executable.with_name("pythonw.exe")
            launcher = Path(__file__).resolve().parents[1] / "gauge.py"
            expected = subprocess.list2cmdline([str(windowed if windowed.exists() else executable), str(launcher)])
            self.assertEqual(preferences.startup_command(), expected)
            self.assertTrue(launcher.is_file())

    def test_language_validation_and_old_preferences(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "settings.json"
            for value in (None, [], "invalid"):
                path.write_text(json.dumps({"language": value}))
                self.assertEqual(preferences.Preferences.load(path).language, "zh")
            path.write_text('{}')
            self.assertEqual(preferences.Preferences.load(path).language, "zh")

    def test_registry_change_targets_only_current_user_named_value(self):
        import winreg
        with patch.object(winreg, "CreateKeyEx") as create, patch.object(winreg, "SetValueEx") as put, patch.object(winreg, "DeleteValue") as delete:
            key = create.return_value.__enter__.return_value
            preferences.write_startup_entry('"C:\\My Apps\\Gauge.exe"')
            create.assert_called_with(winreg.HKEY_CURRENT_USER, preferences.RUN_KEY, 0, winreg.KEY_SET_VALUE)
            put.assert_called_once_with(key, "Gauge", 0, winreg.REG_SZ, '"C:\\My Apps\\Gauge.exe"')
            delete.assert_called_once_with(key, "SafeQuota")
            delete.reset_mock()
            preferences.write_startup_entry(None)
            self.assertEqual([call.args for call in delete.call_args_list], [(key, "Gauge"), (key, "SafeQuota")])

    def test_startup_reads_legacy_entry_when_new_entry_is_absent(self):
        import winreg
        with patch.object(winreg, "OpenKey"), patch.object(
            winreg, "QueryValueEx", side_effect=[FileNotFoundError(), ('old-command', winreg.REG_SZ)]
        ) as query:
            self.assertEqual(preferences.startup_entry(), 'old-command')
            self.assertEqual([call.args[1] for call in query.call_args_list], ["Gauge", "SafeQuota"])

    def test_failed_startup_write_does_not_remove_legacy_entry(self):
        import winreg
        with patch.object(winreg, "CreateKeyEx"), patch.object(
            winreg, "SetValueEx", side_effect=OSError("denied")
        ), patch.object(winreg, "DeleteValue") as delete:
            with self.assertRaises(OSError):
                preferences.write_startup_entry("new-command")
            delete.assert_not_called()


class SettingsPageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.stack = ExitStack()
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        for target, value in (
            ("APP_DIR", self.root), ("WINDOW_FILE", self.root / "window.json"),
            ("load_profiles", lambda: [gauge.Profile("current", "测试", "unused", False)]),
            ("save_profiles", lambda profiles: None), ("startup_entry", lambda: None),
        ):
            self.stack.enter_context(patch.object(gauge, target, value))
        self.stack.enter_context(patch.object(gauge.Monitor, "_build_tray", lambda self: None))
        self.stack.enter_context(patch.object(gauge.Monitor, "refresh_all", lambda self: None))
        self.window = gauge.Monitor()
        self.window.show()
        self.window.show_settings()
        self.page = self.window.settings_page
        self.app.processEvents()

    def tearDown(self):
        set_language("zh")
        self.window._window_size_ready = False
        self.window._size_timer.stop()
        self.window.poll_timer.stop()
        self.window.close()
        self.window.deleteLater()
        self.app.processEvents()
        self.stack.close()

    def test_language_save_updates_ui_persists_and_switches_back(self):
        self.page.language.setCurrentIndex(self.page.language.findData("en"))
        self.page.save()
        self.assertEqual(self.window.refresh_button.text(), "Refresh")
        self.assertEqual(self.window.empty_add_button.text(), "+ Add account")
        self.assertEqual(self.window.cards["current"].name.text(), "测试")
        self.assertEqual(preferences.Preferences.load(self.root / "settings.json").language, "en")
        self.assertEqual(tr("Claude 授权已过期，请重新授权。"), "Claude authorization expired. Sign in again.")
        self.window.show_settings()
        page = self.window.settings_page
        self.assertEqual(page.save_button.text(), "Save")
        self.assertEqual(page.language.currentData(), "en")
        page.language.setCurrentIndex(page.language.findData("zh"))
        page.save()
        self.assertEqual(self.window.refresh_button.text(), "刷新")
        self.assertEqual(self.window.preferences.language, "zh")

    def test_unsaved_language_is_discarded(self):
        self.page.language.setCurrentIndex(self.page.language.findData("en"))
        self.page.back_button.click()
        self.assertEqual(self.window.preferences.language, "zh")
        self.assertEqual(self.window.refresh_button.text(), "刷新")
        self.assertFalse((self.root / "settings.json").exists())

    def test_save_updates_theme_timer_flags_and_preserves_window(self):
        self.window.setGeometry(120, 130, 420, 360)
        self.page.theme.setCurrentIndex(self.page.theme.findData("light"))
        self.page.interval.setCurrentIndex(self.page.interval.findData(10))
        self.page.topmost.setChecked(False)
        with patch.object(gauge, "write_startup_entry") as startup:
            self.page.save()
            startup.assert_not_called()
        self.assertIs(self.window.pages.currentWidget(), self.window.monitor_page)
        self.assertEqual(self.window.preferences.theme, "light")
        self.assertEqual(self.window.poll_timer.interval(), 600000)
        self.assertFalse(self.window.windowFlags() & Qt.WindowStaysOnTopHint)
        self.assertEqual((self.window.width(), self.window.height()), (420, 360))
        self.assertEqual(preferences.Preferences.load(self.root / "settings.json"), self.window.preferences)

    def test_cancel_preview_does_not_save_or_change_owner(self):
        self.page.theme.setCurrentIndex(self.page.theme.findData("light"))
        self.page.opacity.setValue(75)
        self.page.back_button.click()
        self.assertEqual(self.window.preferences.theme, "night")
        self.assertAlmostEqual(self.window.windowOpacity(), 1.0, places=2)
        self.assertIs(self.window.pages.currentWidget(), self.window.monitor_page)
        self.assertFalse((self.root / "settings.json").exists())

    def test_settings_remain_inside_resizable_window_and_reopen_cleanly(self):
        self.window.resize(280, 170)
        self.app.processEvents()
        self.assertEqual((self.window.width(), self.window.height()), (280, 170))
        self.assertFalse(self.page.isWindow())
        self.assertIs(self.page.window(), self.window)
        self.assertGreater(self.page.scroll.verticalScrollBar().maximum(), 0)
        self.assertTrue(self.page.back_button.isVisible())
        self.assertTrue(self.page.save_button.isVisible())
        self.window.show_settings()
        self.assertIs(self.window.settings_page, self.page)
        self.page.theme.setCurrentIndex(self.page.theme.findData("light"))
        self.page.back_button.click()
        self.window.show_settings()
        self.assertEqual(self.window.settings_page.theme.currentData(), "night")
        self.assertEqual(self.window.pages.count(), 2)

    def test_failed_save_rolls_back_startup_and_keeps_page_open(self):
        self.page.autostart.setChecked(True)
        with patch.object(gauge, "write_startup_entry") as startup, patch.object(preferences.Preferences, "save", side_effect=OSError("failure")):
            self.page.save()
            self.assertEqual(startup.call_count, 2)
            self.assertIsNone(startup.call_args.args[0])
        self.assertIs(self.window.pages.currentWidget(), self.page)
        self.assertEqual(self.window.preferences, preferences.Preferences())


if __name__ == "__main__":
    unittest.main()
