"""Gráficos en vivo (PyQtGraph) con degradación elegante.

Se usan para las series temporales de temperatura (fibras), la métrica de
enfoque y la telemetría del anemómetro. Cada curva mantiene un buffer
circular de tamaño fijo: a 1 kHz y varias horas de sesión, guardar todo en el
gráfico sería un fuga de memoria disfrazada de visualización.

La paleta es la misma en todos los gráficos, pensada para fondo oscuro y para
que las series se distingan también por luminancia, no solo por tono.
"""

from __future__ import annotations

from collections import deque
from typing import Deque, Dict, List, Optional, Sequence

import numpy as np
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QLabel, QVBoxLayout, QWidget

from .. import theme

try:
    import pyqtgraph as pg
    pg.setConfigOptions(antialias=False)
    HAS_PYQTGRAPH = True
except Exception:
    HAS_PYQTGRAPH = False

#: Colores de serie: contrastan entre sí y sobre fondo casi negro.
SERIES_COLORS = [
    "#00e5ff", "#ffb300", "#69f0ae", "#ff5252", "#b388ff", "#ffd54f",
    "#4fc3f7", "#f06292", "#aed581", "#ff8a65", "#9fa8da", "#dce775",
]


def color_for(index: int) -> str:
    return SERIES_COLORS[index % len(SERIES_COLORS)]


class _NoPlot(QWidget):
    """Cartel de reemplazo cuando falta PyQtGraph."""

    def __init__(self, title: str, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        label = QLabel(f"{title}\n\nInstala pyqtgraph para ver este gráfico:\n"
                       f"pip install pyqtgraph")
        label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        label.setStyleSheet(f"color: {theme.TEXT_FAINT}; font-size: 12px;")
        layout.addWidget(label)

    # API compatible para que las pestañas no tengan que preguntar
    def set_series(self, *args, **kwargs) -> None:
        return None

    def append(self, *args, **kwargs) -> None:
        return None

    def set_data(self, *args, **kwargs) -> None:
        return None

    def clear_data(self) -> None:
        return None


class TimeSeriesPlot(QWidget):
    """Varias curvas contra el tiempo, con buffer circular por curva."""

    def __init__(self, title: str = "", y_label: str = "",
                 x_label: str = "tiempo (s)", capacity: int = 3000,
                 parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.capacity = capacity
        self._x: Dict[str, Deque[float]] = {}
        self._y: Dict[str, Deque[float]] = {}
        self._curves: Dict[str, object] = {}

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        if not HAS_PYQTGRAPH:
            self._plot = None
            layout.addWidget(_NoPlot(title or "Serie temporal"))
            return

        self._plot = pg.PlotWidget(title=title or None)
        self._plot.setBackground(theme.BG_PANEL)
        self._plot.showGrid(x=True, y=True, alpha=0.2)
        self._plot.setLabel("left", y_label)
        self._plot.setLabel("bottom", x_label)
        self._plot.addLegend(offset=(-10, 10))
        layout.addWidget(self._plot)

    # ------------------------------------------------------------------
    def set_series(self, names: Sequence[str]) -> None:
        """Define las curvas. Se llama cuando cambia el plantel de canales."""
        if self._plot is None:
            return
        for name in list(self._curves):
            if name not in names:
                self._plot.removeItem(self._curves.pop(name))
                self._x.pop(name, None)
                self._y.pop(name, None)
        for index, name in enumerate(names):
            if name in self._curves:
                continue
            pen = pg.mkPen(color_for(index), width=1.4)
            self._curves[name] = self._plot.plot([], [], pen=pen, name=name)
            self._x[name] = deque(maxlen=self.capacity)
            self._y[name] = deque(maxlen=self.capacity)

    def append(self, name: str, x: float, y: float, redraw: bool = True) -> None:
        if self._plot is None or name not in self._curves:
            return
        self._x[name].append(float(x))
        self._y[name].append(float(y))
        if redraw:
            self._curves[name].setData(list(self._x[name]), list(self._y[name]))

    def append_many(self, x: float, values: Dict[str, float]) -> None:
        """Un punto por curva en el mismo instante (redibuja una sola vez)."""
        if self._plot is None:
            return
        for name, value in values.items():
            if name not in self._curves:
                continue
            if value is None or not np.isfinite(value):
                continue
            self._x[name].append(float(x))
            self._y[name].append(float(value))
            self._curves[name].setData(list(self._x[name]), list(self._y[name]))

    def set_data(self, name: str, x: Sequence[float], y: Sequence[float]) -> None:
        """Reemplaza una curva completa (para datos ya calculados)."""
        if self._plot is None:
            return
        if name not in self._curves:
            self.set_series(list(self._curves) + [name])
        self._curves[name].setData(list(x), list(y))

    def clear_data(self) -> None:
        for name in self._curves:
            self._x[name].clear()
            self._y[name].clear()
            if self._plot is not None:
                self._curves[name].setData([], [])

    def set_log_mode(self, x: bool = False, y: bool = False) -> None:
        if self._plot is not None:
            self._plot.setLogMode(x, y)

    def add_horizontal_line(self, value: float, color: str = theme.WARN,
                            label: str = "") -> None:
        if self._plot is None:
            return
        line = pg.InfiniteLine(pos=value, angle=0,
                               pen=pg.mkPen(color, style=Qt.PenStyle.DashLine),
                               label=label or None)
        self._plot.addItem(line)

    def add_vertical_line(self, value: float, color: str = theme.DANGER,
                          label: str = "") -> None:
        if self._plot is None:
            return
        line = pg.InfiniteLine(pos=value, angle=90,
                               pen=pg.mkPen(color, style=Qt.PenStyle.DashLine),
                               label=label or None)
        self._plot.addItem(line)

    @property
    def available(self) -> bool:
        return self._plot is not None


class BarPlot(QWidget):
    """Barras verticales: una por canal. Para ver todas las fibras juntas."""

    def __init__(self, title: str = "", y_label: str = "",
                 parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        if not HAS_PYQTGRAPH:
            self._plot = None
            layout.addWidget(_NoPlot(title or "Barras"))
            return
        self._plot = pg.PlotWidget(title=title or None)
        self._plot.setBackground(theme.BG_PANEL)
        self._plot.showGrid(y=True, alpha=0.2)
        self._plot.setLabel("left", y_label)
        self._bars = None
        layout.addWidget(self._plot)

    def set_values(self, labels: List[str], values: List[float]) -> None:
        if self._plot is None:
            return
        if self._bars is not None:
            self._plot.removeItem(self._bars)
        x = np.arange(len(values), dtype=float)
        finite = [value if value is not None and np.isfinite(value) else 0.0
                  for value in values]
        self._bars = pg.BarGraphItem(x=x, height=finite, width=0.6,
                                     brush=theme.ACCENT)
        self._plot.addItem(self._bars)
        axis = self._plot.getAxis("bottom")
        axis.setTicks([[(position, label)
                        for position, label in zip(x, labels)]])
