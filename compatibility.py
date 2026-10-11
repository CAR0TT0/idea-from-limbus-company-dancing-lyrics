"""Data-only client policy. HTTPS refresh is optional; capability probes run locally."""
from dataclasses import dataclass
import json
from pathlib import Path
import re
import time
import urllib.request

POLICY_URL = "https://raw.githubusercontent.com/Celine258/idea-from-limbus-company-dancing-lyrics/main/compatibility/clients.json"
MAX_BYTES = 65536
CACHE_AGE = 7 * 86400


def client_family(version):
    if not isinstance(version, str) or not re.fullmatch(r"\d{1,6}\.\d{1,6}\.\d{1,6}(?:\.\d{1,6})?", version):
        raise ValueError("客户端版本格式无效")
    return ".".join(version.split(".")[:3])


def validate_policy(data):
    if (not isinstance(data, dict) or set(data) != {"schema_version", "revision", "netease"}
            or type(data.get("schema_version")) is not int or data["schema_version"] != 1
            or type(data.get("revision")) is not int or data["revision"] < 1):
        raise ValueError("兼容列表格式或版本无效")
    entries = data.get("netease")
    if not isinstance(entries, list) or len(entries) > 128:
        raise ValueError("兼容列表数量无效")
    seen = set()
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != {"version", "status", "reason"}:
            raise ValueError("兼容列表字段无效")
        version = entry["version"]
        client_family(version)
        if len(version.split(".")) != 4 or version in seen or entry["status"] not in ("verified", "blocked"):
            raise ValueError("兼容列表客户端无效")
        if not isinstance(entry["reason"], str) or len(entry["reason"]) > 400:
            raise ValueError("兼容列表说明无效")
        seen.add(version)
    return data


def fetch_policy():
    request = urllib.request.Request(POLICY_URL, headers={"User-Agent": "CityEchoes-Compatibility/1"})
    with urllib.request.urlopen(request, timeout=5) as response:
        if response.geturl() != POLICY_URL:
            raise ValueError("兼容列表来源发生变化")
        raw = response.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise ValueError("兼容列表过大")
    return validate_policy(json.loads(raw))


@dataclass(frozen=True)
class ClientSupport:
    version: str
    status: str
    reason: str
    revision: int

    @property
    def label(self):
        return {"verified": "已验证兼容", "unverified": "未验证，可实验性安装", "blocked": "已知不兼容，禁止安装"}[self.status]


class CompatibilityPolicy:
    def __init__(self, state, *, fetcher=fetch_policy, clock=time.time, bundled=None):
        self.path = Path(state) / "compatibility-cache.json"
        self.fetcher, self.clock = fetcher, clock
        self.bundled = validate_policy(bundled if bundled is not None else json.loads(
            (Path(__file__).parent / "compatibility/clients.json").read_text(encoding="utf-8")))
        self.data = self.bundled
        self.message = "使用内置兼容列表；联网失败仍可离线安装。"
        try:
            cache = json.loads(self.path.read_text(encoding="utf-8"))
            age = clock() - cache["fetched_at"]
            data = validate_policy(cache["policy"])
            if 0 <= age <= CACHE_AGE and data["revision"] >= self.data["revision"]:
                self.data = data
                self.message = "使用最近 7 天的在线兼容列表缓存。"
            elif age > CACHE_AGE and data["revision"] >= self.data["revision"]:
                self.data = {**data, "netease": [entry for entry in data["netease"] if entry["status"] == "blocked"]}
                self.message = "在线缓存超过 7 天，沿用内置列表并保留已知不兼容记录。"
        except (OSError, ValueError, KeyError, TypeError):
            pass

    def assess(self, version):
        client_family(version)
        # Online policy may revoke a bundled version. Missing entries retain the bundled baseline.
        entries = {entry["version"]: entry for entry in self.bundled["netease"]}
        entries.update({entry["version"]: entry for entry in self.data["netease"]})
        entry = entries.get(version, {"status": "unverified", "reason": "尚未实测；是否可用取决于框架和播放接口。"})
        return ClientSupport(version, entry["status"], entry["reason"], self.data["revision"])

    def refresh(self):
        from netease_install import atomic_write
        try:
            data = validate_policy(self.fetcher())
            if data["revision"] < self.data["revision"]:
                raise ValueError("在线兼容列表版本较旧")
            content = json.dumps({"fetched_at": self.clock(), "policy": data}, ensure_ascii=False).encode("utf-8")
            atomic_write(self.path, content)
            self.data = data
            self.message = "在线兼容列表已更新。"
            return True
        except (OSError, ValueError, TypeError):
            self.message = "在线兼容列表更新失败，沿用内置列表或有效缓存，可稍后重试。"
            return False


def bridge_client(config):
    """Only a locally installed policy grants a new family; packets cannot grant themselves access."""
    grant = config.get("compatibility", {})
    if not isinstance(grant, dict) or grant.get("status") not in ("verified", "unverified"):
        return None
    return client_family(grant.get("version"))
