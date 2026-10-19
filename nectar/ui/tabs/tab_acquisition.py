"""Pestaña 1 · Adquisición: la que manda sobre todas las demás.

Qué resuelve
------------
Sincronización por software. No hay un cable de disparo común entre una cámara
de eventos por USB, una Emergent por 25GigE, un acondicionador de fibras por
Ethernet y un ESP32 por serie. Lo que sí se puede garantizar es que **todos
empezaron a grabar en el mismo instante conocido**, y que ese instante quedó
escrito con el reloj monotónico del host. Eso es suficiente para alinear las
series después, que es lo que necesita el análisis tomográfico.

Disposición (sigue el boceto)
-----------------------------
::

    +--------------------------------------------------------------------+
    |  destino de la sesión · nombre · marca de tiempo automática        |
    +---------------------+------------------------+-------------------+
    |  EvCam        ● ▽   |   ESTADO Y PRESUPUESTO |   Fibras     ▽ ●  |
    |  Emergent     ● ▽   |   DE ALMACENAMIENTO    |   Anemómetro ▽ ●  |
    |  Recons.EvCam ● ▽   |                        |   IMU-GPS    ▽ ●  |
    |                     |   [ INICIAR / DETENER ]|                   |
    |                     |   monitor + log        |                   |
    |  Tiempo de grabación|                        |   Vista previa    |
    |  [sin límite / ...] |                        |   2x2 miniaturas  |
    +---------------------+------------------------+-------------------+

El panel central de arriba —que en el boceto decía "algo agregar"— es el
**presupuesto de almacenamiento y estado del reloj**. Es la información que
hace falta *antes* de apretar el botón: cuántos instrumentos van a grabar,
cuántos MB/s suman entre todos, cuánto espacio libre queda en el destino y,
por lo tanto, cuánto tiempo se puede grabar antes de llenar el disco. Esa
última cifra es la que decide si una campaña de dos horas es viable o no, y
es justo lo que no se puede calcular a ojo con seis instrumentos de caudales
que difieren en cuatro órdenes de magnitud. Debajo aparece el estado del
reloj: T0 de la sesión y si el GPS ya ancló la hora UTC.
"""

from __future__ import annotations

import time
from typing import Dict, List, Optional

from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtWidgets import (QCheckBox, QComboBox, QFrame, QGridLayout,
                             QHBoxLayout, QLabel, QLineEdit, QMessageBox,
                             QProgressBar, QPushButton, QSpinBox,
                             QStackedWidget, QVBoxLayout, QWidget)

from ... import config
from ...core.device_registry import REGISTRY
from ...core import storage
from ...core.logbook import LOG
from ...core.time_sync import TIME_BASE
from ...devices.base import DeviceState, DeviceWorker
from .. import theme
from ..widgets.common import LogConsole, SavePathSelector, Section, ValueRow
from ..widgets.device_card import FamilyCard
from ..widgets.mini_preview import MiniPreviewGrid

#: Familias que van en la columna izquierda y en la derecha, como el boceto.
LEFT_FAMILIES = ["event_cam", "emergent", "reconstruction"]
RIGHT_FAMILIES = ["fibers", "anemometer", "imu"]


