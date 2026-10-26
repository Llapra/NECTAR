"""Widgets reutilizables.

La consigna de estandarizar el GUI se cumple acá: todas las pestañas arman sus
controles con estas mismas piezas, así "exposición" en la pestaña 3 se ve y se
comporta igual que "acumulación" en la pestaña 2.
"""

from __future__ import annotations

import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (QCheckBox, QComboBox, QFileDialog, QFrame,
                             QGridLayout, QGroupBox, QHBoxLayout, QLabel,
                             QLineEdit, QPushButton, QScrollArea, QSizePolicy,
                             QSlider, QSpinBox, QTextEdit, QVBoxLayout,
                             QWidget)

from .. import theme


# ---------------------------------------------------------------------------
class SliderSpin(QWidget):
    """Deslizador + casilla con el valor exacto, siempre sincronizados.

    Es el control que ya usaba el programa anterior y que conviene mantener:
    el deslizador sirve para explorar y la casilla para poner el número que
    dice el protocolo de medición.
    """

    value_changed = pyqtSignal(int)

    def __init__(self, minimum: int, maximum: int, value: int,
                 step: int = 1, suffix: str = "",
                 parent: Optional[QWidget] = None, snap_to_step: bool = False) -> None:
        super().__init__(parent)
        self._snap_to_step = snap_to_step
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        self.slider = QSlider(Qt.Orientation.Horizontal)
        self.slider.setRange(int(minimum), int(maximum))
        self.slider.setSingleStep(max(1, int(step)))
        self.slider.setValue(int(value))

        self.spin = QSpinBox()
        self.spin.setRange(int(minimum), int(maximum))
        self.spin.setSingleStep(max(1, int(step)))
        self.spin.setValue(int(value))
        self.spin.setFixedWidth(96)
        if suffix:
            self.spin.setSuffix(f" {suffix}")

        self.slider.valueChanged.connect(self._from_slider)
        self.spin.valueChanged.connect(self._from_spin)

        layout.addWidget(self.slider, 1)
        layout.addWidget(self.spin, 0)

    def _from_slider(self, value: int) -> None:
        if self._snap_to_step:
            value = self._valid_value(value)
            self.set_value(value)
        if self.spin.value() != value:
            self.spin.blockSignals(True)
            self.spin.setValue(value)
            self.spin.blockSignals(False)
        self.value_changed.emit(value)

    def _from_spin(self, value: int) -> None:
        if self._snap_to_step:
            value = self._valid_value(value)
            self.set_value(value)
        if self.slider.value() != value:
            self.slider.blockSignals(True)
            self.slider.setValue(value)
            self.slider.blockSignals(False)
        self.value_changed.emit(value)

    def value(self) -> int:
        return self.spin.value()

    def _valid_value(self, value: int) -> int:
        minimum = self.spin.minimum()
        value = max(minimum, min(self.spin.maximum(), int(value)))
        step = self.spin.singleStep()
        return minimum + ((value - minimum) // step) * step

    def set_value(self, value: int) -> None:
        self.slider.blockSignals(True)
        self.spin.blockSignals(True)
        self.slider.setValue(int(value))
        self.spin.setValue(int(value))
        self.slider.blockSignals(False)
        self.spin.blockSignals(False)

    def set_range(self, minimum: int, maximum: int, step: int = 1) -> None:
        self.slider.setRange(int(minimum), int(maximum))
        self.spin.setRange(int(minimum), int(maximum))
        self.slider.setSingleStep(max(1, int(step)))
        self.spin.setSingleStep(max(1, int(step)))


# ---------------------------------------------------------------------------
class LabeledControl(QWidget):
    """Etiqueta arriba, control abajo. Uniforma todos los paneles."""

    def __init__(self, label: str, control: QWidget, tooltip: str = "",
                 parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 2, 0, 2)
        layout.setSpacing(3)
        self.label = QLabel(f"<b>{label}</b>")
        if tooltip:
            self.label.setToolTip(tooltip)
            control.setToolTip(tooltip)
        layout.addWidget(self.label)
        layout.addWidget(control)
        self.control = control


# ---------------------------------------------------------------------------
class ValueRow(QWidget):
    """Fila "nombre: valor" en tipografía monoespaciada.

    Para telemetría: los números quedan alineados y se pueden leer de un
    vistazo aunque cambien varias veces por segundo.
    """

    def __init__(self, name: str, value: str = "—", color: str = theme.CONSOLE_TEXT,
                 size: int = 12, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 1, 0, 1)
        self._name = QLabel(name)
        self._name.setStyleSheet(f"color: {theme.TEXT_FAINT}; font-size: {size}px;")
        self._value = QLabel(value)
        self._value.setStyleSheet(theme.value_style(color, size))
        self._value.setAlignment(Qt.AlignmentFlag.AlignRight
                                 | Qt.AlignmentFlag.AlignVCenter)
        layout.addWidget(self._name)
        layout.addStretch(1)
        layout.addWidget(self._value)

    def set_value(self, value: Any, color: Optional[str] = None) -> None:
        self._value.setText(str(value))
        if color:
            self._value.setStyleSheet(theme.value_style(color, 12))


# ---------------------------------------------------------------------------
class BigValue(QWidget):
    """Número grande con unidad, para la métrica principal de un panel."""

    def __init__(self, unit: str = "", color: str = theme.OK_BRIGHT,
                 size: int = 30, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        self._value = QLabel("0.0")
        self._value.setStyleSheet(theme.value_style(color, size))
        self._unit = QLabel(unit)
        self._unit.setStyleSheet(f"color: {theme.TEXT_FAINT}; font-size: 12px;")
        layout.addWidget(self._value)
        layout.addWidget(self._unit, 0, Qt.AlignmentFlag.AlignBottom)
        layout.addStretch(1)

    def set_value(self, value: Any, color: Optional[str] = None) -> None:
        self._value.setText(str(value))
        if color:
            self._value.setStyleSheet(theme.value_style(color, 30))

    def set_unit(self, unit: str) -> None:
        self._unit.setText(unit)


# ---------------------------------------------------------------------------
class SavePathSelector(QWidget):
    """Selector de carpeta de guardado, idéntico en todas las pestañas.

    Cada pestaña tiene el suyo para poder grabar por su cuenta. Cuando la
    grabación la manda la pestaña 1, este valor se ignora y todo va a la
    carpeta de la sesión: eso se avisa en el propio widget para que nadie
    busque los archivos donde no están.
    """

    path_changed = pyqtSignal(str)

    def __init__(self, initial: str = "", title: str = "Carpeta de guardado",
                 parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._path = initial
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)

        row = QHBoxLayout()
        self.button = QPushButton("📁 Elegir carpeta")
        self.button.setStyleSheet(theme.button_style("warn"))
        self.button.clicked.connect(self._choose)
        row.addWidget(self.button)
        layout.addWidget(QLabel(f"<b>{title}</b>"))
        layout.addLayout(row)

        self.label = QLabel(initial or "(sin definir)")
        self.label.setWordWrap(True)
        self.label.setStyleSheet(
            f"color: {theme.TEXT_FAINT}; font-size: 10px; "
            f"border: 1px solid {theme.BORDER}; padding: 3px;")
        layout.addWidget(self.label)

        self.note = QLabel("Durante una sesión global manda la pestaña 1.")
        self.note.setStyleSheet(f"color: {theme.TEXT_FAINT}; font-size: 10px; "
                                f"font-style: italic;")
        self.note.setWordWrap(True)
        layout.addWidget(self.note)

    def _choose(self) -> None:
        directory = QFileDialog.getExistingDirectory(
            self, "Seleccionar carpeta de guardado", self._path or "")
        if directory:
            self.set_path(directory)

    def set_path(self, path: str) -> None:
        self._path = path
        self.label.setText(path or "(sin definir)")
        self.path_changed.emit(path)

    def path(self) -> str:
        return self._path


# ---------------------------------------------------------------------------
class LogConsole(QTextEdit):
    """Consola con marca de tiempo y tope de líneas.

    El tope importa: una sesión de varias horas con telemetría cada medio
    segundo llenaría la memoria con texto que nadie va a leer.
    """

    def __init__(self, max_lines: int = 800, color: str = theme.CONSOLE_TEXT,
                 parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setReadOnly(True)
        self.setStyleSheet(theme.console_style(color))
        self.document().setMaximumBlockCount(max_lines)

    def log(self, message: str) -> None:
        stamp = time.strftime("%H:%M:%S")
        for line in str(message).splitlines():
            if line.strip():
                self.append(f"<span style='color:#556'>{stamp}</span> {line}")

    def rule(self, title: str = "") -> None:
        self.append(f"<span style='color:{theme.ACCENT}'>{'─' * 8} {title} "
                    f"{'─' * 8}</span>")


# ---------------------------------------------------------------------------
class Section(QGroupBox):
    """Caja con título. Envuelve un layout vertical ya listo."""

    def __init__(self, title: str, parent: Optional[QWidget] = None) -> None:
        super().__init__(title, parent)
        self._layout = QVBoxLayout(self)
        self._layout.setSpacing(6)

    def add(self, widget: QWidget, stretch: int = 0) -> QWidget:
        self._layout.addWidget(widget, stretch)
        return widget

    def add_layout(self, layout) -> None:
        self._layout.addLayout(layout)

    def add_row(self, name: str, value: str = "—",
                color: str = theme.CONSOLE_TEXT) -> ValueRow:
        row = ValueRow(name, value, color)
        self._layout.addWidget(row)
        return row

    def add_stretch(self) -> None:
        self._layout.addStretch(1)

    def body(self) -> QVBoxLayout:
        return self._layout


# ---------------------------------------------------------------------------
class ScrollColumn(QScrollArea):
    """Columna desplazable: la base del "modular aditivo".

    Cada dispositivo que se conecta agrega su panel al fondo; si no caben en
    pantalla, se baja con la rueda. Así se pueden tener 5 cámaras de eventos,
    cada una con su visualización a tamaño real, sin cambiar la interfaz.
    """

    def __init__(self, spacing: int = 10, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setWidgetResizable(True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self._container = QWidget()
        self._layout = QVBoxLayout(self._container)
        self._layout.setContentsMargins(2, 2, 2, 2)
        self._layout.setSpacing(spacing)
        self._layout.addStretch(1)
        self.setWidget(self._container)

    def add_widget(self, widget: QWidget) -> QWidget:
        """Inserta antes del espaciador final, para que quede arriba."""
        self._layout.insertWidget(self._layout.count() - 1, widget)
        return widget

    def remove_widget(self, widget: QWidget) -> None:
        self._layout.removeWidget(widget)
        widget.setParent(None)
        widget.deleteLater()

    def clear(self) -> None:
        while self._layout.count() > 1:
            item = self._layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()

    def count(self) -> int:
        return self._layout.count() - 1


# ---------------------------------------------------------------------------
class Placeholder(QFrame):
    """Cartel para cuando no hay nada conectado todavía."""

    def __init__(self, message: str, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.setStyleSheet(
            f"border: 1px dashed {theme.BORDER_LIGHT}; border-radius: 6px; "
            f"background-color: {theme.BG_PANEL};")
        layout = QVBoxLayout(self)
        label = QLabel(message)
        label.setWordWrap(True)
        label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        label.setStyleSheet(f"color: {theme.TEXT_FAINT}; font-size: 13px; "
                            f"padding: 24px;")
        layout.addWidget(label)
        self.setSizePolicy(QSizePolicy.Policy.Expanding,
                           QSizePolicy.Policy.Minimum)


# ---------------------------------------------------------------------------
class DeviceSelectorRow(QWidget):
    """Fila para agregar un dispositivo: detectar, elegir, conectar.

    Es el encabezado común de las pestañas de familia. Mantiene la misma
    disposición en todas, que es lo que se pidió al estandarizar el GUI.
    """

    detect_requested = pyqtSignal()
    add_requested = pyqtSignal(dict)

    def __init__(self, family_label: str, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        self.detect = QPushButton("⟳ DETECTAR")
        self.detect.setStyleSheet(theme.button_style("accent"))
        self.detect.clicked.connect(self.detect_requested.emit)

        self.combo = QComboBox()
        self.combo.setMinimumWidth(240)

        self.add = QPushButton(f"+ AGREGAR {family_label.upper()}")
        self.add.setStyleSheet(theme.button_style("ok"))
        self.add.clicked.connect(self._emit_add)

        layout.addWidget(self.detect)
        layout.addWidget(QLabel("Disponibles:"))
        layout.addWidget(self.combo, 1)
        layout.addWidget(self.add)

        self._items: List[Dict[str, Any]] = []

    def set_items(self, items: List[Dict[str, Any]]) -> None:
        self._items = list(items)
        self.combo.clear()
        for item in self._items:
            label = item.get("label", item.get("id", "?"))
            extra = []
            if item.get("serial"):
                extra.append(str(item["serial"]))
            if item.get("port"):
                extra.append(str(item["port"]))
            if item.get("ip"):
                extra.append(str(item["ip"]))
            if item.get("simulated"):
                extra.append("SIMULADO")
            suffix = f"  [{' · '.join(extra)}]" if extra else ""
            self.combo.addItem(f"{label}{suffix}")
        self.add.setEnabled(bool(self._items))

    def current(self) -> Optional[Dict[str, Any]]:
        index = self.combo.currentIndex()
        if 0 <= index < len(self._items):
            return dict(self._items[index])
        return None

    def _emit_add(self) -> None:
        item = self.current()
        if item:
            self.add_requested.emit(item)
