"""Record actual KuGou data, with explicit failed and unobserved checks."""
import json
import ctypes
from ctypes import wintypes
from pathlib import Path
import time
from PySide6.QtCore import QTimer


class KugouSmokeCheck:
    def __init__(self, app, panel, directory):
        self.app, self.panel = app, panel
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.started = time.monotonic()
        self.samples = []
        self.effect_check = None
        from kugou_windows import Windows
        self.windows = Windows()
        self.windows.user.IsIconic.argtypes = [wintypes.HWND]
        self._window_at = 0
        self._main_state = (False, False)
        self.epoch = 0
        self.player_epoch_connection = panel.player.discontinuity.connect(self._discontinuity)
        self.timer = QTimer(panel)
        self.timer.setInterval(16)
        self.timer.timeout.connect(self.sample)
        self.timer.start()

    def _discontinuity(self):
        self.epoch += 1

    def sample(self):
        player = self.panel.player
        doc = self.panel.overlay.document
        now = time.monotonic()
        if now >= self._window_at:
            mains = [h for h, _, lyric in self.windows.windows() if not lyric]
            self._main_state = (any(self.windows.user.IsWindowVisible(h) for h in mains),
                                any(self.windows.user.IsIconic(h) for h in mains))
            self._window_at = now + .5
        position = player._timeline_clock.position(now)
        snapshot = player._snapshot
        expected = (min(player.duration, snapshot.position_ms + (min(.25, max(0, now - player._at)) * 1000 * snapshot.rate
                        if player.playing else 0)) if snapshot and snapshot.mode == "native" else None)
        self.samples.append(dict(seconds=round(now - self.started, 3), connected=player.connected,
                                 epoch=self.epoch,
                                 title=player.title, artist=player.artist, playing=player.playing,
                                 position=round(position), duration=player.duration,
                                 native_error_ms=round(abs(position - expected), 1) if expected is not None else None,
                                 explicit_end_lines=sum(line.end_ms is not None for line in doc.lines) if doc else 0,
                                 mode=player._snapshot.mode if player._snapshot else "",
                                 main_visible=self._main_state[0], main_minimized=self._main_state[1],
                                 lines=len(doc.lines) if doc else 0,
                                 words=sum(len(line.words) for line in doc.lines) if doc else 0,
                                 translations=sum(bool(line.translation) for line in doc.lines) if doc else 0,
                                 status=player.compatibility_status, lyric_status=player.lyric_status))
        if doc and self.effect_check is None:
            prefs = self.panel.prefs
            before = (player.song_id, player._anchor, player.playing, player.duration)
            original = (prefs.animation_style, prefs.lyric_display_mode, prefs.text_style, prefs.scramble_exit)
            for style in ("classic", "ripple_wave", "ripple_shake", "fall_wave", "fall_shake"):
                self.panel._set_preference("animation_style", style)
            self.panel._set_preference("text_style", "gradient_fill")
            self.panel._set_preference("scramble_exit", True)
            for mode in ("original", "translation", "bilingual_original", "bilingual_translation"):
                self.panel._set_preference("lyric_display_mode", mode)
            for name, value in zip(("animation_style", "lyric_display_mode", "text_style", "scramble_exit"), original):
                self.panel._set_preference(name, value)
            self.effect_check = before == (player.song_id, player._anchor, player.playing, player.duration)
        if time.monotonic() - self.started >= 45:
            self.finish()

    def finish(self):
        self.timer.stop()
        self.panel.grab().save(str(self.directory / "kugou-panel.png"))
        self.panel.show_effects()
        self.panel.grab().save(str(self.directory / "kugou-effects.png"))
        checks = dict(connected=any(row["connected"] for row in self.samples),
                      nativePanelVisible=bool(self.windows.user.IsWindowVisible(int(self.panel.winId()))),
                      realLyricsReceived=any(row["lines"] for row in self.samples),
                      advancing=any(b["position"] > a["position"] for a, b in zip(self.samples, self.samples[1:])
                                    if a["title"] == b["title"] and a["playing"] and b["playing"]),
                      effectsKeepTransport=self.effect_check is True)
        steady = [(a, b) for a, b in zip(self.samples, self.samples[1:])
                  if a["connected"] and b["connected"] and a["playing"] and b["playing"]
                  and a["title"] == b["title"] and a["epoch"] == b["epoch"]]
        checks["continuousClockNeverRewinds"] = bool(steady) and all(b["position"] >= a["position"] for a, b in steady)
        # Timestamp and position use the same clock instant. Separate calls can
        # falsely report a jump if Windows preempts this thread between them.
        checks["continuousClockNoPacketJumps"] = bool(steady) and all(
            b["position"] - a["position"] <= (b["seconds"] - a["seconds"]) * 1150 + 3 for a, b in steady)
        errors = [row["native_error_ms"] for row in self.samples if row["connected"] and row["playing"] and row["native_error_ms"] is not None]
        if errors:
            checks["nativeClockWithin350ms"] = max(errors) <= 350
        background = [row for row in self.samples if row['seconds'] >= 8 and (row['main_minimized'] or not row['main_visible'])]
        if background:
            checks['backgroundConnected'] = sum(row['connected'] for row in background) >= len(background)*.95
            checks['backgroundChannelIndependentOfWindow'] = all(row['mode'] in ('native','smtc') for row in background if row['connected'])
            checks['backgroundLyricsReceived'] = any(row['lines'] for row in background)
            checks['backgroundProgressAdvances'] = any(b['position'] > a['position'] for a,b in zip(background,background[1:]) if a['epoch']==b['epoch'] and a['playing'] and b['playing'])
        report = dict(passed=all(checks.values()), checks=checks, samples=self.samples,
                      steadyPairCount=len(steady),
                      note="Only real KuGou observations. Pause/seek/switch/minimize require separate observed evidence.")
        (self.directory / "kugou-report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        self.app.exit(0 if report["passed"] else 1)
