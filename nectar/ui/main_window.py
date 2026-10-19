"""Ventana principal: arma las diez pestañas y el estado compartido.

Todo lo que es común vive acá y se pasa a las pestañas por constructor, no por
variables globales:

* ``SessionManager`` — quién decide dónde se guarda cada byte.
* ``GlobalRecorder`` — el protocolo de disparo síncrono de 4 fases.
* ``DeviceManager`` — el plantel de dispositivos conectados, compartido por
  la pestaña 1 y por las pestañas de familia.

Las pestañas no se hablan entre ellas: hablan con estos tres objetos. Por eso
conectar una cámara en la pestaña 2 hace aparecer su LED en la pestaña 1 sin
que ninguna de las dos sepa de la existencia de la otra.
"""

from __future__ import annotations

import time
from typing import Dict, Optional

from PyQt6.QtCore import QTimer
from PyQt6.QtGui import QAction, QKeySequence
from PyQt6.QtWidgets import (QLabel, QMainWindow, QMessageBox, QStatusBar,
                             QTabWidget, QWidget)

from .. import config
from ..core.algorithms.jit_render import HAS_NUMBA, warmup
from ..core.device_manager import DeviceManager
from ..core.logbook import LOG
from ..core.global_recorder import GlobalRecorder
from ..core.session_manager import SessionManager
from ..core.time_sync import TIME_BASE
from . import theme
from .tabs.tab_acquisition import AcquisitionTab
from .tabs.tab_anemometer import AnemometerTab
from .tabs.tab_converter import ConverterTab
from .tabs.tab_emergent import EmergentTab
from .tabs.tab_evcam import EvCamTab
from .tabs.tab_fibers import FibersTab
from .tabs.tab_focus import FocusTab
from .tabs.tab_imu import ImuTab
from .tabs.tab_nn1 import NN1Tab
from .tabs.tab_reconstruction import ReconstructionTab
from .tabs.tab_topography import TopographyTab

#: Orden y rótulo de las pestañas, tal como se pidió.
TAB_SPECS = [
    ("acquisition", "① Adquisición", AcquisitionTab, None),
    ("evcam", "② EvCam", EvCamTab, "event_cam"),
    ("emergent", "③ Emergent", EmergentTab, "emergent"),
    ("reconstruction", "④ Reconstrucción", ReconstructionTab, "reconstruction"),
    ("fibers", "⑤ Fibras", FibersTab, "fibers"),
    ("anemometer", "⑥ Anemómetro", AnemometerTab, "anemometer"),
    ("imu", "⑦ IMU-GPS", ImuTab, "imu"),
    ("focus", "⑧ Enfoque", FocusTab, None),
    ("converter", "⑨ Conversor IMU", ConverterTab, None),
    ("topography", "⑩ Topografía y Enlace", TopographyTab, None),
    ("nn1", "⑪ NN1", NN1Tab, None),
]


