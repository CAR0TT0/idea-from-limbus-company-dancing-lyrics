"""Build an allowlisted public archive, never copying the developer's .state."""
import argparse
import hashlib
import json
from pathlib import Path
import sys
import zipfile
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app_info import APP_VERSION

ROOT = Path(__file__).resolve().parents[1]
FORBIDDEN = {".state", ".venv", "__pycache__", "artifacts", ".git"}
PRIVATE_FILES = {"bridge-config.json", "netease-bridge.json", "settings.ini", "effect-presets.json", "BetterNCMII.dll", "msimg32.dll"}
# General Qt hooks collect Virtual Keyboard (GPL-only) and PDF, neither used by this app.
# Keep the public runtime limited to the interfaces used by this application.
UNUSED_QT = {"qt6virtualkeyboard.dll", "qtvirtualkeyboardplugin.dll", "qt6pdf.dll", "qpdf.dll"}
PLAYERS = {
    "qqmusic": ("DancingLyrics-QQMusic", ("启动QQ音乐联动.bat",)),
    "netease": ("DancingLyrics-NetEase", ("安装网易云联动.bat", "卸载网易云联动.bat", "启动.bat")),
}


def archive_name(player):
    if player not in PLAYERS:
        raise ValueError("请选择 qqmusic 或 netease。")
    return f"dancing-lyrics-{APP_VERSION}-{player}-windows-x64.zip"


def archive_prefix(player):
    archive_name(player)
    return PLAYERS[player][0]


def package_release(package, output_dir, root=ROOT, *, player="netease"):
    package, output_dir, root = Path(package), Path(output_dir), Path(root)
    filename = archive_name(player)
    if not (package / "FloatingLyrics.exe").is_file() or not (package / "_internal").is_dir():
        raise ValueError("先构建完整的 FloatingLyrics 成品。")
    required = [f"_internal/PySide6/Qt{name}.pyd" for name in ("Core", "Gui", "Widgets")]
    required += (["_internal/native/qqmusic_smtc.ps1"] if player == "qqmusic" else
                 [f"_internal/plugins/netease/{name}" for name in ("manifest.json", "index.js", "adapter.js")])
    for name in required:
        if not (package / name).is_file():
            raise ValueError(f"{player} 成品缺少运行文件：{name}")
    licenses = root / "release/licenses"
    if not licenses.is_dir() or not any(licenses.iterdir()):
        raise ValueError("缺少第三方许可文件，不能发布。")
    output_dir.mkdir(parents=True, exist_ok=True)
    output = output_dir / filename
    prefix, scripts = PLAYERS[player]
    entries = [(package / "FloatingLyrics.exe", "FloatingLyrics.exe")]
    for path in sorted((package / "_internal").rglob("*")):
        if path.is_file():
            relative = path.relative_to(package)
            if player == "qqmusic" and relative.parts[:3] == ("_internal", "plugins", "netease"):
                continue
            if player == "netease" and relative.as_posix() == "_internal/native/qqmusic_smtc.ps1":
                continue
            if path.name.lower() in UNUSED_QT:
                continue
            if any(part in FORBIDDEN for part in relative.parts) or path.name in PRIVATE_FILES or path.suffix.lower() in (".log", ".pyc"):
                continue
            entries.append((path, relative.as_posix()))
    # Explicit entry points: never mix other players or Mac scripts into the ZIP.
    for name in scripts:
        entries.append((root / "release" / name, name))
    entries.append((root / "release" / player / "使用说明.txt", "使用说明.txt"))
    for path in sorted(licenses.rglob("*")):
        if path.is_file():
            entries.append((path, path.relative_to(root / "release").as_posix()))
    for name in ("LICENSE", "THIRD_PARTY_NOTICES.md"):
        entries.append((root / name, name))
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for path, relative in entries:
            if any(part in FORBIDDEN for part in Path(relative).parts):
                raise ValueError(f"禁止发布个人文件：{relative}")
            archive.write(path, f"{prefix}/{relative}")
        archive.writestr(f"{prefix}/package-info.json", json.dumps(
            {"format_version": 1, "app_version": APP_VERSION, "player": player,
             "platform": "windows-x64"}, ensure_ascii=False, indent=2))
    with zipfile.ZipFile(output) as archive:
        bad = archive.testzip()
        if bad:
            raise ValueError(f"发布包校验失败：{bad}")
    checksum = output.with_suffix(".zip.sha256")
    checksum.write_text(f"{hashlib.sha256(output.read_bytes()).hexdigest()}  {output.name}\n", encoding="ascii")
    return output


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--package", type=Path, default=ROOT / "dist/FloatingLyrics")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "artifacts/releases")
    parser.add_argument("--player", choices=(*PLAYERS, "all"), default="all")
    args = parser.parse_args()
    for player in PLAYERS if args.player == "all" else (args.player,):
        print(package_release(args.package, args.output_dir, player=player), flush=True)
