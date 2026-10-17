#!/bin/bash
set -euo pipefail
cd -- "$(dirname -- "$0")"
if [ "$(uname -s)" != "Darwin" ]; then exit 2; fi
bash 安装酷狗Mac.command
.venv/bin/python -m pip install -r requirements-build.txt
.venv/bin/python tools/build_macos.py --player kugou
open artifacts/mac-release
