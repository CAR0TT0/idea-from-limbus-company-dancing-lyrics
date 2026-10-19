#!/bin/bash
set -u
cd -- "$(dirname -- "$0")"
if [ "$(uname -s)" != "Darwin" ]; then exit 2; fi
if [ ! -x .venv/bin/python ]; then
    printf '%s\n' '请先双击“安装酷狗Mac.command”。' >&2
    exit 1
fi
.venv/bin/python tools/probe_kugou_macos.py
task_status=$?
if [ -t 0 ]; then
    printf '%s\n' '可复制上方诊断结果。按回车关闭。'
    read -r task_answer
fi
exit "$task_status"
