"""Pestaña 3 · Emergent: cámaras de fotogramas por 25GigE.

Misma filosofía que la pestaña 2 —modular aditiva y con la misma disposición
de controles— pero con dos diferencias que vienen del hardware:

**Los controles se generan solos.** No están escritos en el código: al
conectar, el programa le pregunta a la cámara qué parámetros GenICam soporta,
y con qué mínimo, máximo y paso. Eso es lo que evita el problema del programa
Qt anterior, que tenía "1600x1096" y el tope de exposición escritos a mano
cuando el sensor es de 1604x1100. Si la cámara dice que el ancho va de 16 en
16, el control va de 16 en 16 y no se puede pedir un valor que la cámara
recorte en silencio.

**La grabación va directo al SSD.** El programa anterior reservaba RAM,
grababa la ráfaga completa ahí y volcaba al terminar; con 1,67 GB/s por cámara
eso limita la toma a unos 75 segundos y obliga a esperar el volcado. Acá el
anillo en RAM es solo un amortiguador de unos cientos de megabytes y un hilo
escritor vuelca en continuo al NVMe. La consecuencia práctica: la duración de
la toma la limita el tamaño del SSD, no la RAM, y entre dos tomas no hay que
esperar nada.

Los parámetros que cambian el tamaño del frame (ROI, formato de píxel)
invalidan los buffers, así que se aplican parando y volviendo a montar el
stream. Eso queda avisado en el propio control y está bloqueado mientras se
graba.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from PyQt6.QtWidgets import (QCheckBox, QComboBox, QGridLayout, QHBoxLayout,
                             QLabel, QPushButton, QVBoxLayout, QWidget)

from ...devices.base import DeviceWorker
from .. import theme
from ..widgets.common import (LabeledControl, Placeholder, ScrollColumn,
                              Section, SliderSpin, ValueRow)
from ..widgets.image_view import ImageVisor
from .base_family_tab import DevicePanel, FamilyTab


class EmergentPanel(DevicePanel):
    """Panel de una cámara Emergent: visor y estado del enlace y del disco."""

    def __init__(self, worker, parent: Optional[QWidget] = None) -> None:
        super().__init__(worker, parent)
        layout = self.body()

        top = QHBoxLayout()
        self.lbl_fps = QLabel("0.0 fps")
        self.lbl_fps.setStyleSheet(theme.value_style(theme.OK_BRIGHT, 18))

        # ROI arrastrable sobre la imagen, mapeado a los parámetros GenICam
        # del sensor con ajuste por incrementos. Antes esta cámara solo podía
        # recortarse escribiendo números en los controles de la derecha.
        self.chk_roi = QCheckBox("ROI en el sensor")
        self.chk_roi.setToolTip(
            "Dibuja el recorte sobre la imagen. Se traduce a OffsetX/OffsetY/"
            "Width/Height ajustados a los incrementos que la propia cámara "
            "declara, y se aplica remontando el stream.")
        self.chk_roi.toggled.connect(self._on_roi_toggled)
        self.btn_roi_apply = QPushButton("Aplicar")
        self.btn_roi_apply.setEnabled(False)
        self.btn_roi_apply.clicked.connect(self._apply_roi)
        self.btn_roi_full = QPushButton("Cuadro completo")
        self.btn_roi_full.clicked.connect(self._full_frame)

        self.lbl_geometry = QLabel("—")
        self.lbl_geometry.setStyleSheet(theme.value_style(theme.TEXT_DIM, 12))
        top.addWidget(self.lbl_fps)
        top.addWidget(self.chk_roi)
        top.addWidget(self.btn_roi_apply)
        top.addWidget(self.btn_roi_full)
        top.addStretch(1)
        top.addWidget(self.lbl_geometry)
        layout.addLayout(top)

        self.visor = ImageVisor(sensor_size=(1604, 1100), with_roi=True,
                                min_height=320)
        layout.addWidget(self.visor, 1)
        if not self.visor.supports_roi:
            self.chk_roi.setEnabled(False)
            self.chk_roi.setToolTip(
                "Hace falta pyqtgraph para dibujar el rectángulo de ROI.")

        grid = QGridLayout()
        grid.setHorizontalSpacing(16)
        self.row_received = ValueRow("Frames recibidos", "0")
        self.row_lost_net = ValueRow("Perdidos en el enlace", "0")
        self.row_lost_disk = ValueRow("Perdidos por disco", "0")
        self.row_rate = ValueRow("Caudal", "—")
        self.row_temp = ValueRow("Temperatura del sensor", "—")
        self.row_ring = ValueRow("Anillo de escritura", "—")
        self.row_file = ValueRow("Archivo de la toma", "—", theme.WARN)
        grid.addWidget(self.row_received, 0, 0)
        grid.addWidget(self.row_rate, 0, 1)
        grid.addWidget(self.row_lost_net, 1, 0)
        grid.addWidget(self.row_temp, 1, 1)
        grid.addWidget(self.row_lost_disk, 2, 0)
        grid.addWidget(self.row_ring, 2, 1)
        grid.addWidget(self.row_file, 3, 0, 1, 2)
        layout.addLayout(grid)

    # ------------------------------------------------------------------
    def _on_roi_toggled(self, checked: bool) -> None:
        self.visor.set_roi_visible(checked)
        self.btn_roi_apply.setEnabled(checked)

    def _apply_roi(self) -> None:
        geometry = self.worker.roi_geometry()
        x, y, width, height = self.visor.roi()
        if geometry is not None:
            # El rectángulo dibujado se lleva al rectángulo válido más cercano
            # ANTES de mandarlo, y se devuelve al visor: lo que se ve es lo
            # que la cámara va a aceptar, no lo que se pidió.
            x, y, width, height = geometry.snap_rect(x, y, width, height)
            self.visor.set_roi(x, y, width, height)
        self.worker.set_roi(x, y, width, height)

    def _full_frame(self) -> None:
        self.chk_roi.setChecked(False)
        self.worker.clear_roi()

    def set_locked(self, locked: bool) -> None:
        super().set_locked(locked)
        for widget in (self.chk_roi, self.btn_roi_full):
            widget.setEnabled(not locked and self.visor.supports_roi)
        self.btn_roi_apply.setEnabled(not locked and self.chk_roi.isChecked())

    # ------------------------------------------------------------------
    def refresh_stats(self, stats: Dict[str, Any]) -> None:
        self.lbl_fps.setText(f"{stats.get('fps', 0):.1f} fps")
        width, height = stats.get("ancho", 0), stats.get("alto", 0)
        if width and height:
            self.lbl_geometry.setText(
                f"{width}x{height} · {stats.get('formato', '')} · "
                f"{stats.get('frame_mb', 0):.2f} MB/frame")
            self.visor.set_sensor_size(width, height)

        self.row_received.set_value(f"{stats.get('frames_recibidos', 0):,}")

        lost_net = stats.get("perdidos_red", 0)
        self.row_lost_net.set_value(
            f"{lost_net:,}", theme.DANGER if lost_net else theme.OK_BRIGHT)
        lost_disk = stats.get("perdidos_disco", 0)
        self.row_lost_disk.set_value(
            f"{lost_disk:,}", theme.DANGER if lost_disk else theme.OK_BRIGHT)

        self.row_rate.set_value(f"{stats.get('mb_s', 0):,.0f} MB/s")
        temperature = stats.get("temperatura_c")
        self.row_temp.set_value(
            f"{temperature} °C" if temperature is not None else "—",
            theme.WARN if (temperature or 0) > 60 else theme.CONSOLE_TEXT)

        roi = stats.get("roi")
        if roi and stats.get("roi_recortado"):
            self.lbl_geometry.setText(
                self.lbl_geometry.text()
                + f" · ROI {roi[2]}x{roi[3]} @({roi[0]},{roi[1]})")

        disk = stats.get("disco") or {}
        if disk:
            archivo = stats.get("archivo") or "—"
            self.row_ring.set_value(
                f"{disk.get('ocupacion_maxima_anillo', 0)} slots pico · "
                f"{disk.get('caudal_ultimo_mb_s', 0):,.0f} MB/s al SSD · "
                f"peor write {disk.get('peor_write_ms', 0)} ms")
            self.row_file.set_value(
                f"{archivo} · {stats.get('gb_archivo', 0):,.2f} GB"
                if archivo != "—" else "—")
        else:
            self.row_ring.set_value("—")
            self.row_file.set_value("—")
        if stats.get("fallo_escritura"):
            self.row_file.set_value(f"⚠ {stats['fallo_escritura']}",
                                    theme.DANGER)

    def on_preview(self, image) -> None:
        self.visor.set_image(image)
        # Contrapresión: el worker no publica el frame siguiente hasta que la
        # interfaz acusa recibo de este. Sin este acuse la vista previa se
        # congelaría después del primer cuadro (la grabación no se ve
        # afectada, pero parecería que la cámara se colgó).
        self.worker.hub.acknowledge()


class EmergentTab(FamilyTab):
    """Pestaña de las cámaras Emergent."""

    family = "emergent"
    family_label = "Emergent"
    empty_message = ("No hay cámaras Emergent conectadas.\n\n"
                     "Pulsa DETECTAR para enumerar la red 25GigE y luego "
                     "AGREGAR EMERGENT.\n\n"
                     "Si no aparece ninguna, el motivo se muestra en el "
                     "registro de esta pestaña: sin eSDK, sin cámaras en la "
                     "red, o sin permisos. Para ensayar sin hardware: "
                     "python main.py --demo")
    side_width: int = 330

    #: Atributos de clase, no de instancia: ``build_side_panel()`` corre
    #: durante la construcción de la base y ya los necesita. Definirlos acá
    #: evita tener que tocarlos antes de que QWidget esté inicializado, que en
    #: PyQt es una fuente clásica de errores.
    _param_widgets: Dict[str, QWidget] = {}
    _active_worker: Optional[DeviceWorker] = None

    def create_panel(self, worker: DeviceWorker) -> DevicePanel:
        return EmergentPanel(worker)

    # ------------------------------------------------------------------
    def build_side_panel(self) -> Optional[QWidget]:
        self._param_widgets = {}
        self._active_worker = None

        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        selector = Section("Cámara activa")
        self.cmb_active = QComboBox()
        self.cmb_active.currentIndexChanged.connect(self._on_active_changed)
        selector.add(self.cmb_active)
        self.chk_apply_all = QCheckBox("Aplicar los cambios a todas")
        self.chk_apply_all.setToolTip(
            "Útil cuando dos cámaras miran el mismo blanco y deben tener la "
            "misma exposición y ganancia.")
        selector.add(self.chk_apply_all)
        layout.addWidget(selector)

        params = Section("Parámetros de la cámara")
        note = QLabel("Los rangos y el paso los reporta la propia cámara.")
        note.setWordWrap(True)
        note.setStyleSheet(f"color: {theme.TEXT_FAINT}; font-size: 10px; "
                           f"font-style: italic;")
        params.add(note)
        self.param_column = ScrollColumn(spacing=4)
        self.param_placeholder = Placeholder(
            "Conecta una cámara para que aparezcan sus controles.")
        self.param_column.add_widget(self.param_placeholder)
        params.add(self.param_column, 1)
        layout.addWidget(params, 1)

        preview = Section("Vista previa")
        self.ctl_preview_step = SliderSpin(1, 8, 2)
        self.ctl_preview_step.value_changed.connect(self._on_preview_step)
        preview.add(LabeledControl(
            "Decimación (1 de cada N píxeles)", self.ctl_preview_step,
            "Desempaquetar 1604x1100 en 12 bits cuesta milisegundos. Decimar "
            "antes de convertir es lo que mantiene la vista previa barata; no "
            "afecta a lo que se graba."))
        self.chk_contrast = QCheckBox("Estirar contraste (percentiles 1–99)")
        self.chk_contrast.setChecked(True)
        self.chk_contrast.toggled.connect(self._on_contrast)
        preview.add(self.chk_contrast)
        self.row_total = preview.add_row("Cámaras conectadas", "0")
        self.row_agg = preview.add_row("Caudal agregado", "0 MB/s")
        layout.addWidget(preview)
        return container

    # ------------------------------------------------------------------
    def on_device_added(self, worker: DeviceWorker, panel: DevicePanel) -> None:
        worker.preview_step = self.ctl_preview_step.value()
        worker.auto_contrast = self.chk_contrast.isChecked()
        worker.info_ready.connect(self._on_info)
        self._refresh_active_combo()

    def _on_device_removed(self, device_id: str) -> None:  # type: ignore[override]
        super()._on_device_removed(device_id)
        self._refresh_active_combo()

    def _refresh_active_combo(self) -> None:
        workers = self.workers()
        current = self.cmb_active.currentText()
        self.cmb_active.blockSignals(True)
        self.cmb_active.clear()
        for worker in workers:
            self.cmb_active.addItem(worker.label)
        if current:
            index = self.cmb_active.findText(current)
            if index >= 0:
                self.cmb_active.setCurrentIndex(index)
        self.cmb_active.blockSignals(False)
        self._on_active_changed(self.cmb_active.currentIndex())

    def _on_active_changed(self, index: int) -> None:
        workers = self.workers()
        if 0 <= index < len(workers):
            self._active_worker = workers[index]
            self._rebuild_param_controls(self._active_worker.snapshot_params())
        else:
            self._active_worker = None
            self._rebuild_param_controls([])

    def _on_info(self, payload: Dict[str, Any]) -> None:
        worker = self.sender()
        if worker is self._active_worker and payload.get("parametros"):
            self._rebuild_param_controls(payload["parametros"])
        info = payload.get("informacion") or {}
        if info:
            self.console.log("Equipo: " + " · ".join(
                f"{key}={value}" for key, value in info.items()))

    # ------------------------------------------------------------------
    def _rebuild_param_controls(self, entries: List[Dict[str, Any]]) -> None:
        """Genera los controles a partir de lo que reportó la cámara."""
        self.param_column.clear()
        self._param_widgets = {}
        if not entries:
            self.param_placeholder = Placeholder(
                "Conecta una cámara para que aparezcan sus controles.")
            self.param_column.add_widget(self.param_placeholder)
            return

        groups: Dict[str, List[Dict[str, Any]]] = {}
        for entry in entries:
            groups.setdefault(entry.get("group", "General"), []).append(entry)

        for group, items in groups.items():
            box = Section(group)
            for entry in items:
                widget = self._build_param_widget(entry)
                if widget is None:
                    continue
                label = entry["label"]
                if entry.get("unit"):
                    label += f" ({entry['unit']})"
                if entry.get("restart"):
                    label += "  ⟳"
                tooltip = entry.get("tooltip", "")
                if entry.get("restart"):
                    tooltip += ("\n\nCambiarlo altera el tamaño del frame: el "
                                "stream se detiene, se aplica y se vuelve a "
                                "montar. Bloqueado mientras se graba.")
                box.add(LabeledControl(label, widget, tooltip))
                self._param_widgets[entry["name"]] = widget
            self.param_column.add_widget(box)

    def _build_param_widget(self, entry: Dict[str, Any]) -> Optional[QWidget]:
        kind = entry.get("kind")
        name = entry["name"]

        if kind == "uint32":
            minimum = int(entry.get("minimum", 0))
            maximum = int(entry.get("maximum", max(minimum + 1, 1)))
            step = int(entry.get("increment", 1) or 1)
            value = int(entry.get("value", minimum))
            control = SliderSpin(minimum, maximum, value, step=step)
            control.value_changed.connect(
                lambda new_value, key=name: self._apply_param(key, new_value))
            return control

        if kind == "bool":
            check = QCheckBox("activado")
            check.setChecked(bool(entry.get("value")))
            check.toggled.connect(
                lambda checked, key=name: self._apply_param(key, checked))
            return check

        if kind == "enum":
            combo = QComboBox()
            options = entry.get("options") or []
            combo.addItems([str(option) for option in options])
            current = str(entry.get("value", ""))
            index = combo.findText(current)
            if index >= 0:
                combo.setCurrentIndex(index)
            combo.currentTextChanged.connect(
                lambda text, key=name: self._apply_param(key, text))
            return combo

        if kind == "int32":
            label = QLabel(str(entry.get("value", "—")))
            label.setStyleSheet(theme.value_style(theme.CONSOLE_TEXT, 12))
            return label

        return None

    def _apply_param(self, name: str, value: Any) -> None:
        targets = (self.workers() if self.chk_apply_all.isChecked()
                   else ([self._active_worker] if self._active_worker else []))
        for worker in targets:
            if worker is None:
                continue
            worker.set_param(name, value)

    def _on_preview_step(self, value: int) -> None:
        for worker in self.workers():
            worker.preview_step = int(value)

    def _on_contrast(self, checked: bool) -> None:
        for worker in self.workers():
            worker.auto_contrast = bool(checked)

    # ------------------------------------------------------------------
    def on_refresh(self) -> None:
        workers = self.workers()
        self.row_total.set_value(str(len(workers)))
        total = sum(worker.estimated_mb_s() for worker in workers)
        self.row_agg.set_value(f"{total:,.0f} MB/s")
        # Los parámetros no se tocan durante la grabación: la geometría del
        # anillo está dimensionada para un tamaño de frame concreto.
        recording = any(worker.is_recording for worker in workers)
        for widget in self._param_widgets.values():
            widget.setEnabled(not recording)
