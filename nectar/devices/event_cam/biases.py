"""Gestión de los biases del sensor de eventos.

Los biases son **voltajes de polarización** de los comparadores del píxel, no
parámetros de software. Cambiarlos mueve el umbral físico de detección, así
que los valores por defecto que trae ``config.EVENT_BIAS_DEFAULTS`` son los
que ya estaban calibrados en ``main_v14.1.py`` y no se tocan a la ligera.

Qué hace cada uno:

``bias_diff``
    Nivel de referencia del comparador diferencial. Es el punto medio entre
    los umbrales ON y OFF. Moverlo desbalancea la respuesta del píxel: se
    ajusta al final, y poco.

``bias_diff_on``
    Umbral para eventos de *subida* de luz. Bajarlo lo hace más sensible y
    aparece más ruido verde.

``bias_diff_off``
    Umbral para eventos de *bajada* de luz. Bajarlo genera más ruido rojo.

``bias_refr``
    Periodo refractario: el tiempo muerto del píxel después de disparar.
    Subirlo filtra ruido térmico y limita la tasa máxima por píxel.

En NECTAR todas las cámaras de evento comparten el mismo juego de biases (es
lo que se pidió: la pestaña 2 es aditiva pero los biases son globales), porque
en una medición tomográfica las cámaras deben responder igual para poder
comparar sus series.
"""

from __future__ import annotations

import threading
from typing import Callable, Dict, List, Optional

from ... import config

BIAS_NAMES: List[str] = list(config.EVENT_BIAS_DEFAULTS.keys())


class BiasSet:
    """Juego de biases compartido por todas las cámaras de eventos.

    Es observable: cada worker se suscribe y recibe los cambios para
    aplicarlos a su propio sensor.
    """

    def __init__(self, values: Optional[Dict[str, int]] = None) -> None:
        self._lock = threading.Lock()
        self._values: Dict[str, int] = dict(config.EVENT_BIAS_DEFAULTS)
        if values:
            self.update_many(values)
        self._listeners: List[Callable[[str, int], None]] = []

    # ------------------------------------------------------------------
    @staticmethod
    def clamp(name: str, value: int) -> int:
        """Recorta al rango admitido. Nunca se manda un voltaje fuera de rango."""
        low, high, _ = config.EVENT_BIAS_RANGES.get(name, (0, 4095, ""))
        return int(max(low, min(high, int(value))))

    @staticmethod
    def limits(name: str):
        return config.EVENT_BIAS_RANGES.get(name, (0, 4095, ""))

    # ------------------------------------------------------------------
    def get(self, name: str) -> int:
        with self._lock:
            return self._values.get(name, config.EVENT_BIAS_DEFAULTS.get(name, 0))

    def as_dict(self) -> Dict[str, int]:
        with self._lock:
            return dict(self._values)

    def set(self, name: str, value: int, notify: bool = True) -> int:
        value = self.clamp(name, value)
        with self._lock:
            if self._values.get(name) == value:
                return value
            self._values[name] = value
        if notify:
            self._notify(name, value)
        return value

    def update_many(self, values: Dict[str, int], notify: bool = True) -> None:
        for name, value in values.items():
            self.set(name, value, notify=notify)

    def reset(self) -> None:
        self.update_many(dict(config.EVENT_BIAS_DEFAULTS))

    # ------------------------------------------------------------------
    def subscribe(self, callback: Callable[[str, int], None]) -> None:
        self._listeners.append(callback)

    def unsubscribe(self, callback: Callable[[str, int], None]) -> None:
        if callback in self._listeners:
            self._listeners.remove(callback)

    def _notify(self, name: str, value: int) -> None:
        for callback in list(self._listeners):
            try:
                callback(name, value)
            except Exception:
                pass    # un worker caído no puede romper el resto


#: Juego de biases global del programa.
SHARED_BIASES = BiasSet()
