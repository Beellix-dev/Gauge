"""Offline ChatGPT account switching. Never stop Codex or edit its workspace state."""
from __future__ import annotations

import base64
import ctypes
from ctypes import wintypes as wt
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import tomllib
import uuid


class SwitchError(RuntimeError):
    pass


@dataclass(repr=False)
class Auth:
    raw: bytes
    identity: str
    account_id: str


def read_auth(home: Path) -> Auth:
    path = home / "auth.json"
    try:
        if path.is_symlink() or path.stat().st_file_attributes & 0x400:
            raise SwitchError("授权文件不能是链接。")
        if path.stat().st_size > 2_000_000:
            raise SwitchError("授权文件格式不支持，请重新登录。")
        raw = path.read_bytes()
        data = json.loads(raw)
        if not isinstance(data, dict) or data.get("OPENAI_API_KEY"):
            raise ValueError()
        if data.get("auth_mode", "chatgpt") != "chatgpt":
            raise ValueError()
        tokens = data["tokens"]
        for name in ("access_token", "refresh_token", "id_token", "account_id"):
            if not isinstance(tokens.get(name), str) or not tokens[name]:
                raise ValueError()
        payload = tokens["id_token"].split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        subject = claims["sub"]
        if not isinstance(subject, str) or not subject:
            raise ValueError()
        # Used only to distinguish locally saved users/workspaces, never to authorize a request.
        identity = hashlib.sha256(json.dumps([subject, tokens["account_id"]]).encode()).hexdigest()
        return Auth(raw, identity, tokens["account_id"])
    except SwitchError:
        raise
    except (OSError, ValueError, KeyError, TypeError, IndexError, AttributeError):
        raise SwitchError("无法读取 ChatGPT 授权，请重新登录此账号。") from None


def managed_home(profile: dict, accounts: Path) -> Path:
    try:
        key = profile["id"]
        if str(uuid.UUID(key)) != key or not profile["managed"]:
            raise ValueError()
        home = Path(profile["home"])
        if home.is_symlink() or home.stat().st_file_attributes & 0x400:
            raise ValueError()
        if home.resolve() != (accounts / key).resolve() or home.parent.resolve() != accounts.resolve():
            raise ValueError()
        return home
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        raise SwitchError("账号目录未通过安全检查。") from None


def identity_or_none(home: Path) -> str | None:
    try:
        return read_auth(home).identity
    except SwitchError:
        return None


def check_storage(home: Path, account_id: str) -> None:
    try:
        config = home / "config.toml"
        values = tomllib.loads(config.read_text(encoding="utf-8")) if config.exists() else {}
    except (OSError, ValueError):
        raise SwitchError("无法检查 Codex 配置，暂不能切换。") from None
    if values.get("cli_auth_credentials_store", "file") != "file":
        raise SwitchError("当前版本只支持文件授权；此 Codex 配置使用了其他凭据存储。")
    if values.get("forced_login_method") not in (None, "chatgpt"):
        raise SwitchError("Codex 配置限制了登录方式，不能切换 ChatGPT 账号。")
    if values.get("forced_chatgpt_workspace_id") not in (None, account_id):
        raise SwitchError("目标账号不符合 Codex 配置中的工作空间限制。")


