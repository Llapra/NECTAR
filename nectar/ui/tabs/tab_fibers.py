"""Pestaña 5 · Fibras: termometría óptica de alta velocidad.

Cómo está armado el sistema y por qué la pestaña se ve así
---------------------------------------------------------
Las fibras no se conectan al PC: se conectan al **acondicionador de señal**
(Opsens CoreSens), que es el que interroga ópticamente cada sonda y publica
las temperaturas ya convertidas. El PC habla con ese equipo por Ethernet, con
un canal de control en comandos tipo SCPI y otro canal por donde llegan las
muestras en binario.

Por eso la unidad que se "agrega" en esta pestaña es el acondicionador, y cada
fibra es un canal dentro de él. La lista desplegable de fibras numeradas
aparece dentro del panel del equipo, y se arma sola: el programa no pregunta
cuántas fibras hay, las descubre según qué (módulo, canal) están publicando.
Si hay más de un acondicionador, se agregan más equipos y la lógica aditiva
funciona igual en los dos niveles.

Sobre las frecuencias admitidas
-------------------------------
Del datasheet del equipo (IMP0160 CoreSens Rev3.8): cada módulo de doble canal
corre **hasta 1 kHz por canal**, y el tope depende del módulo instalado:

* ``WLX-2`` (interferometría de luz blanca): hasta **500 Hz**
* ``WLX-2S`` (la versión rápida de WLPI): hasta **1000 Hz**
* ``GSX-2`` (banda prohibida de semiconductor, sondas OTG): hasta **1000 Hz**

O sea que no se puede forzar cualquier frecuencia: el equipo acepta un
conjunto discreto y con un techo por módulo. La pestaña ofrece la lista
admitida y además deja escribir un valor a mano, pero lo valida: si se pide
más de lo que el módulo puede, se recorta al máximo legal y queda dicho en el
registro, en vez de mandar un comando que el equipo va a ignorar en silencio.

La cifra grande de cada fibra se refresca **una vez por segundo**, aunque se
esté midiendo a 1 kHz: a esa velocidad los dígitos serían ilegibles y no
aportan nada al operador. La grabación, en cambio, guarda todas las muestras.
"""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

import numpy as np
from PyQt6.QtWidgets import (QComboBox, QGridLayout, QHBoxLayout, QLabel,
                             QLineEdit, QPushButton, QSpinBox, QVBoxLayout,
                             QWidget)

from ... import config
from ...devices.base import DeviceWorker
from ...devices.fibers import worker as fiber_worker
from ...devices.fibers import protocol
from .. import theme
from ..widgets.common import (LabeledControl, Section, ValueRow)
from ..widgets.plots import BarPlot, TimeSeriesPlot, color_for
from .base_family_tab import DevicePanel, FamilyTab


