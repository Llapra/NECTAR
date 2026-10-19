"""Tarjeta de familia para la matriz de la pestaña 1.

Cada familia de dispositivo (EvCam, Emergent, Fibras, Anemómetro, IMU-GPS,
Reconstrucción) tiene una tarjeta con:

* el nombre de la familia y un **LED resumen**: verde si hay al menos un
  dispositivo listo e incluido, ámbar si están armados, rojo parpadeando si
  están grabando, gris si no hay nada operativo;
* un **triángulo para desplegar** la lista de los dispositivos concretos que
  están conectados en esa familia, cada uno con su propio mini-LED;
* una **casilla por dispositivo** para dejarlo fuera de la toma de datos sin
  desconectarlo, que es exactamente lo que se pidió: "que desde la pestaña 1
  se pueda apagar si no se quiere considerar".

El LED verde se enciende cuando el dispositivo está de verdad en condiciones
de grabar (``READY`` o ``STREAMING``), no cuando simplemente existe el objeto:
así el botón central nunca dispara sobre algo que no va a escribir datos.
"""

from __future__ import annotations

from typing import Dict, List, Optional

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (QCheckBox, QFrame, QHBoxLayout, QLabel,
                             QLineEdit, QPushButton, QVBoxLayout,
                             QWidget)

from ...devices.base import DeviceState, DeviceWorker
from .. import theme
from .led import LedIndicator


