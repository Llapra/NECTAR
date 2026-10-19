"""Cámara de fotogramas ultrarrápida Emergent Vision Technologies (25GigE).

La capa ``_ffi`` / ``api`` / ``params`` / ``demo`` viene portada intacta de
``pastam_cam``, que ya estaba validada contra ``EmergentCameraC.dll`` real.
Lo nuevo es ``worker.py``, que la envuelve en la interfaz ``DeviceWorker`` y
cambia la grabación de "todo a RAM y volcar al final" por streaming directo
al SSD.
"""

from .worker import EmergentWorker, discover_emergent

__all__ = ["EmergentWorker", "discover_emergent"]
