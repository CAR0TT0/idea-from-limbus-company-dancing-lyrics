# 酷狗音乐版

基于上游 v0.8.0-beta.1，复用全部歌词绘制、设置、字体、主题、预设和双语逻辑。新适配见 [kugou.py](kugou.py)、[读取助手](kugou_reader.py)、[歌词读取](kugou_lyrics.py)、[自动联动](kugou_startup.py) 与 [包内说明](release/kugou-guide.txt)。

程序独立安装，不修改酷狗文件、不注入 DLL、不检查酷狗版本白名单。系统接口提供完整时间轴时使用 SMTC；只提供曲目时，以系统曲目和播放状态配合界面 OCR 进度。最后才尝试纯 OCR。助手独立进程运行，超时自动重建；歌词工作线程保留歌曲代次检查，拒绝旧曲目结果覆盖新曲目。

界面兼容通道需要系统 OCR 组件、还原的酷狗主窗口，进度约 1 秒精度。最小化时停止同步，恢复窗口后重新连接。未知客户端界面或缺失接口会明确报告，不能保证任意未来版本的联动。安装不依赖酷狗版本与安装目录。

本地 KRC/LRC 优先；自动从个人 INI 定位歌词目录，支持手动指定。联网只向酷狗歌词域名发出 HTTPS 请求，要求歌名、歌手和时长匹配。KRC 支持真实逐字和语言标签；缺失时保留整句歌词，不伪造逐字时间或译文。

设置放在 `%LOCALAPPDATA%\DancingLyrics-Kugou\.state`。快捷方式只写当前用户桌面与开始菜单；自动联动由用户在设置中选择，关闭后移除本程序的登录项。

开发验证：`python -X utf8 -m unittest discover -s tests -q`；`python main.py --smoke --report-dir artifacts/kugou-engine`；`python main.py --kugou --kugou-smoke --report-dir artifacts/kugou-live`。最后一项只接受真实酷狗数据，未观察到的暂停、拖动、切歌和最小化行为不能据此宣称通过。
