"""Capa de hardware: un paquete por familia de instrumento.

Al importar este paquete se puebla el registro de tipos de dispositivo
(``nectar.core.device_registry.REGISTRY``). La pestaña 1 construye su matriz
recorriendo ese registro, así que **agregar un instrumento nuevo a NECTAR es
agregar una carpeta aquí y una línea en este archivo**: no hay que tocar la
ventana principal ni la lógica del disparo síncrono.
"""

from ..core.device_registry import REGISTRY, DeviceType
from .base import (ARMABLE_STATES, STATE_COLORS, STATE_LABELS, DeviceState,
                   DeviceWorker)


def _register_all() -> None:
    from .anemometer.worker import AnemometerWorker, discover_anemometers
    from .emergent.worker import EmergentWorker, discover_emergent
    from .event_cam.worker import EventCamWorker, discover_event_cams
    from .fibers.worker import FiberWorker, discover_fibers
    from .imu.worker import ImuWorker, discover_imu
    from .reconstruction.worker import (ReconstructionWorker,
                                        discover_reconstruction)

    REGISTRY.register(DeviceType(
        family="event_cam", label="EvCam",
        worker_factory=EventCamWorker, discover=discover_event_cams,
        throughput_mb_s=EventCamWorker.nominal_mb_s, icon="◉",
        notes="Cámara neuromórfica de eventos (Prophesee / Metavision)."))

    REGISTRY.register(DeviceType(
        family="emergent", label="Emergent",
        worker_factory=EmergentWorker, discover=discover_emergent,
        throughput_mb_s=EmergentWorker.nominal_mb_s, icon="▢",
        notes="Cámara de fotogramas HB-1800-S-M por 25GigE."))

    REGISTRY.register(DeviceType(
        family="reconstruction", label="Recons. EvCam",
        worker_factory=ReconstructionWorker, discover=discover_reconstruction,
        throughput_mb_s=ReconstructionWorker.nominal_mb_s, icon="◐",
        notes="Integrador de Euler sobre el flujo de eventos (video MP4)."))

    REGISTRY.register(DeviceType(
        family="fibers", label="Fibras",
        worker_factory=FiberWorker, discover=discover_fibers,
        throughput_mb_s=FiberWorker.nominal_mb_s, icon="≈",
        notes="Acondicionador Opsens CoreSens, hasta 1 kHz por canal."))

    REGISTRY.register(DeviceType(
        family="anemometer", label="Anemómetro",
        worker_factory=AnemometerWorker, discover=discover_anemometers,
        throughput_mb_s=AnemometerWorker.nominal_mb_s, icon="↝",
        notes="Termo-anemómetro ultrasónico ToF (en construcción)."))

    REGISTRY.register(DeviceType(
        family="imu", label="IMU-GPS",
        worker_factory=ImuWorker, discover=discover_imu,
        throughput_mb_s=ImuWorker.nominal_mb_s, icon="⌁",
        notes="ESP32 con IMU y GPS: vibración y ancla UTC por PPS."))


_register_all()

__all__ = [
    "REGISTRY", "DeviceType", "DeviceWorker", "DeviceState",
    "STATE_LABELS", "STATE_COLORS", "ARMABLE_STATES",
]
