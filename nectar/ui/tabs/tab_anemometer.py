"""Pestaña 6 · Anemómetro ultrasónico de tiempo de vuelo.

El instrumento está en construcción (FPGA Arty S7 emitiendo pulsos a 32,8 kHz
y midiendo el tiempo de vuelo), así que todavía no se sabe con qué formato va
a entregar los datos. Esta pestaña deja armada toda la estructura —modular
aditiva, carpeta de guardado, frecuencia deseada, grabar y detener,
participación en el disparo global— sin inventar un protocolo que después no
calce con el firmware.

Para eso hay tres modos de ingesta, seleccionables:

``crudo``
    Vuelca los bytes tal como llegan. Es el modo seguro: sirva el formato que
    sirva, los datos quedan guardados y se pueden re-interpretar después.

``lineas``
    Espera líneas de texto ``t_ida_ns,t_vuelta_ns``, que es lo que suele
    emitir un prototipo en su primera versión. Con eso ya se calcula y se
    grafica temperatura y viento en vivo.

``binario``
    Paquetes de tamaño fijo. Cuando el firmware defina su estructura, lo
    único que hay que escribir es ``parse_packet()`` en el worker; todo lo
    demás ya funciona.

La física que se aplica cuando llegan los dos tiempos de vuelo::

    c = (d/2) · (1/t_ida + 1/t_vuelta)     -> temperatura, vía c = √(γ·R·T)
    v = (d/2) · (1/t_ida - 1/t_vuelta)     -> viento sobre el eje

La ventaja sobre la fibra óptica es que mide la temperatura del camino óptico
**sin inercia térmica**: no hay un sensor físico que tenga que llegar al
equilibrio con el aire.
"""

from __future__ import annotations

import time
from typing import Any, Dict, Optional

from PyQt6.QtWidgets import (QComboBox, QDoubleSpinBox, QGridLayout,
                             QHBoxLayout, QLabel, QVBoxLayout, QWidget)

from ... import config
from ...devices.anemometer.worker import MODES, temperature_to_sound_speed
from ...devices.base import DeviceWorker
from .. import theme
from ..widgets.common import LabeledControl, Section, SliderSpin, ValueRow
from ..widgets.plots import TimeSeriesPlot
from .base_family_tab import DevicePanel, FamilyTab


class AnemometerPanel(DevicePanel):
    """Panel de un anemómetro: derivados físicos y series en vivo."""

    def __init__(self, worker, parent: Optional[QWidget] = None) -> None:
        super().__init__(worker, parent)
        layout = self.body()

        top = QHBoxLayout()
        self.lbl_temp = QLabel("— °C")
        self.lbl_temp.setStyleSheet(theme.value_style(theme.OK_BRIGHT, 24))
        self.lbl_wind = QLabel("— m/s")
        self.lbl_wind.setStyleSheet(theme.value_style(theme.ACCENT_BRIGHT, 24))
        self.lbl_hz = QLabel("— Hz")
        self.lbl_hz.setStyleSheet(theme.value_style(theme.TEXT_DIM, 14))
        top.addWidget(QLabel("Temperatura:"))
        top.addWidget(self.lbl_temp)
        top.addSpacing(20)
        top.addWidget(QLabel("Viento:"))
        top.addWidget(self.lbl_wind)
        top.addStretch(1)
        top.addWidget(self.lbl_hz)
        layout.addLayout(top)

        self.plot = TimeSeriesPlot(y_label="valor", capacity=1200)
        self.plot.setMinimumHeight(200)
        self.plot.set_series(["temperatura (°C)", "viento (m/s)"])
        layout.addWidget(self.plot, 1)

        grid = QGridLayout()
        self.row_tof = ValueRow("Tiempos de vuelo", "—")
        self.row_mode = ValueRow("Modo de ingesta", "—")
        self.row_samples = ValueRow("Muestras", "0")
        self.row_flow = ValueRow("Caudal", "—")
        grid.addWidget(self.row_tof, 0, 0)
        grid.addWidget(self.row_mode, 0, 1)
        grid.addWidget(self.row_samples, 1, 0)
        grid.addWidget(self.row_flow, 1, 1)
        layout.addLayout(grid)

        self._t_start = time.perf_counter()
        self._last_plot = 0.0

    # ------------------------------------------------------------------
    def refresh_stats(self, stats: Dict[str, Any]) -> None:
        self.lbl_hz.setText(f"{stats.get('hz', 0):.0f} Hz "
                            f"(objetivo {stats.get('hz_objetivo', 0)})")
        self.row_mode.set_value(str(stats.get("modo", "—")))
        self.row_samples.set_value(f"{stats.get('muestras', 0):,}")
        self.row_flow.set_value(f"{stats.get('bytes_s', 0):,} B/s")

        temperature = stats.get("ultimo_temperatura_c")
        wind = stats.get("ultimo_viento_m_s")
        if temperature is not None:
            self.lbl_temp.setText(f"{temperature:.2f} °C")
        if wind is not None:
            self.lbl_wind.setText(f"{wind:+.2f} m/s")

        t_up = stats.get("ultimo_tof_ida_ns")
        t_down = stats.get("ultimo_tof_vuelta_ns")
        if t_up and t_down:
            self.row_tof.set_value(f"{t_up:,.0f} / {t_down:,.0f} ns")
        elif stats.get("ultimo_tof_ns"):
            self.row_tof.set_value(f"{stats['ultimo_tof_ns']:,.0f} ns")

        now = time.perf_counter()
        if now - self._last_plot >= 0.5 and temperature is not None:
            self._last_plot = now
            values = {"temperatura (°C)": temperature}
            if wind is not None:
                values["viento (m/s)"] = wind
            self.plot.append_many(now - self._t_start, values)


