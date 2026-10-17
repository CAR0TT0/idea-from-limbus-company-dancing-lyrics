"""Real Windows reader checks. Requires KuGou playing with desktop lyrics on."""
import argparse
import ctypes as c
from ctypes import wintypes as w
import json
import os
from pathlib import Path
import queue
import signal
import subprocess
import sys
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from kugou_reader import WindowReader
from kugou_windows import Windows, NativeVisibility, PROPERTY


class Helper:
    def __init__(self, command, parent_pid):
        env = os.environ.copy()
        env["PATH"] = str(Path(env.get("SystemRoot", r"C:\Windows")) / "System32")
        self.process = subprocess.Popen(command + ["--kugou-reader", "--parent-pid", str(parent_pid)],
                                        cwd=Path(command[0]).parent, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=subprocess.DEVNULL, env=env, creationflags=0x08000000)
        self.queue = queue.Queue()
        self.worker_pid = self.process.pid
        def read():
            for line in self.process.stdout:
                try:
                    self.queue.put(json.loads(line.decode("utf-8")))
                except (ValueError, UnicodeError):
                    pass
        threading.Thread(target=read, daemon=True).start()

    def command(self, **data):
        self.process.stdin.write((json.dumps(data) + "\n").encode())
        self.process.stdin.flush()

    def packet(self, timeout=7):
        packet = self.queue.get(timeout=timeout)
        self.worker_pid = packet.get("reader_pid", self.worker_pid)
        return packet

    def kill(self):
        # Windows venv python.exe may be a redirector with a different PID.
        os.kill(self.worker_pid, signal.SIGTERM)
        self.process.wait(3)

    def close(self):
        if self.process.poll() is None:
            self.command(quit=True)
            try:
                self.process.wait(3)
            except subprocess.TimeoutExpired:
                NativeVisibility(owner_pid=self.worker_pid).close()
                self.kill()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--exe", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--tray-hidden", action="store_true", help="Hide the main window as KuGou's tray minimization does")
    args = parser.parse_args()
    command = [str(args.exe.resolve())] if args.exe else [sys.executable, str(ROOT / "main.py")]
    windows, reader = Windows(), WindowReader()
    hwnd = reader.find(include_hidden=True)
    if not hwnd:
        raise RuntimeError("KuGou main window unavailable")
    was_minimized = bool(reader.user.IsIconic(hwnd))
    was_visible = bool(reader.user.IsWindowVisible(hwnd))
    reader.user.ShowWindow.argtypes = [w.HWND, c.c_int]
    pid = windows.pid()
    native = [h for h, p, lyric in windows.windows() if lyric and p == pid and windows.user.IsWindowVisible(h)]
    if not native:
        raise RuntimeError("Enable KuGou desktop lyrics before validation")
    checks, samples, helpers = {}, [], []
    sentinel = None
    def visible():
        return all(windows.user.IsWindowVisible(h) and not windows.user.GetPropW(h, PROPERTY) for h in native)
    def hidden(owner):
        return all(not windows.user.IsWindowVisible(h) and windows.user.GetPropW(h, PROPERTY) == owner for h in native)
    def observe(helper, seconds, phase, replace=False):
        until, deadline = None, time.monotonic() + seconds + 10
        rows = []
        while time.monotonic() < deadline and (until is None or time.monotonic() < until):
            if replace:
                helper.command(replace_native=True)
            packet = helper.packet()
            value = packet.get("snapshot")
            row = dict(phase=phase, mode=value.get("mode") if value else None,
                       position=value.get("position_ms") if value else None,
                       playing=value.get("playing") if value else None,
                       main_minimized=bool(reader.user.IsIconic(hwnd)),
                       main_visible=bool(reader.user.IsWindowVisible(hwnd)))
            samples.append(row)
            if value:
                rows.append(row)
                if until is None:
                    until = time.monotonic() + seconds
        return rows
    try:
        reader.user.ShowWindow(hwnd, 0 if args.tray_hidden else 6)
        helper = Helper(command, os.getpid()); helpers.append(helper)
        rows = observe(helper, 2, "cold_minimized")
        checks["coldStartMinimizedUsesNative"] = bool(rows) and all(
            row["mode"] == "native" and (not row["main_visible"] if args.tray_hidden else row["main_minimized"]) for row in rows)
        checks["minimizedProgressAdvances"] = len(rows) >= 2 and rows[-1]["position"] > rows[0]["position"]
        observe(helper, 1.5, "replacement", True)
        checks["replacementHidesOnlyNativeWindow"] = hidden(helper.worker_pid)
        rows = observe(helper, 1.5, "hidden_progress", True)
        checks["hiddenNativeProgressAdvances"] = len(rows) >= 2 and rows[-1]["position"] > rows[0]["position"]
        observe(helper, 3, "heartbeat_lost")
        checks["heartbeatLossRestoresNative"] = visible()
        observe(helper, 1, "replacement_again", True)
        helper.close()
        checks["gracefulExitRestoresNative"] = visible() and helper.process.returncode == 0
        # A private sentinel stands in for a crashed GUI; never terminate KuGou.
        sentinel = subprocess.Popen([sys.executable, "-c", "import os,time; print(os.getpid(),flush=True); time.sleep(60)"], stdout=subprocess.PIPE, creationflags=0x08000000)
        sentinel_pid = int(sentinel.stdout.readline())
        helper = Helper(command, sentinel_pid); helpers.append(helper)
        observe(helper, 2, "parent_test", True)
        checks["parentTestActuallyHidWindow"] = hidden(helper.worker_pid)
        os.kill(sentinel_pid, signal.SIGTERM);sentinel.wait(3)
        helper.process.wait(4)
        checks["parentExitRestoresNative"] = visible() and helper.process.returncode == 0
        # Simulate a reader crash, then verify the next helper reclaims its lease.
        helper = Helper(command, os.getpid());helpers.append(helper)
        observe(helper, 2, "crash_test", True)
        checks["crashTestActuallyHidWindow"] = hidden(helper.worker_pid)
        helper.kill()
        helper = Helper(command, os.getpid());helpers.append(helper)
        observe(helper, 2, "recovery")
        checks["nextHelperRecoversDeadOwner"] = visible()
        helper.close()
    finally:
        for helper in helpers:
            helper.close()
            NativeVisibility(owner_pid=helper.worker_pid).close()
        if sentinel and sentinel.poll() is None:
            os.kill(sentinel_pid, signal.SIGTERM);sentinel.wait(3)
        reader.user.ShowWindow(hwnd, 0 if not was_visible else 6 if was_minimized else 4)
    report = dict(passed=all(checks.values()), checks=checks, samples=samples, packaged=bool(args.exe),
                  clean_machine=False, playback_commands=0, native_window_restored=visible(), tray_hidden=args.tray_hidden)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes((json.dumps(report, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
    print(json.dumps({key: value for key, value in report.items() if key != "samples"}, indent=2))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
