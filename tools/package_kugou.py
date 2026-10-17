"""Build an isolated portable KuGou package from the v0.8.0-beta.1 engine."""
import argparse
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]
VERSION = "0.8.0-beta.1-kugou.4"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--upstream-package", type=Path, required=True)
    parser.add_argument("--skip-build", action="store_true")
    args = parser.parse_args()
    dist = ROOT / "dist-kugou"
    ready = dist / "DancingLyrics-Kugou"
    if not args.skip_build:
        helper = ROOT / "build/native/ProcessEnergy.exe"
        if not helper.is_file():
            with zipfile.ZipFile(args.upstream_package) as upstream:
                helper.parent.mkdir(parents=True, exist_ok=True)
                helper.write_bytes(upstream.read("DancingLyrics-NetEase/_internal/native/ProcessEnergy.exe"))
        command = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--windowed", "--onedir",
                   "--name", "DancingLyrics-Kugou", "--icon", str(ROOT / "assets/app.ico"),
                   "--add-data", str(ROOT / "assets") + ";assets",
                   "--add-data", str(ROOT / "plugins/netease") + ";plugins/netease",
                   "--add-data", str(ROOT / "native/qqmusic_smtc.ps1") + ";native",
                   "--add-binary", str(helper) + ";native",
                   "--collect-submodules", "winrt", "--exclude-module", "tkinter",
                   "--exclude-module", "PySide6.QtPdf", "--exclude-module", "PySide6.QtVirtualKeyboard",
                   "--distpath", str(dist), "--workpath", str(ROOT / "build/kugou"),
                   "--specpath", str(ROOT / "build/kugou-spec"), str(ROOT / "main.py")]
        subprocess.run(command, cwd=ROOT, check=True)
    if not (ready / "DancingLyrics-Kugou.exe").is_file():
        raise FileNotFoundError("Build output missing")
    for name, flags in {"启动酷狗联动.bat": "--kugou", "安装酷狗联动.bat": "--install-kugou",
                        "卸载酷狗联动.bat": "--uninstall-kugou", "本地音乐与演示.bat": "--demo",
                        "诊断酷狗联动.bat": "--kugou --kugou-smoke"}.items():
        script = ('@echo off\nsetlocal\ncd /d "%~dp0"\n'
                  'if not exist "_internal" (\n  echo Extract the entire ZIP before running this file.\n  pause\n  exit /b 1\n)\n'
                  'if not exist "DancingLyrics-Kugou.exe" (\n  echo Application executable missing.\n  pause\n  exit /b 1\n)\n'
                  'start "" "DancingLyrics-Kugou.exe" ' + flags + ' %*\n')
        (ready / name).write_bytes(script.replace("\n", "\r\n").encode("ascii"))
    shutil.copyfile(ROOT / "release/kugou-guide.txt", ready / "使用说明.txt")
    shutil.copyfile(ROOT / "LICENSE", ready / "LICENSE")
    shutil.copyfile(ROOT / "THIRD_PARTY_NOTICES.md", ready / "THIRD_PARTY_NOTICES.md")
    with zipfile.ZipFile(args.upstream_package) as upstream:
        for entry in upstream.infolist():
            parts = Path(entry.filename).parts
            if len(parts) >= 3 and parts[1] == "licenses" and not entry.is_dir():
                target = ready / "licenses" / Path(*parts[2:])
                if not target.resolve().is_relative_to((ready / "licenses").resolve()):
                    raise ValueError("Invalid license path")
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(upstream.read(entry))
    actual_python_license = Path(sys.base_prefix) / "LICENSE.txt"
    if actual_python_license.is_file():
        shutil.copyfile(actual_python_license, ready / "licenses/Python-LICENSE.txt")
    for name in ("winrt-runtime", "winrt-Windows.Media.Control", "winrt-Windows.Media.Ocr",
                 "winrt-Windows.Foundation", "winrt-Windows.Foundation.Collections", "winrt-Windows.Globalization",
                 "winrt-Windows.Graphics.Imaging", "winrt-Windows.Storage.Streams", "pefile"):
        distribution = importlib.metadata.distribution(name)
        for item in distribution.files:
            if "dist-info" in str(item) and ("license" in item.name.casefold() or "copying" in item.name.casefold()):
                shutil.copyfile(distribution.locate_file(item), ready / "licenses" / (name + "-" + item.name))
    with (ready / "THIRD_PARTY_NOTICES.md").open("a", encoding="utf-8") as out:
        out.write("\n\n酷狗版本补充：基于 Celine258 v0.8.0-beta.1 修改；运行时 Python " + sys.version.split()[0] +
                  "。新增 PyWinRT 3.2.1（MIT），用于 Windows 系统媒体与 OCR；许可保存在 licenses。" +
                  "\nhttps://github.com/pywinrt/pywinrt/tree/v3.2.1\n另包含 pefile 2023.2.7（MIT），只读解析酷狗歌词模块；许可保存在 licenses。" +
                  "\nhttps://github.com/erocarrera/pefile\n酷狗版不安装 BetterNCM。上游归属记录仍予保留。\n")
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    info = dict(version=VERSION, base="v0.8.0-beta.1", source_commit=commit, python=sys.version.split()[0],
                upstream_package_sha256=hashlib.sha256(args.upstream_package.read_bytes()).hexdigest(),
                exe_sha256=hashlib.sha256((ready / "DancingLyrics-Kugou.exe").read_bytes()).hexdigest(),
                compatibility="Independent install; runtime interfaces remain subject to client changes; see guide.")
    (ready / "package-info.json").write_text(json.dumps(info, ensure_ascii=False, indent=2), encoding="utf-8")
    archive = dist / ("dancing-lyrics-" + VERSION + "-windows-x64.zip")
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as output:
        for file in sorted(ready.rglob("*")):
            if file.is_file() and not any(part in (".state", "artifacts", "__pycache__") for part in file.relative_to(ready).parts):
                output.write(file, str(Path("DancingLyrics-Kugou") / file.relative_to(ready)))
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    archive.with_suffix(".zip.sha256").write_text(digest + "  " + archive.name + "\n", encoding="ascii")
    print(json.dumps(dict(archive=str(archive), bytes=archive.stat().st_size, sha256=digest), indent=2))


if __name__ == "__main__":
    main()
