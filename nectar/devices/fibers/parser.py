"""Decodificador del flujo binario del acondicionador de fibras.

El equipo empuja muestras de 8 bytes sin separador ni longitud: hay que
llevar un buffer propio, consumir de a 8 y quedarse con el resto para el
siguiente paquete TCP. Eso es todo lo que hace esta clase, más dos cosas que
el cliente en C++ no hacía y aquí importan:

1. **Estampar cada muestra con el reloj del host** en el momento de leerla del
   socket. Sin eso no hay forma de alinear las fibras con las cámaras.
2. **Descubrir la topología sola**: qué módulos y canales están realmente
   publicando. Así la pestaña 5 puede ser "modular aditiva" de verdad, sin que
   nadie configure cuántas fibras hay.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import numpy as np

from . import protocol


@dataclass
class ChannelState:
    """Última lectura y estadística de un canal (una fibra)."""

    index: int
    slot: int
    channel: int
    label: str = ""
    last_value: float = float("nan")
    samples: int = 0
    last_host_ns: int = 0
    #: Muestras del último segundo, para mostrar la tasa real de captación.
    rate_hz: float = 0.0
    minimum: float = float("inf")
    maximum: float = float("-inf")
    _sum: float = 0.0

    def update(self, value: float, host_ns: int) -> None:
        self.last_value = value
        self.last_host_ns = host_ns
        self.samples += 1
        self._sum += value
        if value < self.minimum:
            self.minimum = value
        if value > self.maximum:
            self.maximum = value

    @property
    def mean(self) -> float:
        return self._sum / self.samples if self.samples else float("nan")

    def as_dict(self) -> Dict[str, object]:
        return {
            "indice": self.index,
            "modulo": self.slot,
            "canal": self.channel,
            "etiqueta": self.label,
            "valor": self.last_value,
            "muestras": self.samples,
            "hz": round(self.rate_hz, 1),
            "min": None if self.minimum == float("inf") else self.minimum,
            "max": None if self.maximum == float("-inf") else self.maximum,
        }


class FiberParser:
    """Mantiene el buffer de bytes y el estado de todos los canales."""

    def __init__(self) -> None:
        self._buffer = bytearray()
        self.channels: Dict[int, ChannelState] = {}
        self.total_samples = 0
        self.malformed = 0

    # ------------------------------------------------------------------
    def feed(self, block: bytes, host_ns: int
             ) -> List[Tuple[int, float, int]]:
        """Consume bytes y devuelve ``[(indice_canal, valor, host_ns)]``.

        Los bytes que no completan una muestra quedan en el buffer para el
        próximo bloque: es la parte que, mal hecha, desalinea todo el flujo.
        """
        self._buffer.extend(block)
        decoded: List[Tuple[int, float, int]] = []

        size = protocol.PACKET_SIZE
        while len(self._buffer) >= size:
            slot, _pad1, channel, _pad2, value = protocol.PACKET.unpack_from(
                self._buffer, 0)
            del self._buffer[:size]

            # Un slot 0 o un canal fuera de rango significa desalineación:
            # se descarta un byte y se vuelve a probar.
            if slot < 1 or channel < 1 or channel > 2 or slot > 64:
                self.malformed += 1
                continue

            index = protocol.channel_index(slot, channel)
            state = self.channels.get(index)
            if state is None:
                state = ChannelState(index=index, slot=slot, channel=channel,
                                     label=protocol.channel_label(slot, channel))
                self.channels[index] = state
            state.update(float(value), host_ns)
            self.total_samples += 1
            decoded.append((index, float(value), host_ns))

        return decoded

    # ------------------------------------------------------------------
    def refresh_rates(self, window_s: float) -> None:
        """Recalcula la tasa por canal al cerrar cada ventana de estadística."""
        if window_s <= 0:
            return
        for state in self.channels.values():
            delta = state.samples - getattr(state, "_last_count", 0)
            state.rate_hz = delta / window_s
            setattr(state, "_last_count", state.samples)

    def ordered(self) -> List[ChannelState]:
        return [self.channels[key] for key in sorted(self.channels)]

    def snapshot(self) -> List[Dict[str, object]]:
        return [state.as_dict() for state in self.ordered()]

    def values_array(self) -> np.ndarray:
        return np.array([state.last_value for state in self.ordered()],
                        dtype=np.float64)

    def labels(self) -> List[str]:
        return [state.label for state in self.ordered()]

    def reset(self) -> None:
        self._buffer.clear()
        self.channels.clear()
        self.total_samples = 0
        self.malformed = 0


# ---------------------------------------------------------------------------
def structure_function(values: np.ndarray, positions: Optional[np.ndarray] = None
                       ) -> Dict[str, float]:
    """Función de estructura térmica entre sondas: D_T(r) = <[T(x)-T(x+r)]²>.

    Es el diagnóstico que pide el dossier: si dentro del volumen confinado de
    la cúpula existe el subrango inercial de Kolmogorov, D_T(r) debe crecer
    como r^(2/3). Con pocas sondas no alcanza para ajustar el exponente, pero
    sí para ver el orden de magnitud de las diferencias térmicas, que es lo
    que se quiere vigilar en vivo.
    """
    result: Dict[str, float] = {}
    finite = values[np.isfinite(values)]
    if finite.size < 2:
        return result
    for lag in range(1, finite.size):
        differences = finite[lag:] - finite[:-lag]
        result[f"D_T_lag{lag}"] = float(np.mean(differences ** 2))
    result["desv_std_C"] = float(np.std(finite))
    result["rango_C"] = float(np.max(finite) - np.min(finite))
    return result
