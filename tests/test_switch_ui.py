import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from contextlib import ExitStack
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from PySide6.QtWidgets import QApplication
from src import main as s
from tests.test_account_switch import auth


class SwitchUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.stack = ExitStack()
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.default = self.root / "codex"
        self.managed = self.root / "account"
        self.default.mkdir()
        self.managed.mkdir()
        (self.default / "auth.json").write_bytes(auth())
        (self.managed / "auth.json").write_bytes(auth("two"))
        self.profiles = [s.Profile("current", "当前账号", str(self.default), False), s.Profile("target", "备用账号", str(self.managed), True)]
        for target, value in (("APP_DIR", self.root), ("WINDOW_FILE", self.root / "window.json"),
                              ("load_profiles", lambda: self.profiles), ("save_profiles", lambda profiles: None)):
            self.stack.enter_context(patch.object(s, target, value))
        self.stack.enter_context(patch.object(s.Monitor, "_build_tray", lambda self: None))
        self.processes = self.stack.enter_context(patch.object(s.account_switch, "codex_processes", return_value=[(1, "running")]))
        self.stack.enter_context(patch.object(s.account_switch, "desktop_path", return_value=None))
        self.window = s.Monitor()
        self.window.pool = Mock()
        self.window.show()
        self.app.processEvents()

    def tearDown(self):
        if self.window.switch_page:
            self.window.switch_page.timer.stop()
        self.window._window_size_ready = False
        self.window._size_timer.stop()
        self.window.poll_timer.stop()
        self.window.close()
        self.window.deleteLater()
        self.app.processEvents()
        self.stack.close()

    def test_switch_page_is_inline_and_wait_can_be_cancelled_without_work(self):
        self.window.show_switch(self.profiles[1])
        page = self.window.switch_page
        self.assertIs(page.window(), self.window)
        page.start()
        page.check()
        self.window.refresh_all()
        self.window.pool.start.assert_not_called()
        self.assertTrue(page.waiting)
        page.back.click()
        self.assertIsNone(self.window.switch_page)
        self.assertFalse(page.timer.isActive())
        tasks = [call.args[0] for call in self.window.pool.start.call_args_list]
        self.assertTrue(all(task.action == s.read_quota for task in tasks))

    def test_switch_waits_for_readers_and_two_quiet_checks(self):
        self.window.show_switch(self.profiles[1])
        page = self.window.switch_page
        self.processes.return_value = []
        self.window.running.add("current")
        page.start()
        self.window.pool.start.assert_not_called()
        self.window.running.clear()
        page.check()
        self.window.pool.start.assert_not_called()
        page.check()
        task = self.window.pool.start.call_args.args[0]
        self.assertEqual(task.key, "switch")
        self.assertFalse(page.back.isEnabled())
        self.assertFalse(page.timer.isActive())
        self.window.cancel_switch()
        self.assertIs(self.window.switch_page, page)

    def test_active_account_reads_shared_home_and_duplicate_is_not_queried(self):
        (self.managed / "auth.json").write_bytes(auth())
        self.window.refresh_all()
        self.assertEqual(self.window.active_id, "target")
        self.assertNotIn("current", self.window.cards)
        self.window.pool.start.assert_called_once()
        task = self.window.pool.start.call_args.args[0]
        self.assertEqual(task.args, (str(self.default),))
        self.assertEqual(task.key, "target")

    def test_duplicate_saved_accounts_share_one_query_result(self):
        self.window.profiles.append(s.Profile("duplicate", "重复账号", str(self.managed), True))
        self.window._build_cards()
        self.window.refresh_all()
        self.assertEqual(self.window.pool.start.call_count, 2)
        quota = s.Quota(None, s.Window(20, 10080, None), 0)
        self.window._task_done("target", quota, "")
        self.assertIs(self.window.quotas["target"], quota)
        self.assertIs(self.window.quotas["duplicate"], quota)
        self.assertNotIn("duplicate", self.window.running)


if __name__ == "__main__":
    unittest.main()
