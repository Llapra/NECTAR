"""Pestaña 8 · Enfoque: asistencia para enfocar la cámara de eventos.

Implementa el método de Li et al. tal como está en ``Enfoque EvCam.md``, con
el añadido que ese documento pide para un lente sin encoder: un observador
cinemático sobre un buffer de memoria corta que convierte una formulación
pensada para motores en una instrucción que un operador puede seguir con las
manos.

Por qué enfocar una cámara de eventos no es como enfocar una normal
-------------------------------------------------------------------
Una cámara convencional se enfoca maximizando el gradiente espacial de un
fotograma. Un sensor de eventos no entrega fotogramas y, además, se comporta
al revés de lo que uno esperaría: **desenfocado genera más eventos**, porque
el ensanchamiento del disco de Airy reparte cada transición de luz sobre
muchos píxeles vecinos y cada uno dispara por su cuenta.

Las dos etapas
--------------
**Etapa gruesa · EPR.** Se cuenta la razón entre eventos positivos y negativos
en ventanas de 10 ms, se suaviza con un núcleo gaussiano y se mira el signo de
su derivada. Positiva quiere decir "vas hacia el foco". Es barata —contar— y
es robusta a que el giro manual sea desparejo, porque no depende de la
magnitud absoluta de eventos. El área acumulada del tramo de derivada positiva
más grande es lo que decide que ya se está en la vecindad del foco.

**Etapa fina · ER + HFE.** Recién ahí se enciende la DFT 2D con máscara
anular (r_in = 0,24, r_out = 0,71) y se busca a la vez el mínimo de tasa de
eventos y el máximo de energía de alta frecuencia en dB. Los dos tienen que
coincidir para declarar foco: un mínimo de ER sin pico de HFE no es un
enfoque, es la escena apagándose.

Dónde corre el cálculo
----------------------
En su propio hilo, no en el lazo de adquisición. La cámara sigue entregando
mientras se calcula la FFT, y con el toggle apagado el lazo ni siquiera copia
los eventos: el costo es exactamente cero, como pide el documento.

Manual o motorizado
-------------------
En **manual** el programa dice hacia dónde girar y avisa del rebase. En
**motorizado** el mismo motor de decisión conduce una búsqueda automática en
dos etapas con compensación de juego mecánico. Los motores todavía no existen
en el banco: los controles aparecen deshabilitados con el motivo, no
simulando que funcionan.
"""

from __future__ import annotations

import time
from typing import Any, Dict, Optional

import numpy as np
from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtWidgets import (QComboBox, QFrame, QGridLayout, QGroupBox,
                             QHBoxLayout, QLabel, QProgressBar, QPushButton,
                             QVBoxLayout, QWidget)

from ...core.algorithms import focus_metrics
from ...core.algorithms.focus_metrics import (COLOR_FOCUS, COLOR_KEEP,
                                              COLOR_REVERSE, COLOR_SLOW,
                                              STAGE_COARSE, STAGE_FINE,
                                              STAGE_FOCUSED)
from ...devices.base import DeviceWorker
from ...devices.event_cam.focus_motor import (HAS_SERIAL, AutoFocusRunner,
                                              SerialFocusMotor)
from .. import theme
from ..widgets.common import (LabeledControl, LogConsole, Section, SliderSpin)
from ..widgets.image_view import ImageVisor
from ..widgets.plots import TimeSeriesPlot

MODE_MANUAL = "Manual (lente a mano)"
MODE_MOTOR = "Motorizado (controlador de pasos)"

#: Del color que reporta el motor de decisión al color del tema.
COLOR_MAP = {
    COLOR_KEEP: theme.OK_BRIGHT,
    COLOR_REVERSE: theme.ORANGE,
    COLOR_SLOW: theme.WARN,
    COLOR_FOCUS: theme.ACCENT_BRIGHT,
}
ARROW_MAP = {
    COLOR_KEEP: "↻",
    COLOR_REVERSE: "↺",
    COLOR_SLOW: "…",
    COLOR_FOCUS: "✔",
}
STAGE_LABEL = {
    STAGE_COARSE: "① Aproximación gruesa · EPR",
    STAGE_FINE: "② Ajuste fino · ER + HFE",
    STAGE_FOCUSED: "✔ Óptimo alcanzado",
}


