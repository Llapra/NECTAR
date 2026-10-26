"""Visor de imagen para las vistas previas de cámara.

Usa PyQtGraph cuando está disponible (aceleración por GPU y ROI arrastrable
con el mouse, igual que el visor del programa anterior) y cae a un visor
propio con ``QPainter`` si no lo está, para que la falta de una dependencia
opcional no deje al programa sin vista previa.

El ROI se reporta siempre en **coordenadas del sensor**, no de la pantalla:
lo que se manda al worker tiene que ser el recorte real en píxeles del sensor,
independientemente de cómo esté escalada la ventana.
"""

from __future__ import annotations

from typing import Optional, Tuple

import numpy as np
from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QImage, QPainter, QPen, QColor
from PyQt6.QtWidgets import QSizePolicy, QVBoxLayout, QWidget

from .. import theme

try:
    import pyqtgraph as pg
    # ``useOpenGL`` manda el pintado a la GPU, que es lo que hace que la vista
    # de la cámara de eventos se sienta fluida a 60 cuadros por segundo. El
    # visor del programa anterior del laboratorio lo activa, y su ausencia
    # acá era una de las diferencias visibles de la versión 1.0.0.
    #
    # Depende de PyOpenGL, que es opcional: si falta, pyqtgraph puede fallar
    # al dibujar (no al configurarse), y el error aparecería recién con la
    # primera imagen. Así que se comprueba el import ahora y solo se activa
    # si está, cayendo al pintado por CPU en silencio si no.
    try:
        import OpenGL                                   # noqa: F401
        HAS_OPENGL = True
    except Exception:
        HAS_OPENGL = False
    pg.setConfigOptions(imageAxisOrder="row-major", antialias=False,
                        useOpenGL=HAS_OPENGL)
    HAS_PYQTGRAPH = True
except Exception:       # pyqtgraph ausente o sin backend
    HAS_PYQTGRAPH = False
    HAS_OPENGL = False


def aceleracion_estado() -> str:
    """Qué se está usando para pintar. Lo informa ``--check``."""
    if not HAS_PYQTGRAPH:
        return ("pyqtgraph no disponible: vista previa por QPainter "
                "(funciona, pero sin ROI arrastrable ni aceleración)")
    if HAS_OPENGL:
        return "pyqtgraph con OpenGL (pintado por GPU)"
    return ("pyqtgraph sin OpenGL (pintado por CPU). Para la vista fluida de "
            "la cámara de eventos: pip install PyOpenGL")


def _to_qimage(array: np.ndarray) -> Optional[QImage]:
    """Convierte un arreglo de NumPy a QImage sin copiar más de lo necesario."""
    if array is None or array.size == 0:
        return None
    data = np.ascontiguousarray(array)
    if data.dtype != np.uint8:
        data = np.clip(data, 0, 255).astype(np.uint8)

    if data.ndim == 2:
        height, width = data.shape
        image = QImage(data.data, width, height, width,
                       QImage.Format.Format_Grayscale8)
    elif data.ndim == 3 and data.shape[2] == 3:
        height, width, _ = data.shape
        image = QImage(data.data, width, height, width * 3,
                       QImage.Format.Format_RGB888)
    else:
        return None
    # copy(): el arreglo puede ser reutilizado por el worker en el próximo
    # frame, y QImage no es dueño de esa memoria.
    return image.copy()