class AcquisitionTab(QWidget):
    """Matriz de hardware, presupuesto, disparo global y monitor."""

    open_tab_requested = pyqtSignal(str)

    def __init__(self, manager, recorder, sessions,
                 parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.manager = manager
        self.recorder = recorder
        self.sessions = sessions
        self.cards: Dict[str, FamilyCard] = {}

        self._build()
        self._wire()

        self._refresh_timer = QTimer(self)
        self._refresh_timer.setInterval(500)
        self._refresh_timer.timeout.connect(self.refresh)
        self._refresh_timer.start()

    # ------------------------------------------------------------------
    # Construcción
    # ------------------------------------------------------------------
    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(8, 6, 8, 8)
        root.setSpacing(8)

        root.addWidget(self._build_destination_bar())

        columns = QHBoxLayout()
        columns.setSpacing(10)
        columns.addWidget(self._build_left_column(), 22)
        columns.addWidget(self._build_center_column(), 46)
        columns.addWidget(self._build_right_column(), 22)
        root.addLayout(columns, 1)

    # --- barra superior: destino y nombre de la sesión ------------------
    def _build_destination_bar(self) -> QWidget:
        frame = QFrame()
        frame.setFrameShape(QFrame.Shape.StyledPanel)
        frame.setStyleSheet(
            f"QFrame {{ border: 1px solid {theme.BORDER}; border-radius: 6px; "
            f"background-color: {theme.BG_PANEL}; }}")
        layout = QHBoxLayout(frame)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(12)

        title = QLabel("DESTINO DE LA SESIÓN")
        title.setStyleSheet(f"color: {theme.ACCENT}; font-weight: bold; "
                            f"font-size: 12px;")

        self.btn_destination = QPushButton("📁 Elegir carpeta base")
        self.btn_destination.setStyleSheet(theme.button_style("warn"))
        self.btn_destination.clicked.connect(self._choose_destination)

        self.lbl_destination = QLabel(str(self.sessions.base_dir))
        self.lbl_destination.setStyleSheet(
            f"color: {theme.CONSOLE_TEXT}; font-family: {theme.MONO}; "
            f"font-size: 11px; border: 1px solid {theme.BORDER}; padding: 4px;")
        self.lbl_destination.setWordWrap(True)

        self.txt_session = QLineEdit("Cupula")
        self.txt_session.setToolTip(
            "Nombre de la sesión. Se le agrega la fecha y hora, y con él se "
            "crea la carpeta que contiene a todas las familias.")
        self.txt_session.setMaximumWidth(220)

        self.chk_timestamp = QCheckBox("Marca de tiempo automática")
        self.chk_timestamp.setChecked(True)

        self.btn_benchmark = QPushButton("⏱ Probar velocidad")
        self.btn_benchmark.setToolTip(
            "Escribe 512 MB de verdad en el destino y mide el caudal "
            "sostenido y el peor bloque. Es la única forma de saber si un "
            "disco aguanta una Emergent antes de confiarle una noche.")
        self.btn_benchmark.clicked.connect(self._run_benchmark)

        layout.addWidget(title)
        layout.addWidget(self.btn_destination)
        layout.addWidget(self.lbl_destination, 1)
        layout.addWidget(self.btn_benchmark)
        layout.addWidget(QLabel("Sesión:"))
        layout.addWidget(self.txt_session)
        layout.addWidget(self.chk_timestamp)
        return frame

    # ------------------------------------------------------------------
    def _run_benchmark(self) -> None:
        """Mide el disco de destino escribiendo en él de verdad."""
        if self.recorder.busy:
            return
        destination = str(self.sessions.base_dir)
        self.console.rule("BANCO DE PRUEBAS DE ESCRITURA")
        self.console.log(f"Escribiendo 512 MB en {destination}…")
        self.btn_benchmark.setEnabled(False)
        self.btn_benchmark.setText("⏱ Midiendo…")
        # La medición bloquea unos segundos; se procesa la cola de eventos
        # antes para que el mensaje de arriba llegue a verse.
        from PyQt6.QtWidgets import QApplication
        QApplication.processEvents()
        try:
            result = storage.benchmark_write(destination, total_mb=512,
                                             block_mb=8)
        except Exception as exc:
            result = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        finally:
            self.btn_benchmark.setEnabled(True)
            self.btn_benchmark.setText("⏱ Probar velocidad")

        if not result.get("ok"):
            message = f"✖ El banco de pruebas falló: {result.get('error')}"
            self.console.log(f"<span style='color:{theme.DANGER}'>{message}</span>")
            LOG.error(message)
            return

        mean = result["mb_s_medio"]
        worst = result["mb_s_peor_bloque"]
        lines = [
            f"Sistema de archivos: {result.get('sistema_archivos') or '?'} · "
            f"{result.get('libre_gb')} GB libres",
            f"Caudal sostenido: {mean:,.0f} MB/s",
            f"Peor bloque: {result['peor_bloque_ms']} ms "
            f"({worst:,.0f} MB/s equivalentes)",
            f"Percentil 99 de bloque: {result['p99_bloque_ms']} ms",
        ]
        for line in lines:
            self.console.log(line)
            LOG.info(f"[banco de pruebas] {line}")

        # El veredicto se da contra el caudal que de verdad se va a pedir,
        # no contra un número abstracto.
        needed = self.recorder.budget(self.duration_s()).total_mb_s
        if needed <= 0:
            veredicto = ("Conecta los instrumentos y repite la prueba para "
                         "comparar contra el caudal real.")
            color = theme.TEXT_DIM
        elif worst >= needed * 1.5:
            veredicto = (f"✔ Con holgura: el peor bloque da {worst:,.0f} MB/s "
                         f"y los instrumentos piden {needed:,.0f} MB/s.")
            color = theme.OK_BRIGHT
        elif worst >= needed:
            veredicto = (f"⚠ Justo: el peor bloque da {worst:,.0f} MB/s para "
                         f"{needed:,.0f} MB/s pedidos. Un hipo del sistema "
                         f"costaría frames.")
            color = theme.WARN
        else:
            veredicto = (f"✖ No alcanza: el peor bloque da {worst:,.0f} MB/s "
                         f"y los instrumentos piden {needed:,.0f} MB/s. "
                         f"Se van a perder frames.")
            color = theme.DANGER
        self.console.log(f"<span style='color:{color}'>{veredicto}</span>")
        LOG.info(f"[banco de pruebas] {veredicto}")

    # --- columna izquierda ---------------------------------------------
    def _build_left_column(self) -> QWidget:
        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        for family in LEFT_FAMILIES:
            device_type = REGISTRY.get(family)
            if device_type is None:
                continue
            card = FamilyCard(family, config.FAMILY_LABELS.get(
                family, device_type.label))
            card.include_changed.connect(self._on_include_changed)
            card.alias_changed.connect(self._on_alias_changed)
            card.open_tab_requested.connect(self.open_tab_requested.emit)
            self.cards[family] = card
            layout.addWidget(card)

        layout.addStretch(1)
        layout.addWidget(self._build_duration_box())
        return container

    def _build_duration_box(self) -> QWidget:
        """Control de duración: una sola cosa visible a la vez.

        Antes había un desplegable con cuatro opciones **y** una casilla de
        minutos, y una mandaba sobre la otra según una regla que había que
        recordar. Con seis instrumentos armados a las tres de la mañana eso es
        una trampa. Ahora se ve "SIN LÍMITE" y un botón; si se aprieta el
        botón, el rótulo se cambia por un campo de minutos y el botón vuelve a
        "sin límite". En todo momento lo que está en pantalla es exactamente
        lo que va a pasar.
        """
        box = Section("Tiempo de grabación")

        self.duration_stack = QStackedWidget()

        unlimited = QLabel("SIN LÍMITE")
        unlimited.setAlignment(Qt.AlignmentFlag.AlignCenter)
        unlimited.setStyleSheet(theme.value_style(theme.OK_BRIGHT, 20))
        unlimited.setToolTip(
            "La grabación sigue hasta que se presione DETENER, o hasta que "
            f"el destino baje del margen de "
            f"{config.MIN_FREE_BYTES / 1e9:.0f} GB libres.")
        self.duration_stack.addWidget(unlimited)

        timed = QWidget()
        timed_layout = QHBoxLayout(timed)
        timed_layout.setContentsMargins(0, 0, 0, 0)
        self.spin_minutes = QSpinBox()
        self.spin_minutes.setRange(1, 1440)
        self.spin_minutes.setValue(30)
        self.spin_minutes.setSuffix(" min")
        self.spin_minutes.setStyleSheet("font-size: 18px;")
        self.spin_minutes.setToolTip("Duración exacta de la toma.")
        self.spin_minutes.valueChanged.connect(lambda _: self.refresh())
        timed_layout.addWidget(self.spin_minutes)
        self.duration_stack.addWidget(timed)

        box.add(self.duration_stack)

        self.btn_duration_mode = QPushButton("⏱ Poner un límite de tiempo")
        self.btn_duration_mode.setToolTip(
            "Cambia entre grabar sin límite y grabar una cantidad fija de "
            "minutos.")
        self.btn_duration_mode.clicked.connect(self._toggle_duration_mode)
        box.add(self.btn_duration_mode)

        # --- modo de escritura -----------------------------------------
        self.chk_chunks = QCheckBox("Partir en bloques")
        self.chk_chunks.setToolTip(
            "Apagado (recomendado): un solo archivo por toma y por "
            "instrumento, sin costuras.\n"
            "Encendido: se corta cada cierto tiempo. En la cámara de eventos "
            "cada corte pierde los milisegundos que tarda el HAL en cerrar un "
            ".raw y abrir el siguiente.")
        self.chk_chunks.toggled.connect(self._on_chunk_toggled)
        box.add(self.chk_chunks)

        self.cmb_chunk = QComboBox()
        for label, seconds in config.CHUNK_OPTIONS:
            self.cmb_chunk.addItem(label, seconds)
        index = [seconds for _, seconds in config.CHUNK_OPTIONS]
        if config.CHUNK_DEFAULT_S in index:
            self.cmb_chunk.setCurrentIndex(index.index(config.CHUNK_DEFAULT_S))
        self.cmb_chunk.setEnabled(False)
        box.add(self.cmb_chunk)

        self.lbl_write_mode = QLabel("Un archivo por toma · sin costuras")
        self.lbl_write_mode.setWordWrap(True)
        self.lbl_write_mode.setStyleSheet(
            f"color: {theme.OK_BRIGHT}; font-size: 10px;")
        box.add(self.lbl_write_mode)
        return box

    def _toggle_duration_mode(self) -> None:
        limited = self.duration_stack.currentIndex() == 0
        self.duration_stack.setCurrentIndex(1 if limited else 0)
        self.btn_duration_mode.setText(
            "∞ Quitar el límite de tiempo" if limited
            else "⏱ Poner un límite de tiempo")
        self.refresh()

    def _on_chunk_toggled(self, checked: bool) -> None:
        self.cmb_chunk.setEnabled(checked)
        if checked:
            self.lbl_write_mode.setText(
                "⚠ Por bloques: cada corte pierde unos milisegundos de "
                "eventos en la EvCam. Queda registrado en el log.")
            self.lbl_write_mode.setStyleSheet(
                f"color: {theme.WARN}; font-size: 10px;")
        else:
            self.lbl_write_mode.setText("Un archivo por toma · sin costuras")
            self.lbl_write_mode.setStyleSheet(
                f"color: {theme.OK_BRIGHT}; font-size: 10px;")
        self.refresh()

    def chunk_duration_s(self):
        return self.cmb_chunk.currentData() if self.chk_chunks.isChecked() else None

    # --- columna central ------------------------------------------------
    def _build_center_column(self) -> QWidget:
        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        layout.addWidget(self._build_budget_panel())
        layout.addWidget(self._build_fire_button())
        layout.addWidget(self._build_monitor(), 1)
        return container

    def _build_budget_panel(self) -> QWidget:
        box = Section("Estado de la sesión y presupuesto de almacenamiento")
        grid = QGridLayout()
        grid.setHorizontalSpacing(18)
        grid.setVerticalSpacing(2)

        self.row_devices = ValueRow("Instrumentos listos", "0")
        self.row_rate = ValueRow("Caudal agregado", "0.0 MB/s")
        self.row_free = ValueRow("Espacio libre en destino", "—")
        self.row_autonomy = ValueRow("Autonomía a ese caudal", "—")
        self.row_needed = ValueRow("Necesario para la duración", "—")
        self.row_clock = ValueRow("Reloj", "T0 sin fijar")
        self.row_utc = ValueRow("Ancla UTC (PPS del GPS)", "sin anclar",
                                theme.WARN)
        self.row_alarms = ValueRow("Alarmas de la toma", "ninguna")
        self.row_volume = ValueRow("Volumen de destino", "—")
        self.row_largest = ValueRow("Archivo mayor previsto", "—")

        grid.addWidget(self.row_devices, 0, 0)
        grid.addWidget(self.row_clock, 0, 1)
        grid.addWidget(self.row_rate, 1, 0)
        grid.addWidget(self.row_utc, 1, 1)
        grid.addWidget(self.row_free, 2, 0)
        grid.addWidget(self.row_volume, 2, 1)
        grid.addWidget(self.row_autonomy, 3, 0)
        grid.addWidget(self.row_needed, 3, 1)
        grid.addWidget(self.row_largest, 4, 0)
        grid.addWidget(self.row_alarms, 4, 1)
        box.add_layout(grid)

        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.setFormat("en espera")
        box.add(self.progress)
        return box

    def _build_fire_button(self) -> QWidget:
        self.btn_fire = QPushButton("● INICIAR")
        self.btn_fire.setStyleSheet(theme.button_style("fire"))
        self.btn_fire.setMinimumHeight(74)
        self.btn_fire.setToolTip(
            "Arma y dispara todos los instrumentos con LED verde. "
            "Al presionarlo se convierte en DETENER.")
        self.btn_fire.clicked.connect(self._on_fire_clicked)

        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        layout.addWidget(self.btn_fire)

        # Las alarmas no se apagan solas: alguien tiene que haberlas visto.
        self.btn_ack = QPushButton("Reconocer alarmas")
        self.btn_ack.setVisible(False)
        self.btn_ack.setStyleSheet(theme.button_style("warn"))
        self.btn_ack.clicked.connect(self._acknowledge_alarms)
        layout.addWidget(self.btn_ack)
        return container

    def _acknowledge_alarms(self) -> None:
        for device_id in list(self.recorder.alarms):
            self.recorder.clear_alarm(device_id)
        self.console.log("Alarmas reconocidas por el operador.")
        LOG.info("Alarmas reconocidas por el operador.")
        self.refresh()

    def _build_monitor(self) -> QWidget:
        box = Section("Monitor de la sesión")
        self.lbl_phase = QLabel("Fase: inactivo")
        self.lbl_phase.setStyleSheet(theme.value_style(theme.ACCENT, 13))
        box.add(self.lbl_phase)

        row = QHBoxLayout()
        self.lbl_elapsed = QLabel("00:00:00")
        self.lbl_elapsed.setStyleSheet(theme.value_style(theme.OK_BRIGHT, 26))
        self.lbl_written = QLabel("0.0 GB escritos")
        self.lbl_written.setStyleSheet(theme.value_style(theme.TEXT_DIM, 13))
        row.addWidget(self.lbl_elapsed)
        row.addStretch(1)
        row.addWidget(self.lbl_written)
        box.add_layout(row)

        self.console = LogConsole(max_lines=600)
        self.console.log(f"{config.APP_NAME} {config.APP_VERSION} listo.")
        box.add(self.console, 1)
        return box

    # --- columna derecha ------------------------------------------------
    def _build_right_column(self) -> QWidget:
        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        for family in RIGHT_FAMILIES:
            device_type = REGISTRY.get(family)
            if device_type is None:
                continue
            card = FamilyCard(family, config.FAMILY_LABELS.get(
                family, device_type.label), align_right=True)
            card.include_changed.connect(self._on_include_changed)
            card.alias_changed.connect(self._on_alias_changed)
            card.open_tab_requested.connect(self.open_tab_requested.emit)
            self.cards[family] = card
            layout.addWidget(card)

        layout.addStretch(1)

        preview_box = Section("Vista previa (2 eventos · 2 Emergent)")
        self.previews = MiniPreviewGrid()
        preview_box.add(self.previews, 1)
        layout.addWidget(preview_box, 1)
        return container

    # ------------------------------------------------------------------
    # Conexiones
    # ------------------------------------------------------------------
    def _wire(self) -> None:
        self.manager.device_added.connect(self._on_device_added)
        self.manager.device_removed.connect(self._on_device_removed)
        self.manager.log.connect(self.console.log)

        self.recorder.log.connect(self.console.log)
        self.recorder.failed.connect(self._on_failed)
        self.recorder.phase_changed.connect(self._on_phase)
        self.recorder.progress.connect(self._on_progress)
        self.recorder.session_started.connect(self._on_session_started)
        self.recorder.session_finished.connect(self._on_session_finished)

        self.recorder.lock_changed.connect(self._on_lock_changed)
        self.recorder.device_alarm.connect(self._on_device_alarm)
        self.manager.locked_action.connect(
            lambda message: self.console.log(f"🔒 {message}"))
        self.manager.discovery_failed.connect(self._on_discovery_failed)

    def _on_discovery_failed(self, family: str, message: str) -> None:
        label = config.FAMILY_LABELS.get(family, family)
        self.console.log(
            f"<span style='color:{theme.DANGER}'>✖ {label}: {message}</span>")

    def _on_device_alarm(self, device_id: str, message: str) -> None:
        """Un instrumento se cayó grabando. No se deja pasar en silencio."""
        self.console.log(
            f"<span style='color:{theme.DANGER}'>⚠ ALARMA · {message}</span>")

    def _on_lock_changed(self, locked: bool) -> None:
        """Congela todo lo que podría alterar la toma en curso."""
        for widget in (self.btn_destination, self.txt_session,
                       self.chk_timestamp, self.btn_duration_mode,
                       self.spin_minutes, self.chk_chunks, self.cmb_chunk,
                       self.btn_benchmark):
            widget.setEnabled(not locked)
        if locked is False:
            self.cmb_chunk.setEnabled(self.chk_chunks.isChecked())
        for card in self.cards.values():
            card.set_locked(locked)

    # ------------------------------------------------------------------
    def _on_device_added(self, worker: DeviceWorker) -> None:
        card = self.cards.get(worker.family)
        if card is not None:
            card.add_worker(worker)
        cell = self.previews.assign(worker)
        if cell is not None:
            # Señal que cruza de hilo: se conecta a un método ligado de este
            # widget (no a un lambda) para que Qt la encole en el hilo de la
            # interfaz. Con un lambda se ejecutaría en el hilo del worker y
            # pintar desde ahí revienta.
            worker.preview_ready.connect(self._on_preview)
        self.console.log(f"{worker.label} agregado ({worker.family}).")

    def _on_device_removed(self, device_id: str) -> None:
        for card in self.cards.values():
            card.remove_worker(device_id)
        self.previews.release(device_id)

    def _on_preview(self, image) -> None:
        worker = self.sender()
        if worker is None:
            return
        cell = self.previews.cell_for(getattr(worker, "device_id", ""))
        if cell is not None:
            cell.set_image(image)

    def _on_alias_changed(self, device_id: str, alias: str) -> None:
        self.manager.rename(device_id, alias)
        self.refresh()

    def _on_include_changed(self, device_id: str, included: bool) -> None:
        worker = self.manager.get(device_id)
        if worker is not None:
            estado = "incluido en" if included else "excluido de"
            self.console.log(f"{worker.label} {estado} la toma de datos.")
        self.refresh()

    # ------------------------------------------------------------------
    # Destino
    # ------------------------------------------------------------------
    def _choose_destination(self) -> None:
        from PyQt6.QtWidgets import QFileDialog
        directory = QFileDialog.getExistingDirectory(
            self, "Carpeta base donde se volcarán las sesiones",
            str(self.sessions.base_dir))
        if not directory:
            return
        # Se valida antes de aceptarlo, no al disparar: un destino en FAT32 o
        # sin permisos tiene que aparecer ahora, con todo el tiempo del mundo
        # para cambiarlo.
        verdict = storage.validate_destination(directory, create=True)
        for message in verdict.messages:
            self.console.log(message)
            LOG.warn(f"[destino] {message}")
        if not verdict.ok:
            QMessageBox.critical(
                self, "Destino no utilizable",
                "\n".join(verdict.errors)
                + "\n\nElige otra carpeta antes de grabar.")
            return
        self.sessions.set_base_dir(directory)
        self.lbl_destination.setText(directory)
        info = verdict.info
        detail = (f" · {info.filesystem} · {info.free_gb:,.1f} GB libres"
                  if info else "")
        self.console.log(f"Destino de sesiones: {directory}{detail}")
        LOG.info(f"Destino de sesiones: {directory}{detail}")

    # ------------------------------------------------------------------
    # Disparo
    # ------------------------------------------------------------------
    def duration_s(self) -> Optional[float]:
        """Duración pedida. ``None`` = sin límite, y se ve en pantalla."""
        if self.duration_stack.currentIndex() == 0:
            return None
        return float(self.spin_minutes.value() * 60)

    def _on_fire_clicked(self) -> None:
        if self.recorder.recording:
            self.recorder.stop_all(reason="manual")
            return
        self.console.rule("DISPARO GLOBAL")
        self.recorder.fire(session_name=self.txt_session.text(),
                           duration_s=self.duration_s(),
                           auto_timestamp=self.chk_timestamp.isChecked(),
                           chunk_duration_s=self.chunk_duration_s())

    def _on_failed(self, message: str) -> None:
        self.console.log(f"<span style='color:{theme.DANGER}'>✖ {message}</span>")
        self._set_fire_mode(recording=False)

    def _on_phase(self, phase: str, detail: str) -> None:
        text = f"Fase: {phase}" + (f" · {detail}" if detail else "")
        self.lbl_phase.setText(text)
        recording = phase in ("disparo", "grabando")
        self._set_fire_mode(recording)
        if phase == "inactivo":
            self.progress.setValue(0)
            self.progress.setFormat("en espera")

    def _set_fire_mode(self, recording: bool) -> None:
        if recording:
            self.btn_fire.setText("■ DETENER")
            self.btn_fire.setStyleSheet(theme.button_style("stop"))
        else:
            self.btn_fire.setText("● INICIAR")
            self.btn_fire.setStyleSheet(theme.button_style("fire"))

    def _on_progress(self, elapsed: float, total) -> None:
        self.lbl_elapsed.setText(time.strftime("%H:%M:%S", time.gmtime(elapsed)))
        if total:
            fraction = max(0.0, min(1.0, elapsed / float(total)))
            self.progress.setValue(int(fraction * 100))
            remaining = max(0.0, float(total) - elapsed)
            self.progress.setFormat(
                f"{fraction * 100:.0f}% · quedan "
                f"{time.strftime('%H:%M:%S', time.gmtime(remaining))}")
        else:
            self.progress.setValue(0)
            self.progress.setFormat("sin límite de tiempo")

    def _on_session_started(self, payload: dict) -> None:
        self.console.log(f"Sesión: {payload.get('session_dir')}")
        self.console.log("Instrumentos grabando: "
                         + ", ".join(payload.get("devices", [])))

    def _on_session_finished(self, summary: dict) -> None:
        self.console.rule("SESIÓN CERRADA")
        self.console.log(f"Duración real: {summary.get('duracion_real_s')} s")
        if summary.get("session_dir"):
            self.console.log(f"Carpeta: {summary['session_dir']}")
        self.lbl_elapsed.setText("00:00:00")

    # ------------------------------------------------------------------
    # Refresco periódico
    # ------------------------------------------------------------------
    def refresh(self) -> None:
        for card in self.cards.values():
            card.refresh()

        report = self.recorder.budget(self.duration_s())
        total_devices = self.manager.count()
        self.row_devices.set_value(
            f"{report.devices} / {total_devices}",
            theme.OK_BRIGHT if report.devices else theme.TEXT_FAINT)
        self.row_rate.set_value(f"{report.total_mb_s:,.1f} MB/s")
        self.row_free.set_value(f"{report.free_gb:,.1f} GB")

        if report.seconds_until_full is None:
            self.row_autonomy.set_value("—")
        else:
            minutes = report.seconds_until_full / 60.0
            color = (theme.DANGER if minutes < 10 else
                     theme.WARN if minutes < 60 else theme.OK_BRIGHT)
            if minutes >= 120:
                self.row_autonomy.set_value(f"{minutes / 60:,.1f} h", color)
            else:
                self.row_autonomy.set_value(f"{minutes:,.1f} min", color)

        duration = self.duration_s()
        if duration and report.total_mb_s > 0:
            self.row_needed.set_value(
                f"{report.needed_gb:,.1f} GB",
                theme.OK_BRIGHT if report.ok else theme.DANGER)
        else:
            self.row_needed.set_value("sin límite" if not duration else "—")

        t0 = TIME_BASE.t0_ns
        self.row_clock.set_value(
            f"T0 = {t0}" if t0 else "T0 sin fijar",
            theme.OK_BRIGHT if t0 else theme.TEXT_FAINT)

        anchor = TIME_BASE.anchor
        if anchor is None:
            self.row_utc.set_value("sin anclar", theme.WARN)
        else:
            stamp = time.strftime("%H:%M:%S", time.gmtime(anchor.unix_ts))
            self.row_utc.set_value(f"{stamp} UTC · {anchor.source}",
                                   theme.OK_BRIGHT)

        info = storage.inspect_target(self.sessions.base_dir)
        filesystem = (info.filesystem or "?").lower()
        if filesystem in config.FILESYSTEMS_REJECTED:
            self.row_volume.set_value(f"{filesystem.upper()} · NO SIRVE",
                                      theme.DANGER)
        elif filesystem in config.FILESYSTEMS_WARNED:
            self.row_volume.set_value(f"{filesystem} · verifica el caudal",
                                      theme.WARN)
        elif filesystem:
            self.row_volume.set_value(f"{filesystem} · {info.mountpoint}",
                                      theme.TEXT_DIM)
        else:
            self.row_volume.set_value("—")

        if report.largest_file_bytes:
            limit = info.max_file_bytes
            color = (theme.DANGER if limit and report.largest_file_bytes > limit
                     else theme.TEXT_DIM)
            self.row_largest.set_value(
                f"{report.largest_file_bytes / 1e9:,.1f} GB · "
                f"{report.largest_file_device}", color)
        else:
            self.row_largest.set_value("sin límite de tiempo")

        alarms = self.recorder.alarms
        self.btn_ack.setVisible(bool(alarms))
        if alarms:
            self.row_alarms.set_value(f"⚠ {len(alarms)} · revisa el registro",
                                      theme.DANGER)
        else:
            self.row_alarms.set_value("ninguna", theme.TEXT_DIM)

        written = 0
        for worker in self.manager.all():
            stats = worker.last_stats
            disk = stats.get("disco") or {}
            written += float(disk.get("bytes_escritos", 0) or 0)
        if written:
            self.lbl_written.setText(f"{written / 1e9:,.2f} GB escritos")

    # ------------------------------------------------------------------
    def append_log(self, message: str) -> None:
        self.console.log(message)