class FocusTab(QWidget):
    """Asistente de enfoque sobre una cámara de eventos."""

    def __init__(self, manager, recorder, sessions,
                 parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.manager = manager
        self.recorder = recorder
        self.sessions = sessions

        self._worker: Optional[DeviceWorker] = None
        self._t_start = time.perf_counter()
        self._last_frame_update = 0.0
        self._locked = False

        self.motor = SerialFocusMotor()
        self.autofocus: Optional[AutoFocusRunner] = None

        self._build()
        self.manager.roster_changed.connect(self._refresh_cameras)
        self._refresh_cameras()

        self._timer = QTimer(self)
        self._timer.setInterval(800)
        self._timer.timeout.connect(self._refresh_cameras)
        self._timer.start()

    # ------------------------------------------------------------------
    def _build(self) -> None:
        root = QHBoxLayout(self)
        root.setContentsMargins(8, 6, 8, 8)
        root.setSpacing(8)

        left = QVBoxLayout()
        left.setSpacing(8)

        self.visor = ImageVisor(sensor_size=(640, 480), with_roi=False,
                                min_height=280)
        left.addWidget(self.visor, 1)

        self.plot_coarse = TimeSeriesPlot(y_label="EPR = P/N", capacity=800)
        self.plot_coarse.setMinimumHeight(150)
        self.plot_coarse.set_series(["EPR"])
        left.addWidget(self.plot_coarse, 1)

        self.plot_fine = TimeSeriesPlot(y_label="HFE (dB) · ER (MEv/s)",
                                        capacity=800)
        self.plot_fine.setMinimumHeight(150)
        self.plot_fine.set_series(["HFE dB", "mejor HFE", "tasa MEv/s"])
        left.addWidget(self.plot_fine, 1)

        container_left = QWidget()
        container_left.setLayout(left)
        root.addWidget(container_left, 1)

        right = QVBoxLayout()
        right.setSpacing(8)
        panel = QWidget()
        panel.setFixedWidth(380)
        panel.setLayout(right)

        right.addWidget(self._build_advice_box())
        right.addWidget(self._build_source_box())
        right.addWidget(self._build_motor_box())

        self.console = LogConsole(max_lines=200)
        right.addWidget(QLabel("Registro:"))
        right.addWidget(self.console, 1)

        root.addWidget(panel, 0)

    # ------------------------------------------------------------------
    def _build_advice_box(self) -> QWidget:
        box = Section("Consejo de enfoque")

        self.lbl_stage = QLabel("—")
        self.lbl_stage.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.lbl_stage.setStyleSheet(theme.value_style(theme.TEXT_FAINT, 12))
        box.add(self.lbl_stage)

        self.advice_frame = QFrame()
        self.advice_frame.setFrameShape(QFrame.Shape.StyledPanel)
        self.advice_frame.setMinimumHeight(120)
        advice_layout = QVBoxLayout(self.advice_frame)

        self.lbl_arrow = QLabel("—")
        self.lbl_arrow.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.lbl_arrow.setStyleSheet(theme.value_style(theme.TEXT_FAINT, 46))

        self.lbl_advice = QLabel("Selecciona una cámara y activa el asistente.")
        self.lbl_advice.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.lbl_advice.setWordWrap(True)
        self.lbl_advice.setStyleSheet(theme.value_style(theme.TEXT_DIM, 13))

        advice_layout.addWidget(self.lbl_arrow)
        advice_layout.addWidget(self.lbl_advice)
        self._set_advice_color(theme.BORDER_LIGHT)
        box.add(self.advice_frame)

        self.progress_coarse = QProgressBar()
        self.progress_coarse.setRange(0, 100)
        self.progress_coarse.setFormat("zona gruesa: %p%")
        self.progress_coarse.setToolTip(
            "Área acumulada bajo la derivada positiva de EPR. Al llegar al "
            "100% se enciende la etapa fina y con ella la DFT 2D.")
        box.add(self.progress_coarse)

        self.row_epr = box.add_row("EPR (P/N)", "—")
        self.row_epr_slope = box.add_row("dEPR/dt", "—")
        self.row_hfe = box.add_row("HFE", "—")
        self.row_hfe_slope = box.add_row("dHFE/dt", "—")
        self.row_rate = box.add_row("Tasa de eventos (ER)", "—")
        self.row_coupling = box.add_row("Acoplamiento ER↔HFE", "—")
        return box

    def _build_source_box(self) -> QWidget:
        box = Section("Fuente y modo")

        self.cmb_camera = QComboBox()
        self.cmb_camera.currentIndexChanged.connect(self._on_camera_changed)
        box.add(LabeledControl("Cámara de eventos", self.cmb_camera,
                               "Cámaras conectadas en la pestaña ②. El "
                               "enfoque se deriva del flujo ya abierto: no "
                               "abre la cámara otra vez."))

        row = QHBoxLayout()
        self.btn_refresh = QPushButton("⟳ Refrescar lista")
        self.btn_refresh.clicked.connect(self._refresh_cameras)
        row.addWidget(self.btn_refresh)
        box.add_layout(row)

        self.cmb_mode = QComboBox()
        self.cmb_mode.addItems([MODE_MANUAL, MODE_MOTOR])
        self.cmb_mode.currentTextChanged.connect(self._on_mode_changed)
        box.add(LabeledControl(
            "Modo de enfoque", self.cmb_mode,
            "Manual: el programa dice hacia dónde girar y avisa del rebase.\n"
            "Motorizado: el mismo criterio conduce una búsqueda automática "
            "en dos etapas."))

        row = QHBoxLayout()
        self.btn_enable = QPushButton("▶ ACTIVAR ASISTENTE")
        self.btn_enable.setStyleSheet(theme.button_style("ok"))
        self.btn_enable.clicked.connect(self._toggle_assistant)
        self.btn_reset = QPushButton("↺ Reiniciar")
        self.btn_reset.clicked.connect(self._reset)
        row.addWidget(self.btn_enable)
        row.addWidget(self.btn_reset)
        box.add_layout(row)

        # Solo aparece con cámaras simuladas: permite ensayar el asistente
        # completo sin lente ni banco óptico.
        self.sim_box = QWidget()
        sim_layout = QVBoxLayout(self.sim_box)
        sim_layout.setContentsMargins(0, 0, 0, 0)
        self.ctl_defocus = SliderSpin(0, 100, 0)
        self.ctl_defocus.value_changed.connect(self._on_defocus)
        sim_layout.addWidget(LabeledControl(
            "Desenfoque simulado (%)", self.ctl_defocus,
            "Solo con cámara simulada: ensancha las manchas y sube la tasa "
            "de eventos, igual que el desenfoque real."))
        box.add(self.sim_box)
        self.sim_box.setVisible(False)
        return box

    def _build_motor_box(self) -> QWidget:
        box = QGroupBox("Control del motor del anillo")
        layout = QVBoxLayout(box)

        self.lbl_motor = QLabel()
        self.lbl_motor.setWordWrap(True)
        self.lbl_motor.setStyleSheet(f"color: {theme.WARN}; font-size: 10px;")
        layout.addWidget(self.lbl_motor)

        self.cmb_motor_port = QComboBox()
        self.cmb_motor_port.setEditable(True)
        self.cmb_motor_port.setToolTip(
            "Puerto del controlador de pasos. Protocolo: MOVE/POS?/HOME/STOP "
            "en texto, terminado en salto de línea.")
        layout.addWidget(LabeledControl("Puerto", self.cmb_motor_port))

        self.btn_motor_connect = QPushButton("Conectar motor")
        self.btn_motor_connect.clicked.connect(self._toggle_motor)
        layout.addWidget(self.btn_motor_connect)

        grid = QGridLayout()
        self.btn_ccw = QPushButton("↺ Paso")
        self.btn_cw = QPushButton("↻ Paso")
        self.btn_auto = QPushButton("BUSCAR FOCO (automático)")
        self.btn_auto.setStyleSheet(theme.button_style("accent"))
        self.btn_ccw.clicked.connect(lambda: self._step(-1))
        self.btn_cw.clicked.connect(lambda: self._step(1))
        self.btn_auto.clicked.connect(self._run_autofocus)
        grid.addWidget(self.btn_ccw, 0, 0)
        grid.addWidget(self.btn_cw, 0, 1)
        grid.addWidget(self.btn_auto, 1, 0, 1, 2)
        layout.addLayout(grid)

        self.ctl_step = SliderSpin(1, 500, 20)
        layout.addWidget(LabeledControl(
            "Tamaño del paso (unidades motor)", self.ctl_step,
            "El movimiento compensa el juego mecánico: al invertir el "
            "sentido se pasa de largo y se vuelve, para llegar siempre desde "
            "el mismo lado."))

        self.lbl_motor_state = QLabel("sin conectar")
        self.lbl_motor_state.setStyleSheet(
            theme.value_style(theme.TEXT_FAINT, 11))
        layout.addWidget(self.lbl_motor_state)

        self._motor_widgets = [self.cmb_motor_port, self.btn_motor_connect,
                               self.btn_ccw, self.btn_cw, self.btn_auto,
                               self.ctl_step]
        self._update_motor_box()
        return box

    # ------------------------------------------------------------------
    def _update_motor_box(self) -> None:
        motorized = self.cmb_mode.currentText() == MODE_MOTOR
        connected = self.motor.status.connected
        for widget in self._motor_widgets:
            widget.setEnabled(motorized and not self._locked)
        for widget in (self.btn_ccw, self.btn_cw, self.btn_auto):
            widget.setEnabled(motorized and connected and not self._locked)

        if not motorized:
            self.lbl_motor.setText(
                "Modo manual: el anillo lo giras tú y el programa te va "
                "diciendo hacia dónde.")
            self.lbl_motor.setStyleSheet(
                f"color: {theme.TEXT_FAINT}; font-size: 10px;")
        elif not HAS_SERIAL:
            self.lbl_motor.setText(
                "⚠ pyserial no está instalado: no se puede hablar con un "
                "controlador de pasos. pip install pyserial")
            self.lbl_motor.setStyleSheet(
                f"color: {theme.DANGER}; font-size: 10px;")
        elif not connected:
            self.lbl_motor.setText(
                "⚠ Todavía no hay motores montados en el banco. La lógica de "
                "búsqueda está lista: en cuanto exista el controlador, se "
                "escribe el puerto y se conecta.")
            self.lbl_motor.setStyleSheet(
                f"color: {theme.WARN}; font-size: 10px;")
        else:
            self.lbl_motor.setText("Motor conectado.")
            self.lbl_motor.setStyleSheet(
                f"color: {theme.OK_BRIGHT}; font-size: 10px;")
        self.btn_motor_connect.setText(
            "Desconectar motor" if connected else "Conectar motor")
        self.lbl_motor_state.setText(
            f"posición {self.motor.status.position} pasos"
            if connected else (self.motor.status.error or "sin conectar"))

    def _toggle_motor(self) -> None:
        if self.motor.status.connected:
            self.motor.disconnect()
            self.console.log("Motor desconectado.")
        else:
            self.motor.port = self.cmb_motor_port.currentText().strip()
            if self.motor.connect():
                self.console.log(f"Motor conectado en {self.motor.port}.")
            else:
                self.console.log(
                    f"<span style='color:{theme.DANGER}'>No se pudo conectar "
                    f"el motor: {self.motor.status.error}</span>")
        self._update_motor_box()

    def _step(self, direction: int) -> None:
        if not self.motor.status.connected:
            return
        self.motor.move(direction * int(self.ctl_step.value()))
        self._update_motor_box()

    def _run_autofocus(self) -> None:
        if self._worker is None:
            self.console.log("Activa primero el asistente sobre una cámara.")
            return
        focus = getattr(self._worker, "_focus", None)
        if focus is None:
            self.console.log("El calculador de enfoque no está corriendo.")
            return
        if self.autofocus is not None and self.autofocus.running:
            self.autofocus.abort()
            self.console.log("Búsqueda automática abortada.")
            return
        self.autofocus = AutoFocusRunner(self.motor, focus.engine,
                                         on_progress=self._on_autofocus)
        if self.autofocus.start():
            self.console.log("Búsqueda automática de foco en marcha…")

    def _on_autofocus(self, payload: Dict[str, Any]) -> None:
        # Llega desde el hilo del autofoco: solo se guarda, la interfaz lo
        # recoge en el próximo refresco.
        self._autofocus_state = payload

    # ------------------------------------------------------------------
    def _set_advice_color(self, color: str) -> None:
        self.advice_frame.setStyleSheet(
            f"QFrame {{ border: 2px solid {color}; border-radius: 8px; "
            f"background-color: {theme.BG_PANEL}; }}")

    def set_locked(self, locked: bool) -> None:
        self._locked = locked
        self.btn_enable.setEnabled(not locked)
        self.cmb_mode.setEnabled(not locked)
        self._update_motor_box()

    # ------------------------------------------------------------------
    def _refresh_cameras(self) -> None:
        workers = self.manager.by_family("event_cam")
        labels = [worker.display_name for worker in workers]
        current = self.cmb_camera.currentText()

        self.cmb_camera.blockSignals(True)
        self.cmb_camera.clear()
        self.cmb_camera.addItems(labels)
        if current in labels:
            self.cmb_camera.setCurrentIndex(labels.index(current))
        self.cmb_camera.blockSignals(False)

        if self._worker is not None and self._worker not in workers:
            self._detach()
        self.btn_enable.setEnabled(bool(labels) and not self._locked)

        state = getattr(self, "_autofocus_state", None)
        if state:
            self.lbl_motor_state.setText(
                f"{state.get('fase', '')} · "
                f"{state.get('hecho', 0)}/{state.get('total', 0)} · "
                f"pos {state.get('posicion', 0)}")

    def _current_worker(self) -> Optional[DeviceWorker]:
        workers = self.manager.by_family("event_cam")
        index = self.cmb_camera.currentIndex()
        if 0 <= index < len(workers):
            return workers[index]
        return None

    def _on_camera_changed(self, _index: int) -> None:
        if self._worker is not None:
            self._detach()

    def _on_mode_changed(self, text: str) -> None:
        motorized = text == MODE_MOTOR
        if self._worker is not None:
            self._worker.set_focus_motorized(motorized)
        self._update_motor_box()
        self.console.log(
            f"Modo de enfoque: {'motorizado' if motorized else 'manual'}. "
            + ("La tolerancia de acoplamiento vuelve a los 10 ms del método."
               if motorized else
               "La tolerancia de acoplamiento se adapta al ritmo del giro."))

    def _toggle_assistant(self) -> None:
        if self._worker is not None:
            self._detach()
            return
        worker = self._current_worker()
        if worker is None:
            self.console.log("No hay cámaras de eventos conectadas.")
            return
        self._worker = worker
        worker.focus_metrics_ready.connect(self._on_metrics)
        worker.set_focus_enabled(True)
        worker.set_focus_motorized(self.cmb_mode.currentText() == MODE_MOTOR)
        self._t_start = time.perf_counter()
        self.plot_coarse.clear_data()
        self.plot_fine.clear_data()
        self.btn_enable.setText("■ DETENER ASISTENTE")
        self.btn_enable.setStyleSheet(theme.button_style("danger"))
        self.sim_box.setVisible(bool(getattr(worker, "simulated", False)))
        self.console.log(f"Asistente activo sobre {worker.display_name}. "
                         f"Gira el anillo despacio y mira la flecha.")

    def _detach(self) -> None:
        worker = self._worker
        self._worker = None
        if worker is not None:
            try:
                worker.focus_metrics_ready.disconnect(self._on_metrics)
            except TypeError:
                pass
            worker.set_focus_enabled(False)
            self.console.log(f"Asistente detenido ({worker.display_name}).")
        self.btn_enable.setText("▶ ACTIVAR ASISTENTE")
        self.btn_enable.setStyleSheet(theme.button_style("ok"))
        self.sim_box.setVisible(False)

    def _reset(self) -> None:
        if self._worker is not None:
            focus = getattr(self._worker, "_focus", None)
            if focus is not None:
                focus.reset()
        self.plot_coarse.clear_data()
        self.plot_fine.clear_data()
        self.console.log("Curvas de enfoque reiniciadas.")

    def _on_defocus(self, value: int) -> None:
        if self._worker is not None:
            self._worker.set_simulated_defocus(value / 100.0 * 0.8)

    # ------------------------------------------------------------------
    def _on_metrics(self, payload: Dict[str, Any]) -> None:
        """Una actualización del motor de decisión. Llega por señal de Qt."""
        stage = payload.get("etapa", STAGE_COARSE)
        color_key = payload.get("color", COLOR_SLOW)
        color = COLOR_MAP.get(color_key, theme.TEXT_FAINT)

        self.lbl_stage.setText(STAGE_LABEL.get(stage, stage))
        self.lbl_arrow.setText(ARROW_MAP.get(color_key, "—"))
        self.lbl_arrow.setStyleSheet(theme.value_style(color, 46))
        self.lbl_advice.setText(payload.get("mensaje", ""))
        self._set_advice_color(color)

        epr = payload.get("epr", 0.0)
        self.row_epr.set_value(f"{epr:.3f}")
        self.row_epr_slope.set_value(f"{payload.get('epr_pendiente', 0):+.3f} /s")

        hfe = payload.get("hfe_db")
        self.row_hfe.set_value(f"{hfe:.2f} dB" if hfe is not None else "—")
        self.row_hfe_slope.set_value(
            f"{payload.get('hfe_pendiente', 0):+.2f} dB/s")
        self.row_rate.set_value(f"{payload.get('tasa_mev_s', 0):.3f} MEv/s")

        coupled = payload.get("acoplado", False)
        self.row_coupling.set_value(
            "✔ coinciden" if coupled else "aún no",
            theme.OK_BRIGHT if coupled else theme.TEXT_FAINT)

        area = payload.get("area_gruesa", 0.0)
        umbral = focus_metrics.COARSE_AREA_THRESHOLD
        self.progress_coarse.setValue(
            int(min(100.0, area / umbral * 100.0)))

        elapsed = time.perf_counter() - self._t_start
        self.plot_coarse.append_many(elapsed, {"EPR": epr})
        if hfe is not None:
            self.plot_fine.append_many(elapsed, {
                "HFE dB": hfe,
                "tasa MEv/s": payload.get("tasa_mev_s", 0.0),
            })

        # El marco acumulado se refresca despacio: es una ayuda visual, no la
        # vista previa principal (esa está en la pestaña 2).
        now = time.perf_counter()
        frame = payload.get("frame")
        if frame is not None and now - self._last_frame_update >= 0.25:
            self._last_frame_update = now
            normalized = focus_metrics.normalize_log(frame)
            image = np.clip(normalized, 0, 255).astype(np.uint8)
            self.visor.set_image(image)