def protect(raw: bytes) -> bytes:
    """DPAPI binds the backup to this Windows user; plaintext never goes to a log."""
    class Blob(ctypes.Structure):
        _fields_ = [("size", wt.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]
    crypt = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    crypt.CryptProtectData.argtypes = [ctypes.POINTER(Blob), wt.LPCWSTR, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, wt.DWORD, ctypes.POINTER(Blob)]
    crypt.CryptProtectData.restype = wt.BOOL
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    buffer = (ctypes.c_ubyte * len(raw)).from_buffer_copy(raw)
    source, output = Blob(len(raw), buffer), Blob()
    if not crypt.CryptProtectData(ctypes.byref(source), "Gauge account backup", None, None, None, 1, ctypes.byref(output)):
        raise SwitchError("Windows 无法加密备份，已取消切换。")
    try:
        return ctypes.string_at(output.data, output.size)
    finally:
        kernel.LocalFree(output.data)


def restrict_file(path: Path) -> None:
    """Restrict an EMPTY staging file before writing any credentials to it."""
    system = Path(os.environ["SystemRoot"]) / "System32"
    result = subprocess.run([str(system / "whoami.exe"), "/user", "/fo", "csv", "/nh"],
                            capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW, timeout=10)
    import re
    match = re.search(rb"S-1-5-(?:[0-9]+-)*[0-9]+", result.stdout)
    if result.returncode or not match:
        raise SwitchError("无法确认 Windows 文件权限，已取消切换。")
    sid = match.group().decode("ascii")
    changed = subprocess.run([str(system / "icacls.exe"), str(path), "/inheritance:r", "/grant:r", f"*{sid}:(F)", "*S-1-5-18:(F)"],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             creationflags=subprocess.CREATE_NO_WINDOW, timeout=10)
    if changed.returncode:
        raise SwitchError("无法保护授权文件，已取消切换。")


def atomic_private_write(path: Path, raw: bytes, before_replace=None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, staging = tempfile.mkstemp(prefix=".gauge-", suffix=".tmp", dir=path.parent)
    os.close(handle)
    staging = Path(staging)
    try:
        restrict_file(staging)
        with staging.open("wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        if before_replace:
            before_replace()
        os.replace(staging, path)
    finally:
        staging.unlink(missing_ok=True)


def codex_processes() -> list[tuple[int, str]]:
    """Read process names and image paths, never arguments or environment values."""
    class Entry(ctypes.Structure):
        _fields_ = [("size", wt.DWORD), ("usage", wt.DWORD), ("pid", wt.DWORD),
                    ("heap", ctypes.c_size_t), ("module", wt.DWORD), ("threads", wt.DWORD),
                    ("parent", wt.DWORD), ("priority", wt.LONG), ("flags", wt.DWORD),
                    ("name", wt.WCHAR * 260)]
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateToolhelp32Snapshot.argtypes = [wt.DWORD, wt.DWORD]
    kernel.CreateToolhelp32Snapshot.restype = wt.HANDLE
    kernel.Process32FirstW.argtypes = [wt.HANDLE, ctypes.POINTER(Entry)]
    kernel.Process32NextW.argtypes = [wt.HANDLE, ctypes.POINTER(Entry)]
    kernel.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
    kernel.OpenProcess.restype = wt.HANDLE
    kernel.QueryFullProcessImageNameW.argtypes = [wt.HANDLE, wt.DWORD, wt.LPWSTR, ctypes.POINTER(wt.DWORD)]
    kernel.CloseHandle.argtypes = [wt.HANDLE]
    snapshot = kernel.CreateToolhelp32Snapshot(2, 0)
    if snapshot == ctypes.c_void_p(-1).value:
        raise SwitchError("无法检查 Codex 是否已退出。")
    result = []
    try:
        entry = Entry()
        entry.size = ctypes.sizeof(Entry)
        found = kernel.Process32FirstW(snapshot, ctypes.byref(entry))
        if not found:
            raise SwitchError("无法检查 Codex 是否已退出。")
        while found:
            name = entry.name.lower()
            if name in ("codex.exe", "chatgpt.exe"):
                path = ""
                process = kernel.OpenProcess(0x1000, False, entry.pid)
                if process:
                    try:
                        buffer, length = ctypes.create_unicode_buffer(32768), wt.DWORD(32768)
                        if kernel.QueryFullProcessImageNameW(process, 0, buffer, ctypes.byref(length)):
                            path = buffer.value
                    finally:
                        kernel.CloseHandle(process)
                if name == "codex.exe" or not path or "openai.codex_" in path.lower() or "\\codex\\" in path.lower():
                    result.append((entry.pid, path))
            found = kernel.Process32NextW(snapshot, ctypes.byref(entry))
        if ctypes.get_last_error() != 18:  # ERROR_NO_MORE_FILES; fail closed on partial enumeration.
            raise SwitchError("进程检查未完成，请重试。")
        return result
    finally:
        kernel.CloseHandle(snapshot)


def ensure_closed() -> None:
    if codex_processes():
        raise SwitchError("请先退出 Codex 桌面应用和正在运行的 Codex CLI。")


def desktop_path(processes: list[tuple[int, str]]) -> str | None:
    for _, value in processes:
        path = Path(value)
        if path.name.lower() == "chatgpt.exe" and "openai.codex_" in value.lower() and path.is_file():
            return str(path)
    return None


def switch_account(profile: dict, profiles: list[dict], default_home: Path, app_dir: Path, validate) -> list[dict]:
    """Validate first and replace only auth.json last; any earlier failure leaves login intact."""
    try:
        ensure_closed()
        accounts = app_dir / "accounts"
        target_home = managed_home(profile, accounts)
        target = read_auth(target_home)
        check_storage(target_home, target.account_id)
        check_storage(default_home, target.account_id)
        current = read_auth(default_home)
        if current.identity == target.identity:
            raise SwitchError("此账号已经在 Codex 中使用。")
        # Let the official CLI verify/refresh the target before copying its credentials.
        try:
            validate(str(target_home))
        except Exception:
            raise SwitchError("目标账号验证失败，当前账号未改变。请重新登录或稍后重试。") from None
        fresh_target = read_auth(target_home)
        if fresh_target.identity != target.identity:
            raise SwitchError("目标登录状态已变化，请重新选择账号。")

        def unchanged_and_closed():
            ensure_closed()
            if read_auth(default_home).raw != current.raw:
                raise SwitchError("Codex 登录状态已被其他程序更改，已取消切换。")

        unchanged_and_closed()
        updated = [dict(item) for item in profiles]
        old_profile = None
        for item in updated:
            if item.get("managed") and item.get("provider", "codex") == "codex":
                home = managed_home(item, accounts)
                if identity_or_none(home) == current.identity:
                    old_profile = item
                    break
        if old_profile is None:
            key = str(uuid.uuid4())
            home = accounts / key
            home.mkdir(parents=True, exist_ok=False)
            (home / "config.toml").write_text('cli_auth_credentials_store = "file"\n', encoding="utf-8")
            current_name = next((p["name"] for p in updated if not p.get("managed") and p.get("provider", "codex") == "codex"), "原账号")
            old_profile = {"id": key, "name": "原账号" if current_name == "当前账号" else current_name,
                           "home": str(home), "managed": True}
            updated.append(old_profile)
        atomic_private_write(Path(old_profile["home"]) / "auth.json", current.raw)
        backup = json.dumps({"version": 1, "home": str(default_home.resolve()),
                             "auth": base64.b64encode(current.raw).decode("ascii")}).encode()
        atomic_private_write(app_dir / "last-switch.dpapi", protect(backup))
        metadata = json.dumps({"version": 1, "profiles": updated}, ensure_ascii=False, indent=2).encode("utf-8")
        atomic_private_write(app_dir / "profiles.json", metadata)
        # No failure-prone steps after the credential commit. Config, projects, and history are untouched.
        atomic_private_write(default_home / "auth.json", fresh_target.raw, unchanged_and_closed)
        return updated
    except SwitchError:
        raise
    except Exception:
        raise SwitchError("切换未完成，原登录文件未被替换。请检查文件权限后重试。") from None
