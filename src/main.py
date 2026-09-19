"""Gauge: a Windows quota monitor for Codex and Claude Code accounts."""

from __future__ import annotations

import json
import hashlib
import os
import shutil
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import asdict
from pathlib import Path

from PySide6.QtCore import QObject, QRunnable, QRect, QRectF, QSize, QEvent, QThreadPool, QTimer, Qt, Signal, QLockFile, QUrl
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtGui import QAction, QColor, QFont, QFontDatabase, QIcon, QMouseEvent, QPainter, QPen, QPixmap, QDesktopServices
from PySide6.QtWidgets import (
    QApplication, QFrame, QHBoxLayout, QLabel,
    QMenu, QMessageBox, QPushButton, QScrollArea, QSystemTrayIcon,
    QVBoxLayout, QWidget, QInputDialog, QSizePolicy, QFormLayout,
    QComboBox, QSlider, QCheckBox, QStackedWidget,
)

from .preferences import (
    Preferences, THEMES, ACCENTS, INTERVALS, stylesheet,
    startup_command, startup_entry, write_startup_entry,
)
from . import account_switch, claude_provider, codex_provider
from .codex_provider import binary as codex_binary
from .models import Profile, Quota, Window
from .i18n import LANGUAGES, tr, set_language
from . import __version__


APP_NAME = "Gauge"
RESOURCE_ROOT = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[1]))
APP_ICON = RESOURCE_ROOT / "assets" / "gauge.ico"


def app_data_directory() -> Path:
    """Reuse existing credentials in place; new installations use the Gauge name."""
    local = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    legacy = local / "SafeQuota"
    # Profiles contain absolute paths. Keeping an existing store avoids moving
    # credentials or creating a second store while an older release is running.
    return legacy if legacy.is_dir() else local / APP_NAME


APP_DIR = app_data_directory()
ACCOUNTS_DIR = APP_DIR / "accounts"
PROFILES_FILE = APP_DIR / "profiles.json"
WINDOW_FILE = APP_DIR / "window.json"
MIN_WINDOW_WIDTH = 280
MIN_WINDOW_HEIGHT = 170
LOGIN_TIMEOUT_SECONDS = 240
WORK_DIR = APP_DIR / "empty-workdir"
DEFAULT_HOME = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex").resolve()


def load_profiles() -> list[Profile]:
    APP_DIR.mkdir(parents=True, exist_ok=True)
    ACCOUNTS_DIR.mkdir(parents=True, exist_ok=True)
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    profiles: list[Profile] = []
    if PROFILES_FILE.exists():
        try:
            payload = json.loads(PROFILES_FILE.read_text(encoding="utf-8"))
            for item in payload.get("profiles", []):
                if item.get("provider", "codex") not in ("codex", "claude"):
                    continue
                profiles.append(Profile(
                    id=str(item["id"]), name=str(item["name"])[:40],
                    home=str(item["home"]), managed=bool(item["managed"]),
                    provider=item.get("provider", "codex"),
                ))
        except (OSError, ValueError, KeyError, TypeError):
            profiles = []
    profiles = [item for item in profiles if item.managed or (
        claude_provider.local_login_available(Path(item.home)) if item.provider == "claude" else
        account_switch.identity_or_none(Path(item.home)) is not None
    )]
    if account_switch.identity_or_none(DEFAULT_HOME) is not None and not any(not item.managed and item.provider == "codex" for item in profiles):
        profiles.insert(0, Profile("current", "当前账号", str(DEFAULT_HOME), False))
    if claude_provider.local_login_available(claude_provider.DEFAULT_HOME) and not any(
        not item.managed and item.provider == "claude" for item in profiles
    ):
        profiles.append(Profile("claude-current", "Claude 当前账号", str(claude_provider.DEFAULT_HOME), False, "claude"))
    return profiles


