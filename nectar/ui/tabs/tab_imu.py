"""Pestaña 7 · IMU-GPS: vibración, telemetría inercial y ancla de tiempo.

Es la pestaña de ``main_v14.1.py`` con la lógica aditiva de las demás: antes
había exactamente dos ESP32 cableados en la interfaz ("ESP32-1" y "ESP32-2");
ahora cada módulo que se conecta agrega su propio recuadro, con lo que ese
módulo sabe hacer: frecuencia de paquetes, caudal, errores de sincronismo,
contador de PPS, coordenada del GPS y la aceleración en vivo.

El papel doble de este instrumento
----------------------------------
Mide vibración —que contamina el wander de las manchas y hay que poder
descontarla— pero además **es el reloj absoluto de todo NECTAR**. Cuando llega
la primera trama de coordenadas válida junto a un PPS, el worker ancla la hora
UTC en la base de tiempo global, y desde ese instante las cámaras, las fibras
y el anemómetro pueden datarse en UTC con el mismo offset. Por eso el recuadro
del ancla es lo primero que hay que mirar antes de una toma: sin ancla, el
dataset queda con tiempos relativos a T0 y no se puede cruzar con la
telemetría del telescopio.
"""

from __future__ import annotations

import time
from typing import Any, Dict, Optional

from PyQt6.QtWidgets import (QCheckBox, QComboBox, QGridLayout, QHBoxLayout,
                             QLabel, QPushButton, QVBoxLayout, QWidget)

from ... import config
from ...core.time_sync import TIME_BASE
from ...devices.base import DeviceWorker
from ...devices.imu.worker import list_serial_ports
from .. import theme
from ..widgets.common import LabeledControl, Section, ValueRow
from ..widgets.plots import TimeSeriesPlot
from .base_family_tab import DevicePanel, FamilyTab


