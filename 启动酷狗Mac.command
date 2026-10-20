#!/bin/bash
set -u
cd -- "$(dirname -- "$0")"
if [ "$(uname -s)" != "Darwin" ]; then
    printf '%s\n' '此入口仅用于 macOS。' >&2
    exit 2
fi
if [ ! -x .venv/bin/python ]; then
    printf '%s\n' '请先双击“安装酷狗Mac.command”。' >&2
    exit 1
fi
.venv/bin/python main_mac_kugou.py "$@"
task_status=$?
if [ "$task_status" -ne 0 ] && [ -t 0 ]; then
    printf '%s\n' '启动失败，请保留上方提示。按回车关闭。'
    read -r task_answer
fi
exit "$task_status"
