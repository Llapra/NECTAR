"""Rejilla de vistas previas pequeñas para la pestaña 1.

El requisito era explícito: "una mini visualización para ver que aparentemente
se vea lo que queremos enfocar de por lo menos 4 cámaras, 2 de evento y 2
emergent, en versión pequeña que casi no ocupe cómputo".

Cómo se cumple lo de "casi no ocupar cómputo":

1. **Limitación de tasa propia**: cada celda descarta los frames que llegan
   antes de su próximo turno (4 fps por defecto). El worker sigue mandando a
   su ritmo; la celda simplemente los ignora.
2. **Escalado a miniatura una sola vez**: la imagen se reduce a ~160 px de
   ancho antes de construir el ``QPixmap``, no se dibuja a tamaño completo
   para después achicarla.
3. **Sin ROI, sin ejes, sin PyQtGraph**: es un ``QLabel`` con un pixmap. No
   hay escena gráfica que recalcular.

El resultado es una confirmación visual de que las cámaras están apuntando a
donde deben, que es para lo que sirve: no reemplaza a la vista previa a tamaño
real de las pestañas 2 y 3.
"""

from __future__ import annotations

import time
from typing import Dict, Optional

import numpy as np
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QImage, QPixmap
from PyQt6.QtWidgets import (QGridLayout, QLabel, QSizePolicy, QVBoxLayout,
                             QWidget)

from .. import theme


class MiniPreview(QWidget):
    """Una celda: título arriba, miniatura abajo."""

    def __init__(self, title: str = "—", max_fps: float = 4.0,
                 thumb_width: int = 170,
                 parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.thumb_width = thumb_width
        self._interval = 1.0 / max(0.5, max_fps)
        self._next_due = 0.0

        layout = QVBoxLayout(self)
        layout.setContentsMargins(1, 1, 1, 1)
        layout.setSpacing(1)

        self.title = QLabel(title)
        self.title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.title.setStyleSheet(
            f"color: {theme.TEXT_FAINT}; font-size: 10px; "
            f"font-family: {theme.MONO};")

        self.view = QLabel("—")
        self.view.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.view.setMinimumSize(120, 92)
        self.view.setStyleSheet(
            f"background-color: #000000; color: #333333; "
            f"border: 1px solid {theme.BORDER}; font-size: 10px;")
        self.view.setSizePolicy(QSizePolicy.Policy.Expanding,
                                QSizePolicy.Policy.Expanding)

        layout.addWidget(self.title)
        layout.addWidget(self.view, 1)

    # ------------------------------------------------------------------
    def set_title(self, title: str, active: bool = False) -> None:
        self.title.setText(title)
        color = theme.OK_BRIGHT if active else theme.TEXT_FAINT
        self.title.setStyleSheet(
            f"color: {color}; font-size: 10px; font-family: {theme.MONO};")

    def clear(self, message: str = "—") -> None:
        self.view.setPixmap(QPixmap())
        self.view.setText(message)

    def set_image(self, array: np.ndarray) -> None:
        """Acepta el frame y lo descarta si no toca todavía."""
        now = time.perf_counter()
        if now < self._next_due:
            return
        self._next_due = max(now, self._next_due + self._interval)

        if array is None or array.size == 0:
            return

        data = np.ascontiguousarray(array)
        if data.dtype != np.uint8:
            data = np.clip(data, 0, 255).astype(np.uint8)

        # Decimación entera antes de construir el QImage: es la parte que
        # mantiene esto barato con imágenes de 1604x1100.
        height = data.shape[0]
        width = data.shape[1]
        step = max(1, width // self.thumb_width)
        if step > 1:
            data = data[::step, ::step]
            data = np.ascontiguousarray(data)
            height, width = data.shape[0], data.shape[1]

        if data.ndim == 2:
            image = QImage(data.data, width, height, width,
                           QImage.Format.Format_Grayscale8)
        elif data.ndim == 3 and data.shape[2] == 3:
            image = QImage(data.data, width, height, width * 3,
                           QImage.Format.Format_RGB888)
        else:
            return

        pixmap = QPixmap.fromImage(image.copy())
        self.view.setText("")
        self.view.setPixmap(pixmap.scaled(
            self.view.size(), Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.FastTransformation))


class MiniPreviewGrid(QWidget):
    """Rejilla 2x2 fija: dos de eventos y dos Emergent.

    Las celdas se asignan solas a los primeros dos dispositivos conectados de
    cada familia. Si hay más, siguen viéndose completos en su propia pestaña.
    """

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        layout = QGridLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(3)

        self.cells: Dict[str, MiniPreview] = {
            "event_cam_0": MiniPreview("EvCam 1"),
            "emergent_0": MiniPreview("EmerCam 1"),
            "event_cam_1": MiniPreview("EvCam 2"),
            "emergent_1": MiniPreview("EmerCam 2"),
        }
        layout.addWidget(self.cells["event_cam_0"], 0, 0)
        layout.addWidget(self.cells["emergent_0"], 0, 1)
        layout.addWidget(self.cells["event_cam_1"], 1, 0)
        layout.addWidget(self.cells["emergent_1"], 1, 1)

        #: device_id -> clave de celda, para saber dónde pintar cada frame.
        self._assigned: Dict[str, str] = {}

    # ------------------------------------------------------------------
    def assign(self, worker) -> Optional[MiniPreview]:
        """Da una celda al worker si su familia tiene lugar."""
        if worker.device_id in self._assigned:
            return self.cells[self._assigned[worker.device_id]]
        if worker.family not in ("event_cam", "emergent"):
            return None
        for slot in range(2):
            key = f"{worker.family}_{slot}"
            if key in self._assigned.values():
                continue
            self._assigned[worker.device_id] = key
            cell = self.cells[key]
            cell.set_title(worker.label, active=True)
            cell.clear("esperando…")
            return cell
        return None

    def release(self, device_id: str) -> None:
        key = self._assigned.pop(device_id, None)
        if key is None:
            return
        family, slot = key.rsplit("_", 1)
        default = {"event_cam": "EvCam", "emergent": "EmerCam"}.get(family, "—")
        cell = self.cells[key]
        cell.set_title(f"{default} {int(slot) + 1}", active=False)
        cell.clear("—")

    def cell_for(self, device_id: str) -> Optional[MiniPreview]:
        key = self._assigned.get(device_id)
        return self.cells.get(key) if key else None
