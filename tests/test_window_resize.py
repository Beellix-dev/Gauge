import json
import os
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEvent, QPointF, QRect, Qt
from PySide6.QtGui import QMouseEvent
from PySide6.QtWidgets import QApplication

from src import main as app_module


class WindowResizeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.stack = ExitStack()
        root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.settings = root / "window.json"
        for target, value in (
            ("APP_DIR", root), ("WINDOW_FILE", self.settings),
            ("load_profiles", lambda: [app_module.Profile("current", "测试", "unused", False)]),
            ("save_profiles", lambda profiles: None),
        ):
            self.stack.enter_context(patch.object(app_module, target, value))
        self.stack.enter_context(patch.object(app_module.Monitor, "_build_tray", lambda self: None))
        self.stack.enter_context(patch.object(app_module.Monitor, "refresh_all", lambda self: None))
        self.window = app_module.Monitor()
        self.window.show()
        self.app.processEvents()

    def tearDown(self):
        self.window._window_size_ready = False
        self.window._size_timer.stop()
        self.window.poll_timer.stop()
        self.window.close()
        self.window.deleteLater()
        self.app.processEvents()
        self.stack.close()

    def drag(self, edges, dx, dy):
        handle = next(handle for handle in self.window._resize_handles if handle.edges == edges)
        local = QPointF(handle.rect().center())
        start = QPointF(handle.mapToGlobal(handle.rect().center()))
        for event_type, global_position, button, buttons in (
            (QEvent.MouseButtonPress, start, Qt.LeftButton, Qt.LeftButton),
            (QEvent.MouseMove, start + QPointF(dx, dy), Qt.NoButton, Qt.LeftButton),
            (QEvent.MouseButtonRelease, start + QPointF(dx, dy), Qt.LeftButton, Qt.NoButton),
        ):
            self.app.sendEvent(handle, QMouseEvent(event_type, local, global_position, button, buttons, Qt.NoModifier))
        self.app.processEvents()

    def test_drag_all_edges_and_corners(self):
        for edges, dx, dy, size in (
            (Qt.LeftEdge, -40, 0, (460, 350)),
            (Qt.RightEdge, 40, 0, (460, 350)),
            (Qt.TopEdge, 0, -40, (420, 390)),
            (Qt.BottomEdge, 0, 40, (420, 390)),
            (Qt.LeftEdge | Qt.TopEdge, -40, -40, (460, 390)),
            (Qt.RightEdge | Qt.TopEdge, 40, -40, (460, 390)),
            (Qt.LeftEdge | Qt.BottomEdge, -40, 40, (460, 390)),
            (Qt.RightEdge | Qt.BottomEdge, 40, 40, (460, 390)),
        ):
            with self.subTest(edges=edges):
                self.window.setGeometry(200, 200, 420, 350)
                self.app.processEvents()
                self.drag(edges, dx, dy)
                self.assertEqual((self.window.width(), self.window.height()), size)

    def test_shrinking_keeps_opposite_corner_anchored(self):
        self.window.setGeometry(200, 200, 420, 350)
        before = QRect(self.window.geometry())
        self.drag(Qt.TopEdge | Qt.LeftEdge, 1000, 1000)
        self.assertEqual(self.window.size(), self.window.minimumSize())
        self.assertEqual(self.window.geometry().bottomRight(), before.bottomRight())

    def test_rebuild_preserves_size_and_restart_restores_it(self):
        self.window.resize(500, 420)
        self.app.processEvents()
        self.window._save_window_size()
        self.window._build_cards()
        self.assertEqual((self.window.width(), self.window.height()), (500, 420))
        self.assertEqual(json.loads(self.settings.read_text()), {"width": 500, "height": 420})
        other = app_module.Monitor()
        try:
            self.assertEqual((other.width(), other.height()), (500, 420))
        finally:
            other._window_size_ready = False
            other.close()
            other.deleteLater()

    def test_native_resize_does_not_apply_manual_cross_screen_delta(self):
        before = QRect(self.window.geometry())
        native = Mock()
        native.startSystemResize.return_value = True
        with patch.object(self.window, "windowHandle", return_value=native):
            self.drag(Qt.RightEdge | Qt.BottomEdge, 4000, 2000)
        native.startSystemResize.assert_called_once_with(Qt.RightEdge | Qt.BottomEdge)
        self.assertEqual(self.window.geometry(), before)

    def test_header_move_preserves_size_and_restores_free_resizing(self):
        self.window.resize(420, 350)
        self.app.processEvents()
        header = self.window.findChild(app_module.DragHeader)
        native = Mock()
        native.startSystemMove.return_value = True
        point = QPointF(header.mapToGlobal(header.rect().center()))
        press = QMouseEvent(QEvent.MouseButtonPress, QPointF(20, 10), point, Qt.LeftButton, Qt.LeftButton, Qt.NoModifier)
        with patch.object(self.window, "windowHandle", return_value=native):
            header.mousePressEvent(press)
        native.startSystemMove.assert_called_once()
        self.window.resize(1500, 1200)  # A snap/DPI resize during a move must not stretch the window.
        self.assertEqual((self.window.width(), self.window.height()), (420, 350))
        self.window._finish_interaction()
        self.assertEqual(self.window.minimumSize().width(), app_module.MIN_WINDOW_WIDTH)
        self.window.resize(500, 400)
        self.assertEqual((self.window.width(), self.window.height()), (500, 400))

    def test_manual_resize_cancels_when_dpi_changes(self):
        before = QRect(self.window.geometry())
        handle = self.window._resize_handles[1]
        handle.start_position = QPointF(0, 0).toPoint()
        handle.start_geometry = before
        handle.start_screen = self.window.screen()
        handle.start_scale = self.window.devicePixelRatioF() + 0.5
        move = QMouseEvent(QEvent.MouseMove, QPointF(10, 10), QPointF(4000, 2000), Qt.NoButton, Qt.LeftButton, Qt.NoModifier)
        handle.mouseMoveEvent(move)
        self.assertIsNone(handle.start_position)
        self.assertEqual(self.window.geometry(), before)


if __name__ == "__main__":
    unittest.main()
