#!/bin/bash
set -euo pipefail
if [ "$(uname -s)" != "Darwin" ]; then exit 2; fi
task_brew=''
for candidate in /opt/homebrew/bin/brew /usr/local/bin/brew; do
    if [ -x "$candidate" ]; then task_brew="$candidate"; break; fi
done
if [ -z "$task_brew" ]; then task_brew="$(command -v brew || true)"; fi
if [ -z "$task_brew" ]; then
    printf '%s\n' '请先从 https://brew.sh/ 安装 Homebrew，再重新运行。' >&2
    exit 1
fi
"$task_brew" install media-control
printf '%s\n' '播放读取工具安装完成。请在酷狗播放歌曲，再打开“都市回响·酷狗.app”。'
