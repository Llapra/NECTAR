"""Pestaña 9 · Conversor IMU: de binario a formato de análisis.

Dos cambios respecto a la pestaña equivalente del programa anterior, los dos
pedidos:

**1. Carpeta completa, no archivo por archivo.** Una toma real se graba en
bloques de N minutos, así que el análisis quiere la serie completa. Acá se
elige la carpeta de la medición y se decodifican todos los bloques en orden,
encadenando el eje temporal como si fuera un solo archivo continuo.

**2. Se fue el gráfico de deriva temporal, entró el de vibración.** La deriva
del oscilador del ESP32 frente al PPS era un dato de puesta en marcha: servía
para saber si el firmware estampaba bien los tiempos. Ya está verificado y en
campaña no aporta nada. Lo que sí importa dentro de una cúpula es la
**vibración mecánica**: si la estructura o el banco óptico vibra a alguna
frecuencia, eso se mete en el wander de las manchas y hay que poder verlo para
descontarlo.

Entonces el análisis ahora muestra:

* la densidad espectral de potencia de |a| (con la media quitada, o sea sin
  gravedad ni inclinación estática), en log-log, que es donde un modo
  mecánico aparece como un pico limpio;
* la tabla de los picos dominantes, con su frecuencia y su amplitud RMS;
* el RMS deslizante, para ver si la vibración aparece y desaparece durante la
  toma (una bomba que arranca, la cúpula que rota, alguien caminando por la
  plataforma).

Formatos de salida: ``.csv``, ``.dat`` (texto tabulado), ``.mat``, ``.npz`` y
``.parquet``.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

import numpy as np
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (QApplication, QComboBox, QFileDialog, QHBoxLayout,
                             QLabel, QPushButton, QVBoxLayout, QWidget)

from ...core.algorithms.imu_decode import (SUPPORTED_FORMATS, ImuRecord,
                                           analyze_vibration, decode_file,
                                           decode_folder, export_record)
from .. import theme
from ..widgets.common import LabeledControl, LogConsole, Section, ValueRow

try:
    import matplotlib
    matplotlib.use("QtAgg")
    import matplotlib.gridspec as gridspec
    import matplotlib.pyplot as plt
    from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg as Canvas
    from matplotlib.backends.backend_qtagg import NavigationToolbar2QT as Toolbar
    HAS_MATPLOTLIB = True
except Exception:
    HAS_MATPLOTLIB = False


class ConverterTab(QWidget):
    """Decodificación, análisis de vibración y exportación del IMU."""

    def __init__(self, manager, recorder, sessions,
                 parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.manager = manager
        self.sessions = sessions
        self.record: Optional[ImuRecord] = None
        self.vibration = None
        self._canvas = None
        self._toolbar = None
        self._figure = None

        self._build()

    # ------------------------------------------------------------------
    def _build(self) -> None:
        root = QHBoxLayout(self)
        root.setContentsMargins(8, 6, 8, 8)
        root.setSpacing(8)

        # --- columna izquierda -----------------------------------------
        left = QWidget()
        left.setFixedWidth(330)
        layout = QVBoxLayout(left)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        source = Section("Origen de los datos")
        self.btn_file = QPushButton("📄 ABRIR UN ARCHIVO .bin")
        self.btn_file.setStyleSheet(theme.button_style("accent"))
        self.btn_file.clicked.connect(self._open_file)
        source.add(self.btn_file)

        self.btn_folder = QPushButton("📂 ABRIR CARPETA COMPLETA")
        self.btn_folder.setStyleSheet(theme.button_style("ok"))
        self.btn_folder.setToolTip(
            "Decodifica todos los bloques .bin de la carpeta en orden y los "
            "encadena en un solo eje temporal continuo.")
        self.btn_folder.clicked.connect(self._open_folder)
        source.add(self.btn_folder)

        self.lbl_source = QLabel("(sin cargar)")
        self.lbl_source.setWordWrap(True)
        self.lbl_source.setStyleSheet(
            f"color: {theme.TEXT_FAINT}; font-size: 10px; "
            f"border: 1px solid {theme.BORDER}; padding: 3px;")
        source.add(self.lbl_source)
        layout.addWidget(source)

        summary = Section("Resumen de la medición")
        self.row_packets = summary.add_row("Paquetes IMU", "—")
        self.row_pps = summary.add_row("Pulsos PPS", "—")
        self.row_duration = summary.add_row("Duración", "—")
        self.row_rate = summary.add_row("Frecuencia media", "—")
        self.row_atm = summary.add_row("Sello UTC (ATM)", "—", theme.WARN)
        self.row_discarded = summary.add_row("Bytes descartados", "—")
        layout.addWidget(summary)

        vibration = Section("Vibración")
        self.row_rms = vibration.add_row("RMS global", "—", theme.ACCENT_BRIGHT)
        self.row_dominant = vibration.add_row("Frecuencia dominante", "—")
        self.row_peaks = vibration.add_row("Picos", "—")
        self.row_fs = vibration.add_row("Muestreo efectivo", "—")
        layout.addWidget(vibration)

        export = Section("Exportación")
        self.cmb_format = QComboBox()
        self.cmb_format.addItems(SUPPORTED_FORMATS)
        export.add(LabeledControl(
            "Formato de salida", self.cmb_format,
            "csv y dat no necesitan nada extra. mat requiere SciPy; "
            "parquet requiere pandas y pyarrow."))
        self.btn_export = QPushButton("💾 EXPORTAR")
        self.btn_export.setStyleSheet(theme.button_style("warn"))
        self.btn_export.setEnabled(False)
        self.btn_export.clicked.connect(self._export)
        export.add(self.btn_export)
        layout.addWidget(export)

        self.console = LogConsole(max_lines=300)
        layout.addWidget(QLabel("Registro de operaciones:"))
        layout.addWidget(self.console, 1)
        root.addWidget(left, 0)

        # --- columna derecha: gráficos ---------------------------------
        self.plot_container = QWidget()
        self.plot_layout = QVBoxLayout(self.plot_container)
        self.plot_layout.setContentsMargins(0, 0, 0, 0)
        self.placeholder = QLabel(
            "Carga un archivo o una carpeta para ver la aceleración, el "
            "giróscopo y el análisis de vibración."
            if HAS_MATPLOTLIB else
            "matplotlib no está instalado: no se pueden dibujar los gráficos.\n"
            "pip install matplotlib")
        self.placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.placeholder.setWordWrap(True)
        self.placeholder.setStyleSheet(
            f"color: {theme.TEXT_FAINT}; font-size: 13px;")
        self.plot_layout.addWidget(self.placeholder)
        root.addWidget(self.plot_container, 1)

    # ------------------------------------------------------------------
    def _open_file(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Abrir binario del IMU", str(self.sessions.base_dir),
            "Binarios (*.bin);;Todos los archivos (*)")
        if not path:
            return
        self.console.rule("ARCHIVO")
        self.console.log(f"Leyendo {os.path.basename(path)}…")
        QApplication.processEvents()
        try:
            self.record = decode_file(path)
        except Exception as exc:
            self.console.log(f"✖ Error: {exc}")
            return
        self.lbl_source.setText(path)
        self._after_decode()

    def _open_folder(self) -> None:
        directory = QFileDialog.getExistingDirectory(
            self, "Carpeta con los bloques .bin de la toma",
            str(self.sessions.base_dir))
        if not directory:
            return
        self.console.rule("CARPETA")
        blocks = sorted(Path(directory).glob("*.bin"))
        if not blocks:
            self.console.log("No hay archivos .bin en esa carpeta.")
            return
        self.console.log(f"{len(blocks)} bloque(s) encontrados.")

        def progress(index: int, total: int, name: str) -> None:
            self.console.log(f"  [{index}/{total}] {name}")
            QApplication.processEvents()

        try:
            self.record = decode_folder(directory, progress=progress)
        except Exception as exc:
            self.console.log(f"✖ Error: {exc}")
            return
        self.lbl_source.setText(f"{directory}  ({len(blocks)} bloques)")
        self._after_decode()

    # ------------------------------------------------------------------
    def _after_decode(self) -> None:
        record = self.record
        if record is None or record.empty:
            self.console.log("✖ No se encontraron datos válidos.")
            self.btn_export.setEnabled(False)
            return

        for line in record.summary_lines():
            self.console.log(line)

        self.row_packets.set_value(f"{record.packets:,}")
        self.row_pps.set_value(f"{record.pps_count:,}")
        self.row_duration.set_value(f"{record.duration_s:,.2f} s")
        self.row_rate.set_value(f"{record.rate_hz:,.1f} Hz")
        self.row_discarded.set_value(f"{record.discarded_bytes:,}")
        if record.root_atm is not None:
            import datetime
            stamp = datetime.datetime.fromtimestamp(
                record.root_atm, tz=datetime.timezone.utc)
            self.row_atm.set_value(f"{stamp:%Y-%m-%d %H:%M:%S} UTC",
                                   theme.OK_BRIGHT)
        else:
            self.row_atm.set_value("ausente (tiempos relativos)", theme.WARN)

        self.console.log("Analizando vibración…")
        QApplication.processEvents()
        self.vibration = analyze_vibration(record)
        if self.vibration.valid:
            self.row_rms.set_value(f"{self.vibration.rms_global:.5f} g")
            self.row_dominant.set_value(f"{self.vibration.dominant_hz:.2f} Hz")
            self.row_fs.set_value(f"{self.vibration.sample_rate:,.1f} Hz")
            peaks = " · ".join(f"{peak['hz']:.1f} Hz"
                               for peak in self.vibration.peaks[:4])
            self.row_peaks.set_value(peaks or "—")
            self.console.log(f"Pico dominante: {self.vibration.dominant_hz:.2f} Hz "
                             f"({self.vibration.dominant_amplitude:.5f} g RMS)")
        else:
            self.console.log("No hay suficientes muestras para el espectro.")

        self.btn_export.setEnabled(True)
        self._draw()

    # ------------------------------------------------------------------
    def _draw(self) -> None:
        if not HAS_MATPLOTLIB or self.record is None:
            return
        record = self.record

        if self._canvas is not None:
            self.plot_layout.removeWidget(self._canvas)
            self._canvas.deleteLater()
            self._canvas = None
        if self._toolbar is not None:
            self.plot_layout.removeWidget(self._toolbar)
            self._toolbar.deleteLater()
            self._toolbar = None
        if self._figure is not None:
            plt.close(self._figure)
        self.placeholder.setVisible(False)

        plt.style.use("dark_background")
        self._figure = plt.figure(figsize=(11, 7), dpi=100)
        self._figure.patch.set_facecolor(theme.BG)
        grid = gridspec.GridSpec(3, 2, height_ratios=[1, 1, 1.1],
                                 width_ratios=[2, 1.2], figure=self._figure)

        ax_accel = self._figure.add_subplot(grid[0, 0])
        ax_gyro = self._figure.add_subplot(grid[1, 0], sharex=ax_accel)
        ax_rms = self._figure.add_subplot(grid[2, 0], sharex=ax_accel)
        ax_psd = self._figure.add_subplot(grid[:, 1])
        for axis in (ax_accel, ax_gyro, ax_rms, ax_psd):
            axis.set_facecolor(theme.BG_PANEL)
            axis.grid(True, color="#444444", linestyle="--", alpha=0.3)
            axis.tick_params(colors="#999999", labelsize=8)

        # Decimación para el dibujo: con millones de puntos matplotlib se
        # arrastra y el gráfico se ve igual.
        step = max(1, record.t.size // 12000)
        if step > 1:
            self.console.log(f"Gráfico: 1 de cada {step} puntos.")
        t = record.t[::step]

        ax_accel.plot(t, record.ax[::step], lw=0.7, color="#ff6b6b", label="ax")
        ax_accel.plot(t, record.ay[::step], lw=0.7, color="#4ecdc4", label="ay")
        ax_accel.plot(t, record.az[::step], lw=0.7, color="#f7fff7", label="az")
        ax_accel.set_ylabel("aceleración (g)", color="white", fontsize=9)
        ax_accel.legend(loc="upper right", facecolor="#333333",
                        edgecolor="none", labelcolor="white", fontsize=8)

        ax_gyro.plot(t, record.gx[::step], lw=0.7, color="#ff9ff3", label="gx")
        ax_gyro.plot(t, record.gy[::step], lw=0.7, color="#feca57", label="gy")
        ax_gyro.plot(t, record.gz[::step], lw=0.7, color="#54a0ff", label="gz")
        ax_gyro.set_ylabel("giróscopo (°/s)", color="white", fontsize=9)
        ax_gyro.legend(loc="upper right", facecolor="#333333",
                       edgecolor="none", labelcolor="white", fontsize=8)

        vibration = self.vibration
        if vibration is not None and vibration.valid:
            if vibration.rms.size:
                ax_rms.plot(vibration.rms_t, vibration.rms, lw=1.0,
                            color="#00e5ff")
                ax_rms.fill_between(vibration.rms_t, 0, vibration.rms,
                                    color="#00e5ff", alpha=0.25)
            ax_rms.set_ylabel("RMS |a| (g)", color="white", fontsize=9)
            ax_rms.set_xlabel("tiempo (s)", color="white", fontsize=9)

            mask = vibration.freqs > 0
            ax_psd.loglog(vibration.freqs[mask], vibration.psd[mask],
                          lw=0.9, color="#69f0ae")
            for peak in vibration.peaks[:4]:
                ax_psd.axvline(peak["hz"], color="#ffb300", linestyle=":",
                               lw=0.9)
                ax_psd.annotate(f"{peak['hz']:.1f} Hz",
                                xy=(peak["hz"], peak["psd"]),
                                xytext=(4, 4), textcoords="offset points",
                                color="#ffb300", fontsize=8)
            ax_psd.set_title("Densidad espectral de |a| (sin gravedad)",
                             color="white", fontsize=10)
            ax_psd.set_xlabel("frecuencia (Hz)", color="white", fontsize=9)
            ax_psd.set_ylabel("PSD (g²/Hz)", color="white", fontsize=9)
        else:
            ax_psd.text(0.5, 0.5, "sin espectro", color="white",
                        ha="center", va="center")
            ax_rms.set_xlabel("tiempo (s)", color="white", fontsize=9)

        self._figure.tight_layout()
        self._canvas = Canvas(self._figure)
        self._toolbar = Toolbar(self._canvas, self.plot_container)
        self.plot_layout.addWidget(self._toolbar)
        self.plot_layout.addWidget(self._canvas)

    # ------------------------------------------------------------------
    def _export(self) -> None:
        if self.record is None:
            return
        fmt = self.cmb_format.currentText()
        path, _ = QFileDialog.getSaveFileName(
            self, f"Guardar como .{fmt}",
            str(Path(self.sessions.base_dir) / f"imu_convertido.{fmt}"),
            f"{fmt.upper()} (*.{fmt});;Todos los archivos (*)")
        if not path:
            return
        if not path.lower().endswith(f".{fmt}"):
            path = f"{path}.{fmt}"
        self.console.log(f"Exportando {self.record.t.size:,} muestras a .{fmt}…")
        QApplication.processEvents()
        try:
            written = export_record(self.record, path, fmt)
        except Exception as exc:
            self.console.log(f"✖ {exc}")
            return
        size = written.stat().st_size / 1e6
        self.console.log(f"✔ {written.name} ({size:,.2f} MB)")
