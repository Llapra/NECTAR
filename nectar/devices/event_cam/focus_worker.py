"""Cálculo de enfoque fuera del lazo de adquisición.

Por qué esto es un hilo aparte
------------------------------
En la primera versión, la DFT 2D del enfoque se calculaba **dentro** del bucle
que consume el iterador de eventos. Con un sensor VGA eso son unos pocos
milisegundos por evaluación, y a 10 Hz parece inofensivo; pero el iterador de
Metavision no espera a nadie: mientras el hilo está en la FFT, los eventos se
acumulan en el buffer del SDK. Con la cámara tranquila no se nota; con la
cámara entregando 20 MEv/s y el disco grabando, sí. Y lo peor es que se
notaría justo cuando se está enfocando antes de una toma, que es cuando hace
falta que la cámara vaya fina.

Aquí el lazo de adquisición solo hace una cosa: si el calculador está libre,
copia el bloque de eventos a un buzón de un solo hueco y sigue. Si está
ocupado, no copia nada y la muestra se pierde, que es exactamente lo correcto
—el enfoque necesita muestras recientes, no todas—. Todo el resto (contar
polaridades, acumular el cuadro, la FFT anular, el observador cinemático)
ocurre en este hilo, que puede tardar lo que quiera sin estorbar.

El propio método ayuda: en etapa gruesa la métrica es la razón de polaridad,
que es contar, y la FFT ni se enciende. Solo al entrar en la vecindad del foco
se empieza a pagar el espectro.
"""

from __future__ import annotations

import threading
import time
from typing import Any, Callable, Dict, Optional

import numpy as np

from ...core.algorithms import focus_metrics
from ...core.algorithms.jit_render import accumulate_counts


class FocusComputer:
    """Hilo que convierte bloques de eventos en consejos de enfoque."""

    def __init__(self, width: int, height: int,
                 on_report: Optional[Callable[[Dict[str, Any]], None]] = None,
                 target_hz: float = 20.0) -> None:
        self.width = int(width)
        self.height = int(height)
        self.on_report = on_report
        self.target_period = 1.0 / max(1.0, float(target_hz))

        self.engine = focus_metrics.FocusEngine()

        self._slot: Optional[tuple] = None
        self._lock = threading.Lock()
        self._ready = threading.Condition(self._lock)
        self._busy = False
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._next_accept = 0.0

        self._counts = np.zeros((self.height, self.width), dtype=np.float32)
        self.last_report: Optional[focus_metrics.FocusReport] = None
        self.last_frame: Optional[np.ndarray] = None

    # ------------------------------------------------------------------
    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="focus-calc",
                                        daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        with self._lock:
            self._ready.notify_all()
        if self._thread is not None:
            self._thread.join(2.0)
            self._thread = None

    def set_geometry(self, width: int, height: int) -> None:
        if (width, height) == (self.width, self.height):
            return
        self.width, self.height = int(width), int(height)
        self._counts = np.zeros((self.height, self.width), dtype=np.float32)
        self.engine.reset()

    def set_motorized(self, motorized: bool) -> None:
        self.engine.set_motorized(motorized)

    def reset(self) -> None:
        self.engine.reset()

    # ------------------------------------------------------------------
    def wants_sample(self, now: Optional[float] = None) -> bool:
        """True si conviene entregarle un bloque. Lo pregunta la adquisición.

        Dos filtros: que el calculador esté libre y que haya pasado el período
        objetivo. El segundo evita copiar 40 veces por segundo cuando con 20
        alcanza para guiar una mano.
        """
        if self._thread is None:
            return False
        now = now if now is not None else time.perf_counter()
        if now < self._next_accept:
            return False
        with self._lock:
            return not self._busy

    def submit(self, events: np.ndarray, delta_t_s: float,
               now: Optional[float] = None) -> bool:
        """Entrega un bloque de eventos. Un memcpy y vuelve.

        La copia es obligatoria: el iterador de Metavision reutiliza su buffer
        en la próxima vuelta, así que guardar la referencia daría datos que
        cambian bajo los pies del calculador.
        """
        if events is None or len(events) == 0:
            return False
        now = now if now is not None else time.perf_counter()
        with self._lock:
            if self._busy:
                return False
            self._busy = True
            self._slot = (events.copy(), float(delta_t_s), now)
            self._next_accept = now + self.target_period
            self._ready.notify()
        return True

    # ------------------------------------------------------------------
    def _loop(self) -> None:
        while not self._stop.is_set():
            with self._lock:
                while self._slot is None and not self._stop.is_set():
                    self._ready.wait(0.2)
                if self._stop.is_set():
                    return
                payload, self._slot = self._slot, None
            try:
                self._process(*payload)
            except Exception:
                # Un bloque mal formado no puede tumbar el enfoque.
                pass
            finally:
                with self._lock:
                    self._busy = False

    def _process(self, events: np.ndarray, delta_t_s: float, now: float) -> None:
        polarity = events["p"]
        positives = int(np.count_nonzero(polarity))
        negatives = int(polarity.size - positives)

        hfe_db = float("-inf")
        frame = None
        if self.engine.needs_spectrum:
            # Solo en etapa fina se paga el espectro, como manda el método.
            self._counts.fill(0.0)
            accumulate_counts(self._counts, events["x"], events["y"])
            _, hfe_db = focus_metrics.hfe_energy_db(self._counts)
            frame = self._counts.copy()

        report = self.engine.update(now, positives, negatives,
                                    delta_t_s, hfe_db)
        self.last_report = report
        if frame is not None:
            self.last_frame = frame

        if self.on_report is not None:
            payload: Dict[str, Any] = report.as_dict()
            payload.update({
                "eventos": int(polarity.size),
                "positivos": positives,
                "negativos": negatives,
                "host_s": now,
                "frame": frame,
            })
            self.on_report(payload)
