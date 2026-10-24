"""Run the real GUI across minimized/tray states, preserving client playback.

The client must already be playing with desktop lyrics enabled. Reports contain
real observations; this tool never sends playback, seek or next-track commands.
"""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from kugou_windows import Windows


def verify(output, exe=None, bat=False):
    windows = Windows()
    from ctypes import wintypes
    windows.user.IsIconic.argtypes = [wintypes.HWND]
    mains = [h for h, _, lyric in windows.windows() if not lyric]
    if len(mains) != 1:
        raise RuntimeError('请打开一个酷狗音乐实例并播放有歌词的歌曲。')
    hwnd = mains[0]
    visible, minimized = bool(windows.user.IsWindowVisible(hwnd)), bool(windows.user.IsIconic(hwnd))
    output = output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    report_path = output/'kugou-report.json'
    if report_path.exists():
        raise RuntimeError('请使用新的验证输出目录，避免读取旧报告。')
    env = dict(os.environ)
    env.pop('QT_QPA_PLATFORM', None)
    env['LOCALAPPDATA'] = str(output/'profile')
    if exe:
        for name in ('VIRTUAL_ENV','PYTHONHOME','PYTHONPATH'):
            env.pop(name, None)
        env['PATH'] = os.pathsep.join((str(exe.parent), str(Path(os.environ['WINDIR'])/'System32'), os.environ['WINDIR']))
    arguments = ['--kugou-smoke','--report-dir',str(output)]
    process = None
    states = []
    started = time.monotonic()
    parent_env = dict(os.environ)
    try:
        windows.user.ShowWindow(hwnd, 6)
        if bat:
            os.environ.clear()
            os.environ.update(env)
            os.startfile(str(exe.parent/'启动酷狗联动.bat'), arguments=subprocess.list2cmdline(arguments))
        else:
            command = [str(exe)] if exe else [sys.executable, str(Path(__file__).resolve().parents[1]/'main.py')]
            process = subprocess.Popen(command+['--kugou']+arguments, env=env, creationflags=0x08000000)
        phase = -1
        while time.monotonic()-started < 90:
            elapsed = time.monotonic()-started
            index = 0 if elapsed < 10 else 1 if elapsed < 18 else 2 if elapsed < 28 else 3
            if index != phase:
                windows.user.ShowWindow(hwnd, (6,4,6,0)[index])
                states.append(dict(seconds=round(elapsed,2),phase=('cold_minimized','restored','minimized','tray_hidden')[index]))
                phase = index
            if report_path.exists():
                break
            if process and process.poll() is not None:
                raise RuntimeError('歌词程序提前退出：'+str(process.returncode))
            time.sleep(.2)
        data = json.loads(report_path.read_text(encoding='utf-8'))
        if process:
            process.wait(timeout=10)
        else:
            time.sleep(1)
        checks = dict(data['checks'])
        cold = [r for r in data['samples'] if r['seconds'] < 8 and r['connected']]
        checks['coldStartMinimized'] = bool(cold) and all(r['main_minimized'] and r['mode'] in ('native','smtc') for r in cold)
        checks['nativeBackgroundAfterMinimizeObserved'] = 'backgroundProgressAdvances' in data['checks']
        result = dict(passed=all(checks.values()),checks=checks,packaged=bool(exe),actual_bat_entry=bat,
                      developer_path_removed=bool(exe),clean_machine=False,simulated_live_data=False,
                      playback_commands=0,window_states=states,seconds=round(time.monotonic()-started,2),
                      max_lines=max(r['lines'] for r in data['samples']),max_words=max(r['words'] for r in data['samples']),
                      max_translations=max(r['translations'] for r in data['samples']))
        (output/'background-report.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
        print(json.dumps(result,ensure_ascii=False),flush=True)
        return result['passed']
    finally:
        windows.user.ShowWindow(hwnd, 0 if not visible else 6 if minimized else 4)
        os.environ.clear()
        os.environ.update(parent_env)
        if process and process.poll() is None:
            process.terminate()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--exe',type=Path)
    parser.add_argument('--bat',action='store_true')
    args = parser.parse_args()
    if args.bat and not args.exe:
        parser.error('--bat requires --exe')
    raise SystemExit(0 if verify(args.output,args.exe,args.bat) else 1)
