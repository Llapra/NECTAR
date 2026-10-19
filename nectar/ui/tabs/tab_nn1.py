"""Pestaña ⑪ · NN1 — reservada para la red neuronal.

Está vacía a propósito. Es el hueco donde va a vivir el primer modelo de la
línea de trabajo de redes: el que tome los datos que NECTAR graba (frames de
la Emergent, flujo de eventos, series de las fibras) y produzca la estimación
que hoy se hace a mano.

Se deja creada desde ahora, y no cuando haya modelo, por una razón práctica:
la posición de las pestañas y los atajos se aprenden con el uso, y mover la
numeración a mitad de campaña obliga a reaprender la ventana justo cuando
menos tiempo hay. Mejor que el hueco esté desde el principio.

Cuando llegue el modelo, esta pestaña es donde entran: la selección de la
sesión o el archivo de entrada, la elección de arquitectura (las candidatas
anotadas en el proyecto son CNN para lo espacial, Transformer para el
relacionamiento tensorial y SNN si resulta eficiente sobre eventos), los
parámetros de inferencia, el estado de la GPU y la salida del modelo.
"""

from __future__ import annotations

from typing import Optional

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QLabel, QVBoxLayout, QWidget

from .. import theme


class NN1Tab(QWidget):
    """Marcador de sitio para el módulo de red neuronal."""

    def __init__(self, manager, recorder, sessions,
                 parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.manager = manager
        self.recorder = recorder
        self.sessions = sessions

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addStretch(1)

        title = QLabel("NN1")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title.setStyleSheet(
            f"color: {theme.ACCENT}; font-size: 96px; font-weight: bold; "
            f"letter-spacing: 12px; background: transparent;")
        layout.addWidget(title)

        subtitle = QLabel("Reservado para el módulo de red neuronal")
        subtitle.setAlignment(Qt.AlignmentFlag.AlignCenter)
        subtitle.setStyleSheet(
            f"color: {theme.TEXT_FAINT}; font-size: 13px; "
            f"background: transparent;")
        layout.addWidget(subtitle)

        layout.addStretch(1)
