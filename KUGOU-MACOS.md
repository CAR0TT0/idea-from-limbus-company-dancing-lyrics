# 酷狗音乐 macOS 实验适配

版本 **0.8.0-beta.1-kugou-macos.1**。这是基于现有酷狗版的独立 Mac 适配，
沿用共享渲染、设置、字体、主题、预设与 KRC 解析。
**本轮只在 Windows 完成自动测试和模拟输入验证，尚未完成 Mac 真机验收，也尚未生成 Mac 成品 `.app`。**
不要将旧的网易云 Mac 下载包或 Windows EXE 当成本版。
本轮测试及未验证项目记录在 [结构化验证范围](docs/research/kugou-macos-20261017.json)。

## 安装源码测试版

无需提前确定芯片：安装入口会读取 `uname -m`，通过当前 Mac 的 Homebrew 与 Python 安装对应架构依赖。
计划支持 Apple Silicon（arm64）和 Intel（x86_64），最低 macOS 13；两种架构均待实测。

1. 完整解压源码 ZIP 到一个新目录，保留目录结构。
2. 如果尚未安装 Homebrew，先按 [Homebrew 官网](https://brew.sh/) 操作。
3. 双击 **安装酷狗Mac.command**。会安装 Python 3.12（已有兼容版本时复用）、外部 `media-control` 和项目依赖。
4. 在酷狗 Mac 版播放完整歌曲，再双击 **启动酷狗Mac.command**。
5. 需要排查时运行 **检查酷狗Mac.command**。诊断不包含歌名、艺人或歌词正文；只检测当前来源和时间轴，
   不代表后台播放或关闭操作已经通过验收。

双击受系统限制时，可在此目录的终端运行 `bash 安装酷狗Mac.command`、`bash 启动酷狗Mac.command`。
脚本不要求管理员权限，不安装登录项，不更改酷狗或系统隐私设置。

## 同步方式及保留功能

- 只执行外部 `media-control get --micros --no-artwork`，读取系统当前播放信息；
  必须具有酷狗标识 `com.kugou.mac.Music`、明确播放状态、有效时长、进度与时间戳。
  来源标识核对自 Apple 查询接口 `https://itunes.apple.com/lookup?id=658741592&country=cn`（2026-10-17）。
  拒绝来源不明和其他播放器，缺少真实时间轴时不凭空计时。
- 依据时间戳外推当前进度，使用单调时钟驱动连续动画；正常观测逐渐校准，不逐包回拨。
  暂停冻结，实际跳转重新对齐，读取中断最多外推 1.5 秒，随后清空歌词并提示。
- 根据歌名、歌手和时长匹配酷狗在线 KRC。也可在面板选择本地 KRC/LRC 目录；只读取直接子文件。
  不自动猜测 Mac 缓存路径，不使用 Windows 的缓存和原生进程读取模块。
  无可靠匹配时保持空白；可点击“重新获取歌词”。联网接口不承诺长期稳定。
- 保留真实逐字时间、歌词结束时间、中文翻译、上下句动画、文字效果、字体导入、主题、预设与逐曲偏移。
  **没有接入 Mac 真实音频强弱采集**，默认使用轻波浪，不宣称与 Windows 音频律动功能完全相同。
- **不自动接替、隐藏或恢复酷狗原生歌词窗口，不操作酷狗主窗口。**
  若出现两套歌词，请在酷狗自身设置中关闭原生桌面歌词。
  插件退出只停止自己的线程和窗口；不向酷狗发送播放、暂停、关闭或最小化命令。

酷狗最小化后，只要系统仍持续提供该来源的播放信息，读取路径就不依赖主窗口是否可见。
这是实现方式，不是后台同步实测结论：**最小化、隐藏到 Dock、暂停、拖动、切歌、窗口关闭与恢复、全屏 Spaces 都待 Mac 真机验收。**
系统升级或酷狗改变媒体信息发布方式仍可能影响兼容，不能保证所有未来版本。

设置、字体、预设、偏移和日志统一保存在 `~/Library/Application Support/DancingLyrics-Kugou/`，
不会读取 Windows 版的注册表或自动启动配置。关闭应用后再更新源码，设置会保留。
卸载源码测试版时移走源码目录即可；个人设置和共用 Homebrew 工具默认保留。

## 在 Mac 生成应用包

安装后双击 **打包酷狗Mac.command**，或在该 Mac 的终端运行：

```bash
.venv/bin/python -m pip install -r requirements-build.txt
.venv/bin/python tools/build_macos.py --player kugou
```

生成当前芯片的 `都市回响·酷狗.app`，ZIP 写入 `artifacts/mac-release/`，文件名包含 `arm64` 或 `x86_64`。
运行 `tools/verify_macos.py ZIP路径 --player kugou --report-dir artifacts/mac-verification` 可检查
解压、签名、Cocoa 窗口启动与设置保存；这是模拟播放验证，不是实际酷狗播放验收。
仓库另外提供手动触发的 `.github/workflows/kugou-macos-package.yml`，本轮未推送或触发远端构建。
构建器在 Windows 明确拒绝生成 Mac 成品，不把源码 ZIP 冒充应用包。
应用采用 ad-hoc 签名，未获 Apple Developer ID 签名或公证；打开限制请参考 [Apple 官方说明](https://support.apple.com/zh-cn/102445)。

系统读取工具来自 [media-control](https://github.com/ungive/media-control)，BSD-3-Clause，通过 Homebrew 独立安装。
其底层 [MediaRemote Adapter](https://github.com/ungive/mediaremote-adapter) 使用系统私有框架，未来 macOS 升级可能导致失效。
本包不附带工具二进制；数据字段与微秒时间戳依据工具作者文档实现。
