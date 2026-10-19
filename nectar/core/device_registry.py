"""Registro de tipos de dispositivo.

Sin esto, agregar un instrumento obliga a tocar a mano ``main_window.py``, la
matriz de la pestaña 1 y la creación de pestañas. Con 4 dispositivos se
aguanta; con 6-8 (que es hacia dónde apunta el dossier: Emergent x2, EvCam x2,
fibras, anemómetro ToF, IMU/PPS) se vuelve frágil.

Aquí cada familia se registra una sola vez, declarando su etiqueta, su clase
de worker, su descubridor de dispositivos y cuánto caudal genera cada unidad.
La pestaña 1 construye su matriz recorriendo este registro, así que un
dispositivo nuevo aparece solo.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from .. import config


@dataclass
class DeviceType:
    """Todo lo que el resto del programa necesita saber de una familia."""

    family: str
    label: str
    worker_factory: Callable[..., Any]
    #: Devuelve una lista de descriptores de dispositivos disponibles.
    discover: Optional[Callable[[], List[Dict[str, Any]]]] = None
    #: Caudal aproximado por unidad, en MB/s. Alimenta el presupuesto de disco.
    throughput_mb_s: float = 0.0
    #: Si False, no participa del disparo síncrono global (p. ej. la
    #: reconstrucción, que es una vista derivada y no un sensor).
    recordable: bool = True
    icon: str = ""
    notes: str = ""
    extra: Dict[str, Any] = field(default_factory=dict)


class DeviceRegistry:
    """Catálogo de familias de dispositivo, en el orden en que se muestran."""

    def __init__(self) -> None:
        self._types: Dict[str, DeviceType] = {}

    def register(self, device_type: DeviceType) -> None:
        self._types[device_type.family] = device_type

    def get(self, family: str) -> Optional[DeviceType]:
        return self._types.get(family)

    def families(self) -> List[str]:
        """Familias registradas, respetando el orden de ``config``."""
        known = [f for f in config.FAMILY_ORDER if f in self._types]
        extra = [f for f in self._types if f not in known]
        return known + extra

    def types(self) -> List[DeviceType]:
        return [self._types[f] for f in self.families()]

    def discover(self, family: str) -> List[Dict[str, Any]]:
        """Descubre dispositivos de una familia. **Propaga los errores.**

        Antes esto devolvía una lista vacía ante cualquier excepción, y el
        motivo real —falta el SDK, no hay permisos sobre el puerto serie, la
        cámara la tomó otro proceso— se perdía. Quien llama decide qué hacer
        con el error, pero tiene que poder verlo.
        """
        device_type = self._types.get(family)
        if device_type is None or device_type.discover is None:
            return []
        return list(device_type.discover())

    def create_worker(self, family: str, *args, **kwargs):
        device_type = self._types.get(family)
        if device_type is None:
            raise KeyError(f"Familia de dispositivo no registrada: {family}")
        return device_type.worker_factory(*args, **kwargs)


#: Registro único del programa. ``nectar.devices`` lo puebla al importarse.
REGISTRY = DeviceRegistry()
