#!/bin/bash
# Separate Mac environment; no modifications to KuGou or login items.
set -euo pipefail
cd -- "$(dirname -- "$0")"
if [ "$(uname -s)" != "Darwin" ]; then
    printf '%s\n' '此安装入口仅用于 macOS。' >&2
    exit 2
fi
task_brew=''
for candidate in /opt/homebrew/bin/brew /usr/local/bin/brew; do
    if [ -x "$candidate" ]; then task_brew="$candidate"; break; fi
done
if [ -z "$task_brew" ]; then task_brew="$(command -v brew || true)"; fi
if [ -z "$task_brew" ]; then
    printf '%s\n' '请先从 https://brew.sh/ 安装 Homebrew，再双击本文件。' >&2
    exit 1
fi
export PATH="$(dirname -- "$task_brew"):$PATH"
if ! command -v media-control >/dev/null 2>&1; then "$task_brew" install media-control; fi
task_python=''
for candidate in "$("$task_brew" --prefix)/opt/python@3.12/bin/python3.12" python3.12 python3.11 python3.10; do
    if command -v "$candidate" >/dev/null 2>&1 &&
       "$candidate" -c 'import sys; sys.exit(not ((3, 10) <= sys.version_info[:2] <= (3, 12)))'; then
        task_python="$candidate"; break
    fi
done
if [ -z "$task_python" ]; then
    "$task_brew" install python@3.12
    task_python="$("$task_brew" --prefix python@3.12)/bin/python3.12"
fi
if [ -d .venv ] && [ ! -x .venv/bin/python ]; then
    printf '%s\n' '.venv 不是可用的 Mac 环境。请将此源码解压到新目录后安装。' >&2
    exit 1
fi
if [ ! -d .venv ]; then "$task_python" -m venv .venv; fi
.venv/bin/python -c 'import sys; sys.exit(not ((3, 10) <= sys.version_info[:2] <= (3, 12)))'
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -c 'import macos_kugou, controls, overlay'
.venv/bin/python main_mac_kugou.py --check
chmod +x 启动酷狗Mac.command 检查酷狗Mac.command 打包酷狗Mac.command
printf '%s\n' "已识别芯片：$(uname -m)" '安装完成，请打开酷狗并播放歌曲，然后双击“启动酷狗Mac.command”。' \
    '本版为待 Mac 真机验收的实验源码版，已知限制见 KUGOU-MACOS.md。'
