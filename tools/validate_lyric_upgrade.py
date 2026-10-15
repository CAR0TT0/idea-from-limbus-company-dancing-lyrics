"""Native screenshots, reproducible lyric-material benchmarks and a Qt recording.

Uses an isolated settings directory and synthetic, explicitly labelled lyric times.
Does not launch or control a music client.
"""
import argparse
import ctypes
import json
import os
from dataclasses import replace
from pathlib import Path
import sys
import time
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import numpy as np
from PySide6.QtCore import QRectF, QPointF, QPoint, Qt
from PySide6.QtGui import QColor, QPainter, QWheelEvent
from PySide6.QtWidgets import QApplication, QWidget
from PySide6.QtTest import QTest
from player import MusicPlayer
from overlay import LyricsOverlay
from controls import ControlPanel
from settings import Preferences, SettingsStore
from fonts import FontLibrary
from lrc import LyricLine, LyricDocument, LyricTimeline, TimedWord, display_document
from animation import _glyph_layout
from glyph_motion import glyph_states
from text_effects import TextEffects, transparent_image


def scene(renderer, prefs, dpr):
    raw = LyricDocument([
        LyricLine(0, "Let's visit the library one day.", (TimedWord(800, 1300, 0, 5),), "啊，对了！但丁。有一天，去一趟图书馆吧。"),
        LyricLine(3000, "Follow the rhythm of the city.", (), "让文字跟随都市的节奏。"),
        LyricLine(6000, "")], [])
    document = display_document(raw, mode="bilingual_original")
    timeline = LyricTimeline(document, animation_style=prefs.animation_style, scramble_exit=prefs.scramble_exit)
    surfaces = []
    for line in document.lines[:2]:
        glyphs, _, _ = _glyph_layout(line.text, 32, 700, prefs.font_family, line.secondary_start)
        surfaces.append(renderer.prepare(glyphs, 32, prefs, dpr, prefs.jump))
    return timeline, surfaces


def paint_scene(renderer, prefs, timeline, surfaces, position, dpr):
    canvas = transparent_image(QRectF(0, 0, 1180, 560), dpr)
    canvas.fill(QColor("#18232f"))
    painter = QPainter(canvas)
    for item in timeline.visible(position, 6000):
        surface = surfaces[item.index]
        states = glyph_states(surface.glyphs, prefs, item, position, .55, 32, 730 + item.index)
        image = renderer.render(surface, prefs, states=states)
        painter.save()
        painter.setOpacity(prefs.opacity / 100)
        painter.translate(590, 170 if item.index == 0 else 390)
        painter.drawImage(surface.origin, image)
        painter.restore()
    painter.end()
    return canvas


class FrameWindow(QWidget):
    def paintEvent(self, event):
        painter = QPainter(self)
        painter.drawImage(QPointF(), self.picture)
        painter.end()


