"""Small supported symbol set, fitted to the original character's reserved cell."""
from functools import lru_cache
from PySide6.QtCore import QPointF
from PySide6.QtGui import QPainterPath, QTransform, QFontMetricsF
from fonts import lyric_font

SYMBOLS = "#%?+*=<>"


@lru_cache(maxsize=512)
def paths(family, pixels, width, center_x, center_y):
    font = lyric_font(family, pixels)
    metrics = QFontMetricsF(font)
    available = [symbol for symbol in SYMBOLS if metrics.inFont(symbol)] or ["?"]
    result = []
    for index in range(8):
        path = QPainterPath()
        path.addText(QPointF(), font, available[index % len(available)])
        bounds = path.boundingRect()
        scale = min(1, max(1, width) / max(1, bounds.width()))
        path = QTransform().scale(scale, scale).map(path)
        center = path.boundingRect().center()
        path.translate(center_x-center.x(), center_y-center.y())
        result.append(path)
    return tuple(result)
