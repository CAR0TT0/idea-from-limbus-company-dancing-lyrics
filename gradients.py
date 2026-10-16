"""Gradients anchored to the original lyric layout, independent of animation poses."""
import json
import numpy as np
from PySide6.QtCore import QPointF
from PySide6.QtGui import QColor, QBrush, QLinearGradient


def enabled(prefs):
    return prefs.text_style == "gradient_fill" or (prefs.text_style == "glow" and prefs.glow_variant == "gradient")


def endpoints(bounds, direction):
    start = bounds.topLeft()
    end = (QPointF(bounds.right(), bounds.top()) if direction == "horizontal" else
           QPointF(bounds.left(), bounds.bottom()) if direction == "vertical" else bounds.bottomRight())
    if end == start:
        end = start + QPointF(1, 0)
    return start, end


def brush(bounds, glyph, prefs):
    start, end = endpoints(bounds, prefs.gradient_direction)
    origin = QPointF(glyph.x, glyph.baseline)
    gradient = QLinearGradient(start - origin, end - origin)
    colors = json.loads(prefs.gradient_colors)
    for index, color in enumerate(colors):
        gradient.setColorAt(index / (len(colors) - 1), QColor(color))
    return QBrush(gradient)


def tint(image, origin, glyph, bounds, prefs):
    """Color an existing premultiplied white alpha mask, without another blur."""
    result = image.copy()
    view = np.ndarray((result.height(), result.width(), 4), dtype=np.uint8,
                      buffer=result.bits(), strides=(result.bytesPerLine(), 4, 1))
    start, end = endpoints(bounds, prefs.gradient_direction)
    dx, dy = end.x() - start.x(), end.y() - start.y()
    dpr = result.devicePixelRatio()
    x = (np.arange(result.width(), dtype=np.float32)[None, :] + .5) / dpr + origin.x() + glyph.x
    y = (np.arange(result.height(), dtype=np.float32)[:, None] + .5) / dpr + origin.y() + glyph.baseline
    weight = np.clip(((x - start.x()) * dx + (y - start.y()) * dy) / max(1e-6, dx * dx + dy * dy), 0, 1)
    colors = [QColor(color) for color in json.loads(prefs.gradient_colors)]
    stops = np.linspace(0, 1, len(colors))
    alpha = view[:, :, 3].astype(np.float32) / 255
    for channel, getter in enumerate((QColor.red, QColor.green, QColor.blue)):
        view[:, :, channel] = np.rint(np.interp(weight, stops, [getter(color) for color in colors]) * alpha).astype(np.uint8)
    return result