class ImuPanel(DevicePanel):
    """Recuadro de un ESP32: lo que ese módulo está entregando."""

    def __init__(self, worker, parent: Optional[QWidget] = None) -> None:
        super().__init__(worker, parent)
        layout = self.body()

        top = QHBoxLayout()
        self.lbl_hz = QLabel("0.0 Hz")
        self.lbl_hz.setStyleSheet(theme.value_style(theme.OK_BRIGHT, 28))
        column = QVBoxLayout()
        self.row_bps = ValueRow("Caudal", "0 B/s", size=11)
        self.row_errors = ValueRow("Errores de sincronismo", "0",
                                   theme.DANGER, size=11)
        self.row_packets = ValueRow("Paquetes", "0", size=11)
        column.addWidget(self.row_bps)
        column.addWidget(self.row_errors)
        column.addWidget(self.row_packets)

        pps_column = QVBoxLayout()
        self.lbl_pps = QLabel("⏱ PPS: 0")
        self.lbl_pps.setStyleSheet(theme.value_style(theme.WARN, 15))
        self.lbl_utc = QLabel("UTC: sin anclar")
        self.lbl_utc.setStyleSheet(theme.value_style(theme.TEXT_FAINT, 11))
        pps_column.addWidget(self.lbl_pps)
        pps_column.addWidget(self.lbl_utc)

        top.addWidget(self.lbl_hz)
        top.addSpacing(14)
        top.addLayout(column)
        top.addStretch(1)
        top.addLayout(pps_column)
        layout.addLayout(top)

        # --- GPS --------------------------------------------------------
        gps = QHBoxLayout()
        self.btn_fix = QPushButton("📍 FIJAR COORDENADA")
        self.btn_fix.setStyleSheet(theme.button_style("ok"))
        self.btn_fix.clicked.connect(self._fix_coordinate)
        self.lbl_coord = QLabel("Lat: --.------ | Lon: --.------")
        self.lbl_coord.setStyleSheet(theme.value_style("#ffd700", 12))
        gps.addWidget(self.btn_fix)
        gps.addWidget(self.lbl_coord)
        gps.addStretch(1)
        layout.addLayout(gps)

        # --- inercial en vivo -------------------------------------------
        grid = QGridLayout()
        grid.setHorizontalSpacing(16)
        self.row_accel = ValueRow("Aceleración (g)", "—")
        self.row_gyro = ValueRow("Giróscopo (°/s)", "—")
        self.row_vib = ValueRow("Vibración RMS", "—", theme.ACCENT_BRIGHT)
        self.row_mode = ValueRow("Modo", "—")
        grid.addWidget(self.row_accel, 0, 0)
        grid.addWidget(self.row_gyro, 0, 1)
        grid.addWidget(self.row_vib, 1, 0)
        grid.addWidget(self.row_mode, 1, 1)
        layout.addLayout(grid)

        self.plot = TimeSeriesPlot(y_label="g", capacity=900)
        self.plot.setMinimumHeight(170)
        self.plot.set_series(["ax", "ay", "az"])
        layout.addWidget(self.plot, 1)

        self._fixed_lat: Optional[float] = None
        self._fixed_lon: Optional[float] = None
        self._t_start = time.perf_counter()
        self._last_plot = 0.0

    # ------------------------------------------------------------------
    def _fix_coordinate(self) -> None:
        latitude = self.worker.last_lat
        longitude = self.worker.last_lon
        if latitude is None or longitude is None:
            self.lbl_coord.setText("Esperando trama 0xBBDD del GPS…")
            return
        self._fixed_lat, self._fixed_lon = latitude, longitude
        self.lbl_coord.setText(f"FIJADA · Lat: {latitude:.6f} | "
                               f"Lon: {longitude:.6f}")

    # ------------------------------------------------------------------
    def refresh_stats(self, stats: Dict[str, Any]) -> None:
        self.lbl_hz.setText(f"{stats.get('hz', 0):.1f} Hz")
        self.row_bps.set_value(f"{stats.get('bytes_s', 0):,} B/s")
        errors = stats.get("errores_sincronismo", 0)
        self.row_errors.set_value(
            f"{errors:,}", theme.DANGER if errors else theme.OK_BRIGHT)
        self.row_packets.set_value(f"{stats.get('paquetes', 0):,}")

        self.lbl_pps.setText(f"⏱ PPS: {stats.get('pps', 0)}")
        color = theme.OK_BRIGHT if stats.get("pps", 0) % 2 == 0 else theme.WARN
        self.lbl_pps.setStyleSheet(theme.value_style(color, 15))

        if stats.get("utc_anclado"):
            anchor = TIME_BASE.anchor
            if anchor is not None:
                stamp = time.strftime("%H:%M:%S", time.gmtime(anchor.unix_ts))
                self.lbl_utc.setText(f"UTC anclado: {stamp}")
                self.lbl_utc.setStyleSheet(
                    theme.value_style(theme.OK_BRIGHT, 11))
        elif stats.get("esperando_pps"):
            self.lbl_utc.setText("esperando PPS para grabar…")
            self.lbl_utc.setStyleSheet(theme.value_style(theme.WARN, 11))

        if self._fixed_lat is None:
            latitude, longitude = stats.get("lat"), stats.get("lon")
            if latitude is not None and longitude is not None:
                self.lbl_coord.setText(f"Lat: {latitude:.6f} | "
                                       f"Lon: {longitude:.6f}")

        ax = stats.get("ax_g", 0.0)
        ay = stats.get("ay_g", 0.0)
        az = stats.get("az_g", 0.0)
        self.row_accel.set_value(f"{ax:+.3f}  {ay:+.3f}  {az:+.3f}")
        self.row_gyro.set_value(
            f"{stats.get('gx_dps', 0):+.2f}  {stats.get('gy_dps', 0):+.2f}  "
            f"{stats.get('gz_dps', 0):+.2f}")
        self.row_vib.set_value(f"{stats.get('vibracion_rms_g', 0):.5f} g")
        self.row_mode.set_value("RAW (sin parsear)" if stats.get("modo_raw")
                                else "parseado por paquete")

        now = time.perf_counter()
        if now - self._last_plot >= 0.25:
            self._last_plot = now
            self.plot.append_many(now - self._t_start,
                                  {"ax": ax, "ay": ay, "az": az})


