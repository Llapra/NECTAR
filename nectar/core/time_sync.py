"""Base de tiempo única de NECTAR.

El problema que resuelve
------------------------
NECTAR mezcla instrumentos con tres relojes distintos:

* la cámara Emergent estampa cada frame con su propio contador interno
  (``GevTimestamp``), que no tiene relación con la hora del mundo;
* la cámara de eventos entrega marcas en microsegundos relativas al inicio
  de su stream;
* el ESP32 del IMU cuenta ``micros()`` desde que arrancó, pero además recibe
  el pulso PPS del GPS, que *sí* está anclado a UTC.

Para que el dataset sirva científicamente (correlacionar Cn² entre cámara,
fibras y anemómetro con resolución de microsegundos) hace falta un único eje
temporal. La regla en NECTAR es:

1. **El reloj monotónico del host manda.** Cada muestra de cada dispositivo se
   estampa, al llegar, con ``time.perf_counter_ns()``. Es monotónico, no lo
   afecta NTP ni el cambio de hora, y es barato de leer.
2. **T0 es el instante del disparo síncrono global.** Lo fija la pestaña 1 con
   ``mark_zero()`` y queda escrito en el manifiesto de la sesión. Todos los
   tiempos relativos del dataset se miden desde ahí.
3. **UTC entra como un ancla, no como base.** Cuando el IMU recibe una trama
   0xBBDD válida junto a un PPS, llama a ``set_utc_anchor(unix_ts, host_ns)``:
   eso registra el par (época UTC, reloj del host) y permite convertir
   *cualquier* marca monotónica a UTC a posteriori, con el mismo offset para
   todos los instrumentos.

Así queda explícito lo que en la especificación original estaba implícito: el
reloj del host es la columna vertebral y el GPS aporta el offset absoluto.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class UtcAnchor:
    """Par (época UTC, reloj monotónico del host) en el que se tomó."""

    unix_ts: float
    host_ns: int
    source: str = "gps_pps"

    def to_utc(self, host_ns: int) -> float:
        """Convierte una marca monotónica del host a segundos Unix."""
        return self.unix_ts + (host_ns - self.host_ns) / 1e9


class TimeBase:
    """Reloj canónico compartido por todos los dispositivos.

    Es seguro llamarlo desde cualquier hilo: lo único mutable está protegido
    por un cerrojo y las lecturas de ``now_ns`` no lo tocan.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._t0_ns: Optional[int] = None
        self._anchor: Optional[UtcAnchor] = None
        #: Hora de pared del arranque del programa, solo para nombrar archivos.
        self.boot_unix = time.time()
        self.boot_host_ns = time.perf_counter_ns()

    # ------------------------------------------------------------------
    # Lectura del reloj (camino crítico: tiene que ser barato)
    # ------------------------------------------------------------------
    @staticmethod
    def now_ns() -> int:
        """Marca monotónica del host, en nanosegundos."""
        return time.perf_counter_ns()

    # ------------------------------------------------------------------
    # T0 del disparo síncrono
    # ------------------------------------------------------------------
    def mark_zero(self) -> int:
        """Fija T0 y lo devuelve. Lo llama el orquestador de la pestaña 1."""
        with self._lock:
            self._t0_ns = time.perf_counter_ns()
            return self._t0_ns

    def clear_zero(self) -> None:
        with self._lock:
            self._t0_ns = None

    @property
    def t0_ns(self) -> Optional[int]:
        with self._lock:
            return self._t0_ns

    def rel_s(self, host_ns: Optional[int] = None) -> float:
        """Segundos desde T0. Si no hay T0, desde el arranque del programa."""
        host_ns = time.perf_counter_ns() if host_ns is None else host_ns
        with self._lock:
            origin = self._t0_ns if self._t0_ns is not None else self.boot_host_ns
        return (host_ns - origin) / 1e9

    # ------------------------------------------------------------------
    # Ancla UTC (la aporta el GPS a través del IMU)
    # ------------------------------------------------------------------
    def set_utc_anchor(self, unix_ts: float, host_ns: int,
                       source: str = "gps_pps") -> None:
        """Registra el par (UTC, reloj del host) del primer PPS válido.

        Solo se guarda el primero de la sesión: reanclar a mitad de camino
        introduciría un salto en el eje temporal del dataset. La deriva del
        oscilador se estudia después, comparando los PPS sucesivos con el
        reloj del host (eso lo hace la pestaña 9).
        """
        with self._lock:
            if self._anchor is None:
                self._anchor = UtcAnchor(unix_ts=unix_ts, host_ns=host_ns,
                                         source=source)

    def force_utc_anchor(self, unix_ts: float, host_ns: int,
                         source: str = "manual") -> None:
        """Reemplaza el ancla. Solo para corregir a mano antes de grabar."""
        with self._lock:
            self._anchor = UtcAnchor(unix_ts=unix_ts, host_ns=host_ns,
                                     source=source)

    @property
    def anchor(self) -> Optional[UtcAnchor]:
        with self._lock:
            return self._anchor

    def to_utc(self, host_ns: int) -> Optional[float]:
        """Marca monotónica -> segundos Unix, o None si aún no hay ancla."""
        anchor = self.anchor
        return None if anchor is None else anchor.to_utc(host_ns)

    def utc_available(self) -> bool:
        return self.anchor is not None

    # ------------------------------------------------------------------
    def snapshot(self) -> dict:
        """Estado del reloj para escribirlo en el manifiesto de la sesión."""
        with self._lock:
            t0 = self._t0_ns
            anchor = self._anchor
        data = {
            "host_clock": "time.perf_counter_ns (monotonico)",
            "t0_host_ns": t0,
            "boot_unix": self.boot_unix,
            "boot_host_ns": self.boot_host_ns,
            "utc_anchor": None,
        }
        if anchor is not None:
            data["utc_anchor"] = {
                "unix_ts": anchor.unix_ts,
                "host_ns": anchor.host_ns,
                "source": anchor.source,
            }
            if t0 is not None:
                data["t0_utc"] = anchor.to_utc(t0)
        return data

    def reset(self) -> None:
        """Borra T0 y el ancla. Se llama al empezar una sesión nueva."""
        with self._lock:
            self._t0_ns = None
            self._anchor = None


#: Instancia única. Todo el programa importa esta, no crea otra.
TIME_BASE = TimeBase()
