"""Pestaña 4 · Reconstrucción: intensidad a partir del flujo de eventos.

Es lo que en ``main_v14.1.py`` era la pestaña de enfoque, aislado acá como lo
que realmente es: una **vista derivada**, no un instrumento. Una cámara de
eventos no entrega imágenes sino cambios de contraste; para ver el blanco como
lo vería una cámara normal hay que integrar esos cambios.

Cada evento dice que el logaritmo de la irradiancia del píxel cruzó un umbral:
``+c_on`` si subió, ``-c_off`` si bajó. Sumarlos reconstruye ``log I``, y
exponenciando se recupera la intensidad. El integrador puro acumularía error
para siempre, así que se le agrega un decaimiento con constante ``tau``, que
es literalmente la memoria visual de la imagen:

* ``tau`` corto (≈10 ms): solo se ve lo que se está moviendo ahora.
* ``tau`` largo (≈1 s): el blanco estático se ve estable pero deja estelas.

Para enfocar la grilla de 100 manchas conviene un valor intermedio, del orden
de 100 ms, que es el que viene por defecto.

Lo que graba esta pestaña es el **video** de la reconstrucción. Los datos
crudos ya los está guardando la pestaña 2; acá lo que tiene valor es el
registro visual de lo que vio el operador durante la toma.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from PyQt6.QtWidgets import (QGridLayout, QHBoxLayout, QLabel, QVBoxLayout,
                             QWidget)

from ...devices.base import DeviceWorker
from .. import theme
from ..widgets.common import LabeledControl, Section, SliderSpin, ValueRow
from ..widgets.image_view import ImageVisor
from .base_family_tab import DevicePanel, FamilyTab


class ReconstructionPanel(DevicePanel):
    """Panel de una reconstrucción: imagen integrada y estado del video."""

    def __init__(self, worker, parent: Optional[QWidget] = None) -> None:
        super().__init__(worker, parent)
        layout = self.body()

        top = QHBoxLayout()
        self.lbl_rate = QLabel("0.00 MEv/s")
        self.lbl_rate.setStyleSheet(theme.value_style(theme.OK_BRIGHT, 18))
        self.lbl_physics = QLabel("—")
        self.lbl_physics.setStyleSheet(theme.value_style(theme.TEXT_DIM, 12))
        top.addWidget(self.lbl_rate)
        top.addStretch(1)
        top.addWidget(self.lbl_physics)
        layout.addLayout(top)

        self.visor = ImageVisor(sensor_size=(worker.width, worker.height),
                                with_roi=False, min_height=340)
        layout.addWidget(self.visor, 1)

        grid = QGridLayout()
        grid.setHorizontalSpacing(16)
        self.row_video = ValueRow("Archivo de video", "—", theme.WARN)
        self.row_frames = ValueRow("Cuadros escritos", "0")
        grid.addWidget(self.row_video, 0, 0)
        grid.addWidget(self.row_frames, 0, 1)
        layout.addLayout(grid)

    def refresh_stats(self, stats: Dict[str, Any]) -> None:
        self.lbl_rate.setText(f"{stats.get('tasa_mev_s', 0):.2f} MEv/s")
        self.lbl_physics.setText(
            f"τ={stats.get('tau_s', 0):.3f} s · "
            f"c_on={stats.get('c_on', 0):.2f} · "
            f"c_off={stats.get('c_off', 0):.2f} · "
            f"Δt={stats.get('delta_t_us', 0):,} µs")
        self.row_video.set_value(stats.get("archivo") or "—")
        self.row_frames.set_value(f"{stats.get('frames_video', 0):,}")
        if not stats.get("opencv", True):
            self.row_video.set_value("OpenCV no instalado", theme.DANGER)

    def on_preview(self, image) -> None:
        self.visor.set_image(image)
        # Acuse de recibo: el worker no genera el frame siguiente hasta que la
        # interfaz terminó con este. La grabación de video no depende de esto.
        self.worker.acknowledge()


class ReconstructionTab(FamilyTab):
    """Pestaña de reconstrucción."""

    family = "reconstruction"
    family_label = "Reconstrucción"
    empty_message = ("No hay reconstrucciones activas.\n\n"
                     "Pulsa DETECTAR y AGREGAR: se ofrece una entrada por "
                     "cada cámara de eventos disponible. La reconstrucción "
                     "consume el mismo flujo de eventos y produce una imagen "
                     "de intensidad.")
    side_width: int = 310

    def create_panel(self, worker: DeviceWorker) -> DevicePanel:
        return ReconstructionPanel(worker)

    # ------------------------------------------------------------------
    def build_side_panel(self) -> Optional[QWidget]:
        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        box = Section("Física del integrador")

        self.ctl_tau = SliderSpin(1, 1000, 100)
        self.ctl_tau.value_changed.connect(self._on_physics)
        box.add(LabeledControl(
            "τ · memoria (ms)", self.ctl_tau,
            "Constante de decaimiento. Corta: solo se ve el movimiento. "
            "Larga: el blanco estático se ve estable pero deja estelas."))

        self.ctl_con = SliderSpin(1, 100, 15)
        self.ctl_con.value_changed.connect(self._on_physics)
        box.add(LabeledControl(
            "c_on · salto por evento ON (x0,01)", self.ctl_con,
            "Cuánto sube el logaritmo de intensidad por cada evento de "
            "subida de luz. Es el umbral de contraste del sensor."))

        self.ctl_coff = SliderSpin(1, 100, 15)
        self.ctl_coff.value_changed.connect(self._on_physics)
        box.add(LabeledControl(
            "c_off · salto por evento OFF (x0,01)", self.ctl_coff,
            "Igual que c_on pero para las bajadas de luz. Si la imagen se "
            "va a oscuro o a blanco, el desbalance entre los dos es la causa "
            "más probable."))

        self.lbl_values = QLabel("")
        self.lbl_values.setStyleSheet(theme.value_style(theme.CONSOLE_TEXT, 12))
        box.add(self.lbl_values)
        layout.addWidget(box)

        timing = Section("Tiempos")
        self.ctl_delta = SliderSpin(200, 100000, 16666, step=100)
        self.ctl_delta.value_changed.connect(self._on_delta)
        timing.add(LabeledControl(
            "Δt de integración (µs)", self.ctl_delta,
            "Ventana de eventos por iteración. 16 666 µs ≈ 60 Hz."))
        self.ctl_fps = SliderSpin(1, 60, 30)
        self.ctl_fps.value_changed.connect(self._on_fps)
        timing.add(LabeledControl(
            "Cuadros por segundo del video", self.ctl_fps,
            "Manda sobre el video grabado y sobre el refresco en pantalla."))
        self.row_active = timing.add_row("Reconstrucciones activas", "0")
        layout.addWidget(timing)

        self._update_values()
        return container

    # ------------------------------------------------------------------
    def _physics(self):
        return (self.ctl_tau.value() / 1000.0,
                self.ctl_con.value() / 100.0,
                self.ctl_coff.value() / 100.0)

    def _update_values(self) -> None:
        tau, c_on, c_off = self._physics()
        self.lbl_values.setText(
            f"τ = {tau:.3f} s · c_on = {c_on:.2f} · c_off = {c_off:.2f}")

    def _on_physics(self, _value: int = 0) -> None:
        tau, c_on, c_off = self._physics()
        self._update_values()
        for worker in self.workers():
            worker.set_physics(tau, c_on, c_off)

    def _on_delta(self, value: int) -> None:
        for worker in self.workers():
            worker.set_accumulation(value)

    def _on_fps(self, value: int) -> None:
        for worker in self.workers():
            worker.display_fps = int(value)

    def on_device_added(self, worker: DeviceWorker, panel: DevicePanel) -> None:
        tau, c_on, c_off = self._physics()
        worker.set_physics(tau, c_on, c_off)
        worker.set_accumulation(self.ctl_delta.value())
        worker.display_fps = self.ctl_fps.value()

    def on_refresh(self) -> None:
        self.row_active.set_value(str(len(self.workers())))
