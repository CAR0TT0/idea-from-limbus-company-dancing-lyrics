"""One-shot read-only diagnostic. Omits track names, artist and lyric contents."""
import json
from pathlib import Path
import platform
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def probe():
    report = {"platform": sys.platform, "architecture": platform.machine(),
              "macos": platform.mac_ver()[0], "playback_detected": False,
              "background_verified": False, "window_close_verified": False}
    if sys.platform != "darwin":
        report["error"] = "需要在 macOS 运行此诊断。"
        return report
    from macos_kugou import find_media_control, query_media_control
    cli = find_media_control()
    report["reader_found"] = bool(cli)
    if not cli:
        report["error"] = "未找到 media-control；请重新运行安装入口。"
        return report
    try:
        info = query_media_control(cli)
        if info:
            report.update(playback_detected=True, playing=info.state == "playing",
                          position_ms=info.position_ms, duration_ms=info.duration_ms)
        else:
            report["error"] = "系统当前播放来源不是酷狗，或酷狗没有提供完整播放信息。"
    except Exception as error:
        report["error"] = str(error)
    return report


if __name__ == "__main__":
    report = probe()
    print(json.dumps(report, ensure_ascii=False, indent=2))
    raise SystemExit(0 if report["playback_detected"] else 1)