class MainWindow(QMainWindow):
    """Ventana de NECTAR."""

    def __init__(self, base_dir: Optional[str] = None,
                 force_demo: bool = False) -> None:
        super().__init__()
        demo_mark = "  ·  ⚠ MODO DEMOSTRACIÓN (datos falsos)" if config.force_demo() else ""
        self.setWindowTitle(f"{config.APP_NAME} v{config.APP_VERSION} · "
                            f"{config.APP_SUBTITLE}{demo_mark}")
        self.setGeometry(40, 40, 1680, 980)
        self.setStyleSheet(theme.stylesheet())

        # --- estado compartido -----------------------------------------
        self.sessions = SessionManager(base_dir=base_dir)
        self.recorder = GlobalRecorder(self.sessions, parent=self)
        self.manager = DeviceManager(self.recorder, parent=self)
        self.force_demo = force_demo

        LOG.info(f"{config.APP_NAME} {config.APP_VERSION} iniciado"
                 + (" en MODO DEMOSTRACIÓN" if config.force_demo() else ""))
        if HAS_NUMBA:
            # Compilar ahora y no en el primer frame: la primera llamada a una
            # función @njit paga la compilación, y no se quiere ese tirón justo
            # cuando la cámara empieza a entregar datos.
            warmup()

        # --- pestañas ---------------------------------------------------
        self.tabs = QTabWidget()
        self.setCentralWidget(self.tabs)
        self.tab_widgets: Dict[str, QWidget] = {}
        self._family_tab_index: Dict[str, int] = {}

        for key, label, tab_class, family in TAB_SPECS:
            widget = tab_class(self.manager, self.recorder, self.sessions)
            index = self.tabs.addTab(widget, label)
            self.tab_widgets[key] = widget
            if family:
                self._family_tab_index[family] = index

        acquisition = self.tab_widgets["acquisition"]
        if hasattr(acquisition, "open_tab_requested"):
            acquisition.open_tab_requested.connect(self._open_family_tab)

        self._build_status_bar()
        self._build_shortcuts()

        self._clock = QTimer(self)
        self._clock.setInterval(1000)
        self._clock.timeout.connect(self._update_status)
        self._clock.start()

        # Todo lo que dice el orquestador queda también en la bitácora de
        # disco: la consola sirve mientras alguien la mira, y en una campaña
        # nocturna nadie la mira todo el rato.
        self.recorder.log.connect(LOG.info)
        self.manager.log.connect(LOG.info)
        self.manager.log.connect(self._on_log)
        self.manager.locked_action.connect(self._on_locked_action)
        self.recorder.failed.connect(self._on_recorder_failed)
        self.recorder.lock_changed.connect(self._on_lock_changed)

    # ------------------------------------------------------------------
    def _build_status_bar(self) -> None:
        bar = QStatusBar()
        self.setStatusBar(bar)

        self.lbl_status_devices = QLabel("0 dispositivos")
        self.lbl_status_phase = QLabel("inactivo")
        self.lbl_status_clock = QLabel("—")
        self.lbl_status_session = QLabel("sin sesión")

        for label in (self.lbl_status_devices, self.lbl_status_phase,
                      self.lbl_status_session, self.lbl_status_clock):
            label.setStyleSheet(
                f"color: {theme.TEXT_FAINT}; font-family: {theme.MONO}; "
                f"font-size: 11px; padding: 0 10px;")
            bar.addPermanentWidget(label)

        deps = []
        if not HAS_NUMBA:
            deps.append("sin numba")
        from .widgets.image_view import HAS_PYQTGRAPH
        if not HAS_PYQTGRAPH:
            deps.append("sin pyqtgraph")
        if deps:
            warning = QLabel(" · ".join(deps))
            warning.setStyleSheet(f"color: {theme.WARN}; font-size: 11px; "
                                  f"padding: 0 10px;")
            bar.addPermanentWidget(warning)

        if config.force_demo():
            banner = QLabel("⚠ MODO DEMOSTRACIÓN · LOS DATOS SON FALSOS")
            banner.setStyleSheet(
                f"color: #000; background-color: {theme.WARN}; "
                f"font-weight: bold; font-size: 12px; padding: 2px 10px;")
            bar.addPermanentWidget(banner)
        bar.showMessage(f"{config.APP_NAME} listo. "
                        f"Empieza por la pestaña ① Adquisición.")

    def _build_shortcuts(self) -> None:
        """Atajos para operar de noche sin buscar el mouse."""
        actions = [
            ("Disparo global", "F5", self._shortcut_fire),
            ("Detener todo", "F6", self._shortcut_stop),
            ("Detectar todos los dispositivos", "F4",
             self.manager.discover_all),
        ]
        for name, key, handler in actions:
            action = QAction(name, self)
            action.setShortcut(QKeySequence(key))
            action.triggered.connect(handler)
            self.addAction(action)

    def _shortcut_fire(self) -> None:
        self.tabs.setCurrentIndex(0)
        acquisition = self.tab_widgets["acquisition"]
        if not self.recorder.recording:
            acquisition._on_fire_clicked()

    def _shortcut_stop(self) -> None:
        if self.recorder.recording:
            self.recorder.stop_all(reason="atajo_f6")

    # ------------------------------------------------------------------
    def _open_family_tab(self, family: str) -> None:
        index = self._family_tab_index.get(family)
        if index is not None:
            self.tabs.setCurrentIndex(index)

    def _on_log(self, message: str) -> None:
        self.statusBar().showMessage(message, 6000)

    def _on_recorder_failed(self, message: str) -> None:
        LOG.error(f"Disparo rechazado: {message}")
        self.statusBar().showMessage(f"✖ {message}", 12000)
        QMessageBox.warning(self, "No se puede grabar", message)

    def _on_locked_action(self, message: str) -> None:
        self.statusBar().showMessage(f"🔒 {message}", 8000)

    def _on_lock_changed(self, locked: bool) -> None:
        """Congela las pestañas que no son la 1 mientras se graba.

        Durante una toma, cambiar un bias, mover un ROI o quitar un
        instrumento son acciones que corrompen el dato sin avisar: los
        metadatos ya escritos dejan de describir lo que se está grabando. Se
        bloquea la interfaz entera menos la pestaña de adquisición, que es
        donde está el botón de DETENER.
        """
        for key, widget in self.tab_widgets.items():
            if key == "acquisition":
                continue
            index = self.tabs.indexOf(widget)
            if index >= 0:
                self.tabs.setTabEnabled(index, not locked)
            setter = getattr(widget, "set_locked", None)
            if callable(setter):
                try:
                    setter(locked)
                except Exception:
                    pass
        if locked:
            self.tabs.setCurrentIndex(0)
            self.statusBar().showMessage(
                "🔒 Grabación en curso: los parámetros quedan congelados.",
                0)
        else:
            self.statusBar().clearMessage()

    # ------------------------------------------------------------------
    def _update_status(self) -> None:
        total = self.manager.count()
        ready = self.manager.ready_count()
        self.lbl_status_devices.setText(f"{ready}/{total} listos")

        phase = self.recorder.phase
        self.lbl_status_phase.setText(f"fase: {phase}")
        color = theme.DANGER if self.recorder.recording else theme.TEXT_FAINT
        self.lbl_status_phase.setStyleSheet(
            f"color: {color}; font-family: {theme.MONO}; font-size: 11px; "
            f"padding: 0 10px;")

        session = self.sessions.session_name
        self.lbl_status_session.setText(session or "sin sesión")

        anchor = TIME_BASE.anchor
        if anchor is not None:
            stamp = time.strftime("%H:%M:%S",
                                  time.gmtime(anchor.to_utc(TIME_BASE.now_ns())))
            self.lbl_status_clock.setText(f"UTC {stamp}")
        else:
            self.lbl_status_clock.setText(time.strftime("local %H:%M:%S"))

    # ------------------------------------------------------------------
    def closeEvent(self, event) -> None:  # noqa: N802
        if self.recorder.recording:
            answer = QMessageBox.question(
                self, "Grabación en curso",
                "Hay una grabación global en curso. ¿Cerrar de todas formas?\n"
                "Los archivos se cerrarán correctamente antes de salir.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No)
            if answer != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
            self.recorder.stop_all(reason="cierre_del_programa")

        self.manager.shutdown()
        LOG.info("NECTAR cerrado por el operador.")
        LOG.close()
        event.accept()
