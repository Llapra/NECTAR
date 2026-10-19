"""Indicador LED.

Es el elemento central de la pestaña 1: verde = el dispositivo está listo para
grabar, gris = no está operativo. Se dibuja a mano en vez de usar una imagen
para que el color pueda venir directo del estado del dispositivo y para que
tenga brillo (un halo) cuando está grabando, que es lo que hace que se note
de una mirada desde lejos.
"""

from __future__ import annotations

from typing import Optional

from PyQt6.QtCore import QSize, Qt, QTimer
from PyQt6.QtGui import QColor, QPainter, QRadialGradient
from PyQt6.QtWidgets import QWidget

from ...devices.base import STATE_COLORS, DeviceState
from .. import theme


class LedIndicator(QWidget):
    """Círculo de color con halo opcional y parpadeo cuando graba."""

    def __init__(self, diameter: int = 26, color: str = theme.GREY_LED,
                 parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._diameter = diameter
        self._color = QColor(color)
        self._blink = False
        self._blink_on = True
        self.setFixedSize(diameter + 8, diameter + 8)

        self._timer = QTimer(self)
        self._timer.setInterval(500)
        self._timer.timeout.connect(self._toggle)

    # ------------------------------------------------------------------
    def sizeHint(self) -> QSize:
        return QSize(self._diameter + 8, self._diameter + 8)

    def set_color(self, color: str) -> None:
        new_color = QColor(color)
        if new_color != self._color:
            self._color = new_color
            self.update()

    def set_blinking(self, blinking: bool) -> None:
        if blinking == self._blink:
            return
        self._blink = blinking
        if blinking:
            self._timer.start()
        else:
            self._timer.stop()
            self._blink_on = True
        self.update()

    def set_state(self, state: DeviceState) -> None:
        """Toma color y parpadeo del estado del dispositivo."""
        self.set_color(STATE_COLORS.get(state, theme.GREY_LED))
        self.set_blinking(state is DeviceState.RECORDING)
        self.setToolTip(state.name)

    def set_diameter(self, diameter: int) -> None:
        self._diameter = diameter
        self.setFixedSize(diameter + 8, diameter + 8)
        self.update()

    # ------------------------------------------------------------------
    def _toggle(self) -> None:
        self._blink_on = not self._blink_on
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802 (API de Qt)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        color = QColor(self._color)
        if self._blink and not self._blink_on:
            color = color.darker(260)

        radius = self._diameter / 2.0
        center_x = self.width() / 2.0
        center_y = self.height() / 2.0

        # Halo: hace visible el estado desde lejos, sin depender del color.
        if color.lightness() > 60:
            glow = QRadialGradient(center_x, center_y, radius + 4)
            halo = QColor(color)
            halo.setAlpha(110)
            glow.setColorAt(0.0, halo)
            halo_out = QColor(color)
            halo_out.setAlpha(0)
            glow.setColorAt(1.0, halo_out)
            painter.setBrush(glow)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.drawEllipse(int(center_x - radius - 4),
                                int(center_y - radius - 4),
                                int((radius + 4) * 2), int((radius + 4) * 2))

        gradient = QRadialGradient(center_x - radius * 0.3,
                                   center_y - radius * 0.3, radius * 1.6)
        gradient.setColorAt(0.0, color.lighter(160))
        gradient.setColorAt(0.7, color)
        gradient.setColorAt(1.0, color.darker(180))
        painter.setBrush(gradient)
        painter.setPen(QColor("#0a0a0a"))
        painter.drawEllipse(int(center_x - radius), int(center_y - radius),
                            int(radius * 2), int(radius * 2))
        painter.end()
