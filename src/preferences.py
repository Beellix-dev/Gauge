"""Local appearance preferences and the current user's Windows startup entry."""

from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from .i18n import LANGUAGES

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_NAME = "Gauge"
LEGACY_RUN_NAME = "SafeQuota"
THEMES = {
    "night": {"name": "深海", "bg": "#0E1726", "card": "#162337", "border": "#293D55", "text": "#EDF4FA", "muted": "#ACBED0", "track": "#2A3B50", "hover": "#243D52"},
    "graphite": {"name": "石墨", "bg": "#191B20", "card": "#24272E", "border": "#3B404B", "text": "#F0F1F5", "muted": "#B3BAC7", "track": "#3A3E48", "hover": "#363B45"},
    "light": {"name": "月白", "bg": "#F4F6FA", "card": "#FFFFFF", "border": "#D9E0E9", "text": "#202D40", "muted": "#586A80", "track": "#E3E9F0", "hover": "#E8EEF6"},
}
ACCENTS = {
    "mint": ("薄荷", "#69E1CD", "#087969"),
    "blue": ("晴蓝", "#8FC8FF", "#246BC5"),
    "purple": ("鸢尾", "#B8ABFF", "#7351BB"),
    "amber": ("琥珀", "#FFD08F", "#996015"),
}
INTERVALS = (1, 3, 5, 10, 15)


@dataclass
class Preferences:
    theme: str = "night"
    accent: str = "mint"
    opacity: int = 100
    topmost: bool = True
    refresh_minutes: int = 5
    language: str = "zh"

    @classmethod
    def load(cls, path: Path) -> "Preferences":
        result = cls()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return result
        if not isinstance(data, dict):
            return result
        if isinstance(data.get("theme"), str) and data["theme"] in THEMES:
            result.theme = data["theme"]
        if isinstance(data.get("accent"), str) and data["accent"] in ACCENTS:
            result.accent = data["accent"]
        if type(data.get("opacity")) is int:
            result.opacity = max(70, min(100, data["opacity"]))
        if type(data.get("topmost")) is bool:
            result.topmost = data["topmost"]
        if type(data.get("refresh_minutes")) is int and data["refresh_minutes"] in INTERVALS:
            result.refresh_minutes = data["refresh_minutes"]
        if isinstance(data.get("language"), str) and data["language"] in LANGUAGES:
            result.language = data["language"]
        return result

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(asdict(self), ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(path)

    def colors(self) -> dict[str, str]:
        return {**THEMES[self.theme], "accent": ACCENTS[self.accent][2 if self.theme == "light" else 1]}


def startup_command() -> str:
    executable = Path(sys.executable).resolve()
    if getattr(sys, "frozen", False):
        args = [str(executable)]
    else:
        windowed = executable.with_name("pythonw.exe")
        args = [str(windowed if windowed.exists() else executable), str(Path(__file__).resolve().parents[1] / "gauge.py")]
    # Run keys are command lines, not shell scripts. Quote paths containing spaces.
    return subprocess.list2cmdline(args)


def startup_entry() -> str | None:
    if sys.platform != "win32":
        return None
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            for name in (RUN_NAME, LEGACY_RUN_NAME):
                try:
                    value, kind = winreg.QueryValueEx(key, name)
                except FileNotFoundError:
                    continue
                if kind == winreg.REG_SZ:
                    return value
            return None
    except FileNotFoundError:
        return None


def write_startup_entry(command: str | None) -> None:
    if sys.platform != "win32":
        raise OSError("开机自启动仅支持 Windows。")
    import winreg
    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
        if command is None:
            try:
                winreg.DeleteValue(key, RUN_NAME)
            except FileNotFoundError:
                pass
        else:
            winreg.SetValueEx(key, RUN_NAME, 0, winreg.REG_SZ, command)
        # Retire the previous brand only after the new value has been written.
        try:
            winreg.DeleteValue(key, LEGACY_RUN_NAME)
        except FileNotFoundError:
            pass


def stylesheet(prefs: Preferences) -> str:
    c = prefs.colors()
    return """
        #shell {background:%(bg)s; border:1px solid %(border)s; border-radius:19px;}
        #card {background:%(card)s; border:1px solid %(border)s; border-radius:14px;}
        QLabel {color:%(text)s;}
        #heading {font-size:15px; font-weight:700;}
        #subtle {color:%(muted)s; font-size:10px;}
        #accountName {font-size:13px; font-weight:700;}
        #rowTitle {font-size:11px; color:%(muted)s;}
        QPushButton {background:%(hover)s; color:%(text)s; border:1px solid %(border)s; border-radius:8px; padding:6px 12px;}
        QPushButton:hover {border-color:%(accent)s;}
        QPushButton:focus {border:1px solid %(accent)s;}
        QPushButton:disabled {color:%(muted)s;}
        #iconButton {background:transparent; color:%(muted)s; border:0; padding:0; font-size:20px;}
        #iconButton:hover {color:%(accent)s;}
        #addButton {background:%(hover)s; color:%(accent)s; border:0; border-radius:8px; padding:0; font-size:19px;}
        #smallButton {background:transparent; color:%(muted)s; border:0; border-radius:8px; padding:4px 6px; font-size:11px;}
        #smallButton:hover {background:%(hover)s; color:%(text)s;}
        #smallButton:focus, #addButton:focus, #iconButton:focus {border:1px solid %(accent)s;}
        #saveButton {background:%(accent)s; color:%(bg)s; border:0; font-weight:700;}
        QScrollArea {background:transparent; border:0;}
        QScrollBar:vertical {background:transparent; width:6px;}
        QScrollBar::handle:vertical {background:%(border)s; border-radius:3px; min-height:20px;}
        QDialog, QMessageBox, QMenu {background:%(bg)s; color:%(text)s;}
        QMenu {border:1px solid %(border)s; padding:5px;}
        QMenu::item {padding:7px 18px; border-radius:5px;}
        QMenu::item:selected {background:%(hover)s;}
        QMenu#addAccountMenu {background:%(card)s; border:1px solid %(border)s; border-radius:10px; padding:4px; font-size:12px;}
        QMenu#addAccountMenu::item {padding:6px 10px; border-radius:6px;}
        QMenu#addAccountMenu::item:selected {background:%(hover)s; color:%(accent)s;}
        QComboBox, QLineEdit {background:%(card)s; color:%(text)s; border:1px solid %(border)s; border-radius:7px; padding:7px 10px; min-width:130px;}
        QComboBox:focus, QLineEdit:focus {border-color:%(accent)s;}
        QComboBox QAbstractItemView {background:%(card)s; color:%(text)s; selection-background-color:%(hover)s; selection-color:%(text)s;}
        QCheckBox {color:%(text)s; spacing:8px;}
        QCheckBox::indicator {width:17px; height:17px;}
        QSlider::groove:horizontal {height:5px; background:%(track)s; border-radius:2px;}
        QSlider::sub-page:horizontal {background:%(accent)s; border-radius:2px;}
        QSlider::handle:horizontal {background:%(accent)s; border:2px solid %(card)s; width:14px; margin:-6px 0; border-radius:9px;}
    """ % c