class ImuTab(FamilyTab):
    """Pestaña del IMU / GPS."""

    family = "imu"
    family_label = "IMU-GPS"
    empty_message = ("No hay módulos IMU/GPS conectados.\n\n"
                     "Pulsa DETECTAR para listar los puertos serie y luego "
                     "AGREGAR IMU-GPS. Cada módulo agrega su propio recuadro "
                     "con su frecuencia, su contador de PPS y su coordenada.")
    side_width: int = 320

    def create_panel(self, worker: DeviceWorker) -> DevicePanel:
        return ImuPanel(worker)

    # ------------------------------------------------------------------
    def build_side_panel(self) -> Optional[QWidget]:
        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        link = Section("Enlace serie")
        self.cmb_baud = QComboBox()
        self.cmb_baud.addItems(config.IMU_BAUD_OPTIONS)
        self.cmb_baud.setCurrentText(str(config.IMU_DEFAULT_BAUD))
        link.add(LabeledControl("Velocidad (bps)", self.cmb_baud,
                                "Tiene que coincidir con el firmware del ESP32."))
        self.btn_ports = QPushButton("⟳ Releer puertos")
        self.btn_ports.clicked.connect(lambda: self._refresh_ports())
        link.add(self.btn_ports)
        self.lbl_ports = QLabel("—")
        self.lbl_ports.setWordWrap(True)
        self.lbl_ports.setStyleSheet(
            f"color: {theme.TEXT_FAINT}; font-size: 10px; "
            f"font-family: {theme.MONO};")
        link.add(self.lbl_ports)
        layout.addWidget(link)

        recording = Section("Formato de grabación")
        self.chk_raw = QCheckBox("Modo RAW (volcado directo sin parsear)")
        self.chk_raw.setToolTip(
            "Escribe el flujo serie tal como llega. Es más rápido y no puede "
            "perder nada por un error de sincronismo, pero el archivo no "
            "lleva el sello de época UTC.")
        self.chk_raw.toggled.connect(self._on_raw)
        recording.add(self.chk_raw)

        self.chk_wait_pps = QCheckBox("Esperar el primer PPS antes de grabar")
        self.chk_wait_pps.setChecked(True)
        self.chk_wait_pps.setToolTip(
            "Recomendado: el archivo arranca con el sello de época absoluta "
            "(0xFFEE), que es lo que permite pasar todas las muestras a UTC.")
        self.chk_wait_pps.toggled.connect(self._on_wait_pps)
        recording.add(self.chk_wait_pps)
        layout.addWidget(recording)

        clock = Section("Base de tiempo de NECTAR")
        note = QLabel("Este instrumento ancla la hora UTC de toda la sesión.")
        note.setWordWrap(True)
        note.setStyleSheet(f"color: {theme.TEXT_FAINT}; font-size: 10px; "
                           f"font-style: italic;")
        clock.add(note)
        self.row_anchor = clock.add_row("Ancla UTC", "sin anclar", theme.WARN)
        self.row_t0 = clock.add_row("T0 de la sesión", "sin fijar")
        self.row_modules = clock.add_row("Módulos conectados", "0")
        self.row_pps = clock.add_row("PPS acumulados", "0")
        layout.addWidget(clock)

        protocol_box = Section("Protocolo (18 bytes por paquete)")
        text = QLabel(
            "0xBBAA  IMU   micros + ax,ay,az,gx,gy,gz\n"
            "0xBBCC  PPS   micros del flanco del GPS\n"
            "0xBBDD  COORD lat, lon, fecha/hora UTC, validez\n"
            "0xFFEE  ATM   sello de época UTC (raíz del archivo)\n\n"
            f"Escalas: {config.IMU_ACCEL_SCALE:.0f} LSB/g · "
            f"{config.IMU_GYRO_SCALE:.1f} LSB/(°/s)")
        text.setStyleSheet(f"color: {theme.TEXT_FAINT}; font-size: 10px; "
                           f"font-family: {theme.MONO};")
        protocol_box.add(text)
        layout.addWidget(protocol_box)

        # Solo se rellena la etiqueta: el descubrimiento de verdad lo lanza
        # ``FamilyTab.__init__`` 200 ms después, con la ventana ya construida.
        # Hacer E/S de hardware a mitad de construir un widget es pedir que un
        # fallo del hardware impida arrancar el programa.
        self._refresh_ports(detect=False)
        return container

    # ------------------------------------------------------------------
    def _refresh_ports(self, detect: bool = True) -> None:
        ports = list_serial_ports()
        self.lbl_ports.setText(", ".join(ports) if ports
                               else "sin puertos serie con hardware detrás")
        if detect:
            self.detect()

    def _on_raw(self, checked: bool) -> None:
        for worker in self.workers():
            worker.raw_mode = bool(checked)

    def _on_wait_pps(self, checked: bool) -> None:
        for worker in self.workers():
            worker.wait_for_pps = bool(checked)

    def add_device(self, descriptor: Dict[str, Any]) -> None:
        descriptor = dict(descriptor)
        descriptor["baudrate"] = int(self.cmb_baud.currentText())
        super().add_device(descriptor)

    def on_device_added(self, worker: DeviceWorker, panel: DevicePanel) -> None:
        worker.raw_mode = self.chk_raw.isChecked()
        worker.wait_for_pps = self.chk_wait_pps.isChecked()

    def on_refresh(self) -> None:
        workers = self.workers()
        self.row_modules.set_value(str(len(workers)))
        self.row_pps.set_value(
            str(sum(worker.pps_count for worker in workers)))

        anchor = TIME_BASE.anchor
        if anchor is None:
            self.row_anchor.set_value("sin anclar", theme.WARN)
        else:
            stamp = time.strftime("%Y-%m-%d %H:%M:%S",
                                  time.gmtime(anchor.unix_ts))
            self.row_anchor.set_value(f"{stamp} UTC", theme.OK_BRIGHT)

        t0 = TIME_BASE.t0_ns
        self.row_t0.set_value(f"{t0}" if t0 else "sin fijar",
                              theme.OK_BRIGHT if t0 else theme.TEXT_FAINT)
