"""Catalogo de parametros de la camara que se exponen en la interfaz.

Nada de rangos ni de resoluciones fijas en el codigo: cada parametro se
descubre en tiempo de ejecucion y sus limites (minimo, maximo, incremento) se
leen de la propia camara. Asi el programa funciona igual con una HB-1800-S que
con cualquier otro modelo EVT, y no hay valores recortados en silencio.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List

from .api import Camera, PixelFormat
from ._ffi import EvtError

# Grupos, en el orden en que aparecen en el panel de configuracion.
GROUP_ACQUISITION = "Adquisicion"
GROUP_ROI = "Region de interes (ROI)"
GROUP_FORMAT = "Formato"
GROUP_TRIGGER = "Disparo"
GROUP_AUTO = "Exposicion automatica"
GROUP_ADVANCED = "Avanzado"

GROUP_ORDER = [
    GROUP_ACQUISITION,
    GROUP_ROI,
    GROUP_FORMAT,
    GROUP_TRIGGER,
    GROUP_AUTO,
    GROUP_ADVANCED,
]


@dataclass(frozen=True)
class ParamSpec:
    """Descripcion de un parametro ajustable."""

    name: str            # nombre GenICam
    label: str           # etiqueta en la interfaz
    kind: str            # 'uint32' | 'int32' | 'bool' | 'enum'
    group: str
    unit: str = ""
    #: Cambiarlo obliga a reconfigurar los buffers -> se reinicia el stream.
    restart: bool = False
    tooltip: str = ""


#: Parametros candidatos. Se filtran contra la camara real al conectar.
CATALOG: List[ParamSpec] = [
    # --- Adquisicion: ajustables en vivo, sin cortar el stream --------------
    ParamSpec("FrameRate", "Tasa de captura", "uint32", GROUP_ACQUISITION, "fps",
              tooltip="Frames por segundo que entrega la camara. El maximo "
                      "depende del ROI y de la exposicion."),
    ParamSpec("Exposure", "Exposicion", "uint32", GROUP_ACQUISITION, "us",
              tooltip="Tiempo de integracion en microsegundos. Limita la tasa "
                      "maxima de captura."),
    ParamSpec("Gain", "Ganancia", "uint32", GROUP_ACQUISITION,
              tooltip="Ganancia analogica del sensor."),
    ParamSpec("Offset", "Offset (nivel de negro)", "uint32", GROUP_ACQUISITION),

    # --- ROI: requiere reiniciar el stream ---------------------------------
    ParamSpec("Width", "Ancho", "uint32", GROUP_ROI, "px", restart=True,
              tooltip="Recortar el ancho en la camara reduce el ancho de banda "
                      "y sube la tasa maxima de captura."),
    ParamSpec("Height", "Alto", "uint32", GROUP_ROI, "px", restart=True,
              tooltip="Recortar el alto en la camara sube la tasa maxima de captura."),
    ParamSpec("OffsetX", "Desplazamiento X", "uint32", GROUP_ROI, "px", restart=True),
    ParamSpec("OffsetY", "Desplazamiento Y", "uint32", GROUP_ROI, "px", restart=True),

    # --- Formato -----------------------------------------------------------
    ParamSpec("PixelFormat", "Formato de pixel", "enum", GROUP_FORMAT, restart=True,
              tooltip="Mono8 = 1 byte/px. Mono12 = 2 bytes/px. "
                      "Mono12Packed = 1,5 bytes/px (25% menos de datos)."),

    # --- Disparo -----------------------------------------------------------
    ParamSpec("AcquisitionMode", "Modo de adquisicion", "enum", GROUP_TRIGGER, restart=True),
    ParamSpec("TriggerMode", "Modo de disparo", "enum", GROUP_TRIGGER, restart=True,
              tooltip="Off = flujo libre a la tasa configurada. "
                      "On = la camara espera un disparo."),
    ParamSpec("TriggerSource", "Fuente de disparo", "enum", GROUP_TRIGGER, restart=True,
              tooltip="Software o Hardware (GPIO)."),
    ParamSpec("AcquisitionFrameCount", "Frames por disparo", "uint32", GROUP_TRIGGER,
              restart=True),

    # --- Exposicion automatica --------------------------------------------
    ParamSpec("AutoExposure", "Exposicion automatica", "bool", GROUP_AUTO),
    ParamSpec("AutoExpSet", "Consigna de auto exposicion", "uint32", GROUP_AUTO,
              tooltip="Nivel de gris objetivo, en 10 bits."),
    ParamSpec("AutoExpIGain", "Velocidad de auto exposicion", "uint32", GROUP_AUTO),

    # --- Avanzado ----------------------------------------------------------
    ParamSpec("LineTime", "Tiempo de linea", "uint32", GROUP_ADVANCED, "ciclos",
              tooltip="Ralentiza la lectura linea a linea. Poner al minimo "
                      "salvo que el host no alcance a recibir."),
    ParamSpec("GevSCPSPacketSize", "Tamano de paquete", "uint32", GROUP_ADVANCED, "bytes",
              restart=True,
              tooltip="Jumbo frames. Conviene el maximo que soporte la NIC."),
    ParamSpec("StreamBytesPerSecond", "Ancho de banda", "uint32", GROUP_ADVANCED, "B/s",
              tooltip="Limita el caudal que emite la camara."),
]


#: Informacion de solo lectura que se muestra en la pestana "Informacion".
INFO_STRINGS = [
    ("DeviceModelName", "Modelo"),
    ("DeviceVendorName", "Fabricante"),
    ("DeviceSerialNumber", "Numero de serie"),
    ("DeviceVersion", "Version"),
    ("DeviceFirmwareVersion", "Firmware"),
]

#: Temperatura del sensor, en grados Celsius (entero con signo).
SENSOR_TEMP_PARAM = "SensTemp"


def supported_params(camera: Camera) -> List[ParamSpec]:
    """Filtra el catalogo dejando solo lo que esta camara soporta de verdad."""
    available: List[ParamSpec] = []
    for spec in CATALOG:
        try:
            if spec.kind == "uint32":
                camera.get_uint32(spec.name)
            elif spec.kind == "int32":
                camera.get_int32(spec.name)
            elif spec.kind == "bool":
                camera.get_bool(spec.name)
            elif spec.kind == "enum":
                camera.get_enum(spec.name)
            else:
                continue
        except EvtError:
            continue
        available.append(spec)
    return available


def read_device_info(camera: Camera) -> List[tuple]:
    """Lee los campos informativos disponibles como pares (etiqueta, valor)."""
    rows: List[tuple] = []
    for name, label in INFO_STRINGS:
        try:
            rows.append((label, camera.get_string(name)))
        except EvtError:
            continue
    rows.append(("IP", camera.info.ip))
    rows.append(("MAC", camera.info.mac))
    return rows


def read_sensor_temperature(camera: Camera):
    """Temperatura del sensor en Celsius, o None si la camara no la expone."""
    try:
        return camera.get_int32(SENSOR_TEMP_PARAM)
    except EvtError:
        return None


def maximize_packet_size(camera: Camera) -> int | None:
    """Sube GevSCPSPacketSize al maximo que reporte la camara.

    Sin jumbo frames el enlace de 25 GbE no rinde. Devuelve el valor aplicado
    o None si el parametro no existe.
    """
    try:
        info = camera.param_info("GevSCPSPacketSize")
        camera.set_uint32("GevSCPSPacketSize", info.maximum)
        return camera.get_uint32("GevSCPSPacketSize")
    except EvtError:
        return None


def bits_per_pixel(fmt: PixelFormat) -> int:
    """Bits por pixel del formato, para calcular memoria y ancho de banda."""
    name = fmt.name
    if name.endswith("_PACKED"):
        if "12" in name:
            return 12
        if "10" in name:
            return 10
    if "12" in name:
        return 16      # Mono12 / Bayer12 se transmiten en 2 bytes
    if "10" in name:
        return 16
    if name.startswith(("RGB", "BGR")):
        return 24
    return 8