class DeviceRow(QWidget):
    """Una fila de la lista desplegable: un dispositivo concreto."""

    include_changed = pyqtSignal(str, bool)
    alias_changed = pyqtSignal(str, str)

    def __init__(self, worker: DeviceWorker, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.worker = worker
        self._locked = False

        layout = QHBoxLayout(self)
        layout.setContentsMargins(18, 1, 4, 1)
        layout.setSpacing(6)

        self.led = LedIndicator(diameter=13)
        self.check = QCheckBox(worker.label)
        self.check.setChecked(worker.include_in_session)
        self.check.setStyleSheet(f"color: {theme.TEXT_DIM}; font-size: 11px;")
        self.check.toggled.connect(self._on_toggle)

        # El alias es el nombre que el operador le pone al equipo ("Norte",
        # "Pilar M2"). Se guarda en disco anclado a la identidad física de la
        # cámara, así que sobrevive a las campañas; la etiqueta de al lado
        # sigue nombrando la carpeta.
        self.alias_edit = QLineEdit(worker.alias)
        self.alias_edit.setPlaceholderText("alias…")
        self.alias_edit.setMaximumWidth(110)
        self.alias_edit.setStyleSheet(
            f"QLineEdit {{ color: {theme.ACCENT}; font-size: 11px; "
            f"background: transparent; border: 1px solid {theme.BORDER}; "
            f"border-radius: 3px; padding: 1px 3px; }}")
        self.alias_edit.setToolTip(
            "Nombre que le pones a este equipo. Se guarda y vuelve a "
            "aparecer en la próxima sesión.")
        self.alias_edit.editingFinished.connect(self._on_alias)

        self.state_label = QLabel("")
        self.state_label.setStyleSheet(theme.value_style(theme.TEXT_FAINT, 10))
        self.rate_label = QLabel("")
        self.rate_label.setStyleSheet(theme.value_style(theme.TEXT_FAINT, 10))

        layout.addWidget(self.led)
        layout.addWidget(self.check, 1)
        layout.addWidget(self.alias_edit)
        layout.addWidget(self.state_label)
        layout.addWidget(self.rate_label)

        self.refresh()

    def set_locked(self, locked: bool) -> None:
        """Durante una toma no se excluye ni se renombra nada."""
        self._locked = locked
        self.check.setEnabled(not locked)
        self.alias_edit.setReadOnly(locked)

    def _on_toggle(self, checked: bool) -> None:
        if self._locked:
            self.check.blockSignals(True)
            self.check.setChecked(self.worker.include_in_session)
            self.check.blockSignals(False)
            return
        self.worker.include_in_session = checked
        self.include_changed.emit(self.worker.device_id, checked)
        self.refresh()

    def _on_alias(self) -> None:
        if self._locked:
            return
        alias = self.alias_edit.text().strip()
        if alias != self.worker.alias:
            self.alias_changed.emit(self.worker.device_id, alias)

    def refresh(self) -> None:
        state = self.worker.state
        fault = getattr(self.worker, "recording_fault", "")
        if fault:
            # Falló durante la grabación: rojo parpadeante hasta que el
            # operador lo reconozca. No se apaga solo aunque el equipo se
            # recupere, porque esa toma ya tiene un agujero.
            self.led.set_color(theme.DANGER)
            self.led.set_blinking(True)
            self.state_label.setText("¡FALLÓ!")
            self.state_label.setStyleSheet(theme.value_style(theme.DANGER, 10))
            self.setToolTip(fault)
        elif not self.worker.include_in_session:
            # Excluido a propósito: el LED queda gris aunque el equipo esté
            # perfecto, para que se vea que no va a entrar a la grabación.
            self.led.set_color(theme.GREY_LED)
            self.led.set_blinking(False)
            self.state_label.setText("excluido")
            self.state_label.setStyleSheet(theme.value_style(theme.TEXT_FAINT, 10))
        else:
            self.led.set_state(state)
            self.state_label.setText(self.worker.state_label.lower())
            self.state_label.setStyleSheet(theme.value_style(theme.TEXT_FAINT, 10))
            self.setToolTip(f"{self.worker.identity_key} "
                            f"({self.worker.identity_source})")
        if getattr(self.worker, "simulated", False):
            self.check.setText(f"{self.worker.label} (SIM)")
            self.check.setStyleSheet(f"color: {theme.WARN}; font-size: 11px;")
        if not self.alias_edit.hasFocus() \
                and self.alias_edit.text() != self.worker.alias:
            self.alias_edit.setText(self.worker.alias)
        rate = self.worker.estimated_mb_s()
        self.rate_label.setText(f"{rate:7.1f} MB/s" if rate >= 0.1
                                else f"{rate * 1000:7.1f} KB/s")


class FamilyCard(QFrame):
    """Tarjeta de una familia, con LED resumen y lista desplegable."""

    include_changed = pyqtSignal(str, bool)
    alias_changed = pyqtSignal(str, str)
    open_tab_requested = pyqtSignal(str)

    def __init__(self, family: str, label: str, align_right: bool = False,
                 parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.family = family
        self.family_label = label
        self._rows: Dict[str, DeviceRow] = {}
        self._expanded = False
        self._locked = False

        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.setStyleSheet(
            f"QFrame {{ border: 1px solid {theme.BORDER_LIGHT}; "
            f"border-radius: 6px; background-color: {theme.BG_PANEL}; }}")

        outer = QVBoxLayout(self)
        outer.setContentsMargins(6, 4, 6, 6)
        outer.setSpacing(2)

        # --- encabezado -------------------------------------------------
        header = QHBoxLayout()
        header.setSpacing(8)

        self.toggle = QPushButton("▽")
        self.toggle.setFixedWidth(26)
        self.toggle.setToolTip("Desplegar los dispositivos de esta familia")
        self.toggle.clicked.connect(self.toggle_expanded)

        self.title = QPushButton(label)
        self.title.setStyleSheet(
            f"QPushButton {{ background: transparent; border: none; "
            f"color: {theme.TEXT}; font-size: 15px; font-weight: bold; "
            f"text-align: left; padding: 2px; }}"
            f"QPushButton:hover {{ color: {theme.ACCENT}; }}")
        self.title.setToolTip("Ir a la pestaña de esta familia")
        self.title.clicked.connect(lambda: self.open_tab_requested.emit(self.family))

        self.count_label = QLabel("0")
        self.count_label.setStyleSheet(theme.chip_style(theme.TEXT_FAINT))

        self.led = LedIndicator(diameter=24)

        if align_right:
            header.addWidget(self.led)
            header.addWidget(self.count_label)
            header.addWidget(self.title, 1)
            header.addWidget(self.toggle)
        else:
            header.addWidget(self.toggle)
            header.addWidget(self.title, 1)
            header.addWidget(self.count_label)
            header.addWidget(self.led)
        outer.addLayout(header)

        # --- cuerpo desplegable -----------------------------------------
        self.body = QWidget()
        self._body_layout = QVBoxLayout(self.body)
        self._body_layout.setContentsMargins(0, 0, 0, 0)
        self._body_layout.setSpacing(1)
        self.empty_label = QLabel("sin dispositivos conectados")
        self.empty_label.setStyleSheet(
            f"color: {theme.TEXT_FAINT}; font-size: 10px; "
            f"font-style: italic; padding-left: 18px;")
        self._body_layout.addWidget(self.empty_label)
        self.body.setVisible(False)
        outer.addWidget(self.body)

    # ------------------------------------------------------------------
    def toggle_expanded(self) -> None:
        self.set_expanded(not self._expanded)

    def set_expanded(self, expanded: bool) -> None:
        self._expanded = expanded
        self.body.setVisible(expanded)
        self.toggle.setText("△" if expanded else "▽")

    # ------------------------------------------------------------------
    def add_worker(self, worker: DeviceWorker) -> None:
        if worker.device_id in self._rows:
            return
        row = DeviceRow(worker)
        row.include_changed.connect(self.include_changed.emit)
        row.alias_changed.connect(self.alias_changed.emit)
        row.set_locked(self._locked)
        self._rows[worker.device_id] = row
        self._body_layout.addWidget(row)
        self.empty_label.setVisible(False)
        # Al aparecer el primer dispositivo, se despliega sola: es la señal de
        # que ahí hay algo que mirar.
        if len(self._rows) == 1:
            self.set_expanded(True)
        self.refresh()

    def remove_worker(self, device_id: str) -> None:
        row = self._rows.pop(device_id, None)
        if row is not None:
            self._body_layout.removeWidget(row)
            row.setParent(None)
            row.deleteLater()
        self.empty_label.setVisible(not self._rows)
        self.refresh()

    def workers(self) -> List[DeviceWorker]:
        return [row.worker for row in self._rows.values()]

    # ------------------------------------------------------------------
    def set_locked(self, locked: bool) -> None:
        self._locked = locked
        for row in self._rows.values():
            row.set_locked(locked)

    def refresh(self) -> None:
        for row in self._rows.values():
            row.refresh()

        included = [row.worker for row in self._rows.values()
                    if row.worker.include_in_session]
        self.count_label.setText(f"{len(included)}/{len(self._rows)}")

        if not self._rows:
            self.led.set_color(theme.GREY_LED)
            self.led.set_blinking(False)
            self.count_label.setStyleSheet(theme.chip_style(theme.TEXT_FAINT))
            return

        states = [worker.state for worker in included]
        if any(getattr(worker, "recording_fault", "") for worker in included):
            # Una falla durante la toma manda sobre cualquier otro estado:
            # es lo único que el operador tiene que ver desde el otro lado de
            # la cúpula.
            self.led.set_color(theme.DANGER)
            self.led.set_blinking(True)
            self.count_label.setStyleSheet(theme.chip_style(theme.DANGER))
            return
        if any(state is DeviceState.RECORDING for state in states):
            self.led.set_color(theme.DANGER)
            self.led.set_blinking(True)
        elif any(state is DeviceState.ARMED for state in states):
            self.led.set_color(theme.WARN)
            self.led.set_blinking(False)
        elif any(state in (DeviceState.READY, DeviceState.STREAMING)
                 for state in states):
            self.led.set_color(theme.OK_BRIGHT)
            self.led.set_blinking(False)
        elif any(state is DeviceState.ERROR_FATAL for state in states):
            self.led.set_color(theme.DANGER_DARK)
            self.led.set_blinking(False)
        elif any(state is DeviceState.ERROR_RECOVERABLE for state in states):
            self.led.set_color(theme.ORANGE)
            self.led.set_blinking(False)
        elif any(state is DeviceState.CONNECTING for state in states):
            self.led.set_color(theme.ACCENT)
            self.led.set_blinking(False)
        else:
            self.led.set_color(theme.GREY_LED)
            self.led.set_blinking(False)

        ready = sum(1 for worker in included if worker.is_armable)
        color = theme.OK_BRIGHT if ready else theme.TEXT_FAINT
        self.count_label.setStyleSheet(theme.chip_style(color))

    def total_mb_s(self) -> float:
        return sum(worker.estimated_mb_s() for row in self._rows.values()
                   for worker in [row.worker]
                   if worker.include_in_session and worker.is_armable)
