import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from contextlib import ExitStack
from pathlib import Path
import tempfile
import threading
import unittest
import uuid
from unittest.mock import Mock, patch

from PySide6.QtWidgets import QApplication
from src import main as s
from tests.test_account_switch import auth


class LoginWorkerTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.stack.enter_context(patch.object(s, "ACCOUNTS_DIR", self.root))
        self.stack.enter_context(patch.object(s, "WORK_DIR", self.root))
        self.stack.enter_context(patch.object(s, "codex_binary", return_value="test-codex.exe"))
        self.process = Mock()
        self.process.poll.return_value = None
        self.process.terminate.side_effect = lambda: setattr(self.process.poll, "return_value", -1)
        self.launch = self.stack.enter_context(patch.object(s.subprocess, "Popen", return_value=self.process))
        self.cancel = threading.Event()
        self.key = str(uuid.uuid4())
        self.home = self.root / self.key

    def test_cancel_before_start_does_not_launch_or_create_account(self):
        self.cancel.set()
        self.assertIsNone(s.login_new_account(self.key, self.cancel, float("inf")))
        self.launch.assert_not_called()
        self.assertFalse(self.home.exists())

    def test_cancel_stops_owned_process_before_removing_partial_credentials(self):
        def wait(_):
            (self.home / "auth.json").write_bytes(auth())
            self.cancel.set()

        def terminate():
            self.assertTrue((self.home / "auth.json").exists())
            self.process.poll.return_value = -1

        self.process.terminate.side_effect = terminate
        with patch.object(self.cancel, "wait", side_effect=wait):
            self.assertIsNone(s.login_new_account(self.key, self.cancel, float("inf")))
        self.process.terminate.assert_called_once()
        self.process.wait.assert_called_once_with(timeout=2)
        self.assertFalse(self.home.exists())

    def test_timeout_stops_process_and_cleans_directory(self):
        with patch.object(s.time, "monotonic", side_effect=[0, 2]):
            with self.assertRaisesRegex(RuntimeError, "授权超时"):
                s.login_new_account(self.key, self.cancel, 1)
        self.process.terminate.assert_called_once()
        self.assertFalse(self.home.exists())

    def test_success_keeps_credentials_and_does_not_terminate_exited_process(self):
        def launch(*args, **kwargs):
            (self.home / "auth.json").write_bytes(auth())
            self.assertEqual(kwargs["env"]["CODEX_HOME"], str(self.home))
            self.assertEqual(args[0], ["test-codex.exe", "login"])
            return self.process

        self.launch.side_effect = launch
        self.process.poll.return_value = 0
        self.process.returncode = 0
        result = s.login_new_account(self.key, self.cancel, float("inf"))
        self.assertEqual(result.home, str(self.home))
        self.assertTrue((self.home / "auth.json").exists())
        self.process.terminate.assert_not_called()

    def test_failed_login_cleans_directory(self):
        self.process.poll.return_value = 1
        self.process.returncode = 1
        with self.assertRaisesRegex(RuntimeError, "授权未完成"):
            s.login_new_account(self.key, self.cancel, float("inf"))
        self.assertFalse(self.home.exists())

    def test_launch_failure_is_sanitized_and_cleaned(self):
        self.launch.side_effect = OSError("private details")
        with self.assertRaisesRegex(RuntimeError, "请检查 Codex CLI") as error:
            s.login_new_account(self.key, self.cancel, float("inf"))
        self.assertNotIn("private details", str(error.exception))
        self.assertFalse(self.home.exists())


class LoginUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.stack = ExitStack()
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.stack.enter_context(patch.multiple(
            s, APP_DIR=self.root, ACCOUNTS_DIR=self.root / "accounts",
            WINDOW_FILE=self.root / "window.json",
            load_profiles=lambda: [], save_profiles=lambda _: None,
        ))
        self.stack.enter_context(patch.object(s.Monitor, "_build_tray", lambda _: None))
        self.stack.enter_context(patch.object(s.Monitor, "refresh_all", lambda _: None))
        self.window = s.Monitor()
        self.window.pool = Mock()
        self.window.show()
        self.app.processEvents()

    def tearDown(self):
        self.window.cancel_login()
        self.window._window_size_ready = False
        self.window._size_timer.stop()
        self.window.poll_timer.stop()
        self.window.close()
        self.window.deleteLater()
        self.app.processEvents()
        self.stack.close()

    def test_cancel_completes_and_allows_retry_without_popup(self):
        self.window.add_account()
        task = self.window.pool.start.call_args.args[0]
        self.assertTrue(self.window.cancel_login_button.isVisible())
        self.assertFalse(self.window.add_button.isEnabled())
        self.assertIn("等待授权", self.window.note.text())
        self.window.cancel_login_button.click()
        with patch.object(s.subprocess, "Popen") as launch:
            task.run()
            launch.assert_not_called()
        self.assertEqual(self.window.note.text(), "已取消")
        self.assertNotIn("add", self.window.running)
        self.assertTrue(self.window.add_button.isEnabled())
        self.assertFalse(self.window.login_timer.isActive())
        self.window.add_account()
        self.assertFalse(self.window._login_cancel.is_set())

    def test_cancel_wins_over_queued_success(self):
        self.window.add_account()
        task = self.window.pool.start.call_args.args[0]
        key = task.args[0]
        home = self.root / "accounts" / key
        home.mkdir(parents=True)
        (home / "auth.json").write_bytes(auth())
        self.window.cancel_login()
        self.window._task_done("add", s.Profile(key, "Test", str(home), True), "")
        self.assertEqual(self.window.profiles, [])
        self.assertFalse(home.exists())
        self.assertEqual(self.window.note.text(), "已取消")

    def test_failure_is_inline_and_controls_recover(self):
        self.window.add_account()
        with patch.object(s.QMessageBox, "warning") as warning:
            self.window._task_done("add", None, "授权超时，请重新添加。")
            warning.assert_not_called()
        self.assertEqual(self.window.note.text(), "授权超时，请重新添加。")
        self.assertTrue(self.window.add_button.isEnabled())
        self.assertFalse(self.window.cancel_login_button.isVisible())


if __name__ == "__main__":
    unittest.main()
