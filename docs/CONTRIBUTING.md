# 贡献指南

## 开发环境

Windows 10 / 11、Python 3.11、PowerShell 7。在项目根目录执行：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r config/requirements-dev.txt
.\.venv\Scripts\python.exe .\gauge.py
```

运行依赖固定在 `config/requirements.txt`，构建依赖固定在 `config/requirements-dev.txt`。

## 项目结构

```text
Gauge/
├─ src/            应用源码
├─ apps/           可执行程序
├─ assets/         图标资源
├─ config/         构建配置与依赖清单
├─ docs/           开发、安全与许可说明
├─ exports/        源码分发包
├─ tests/          自动化测试
├─ tools/          构建与维护脚本
├─ gauge.py        启动入口
├─ README.md       使用说明
└─ LICENSE         MIT 许可证
```

| 路径 | 职责 |
| --- | --- |
| `src/main.py` | 窗口、账号管理、登录与查询任务调度 |
| `src/models.py` | 账号、额度窗口和查询结果的数据模型 |
| `src/codex_provider.py` | Codex CLI 发现、额度 RPC 与响应解析 |
| `src/claude_provider.py` | Claude 额度查询、授权续期、缓存与限流 |
| `src/account_switch.py` | Codex 登录切换、备份及凭据写入保护 |
| `src/preferences.py` | 偏好存储、主题样式和 Windows 自启动 |
| `src/i18n.py` | 中文与英文文案 |
| `config/Gauge.spec` | PyInstaller 构建配置 |

## 验证

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -t . -v
pwsh -File .\tools\build.ps1
```

构建前退出正在运行的 Gauge，产物为 `apps/Gauge.exe`。构建脚本支持 `-Python`、`-DistPath` 和 `-WorkPath` 参数，分别指定解释器、产物目录和构建缓存目录。

测试使用临时目录、合成凭据和 Qt 离屏环境，不依赖真实账号。禁止在自动化测试中修改开发者的登录文件或 Windows 启动项。

按改动范围补充验证：

| 改动 | 验证重点 |
| --- | --- |
| 账号切换 | 验证失败、写入失败、进程未退出、原授权变化时保持原登录 |
| Claude 授权 | 令牌续期、并发锁、限流、过期授权、错误信息脱敏 |
| 窗口与设置 | 最小尺寸、跨 DPI 显示器移动、设置保存与取消 |
| 文案 | 两种语言、窄窗口排版、菜单、提示和倒计时 |
| 登录流程 | 成功、取消、超时和授权失败 |

离线测试不替代真实授权验证。提交说明应区分自动化结果、人工验证及未验证场景。

## 修改约定

- 保持文案简洁，复用 `src/preferences.py` 中的主题和颜色。
- 界面文案通过 `src/i18n.py` 翻译；内部错误判断和用户账号名称不随语言改变。
- 修改图标源文件 `assets/gauge.svg` 后，运行 `.\.venv\Scripts\python.exe .\tools\render_icon.py` 生成 PNG 和 ICO。
- 新增可分发文件时，同步更新 `tools/export_source.py` 的清单。
- 旧版数据目录、启动项和单实例通信的兼容逻辑需要保留；不要批量迁移用户授权文件。

## 提交变更

Pull Request 应包含修改目的、实现范围、验证结果及已知限制。界面变更附使用模拟账号数据的截图。缺陷报告注明复现步骤、预期行为、实际行为及相关 Windows、Gauge、CLI 版本。

不得提交授权文件、令牌、个人配置或真实账号截图。安全问题按 [SECURITY.md](SECURITY.md#漏洞报告) 报告。提交贡献表示贡献者有权按项目 MIT 许可证提供相应内容。

## 源码分发

面向两类用户提供独立下载项：

| 分发文件 | 用途 | 构建位置 |
| --- | --- | --- |
| `Gauge.exe` | 普通用户直接运行；不需要源码、Python 或额外资源文件 | `apps/Gauge.exe` |
| `Gauge-source.zip` | 二次开发；包含源码、测试、资源、文档和构建工具 | `exports/Gauge-source.zip` |

`Gauge.exe` 使用 PyInstaller 单文件打包，图标和运行库随程序包含。Codex / Claude Code 官方 CLI 属于外部运行依赖。发布时同时提供两个文件，不要求普通用户下载项目源码。

```powershell
.\.venv\Scripts\python.exe .\tools\export_source.py
```

导出器按明确清单生成：

- `exports/Gauge-source.zip`
- `exports/Gauge-source.zip.sha256`

源码包不包含运行程序、构建缓存、虚拟环境和账号数据。发布前检查文件清单、校验和，并确认解压后的源码可安装依赖、运行测试和构建。

`build/`、`apps/` 和 `exports/` 是生成目录，不提交到仓库。公开分发二进制前，按 [第三方许可](THIRD_PARTY_NOTICES.md#二进制分发) 整理实际构建所含组件的许可材料。

## 版本发布

版本号定义于 `src/__init__.py`，构建时写入 EXE 文件属性。发布前更新该版本号、`docs/CHANGELOG.md`、对应的 `docs/releases/v版本号.md` 和 README 下载链接。

```powershell
pwsh -File tools/build.ps1
.\.venv\Scripts\python.exe -B tools/prepare_release.py
```

发布文件生成到 `exports/v版本号/`：带版本号的 Windows x64 EXE、完整源码 ZIP、许可 ZIP 和 `SHA256SUMS.txt`。在 GitHub 创建相同名称的 `v版本号` 标签与 Release，上传这些文件；预览版本标记为 Pre-release，不覆盖历史版本资产。

截图通过 `.\.venv\Scripts\python.exe -B tools/capture_previews.py` 重建。此脚本使用真实 Qt 界面与合成账号，隔离本地数据目录并禁用网络查询。
