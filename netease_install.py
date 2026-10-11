"""Portable, transactional BetterNCM installer; no Python installation required when frozen."""
import ctypes
from ctypes import wintypes
import hashlib
import json
import os
from pathlib import Path
import secrets
import subprocess
import tempfile
import urllib.request
import zipfile
import uuid
import time
from app_info import NETEASE_VERSIONS
from compatibility import CompatibilityPolicy, client_family

CLIENT_VERSIONS = NETEASE_VERSIONS
CLIENT_VERSION = CLIENT_VERSIONS[-1]
FRAMEWORK_VERSION = "1.3.4"
FRAMEWORK_URL = "https://github.com/std-microblock/chromatic/releases/download/1.3.4/BetterNCMII.dll"
FRAMEWORK_HASH = "a7c77af418d7940e63faa58ea036fba1f4baad497947109ea52ed78c8e86608f"
PLUGIN_NAME = "FloatingLyrics.plugin"


class InstallError(ValueError):
    pass


class InstallCancelled(InstallError):
    pass


class InstallOperation:
    """Cooperative cancellation stops preparation; file transactions finish or roll back."""
    def __init__(self, progress=lambda message: None, cancelled=lambda: False):
        self.progress, self.cancelled = progress, cancelled
        self.committing = False

    def check(self):
        if not self.committing and self.cancelled():
            raise InstallCancelled("操作已取消，未写入客户端。")

    def stage(self, message):
        self.check()
        self.progress(message)

    def begin_commit(self, message):
        self.check()
        self.committing = True
        self.progress(message + "；正在完成文件事务，请稍候。")


