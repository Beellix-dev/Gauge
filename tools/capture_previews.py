"""Render the real Qt interface with synthetic accounts, without reading credentials."""

import os
from pathlib import Path
import sys
import tempfile
from contextlib import ExitStack
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ["QT_QPA_PLATFORM"] = "offscreen"
os.environ["QT_SCALE_FACTOR"] = "2"


def main():
    destination = ROOT / "docs" / "images"
    destination.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="gauge-preview-") as temporary:
        # Set all data locations before importing the application.
        with patch.dict(os.environ, {
            "LOCALAPPDATA": temporary,
            "CODEX_HOME": str(Path(temporary) / "codex"),
            "CLAUDE_CONFIG_DIR": str(Path(temporary) / "claude"),
        }):
            from PySide6.QtWidgets import QApplication
            from src import main as gauge
            from src.preferences import Preferences

            app = QApplication.instance() or QApplication([])
            profiles = [
                gauge.Profile("current", "Personal", temporary, True),
                gauge.Profile("work", "Work", temporary, True),
                gauge.Profile("claude-current", "Studio", temporary, True, "claude"),
            ]
            now = 1_800_000_000
            quotas = {
                "current": gauge.Quota(None, gauge.Window(9, 10080, now + 6 * 86400 + 20 * 3600), now, "Plus"),
                "work": gauge.Quota(None, gauge.Window(58, 10080, now + 2 * 86400 + 3 * 3600), now, "Pro"),
                "claude-current": gauge.Quota(gauge.Window(26, 300, now + 3 * 3600 + 24 * 60),
                                             gauge.Window(12, 10080, now + 5 * 86400 + 8 * 3600), now, "Max 20×"),
            }
            with ExitStack() as stack:
                stack.enter_context(patch.object(gauge, "load_profiles", return_value=profiles))
                stack.enter_context(patch.object(gauge, "save_profiles"))
                stack.enter_context(patch.object(gauge, "startup_entry", return_value=None))
                stack.enter_context(patch.object(gauge.account_switch, "identity_or_none", return_value="demo"))
                stack.enter_context(patch.object(gauge.claude_provider, "local_login_available", return_value=True))
                stack.enter_context(patch.object(gauge.claude_provider, "identity", return_value="demo"))
                stack.enter_context(patch.object(gauge.claude_provider, "saved_plan", return_value="Max 20×"))
                stack.enter_context(patch.object(gauge.Monitor, "refresh_all"))
                stack.enter_context(patch.object(gauge.Monitor, "_build_tray"))
                stack.enter_context(patch.object(gauge.Monitor, "_save_window_size"))
                stack.enter_context(patch.object(gauge.time, "time", return_value=now))
                for name, theme, accent, language, settings in (
                    ("monitor-dark", "night", "mint", "zh", False),
                    ("monitor-light", "light", "purple", "en", False),
                    ("settings", "graphite", "purple", "zh", True),
                ):
                    with patch.object(Preferences, "load", return_value=Preferences(theme, accent, 100, True, 5, language)):
                        window = gauge.Monitor()
                        window.quotas = quotas
                        window._build_cards()
                        window.resize(390, 440)
                        if settings:
                            window.show_settings()
                        window.show()
                        app.processEvents()
                        path = destination / f"{name}.png"
                        if not window.grab().save(str(path)):
                            raise RuntimeError(f"Could not save {path}")
                        window.poll_timer.stop()
                        window.hide()
                        window.deleteLater()
                        app.processEvents()
                        print(path)


if __name__ == "__main__":
    main()
