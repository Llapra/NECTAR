"""Protocolo del acondicionador de señal de fibra óptica (Opsens CoreSens).

Cómo está cableado
------------------
Las fibras no llegan al PC: llegan al acondicionador (el "controlador"), que
es el que interroga ópticamente cada sonda y publica las temperaturas ya
convertidas. El PC habla con ese equipo por Ethernet, con **dos canales
separados**:

* un canal de **control** en ASCII (2001), con ``measure:start`` y
  ``rate 0.001`` (período en segundos para 1 kHz);
* un canal de **datos** (2003), que pide ``Key?:``. Se responde con la clave
  anunciada al abrir control (``Key:2`` en el equipo del laboratorio).

Formato de cada muestra (8 bytes, tal como lo decodificaba el cliente en C++
del laboratorio)::

    offset 0  uint8   slot / módulo   (1..13)
    offset 1  uint8   relleno
    offset 2  uint8   canal           (1..2 dentro del módulo)
    offset 3  uint8   relleno
    offset 4  float32 valor, little-endian  (°C para las sondas OTG)

Tasas admitidas (datasheet IMP0160 CoreSens Rev3.8)
---------------------------------------------------
Cada módulo de doble canal corre hasta 1 kHz **por canal**:

* ``WLX-2``  (interferometría de luz blanca, WLPI): hasta **500 Hz**
* ``WLX-2S`` (WLPI, versión rápida): hasta **1000 Hz**
* ``GSX-2``  (banda prohibida de semiconductor, SCBG, sondas OTG): hasta **1000 Hz**

O sea: no se puede "forzar" cualquier frecuencia. El equipo acepta un conjunto
discreto y el tope depende del módulo instalado. Eso es lo que valida
``clamp_rate()``: si se pide más de lo que el módulo puede, se recorta al
máximo legal y se avisa, en vez de mandar un comando que el equipo va a
rechazar en silencio.

El equipo del laboratorio publica tres módulos de dos canales (seis fibras).
La frecuencia aplicada se confirma mediante ``rate?``; no se supone que un
comando enviado haya sido aceptado. Los comandos de módulo que se conservan
abajo no se usan para configurar este CoreSens2.
"""

from __future__ import annotations

import struct
from typing import Dict, List, Optional, Tuple

from ... import config

#: ``<B B B B f>`` = slot, relleno, canal, relleno, valor
PACKET = struct.Struct("<BBBBf")
PACKET_SIZE = PACKET.size          # 8 bytes

MODULE_TYPES = list(config.FIBER_MODULE_MAX_HZ.keys())


# ---------------------------------------------------------------------------
# Comandos de control
# ---------------------------------------------------------------------------
def cmd_start(num_data: int = 0) -> bytes:
    """Arranca la adquisición. ``num_data=0`` = infinita."""
    if num_data <= 0:
        return b"measure:start infi\n"
    return f"measure:start {int(num_data)}\n".encode()


def cmd_stop() -> bytes:
    return b"measure:stop\n"


def cmd_sampling_rate(hz: int, system: int = 1, module: int = 1) -> bytes:
    """Tasa de muestreo de un módulo concreto.

    El nombre del nodo SCPI lleva sistema y módulo pegados con dos dígitos
    cada uno: ``system0101`` es sistema 01, módulo 01.
    """
    return f"system{system:02d}{module:02d}:samplingrate {int(hz)}\n".encode()


def cmd_master_rate(hz: int) -> bytes:
    """Limita la tasa de publicación del maestro hacia el PC."""
    return f"systems:measure:rate {int(hz)}\n".encode()


def cmd_rate(hz: int) -> bytes:
    """CoreSens2: el comando rate recibe el período en segundos."""
    if hz <= 0:
        raise ValueError("La frecuencia debe ser positiva")
    return f"rate {1.0 / hz:.9g}\n".encode("ascii")


def cmd_rate_query() -> bytes:
    return b"rate?\n"


def cmd_status() -> bytes:
    return b"system:status?\n"


def cmd_identify() -> bytes:
    return b"*IDN?\n"


# ---------------------------------------------------------------------------
# Validación de tasas
# ---------------------------------------------------------------------------
def allowed_rates(module_type: str = config.FIBER_DEFAULT_MODULE) -> List[int]:
    """Tasas ofrecidas para ese tipo de módulo, de menor a mayor."""
    ceiling = config.FIBER_MODULE_MAX_HZ.get(module_type, 1000)
    return [hz for hz in config.FIBER_SAMPLING_RATES if hz <= ceiling]


def clamp_rate(hz: int, module_type: str = config.FIBER_DEFAULT_MODULE
                ) -> Tuple[int, Optional[str]]:
    """Ajusta la tasa pedida a la más cercana admitida.

    Devuelve ``(hz_valida, aviso)``. El aviso es ``None`` si la tasa pedida ya
    era legal; si no, explica qué se hizo y por qué, para que quede en el log
    de la pestaña y no sea una sorpresa silenciosa.
    """
    options = allowed_rates(module_type)
    if not options:
        return int(hz), f"Módulo desconocido: {module_type}"
    ceiling = options[-1]
    if hz > ceiling:
        return ceiling, (f"{hz} Hz supera el máximo del módulo {module_type} "
                         f"({ceiling} Hz): se usa {ceiling} Hz.")
    if hz in options:
        return int(hz), None
    nearest = min(options, key=lambda option: abs(option - hz))
    return nearest, (f"{hz} Hz no está en la lista admitida {options}: "
                     f"se usa {nearest} Hz.")


def channel_index(slot: int, channel: int) -> int:
    """Índice lineal 0-based de un canal a partir de (módulo, canal).

    Módulo 1 canal 1 -> 0, módulo 1 canal 2 -> 1, módulo 2 canal 1 -> 2…
    """
    return (int(slot) - 1) * config.FIBER_CHANNELS_PER_MODULE + (int(channel) - 1)


def channel_label(slot: int, channel: int) -> str:
    """Etiqueta legible, con el número de fibra que ve el operador."""
    return f"Fibra {channel_index(slot, channel) + 1:02d} (M{slot}·CH{channel})"


def slot_channel(index: int) -> Tuple[int, int]:
    """Inverso de ``channel_index``."""
    per = config.FIBER_CHANNELS_PER_MODULE
    return index // per + 1, index % per + 1


def describe_rates() -> Dict[str, List[int]]:
    """Tabla de tasas por módulo, para mostrarla en la pestaña."""
    return {module: allowed_rates(module) for module in MODULE_TYPES}