def atomic_write(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def file_version(executable):
    """Read the executable's own version resource, rather than registry display strings."""
    api = ctypes.windll.version
    api.GetFileVersionInfoSizeW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(wintypes.DWORD)]
    api.GetFileVersionInfoW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p]
    api.VerQueryValueW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(wintypes.UINT)]
    size = api.GetFileVersionInfoSizeW(str(executable), None)
    if not size:
        raise InstallError("无法读取网易云版本，请选择原版 cloudmusic.exe。")
    data = ctypes.create_string_buffer(size)
    pointer, length = ctypes.c_void_p(), wintypes.UINT()
    if not api.GetFileVersionInfoW(str(executable), 0, size, data) or not api.VerQueryValueW(data, "\\", ctypes.byref(pointer), ctypes.byref(length)):
        raise InstallError("网易云版本信息损坏。")
    # The fixed resource only has 16 bits per component (205529 becomes 8921).
    # Match Windows FileVersionInfo's full string instead of the truncated numeric build.
    translations, translation_size = ctypes.c_void_p(), wintypes.UINT()
    if api.VerQueryValueW(data, r"\VarFileInfo\Translation", ctypes.byref(translations), ctypes.byref(translation_size)):
        words = ctypes.cast(translations, ctypes.POINTER(wintypes.WORD))
        for index in range(0, translation_size.value // 2, 2):
            query = f"\\StringFileInfo\\{words[index]:04x}{words[index + 1]:04x}\\FileVersion"
            version_string, string_size = ctypes.c_void_p(), wintypes.UINT()
            if api.VerQueryValueW(data, query, ctypes.byref(version_string), ctypes.byref(string_size)):
                return ctypes.wstring_at(version_string).strip().replace(",", ".").replace(" ", "")
    values = ctypes.cast(pointer, ctypes.POINTER(wintypes.DWORD))
    high, low = values[2], values[3]
    return f"{high >> 16}.{high & 65535}.{low >> 16}.{low & 65535}"


def client_running():
    try:
        result = subprocess.run(["tasklist.exe", "/FI", "IMAGENAME eq cloudmusic.exe", "/FO", "CSV", "/NH"],
                                capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW, timeout=15)
    except subprocess.TimeoutExpired as error:
        raise InstallError("网易云进程检查超过 15 秒，已停止；请重试或检查 Windows 进程管理是否正常。") from error
    if result.returncode:
        raise InstallError("无法检查网易云进程，请完全退出网易云后重试。")
    return b'"cloudmusic.exe"' in result.stdout.lower()


def find_client():
    candidates = []
    if os.name == "nt":
        import winreg
        for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
            for view in (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY):
                try:
                    with winreg.OpenKey(hive, r"Software\Microsoft\Windows\CurrentVersion\App Paths\cloudmusic.exe", 0, winreg.KEY_READ | view) as key:
                        candidates.append(Path(winreg.QueryValue(key, None).strip('"')).parent)
                except OSError:
                    pass
    for name in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA"):
        if os.environ.get(name):
            candidates.append(Path(os.environ[name]) / "NetEase/CloudMusic")
    return next((path for path in candidates if (path / "cloudmusic.exe").is_file()), None)


def default_profile():
    # BetterNCM 1.3.4 stores its profile at the root of the Windows system drive.
    return Path(os.environ.get("SystemDrive", "C:") + "\\betterncm")


def bridge_contents(state_dir, command, compatibility=None):
    config_path = Path(state_dir) / "netease-bridge.json"
    if config_path.exists():
        try:
            token = json.loads(config_path.read_text(encoding="utf-8-sig"))["token"]
            if not isinstance(token, str) or len(token) != 64 or any(c not in "0123456789abcdefABCDEF" for c in token):
                raise ValueError()
        except (ValueError, KeyError, TypeError) as error:
            raise InstallError("现有连接配置损坏；请先备份 .state/netease-bridge.json 后重试。") from error
    else:
        token = secrets.token_hex(32)
    config = {"token": token, "command": command}
    if compatibility is not None:
        config["compatibility"] = compatibility
    return json.dumps(config, ensure_ascii=False, indent=2).encode("utf-8")


def plugin_bytes(templates, config):
    import io
    result = io.BytesIO()
    with zipfile.ZipFile(result, "w", zipfile.ZIP_DEFLATED) as archive:
        manifest = json.loads((Path(templates) / "manifest.json").read_text(encoding="utf-8"))
        grant = json.loads(config).get("compatibility")
        if grant:
            family = client_family(grant["version"])
            major, minor, patch = map(int, family.split("."))
            # Scope a generated plugin to the installed family, including experimental versions.
            manifest["ncm-version-req"] = f">= {family} < {major}.{minor}.{patch + 1}"
        archive.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
        for name in ("adapter.js", "index.js"):
            archive.write(Path(templates) / name, name)
        archive.writestr("bridge-config.json", config)
    return result.getvalue()


def package_plugin(state_dir, output, command):
    templates = Path(__file__).parent / "plugins/netease"
    config = bridge_contents(state_dir, command)
    data = plugin_bytes(templates, config)
    atomic_write(output, data)
    atomic_write(Path(state_dir) / "netease-bridge.json", config)
    return Path(output)


def verify_plugin(path):
    try:
        with zipfile.ZipFile(path) as archive:
            manifest = json.loads(archive.read("manifest.json"))
            config = json.loads(archive.read("bridge-config.json"))
        if manifest.get("slug") != "floating-lyrics" or not isinstance(config.get("command"), str):
            raise ValueError()
        return config
    except (OSError, ValueError, KeyError, zipfile.BadZipFile) as error:
        raise InstallError("目标位置已有无法识别的插件；请备份后处理，未覆盖或删除。") from error


def download_framework(target, *, operation=None, clock=time.monotonic):
    operation = operation or InstallOperation()
    started = clock()
    try:
        operation.stage("正在从 GitHub 下载 BetterNCM 1.3.4（总时限 45 秒，可取消）")
        with urllib.request.urlopen(FRAMEWORK_URL, timeout=5) as response, Path(target).open("wb") as output:
            size = 0
            while True:
                operation.check()
                if clock() - started >= 45:
                    raise InstallError("BetterNCM 下载超过 45 秒，已停止。请从使用说明的官方地址下载 DLL，选择“本地框架 DLL”后重试。")
                chunk = response.read(65536)
                operation.check()
                if clock() - started >= 45:
                    raise InstallError("BetterNCM 下载超过 45 秒，已停止。请从使用说明的官方地址下载 DLL，选择“本地框架 DLL”后重试。")
                if not chunk:
                    break
                size += len(chunk)
                if size > 32 * 1024 * 1024:
                    raise InstallError("框架下载大小异常。")
                output.write(chunk)
                operation.stage(f"正在下载 BetterNCM 1.3.4 · 已收到 {size / 1024 / 1024:.1f} MiB（总时限 45 秒）")
    except OSError as error:
        raise InstallError("BetterNCM 下载失败。请检查网络后重试；也可从使用说明中的官方地址下载 DLL 并在安装窗口选择。") from error


def framework_valid(path):
    path = Path(path)
    if path.stat().st_size > 32 * 1024 * 1024:
        return False
    return hashlib.sha256(path.read_bytes()).hexdigest() == FRAMEWORK_HASH


class NeteaseInstaller:
    def __init__(self, app_dir, templates, *, version_reader=file_version, running=client_running, downloader=download_framework,
                 policy=None):
        self.app_dir = Path(app_dir).resolve()
        self.state = self.app_dir / ".state"
        self.templates = Path(templates)
        self.version_reader, self.running, self.downloader = version_reader, running, downloader
        self.policy = policy or CompatibilityPolicy(self.state)

    @property
    def command(self):
        return f'"{self.app_dir / "FloatingLyrics.exe"}" --netease --background'

    def assess_client(self, client):
        exe = Path(client).resolve() / "cloudmusic.exe"
        if not exe.is_file():
            raise InstallError("未找到 cloudmusic.exe，请选择网易云安装目录。")
        return self.policy.assess(self.version_reader(exe))

    def check_client(self, client, install=True, experimental=False, operation=None):
        operation = operation or InstallOperation()
        client = Path(client).resolve()
        exe = client / "cloudmusic.exe"
        if not exe.is_file():
            raise InstallError("未找到 cloudmusic.exe，请选择网易云安装目录。")
        operation.stage("正在检查网易云是否已完全退出（进程检查最多 15 秒）")
        if self.running():
            raise InstallError("请先完全退出网易云（包括托盘），再重试。安装器不会结束音乐进程。")
        operation.check()
        if install:
            if not (self.app_dir / "FloatingLyrics.exe").is_file():
                raise InstallError("请使用完整解压的成品安装，不要从源码目录直接安装。")
            operation.stage("正在读取客户端版本并检查安装兼容性")
            support = self.assess_client(client)
            if support.status == "blocked":
                raise InstallError(f"网易云 {support.version} 已知不兼容：{support.reason}，未改动客户端。")
            if support.status == "unverified" and not experimental:
                raise InstallError(f"网易云 {support.version} 尚未验证。请勾选“实验性安装（未验证版本）”并确认风险后重试；未改动客户端。")
            # PE machine type 0x8664 is required by the pinned x64 framework.
            with exe.open("rb") as stream:
                stream.seek(0x3c)
                offset = int.from_bytes(stream.read(4), "little")
                stream.seek(offset)
                if stream.read(6) != b"PE\0\0\x64\x86":
                    raise InstallError("当前仅支持网易云 64 位客户端。")
        return client

    def install(self, client, profile, framework=None, *, experimental=False, operation=None):
        operation = operation or InstallOperation()
        client = self.check_client(client, experimental=experimental, operation=operation)
        support = self.assess_client(client)
        profile = Path(profile).resolve()
        if not str(profile).isascii():
            raise InstallError("BetterNCM 1.3.4 无法可靠加载含中文的插件数据目录。请保留默认 C:\\betterncm 或选择英文路径；歌词程序和网易云目录可含中文。")
        dll, plugin = client / "msimg32.dll", profile / "plugins" / PLUGIN_NAME
        operation.stage("正在检查现有框架和插件文件")
        if dll.exists() and not framework_valid(dll):
            raise InstallError("已有不同版本的 msimg32.dll，未覆盖；请先确认现有 BetterNCM 版本。")
        if plugin.exists():
            verify_plugin(plugin)
        grant = {"version": support.version, "status": support.status, "revision": support.revision}
        config = bridge_contents(self.state, self.command, grant)
        data = plugin_bytes(self.templates, config)
        if not dll.exists() and framework is not None and not Path(framework).is_file():
            raise InstallError("选择的本地框架 DLL 不存在或不是文件，请重新选择；本次不会联网下载框架。")
        operation.check()
        self.state.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="install-", dir=self.state) as temporary:
            source = dll if dll.exists() else Path(framework) if framework else Path(temporary) / "BetterNCMII.dll"
            if not source.exists():
                if self.downloader is download_framework:
                    self.downloader(source, operation=operation)
                else:
                    operation.stage("正在下载框架")
                    self.downloader(source)
            operation.stage("正在校验已有框架 SHA-256" if dll.exists() else
                            "正在校验本地框架 DLL SHA-256（不下载框架）" if framework is not None else
                            "正在校验已下载框架 SHA-256")
            if not framework_valid(source):
                raise InstallError("BetterNCM 文件 SHA-256 校验失败，未安装。")
            paths = [plugin, self.state / "netease-bridge.json", self.state / "netease-install.json"]
            previous = {path: path.read_bytes() if path.exists() else None for path in paths}
            created_dll = not dll.exists()
            receipt = {"version": 1, "client": str(client), "profile": str(profile), "app": str(self.app_dir),
                       "clientVersion": self.version_reader(client / "cloudmusic.exe"), "frameworkVersion": FRAMEWORK_VERSION,
                       "frameworkHash": FRAMEWORK_HASH, "createdFramework": created_dll, "installed": True}
            receipt["compatibility"] = grant
            receipt["backupId"] = uuid.uuid4().hex
            backup = self.state / "netease-backups" / receipt["backupId"]
            operation.stage("正在保存安装前备份")
            backup.mkdir(parents=True)
            updates = {paths[0]: data, paths[1]: config,
                       paths[2]: json.dumps(receipt, ensure_ascii=False, indent=2).encode("utf-8")}
            records = []
            for index, path in enumerate(paths):
                original = previous[path]
                if original is not None:
                    atomic_write(backup / f"{index}.bin", original)
                records.append({"before": None if original is None else hashlib.sha256(original).hexdigest(),
                                "after": hashlib.sha256(updates[path]).hexdigest()})
            atomic_write(backup / "backup.json", json.dumps({"records": records}, indent=2).encode("utf-8"))
            operation.begin_commit("正在安装联动")
            try:
                if created_dll:
                    atomic_write(dll, source.read_bytes())
                atomic_write(plugin, data)
                atomic_write(paths[1], config)
                atomic_write(paths[2], json.dumps(receipt, ensure_ascii=False, indent=2).encode("utf-8"))
            except OSError:
                for path, original in previous.items():
                    if original is None:
                        path.unlink(missing_ok=True)
                    elif not path.exists() or path.read_bytes() != original:
                        atomic_write(path, original)
                if created_dll and dll.exists() and framework_valid(dll):
                    dll.unlink()
                raise
        return receipt

    def restore(self, client, profile, *, operation=None):
        """Undo the latest completed installation; never overwrite a subsequently changed file."""
        operation = operation or InstallOperation()
        client = self.check_client(client, install=False, operation=operation)
        operation.stage("正在校验安装记录和恢复备份")
        profile = Path(profile).resolve()
        try:
            receipt = json.loads((self.state / "netease-install.json").read_text(encoding="utf-8"))
            backup_id = receipt["backupId"]
            if not isinstance(backup_id, str) or len(backup_id) != 32 or any(c not in "0123456789abcdef" for c in backup_id):
                raise ValueError()
            if (Path(receipt["client"]).resolve() != client or Path(receipt["profile"]).resolve() != profile
                    or Path(receipt["app"]).resolve() != self.app_dir):
                raise ValueError()
            backup = self.state / "netease-backups" / backup_id
            records = json.loads((backup / "backup.json").read_text(encoding="utf-8"))["records"]
            if not isinstance(records, list) or len(records) != 3:
                raise ValueError()
            for record in records:
                if not isinstance(record, dict) or set(record) != {"before", "after"}:
                    raise ValueError()
                for key in ("before", "after"):
                    digest = record[key]
                    if key == "before" and digest is None:
                        continue
                    if not isinstance(digest, str) or len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
                        raise ValueError()
        except (OSError, ValueError, KeyError, TypeError) as error:
            raise InstallError("没有可恢复的本目录安装备份，或所选目录与安装记录不符。") from error
        paths = [profile / "plugins" / PLUGIN_NAME, self.state / "netease-bridge.json", self.state / "netease-install.json"]
        previous = {}
        for index, path in enumerate(paths):
            record = records[index]
            current = path.read_bytes() if path.exists() else None
            if current is None or hashlib.sha256(current).hexdigest() != record.get("after"):
                raise InstallError("安装后文件已变化，未自动覆盖；请保留备份并检查当前安装。")
            previous[path] = current
        originals = []
        for index, record in enumerate(records):
            original = (backup / f"{index}.bin").read_bytes() if record.get("before") is not None else None
            if original is not None and hashlib.sha256(original).hexdigest() != record["before"]:
                raise InstallError("安装备份校验失败，未恢复。")
            originals.append(original)
        dll = client / "msimg32.dll"
        remove_dll = bool(receipt["createdFramework"])
        if remove_dll and (not dll.is_file() or not framework_valid(dll)):
            raise InstallError("框架文件已变化，未自动删除。")
        if remove_dll and any(path != paths[0] for path in (profile / "plugins").iterdir()):
            raise InstallError("安装后存在其他共用插件，不能自动移除框架；请先备份其他插件再处理。")
        operation.begin_commit("正在恢复上次安装")
        try:
            for path, original in zip(paths, originals):
                if original is None:
                    path.unlink()
                else:
                    atomic_write(path, original)
            # Last operation: only remove the exact DLL created by this installation.
            if remove_dll:
                dll.unlink()
        except OSError:
            for path, original in previous.items():
                atomic_write(path, original)
            raise
        return {"restored": True, "removedCreatedFramework": remove_dll, "preservedSettings": True}

    def uninstall(self, client, profile, *, operation=None):
        operation = operation or InstallOperation()
        self.check_client(client, install=False, operation=operation)
        operation.stage("正在检查待卸载插件")
        plugin = Path(profile).resolve() / "plugins" / PLUGIN_NAME
        removed = False
        if plugin.exists():
            config = verify_plugin(plugin)
            if config["command"] != self.command:
                raise InstallError("此插件指向另一份歌词程序，请从那份程序的卸载入口操作。")
            operation.begin_commit("正在卸载联动")
            plugin.unlink()
            removed = True
        # Preferences, token, imported fonts, presets and the shared framework stay intact.
        return {"removedPlugin": removed, "preservedFramework": True, "preservedSettings": True}
