"""Buzón de un solo hueco para la vista previa.

Portado de ``pastam_cam/core/frame_hub.py``, que a su vez formalizó el patrón
que ``main_v14.1.py`` usaba de forma informal con la bandera ``gui_ready``.

El problema clásico: si la vista previa se alimenta de una cola y la cámara
produce más rápido de lo que la pantalla dibuja, la cola crece y lo que se ve
pasa a ser pasado, cada vez más atrasado.

Aquí no hay cola. Hay un único hueco:

* el hilo de adquisición solo deposita cuando toca según la tasa de pantalla
  (``wants_frame``). A 600 fps de captura y 60 de pantalla, 9 de cada 10
  frames ni se copian;
* si deposita dos veces antes de que la pantalla lea, el segundo pisa al
  primero: nunca se acumula;
* el lector siempre obtiene el frame más reciente, o nada.

Consecuencia: lo que se ve siempre es el presente. Si la pantalla se atrasa se
pierden frames *de vista previa*, que es exactamente lo que se quiere; la
grabación va por otro camino y no se ve afectada.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Any, Optional, Tuple

import numpy as np


@dataclass(frozen=True)
class FrameMeta:
    """Datos que acompañan a los píxeles."""

    frame_id: int = 0
    device_ts: int = 0        # reloj interno del instrumento
    host_ns: int = 0          # perf_counter_ns() del host al recibirlo
    width: int = 0
    height: int = 0
    stride: int = 0
    pixel_format: Any = None


class FrameHub:
    """Buzón de un solo hueco, sin cola, con limitador de tasa."""

    def __init__(self, display_fps: float = 60.0):
        self._lock = threading.Lock()
        self._slot: Optional[np.ndarray] = None     # buffer del escritor
        self._meta: Optional[FrameMeta] = None
        self._out: Optional[np.ndarray] = None      # buffer del lector
        self._has_new = False
        self._gui_ready = True

        self._interval = 1.0 / max(1.0, float(display_fps))
        self._next_due = 0.0
        self._held_since = 0.0

        self.published = 0
        self.skipped = 0
        self.stalls = 0

    #: Si la interfaz no acusa recibo en este tiempo, se asume que no está
    #: consumiendo (pestaña oculta, widget destruido) y se sigue publicando.
    #: Sin esta válvula, un acuse perdido congelaría la vista previa para
    #: siempre y parecería que la cámara se colgó.
    ACK_TIMEOUT_S = 1.0

    # ------------------------------------------------------------------
    # Tasa de visualización
    # ------------------------------------------------------------------
    def set_display_fps(self, fps: float) -> None:
        fps = max(1.0, min(240.0, float(fps)))
        with self._lock:
            self._interval = 1.0 / fps
            self._next_due = 0.0

    @property
    def display_fps(self) -> float:
        return 1.0 / self._interval

    def wants_frame(self, now: Optional[float] = None) -> bool:
        """True si toca refrescar la vista previa.

        Se llama desde el hilo de adquisición en *cada* frame, así que tiene
        que ser barata: dos comparaciones y nada más. Además exige que la
        interfaz haya acusado recibo del frame anterior (contrapresión).
        """
        now = time.perf_counter() if now is None else now
        if not self._gui_ready:
            if (now - self._held_since) < self.ACK_TIMEOUT_S:
                self.skipped += 1
                return False
            # Nadie acusó recibo en el plazo: se libera y se cuenta.
            self._gui_ready = True
            self.stalls += 1
        if now < self._next_due:
            self.skipped += 1
            return False
        # Anclar al reloj para que la tasa no derive si un frame llega tarde.
        self._next_due = now + self._interval
        return True

    # ------------------------------------------------------------------
    # Contrapresión: la interfaz avisa cuando terminó de pintar
    # ------------------------------------------------------------------
    def acknowledge(self) -> None:
        """La interfaz avisa que terminó de pintar el frame anterior."""
        self._gui_ready = True

    def hold(self) -> None:
        self._gui_ready = False
        self._held_since = time.perf_counter()

    # ------------------------------------------------------------------
    # Escritura (hilo de adquisición) y lectura (hilo de vista previa / GUI)
    # ------------------------------------------------------------------
    def publish(self, raw: np.ndarray, meta: Optional[FrameMeta] = None) -> None:
        """Deposita una copia del frame. Pisa lo que hubiera."""
        with self._lock:
            if self._slot is None or self._slot.shape != raw.shape \
                    or self._slot.dtype != raw.dtype:
                self._slot = np.empty_like(raw)
            np.copyto(self._slot, raw)
            self._meta = meta or FrameMeta()
            self._has_new = True
            self.published += 1
        self._gui_ready = False
        self._held_since = time.perf_counter()

    def take(self) -> Optional[Tuple[np.ndarray, FrameMeta]]:
        """Saca el frame más reciente, o None si no hay nada nuevo."""
        with self._lock:
            if not self._has_new or self._slot is None:
                return None
            if self._out is None or self._out.shape != self._slot.shape \
                    or self._out.dtype != self._slot.dtype:
                self._out = np.empty_like(self._slot)
            np.copyto(self._out, self._slot)
            meta = self._meta or FrameMeta()
            self._has_new = False
        return self._out, meta

    def clear(self) -> None:
        with self._lock:
            self._has_new = False
            self._meta = None
            self._next_due = 0.0
            self.published = 0
            self.skipped = 0
        self._gui_ready = True