class AnemometerTab(FamilyTab):
    """Pestaña del anemómetro."""

    family = "anemometer"
    family_label = "Anemómetro"
    empty_message = ("No hay anemómetros conectados.\n\n"
                     "El instrumento real está en construcción. Pulsa "
                     "DETECTAR para ver los puertos serie disponibles y la "
                     "unidad simulada, que entrega tiempos de vuelo "
                     "coherentes con ~14 °C y ~1,5 m/s para poder probar "
                     "toda la cadena.")
    side_width: int = 320

    def create_panel(self, worker: DeviceWorker) -> DevicePanel:
        return AnemometerPanel(worker)

    # ------------------------------------------------------------------
    def build_side_panel(self) -> Optional[QWidget]:
        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        link = Section("Enlace serie")
        self.cmb_baud = QComboBox()
        self.cmb_baud.addItems(config.ANEMO_BAUD_OPTIONS)
        self.cmb_baud.setCurrentText(str(config.ANEMO_DEFAULT_BAUD))
        link.add(LabeledControl("Velocidad (bps)", self.cmb_baud,
                                "Debe coincidir con el firmware de la FPGA."))

        self.cmb_mode = QComboBox()
        self.cmb_mode.addItems(MODES)
        self.cmb_mode.setCurrentText("lineas")
        link.add(LabeledControl(
            "Modo de ingesta", self.cmb_mode,
            "crudo: guarda los bytes sin interpretar (siempre funciona).\n"
            "lineas: texto 't_ida_ns,t_vuelta_ns' terminado en salto de línea.\n"
            "binario: paquetes de tamaño fijo, pendiente de definir."))
        layout.addWidget(link)

        sampling = Section("Muestreo y geometría")
        low, high = config.ANEMO_HZ_RANGE
        self.ctl_hz = SliderSpin(low, min(high, 2000), config.ANEMO_DEFAULT_HZ)
        self.ctl_hz.value_changed.connect(self._on_hz)
        sampling.add(LabeledControl(
            "Frecuencia deseada (Hz)", self.ctl_hz,
            "El dossier apunta a >400 Hz de muestreo sobre pulsos acústicos "
            "de 32,8 kHz."))

        self.spin_path = QDoubleSpinBox()
        self.spin_path.setRange(0.01, 5.0)
        self.spin_path.setDecimals(3)
        self.spin_path.setSingleStep(0.005)
        self.spin_path.setValue(0.20)
        self.spin_path.setSuffix(" m")
        self.spin_path.valueChanged.connect(self._on_path)
        sampling.add(LabeledControl(
            "Separación entre transductores", self.spin_path,
            "Distancia del camino acústico. Entra directo en el cálculo de "
            "temperatura y viento, así que hay que medirla bien en el "
            "montaje."))
        layout.addWidget(sampling)

        physics = Section("Referencia física")
        note = QLabel(
            "c = √(γ·R·T) con γ=1,4 y R=287,05 J/(kg·K)\n\n"
            "c = (d/2)·(1/t_ida + 1/t_vuelta) → temperatura\n"
            "v = (d/2)·(1/t_ida − 1/t_vuelta) → viento en el eje")
        note.setWordWrap(True)
        note.setStyleSheet(f"color: {theme.TEXT_FAINT}; font-size: 10px; "
                           f"font-family: {theme.MONO};")
        physics.add(note)
        self.row_reference = physics.add_row("c a 14 °C", "—")
        self.row_expected = physics.add_row("t de vuelo esperado", "—")
        layout.addWidget(physics)

        summary = Section("Resumen")
        self.row_units = summary.add_row("Unidades conectadas", "0")
        self.row_rate = summary.add_row("Muestreo real", "0 Hz")
        layout.addWidget(summary)

        self._update_reference()
        return container

    # ------------------------------------------------------------------
    def _update_reference(self) -> None:
        speed = temperature_to_sound_speed(14.0)
        distance = self.spin_path.value()
        self.row_reference.set_value(f"{speed:.2f} m/s")
        self.row_expected.set_value(f"{distance / speed * 1e9:,.0f} ns")

    def _on_hz(self, value: int) -> None:
        for worker in self.workers():
            worker.set_target_hz(value)

    def _on_path(self, value: float) -> None:
        for worker in self.workers():
            worker.path_length_m = float(value)
        self._update_reference()

    def add_device(self, descriptor: Dict[str, Any]) -> None:
        descriptor = dict(descriptor)
        descriptor["baudrate"] = int(self.cmb_baud.currentText())
        descriptor["mode"] = self.cmb_mode.currentText()
        super().add_device(descriptor)

    def on_device_added(self, worker: DeviceWorker, panel: DevicePanel) -> None:
        worker.set_target_hz(self.ctl_hz.value())
        worker.path_length_m = float(self.spin_path.value())

    def on_refresh(self) -> None:
        workers = self.workers()
        self.row_units.set_value(str(len(workers)))
        total = sum(worker.last_stats.get("hz", 0) for worker in workers)
        self.row_rate.set_value(f"{total:,.0f} Hz")
