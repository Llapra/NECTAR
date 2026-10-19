"""Armazón común de las pestañas de familia (2, 3, 5, 6 y 7).

Por qué existe
--------------
Las cinco pestañas de instrumento tienen la misma estructura: detectar, elegir
y agregar dispositivos; una columna desplazable donde cada dispositivo suma su
propio panel ("modular aditivo"); una columna lateral con los controles
comunes de la familia; y su propio bloque de grabación independiente con
selector de carpeta, botón de grabar y de detener.

Escribir eso cinco veces garantizaría que las cinco terminen distintas. Acá
está una sola vez, y cada pestaña solo aporta lo que la distingue:

* ``create_panel(worker)`` — el panel de un dispositivo de esa familia;
* ``build_side_panel()`` — los controles propios (biases, parámetros GenICam,
  tasa de muestreo…).

Lo que queda garantizado para todas: mismo lugar del selector, misma columna
desplazable, mismo bloque de guardado, mismos botones, misma consola, y la
misma regla de que durante una sesión global manda la pestaña 1.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

from PyQt6.QtCore import QTimer, pyqtSignal
from PyQt6.QtWidgets import (QFrame, QHBoxLayout, QLabel, QPushButton,
                             QVBoxLayout, QWidget)

from ... import config
from ...core import storage
from ...core.device_registry import REGISTRY
from ...devices.base import DeviceState, DeviceWorker
from .. import theme
from ..widgets.common import (DeviceSelectorRow, LogConsole, Placeholder,
                              SavePathSelector, ScrollColumn, Section)
from ..widgets.led import LedIndicator


class DevicePanel(QFrame):
    """Panel de un dispositivo dentro de la columna desplazable."""

    remove_requested = pyqtSignal(str)

    def __init__(self, worker: DeviceWorker,
                 parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.worker = worker
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.setStyleSheet(
            f"QFrame {{ border: 1px solid {theme.BORDER_LIGHT}; "
            f"border-radius: 6px; background-color: {theme.BG_PANEL}; }}")

        self._root = QVBoxLayout(self)
        self._root.setContentsMargins(8, 6, 8, 8)
        self._root.setSpacing(6)
        self._root.addWidget(self._build_header())

    # ------------------------------------------------------------------
    def _build_header(self) -> QWidget:
        header = QWidget()
        layout = QHBoxLayout(header)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        self.led = LedIndicator(diameter=18)
        self.lbl_title = QLabel(f"<b>{self.worker.display_name}</b>")
        self.lbl_title.setStyleSheet(f"color: {theme.TEXT}; font-size: 14px;")

        self.lbl_state = QLabel(self.worker.state_label)
        self.lbl_state.setStyleSheet(theme.chip_style(theme.TEXT_FAINT))

        subtitle = self.describe_device()
        self.lbl_subtitle = QLabel(subtitle)
        self.lbl_subtitle.setStyleSheet(
            f"color: {theme.TEXT_FAINT}; font-size: 11px; "
            f"font-family: {theme.MONO};")

        self.btn_remove = QPushButton("✕")
        self.btn_remove.setFixedWidth(28)
        self.btn_remove.setToolTip("Desconectar y quitar este dispositivo")
        self.btn_remove.clicked.connect(
            lambda: self.remove_requested.emit(self.worker.device_id))

        layout.addWidget(self.led)
        layout.addWidget(self.lbl_title)
        layout.addWidget(self.lbl_state)
        layout.addWidget(self.lbl_subtitle, 1)
        layout.addWidget(self.btn_remove)
        return header

    def describe_device(self) -> str:
        """Texto corto que identifica al equipo físico."""
        pieces: List[str] = []
        for key in ("serial", "port", "ip", "model"):
            value = self.worker.descriptor.get(key)
            if value:
                pieces.append(str(value))
        if self.worker.descriptor.get("simulated") or \
                getattr(self.worker, "simulated", False):
            pieces.append("SIMULADO")
        return " · ".join(pieces)

    def body(self) -> QVBoxLayout:
        return self._root

    # ------------------------------------------------------------------
    def set_locked(self, locked: bool) -> None:
        """Con una toma en curso no se puede quitar el dispositivo."""
        self.btn_remove.setEnabled(not locked)
        self.btn_remove.setToolTip(
            "No se puede quitar durante una grabación" if locked
            else "Desconectar y quitar este dispositivo")

    def refresh_state(self) -> None:
        fault = getattr(self.worker, "recording_fault", "")
        self.lbl_title.setText(f"<b>{self.worker.display_name}</b>")
        if fault:
            self.led.set_color(theme.DANGER)
            self.led.set_blinking(True)
            self.lbl_state.setText("¡FALLÓ EN LA TOMA!")
            self.lbl_state.setStyleSheet(theme.chip_style(theme.DANGER))
            self.setToolTip(fault)
            return
        self.led.set_state(self.worker.state)
        self.lbl_state.setText(self.worker.state_label)
        color = {
            DeviceState.RECORDING: theme.DANGER,
            DeviceState.ARMED: theme.WARN,
            DeviceState.STREAMING: theme.OK_BRIGHT,
            DeviceState.READY: theme.OK,
            DeviceState.ERROR_FATAL: theme.DANGER_DARK,
            DeviceState.ERROR_RECOVERABLE: theme.ORANGE,
        }.get(self.worker.state, theme.TEXT_FAINT)
        self.lbl_state.setStyleSheet(theme.chip_style(color))
        self.lbl_subtitle.setText(self.describe_device())

    # --- ganchos que sobreescriben las subclases ------------------------
    def refresh_stats(self, stats: Dict[str, Any]) -> None:
        return None

    def on_preview(self, image) -> None:
        return None


class FamilyTab(QWidget):
    """Base de las pestañas de instrumento."""

    family: str = ""
    family_label: str = ""
    #: Texto del cartel cuando no hay nada conectado.
    empty_message: str = "Pulsa DETECTAR y luego AGREGAR para conectar un dispositivo."
    #: Ancho de la columna lateral de controles.
    side_width: int = 300
    #: Si False, la pestaña no ofrece grabación propia (la manda la 1).
    has_own_recording: bool = True

    log_message = pyqtSignal(str)

    def __init__(self, manager, recorder, sessions,
                 parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.manager = manager
        self.recorder = recorder
        self.sessions = sessions
        self.panels: Dict[str, DevicePanel] = {}
        self._locked = False
        self._side_panel: Optional[QWidget] = None
        #: Mensajes que llegaron antes de que existiera la consola.
        self._pending_log: List[str] = []

        self._build()
        self._wire()

        self._timer = QTimer(self)
        self._timer.setInterval(600)
        self._timer.timeout.connect(self.refresh)
        self._timer.start()

        # Descubrimiento inicial sin conectar nada: llena el desplegable.
        QTimer.singleShot(200, self.detect)

    # ------------------------------------------------------------------
    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(8, 6, 8, 8)
        root.setSpacing(6)

        self.selector = DeviceSelectorRow(self.family_label)
        self.selector.detect_requested.connect(self.detect)
        self.selector.add_requested.connect(self.add_device)
        root.addWidget(self.selector)

        columns = QHBoxLayout()
        columns.setSpacing(8)

        self.stack = ScrollColumn()
        self.placeholder = Placeholder(self.empty_message)
        self.stack.add_widget(self.placeholder)
        columns.addWidget(self.stack, 1)

        side = QWidget()
        side.setFixedWidth(self.side_width)
        self._side_layout = QVBoxLayout(side)
        self._side_layout.setContentsMargins(0, 0, 0, 0)
        self._side_layout.setSpacing(8)

        # La consola se crea **antes** que el panel lateral, aunque se muestre
        # después. Varias pestañas hacen trabajo en ``build_side_panel()`` que
        # quiere escribir en el registro (la de IMU, por ejemplo, lista los
        # puertos), y crearla después dejaba una ventana en la que
        # ``self.console`` todavía no existía. Mientras el descubrimiento
        # siempre devolvía algo eso no se notaba; en cuanto empezó a poder
        # devolver vacío, el programa entero no arrancaba por un mensaje de
        # registro. El orden de creación no puede depender de esa suerte.
        self.console = LogConsole(max_lines=400)

        custom = self.build_side_panel()
        if custom is not None:
            self._side_panel = custom
            self._side_layout.addWidget(custom)

        if self.has_own_recording:
            self._side_layout.addWidget(self._build_recording_box())

        self._side_layout.addWidget(QLabel("Registro de la pestaña:"))
        self._side_layout.addWidget(self.console, 1)
        self._flush_pending_log()

        columns.addWidget(side, 0)
        root.addLayout(columns, 1)

    # ------------------------------------------------------------------
    def log(self, message: str) -> None:
        """Escribe en el registro de la pestaña sin poder fallar nunca.

        Segunda red, independiente del orden de construcción: si alguien
        escribe antes de que la consola exista, el mensaje se guarda y sale en
        cuanto esté. Un mensaje de registro no puede tumbar el programa; es
        precisamente lo contrario de lo que el registro está para hacer.
        """
        console = getattr(self, "console", None)
        if console is None:
            self._pending_log.append(message)
            return
        try:
            console.log(message)
        except Exception:
            pass

    def _flush_pending_log(self) -> None:
        pending, self._pending_log = list(self._pending_log), []
        for message in pending:
            self.log(message)

    def _build_recording_box(self) -> QWidget:
        box = Section("Grabación independiente")
        default = str(Path(self.sessions.base_dir) /
                      config.FAMILY_DIRS.get(self.family, self.family))
        self.save_selector = SavePathSelector(default,
                                              "Carpeta de esta familia")
        box.add(self.save_selector)

        row = QHBoxLayout()
        self.btn_record = QPushButton("● GRABAR")
        self.btn_record.setStyleSheet(theme.button_style("danger"))
        self.btn_record.clicked.connect(self.toggle_recording)
        self.btn_stop = QPushButton("■ DETENER")
        self.btn_stop.clicked.connect(self.stop_recording)
        self.btn_stop.setEnabled(False)
        row.addWidget(self.btn_record)
        row.addWidget(self.btn_stop)
        box.add_layout(row)

        self.lbl_rec_state = QLabel("sin grabar")
        self.lbl_rec_state.setStyleSheet(theme.value_style(theme.TEXT_FAINT, 11))
        self.lbl_rec_state.setWordWrap(True)
        box.add(self.lbl_rec_state)
        return box

    def build_side_panel(self) -> Optional[QWidget]:
        """Controles propios de la familia. Se sobreescribe."""
        return None

    # ------------------------------------------------------------------
    def _wire(self) -> None:
        self.manager.device_added.connect(self._on_device_added)
        self.manager.device_removed.connect(self._on_device_removed)

    # ------------------------------------------------------------------
    def set_locked(self, locked: bool) -> None:
        """Congela la pestaña mientras hay una toma en curso.

        Un bias cambiado a mitad de grabación deja los metadatos ya escritos
        describiendo otra cosa, y eso no se puede reconstruir después. Lo
        mismo con el ROI, la tasa de muestreo o quitar un instrumento.
        """
        self._locked = locked
        for panel in self.panels.values():
            panel.set_locked(locked)
        for widget in (getattr(self, "selector", None),
                       getattr(self, "save_selector", None),
                       getattr(self, "btn_record", None)):
            if widget is not None:
                widget.setEnabled(not locked)
        side = getattr(self, "_side_panel", None)
        if side is not None:
            side.setEnabled(not locked)
        self.on_locked(locked)

    def on_locked(self, locked: bool) -> None:
        """Gancho para que cada pestaña agregue lo suyo."""
        return None

    # ------------------------------------------------------------------
    # Alta y baja de dispositivos
    # ------------------------------------------------------------------
    def detect(self) -> None:
        found = self.manager.discover(self.family)
        self.selector.set_items(found)
        if found:
            return
        # Mensaje honesto: el motivo real del descubrimiento fallido, no un
        # "no se detectó nada" que no dice qué hacer.
        reason = self.manager.discovery_error(self.family)
        if reason:
            self.log(
                f"<span style='color:{theme.DANGER}'>✖ {reason}</span>")
        elif config.simulations_allowed():
            self.log("No se detectó ningún dispositivo simulado.")
        else:
            self.log(
                "No hay hardware de esta familia conectado (o el sistema no "
                "lo ve). Para ensayar sin instrumentos: python main.py --demo")

    def add_device(self, descriptor: Dict[str, Any]) -> None:
        worker = self.manager.add(self.family, dict(descriptor))
        if worker is None:
            self.log("No se pudo agregar el dispositivo.")

    def _on_device_added(self, worker: DeviceWorker) -> None:
        if worker.family != self.family:
            return
        panel = self.create_panel(worker)
        panel.remove_requested.connect(self.manager.remove)
        panel.set_locked(self._locked)
        self.panels[worker.device_id] = panel
        self.stack.add_widget(panel)
        self.placeholder.setVisible(False)

        # Señales que cruzan de hilo: siempre a métodos ligados de este
        # widget, nunca a lambdas, para que Qt las encole en el hilo de la
        # interfaz.
        worker.stats_updated.connect(self._on_stats)
        worker.state_changed.connect(self._on_state)
        worker.preview_ready.connect(self._on_preview)
        worker.log_message.connect(self.log)
        worker.recording_finished.connect(self._on_recording_finished)
        self.log(f"{worker.label} conectado.")
        self.on_device_added(worker, panel)

    def _on_device_removed(self, device_id: str) -> None:
        panel = self.panels.pop(device_id, None)
        if panel is not None:
            self.stack.remove_widget(panel)
        if not self.panels:
            self.placeholder.setVisible(True)

    def on_device_added(self, worker: DeviceWorker, panel: DevicePanel) -> None:
        """Gancho para la pestaña concreta (por ejemplo, aplicar biases)."""
        return None

    def create_panel(self, worker: DeviceWorker) -> DevicePanel:
        return DevicePanel(worker)

    # ------------------------------------------------------------------
    # Señales de los workers
    # ------------------------------------------------------------------
    def _panel_of_sender(self) -> Optional[DevicePanel]:
        worker = self.sender()
        device_id = getattr(worker, "device_id", None)
        return self.panels.get(device_id) if device_id else None

    def _on_stats(self, stats: Dict[str, Any]) -> None:
        panel = self._panel_of_sender()
        if panel is not None:
            panel.refresh_stats(stats)

    def _on_state(self, state: DeviceState) -> None:
        panel = self._panel_of_sender()
        if panel is not None:
            panel.refresh_state()

    def _on_preview(self, image) -> None:
        panel = self._panel_of_sender()
        if panel is not None:
            panel.on_preview(image)

    def _on_recording_finished(self, summary: Dict[str, Any]) -> None:
        self.log(
            f"Grabación cerrada en {summary.get('dispositivo')}: "
            f"{summary.get('motivo', '')}")

    # ------------------------------------------------------------------
    # Grabación independiente
    # ------------------------------------------------------------------
    def workers(self) -> List[DeviceWorker]:
        return self.manager.by_family(self.family)

    def toggle_recording(self) -> None:
        if any(worker.is_recording for worker in self.workers()):
            self.stop_recording()
        else:
            self.start_recording()

    def start_recording(self) -> None:
        if self.recorder.recording:
            self.log("Hay una sesión global en curso: la grabación la "
                             "manda la pestaña 1.")
            return
        targets = [worker for worker in self.workers() if worker.is_armable]
        if not targets:
            self.log("Ningún dispositivo de esta familia está listo.")
            return

        base = self.save_selector.path() or str(self.sessions.base_dir)
        # Mismo criterio que en la pestaña 1: se valida el destino antes de
        # armar nada, y se graba en un solo archivo por toma.
        verdict = storage.validate_destination(base, create=True)
        for message in verdict.messages:
            self.log(message)
        if not verdict.ok:
            self.log("No se graba: el destino no sirve.")
            return

        session_root: Optional[Path] = None
        for worker in targets:
            directory = self.sessions.standalone_dir(
                base, self.family, worker.label, with_session_folder=True)
            if session_root is None:
                session_root = directory.parent
            worker.arm_recording(directory=directory, prefix=worker.label,
                                 duration_s=None, chunk_duration_s=None)

        # Se espera la confirmación de armado con un disparo diferido, igual
        # que hace el orquestador global: el armado ocurre en el hilo de cada
        # dispositivo y puede tardar (reservar el anillo de memoria).
        self._pending_targets = targets
        self._arm_attempts = 0
        QTimer.singleShot(120, self._try_start)
        self.log(f"Armando {len(targets)} dispositivo(s) en "
                         f"{session_root if session_root else base}")

    def _try_start(self) -> None:
        targets = getattr(self, "_pending_targets", [])
        if not targets:
            return
        pending = [worker for worker in targets
                   if worker.state is not DeviceState.ARMED]
        self._arm_attempts = getattr(self, "_arm_attempts", 0) + 1
        if pending and self._arm_attempts < 120:
            QTimer.singleShot(120, self._try_start)
            return
        if pending:
            names = ", ".join(worker.label for worker in pending)
            self.log(f"No armaron a tiempo: {names}. No se graba.")
            for worker in targets:
                worker.cancel_arm()
            self._pending_targets = []
            return

        from ...core.time_sync import TIME_BASE
        t0 = TIME_BASE.now_ns()
        for worker in targets:
            worker.start_recording(t0)
        self.log(f"Grabando {len(targets)} dispositivo(s).")
        self._pending_targets = []
        self._update_recording_buttons()

    def stop_recording(self) -> None:
        for worker in self.workers():
            if worker.is_recording:
                worker.stop_recording()
        self._pending_targets = []
        self.log("Grabación de la pestaña detenida.")
        self._update_recording_buttons()

    def _update_recording_buttons(self) -> None:
        if not self.has_own_recording:
            return
        recording = any(worker.is_recording for worker in self.workers())
        global_session = self.recorder.recording
        self.btn_record.setEnabled(not global_session)
        self.btn_stop.setEnabled(recording and not global_session)
        if global_session:
            self.btn_record.setText("● (sesión global)")
            self.btn_record.setStyleSheet(theme.button_style("normal"))
            self.lbl_rec_state.setText(
                "Grabando bajo la sesión global: los archivos van a la "
                "carpeta de la pestaña 1.")
            self.lbl_rec_state.setStyleSheet(
                theme.value_style(theme.WARN, 11))
        elif recording:
            self.btn_record.setText("■ DETENER")
            self.btn_record.setStyleSheet(theme.button_style("stop"))
            self.lbl_rec_state.setText("grabando (independiente)")
            self.lbl_rec_state.setStyleSheet(
                theme.value_style(theme.DANGER, 11))
        else:
            self.btn_record.setText("● GRABAR")
            self.btn_record.setStyleSheet(theme.button_style("danger"))
            self.lbl_rec_state.setText("sin grabar")
            self.lbl_rec_state.setStyleSheet(
                theme.value_style(theme.TEXT_FAINT, 11))

    # ------------------------------------------------------------------
    def refresh(self) -> None:
        for panel in self.panels.values():
            panel.refresh_state()
        self._update_recording_buttons()
        self.on_refresh()

    def on_refresh(self) -> None:
        return None
