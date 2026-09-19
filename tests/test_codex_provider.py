import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from src import codex_provider


class QuotaParsingTests(unittest.TestCase):
    def test_weekly_window_can_be_primary(self):
        result = codex_provider.parse_quota({"rateLimits": {
            "primary": {"usedPercent": 2, "windowDurationMins": 10080, "resetsAt": 1234},
            "secondary": None,
        }})
        self.assertIsNone(result.five)
        self.assertEqual(f"{100 - result.week.used:.0f}%", "98%")

    def test_prefers_codex_limit_from_multiple_buckets(self):
        result = codex_provider.parse_quota({
            "rateLimitsByLimitId": {
                "other": {"primary": {"usedPercent": 90, "windowDurationMins": 300}},
                "codex": {
                    "primary": {"usedPercent": 20, "windowDurationMins": 300},
                    "secondary": {"usedPercent": 40, "windowDurationMins": 10080},
                },
            }
        })
        self.assertEqual(f"{100 - result.five.used:.0f}%", "80%")
        self.assertEqual(f"{100 - result.week.used:.0f}%", "60%")

    def test_rejects_missing_windows(self):
        with self.assertRaises(RuntimeError):
            codex_provider.parse_quota({"rateLimits": {}})


class QuotaRpcTests(unittest.TestCase):
    @staticmethod
    def mock_process(reply):
        process = Mock()
        process.stdin = io.StringIO()
        process.stdout = io.StringIO(
            json.dumps({"id": 1, "result": {}}) + "\n" + json.dumps(reply) + "\n"
        )
        launch = patch.object(codex_provider.subprocess, "Popen", return_value=process)
        return process, launch

    def test_rpc_uses_explicit_home_and_work_directory(self):
        with tempfile.TemporaryDirectory() as folder:
            work_dir = Path(folder)
            home = str(work_dir / "account")
            reply = {"id": 2, "result": {"rateLimits": {
                "primary": {"usedPercent": 9, "windowDurationMins": 10080}, "planType": "plus"
            }}}
            process, launch = self.mock_process(reply)
            with launch as popen, patch.object(codex_provider, "binary", return_value="codex-test.exe"):
                quota = codex_provider.read_quota(home, work_dir)
            self.assertEqual(quota.week.used, 9)
            self.assertEqual(quota.plan, "Plus")
            self.assertEqual(popen.call_args.args[0], ["codex-test.exe", "app-server", "--stdio"])
            self.assertEqual(popen.call_args.kwargs["env"]["CODEX_HOME"], home)
            self.assertEqual(popen.call_args.kwargs["cwd"], work_dir)
            requests = [json.loads(line) for line in process.stdin.getvalue().splitlines()]
            self.assertEqual([item["method"] for item in requests],
                             ["initialize", "initialized", "account/rateLimits/read"])
            process.terminate.assert_called_once()
            process.wait.assert_called_once_with(timeout=2)

    def test_rpc_failure_still_closes_owned_process(self):
        process, launch = self.mock_process({"id": 2, "error": {"message": "private detail"}})
        with launch, patch.object(codex_provider, "binary", return_value="codex-test.exe"):
            with self.assertRaisesRegex(RuntimeError, "查询失败") as error:
                codex_provider.read_quota("test", Path("test"))
        self.assertNotIn("private detail", str(error.exception))
        process.terminate.assert_called_once()
