"""Enumeración de puertos serie que no inventa instrumentos.

El problema que resuelve
------------------------
``serial.tools.list_ports.comports()`` en Linux devuelve los 32 puertos
heredados ``/dev/ttyS0`` … ``/dev/ttyS31`` que el núcleo crea siempre, haya o
no hardware detrás. Son restos del chipset 8250: existen como nodos de
dispositivo y no están conectados a nada.

Ofrecerlos como instrumentos es exactamente el fallo que este programa se
propone no cometer: en la pestaña 1 aparecían "IMU1 … IMU32" y "Anemometro1 …
Anemometro32", todos con aspecto de equipo disponible, sin que hubiera un solo
cable enchufado. Un operador apurado podría marcar uno y creer que está
midiendo.

El criterio
-----------
Un puerto se ofrece como instrumento si detrás hay **hardware identificable**:

* tiene identificador USB (``vid``/``pid``) — es el caso de todo lo que
  usamos: ESP32 con CP210x, CH340 o el USB-CDC nativo, y los FTDI;
* o su nombre delata un adaptador (``ttyUSB``, ``ttyACM``, ``usbserial``,
  ``usbmodem``, ``COM`` en Windows, ``rfcomm`` en Bluetooth).

Todo lo demás se oculta **y se dice cuántos se ocultaron y por qué**, que es
la diferencia entre filtrar y esconder. Si alguna vez hiciera falta un puerto
RS-232 de placa base de verdad, arrancar con ``NECTAR_TODOS_LOS_PUERTOS=1``
los devuelve todos.

Identidad estable
-----------------
Se devuelve también el número de serie USB del adaptador cuando existe. Eso es
lo que ancla el alias del equipo: ``/dev/ttyUSB0`` cambia si se enchufa en otro
puerto o si arranca en otro orden, pero el número de serie del CP210x no. Con
él, el IMU del pilar norte se sigue llamando igual en la próxima campaña.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from typing import List, Optional, Tuple

try:
    import serial                                   # type: ignore
    import serial.tools.list_ports                  # type: ignore
    HAS_SERIAL = True
except ImportError:
    HAS_SERIAL = False

#: Fragmentos de nombre que delatan un adaptador serie real.
USB_HINTS = ("ttyusb", "ttyacm", "usbserial", "usbmodem", "rfcomm", "ttyxrusb")

#: Prefijos de puertos heredados del chipset, que el núcleo crea siempre.
LEGACY_HINTS = ("ttys", "ttyprintk", "ttyama", "ttysc")


@dataclass
class PortInfo:
    """Un puerto serie con lo que se sabe del hardware que hay detrás."""

    device: str = ""
    description: str = ""
    hwid: str = ""
    manufacturer: str = ""
    serial_number: str = ""
    vid: Optional[int] = None
    pid: Optional[int] = None

    @property
    def has_hardware(self) -> bool:
        """True si detrás de este nodo hay un dispositivo identificable."""
        if self.vid is not None:
            return True
        name = os.path.basename(self.device).lower()
        if any(hint in name for hint in USB_HINTS):
            return True
        if sys.platform.startswith("win") and name.startswith("com"):
            return True
        return False

    @property
    def is_legacy(self) -> bool:
        name = os.path.basename(self.device).lower()
        return any(name.startswith(hint) for hint in LEGACY_HINTS)

    @property
    def label(self) -> str:
        """Texto para el desplegable: puerto y qué hay detrás."""
        pieces = [self.device]
        if self.description and self.description not in ("n/a", self.device):
            pieces.append(self.description)
        if self.serial_number:
            pieces.append(f"SN {self.serial_number}")
        return " · ".join(pieces)

    def as_descriptor(self, family: str, index: int, stem: str) -> dict:
        """Descriptor listo para ``DeviceManager.add``."""
        return {
            # La identidad se ancla al número de serie del adaptador cuando
            # existe; el puerto es solo dónde está enchufado hoy.
            "id": f"{family}_{self.serial_number or self.device}",
            "label": f"{stem}{index}",
            "port": self.device,
            "serial": self.serial_number,
            "descripcion": self.description,
            "fabricante": self.manufacturer,
            "vid": self.vid,
            "pid": self.pid,
            "simulated": False,
        }


def show_all_ports() -> bool:
    """True si el operador pidió ver también los puertos heredados."""
    return os.environ.get("NECTAR_TODOS_LOS_PUERTOS", "") not in ("", "0", "false")


def list_ports(include_all: Optional[bool] = None
               ) -> Tuple[List[PortInfo], int]:
    """Puertos con hardware detrás, y cuántos se ocultaron.

    Devuelve ``(puertos, ocultos)``. Levanta ``RuntimeError`` si no está
    pyserial, porque no poder mirar los puertos es distinto de que no haya
    ninguno, y quien llama tiene que poder decir cuál de las dos cosas pasa.
    """
    if not HAS_SERIAL:
        raise RuntimeError(
            "pyserial no está instalado en este intérprete, así que no se "
            "pueden listar los puertos serie. Instálalo con: "
            "pip install pyserial")

    include_all = show_all_ports() if include_all is None else include_all

    found: List[PortInfo] = []
    hidden = 0
    for entry in serial.tools.list_ports.comports():
        info = PortInfo(
            device=str(entry.device),
            description=str(getattr(entry, "description", "") or ""),
            hwid=str(getattr(entry, "hwid", "") or ""),
            manufacturer=str(getattr(entry, "manufacturer", "") or ""),
            serial_number=str(getattr(entry, "serial_number", "") or ""),
            vid=getattr(entry, "vid", None),
            pid=getattr(entry, "pid", None),
        )
        if include_all or info.has_hardware:
            found.append(info)
        else:
            hidden += 1
    found.sort(key=lambda p: p.device)

    # Los adaptadores baratos (CP2102 clonados, sobre todo) suelen venir todos
    # con el mismo número de serie de fábrica, tipo "0001". Si dos puertos lo
    # comparten, ese número deja de identificar nada: se descarta para esos
    # dos y la identidad vuelve a anclarse al puerto, que al menos es único
    # aquí y ahora. Dejarlo pasar haría que el programa creyera que dos
    # instrumentos distintos son el mismo.
    seen: dict = {}
    for info in found:
        if info.serial_number:
            seen.setdefault(info.serial_number, []).append(info)
    for serial_number, group in seen.items():
        if len(group) > 1:
            for info in group:
                info.serial_number = ""
    return found, hidden


def hidden_note(hidden: int) -> str:
    """Frase que explica qué se ocultó, para el registro de la pestaña."""
    if hidden <= 0:
        return ""
    return (f"Se ocultaron {hidden} puerto(s) serie heredados del chipset "
            f"(/dev/ttyS*, sin hardware detrás). Para verlos todos: "
            f"NECTAR_TODOS_LOS_PUERTOS=1 python main.py")


def no_ports_note() -> str:
    """Qué decirle al operador cuando no hay ningún puerto con hardware."""
    return ("No hay ningún puerto serie con hardware detrás. Enchufa el "
            "módulo y vuelve a pulsar DETECTAR. Si está enchufado y no "
            "aparece, suele ser permisos: en Linux hay que estar en el grupo "
            "'dialout' (sudo usermod -aG dialout $USER, y volver a entrar "
            "a la sesión).")
