"""Claude subscription usage. Credentials are sent only to fixed Anthropic endpoints."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import stat
import threading
import time
import urllib.error
import urllib.request

from .account_switch import atomic_private_write

DEFAULT_HOME = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude").resolve()
USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
TOKEN_URL = "https://platform.claude.com/v1/oauth/token"
CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
_cache: dict[str, tuple[float, str, dict]] = {}
_retry_after: dict[str, float] = {}
_last_token: dict[str, str] = {}
_query_lock = threading.Lock()


class ClaudeError(RuntimeError):
    pass


class RateLimited(ClaudeError):
    def __init__(self, seconds=300):
        super().__init__("Claude 查询限流，请稍后刷新。")
        self.seconds = seconds


def plan_label(oauth: dict) -> str:
    subscription = oauth.get("subscriptionType")
    if subscription == "max":
        return {"default_claude_max_5x": "Max 5×", "default_claude_max_20x": "Max 20×"}.get(
            oauth.get("rateLimitTier") if isinstance(oauth.get("rateLimitTier"), str) else "", "Max")
    return {"free": "Free", "pro": "Pro", "team": "Team", "enterprise": "Enterprise"}.get(
        subscription if isinstance(subscription, str) else "", "")


def saved_plan(home: Path) -> str:
    try:
        return plan_label(credentials(home)[2])
    except ClaudeError:
        return ""


def local_login_available(home: Path) -> bool:
    """An abandoned/expired credential file alone must not create a UI account."""
    try:
        _, _, oauth = credentials(home)
        expiry = oauth.get("expiresAt")
        return type(expiry) in (int, float) and math.isfinite(expiry) and expiry > time.time() * 1000
    except ClaudeError:
        return False


def binary() -> str:
    candidates = [
        shutil.which("claude.exe"),
        Path.home() / ".local/bin/claude.exe",
        Path(os.environ.get("APPDATA", "")) / "npm/node_modules/@anthropic-ai/claude-code/bin/claude.exe",
    ]
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return str(candidate)
    raise ClaudeError("找不到 Claude Code，请先安装官方 CLI。")


def login_environment(home: Path) -> dict[str, str]:
    env = os.environ.copy()
    # A newly added subscription must not inherit API-key or custom-provider routing.
    for key in tuple(env):
        if key.startswith("ANTHROPIC_") or key.startswith("CLAUDE_CODE_USE_") or key in (
            "CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN_FILE_DESCRIPTOR",
            "CLAUDE_CODE_CUSTOM_OAUTH_URL", "CLAUDE_CODE_API_KEY_HELPER_TTL_MS",
        ):
            env.pop(key)
    env["CLAUDE_CONFIG_DIR"] = str(home)
    return env


def _read_file(path: Path) -> bytes:
    info = path.lstat()
    if path.is_symlink() or getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
        raise ClaudeError("Claude 授权路径不能是链接。")
    if info.st_size > 2_000_000:
        raise ClaudeError("Claude 授权文件格式不支持。")
    return path.read_bytes()


def credentials(home: Path) -> tuple[bytes, dict, dict]:
    try:
        if home.is_symlink() or getattr(home.stat(), "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
            raise ClaudeError("Claude 授权目录不能是链接。")
        raw = _read_file(home / ".credentials.json")
        document = json.loads(raw)
        oauth = document["claudeAiOauth"]
        if not isinstance(oauth.get("accessToken"), str) or not oauth["accessToken"]:
            raise ValueError()
        if "user:profile" not in oauth.get("scopes", []):
            raise ClaudeError("此授权不支持订阅额度，请使用 Claude 订阅登录。")
        if oauth.get("clientId", CLIENT_ID) != CLIENT_ID:
            raise ClaudeError("此 Claude 登录客户端暂不支持，请通过官方 CLI 登录。")
        return raw, document, oauth
    except ClaudeError:
        raise
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        raise ClaudeError("未找到 Claude 订阅授权，请先登录。") from None


def identity(home: Path) -> str | None:
    try:
        # The default CLI stores account metadata alongside ~/.claude, while
        # CLAUDE_CONFIG_DIR stores it inside the specified directory.
        metadata = home / ".claude.json"
        if home.resolve() == (Path.home() / ".claude").resolve() and not os.environ.get("CLAUDE_CONFIG_DIR"):
            metadata = Path.home() / ".claude.json"
        account = json.loads(_read_file(metadata)).get("oauthAccount", {})
        key = [account["accountUuid"], account.get("organizationUuid", "")]
        if not isinstance(key[0], str) or not key[0]:
            return None
        credentials(home)
        return hashlib.sha256(json.dumps(key).encode()).hexdigest()
    except (ClaudeError, OSError, ValueError, KeyError, TypeError, AttributeError):
        return None


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def _request(url: str, *, token: str | None = None, payload: dict | None = None) -> dict:
    if url not in (USAGE_URL, TOKEN_URL):
        raise ClaudeError("请求地址不受支持。")
    headers = {"Accept": "application/json", "User-Agent": "Gauge/0.1"}
    if token:
        headers.update({"Authorization": f"Bearer {token}", "anthropic-beta": "oauth-2025-04-20"})
    body = None
    if payload is not None:
        body = json.dumps(payload).encode()
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=body, headers=headers)
    try:
        with urllib.request.build_opener(_NoRedirect()).open(request, timeout=20) as response:
            raw = response.read(1_000_001)
        if len(raw) > 1_000_000:
            raise ValueError()
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError()
        return data
    except urllib.error.HTTPError as error:
        code = error.code
        retry = error.headers.get("Retry-After", "300") if error.headers else "300"
        error.close()
        if code == 429:
            seconds = min(86400, max(300, int(retry))) if retry.isdigit() else 300
            raise RateLimited(seconds) from None
        if code in (400, 401, 403):
            raise ClaudeError("Claude 授权已失效，请重新授权。") from None
        raise ClaudeError("Claude 额度服务暂不可用。") from None
    except (OSError, ValueError, urllib.error.URLError):
        raise ClaudeError("无法连接 Claude 额度服务，请稍后重试。") from None


@contextmanager
def _refresh_lock(home: Path):
    # Compatible with the official CLI's proper-lockfile directory and heartbeat.
    lock = home / ".oauth_refresh.lock"
    for attempt in range(2):
        try:
            lock.mkdir()
            break
        except FileExistsError:
            info = lock.lstat()
            if attempt == 0 and not lock.is_symlink() and not getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT and time.time() - info.st_mtime > 60:
                try:
                    # Empty, abandoned protocol locks only; never remove lock contents.
                    if lock.stat().st_ino == info.st_ino and lock.stat().st_mtime == info.st_mtime:
                        lock.rmdir()
                        continue
                except OSError:
                    pass
            raise ClaudeError("Claude 正在刷新授权，请稍后重试。") from None
    inode = lock.stat().st_ino
    stop = threading.Event()
    compromised = threading.Event()

    def heartbeat():
        while not stop.wait(2):
            try:
                if lock.stat().st_ino != inode:
                    raise OSError()
                os.utime(lock, None)
            except OSError:
                compromised.set()
                return

    thread = threading.Thread(target=heartbeat, daemon=True)
    thread.start()
    try:
        def check_lock():
            if compromised.is_set() or lock.stat().st_ino != inode:
                raise ClaudeError("Claude 授权刷新锁已改变，请重试。")
        yield check_lock
    finally:
        stop.set()
        thread.join(timeout=3)
        try:
            if lock.stat().st_ino == inode:
                lock.rmdir()
        except OSError:
            pass


def _valid_token(home: Path) -> str:
    _, _, oauth = credentials(home)
    expiry = oauth.get("expiresAt")
    if type(expiry) not in (int, float) or not math.isfinite(expiry):
        raise ClaudeError("Claude 授权缺少有效期，请重新授权。")
    if expiry > (time.time() + 60) * 1000:
        return oauth["accessToken"]
    with _refresh_lock(home) as check_lock:
        raw, document, oauth = credentials(home)
        if oauth.get("expiresAt", 0) > (time.time() + 60) * 1000:
            return oauth["accessToken"]
        refresh = oauth.get("refreshToken")
        if not isinstance(refresh, str) or not refresh:
            raise ClaudeError("Claude 授权已过期，请重新授权。")
        result = _request(TOKEN_URL, payload={
            "grant_type": "refresh_token", "refresh_token": refresh,
            "client_id": CLIENT_ID, "scope": " ".join(oauth["scopes"]),
        })
        access = result.get("access_token")
        duration = result.get("expires_in")
        rotated = result.get("refresh_token", refresh)
        if not isinstance(access, str) or not access or not isinstance(rotated, str) or not rotated:
            raise ClaudeError("Claude 刷新授权返回异常，请重新授权。")
        if type(duration) not in (int, float) or not math.isfinite(duration) or not 0 < duration <= 366 * 86400:
            raise ClaudeError("Claude 刷新授权返回异常，请重新授权。")
        scope = result.get("scope")
        oauth.update(accessToken=access, refreshToken=rotated, expiresAt=int((time.time() + duration) * 1000))
        if isinstance(scope, str):
            oauth["scopes"] = scope.split()

        def unchanged():
            check_lock()
            if _read_file(home / ".credentials.json") != raw:
                raise ClaudeError("Claude 登录已改变，请重新刷新。")

        unchanged()
        try:
            atomic_private_write(home / ".credentials.json", json.dumps(document).encode(), unchanged)
        except ClaudeError:
            raise
        except Exception:
            raise ClaudeError("无法安全保存 Claude 授权，请重新授权。") from None
        return access


def _window(value, minutes):
    if value is None:
        return None
    try:
        used = value["utilization"]
        if type(used) not in (int, float) or not math.isfinite(used) or not 0 <= used <= 100:
            raise ValueError()
        reset = value.get("resets_at")
        stamp = None
        if reset is not None:
            date = datetime.fromisoformat(reset.replace("Z", "+00:00"))
            if date.tzinfo is None:
                raise ValueError()
            stamp = int(date.timestamp())
        return {"used": used, "minutes": minutes, "reset": stamp}
    except (TypeError, ValueError, KeyError, AttributeError, OverflowError):
        raise ClaudeError("Claude 返回的额度格式不支持。") from None


def read_usage(home: str) -> dict:
    path = Path(home)
    key = str(path.resolve()).casefold()
    with _query_lock:
        try:
            _, _, current = credentials(path)
            fingerprint = hashlib.sha256(current["accessToken"].encode()).hexdigest()
            if _last_token.get(key) != fingerprint:
                _cache.pop(key, None)
                _retry_after.pop(key, None)
                _last_token[key] = fingerprint
            cached = _cache.get(key)
            if cached and cached[0] > time.monotonic() and cached[1] == fingerprint:
                return cached[2]
            if _retry_after.get(key, 0) > time.monotonic():
                raise ClaudeError("Claude 查询冷却中，请稍后刷新。")
            token = _valid_token(path)
            response = _request(USAGE_URL, token=token)
            result = {"five": _window(response.get("five_hour"), 300),
                      "week": _window(response.get("seven_day"), 10080), "checked_at": time.time(),
                      "plan": saved_plan(path)}
            if result["five"] is None and result["week"] is None:
                raise ClaudeError("此 Claude 账号未返回订阅额度。")
            fresh_fingerprint = hashlib.sha256(token.encode()).hexdigest()
            _cache[key] = (time.monotonic() + 300, fresh_fingerprint, result)
            _last_token[key] = fresh_fingerprint
            _retry_after.pop(key, None)
            return result
        except RateLimited as error:
            _retry_after[key] = time.monotonic() + error.seconds
            raise
        except ClaudeError:
            _retry_after[key] = max(_retry_after.get(key, 0), time.monotonic() + 60)
            raise
        except Exception:
            _retry_after[key] = time.monotonic() + 60
            raise ClaudeError("Claude 查询失败，请稍后重试。") from None