def validate(directory, scale, benchmark=False):
    directory.mkdir(parents=True, exist_ok=True)
    app = QApplication([])
    prefs = Preferences(text_style="glow", glow_variant="gradient", lyric_display_mode="bilingual_original", scramble_exit=True)
    player = MusicPlayer()
    overlay = LyricsOverlay(player, prefs)
    fonts = FontLibrary(directory / "fonts")
    panel = ControlPanel(player, overlay, prefs, SettingsStore(directory / "settings.ini"), font_library=fonts)
    panel.show_effects()
    QTest.qWait(200)
    checks = {"native_window_visible": bool(ctypes.windll.user32.IsWindowVisible(int(panel.winId()))),
              "requested_dpr": abs(panel.devicePixelRatioF() - scale) < .01}
    fields = [panel.screen_combo, panel.language_combo, panel.gradient_field, panel.gradient_direction,
              panel.scramble_checkbox, panel.instrumental_checkbox, panel.font_preview, panel.replay_button]
    for name, size in (("default", (1100, 760)), ("minimum", (900, 560))):
        panel.resize(*size)
        QTest.qWait(100)
        for index, widget in enumerate(fields):
            panel.effects_scroll.ensureWidgetVisible(widget, 0, 0)
            app.processEvents()
            point = widget.mapTo(panel.effects_scroll.viewport(), widget.rect().center())
            checks[f"{name}_reachable_{index}"] = panel.effects_scroll.viewport().rect().contains(point)
        checks[name + "_player_bar"] = panel.rect().contains(panel.player_bar.geometry())
        panel.effects_scroll.verticalScrollBar().setValue(0)
        panel.grab().save(str(directory / f"{name}.png"))
    panel.resize(1100, 760)
    panel.effects_scroll.ensureWidgetVisible(panel.animation_combo, 0, 0)
    app.processEvents()
    old_index = panel.animation_combo.currentIndex()
    before = panel.effects_scroll.verticalScrollBar().value()
    local = QPointF(panel.animation_combo.rect().center())
    event = QWheelEvent(local, QPointF(panel.animation_combo.mapToGlobal(local.toPoint())), QPoint(), QPoint(0, -120),
                        Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier, Qt.ScrollPhase.NoScrollPhase, False)
    app.sendEvent(panel.animation_combo, event)
    if not event.isAccepted():
        app.sendEvent(panel.effects_scroll.viewport(), QWheelEvent(QPointF(10,10), QPointF(10,10), QPoint(), QPoint(0,-120),
                      Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier, Qt.ScrollPhase.NoScrollPhase, False))
    checks["wheel_does_not_select"] = panel.animation_combo.currentIndex() == old_index
    checks["page_still_scrolls"] = panel.effects_scroll.verticalScrollBar().value() > before
    prefs.scramble_exit = False
    panel._sync_effect_widgets()
    for style, variant in (("glow", "gradient"), ("gradient_fill", "standard")):
        panel._set_preference("text_style", style)
        panel._set_preference("glow_variant", variant)
        for mode in ("bilingual_original", "bilingual_translation"):
            panel._set_preference("lyric_display_mode", mode)
            panel.font_preview._stop_preview()
            panel.font_preview._running = False
            panel.font_preview._position = 1600
            for dark in (True, False):
                panel.font_preview.set_background(dark)
                app.processEvents()
                panel.font_preview.grab().save(str(directory / f"{style}-{mode}-{'dark' if dark else 'light'}.png"))
    native_screens = len(app.screens())
    for screen in app.screens():
        from screens import screen_key
        prefs.display_screen = screen_key(screen)
        overlay.refresh_screen()
        checks["selected_screen_geometry_" + screen.name()] = overlay.geometry() == screen.availableGeometry()
    measurements = []
    font_file = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts/simkai.ttf"
    if font_file.is_file():
        families, _ = fonts.import_font(font_file)
        imported = FontLibrary(directory / "fonts")
        checks["imported_font_survives_reload"] = imported.restore_family(families[0])[0] == families[0]
        renderer = TextEffects()
        for family in ("SimSun", "KaiTi", families[0]):
            for style in ("glow", "gradient_fill"):
                custom = Preferences(font_family=family, text_style=style, glow_variant="gradient", scramble_exit=True)
                timeline, surfaces = scene(renderer, custom, scale)
                picture = paint_scene(renderer, custom, timeline, surfaces, 3200, scale)
                checks[f"font_render_{family}_{style}"] = not picture.isNull() and all(surface.glyphs for surface in surfaces)
    if benchmark:
        for style in ("glow", "gradient_fill"):
            renderer = TextEffects()
            for animation in ("ripple_wave", "ripple_shake", "fall_wave", "fall_shake"):
                for singing in (False, True):
                    sample_prefs = Preferences(text_style=style, glow_variant="gradient", animation_style=animation,
                                               scramble_exit=True, singing_sync=singing, lyric_display_mode="bilingual_original")
                    started = time.perf_counter()
                    timeline, surfaces = scene(renderer, sample_prefs, scale)
                    cold = (time.perf_counter() - started) * 1000
                    for index in range(5):
                        paint_scene(renderer, sample_prefs, timeline, surfaces, 3100 + index * 5, scale)
                    misses = renderer.misses
                    elapsed = []
                    for index in range(90):
                        started = time.perf_counter()
                        paint_scene(renderer, sample_prefs, timeline, surfaces, 3050 + index * 5, scale)
                        elapsed.append((time.perf_counter() - started) * 1000)
                    row = {"style": style, "animation": animation, "singing": singing, "dpr": scale,
                           "cold_ms": round(cold, 3), "p95_ms": round(float(np.percentile(elapsed, 95)), 3),
                           "cache_bytes": renderer.cache_bytes, "warm_blur_reused": renderer.misses == misses}
                    measurements.append(row)
                    print(json.dumps(row), flush=True)
        checks["p95_under_33ms"] = all(row["p95_ms"] <= 33 for row in measurements)
        checks["cache_within_64mib"] = all(row["cache_bytes"] <= 64 * 1024 * 1024 for row in measurements)
        checks["warm_blur_reused"] = all(row["warm_blur_reused"] for row in measurements)
        renderer = TextEffects()
        demo = Preferences(glow_variant="gradient", scramble_exit=True, animation_style="ripple_wave")
        timeline, surfaces = scene(renderer, demo, scale)
        frames = directory / "frames"
        frames.mkdir(exist_ok=True)
        recording_window = FrameWindow()
        recording_window.picture = paint_scene(renderer, demo, timeline, surfaces, 0, scale)
        recording_window.resize(1180, 560)
        recording_window.setWindowTitle("都市回响 · 双语渐变与乱码退场演示")
        recording_window.show()
        QTest.qWait(100)
        recording_visible = bool(ctypes.windll.user32.IsWindowVisible(int(recording_window.winId())))
        for frame in range(120):
            recording_window.picture = paint_scene(renderer, demo, timeline, surfaces, frame * 1000 / 30, scale)
            recording_window.repaint()
            recording_window.grab().save(str(frames / f"{frame:04d}.png"))
        recording_window.hide()
        (directory / "recording.json").write_text(json.dumps({"frames":120,"fps":30,"synthetic_lyric_times":True,
            "nativeWindowVisible":recording_visible,"logicalSize":[1180,560]}),encoding="utf-8")
    result = {"passed":all(checks.values()), "checks":checks, "dpr":panel.devicePixelRatioF(),
              "native_screens":native_screens,"hotplug_hardware_test":False,"benchmarks":measurements,
              "music_client_commands":0,"synthetic_lyric_times":True}
    (directory / "report.json").write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding="utf-8")
    print(json.dumps({"passed":result["passed"],"checks":checks,"screens":native_screens},ensure_ascii=False),flush=True)
    overlay.hide()
    panel.tray.hide()
    panel.hide()
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--scale", type=float, default=1)
    parser.add_argument("--benchmark", action="store_true")
    args = parser.parse_args()
    raise SystemExit(validate(args.output, args.scale, args.benchmark))
