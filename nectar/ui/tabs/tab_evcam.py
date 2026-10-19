"""Pestaña 2 · EvCam: cámaras neuromórficas de eventos.

Es la parte del programa anterior que ya funcionaba bien, así que se replica
su comportamiento y sus valores por defecto tal cual. Los cambios son dos, y
los dos fueron pedidos:

1. **Modular aditivo.** Antes había exactamente dos cámaras, cableadas en la
   interfaz como "CÁMARA 1 (Arriba)" y "CÁMARA 2 (Abajo)". Ahora cada cámara
   que se conecta agrega su propio panel a una columna desplazable, con su
   visualización a tamaño real. Se pueden tener cinco y se bajan con la rueda.

2. **Biases compartidos.** Un solo juego de voltajes para todas las cámaras de
   evento, que es lo correcto físicamente: si dos cámaras van a mirar el mismo
   blanco para comparar sus series, tienen que tener el mismo umbral de
   detección. El panel de biases vive en la columna lateral y se aplica a
   todas, en vivo.

Los rangos y los valores por defecto de los cuatro biases son los que estaban
calibrados en ``main_v14.1.py`` y no se cambiaron.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from PyQt6.QtWidgets import (QCheckBox, QComboBox, QGridLayout, QHBoxLayout,
                             QLabel, QPushButton, QSpinBox, QVBoxLayout,
                             QWidget)

from ... import config
from ...devices.base import DeviceWorker
from ...devices.event_cam.biases import SHARED_BIASES
from .. import theme
from ..widgets.common import LabeledControl, Section, SliderSpin, ValueRow
from ..widgets.image_view import ImageVisor
from .base_family_tab import DevicePanel, FamilyTab


class EventCamPanel(DevicePanel):
    """Panel de una cámara de eventos: visor a tamaño real y telemetría."""

    def __init__(self, worker, parent: Optional[QWidget] = None) -> None:
        super().__init__(worker, parent)
        layout = self.body()

        controls = QHBoxLayout()
        controls.setSpacing(8)

        self.cmb_sync = QComboBox()
        self.cmb_sync.addItems(config.EVENT_SYNC_MODES)
        self.cmb_sync.setCurrentText(worker.sync_mode)
        self.cmb_sync.setToolTip(
            "Sincronismo por hardware (cable Hirose). MASTER genera el pulso, "
            "SLAVE lo sigue. Es el único sincronismo exacto entre dos cámaras "
            "de eventos; el disparo de la pestaña 1 sincroniza por software.")
        self.cmb_sync.currentTextChanged.connect(worker.set_sync_mode)
        controls.addWidget(QLabel("Sync:"))
        controls.addWidget(self.cmb_sync)

        # El ROI ahora se programa en el sensor con la facilidad I_ROI del
        # HAL. Antes solo filtraba la vista previa: el archivo grabado seguía
        # teniendo el cuadro completo y el caudal no bajaba.
        self.chk_roi = QCheckBox("ROI en el sensor")
        self.chk_roi.setToolTip(
            "Programa el recorte en la cámara. Los píxeles de fuera dejan de "
            "generar eventos: baja la tasa, baja la ocupación del USB y el "
            ".raw contiene solo esta región.\n"
            "Arrastra el rectángulo rojo sobre la imagen y pulsa APLICAR.")
        self.chk_roi.toggled.connect(self._on_roi_toggled)
        controls.addWidget(self.chk_roi)

        self.btn_roi_apply = QPushButton("Aplicar ROI")
        self.btn_roi_apply.setEnabled(False)
        self.btn_roi_apply.setToolTip(
            "Manda el recorte al sensor. Se aplica reabriendo el flujo, así "
            "que hay un parpadeo de la vista previa.")
        self.btn_roi_apply.clicked.connect(self._apply_roi)
        controls.addWidget(self.btn_roi_apply)

        self.lbl_roi = QLabel("cuadro completo")
        self.lbl_roi.setStyleSheet(theme.value_style(theme.TEXT_FAINT, 10))
        controls.addWidget(self.lbl_roi)

        self.lbl_rate = QLabel("0.00 MEv/s")
        self.lbl_rate.setStyleSheet(theme.value_style(theme.OK_BRIGHT, 18))
        controls.addStretch(1)
        controls.addWidget(self.lbl_rate)
        layout.addLayout(controls)

        self.visor = ImageVisor(
            sensor_size=(worker.width, worker.height), with_roi=True,
            min_height=300)
        layout.addWidget(self.visor, 1)
        if not self.visor.supports_roi:
            self.chk_roi.setEnabled(False)
            self.chk_roi.setToolTip(
                "Hace falta pyqtgraph para dibujar el rectángulo de ROI.")

        grid = QGridLayout()
        grid.setHorizontalSpacing(16)
        self.row_events = ValueRow("Eventos totales", "0")
        self.row_delta = ValueRow("Δt integración", "—")
        self.row_mb = ValueRow("Caudal estimado", "—")
        self.row_block = ValueRow("Archivo en curso", "—", theme.WARN)
        grid.addWidget(self.row_events, 0, 0)
        grid.addWidget(self.row_delta, 0, 1)
        grid.addWidget(self.row_mb, 1, 0)
        grid.addWidget(self.row_block, 1, 1)
        layout.addLayout(grid)

    # ------------------------------------------------------------------
    def _on_roi_toggled(self, checked: bool) -> None:
        self.visor.set_roi_visible(checked)
        self.btn_roi_apply.setEnabled(checked)
        if not checked:
            self.worker.set_roi(False)

    def _apply_roi(self) -> None:
        x, y, width, height = self.visor.roi()
        # Se ajusta al rectángulo que el sensor puede aceptar y se devuelve al
        # visor, para que lo que se ve sea lo que de verdad quedó programado.
        snapped = self.worker.snap_roi(x, y, width, height)
        self.visor.set_roi(*snapped)
        self.worker.set_roi(True, snapped)

    def set_locked(self, locked: bool) -> None:
        super().set_locked(locked)
        self.chk_roi.setEnabled(not locked and self.visor.supports_roi)
        self.btn_roi_apply.setEnabled(
            not locked and self.chk_roi.isChecked())
        self.cmb_sync.setEnabled(not locked)

    # ------------------------------------------------------------------
    def refresh_stats(self, stats: Dict[str, Any]) -> None:
        self.lbl_rate.setText(f"{stats.get('tasa_mev_s', 0):.2f} MEv/s")
        self.row_events.set_value(f"{stats.get('eventos_totales', 0):,}")
        self.row_delta.set_value(f"{stats.get('delta_t_us', 0):,} µs")
        self.row_mb.set_value(f"{stats.get('mb_s', 0):,.1f} MB/s")
        archivo = stats.get("archivo") or "—"
        closed = stats.get("archivos_cerrados", 0)
        self.row_block.set_value(f"{archivo}  ({closed} cerrados)"
                                 if archivo != "—" else "—")

        # El estado del ROI se lee del worker, no de la casilla: lo que
        # importa es si el sensor lo aceptó, no si alguien lo pidió.
        roi = stats.get("roi")
        if not roi:
            self.lbl_roi.setText("cuadro completo")
            self.lbl_roi.setStyleSheet(theme.value_style(theme.TEXT_FAINT, 10))
        elif stats.get("roi_en_hardware"):
            fraction = stats.get("roi_fraccion", 1.0) * 100
            self.lbl_roi.setText(f"{roi[2]}x{roi[3]} @({roi[0]},{roi[1]}) · "
                                 f"{fraction:.0f}% del sensor")
            self.lbl_roi.setStyleSheet(theme.value_style(theme.OK_BRIGHT, 10))
        else:
            self.lbl_roi.setText("⚠ ROI NO aplicado en el sensor")
            self.lbl_roi.setStyleSheet(theme.value_style(theme.DANGER, 10))

        width = stats.get("ancho")
        height = stats.get("alto")
        if width and height:
            self.visor.set_sensor_size(width, height)

    def on_preview(self, image) -> None:
        self.visor.set_image(image)


class EvCamTab(FamilyTab):
    """Pestaña de las cámaras de eventos."""

    family = "event_cam"
    family_label = "EvCam"
    empty_message = ("No hay cámaras de eventos conectadas.\n\n"
                     "Pulsa DETECTAR para buscar en el bus USB y luego "
                     "AGREGAR EVCAM. Cada cámara que agregues aparece acá "
                     "con su propia visualización a tamaño real; todas "
                     "comparten el mismo juego de biases.")
    side_width: int = 310

    def create_panel(self, worker: DeviceWorker) -> DevicePanel:
        return EventCamPanel(worker)

    # ------------------------------------------------------------------
    def build_side_panel(self) -> Optional[QWidget]:
        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        layout.addWidget(self._build_bias_box())
        layout.addWidget(self._build_timing_box())
        layout.addWidget(self._build_model_box())
        return container

    def _build_model_box(self) -> QWidget:
        """Qué sensor hay y cuánto caudal puede llegar a generar.

        El presupuesto de disco de la pestaña 1 usa estas cifras mientras la
        cámara no haya medido su propia tasa. Tenerlas a la vista evita la
        confusión de asumir el caudal de un sensor que no es el que está
        conectado: la SilkyEvCam VGA por USB 3.0 no genera lo mismo que un
        IMX636 por Ethernet, ni de lejos.
        """
        box = Section("Modelo de sensor y caudal esperado")
        self.cmb_model = QComboBox()
        for name in config.EVENT_MODEL_RATES:
            self.cmb_model.addItem(name)
        self.cmb_model.setCurrentText(config.EVENT_DEFAULT_MODEL)
        self.cmb_model.currentTextChanged.connect(self._on_model_changed)
        box.add(self.cmb_model)

        self.row_model_res = box.add_row("Resolución", "—")
        self.row_model_link = box.add_row("Enlace", "—")
        self.row_model_typ = box.add_row("Caudal típico", "—")
        self.row_model_peak = box.add_row("Caudal de pico", "—")
        self._refresh_model_rows(config.EVENT_DEFAULT_MODEL)
        return box

    def _refresh_model_rows(self, model: str) -> None:
        profile = config.EVENT_MODEL_RATES.get(model, {})
        bytes_per_event = config.EVENT_BYTES_PER_EVENT
        self.row_model_res.set_value(
            f"{profile.get('ancho', '?')}x{profile.get('alto', '?')}")
        self.row_model_link.set_value(str(profile.get("enlace", "—")))
        typical = profile.get("mev_s_tipico", 0)
        peak = profile.get("mev_s_pico", 0)
        self.row_model_typ.set_value(
            f"{typical:,.0f} MEv/s · {typical * bytes_per_event:,.0f} MB/s"
            if typical else "—")
        self.row_model_peak.set_value(
            f"{peak:,.0f} MEv/s · {peak * bytes_per_event:,.0f} MB/s"
            if peak else "—", theme.WARN)

    def _on_model_changed(self, model: str) -> None:
        self._refresh_model_rows(model)
        for worker in self.workers():
            worker.model = model
        self.console.log(f"Modelo de sensor asumido: {model}")

    def _build_bias_box(self) -> QWidget:
        box = Section("Biases del sensor (compartidos)")
        warning = QLabel("⚠ Son voltajes de polarización: afectan al sensor, "
                         "no al software.")
        warning.setWordWrap(True)
        warning.setStyleSheet(f"color: {theme.WARN}; font-size: 10px;")
        box.add(warning)

        self._bias_controls: Dict[str, SliderSpin] = {}
        for name, default in config.EVENT_BIAS_DEFAULTS.items():
            low, high, tooltip = config.EVENT_BIAS_RANGES[name]
            control = SliderSpin(low, high, SHARED_BIASES.get(name))
            control.value_changed.connect(
                lambda value, key=name: self._on_bias_changed(key, value))
            box.add(LabeledControl(name, control, tooltip))
            self._bias_controls[name] = control

        row = QHBoxLayout()
        reset = QPushButton("↺ Volver a los valores calibrados")
        reset.clicked.connect(self._reset_biases)
        row.addWidget(reset)
        box.add_layout(row)
        return box

    def _build_timing_box(self) -> QWidget:
        box = Section("Tiempos (todas las cámaras)")

        low, high = config.EVENT_ACCUM_US_RANGE
        self.ctl_accum = SliderSpin(low, high, config.EVENT_ACCUM_US_DEFAULT,
                                    step=100)
        self.ctl_accum.value_changed.connect(self._on_accum_changed)
        box.add(LabeledControl(
            "Acumulación Δt (µs)", self.ctl_accum,
            "Ventana de integración del flujo de eventos. Más corta = más "
            "resolución temporal y más lotes por segundo; más larga = imagen "
            "más densa. Cambiarla reabre el iterador del SDK."))

        low, high = config.EVENT_DISPLAY_FPS_RANGE
        self.ctl_fps = SliderSpin(low, high, config.EVENT_DISPLAY_FPS_DEFAULT)
        self.ctl_fps.value_changed.connect(self._on_fps_changed)
        box.add(LabeledControl(
            "FPS de pantalla", self.ctl_fps,
            "Solo afecta a la visualización. La grabación no depende de esto: "
            "si la pantalla se atrasa se pierden cuadros de vista previa, "
            "nunca datos."))

        self.chk_verbose = QCheckBox("Volcar telemetría al registro")
        self.chk_verbose.toggled.connect(self._on_verbose)
        box.add(self.chk_verbose)

        self.row_cameras = box.add_row("Cámaras conectadas", "0")
        self.row_total_rate = box.add_row("Tasa agregada", "0.00 MEv/s")
        return box

    # ------------------------------------------------------------------
    def _on_bias_changed(self, name: str, value: int) -> None:
        SHARED_BIASES.set(name, value)

    def _reset_biases(self) -> None:
        SHARED_BIASES.reset()
        for name, control in self._bias_controls.items():
            control.set_value(SHARED_BIASES.get(name))
        self.console.log("Biases restaurados a los valores calibrados.")

    def _on_accum_changed(self, value: int) -> None:
        for worker in self.workers():
            worker.set_accumulation(value)

    def _on_fps_changed(self, value: int) -> None:
        for worker in self.workers():
            worker.set_display_fps(value)

    def _on_verbose(self, checked: bool) -> None:
        for worker in self.workers():
            worker.show_log = checked

    # ------------------------------------------------------------------
    def on_device_added(self, worker: DeviceWorker, panel: DevicePanel) -> None:
        """La cámara nueva nace con la configuración vigente de la pestaña."""
        worker.set_accumulation(self.ctl_accum.value())
        worker.set_display_fps(self.ctl_fps.value())
        worker.show_log = self.chk_verbose.isChecked()

    def on_refresh(self) -> None:
        workers = self.workers()
        self.row_cameras.set_value(str(len(workers)))
        total = sum(worker.last_stats.get("tasa_mev_s", 0) for worker in workers)
        self.row_total_rate.set_value(f"{total:.2f} MEv/s")
