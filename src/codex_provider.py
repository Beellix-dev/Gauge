"""Official Codex CLI discovery and quota RPC; no Qt dependency."""

from __future__ import annotations

import json
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

from .models import Quota, Window


def binary() -> str:
    binary = shutil.which("codex.exe") or shutil.which("codex")
    if not binary and sys.platform == "win32":
        # Windows startup does not inherit the Codex desktop terminal's PATH.
        installed = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / "OpenAI" / "Codex" / "bin"
        candidates = [path for path in installed.glob("*/codex.exe") if path.is_file()]
        if candidates:
            binary = str(max(candidates, key=lambda path: path.stat().st_mtime_ns))
    if not binary:
        raise RuntimeError("找不到 Codex CLI。请先安装或打开 Codex 桌面应用。")
    return binary



def parse_window(value: Any) -> Window | None:
    if not isinstance(value, dict):
        return None
    try:
        used = float(value["usedPercent"])
        minutes = int(value["windowDurationMins"])
        reset_raw = value.get("resetsAt")
        reset = int(reset_raw) if reset_raw is not None else None
    except (KeyError, ValueError, TypeError):
        return None
    return Window(max(0, min(100, used)), minutes, reset)



def parse_quota(result: Any) -> Quota:
    if not isinstance(result, dict):
        raise RuntimeError("Codex 返回了无法识别的额度数据。")
    grouped = result.get("rateLimitsByLimitId") or {}
    limits = grouped.get("codex") if isinstance(grouped, dict) else None
    if not isinstance(limits, dict):
        limits = result.get("rateLimits") or {}
    windows = [parse_window(limits.get(key)) for key in ("primary", "secondary")]
    windows = [window for window in windows if window]
    five = next((window for window in windows if window.minutes == 300), None)
    week = next((window for window in windows if window.minutes == 10080), None)
    if five is None and week is None and windows:
        shortest = sorted(windows, key=lambda window: window.minutes)
        five = shortest[0]
        week = shortest[-1] if len(shortest) > 1 else None
    if five is None and week is None:
        raise RuntimeError("此账号没有返回可显示的额度窗口。")
    plan_names = {
        "free": "Free", "go": "Go", "plus": "Plus", "pro": "Pro", "prolite": "Pro Lite",
        "team": "Team", "business": "Business", "self_serve_business_prolite": "Business",
        "self_serve_business_usage_based": "Business", "ent26": "Enterprise",
        "enterprise": "Enterprise", "enterprise_cbp_automation": "Enterprise",
        "enterprise_cbp_usage_based": "Enterprise", "edu": "Edu", "edu_plus": "Edu Plus", "edu_pro": "Edu Pro",
    }
    plan_type = limits.get("planType")
    plan = plan_names.get(plan_type, "") if isinstance(plan_type, str) else ""
    return Quota(five, week, time.time(), plan)



def read_quota(home: str, work_dir: Path) -> Quota:
    """Use only official Codex app-server RPC; no token is read by this process."""
    env = os.environ.copy()
    env["CODEX_HOME"] = home
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    process = subprocess.Popen(
        [binary(), "app-server", "--stdio"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        text=True, encoding="utf-8", errors="replace", env=env,
        cwd=work_dir, creationflags=creationflags,
    )
    messages: queue.Queue[str] = queue.Queue()
    threading.Thread(target=lambda: [messages.put(line) for line in process.stdout], daemon=True).start()

    def send(payload: dict[str, Any]) -> None:
        assert process.stdin is not None
        process.stdin.write(json.dumps(payload, separators=(",", ":")) + "\n")
        process.stdin.flush()

    def receive(request_id: int, timeout: int) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                line = messages.get(timeout=max(0.1, deadline - time.monotonic()))
                packet = json.loads(line)
            except queue.Empty:
                break
            except ValueError:
                continue
            if packet.get("id") == request_id:
                return packet
        raise RuntimeError("Codex 查询超时。")

    try:
        send({"id": 1, "method": "initialize", "params": {"clientInfo": {"name": "gauge", "version": "0.1.0"}, "capabilities": {}}})
        initialized = receive(1, 10)
        if "error" in initialized:
            raise RuntimeError("Codex 服务初始化失败。")
        send({"method": "initialized", "params": {}})
        send({"id": 2, "method": "account/rateLimits/read", "params": {}})
        answer = receive(2, 25)
        if "error" in answer:
            raise RuntimeError("查询失败。请确认此账号已通过 Codex 登录。")
        return parse_quota(answer.get("result"))
    finally:
        process.terminate()
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=2)