class _PainterVisor(QWidget):
    """Visor mínimo con QPainter: centra la imagen y respeta el aspecto."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._image: Optional[QImage] = None
        self.setMinimumSize(320, 240)
        self.setSizePolicy(QSizePolicy.Policy.Expanding,
                           QSizePolicy.Policy.Expanding)
        self.setStyleSheet(f"background-color: #000000; "
                           f"border: 1px solid {theme.BORDER};")

    def set_image(self, array: np.ndarray) -> None:
        self._image = _to_qimage(array)
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor("#000000"))
        if self._image is None:
            painter.setPen(QPen(QColor(theme.TEXT_FAINT)))
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter,
                             "sin señal")
            painter.end()
            return

        scaled = self._image.scaled(self.size(),
                                    Qt.AspectRatioMode.KeepAspectRatio,
                                    Qt.TransformationMode.FastTransformation)
        x = (self.width() - scaled.width()) // 2
        y = (self.height() - scaled.height()) // 2
        painter.drawImage(x, y, scaled)
        painter.end()


class ImageVisor(QWidget):
    """Vista previa de una cámara, con ROI opcional.

    ``roi_changed`` entrega ``(x, y, ancho, alto)`` en píxeles del sensor.
    """

    roi_changed = pyqtSignal(tuple)

    def __init__(self, sensor_size: Tuple[int, int] = (640, 480),
                 with_roi: bool = True, min_height: int = 240,
                 parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.sensor_width, self.sensor_height = sensor_size
        self._with_roi = with_roi and HAS_PYQTGRAPH

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.setMinimumHeight(min_height)

        self._image_item = None
        self._roi = None
        self._fallback: Optional[_PainterVisor] = None

        if HAS_PYQTGRAPH:
            self._graphics = pg.GraphicsLayoutWidget()
            self._graphics.setBackground("#000000")
            self._view = self._graphics.addViewBox()
            self._view.setAspectLocked(True)
            self._view.invertY(True)
            self._view.setMenuEnabled(False)
            self._image_item = pg.ImageItem(
                np.zeros((self.sensor_height, self.sensor_width, 3),
                         dtype=np.uint8))
            self._view.addItem(self._image_item)
            layout.addWidget(self._graphics)

            if self._with_roi:
                self._roi = pg.RectROI(
                    [self.sensor_width * 0.25, self.sensor_height * 0.25],
                    [self.sensor_width * 0.5, self.sensor_height * 0.5],
                    pen=pg.mkPen(theme.DANGER, width=2))
                self._roi.addScaleHandle([1, 1], [0, 0])
                self._roi.addScaleHandle([0, 0], [1, 1])
                self._view.addItem(self._roi)
                self._roi.hide()
                self._roi.sigRegionChangeFinished.connect(self._emit_roi)
        else:
            self._fallback = _PainterVisor()
            layout.addWidget(self._fallback)

    # ------------------------------------------------------------------
    def set_image(self, array: np.ndarray) -> None:
        if array is None:
            return
        if self._image_item is not None:
            self._image_item.setImage(array, autoLevels=False)
        elif self._fallback is not None:
            self._fallback.set_image(array)

    def set_sensor_size(self, width: int, height: int) -> None:
        self.sensor_width, self.sensor_height = int(width), int(height)

    # ------------------------------------------------------------------
    def set_roi_visible(self, visible: bool) -> None:
        if self._roi is None:
            return
        if visible:
            self._roi.show()
            self._emit_roi()
        else:
            self._roi.hide()

    def roi(self) -> Tuple[int, int, int, int]:
        """ROI actual en coordenadas del sensor."""
        if self._roi is None:
            return 0, 0, self.sensor_width, self.sensor_height
        position = self._roi.pos()
        size = self._roi.size()
        x = int(max(0, min(self.sensor_width - 1, position.x())))
        y = int(max(0, min(self.sensor_height - 1, position.y())))
        width = int(max(1, min(self.sensor_width - x, size.x())))
        height = int(max(1, min(self.sensor_height - y, size.y())))
        return x, y, width, height

    def set_roi(self, x: int, y: int, width: int, height: int) -> None:
        if self._roi is None:
            return
        self._roi.blockSignals(True)
        self._roi.setPos([x, y])
        self._roi.setSize([width, height])
        self._roi.blockSignals(False)

    def _emit_roi(self) -> None:
        self.roi_changed.emit(self.roi())

    @property
    def supports_roi(self) -> bool:
        return self._roi is not None
