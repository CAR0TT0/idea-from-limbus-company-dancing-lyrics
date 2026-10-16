"""Record actual KuGou data, with explicit failed and unobserved checks."""
import json
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
        self.samples.append(dict(seconds=round(time.monotonic() - self.started, 2), connected=player.connected,
                                 epoch=self.epoch,
                                 title=player.title, artist=player.artist, playing=player.playing,
                                 position=round(player.position()), duration=player.duration,
                                 mode=player._snapshot.mode if player._snapshot else "",
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
                      realLyricsReceived=any(row["lines"] for row in self.samples),
                      advancing=any(b["position"] > a["position"] for a, b in zip(self.samples, self.samples[1:])
                                    if a["title"] == b["title"] and a["playing"] and b["playing"]),
                      effectsKeepTransport=self.effect_check is True)
        steady = [(a, b) for a, b in zip(self.samples, self.samples[1:])
                  if a["connected"] and b["connected"] and a["playing"] and b["playing"]
                  and a["title"] == b["title"] and a["epoch"] == b["epoch"]]
        checks["continuousClockNeverRewinds"] = bool(steady) and all(b["position"] >= a["position"] for a, b in steady)
        # Rounded sample times allow up to 10 ms measurement uncertainty.
        checks["continuousClockNoPacketJumps"] = bool(steady) and all(
            b["position"] - a["position"] <= (b["seconds"] - a["seconds"]) * 1150 + 15 for a, b in steady)
        report = dict(passed=all(checks.values()), checks=checks, samples=self.samples,
                      steadyPairCount=len(steady),
                      note="Only real KuGou observations. Pause/seek/switch/minimize require separate observed evidence.")
        (self.directory / "kugou-report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        self.app.exit(0 if report["passed"] else 1)
