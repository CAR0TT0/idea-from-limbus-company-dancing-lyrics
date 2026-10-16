import os
os.environ["QT_QPA_PLATFORM"] = "offscreen"
import unittest
from PySide6.QtCore import QPointF, QPoint, Qt
from PySide6.QtGui import QWheelEvent
from PySide6.QtWidgets import QApplication
from PySide6.QtTest import QTest
from controls import QComboBox, QSpinBox, QSlider, ThinSlider

APP = QApplication.instance() or QApplication([])


class WheelTests(unittest.TestCase):
    def test_wheel_never_edits_focused_or_unfocused_fields(self):
        for kind in (QComboBox, QSpinBox, QSlider, ThinSlider):
            widget = kind()
            if isinstance(widget, QComboBox):
                widget.addItems(["A", "B", "C"])
                widget.setCurrentIndex(1)
                get_value = widget.currentIndex
            else:
                widget.setRange(0, 100)
                widget.setValue(50)
                get_value = widget.value
            widget.show()
            for focused in (False, True):
                widget.setFocus() if focused else widget.clearFocus()
                for angle, pixel in ((QPoint(0, 120), QPoint()), (QPoint(), QPoint(0, 24))):
                    event = QWheelEvent(QPointF(5, 5), QPointF(5, 5), pixel, angle, Qt.MouseButton.NoButton,
                                        Qt.KeyboardModifier.NoModifier, Qt.ScrollPhase.NoScrollPhase, False)
                    before = get_value()
                    APP.sendEvent(widget, event)
                    self.assertEqual(get_value(), before)
                    self.assertFalse(event.isAccepted())
            widget.close()

    def test_keyboard_and_explicit_selection_remain_available(self):
        spin = QSpinBox()
        spin.setValue(5)
        QTest.keyClick(spin, Qt.Key.Key_Up)
        self.assertEqual(spin.value(), 6)
        combo = QComboBox()
        combo.addItems(["A", "B"])
        combo.setCurrentIndex(1)
        self.assertEqual(combo.currentText(), "B")