class FiberChannelRow(QWidget):
    """Una fibra: número, temperatura y tasa real de captación."""

    def __init__(self, index: int, label: str,
                 parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(4, 1, 4, 1)
        layout.setSpacing(10)

        swatch = QLabel("■")
        swatch.setStyleSheet(f"color: {color_for(index)}; font-size: 14px;")

        self.name = QLabel(label)
        self.name.setStyleSheet(f"color: {theme.TEXT_DIM}; font-size: 11px; "
                                f"font-family: {theme.MONO};")

        self.value = QLabel("—")
        self.value.setStyleSheet(theme.value_style(theme.OK_BRIGHT, 20))
        self.value.setMinimumWidth(110)

        self.rate = QLabel("— Hz")
        self.rate.setStyleSheet(theme.value_style(theme.TEXT_FAINT, 11))

        self.extremes = QLabel("")
        self.extremes.setStyleSheet(theme.value_style(theme.TEXT_FAINT, 10))

        layout.addWidget(swatch)
        layout.addWidget(self.name)
        layout.addStretch(1)
        layout.addWidget(self.extremes)
        layout.addWidget(self.rate)
        layout.addWidget(self.value)

    def update_values(self, data: Dict[str, Any]) -> None:
        value = data.get("valor")
        if value is None or not np.isfinite(value):
            self.value.setText("—")
        else:
            self.value.setText(f"{value:.3f} °C")
        self.rate.setText(f"{data.get('hz', 0):.0f} Hz")
        low, high = data.get("min"), data.get("max")
        if low is not None and high is not None:
            self.extremes.setText(f"[{low:.2f} … {high:.2f}]")


class FiberPanel(DevicePanel):
    """Panel de un acondicionador: lista de fibras y curvas de temperatura."""

    def __init__(self, worker, parent: Optional[QWidget] = None) -> None:
        super().__init__(worker, parent)
        layout = self.body()

        top = QHBoxLayout()
        self.lbl_channels = QLabel("0 fibras")
        self.lbl_channels.setStyleSheet(theme.value_style(theme.ACCENT, 15))
        self.lbl_rate = QLabel("— Hz")
        self.lbl_rate.setStyleSheet(theme.value_style(theme.OK_BRIGHT, 15))
        self.lbl_flow = QLabel("— KB/s")
        self.lbl_flow.setStyleSheet(theme.value_style(theme.TEXT_DIM, 12))
        top.addWidget(self.lbl_channels)
        top.addWidget(self.lbl_rate)
        top.addStretch(1)
        top.addWidget(self.lbl_flow)
        layout.addLayout(top)

        # --- lista desplegable de fibras -------------------------------
        self.channel_box = Section("Fibras detectadas")
        layout.addWidget(self.channel_box)
        self._rows: Dict[int, FiberChannelRow] = {}

        # --- curvas y barras -------------------------------------------
        self.plot = TimeSeriesPlot(y_label="temperatura (°C)", capacity=1200)
        self.plot.setMinimumHeight(220)
        layout.addWidget(self.plot, 1)

        self.bars = BarPlot(y_label="°C")
        self.bars.setMinimumHeight(140)
        layout.addWidget(self.bars)

        grid = QGridLayout()
        self.row_mean = ValueRow("Temperatura media", "—")
        self.row_spread = ValueRow("Rango entre sondas", "—")
        self.row_struct = ValueRow("D_T (sondas vecinas)", "—")
        self.row_samples = ValueRow("Muestras acumuladas", "0")
        grid.addWidget(self.row_mean, 0, 0)
        grid.addWidget(self.row_spread, 0, 1)
        grid.addWidget(self.row_struct, 1, 0)
        grid.addWidget(self.row_samples, 1, 1)
        layout.addLayout(grid)

        self._last_numeric_update = 0.0
        self._t_start = time.perf_counter()

    # ------------------------------------------------------------------
    def refresh_stats(self, stats: Dict[str, Any]) -> None:
        channels: List[Dict[str, Any]] = stats.get("canales") or []

        self.lbl_channels.setText(f"{len(channels)} fibras")
        self.lbl_rate.setText(f"{stats.get('tasa_hz', 0)} Hz")
        self.lbl_flow.setText(f"{stats.get('kb_s', 0):.1f} KB/s")
        self.row_samples.set_value(f"{stats.get('muestras_totales', 0):,}")

        # Filas nuevas si aparecieron fibras.
        for data in channels:
            index = int(data["indice"])
            if index not in self._rows:
                row = FiberChannelRow(index, str(data.get("etiqueta",
                                                          f"Fibra {index + 1}")))
                self._rows[index] = row
                self.channel_box.add(row)
                self.plot.set_series([str(self._rows[key].name.text())
                                      for key in sorted(self._rows)])

        # La cifra visible se refresca una vez por segundo: a 1 kHz los
        # dígitos serían ilegibles y no le sirven a nadie.
        now = time.perf_counter()
        if now - self._last_numeric_update >= 1.0:
            self._last_numeric_update = now
            for data in channels:
                row = self._rows.get(int(data["indice"]))
                if row is not None:
                    row.update_values(data)

            values = {}
            labels, numbers = [], []
            for data in channels:
                row = self._rows.get(int(data["indice"]))
                if row is None:
                    continue
                value = data.get("valor")
                if value is None or not np.isfinite(value):
                    continue
                values[row.name.text()] = value
                labels.append(f"F{int(data['indice']) + 1}")
                numbers.append(value)
            if values:
                self.plot.append_many(now - self._t_start, values)
            if labels:
                self.bars.set_values(labels, numbers)

        mean = stats.get("temp_media_c")
        self.row_mean.set_value(f"{mean:.3f} °C" if mean is not None else "—")
        structure = stats.get("estructura") or {}
        if structure:
            self.row_spread.set_value(
                f"{structure.get('rango_C', 0):.3f} °C")
            lag1 = structure.get("D_T_lag1")
            if lag1 is not None:
                self.row_struct.set_value(f"{lag1:.5f} °C²")


class FibersTab(FamilyTab):
    """Pestaña de las fibras ópticas."""

    family = "fibers"
    family_label = "Fibras"
    empty_message = ("No hay acondicionadores de fibra conectados.\n\n"
                     "El equipo se declara por IP (no se autodescubre): pulsa "
                     "DETECTAR para ver la entrada del laboratorio y la "
                     "unidad simulada, ajusta la IP en el panel lateral si "
                     "hace falta, y AGREGAR FIBRAS.")
    side_width: int = 330

    def create_panel(self, worker: DeviceWorker) -> DevicePanel:
        return FiberPanel(worker)

    # ------------------------------------------------------------------
    def build_side_panel(self) -> Optional[QWidget]:
        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        connection = Section("Conexión del acondicionador")
        self.txt_ip = QLineEdit(config.FIBER_DEFAULT_IP)
        connection.add(LabeledControl(
            "Dirección IP", self.txt_ip,
            "IP del acondicionador en la red del laboratorio. Se aplica al "
            "próximo dispositivo que agregues."))

        ports = QHBoxLayout()
        self.spin_control = QSpinBox()
        self.spin_control.setRange(1, 65535)
        self.spin_control.setValue(config.FIBER_CONTROL_PORT)
        self.spin_data = QSpinBox()
        self.spin_data.setRange(1, 65535)
        self.spin_data.setValue(config.FIBER_DATA_PORT)
        ports.addWidget(QLabel("Control:"))
        ports.addWidget(self.spin_control)
        ports.addWidget(QLabel("Datos:"))
        ports.addWidget(self.spin_data)
        connection.add_layout(ports)
        layout.addWidget(connection)

        rates = Section("Frecuencia de captación")
        self.cmb_module = QComboBox()
        self.cmb_module.addItems(protocol.MODULE_TYPES)
        self.cmb_module.setCurrentText(config.FIBER_DEFAULT_MODULE)
        self.cmb_module.currentTextChanged.connect(self._on_module_changed)
        rates.add(LabeledControl(
            "Módulo instalado", self.cmb_module,
            "Determina el techo de frecuencia: WLX-2 hasta 500 Hz; "
            "WLX-2S y GSX-2 hasta 1000 Hz por canal."))

        self.cmb_rate = QComboBox()
        rates.add(LabeledControl("Frecuencia admitida", self.cmb_rate,
                                 "Valores que el equipo acepta para este módulo."))

        force = QHBoxLayout()
        self.spin_force = QSpinBox()
        self.spin_force.setRange(1, 5000)
        self.spin_force.setValue(1000)
        self.spin_force.setSuffix(" Hz")
        self.btn_force = QPushButton("Forzar")
        self.btn_force.setToolTip(
            "Intenta una frecuencia arbitraria. Si excede el máximo del "
            "módulo se recorta al valor legal más cercano y queda avisado "
            "en el registro.")
        self.btn_force.clicked.connect(self._on_force_rate)
        force.addWidget(self.spin_force)
        force.addWidget(self.btn_force)
        rates.add_layout(force)

        self.btn_apply_rate = QPushButton("Aplicar a todos los equipos")
        self.btn_apply_rate.setStyleSheet(theme.button_style("accent"))
        self.btn_apply_rate.clicked.connect(self._on_apply_rate)
        rates.add(self.btn_apply_rate)

        self.lbl_rates_doc = QLabel("")
        self.lbl_rates_doc.setWordWrap(True)
        self.lbl_rates_doc.setStyleSheet(
            f"color: {theme.TEXT_FAINT}; font-size: 10px;")
        rates.add(self.lbl_rates_doc)
        layout.addWidget(rates)

        summary = Section("Resumen")
        self.row_units = summary.add_row("Acondicionadores", "0")
        self.row_channels = summary.add_row("Fibras totales", "0")
        self.row_flow = summary.add_row("Caudal agregado", "0.0 KB/s")
        layout.addWidget(summary)

        self._on_module_changed(self.cmb_module.currentText())
        return container

    # ------------------------------------------------------------------
    def _on_module_changed(self, module: str) -> None:
        allowed = protocol.allowed_rates(module)
        self.cmb_rate.clear()
        for rate in allowed:
            self.cmb_rate.addItem(f"{rate} Hz", rate)
        if allowed:
            self.cmb_rate.setCurrentIndex(len(allowed) - 1)
        ceiling = config.FIBER_MODULE_MAX_HZ.get(module, 1000)
        self.lbl_rates_doc.setText(
            f"Módulo {module}: máximo {ceiling} Hz por canal. "
            f"Admitidas: {', '.join(str(rate) for rate in allowed)} Hz. "
            f"Hasta {config.FIBER_MAX_MODULES} módulos por chasis "
            f"({config.FIBER_MAX_MODULES * config.FIBER_CHANNELS_PER_MODULE} "
            f"canales).")
        for worker in self.workers():
            worker.set_module_type(module)

    def _on_apply_rate(self) -> None:
        rate = self.cmb_rate.currentData()
        if rate is None:
            return
        for worker in self.workers():
            worker.set_sampling_rate(int(rate))
        self.console.log(f"Frecuencia pedida: {rate} Hz "
                         f"({len(self.workers())} equipo(s)).")

    def _on_force_rate(self) -> None:
        requested = self.spin_force.value()
        module = self.cmb_module.currentText()
        valid, warning = protocol.clamp_rate(requested, module)
        if warning:
            self.console.log(warning)
        for worker in self.workers():
            worker.set_sampling_rate(requested)
        self.console.log(f"Frecuencia aplicada: {valid} Hz.")

    # ------------------------------------------------------------------
    def detect(self) -> None:
        """Sondea el acondicionador en la IP que esté escrita en el panel.

        Antes de esto, DETECTAR no tocaba la red: informaba "1 dispositivo
        detectado" mirando el archivo de configuración. Ahora abre una
        conexión TCP al canal de control, así que "detectado" significa que
        alguien contestó.
        """
        fiber_worker.set_target(
            ip=self.txt_ip.text().strip(),
            control_port=self.spin_control.value(),
            data_port=self.spin_data.value(),
            module_type=self.cmb_module.currentText())
        super().detect()

    def add_device(self, descriptor: Dict[str, Any]) -> None:
        """Toma la IP y los puertos del panel lateral al agregar el equipo."""
        descriptor = dict(descriptor)
        if not descriptor.get("simulated"):
            descriptor["ip"] = self.txt_ip.text().strip() or descriptor.get("ip")
        descriptor["control_port"] = self.spin_control.value()
        descriptor["data_port"] = self.spin_data.value()
        descriptor["module_type"] = self.cmb_module.currentText()
        super().add_device(descriptor)

    def on_device_added(self, worker: DeviceWorker, panel: DevicePanel) -> None:
        rate = self.cmb_rate.currentData()
        if rate:
            worker.set_sampling_rate(int(rate))

    def on_refresh(self) -> None:
        workers = self.workers()
        self.row_units.set_value(str(len(workers)))
        channels = sum(worker.channel_count() for worker in workers)
        self.row_channels.set_value(str(channels))
        flow = sum(worker.last_stats.get("kb_s", 0) for worker in workers)
        self.row_flow.set_value(f"{flow:,.1f} KB/s")
