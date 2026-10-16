"""Stateless character choreography: the song position is the only animation clock."""
from dataclasses import dataclass, replace
import math
from PySide6.QtCore import QRectF
from PySide6.QtGui import QTransform


@dataclass(frozen=True)
class GlyphState:
    x: float
    y: float
    opacity: float = 1
    scale: float = 1
    rotation: float = 0
    emphasis: float = 0
    replacement: int = -1


def smooth(value):
    value = max(0., min(1., value))
    return value * value * (3 - 2 * value)


def noise_value(seed, rank, axis, sample):
    value = (seed * 0x9E3779B1 + rank * 0x85EBCA77 + axis * 0xC2B2AE3D + sample) & 0xFFFFFFFF
    value = ((value ^ (value >> 16)) * 0x7FEB352D) & 0xFFFFFFFF
    value = ((value ^ (value >> 15)) * 0x846CA68B) & 0xFFFFFFFF
    return ((value ^ (value >> 16)) / 0xFFFFFFFF) * 2 - 1


def noise(seed, rank, axis, seconds, frequency=6):
    clock = seconds * frequency
    sample = math.floor(clock)
    weight = smooth(clock - sample)
    first = noise_value(seed, rank, axis, sample)
    return first + (noise_value(seed, rank, axis, sample + 1) - first) * weight


def stagger(position, start, end, rank, count, duration):
    total = max(0., end - start)
    if total == 0:
        return float(position >= end)
    duration = min(duration, total)
    delay = (total - duration) * rank / (count - 1) if count > 1 else 0
    return max(0., min(1., (position - start - delay) / duration))


def word_emphasis(glyph, words, position):
    weight = 0.
    for word in words:
        if glyph.text_start < word.text_end and glyph.text_end > word.text_start and word.start_ms <= position < word.end_ms:
            ramp = min(80, (word.end_ms - word.start_ms) / 4)
            weight = max(weight, smooth(min((position - word.start_ms) / ramp, (word.end_ms - position) / ramp)))
    return weight


def scramble_state(state, rank, count, item, position, prefs, seed, pixels):
    if not prefs.scramble_exit or position < item.exit_start_ms:
        return state
    total = max(0, item.end_ms - item.exit_start_ms)
    duration = total if count == 1 else min(160 * 100 / prefs.exit_speed, 2 * total / max(1, count))
    phase = stagger(position, item.exit_start_ms, item.end_ms, rank, count, duration)
    if phase <= 0:
        return state
    if phase >= 1:
        return replace(state, opacity=0, emphasis=0, replacement=-1)
    sample = math.floor((position - item.exit_start_ms) * 12 / 1000)
    symbol = min(7, int((noise_value(seed, rank, 7, sample) + 1) * 4))
    opacity = state.opacity * (1 - smooth((phase - .4) / .6))
    return replace(state, x=state.x + noise_value(seed, rank, 8, sample) * pixels / 32,
                   opacity=opacity, emphasis=0, replacement=symbol)


def glyph_states(glyphs, prefs, item, position_ms, energy, pixels, seed=0, angle=0, moving=True):
    visible = [glyph for glyph in glyphs if not glyph.path.isEmpty()]
    count, rank = len(visible), 0
    group_counts = {language: sum(glyph.language == language for glyph in visible) for language in (0, 1)}
    ranks = {0: 0, 1: 0}
    seconds = position_ms / 1000
    amplitude = prefs.jump * (.35 + .65 * max(0., min(1., energy)))
    falling = prefs.animation_style.startswith("fall_") and not prefs.scramble_exit
    shaking = prefs.animation_style.endswith("_shake")
    radians = math.radians(angle)
    result = []
    for glyph in glyphs:
        if glyph.path.isEmpty():
            result.append(GlyphState(glyph.x, glyph.baseline, 0))
            continue
        count, rank = group_counts[glyph.language], ranks[glyph.language]
        ranks[glyph.language] += 1
        emphasis = word_emphasis(glyph, item.words, position_ms) if prefs.singing_sync else 0
        if prefs.animation_style == "classic":
            dy = -prefs.jump * energy * (0.6 + 0.4 * math.sin(seconds * 8 - glyph.index * .6)) if moving else 0
            scale = (1 + .04 * energy if moving else 1) * (1 + .08 * emphasis)
            entering = min(1, max(0, (position_ms - item.start_ms) / max(1, item.entry_end_ms-item.start_ms))) if prefs.scramble_exit else 1
            state = GlyphState(glyph.x, glyph.baseline + dy, entering, scale, 0, emphasis)
            result.append(scramble_state(state, rank, count, item, position_ms, prefs, seed + glyph.language * 100003, pixels))
            continue
        entering = stagger(position_ms, item.start_ms, item.entry_end_ms, rank, count, 140 * 100 / prefs.entry_speed)
        leaving = stagger(position_ms, item.exit_start_ms, item.end_ms, rank, count,
                          (350 if falling else 200) * 100 / prefs.exit_speed)
        opacity = smooth(entering) * (1 if prefs.scramble_exit else 1 - smooth(leaving))
        if shaking:
            dx = amplitude * .3 * noise(seed, rank, 0, seconds, prefs.shake_frequency)
            dy = amplitude * .5 * noise(seed, rank, 1, seconds, prefs.shake_frequency)
        else:
            dx = 0
            dy = amplitude * .4 * math.sin(seconds * 2 * math.pi * 1.5 - rank * .6)
        distance = prefs.fall_distance * pixels / 32 * leaving ** 2 if falling else 0
        rotation = (8 if noise_value(seed, rank, 2, 0) >= 0 else -8) * leaving ** 2 if falling else 0
        # Convert screen-vertical fall to the tilted sentence's local coordinates.
        state = GlyphState(glyph.x + dx + math.sin(radians) * distance,
                                 glyph.baseline + dy + math.cos(radians) * distance,
                                 opacity, 1 + .08 * emphasis if opacity > 0 else 1, rotation,
                                 emphasis if opacity > 0 else 0)
        result.append(scramble_state(state, rank, count, item, position_ms, prefs, seed + glyph.language * 100003, pixels))
        rank += 1
    return result


def motion_bounds(ink, pixels, jump, style, angle=0, fall_distance=48, singing_sync=False):
    """Conservative local bounds including rotation, shake and screen-vertical fall."""
    if style == "classic":
        scale = 1.04 * (1.08 if singing_sync else 1)
        scaled = ink.united(QRectF(ink.x() * scale, ink.y() * scale, ink.width() * scale, ink.height() * scale))
        return scaled.united(scaled.translated(0, -jump))
    if singing_sync:
        ink = ink.united(QRectF(ink.x() * 1.08, ink.y() * 1.08, ink.width() * 1.08, ink.height() * 1.08))
    bounds = QRectF(ink)
    if style.startswith("fall_"):
        for rotation in (-8, 8):
            bounds = bounds.united(QTransform().rotate(rotation).mapRect(ink))
        radians = math.radians(angle)
        distance = fall_distance * pixels / 32
        bounds = bounds.united(bounds.translated(math.sin(radians) * distance,
                                                math.cos(radians) * distance))
    return bounds.adjusted(-jump * .3 - 2, -jump * .5 - 2, jump * .3 + 2, jump * .5 + 2)
