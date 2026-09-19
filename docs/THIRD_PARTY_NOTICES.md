# 第三方许可

Gauge 源码和原创图标采用 [MIT](../LICENSE)。第三方组件、平台标志及商标不适用该许可授权。

账号卡片的平台标志 `assets/openai.svg`、`assets/claude.svg` 来自 [Simple Icons v14.0.0](https://github.com/simple-icons/simple-icons/tree/14.0.0/icons)，图标集合按 [CC0-1.0](https://github.com/simple-icons/simple-icons/blob/14.0.0/LICENSE.md) 提供。OpenAI、Claude 及相关标志仍分别属于 OpenAI、Anthropic；CC0 和本项目 MIT 均不授予商标权。它们只用于标识对应服务，不属于 Gauge 自有品牌，也不表示官方认可。另见 [OpenAI 品牌指南](https://openai.com/brand/)。

| 组件 | 用途 | 许可与来源 |
| --- | --- | --- |
| Python | 解释器；本机打包时会随程序包含 | [Python 许可](https://docs.python.org/3/license.html) |
| PySide6 Essentials / Shiboken6 6.8.1 | Qt Python 绑定、界面及运行库 | [Qt for Python](https://doc.qt.io/qtforpython-6/)：LGPLv3 / GPLv3 / 商业许可，具体文件以发行包声明为准 |
| Qt 6 | 窗口、绘图及系统集成 | [Qt 许可](https://doc.qt.io/qt-6/licensing.html)：模块与第三方代码可能采用不同许可 |
| PyInstaller 6.19.0 | 打包工具 | [GPLv2 或更新版本，带 bootloader exception](https://pyinstaller.org/en/stable/license.html)；运行时 hooks 为 Apache-2.0 |
| Codex CLI | 用户单独安装，登录和查询额度 | [官方仓库及许可证](https://github.com/openai/codex)，不包含在本源码包内 |
| Claude Code CLI | 用户单独安装，完成订阅账号登录 | [官方仓库及条款](https://github.com/anthropics/claude-code)，不包含在本源码包内 |

## 二进制分发

源码包不包含 Python、Qt 或 PyInstaller 运行库。构建的 EXE 包含第三方组件，分发时需附带相应版权、许可证和声明。

当前发布包在 EXE 内嵌 `docs/licenses/` 中的许可与声明，可从托盘菜单“开源许可”打开；Release 同时提供独立许可压缩包。Qt / PySide / Shiboken 使用 LGPLv3，所用库未作修改。许可汇编保留对应上游源码版本的声明，包含部分未打包的上游组件。

对应版本源码入口、许可全文与替换库后的重建方式见 [许可索引](licenses/index.html)。项目不限制为调试修改后的 LGPL 库而进行的逆向工程。修改运行库后，按 [开发指南](CONTRIBUTING.md) 在构建环境安装替换库并重新构建；无需专有密钥或激活服务。

Qt 对应版本源码可从 [Qt 官方发行目录](https://download.qt.io/official_releases/QtForPython/pyside6/) 获取。不同版本的依赖和许可可能变化，发布时以实际构建版本为准。
