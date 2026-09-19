# 安全说明

## 数据存储

Gauge 数据默认位于 `%LOCALAPPDATA%\Gauge`；存在旧版 `%LOCALAPPDATA%\SafeQuota` 时原地复用。

| 内容 | 文件或目录 | 保护方式 |
| --- | --- | --- |
| 账号元数据 | `profiles.json` | 保存平台、名称和授权目录，不保存令牌 |
| 额外账号授权 | `accounts/<ID>/` | 由官方 CLI 创建；包含敏感访问令牌和刷新令牌 |
| 偏好与窗口尺寸 | `settings.json`、`window.json` | 本地文件 |
| 最近一次切换备份 | `last-switch.dpapi` | Windows DPAPI 加密，绑定当前 Windows 用户 |

普通授权文件未加密。DPAPI 仅用于切换备份，不能将整个账号目录视为加密存储。不要提交或分享应用数据、`.codex`、`.claude`、`.claude.json`、`auth.json` 或 `.credentials.json`。

## 外部调用

| 操作 | 调用目标 |
| --- | --- |
| Codex 登录 | 本机官方 CLI：`codex login` |
| Codex 额度查询 | 本机官方 CLI：`app-server --stdio` / `account/rateLimits/read` |
| Claude 登录 | 本机官方 CLI：`claude auth login --claudeai` |
| Claude 额度查询 | `https://api.anthropic.com/api/oauth/usage` |
| Claude 授权续期 | `https://platform.claude.com/v1/oauth/token` |

Codex 网络请求和令牌刷新由 CLI 处理。Claude 查询与续期直接调用上述 HTTPS 地址，验证证书、拒绝重定向，不接受自定义服务主机；错误信息不包含响应正文或令牌。

额外账号登录使用隔离的配置目录。Claude 登录同时清理继承的 API Key 和自定义供应商环境变量。Gauge 不发起模型任务，不包含遥测、远程控制或自动更新功能。

程序会执行本机发现的官方 CLI；其安装目录、PATH 和可执行文件属于信任边界。

## Codex 登录切换

切换流程依次执行以下保护：

1. 检查目标身份、凭据模式及 Codex 工作空间限制。
2. 通过官方 CLI 验证目标账号，等待 Codex 桌面应用及 CLI 退出。
3. 保留原账号，使用 DPAPI 加密最近一次切换备份。
4. 限制临时授权文件权限，重新检查原授权文件及进程状态。
5. 原子替换共用目录的 `auth.json`。

切换不迁移项目、历史或缓存，只支持 ChatGPT 文件授权。进程检查与文件替换之间仍存在竞争窗口；切换期间不要重新启动 Codex 或修改授权文件。

## Claude 授权续期

续期使用 `.oauth_refresh.lock` 目录锁和心跳。保存前检查授权文件未被其他进程修改，再限制文件权限并原子替换。超过 60 秒无心跳的空锁可恢复，非空锁不会被删除。

远程刷新成功后若本地写入失败，可能需要重新登录。Claude 目前不提供主 CLI 的实际账号切换。

## 本机配置修改

开机自启动使用当前用户的 `HKCU\Software\Microsoft\Windows\CurrentVersion\Run\Gauge` 注册表项，默认关闭。兼容读取旧版启动项；写入时清理旧项，不修改其他应用的启动配置。

## 安全边界

- 无法防御同一 Windows 用户下的恶意进程、管理员或已被入侵的操作系统。
- DPAPI 备份不可作为跨用户、跨设备的账号备份。
- Codex app-server、Claude 内部额度接口与 OAuth 协议的兼容性取决于上游版本。
- 自动化测试使用合成凭据；真实登录、权限和上游行为需要单独验证。
- 项目尚未经过独立第三方安全审计。

## 漏洞报告

优先使用仓库的私密漏洞报告功能。尚未启用时，先向维护者请求私密报告渠道，勿在公开 Issue 中提交利用细节或凭据。

报告应包含影响范围、相关版本、脱敏复现步骤及建议修复方式。公开日志和截图需移除令牌、邮箱、账号标识和本地私有路径。

如发现凭据泄露，应通过对应平台撤销泄露的授权，并清理已公开的凭据副本；仅删除仓库当前文件不足以清除 Git 历史中的内容。
