"""Audit a ditto archive, including permissions and framework symlinks."""
import hashlib
import json
from pathlib import Path, PurePosixPath
import posixpath
import re
import stat
import subprocess
import zipfile

MAC_RELEASE_TAG = "v0.6.0-beta.2-macos.1"
MAC_PACKAGE_PREFIX = "city-echoes-0.6.0-beta.2-macos.1"
APP_BUNDLE = "都市回响.app"
KUGOU_APP_BUNDLE = "都市回响·酷狗.app"
KUGOU_MAC_RELEASE_TAG = "v0.8.0-beta.1-kugou-macos.1"
FORBIDDEN = {".git", ".venv", ".state", "__pycache__", "settings.ini", "effect-presets.json",
             "offsets.json", "bridge-config.json", "netease-bridge.json", "nowplaying-cli", "media-control"}


def source_commit(root):
    root = Path(root)
    manifest = root / "source-info.json"
    if manifest.is_file():
        value = json.loads(manifest.read_text(encoding="utf-8")).get("source_commit", "")
    else:
        value = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{40}", value):
        raise ValueError("无法验证源码提交标识，请使用完整的源码 ZIP 或 Git 检出目录。")
    return value


def record_runtime_licenses(directory, python_version, numpy_version, architecture):
    directory = Path(directory)
    manifest = json.loads((directory / "sources.json").read_text(encoding="utf-8"))
    for entry in manifest:
        if "python" in entry:
            entry.update(python=python_version, numpy=numpy_version, architecture=architecture)
    for name, source in (("Python-LICENSE.txt", f"https://raw.githubusercontent.com/python/cpython/v{python_version}/LICENSE"),
                         ("NumPy-LICENSE.txt", f"installed numpy=={numpy_version} {architecture} wheel LICENSE.txt")):
        manifest = [entry for entry in manifest if entry.get("file") != name]
        manifest.append({"file": name, "source": source,
                         "sha256": hashlib.sha256((directory / name).read_bytes()).hexdigest()})
    (directory / "sources.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")


def archive_name(architecture, player="netease"):
    if architecture not in ("arm64", "x86_64"):
        raise ValueError("仅支持原生 arm64 / x86_64 构建")
    if player not in ("netease", "kugou"):
        raise ValueError("未知 Mac 播放器")
    prefix = "dancing-lyrics-0.8.0-beta.1-kugou-macos.1" if player == "kugou" else MAC_PACKAGE_PREFIX
    return f"{prefix}-{architecture}.zip"


def audit_archive(archive_path, player="netease"):
    archive_name("arm64", player)  # Validate before inspecting the archive.
    bundle = KUGOU_APP_BUNDLE if player == "kugou" else APP_BUNDLE
    # ditto stores UTF-8 names without setting the ZIP UTF-8 flag.
    with zipfile.ZipFile(archive_path, metadata_encoding="utf-8") as archive:
        if archive.testzip():
            raise ValueError("ZIP CRC 校验失败")
        names = archive.namelist()
        seen = set()
        for entry in archive.infolist():
            name = entry.filename
            path = PurePosixPath(name)
            if name in seen or path.is_absolute() or ".." in path.parts or "\\" in name:
                raise ValueError(f"无效或重复路径：{name}")
            seen.add(name)
            if "__MACOSX" in path.parts:  # ditto's resource fork metadata
                continue
            if any(part in FORBIDDEN for part in path.parts) or path.suffix.lower() == ".log":
                raise ValueError(f"禁止发布个人文件或外部工具：{name}")
            if stat.S_ISLNK(entry.external_attr >> 16):
                target = archive.read(entry).decode("utf-8")
                resolved = posixpath.normpath(posixpath.join(posixpath.dirname(name), target))
                if target.startswith("/") or not resolved.startswith(path.parts[0] + "/"):
                    raise ValueError(f"符号链接越过发布目录：{name}")
        required = [f"{bundle}/Contents/MacOS/CityEchoes", f"{bundle}/Contents/Info.plist",
                    "使用说明.txt", "安装播放读取工具.command", "LICENSE", "THIRD_PARTY_NOTICES.md",
                    "build-info.json", "licenses/Python-LICENSE.txt", "licenses/NumPy-LICENSE.txt",
                    "licenses/LGPL-3.0.txt"]
        for suffix in required:
            if not any(name.endswith("/" + suffix) for name in names):
                raise ValueError(f"发布包缺少 {suffix}")
        for token in ("/PySide6/QtCore.", "/PySide6/QtGui.", "/PySide6/QtWidgets.",
                      "/PySide6/QtMultimedia.", "libqcocoa.dylib", "_multiarray_umath", "base_library.zip"):
            if not any(token in name for name in names):
                raise ValueError(f"发布包缺少运行依赖：{token}")
        executable = next(entry for entry in archive.infolist()
            if entry.filename.endswith(f"/{bundle}/Contents/MacOS/CityEchoes"))
        if not executable.external_attr >> 16 & 0o111:
            raise ValueError("Mac 主程序缺少可执行权限")
        return {"entries": len(names), "sha256": hashlib.sha256(Path(archive_path).read_bytes()).hexdigest(),
                "size": Path(archive_path).stat().st_size}