def save_profiles(profiles: list[Profile]) -> None:
    APP_DIR.mkdir(parents=True, exist_ok=True)
    temp = PROFILES_FILE.with_suffix(".tmp")
    temp.write_text(json.dumps({"version": 1, "profiles": [asdict(p) for p in profiles]}, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(PROFILES_FILE)


def load_window_size(default: tuple[int, int]) -> tuple[int, int]:
    try:
        payload = json.loads(WINDOW_FILE.read_text(encoding="utf-8"))
        width, height = payload["width"], payload["height"]
        if type(width) is int and type(height) is int:
            return max(MIN_WINDOW_WIDTH, min(10000, width)), max(MIN_WINDOW_HEIGHT, min(10000, height))
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return default


def read_quota(home: str) -> Quota:
    return codex_provider.read_quota(home, WORK_DIR)

def read_claude_quota(home: str) -> Quota:
    result = claude_provider.read_usage(home)
    return Quota(Window(**result["five"]) if result["five"] else None,
                 Window(**result["week"]) if result["week"] else None, result["checked_at"], result.get("plan", ""))


def login_new_account(profile_id: str, cancel: threading.Event, deadline: float, provider: str = "codex") -> Profile | None:
    if provider not in ("codex", "claude"):
        raise RuntimeError("不支持的账号类型。")
    if cancel.is_set():
        return None
    home = ACCOUNTS_DIR / profile_id
    home.mkdir(parents=True, exist_ok=False)
    process = None
    completed = False
    try:
        if provider == "claude":
            env = claude_provider.login_environment(home)
            command = [claude_provider.binary(), "auth", "login", "--claudeai"]
            credential_file = ".credentials.json"
        else:
            (home / "config.toml").write_text('cli_auth_credentials_store = "file"\n', encoding="utf-8")
            env = os.environ.copy()
            env["CODEX_HOME"] = str(home)
            command = [codex_binary(), "login"]
            credential_file = "auth.json"
        if cancel.is_set():
            return None
        if time.monotonic() >= deadline:
            raise RuntimeError("授权超时，请重新添加。")
        process = subprocess.Popen(
            command, env=env, cwd=WORK_DIR,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        while process.poll() is None:
            if cancel.is_set():
                return None
            if time.monotonic() >= deadline:
                raise RuntimeError("授权超时，请重新添加。")
            cancel.wait(0.1)
        if cancel.is_set():
            return None
        if process.returncode != 0 or not (home / credential_file).exists():
            raise RuntimeError("授权未完成，请重新添加。")
        if provider == "claude":
            claude_provider.credentials(home)
        completed = True
        return Profile(profile_id, f"账号 {profile_id[:4].upper()}", str(home), True, provider)
    except OSError:
        raise RuntimeError(f"无法完成登录，请检查 {'Claude Code' if provider == 'claude' else 'Codex CLI'} 后重试。") from None
    finally:
        # Stop only the login process created by this attempt, before removing its files.
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)
        if not completed:
            shutil.rmtree(home, ignore_errors=True)


class Signals(QObject):
    finished = Signal(str, object, str)


class Task(QRunnable):
    def __init__(self, key: str, action, *args: object):
        super().__init__()
        self.key, self.action, self.args = key, action, args
        self.signals = Signals()

    def run(self) -> None:
        try:
            self.signals.finished.emit(self.key, self.action(*self.args), "")
        except Exception as error:
            self.signals.finished.emit(self.key, None, str(error))


def remaining(window: Window | None) -> str:
    return "—" if window is None else f"{max(0, 100 - window.used):.0f}%"


def reset_label(window: Window | None) -> str:
    if not window or not window.reset:
        return ""
    seconds = max(0, window.reset - int(time.time()))
    if seconds < 3600:
        return tr("{minutes}分", minutes=max(1, seconds // 60))
    if seconds < 86400:
        return tr("{hours}时", hours=seconds // 3600)
    return tr("{days}天{hours}时", days=seconds // 86400, hours=seconds % 86400 // 3600)


def settings_icon(color: str) -> QIcon:
    pixmap = QPixmap(48, 48)
    pixmap.fill(Qt.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.Antialiasing)
    painter.translate(24, 24)
    painter.setPen(QPen(QColor(color), 3, Qt.SolidLine, Qt.RoundCap))
    for _ in range(8):
        painter.drawLine(0, -14, 0, -18)
        painter.rotate(45)
    painter.drawEllipse(-13, -13, 26, 26)
    painter.drawEllipse(-5, -5, 10, 10)
    painter.end()
    return QIcon(pixmap)


def provider_pixmap(provider: str, light: bool, scale: float) -> QPixmap:
    filename = "claude.svg" if provider == "claude" else "openai.svg"
    color = "#D97757" if provider == "claude" else ("#000000" if light else "#FFFFFF")
    svg = (APP_ICON.parent / filename).read_bytes()
    svg = svg.replace(b"<svg ", f'<svg fill="{color}" '.encode(), 1)
    pixmap = QPixmap(round(20 * scale), round(20 * scale))
    pixmap.setDevicePixelRatio(scale)
    pixmap.fill(Qt.transparent)
    painter = QPainter(pixmap)
    QSvgRenderer(svg).render(painter, QRectF(0, 0, 20, 20))
    painter.end()
    return pixmap


class AccountCard(QFrame):
    def __init__(self, profile: Profile, color: str, menu_callback, track: str = "#283447", active: bool = False,
                 provider: str = "codex", light: bool = False, saved_plan: str = ""):
        super().__init__()
        if provider not in ("codex", "claude"):
            raise ValueError("Unsupported account provider")
        self.profile = profile
        self.provider = provider
        self.color = color
        self.track = track
        self.saved_plan = saved_plan
        self.setObjectName("card")
        self.setMinimumHeight(92 if provider == "codex" else 126)
        body = QVBoxLayout(self)
        body.setContentsMargins(14, 11, 14, 12)
        body.setSpacing(6)
        top = QHBoxLayout()
        top.setSpacing(8)
        self.provider_logo = QLabel()
        self.provider_logo.setFixedSize(24, 24)
        self.provider_logo.setAlignment(Qt.AlignCenter)
        self.provider_logo.setPixmap(provider_pixmap(provider, light, self.devicePixelRatioF()))
        provider_name = "Claude Code" if provider == "claude" else "OpenAI · Codex"
        self.provider_logo.setToolTip(provider_name)
        self.provider_logo.setAccessibleName(provider_name)
        display_name = tr(profile.name) if not profile.managed and profile.name in ("当前账号", "Claude 当前账号") else profile.name
        self.name = QLabel(display_name)
        self.name.setTextFormat(Qt.PlainText)
        self.name.setObjectName("accountName")
        self.name.setStyleSheet("font-size:13px; font-weight:700")
        self.name.ensurePolished()
        self.name.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.name.setToolTip(display_name)
        self.name.setMaximumWidth(self.name.sizeHint().width() + 2)
        self.active_light = QFrame()
        self.active_light.setFixedSize(8, 8)
        self.active_light.setObjectName("activeLight")
        lamp_color = ("#17834B" if light else "#34C985") if active else ("#D03842" if light else "#F0676E")
        self.active_light.setStyleSheet(f"#activeLight {{background:{lamp_color}; border:0; border-radius:4px;}}")
        state = tr("正在使用") if active else tr("未用于当前登录，仍监控额度")
        self.active_light.setToolTip(state)
        self.active_light.setAccessibleName(state)
        self.status = QLabel("")
        self.status.setObjectName("subtle")
        self.plan_badge = QLabel()
        self.plan_badge.setObjectName("planBadge")
        self.plan_badge.setTextFormat(Qt.PlainText)
        self.plan_badge.setAlignment(Qt.AlignCenter)
        self.plan_badge.setFixedHeight(20)
        self.plan_badge.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        tint = QColor(color)
        rgb = f"{tint.red()},{tint.green()},{tint.blue()}"
        self.plan_badge.setStyleSheet(
            f"#planBadge {{color:{color}; background:rgba({rgb},20); border:1px solid rgba({rgb},65);"
            "border-radius:6px; padding:0 6px; font-size:10px; font-weight:600;}"
        )
        self.plan_badge.hide()
        more = QPushButton("⋯")
        more.setObjectName("iconButton")
        more.setFixedSize(25, 23)
        more.setToolTip(tr("账号设置"))
        more.setAccessibleName(tr("账号设置"))
        more.clicked.connect(lambda: menu_callback(profile, more))
        top.addWidget(self.provider_logo)
        top.addWidget(self.name, 1)
        top.addWidget(self.active_light)
        top.addStretch()
        top.addWidget(self.status)
        top.addWidget(self.plan_badge)
        top.addWidget(more)
        body.addLayout(top)
        self.rows: list[tuple[QLabel, QLabel, QFrame]] = []
        for label in ((tr("每周"),) if provider == "codex" else (tr("5 小时"), tr("每周"))):
            row = QHBoxLayout()
            title = QLabel(label)
            title.setObjectName("rowTitle")
            value = QLabel("—")
            value.setObjectName("value")
            value.setStyleSheet(f"color:{color}; font-size:18px; font-weight:700")
            reset = QLabel("")
            reset.setObjectName("subtle")
            bar = QFrame()
            bar.setFixedHeight(5)
            bar.setObjectName("bar")
            row.addWidget(title)
            row.addWidget(value)
            row.addStretch()
            row.addWidget(reset)
            body.addLayout(row)
            body.addWidget(bar)
            self.rows.append((value, reset, bar))

    def update_data(self, quota: Quota | None, error: str = "") -> None:
        needs_login = self.provider == "claude" and any(word in error for word in ("授权", "登录"))
        self.status.setText((tr("需登录") if needs_login else tr("失败")) if error else "")
        self.status.setToolTip(tr(error))
        plan = quota.plan if quota else self.saved_plan
        self.plan_badge.setText(plan)
        self.plan_badge.setVisible(bool(plan))
        origin = tr("最近登录的会员等级") if self.provider == "claude" else tr("会员等级")
        self.plan_badge.setToolTip(f"{origin}：{plan}" if plan else "")
        self.plan_badge.setAccessibleName(f"{origin}：{plan}" if plan else "")
        windows = (quota.week,) if quota and self.provider == "codex" else ((quota.five, quota.week) if quota else (None,) * len(self.rows))
        for (value, reset, bar), window in zip(self.rows, windows):
            value.setText(remaining(window))
            reset.setText(reset_label(window))
            reset.setToolTip(tr("距离额度重置") if window and window.reset else "")
            value.setToolTip(tr("剩余额度") if window else tr("此额度窗口暂无数据"))
            width = 0 if not window else max(0, min(100, 100 - window.used))
            if width == 0:
                background = self.track
            elif width == 100:
                background = self.color
            else:
                background = f"qlineargradient(x1:0,y1:0,x2:1,y2:0,stop:0 {self.color},stop:{width/100:.3f} {self.color},stop:{min(1,width/100+0.001):.3f} {self.track},stop:1 {self.track})"
            bar.setStyleSheet(f"QFrame{{border-radius:2px; background:{background};}}")


class SettingsPage(QWidget):
    def __init__(self, owner: "Monitor"):
        super().__init__(owner)
        self.owner = owner
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(14)
        header = DragHeader(owner)
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(0, 0, 0, 0)
        self.back_button = QPushButton(tr("返回"))
        self.back_button.setObjectName("smallButton")
        self.back_button.setToolTip(tr("返回监控，放弃未保存的修改"))
        self.back_button.clicked.connect(owner.show_monitor)
        header_layout.addWidget(self.back_button)
        heading = QLabel(tr("设置"))
        heading.setObjectName("heading")
        heading.setAttribute(Qt.WA_TransparentForMouseEvents)
        header_layout.addWidget(heading)
        header_layout.addStretch()
        self.save_button = QPushButton(tr("保存"))
        self.save_button.setObjectName("saveButton")
        self.save_button.clicked.connect(self.save)
        header_layout.addWidget(self.save_button)
        layout.addWidget(header)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.scroll.viewport().setAutoFillBackground(False)
        body = QWidget()
        body.setObjectName("settingsBody")
        body.setStyleSheet("#settingsBody {background:transparent;}")
        body_layout = QVBoxLayout(body)
        body_layout.setContentsMargins(0, 0, 6, 4)
        body_layout.setSpacing(12)
        form = QFormLayout()
        form.setSpacing(14)
        form.setRowWrapPolicy(QFormLayout.WrapLongRows)
        form.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
        self.language = QComboBox()
        for key, name in LANGUAGES.items():
            self.language.addItem(name, key)
        self.language.setCurrentIndex(self.language.findData(owner.preferences.language))
        form.addRow("语言 / Language", self.language)
        self.theme = QComboBox()
        for key, colors in THEMES.items():
            self.theme.addItem(tr(colors["name"]), key)
        self.theme.setCurrentIndex(self.theme.findData(owner.preferences.theme))
        form.addRow(tr("风格"), self.theme)
        self.accent = QComboBox()
        for key, (name, dark, light) in ACCENTS.items():
            swatch = QPixmap(16, 16)
            swatch.fill(QColor(dark))
            self.accent.addItem(QIcon(swatch), tr(name), key)
        self.accent.setCurrentIndex(self.accent.findData(owner.preferences.accent))
        form.addRow(tr("强调色"), self.accent)
        opacity_row = QHBoxLayout()
        self.opacity = QSlider(Qt.Horizontal)
        self.opacity.setRange(70, 100)
        self.opacity.setValue(owner.preferences.opacity)
        self.opacity.setAccessibleName(tr("不透明度"))
        self.opacity_value = QLabel(f"{self.opacity.value()}%")
        self.opacity_value.setMinimumWidth(38)
        self.opacity.valueChanged.connect(lambda value: self.opacity_value.setText(f"{value}%"))
        opacity_row.addWidget(self.opacity)
        opacity_row.addWidget(self.opacity_value)
        form.addRow(tr("不透明度"), opacity_row)
        self.interval = QComboBox()
        for minutes in INTERVALS:
            self.interval.addItem(tr("{minutes} 分钟", minutes=minutes), minutes)
        self.interval.setCurrentIndex(self.interval.findData(owner.preferences.refresh_minutes))
        form.addRow(tr("自动刷新"), self.interval)
        self.topmost = QCheckBox(tr("窗口置顶"))
        self.topmost.setChecked(owner.preferences.topmost)
        form.addRow("", self.topmost)
        self.autostart = QCheckBox(tr("开机自启动"))
        try:
            self.initial_startup = startup_entry()
            self.autostart.setChecked(self.initial_startup is not None)
        except OSError:
            self.initial_startup = None
            self.autostart.setEnabled(False)
            self.autostart.setToolTip(tr("无法读取 Windows 启动设置"))
        form.addRow("", self.autostart)
        body_layout.addLayout(form)
        self.error_label = QLabel("")
        self.error_label.setWordWrap(True)
        self.error_label.hide()
        body_layout.addWidget(self.error_label)
        body_layout.addStretch()
        self.scroll.setWidget(body)
        layout.addWidget(self.scroll, 1)
        self.theme.currentIndexChanged.connect(self.preview)
        self.accent.currentIndexChanged.connect(self.preview)
        self.opacity.valueChanged.connect(self.preview)

    def values(self) -> Preferences:
        return Preferences(
            theme=self.theme.currentData(), accent=self.accent.currentData(),
            opacity=self.opacity.value(), topmost=self.topmost.isChecked(),
            refresh_minutes=self.interval.currentData(),
            language=self.language.currentData(),
        )

    def preview(self) -> None:
        self.owner.setStyleSheet(stylesheet(self.values()))
        self.owner.setWindowOpacity(self.opacity.value() / 100)

    def save(self) -> None:
        prefs = self.values()
        changed_startup = False
        try:
            if self.autostart.isEnabled():
                desired = startup_command() if self.autostart.isChecked() else None
                changed_startup = desired != self.initial_startup
                if changed_startup:
                    write_startup_entry(desired)
            prefs.save(APP_DIR / "settings.json")
        except OSError:
            if changed_startup:
                try:
                    write_startup_entry(self.initial_startup)
                except OSError:
                    self.error_label.setText(tr("保存失败，请检查 Windows 启动应用设置。"))
                    self.error_label.show()
                    return
            self.error_label.setText(tr("保存失败，请检查本地目录权限后重试。"))
            self.error_label.show()
            return
        self.owner.preferences = prefs
        self.owner.apply_preferences()
        self.owner.show_monitor()


class SwitchPage(QWidget):
    def __init__(self, owner: "Monitor", profile: Profile):
        super().__init__(owner)
        self.owner, self.profile = owner, profile
        self.waiting = False
        self.completed = False
        self.launcher = None
        self.quiet_ticks = 0
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        header = DragHeader(owner)
        row = QHBoxLayout(header)
        row.setContentsMargins(0, 0, 0, 0)
        self.back = QPushButton(tr("返回"))
        self.back.setObjectName("smallButton")
        self.back.clicked.connect(owner.cancel_switch)
        row.addWidget(self.back)
        title = QLabel(tr("切换账号"))
        title.setObjectName("heading")
        title.setAttribute(Qt.WA_TransparentForMouseEvents)
        row.addWidget(title)
        row.addStretch()
        layout.addWidget(header)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.viewport().setAutoFillBackground(False)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        body = QWidget()
        body.setObjectName("switchBody")
        body.setStyleSheet("#switchBody {background:transparent;}")
        inner = QVBoxLayout(body)
        inner.setContentsMargins(0, 12, 4, 12)
        inner.setSpacing(16)
        name = QLabel(profile.name)
        name.setTextFormat(Qt.PlainText)
        name.setWordWrap(True)
        name.setObjectName("heading")
        inner.addWidget(name)
        self.status = QLabel(tr("请先结束任务并退出 Codex。\n项目、历史和缓存继续共用。"))
        self.status.setTextFormat(Qt.PlainText)
        self.status.setWordWrap(True)
        inner.addWidget(self.status)
        inner.addStretch()
        scroll.setWidget(body)
        layout.addWidget(scroll, 1)
        self.button = QPushButton(tr("切换并打开 Codex"))
        self.button.setObjectName("saveButton")
        self.button.clicked.connect(self.start)
        layout.addWidget(self.button)
        self.timer = QTimer(self)
        self.timer.setInterval(1000)
        self.timer.timeout.connect(self.check)
        try:
            self.launcher = account_switch.desktop_path(account_switch.codex_processes())
        except account_switch.SwitchError:
            pass
        if not self.launcher:
            self.button.setText(tr("切换账号"))

    def start(self) -> None:
        if self.completed:
            self.owner.cancel_switch()
            return
        if self.waiting:
            return
        self.waiting = True
        self.button.setEnabled(False)
        self.button.setText(tr("等待 Codex 退出…"))
        self.status.setText(tr("现在可以退出 Codex。\n退出后自动切换，可点“返回”取消。"))
        self.timer.start()
        self.check()

    def check(self) -> None:
        if self.owner.running:
            self.quiet_ticks = 0
            return
        try:
            processes = account_switch.codex_processes()
            self.launcher = account_switch.desktop_path(processes) or self.launcher
            if processes:
                self.quiet_ticks = 0
                return
        except account_switch.SwitchError as error:
            self.failed(str(error))
            return
        self.quiet_ticks += 1
        if self.quiet_ticks < 2:
            return
        self.timer.stop()
        self.back.setEnabled(False)
        self.status.setText(tr("正在验证账号并切换…"))
        self.button.setText(tr("切换中…"))
        self.owner.running.add("switch")
        task = Task("switch", account_switch.switch_account, asdict(self.profile),
                    [asdict(p) for p in self.owner.profiles], self.owner.default_home(), APP_DIR, read_quota)
        task.signals.finished.connect(self.owner._task_done)
        self.owner.pool.start(task)

    def failed(self, error: str) -> None:
        self.timer.stop()
        self.waiting = False
        self.quiet_ticks = 0
        self.status.setText(tr(error))
        self.back.setEnabled(True)
        self.button.setText(tr("重试切换"))
        self.button.setEnabled(True)

    def succeeded(self) -> None:
        self.completed = True
        self.waiting = False
        self.back.setEnabled(True)
        self.button.setEnabled(True)
        self.button.setText(tr("返回监控"))
        self.status.setText(tr("登录凭据已切换，请打开 Codex 确认。"))
        if self.launcher:
            try:
                os.startfile(self.launcher)
                self.status.setText(tr("登录凭据已切换，已请求打开 Codex。"))
            except OSError:
                self.status.setText(tr("登录凭据已切换，请手动打开 Codex。"))


class DragHeader(QFrame):
    def __init__(self, owner: "Monitor"):
        super().__init__()
        self.owner = owner
        self.origin = None
        self.start_screen = None

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.LeftButton:
            self.origin = None
            self.owner._begin_interaction("move")
            window = self.owner.windowHandle()
            if window and window.startSystemMove():
                event.accept()
                return
            self.start_screen = self.owner.screen()
            self.origin = event.globalPosition().toPoint() - self.owner.frameGeometry().topLeft()
            event.accept()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if self.origin is not None and event.buttons() & Qt.LeftButton:
            if self.owner.screen() != self.start_screen:
                self.origin = None
                self.owner._finish_interaction()
                return
            self.owner.move(event.globalPosition().toPoint() - self.origin)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        self.origin = None
        self.owner._finish_interaction()

    def event(self, event) -> bool:
        if event.type() in (QEvent.UngrabMouse, QEvent.Hide) and self.origin is not None:
            self.origin = None
            self.owner._finish_interaction()
        return super().event(event)


class ResizeHandle(QWidget):
    """Delegate edge resizing to the OS, which owns mixed-DPI desktop coordinates."""

    def __init__(self, owner: "Monitor", edges: Qt.Edge, cursor: Qt.CursorShape):
        super().__init__(owner)
        self.owner = owner
        self.edges = edges
        self.start_position = None
        self.start_geometry = QRect()
        self.start_screen = None
        self.start_scale = None
        self.setCursor(cursor)
        self.setAttribute(Qt.WA_NoSystemBackground)

    def paintEvent(self, event) -> None:
        # Windows layered windows pass clicks through pixels with zero alpha.
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor(0, 0, 0, 1))

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.LeftButton:
            self.start_position = None
            self.owner._begin_interaction("resize")
            window = self.owner.windowHandle()
            if window and window.startSystemResize(self.edges):
                event.accept()
                return
            self.start_position = event.globalPosition().toPoint()
            self.start_geometry = self.owner.geometry()
            self.start_screen = self.owner.screen()
            self.start_scale = self.owner.devicePixelRatioF()
            event.accept()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if self.start_position is None or not event.buttons() & Qt.LeftButton:
            return
        if self.owner.screen() != self.start_screen or self.owner.devicePixelRatioF() != self.start_scale:
            self.start_position = None
            self.owner._finish_interaction()
            return
        delta = event.globalPosition().toPoint() - self.start_position
        initial = self.start_geometry
        x, y, width, height = initial.x(), initial.y(), initial.width(), initial.height()
        if self.edges & Qt.LeftEdge:
            width = max(self.owner.minimumWidth(), width - delta.x())
            x = initial.x() + initial.width() - width
        elif self.edges & Qt.RightEdge:
            width = max(self.owner.minimumWidth(), width + delta.x())
        if self.edges & Qt.TopEdge:
            height = max(self.owner.minimumHeight(), height - delta.y())
            y = initial.y() + initial.height() - height
        elif self.edges & Qt.BottomEdge:
            height = max(self.owner.minimumHeight(), height + delta.y())
        self.owner.setGeometry(x, y, width, height)
        event.accept()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.LeftButton:
            self.start_position = None
            self.owner._finish_interaction()
            event.accept()

    def event(self, event) -> bool:
        if event.type() in (QEvent.UngrabMouse, QEvent.Hide) and self.start_position is not None:
            self.start_position = None
            self.owner._finish_interaction()
        return super().event(event)


class Monitor(QWidget):
    def __init__(self):
        super().__init__()
        self._window_size_ready = False
        self._interaction = None
        self._move_size = None
        self._watched_window = None
        self._resize_handles: list[ResizeHandle] = []
        self.settings_page: SettingsPage | None = None
        self.switch_page: SwitchPage | None = None
        self.active_id = "current"
        self.claude_active_id = "claude-current"
        self._query_followers: dict[str, list[str]] = {}
        self.preferences = Preferences.load(APP_DIR / "settings.json")
        set_language(self.preferences.language)
        font_file = next((path for path in (Path(r"C:\Windows\Fonts\NotoSansSC-VF.ttf"), Path(r"C:\Windows\Fonts\msyh.ttc")) if path.exists()), None)
        if font_file:
            font_id = QFontDatabase.addApplicationFont(str(font_file))
            families = QFontDatabase.applicationFontFamilies(font_id)
            if families:
                QApplication.instance().setFont(QFont(families[0], 10))
        self.profiles = load_profiles()
        save_profiles(self.profiles)
        self.cards: dict[str, AccountCard] = {}
        self.quotas: dict[str, Quota] = {}
        self.errors: dict[str, str] = {}
        self.pool = QThreadPool.globalInstance()
        self.running: set[str] = set()
        self._login_cancel: threading.Event | None = None
        self._login_deadline = 0.0
        self.setWindowTitle(APP_NAME)
        self.setWindowIcon(QIcon(str(APP_ICON)))
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.Tool)
        self.setWindowFlag(Qt.WindowStaysOnTopHint, self.preferences.topmost)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setMinimumSize(MIN_WINDOW_WIDTH, MIN_WINDOW_HEIGHT)
        self.resize(360, 210)
        self.setStyleSheet(stylesheet(self.preferences))
        self.setWindowOpacity(self.preferences.opacity / 100)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(8, 8, 8, 8)
        self.shell = QFrame()
        self.shell.setObjectName("shell")
        outer.addWidget(self.shell)
        shell_layout = QVBoxLayout(self.shell)
        shell_layout.setContentsMargins(14, 12, 14, 14)
        self.pages = QStackedWidget()
        shell_layout.addWidget(self.pages)
        self.monitor_page = QWidget()
        self.pages.addWidget(self.monitor_page)
        content = QVBoxLayout(self.monitor_page)
        content.setContentsMargins(0, 0, 0, 0)
        content.setSpacing(10)
        header = DragHeader(self)
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(0, 0, 0, 0)
        header_layout.setSpacing(5)
        self.brand_logo = QLabel()
        self.brand_logo.setFixedSize(22, 22)
        self.brand_logo.setPixmap(QIcon(str(APP_ICON)).pixmap(QSize(22, 22), self.devicePixelRatioF()))
        self.brand_logo.setAttribute(Qt.WA_TransparentForMouseEvents)
        header_layout.addWidget(self.brand_logo)
        heading = QLabel(APP_NAME)
        heading.setObjectName("heading")
        heading.setAttribute(Qt.WA_TransparentForMouseEvents)
        header_layout.addWidget(heading)
        header_layout.addStretch()
        self.add_button = QPushButton("+")
        self.add_button.setObjectName("addButton")
        self.add_button.setFixedSize(27, 27)
        self.add_button.setToolTip(tr("添加账号"))
        self.add_button.setAccessibleName(tr("添加账号"))
        self.add_button.clicked.connect(self.show_add_menu)
        refresh = QPushButton(tr("刷新"))
        self.refresh_button = refresh
        refresh.setObjectName("smallButton")
        refresh.setToolTip(tr("刷新所有账号额度"))
        refresh.clicked.connect(self.refresh_all)
        settings = QPushButton()
        self.settings_button = settings
        settings.setIcon(settings_icon(self.preferences.colors()["muted"]))
        settings.setObjectName("iconButton")
        settings.setFixedSize(27, 27)
        settings.setToolTip(tr("设置"))
        settings.setAccessibleName(tr("设置"))
        settings.clicked.connect(self.show_settings)
        hide = QPushButton("−")
        self.hide_button = hide
        hide.setObjectName("smallButton")
        hide.setFixedSize(24, 27)
        hide.setToolTip(tr("隐藏到托盘"))
        hide.setAccessibleName(tr("隐藏到托盘"))
        hide.clicked.connect(self.hide)
        header_layout.addWidget(self.add_button)
        header_layout.addWidget(refresh)
        header_layout.addWidget(settings)
        header_layout.addWidget(hide)
        content.addWidget(header)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.scroll.viewport().setAutoFillBackground(False)
        self.list_widget = QWidget()
        self.list_widget.setObjectName("accountList")
        self.list_widget.setStyleSheet("#accountList {background:transparent;}")
        self.list_layout = QVBoxLayout(self.list_widget)
        self.list_layout.setContentsMargins(0, 0, 0, 0)
        self.list_layout.setSpacing(9)
        self.empty_add_button = QPushButton(tr("+ 添加账号"))
        self.empty_add_button.setObjectName("smallButton")
        self.empty_add_button.clicked.connect(self.show_add_menu)
        self.list_layout.addWidget(self.empty_add_button, alignment=Qt.AlignCenter)
        self.list_layout.addStretch()
        self.scroll.setWidget(self.list_widget)
        content.addWidget(self.scroll)
        self.note = QLabel("")
        self.note.setObjectName("subtle")
        self.note.setTextFormat(Qt.PlainText)
        self.note.setWordWrap(True)
        self.note.hide()
        login_status = QHBoxLayout()
        login_status.addWidget(self.note, 1)
        self.cancel_login_button = QPushButton(tr("取消"))
        self.cancel_login_button.setObjectName("smallButton")
        self.cancel_login_button.setToolTip(tr("取消本次账号授权"))
        self.cancel_login_button.clicked.connect(self.cancel_login)
        self.cancel_login_button.hide()
        login_status.addWidget(self.cancel_login_button)
        content.addLayout(login_status)
        self.login_timer = QTimer(self)
        self.login_timer.setInterval(1000)
        self.login_timer.timeout.connect(self._update_login_note)
        QApplication.instance().aboutToQuit.connect(self.cancel_login)
        self._build_cards()
        edges_and_cursors = (
            (Qt.LeftEdge, Qt.SizeHorCursor), (Qt.RightEdge, Qt.SizeHorCursor),
            (Qt.TopEdge, Qt.SizeVerCursor), (Qt.BottomEdge, Qt.SizeVerCursor),
            (Qt.LeftEdge | Qt.TopEdge, Qt.SizeFDiagCursor),
            (Qt.RightEdge | Qt.TopEdge, Qt.SizeBDiagCursor),
            (Qt.LeftEdge | Qt.BottomEdge, Qt.SizeBDiagCursor),
            (Qt.RightEdge | Qt.BottomEdge, Qt.SizeFDiagCursor),
        )
        self._resize_handles = [ResizeHandle(self, edges, cursor) for edges, cursor in edges_and_cursors]
        width, height = load_window_size((360, min(740, 80 + 101 * max(1, len(self.profiles)))))
        available = self.screen().availableGeometry()
        self.resize(min(width, available.width()), min(height, available.height()))
        self._position_resize_handles()
        self._size_timer = QTimer(self)
        self._size_timer.setSingleShot(True)
        self._size_timer.setInterval(250)
        self._size_timer.timeout.connect(self._save_window_size)
        self._window_size_ready = True
        QApplication.instance().aboutToQuit.connect(self._save_window_size)
        self._build_tray()
        self.poll_timer = QTimer(self)
        self.poll_timer.timeout.connect(self.refresh_all)
        self.poll_timer.start(self.preferences.refresh_minutes * 60_000)
        QTimer.singleShot(250, self.refresh_all)

    def _build_cards(self) -> None:
        for card in self.cards.values():
            self.list_layout.removeWidget(card)
            card.hide()
            card.deleteLater()
        self.cards.clear()
        visible_profiles = [p for p in self.profiles if p.managed or
                            (p.provider == "codex" and self.active_id == "current") or
                            (p.provider == "claude" and self.claude_active_id == "claude-current")]
        self.empty_add_button.setVisible(not visible_profiles)
        has_current_login = account_switch.identity_or_none(self.default_home()) is not None
        has_claude_login = claude_provider.local_login_available(self.claude_home()) and claude_provider.identity(self.claude_home()) is not None
        for index, profile in enumerate(visible_profiles):
            colors = self.preferences.colors()
            active = (profile.id == self.claude_active_id and has_claude_login) if profile.provider == "claude" else (profile.id == self.active_id and has_current_login)
            card = AccountCard(profile, colors["accent"], self.show_card_menu, colors["track"],
                               active, provider=profile.provider, light=self.preferences.theme == "light",
                               saved_plan=claude_provider.saved_plan(Path(profile.home)) if profile.provider == "claude" else "")
            self.list_layout.insertWidget(index, card)
            self.cards[profile.id] = card
            card.update_data(self.quotas.get(profile.id), self.errors.get(profile.id, ""))
            if profile.id in self.running:
                card.status.setText("…")
                card.status.setToolTip(tr("正在刷新"))

    def show_settings(self) -> None:
        if self.switch_page is not None:
            self.showNormal()
            return
        if self.settings_page is None:
            self.settings_page = SettingsPage(self)
            self.pages.addWidget(self.settings_page)
        self.pages.setCurrentWidget(self.settings_page)
        self.showNormal()

    def show_monitor(self) -> None:
        self.pages.setCurrentWidget(self.monitor_page)
        if self.settings_page is not None:
            page = self.settings_page
            self.settings_page = None
            self.pages.removeWidget(page)
            page.hide()
            page.deleteLater()
        self.setStyleSheet(stylesheet(self.preferences))
        self.setWindowOpacity(self.preferences.opacity / 100)

    def default_home(self) -> Path:
        return Path(next((p.home for p in self.profiles if not p.managed and p.provider == "codex"), str(DEFAULT_HOME)))

    def claude_home(self) -> Path:
        return Path(next((p.home for p in self.profiles if not p.managed and p.provider == "claude"), str(claude_provider.DEFAULT_HOME)))

    def show_switch(self, profile: Profile) -> None:
        if self.switch_page is not None:
            return
        self.show_monitor()
        self.switch_page = SwitchPage(self, profile)
        self.pages.addWidget(self.switch_page)
        self.pages.setCurrentWidget(self.switch_page)

    def cancel_switch(self) -> None:
        if "switch" in self.running:
            return
        if self.switch_page is not None:
            page = self.switch_page
            self.switch_page = None
            page.timer.stop()
            self.pages.setCurrentWidget(self.monitor_page)
            self.pages.removeWidget(page)
            page.hide()
            page.deleteLater()
        self.refresh_all()

    def apply_preferences(self) -> None:
        set_language(self.preferences.language)
        self.retranslate()
        geometry = self.geometry()
        visible = self.isVisible()
        self.setStyleSheet(stylesheet(self.preferences))
        self.settings_button.setIcon(settings_icon(self.preferences.colors()["muted"]))
        self.setWindowOpacity(self.preferences.opacity / 100)
        self.setWindowFlag(Qt.WindowStaysOnTopHint, self.preferences.topmost)
        self.setGeometry(geometry)
        if visible:
            self.show()
        self.poll_timer.start(self.preferences.refresh_minutes * 60_000)
        self._build_cards()

    def retranslate(self) -> None:
        self.refresh_button.setText(tr("刷新"))
        for widget, label in ((self.add_button, "添加账号"), (self.settings_button, "设置"),
                              (self.hide_button, "隐藏到托盘")):
            widget.setToolTip(tr(label))
            widget.setAccessibleName(tr(label))
        self.refresh_button.setToolTip(tr("刷新所有账号额度"))
        self.empty_add_button.setText(tr("+ 添加账号"))
        self.cancel_login_button.setText(tr("取消"))
        self.cancel_login_button.setToolTip(tr("取消本次账号授权"))
        if "add" in self.running:
            if self._login_cancel is not None and self._login_cancel.is_set():
                self.note.setText(tr("正在取消…"))
            else:
                self._update_login_note()
        else:
            self.note.setText(tr(self.note.text()))
        for action, text in getattr(self, "tray_actions", []):
            action.setText(tr(text))

    def _position_resize_handles(self) -> None:
        if not self._resize_handles:
            return
        width, height, edge, corner = self.width(), self.height(), 8, 14
        rectangles = (
            (0, corner, edge, height - 2 * corner),
            (width - edge, corner, edge, height - 2 * corner),
            (corner, 0, width - 2 * corner, edge),
            (corner, height - edge, width - 2 * corner, edge),
            (0, 0, corner, corner), (width - corner, 0, corner, corner),
            (0, height - corner, corner, corner),
            (width - corner, height - corner, corner, corner),
        )
        for handle, rectangle in zip(self._resize_handles, rectangles):
            handle.setGeometry(*rectangle)
            handle.raise_()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._position_resize_handles()
        if self._window_size_ready and self._interaction != "move":
            self._size_timer.start()

    def _begin_interaction(self, kind: str) -> None:
        self._finish_interaction()
        self._interaction = kind
        if kind == "move":
            self._move_size = QSize(self.size())
            self._old_limits = (QSize(self.minimumSize()), QSize(self.maximumSize()))
            # A floating monitor must not grow via Aero Snap or a DPI transition during a move.
            self.setFixedSize(self._move_size)
        if self._window_size_ready:
            self._size_timer.stop()

    def _finish_interaction(self) -> None:
        if self._interaction is None:
            return
        kind = self._interaction
        self._interaction = None
        if kind == "move" and self._move_size is not None:
            size = self._move_size
            self._move_size = None
            self.setMinimumSize(self._old_limits[0])
            self.setMaximumSize(self._old_limits[1])
            self.resize(size)
        if self._window_size_ready:
            self._size_timer.start()

    def nativeEvent(self, event_type, message):
        if sys.platform == "win32":
            import ctypes
            from ctypes import wintypes
            native = wintypes.MSG.from_address(int(message))
            if native.message == 0x0232:  # WM_EXITSIZEMOVE: native mouse release is not a Qt mouse event.
                QTimer.singleShot(0, self._finish_interaction)
        return super().nativeEvent(event_type, message)

    def showEvent(self, event) -> None:
        super().showEvent(event)
        window = self.windowHandle()
        if window and window is not self._watched_window:
            self._watched_window = window
            window.screenChanged.connect(self._screen_changed)

    def _screen_changed(self, screen) -> None:
        # Force a fresh paint at the new DPR; no cached shadow pixmap crosses monitors.
        self.update()
        self.shell.update()

    def _save_window_size(self) -> None:
        if not self._window_size_ready or self._interaction == "move":
            return
        try:
            APP_DIR.mkdir(parents=True, exist_ok=True)
            temporary = WINDOW_FILE.with_suffix(".tmp")
            temporary.write_text(json.dumps({"width": self.width(), "height": self.height()}), encoding="utf-8")
            temporary.replace(WINDOW_FILE)
        except OSError:
            pass

    def _build_tray(self) -> None:
        self.tray = QSystemTrayIcon(QIcon(str(APP_ICON)), self)
        self.tray.setToolTip(f"{APP_NAME} {__version__}")
        menu = QMenu()
        show_action = QAction(tr("显示悬浮窗"), self)
        show_action.triggered.connect(self.showNormal)
        refresh_action = QAction(tr("刷新额度"), self)
        refresh_action.triggered.connect(self.refresh_all)
        quit_action = QAction(tr("退出"), self)
        quit_action.triggered.connect(QApplication.instance().quit)
        menu.addAction(show_action)
        menu.addAction(refresh_action)
        settings_action = menu.addAction(tr("设置"))
        settings_action.triggered.connect(self.show_settings)
        licenses_action = menu.addAction(tr("开源许可"))
        licenses_action.triggered.connect(lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(
            RESOURCE_ROOT / ("licenses" if getattr(sys, "frozen", False) else "docs/licenses") / "index.html"
        ))))
        menu.addSeparator()
        menu.addAction(quit_action)
        self.tray.setContextMenu(menu)
        self.tray_actions = [(show_action, "显示悬浮窗"), (refresh_action, "刷新额度"),
                             (settings_action, "设置"), (licenses_action, "开源许可"), (quit_action, "退出")]
        self.tray.activated.connect(lambda reason: self.showNormal() if reason == QSystemTrayIcon.Trigger else None)
        self.tray.show()

    def refresh_all(self) -> None:
        if self.switch_page is not None:
            return
        detected = load_profiles()
        # Retain already working Claude accounts while their worker renews tokens.
        known_ids = {p.id for p in detected}
        detected.extend(p for p in self.profiles if not p.managed and p.provider == "claude" and
                        p.id not in known_ids and p.id in self.quotas and
                        not any(text in self.errors.get(p.id, "") for text in ("授权已失效", "授权已过期", "未找到 Claude")) and
                        claude_provider.identity(Path(p.home)) is not None)
        if detected != self.profiles:
            self.profiles = detected
            save_profiles(self.profiles)
            self._build_cards()
        identities = {p.id: (claude_provider.identity(Path(p.home)) if p.provider == "claude" else
                             account_switch.identity_or_none(Path(p.home))) for p in self.profiles}
        current_identity = account_switch.identity_or_none(self.default_home())
        claude_identity = claude_provider.identity(self.claude_home()) if claude_provider.local_login_available(self.claude_home()) else None
        active = next((p.id for p in self.profiles if p.provider == "codex" and p.managed and current_identity and identities[p.id] == current_identity), "current")
        claude_active = next((p.id for p in self.profiles if p.provider == "claude" and p.managed and claude_identity and identities[p.id] == claude_identity), "claude-current")
        if active != self.active_id or claude_active != self.claude_active_id:
            self.active_id = active
            self.claude_active_id = claude_active
            self._build_cards()
        seen = {}
        for profile in self.profiles:
            if not profile.managed and ((profile.provider == "codex" and active != "current") or
                                        (profile.provider == "claude" and claude_active != "claude-current")):
                continue
            raw_identity = identities[profile.id]
            identity = (profile.provider, raw_identity) if raw_identity else None
            if identity and identity in seen:
                key = seen[identity]
                self._query_followers.setdefault(key, [key]).append(profile.id)
                self.running.add(profile.id)
                continue
            if identity:
                seen[identity] = profile.id
            if profile.id in self.running:
                continue
            self._query_followers[profile.id] = [profile.id]
            self.running.add(profile.id)
            card = self.cards.get(profile.id)
            if card:
                card.status.setText("…")
                card.status.setToolTip(tr("正在刷新"))
            if profile.provider == "claude":
                home = profile.home
                task = Task(profile.id, read_claude_quota, home)
            else:
                home = str(self.default_home()) if raw_identity and raw_identity == current_identity else profile.home
                task = Task(profile.id, read_quota, home)
            task.signals.finished.connect(self._task_done)
            self.pool.start(task)

    def _task_done(self, key: str, result: object, error: str) -> None:
        self.running.discard(key)
        if key == "switch":
            self.profiles = load_profiles()
            self.quotas.clear()
            self.errors.clear()
            if isinstance(result, list):
                self.active_id = self.switch_page.profile.id if self.switch_page else "current"
                self._build_cards()
                if self.switch_page:
                    self.switch_page.succeeded()
            elif self.switch_page:
                self.switch_page.failed(error)
            return
        if key == "add":
            cancelled = self._login_cancel is not None and self._login_cancel.is_set()
            self._login_cancel = None
            self.login_timer.stop()
            self.cancel_login_button.hide()
            self.add_button.setEnabled(True)
            if cancelled:
                # A successful worker result may already be queued when Cancel is clicked.
                if isinstance(result, Profile):
                    home = account_switch.managed_home(asdict(result), ACCOUNTS_DIR)
                    shutil.rmtree(home, ignore_errors=True)
                self.note.setText(tr("已取消"))
            elif isinstance(result, Profile):
                self.note.hide()
                self.profiles.append(result)
                save_profiles(self.profiles)
                self._build_cards()
                self.refresh_all()
            else:
                self.note.setText(tr(error or "授权未完成，请重新添加。"))
            return
        for follower in self._query_followers.pop(key, [key]):
            self.running.discard(follower)
            card = self.cards.get(follower)
            if isinstance(result, Quota):
                self.errors.pop(follower, None)
                self.quotas[follower] = result
                if card:
                    card.update_data(result)
            else:
                self.errors[follower] = error
                if card:
                    card.update_data(None, error)

    def show_add_menu(self) -> None:
        menu = QMenu(self)
        menu.setObjectName("addAccountMenu")
        menu.setFixedWidth(144)
        menu.setWindowFlags(Qt.Popup | Qt.FramelessWindowHint | Qt.NoDropShadowWindowHint)
        menu.setAttribute(Qt.WA_TranslucentBackground)
        codex = menu.addAction(QIcon(provider_pixmap("codex", self.preferences.theme == "light", self.devicePixelRatioF())), "Codex")
        claude = menu.addAction(QIcon(provider_pixmap("claude", self.preferences.theme == "light", self.devicePixelRatioF())), "Claude Code")
        anchor = self.empty_add_button if self.sender() is self.empty_add_button else self.add_button
        position = anchor.mapToGlobal(anchor.rect().bottomLeft())
        position.setY(position.y() + 5)
        action = menu.exec(position)
        menu.deleteLater()
        if action in (codex, claude):
            self.add_account("claude" if action == claude else "codex")

    def add_account(self, provider: str = "codex") -> None:
        if "add" in self.running or self.switch_page is not None:
            return
        self.running.add("add")
        self._login_cancel = threading.Event()
        self._login_deadline = time.monotonic() + LOGIN_TIMEOUT_SECONDS
        self._update_login_note()
        self.note.show()
        self.cancel_login_button.setEnabled(True)
        self.cancel_login_button.show()
        self.login_timer.start()
        self.add_button.setEnabled(False)
        task = Task("add", login_new_account, str(uuid.uuid4()), self._login_cancel, self._login_deadline, provider)
        task.signals.finished.connect(self._task_done)
        self.pool.start(task)

    def _update_login_note(self) -> None:
        if self._login_cancel is None or self._login_cancel.is_set():
            return
        seconds = max(0, int(self._login_deadline - time.monotonic()))
        self.note.setText(tr("等待授权 · {time}", time=f"{seconds // 60}:{seconds % 60:02d}"))

    def cancel_login(self) -> None:
        if self._login_cancel is not None:
            self._login_cancel.set()
            self.login_timer.stop()
            self.cancel_login_button.setEnabled(False)
            self.note.setText(tr("正在取消…"))

    def show_card_menu(self, profile: Profile, button: QPushButton) -> None:
        menu = QMenu(self)
        switch = menu.addAction(tr("切换到此账号")) if profile.managed and profile.provider == "codex" else None
        reauth = menu.addAction(tr("重新添加账号")) if profile.provider == "claude" else None
        if switch:
            switch.setEnabled(profile.id != self.active_id)
        rename = menu.addAction(tr("重命名"))
        remove = menu.addAction(tr("移除账号")) if profile.managed else None
        if remove:
            remove.setEnabled(profile.id not in self.running and profile.id not in (self.active_id, self.claude_active_id))
        action = menu.exec(button.mapToGlobal(button.rect().bottomLeft()))
        if switch and action == switch:
            self.show_switch(profile)
        elif reauth and action == reauth:
            self.add_account("claude")
        elif action == rename:
            dialog = QInputDialog(self)
            dialog.setWindowTitle(tr("账号名称"))
            dialog.setLabelText(tr("名称"))
            dialog.setTextValue(profile.name)
            dialog.setOkButtonText(tr("确定"))
            dialog.setCancelButtonText(tr("取消"))
            accepted = dialog.exec()
            name = dialog.textValue()
            dialog.deleteLater()
            if accepted and name.strip():
                profile.name = name.strip()[:40]
                save_profiles(self.profiles)
                self.cards[profile.id].name.setText(profile.name)
                self.cards[profile.id].name.setMaximumWidth(self.cards[profile.id].name.sizeHint().width() + 2)
                self.cards[profile.id].name.setToolTip(profile.name)
        elif remove and action == remove:
            dialog = QMessageBox(QMessageBox.Question, tr("移除账号"),
                                tr("移除这个账号并删除本程序保存的授权？\n不会退出 Codex 当前账号。"),
                                QMessageBox.Yes | QMessageBox.No, self)
            dialog.button(QMessageBox.Yes).setText(tr("是"))
            dialog.button(QMessageBox.No).setText(tr("否"))
            dialog.setDefaultButton(QMessageBox.No)
            answer = dialog.exec()
            dialog.deleteLater()
            if answer != QMessageBox.Yes:
                return
            home = Path(profile.home)
            if home.name != profile.id or home.is_symlink() or home.parent.resolve() != ACCOUNTS_DIR.resolve():
                QMessageBox.warning(self, tr("无法移除"), tr("账号目录未通过安全检查。"))
                return
            shutil.rmtree(home, ignore_errors=False)
            self.profiles = [item for item in self.profiles if item.id != profile.id]
            self.quotas.pop(profile.id, None)
            self.errors.pop(profile.id, None)
            save_profiles(self.profiles)
            self._build_cards()

def main() -> None:
    QApplication.setAttribute(Qt.AA_EnableHighDpiScaling, True)
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setWindowIcon(QIcon(str(APP_ICON)))
    app.setQuitOnLastWindowClosed(False)
    APP_DIR.mkdir(parents=True, exist_ok=True)
    instance_lock = QLockFile(str(APP_DIR / "instance.lock"))
    # Match older releases when reusing their store, so only one app owns it.
    prefix = "SafeQuota" if APP_DIR.name == "SafeQuota" else APP_NAME
    server_name = prefix + "-" + hashlib.sha256(str(APP_DIR.resolve()).lower().encode()).hexdigest()[:20]
    if not instance_lock.tryLock(0):
        socket = QLocalSocket()
        socket.connectToServer(server_name)
        if socket.waitForConnected(1500):
            socket.write(b"show")
            socket.waitForBytesWritten(1000)
            socket.disconnectFromServer()
        return
    server = QLocalServer()
    server.setSocketOptions(QLocalServer.UserAccessOption)
    QLocalServer.removeServer(server_name)
    server.listen(server_name)
    window = Monitor()

    def show_existing() -> None:
        while server.hasPendingConnections():
            socket = server.nextPendingConnection()
            socket.disconnectFromServer()
            socket.deleteLater()
        window.showNormal()
        window.raise_()
        window.activateWindow()

    server.newConnection.connect(show_existing)
    window.show()
    try:
        app.exec()
    finally:
        server.close()
        instance_lock.unlock()


if __name__ == "__main__":
    main()
