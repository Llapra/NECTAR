"""Constantes globales de NECTAR.

Todo lo que se toca al cambiar de laboratorio, de cámara o de campaña vive
aquí, para no tener que buscarlo dentro de la lógica.

NECTAR · Neuromorphic & Emergent Camera Tomography for Atmospheric Research
Proyecto PASTAM · ANID FONDEF IDeA I+D · PUCV / ESO Paranal
"""

from __future__ import annotations

import os
from pathlib import Path

APP_NAME = "NECTAR"
APP_VERSION = "1.0.0"
APP_SUBTITLE = "Neuromorphic & Emergent Camera Tomography for Atmospheric Research"

# ---------------------------------------------------------------------------
# Rutas por defecto
# ---------------------------------------------------------------------------
#: Destino final de las sesiones. La pestaña 1 manda sobre esto.
DEFAULT_BASE_DIR = str(Path.home() / "NECTAR_Sesiones")

#: Carpeta de estado del programa: alias de dispositivos, registro rodante.
#: No guarda datos de medición, solo preferencias y bitácora.
STATE_DIR = Path(os.environ.get("NECTAR_STATE", Path.home() / ".nectar"))
ALIASES_FILE = STATE_DIR / "alias_dispositivos.json"
PROGRAM_LOG = STATE_DIR / "nectar.log"
#: Nombre del registro que se escribe dentro de cada carpeta de sesión.
SESSION_LOG_NAME = "nectar_sesion.log"

#: Nombre de la carpeta de cada familia de dispositivo dentro de la sesión.
FAMILY_DIRS = {
    "event_cam": "EvCam",
    "emergent": "Emergent",
    "fibers": "Fibras",
    "anemometer": "Anemometro",
    "imu": "IMU_GPS",
    "reconstruction": "Reconstruccion",
}

#: Etiqueta legible de cada familia (la que se ve en la pestaña 1).
FAMILY_LABELS = {
    "event_cam": "EvCam",
    "emergent": "Emergent",
    "fibers": "Fibras",
    "anemometer": "Anemómetro",
    "imu": "IMU-GPS",
    "reconstruction": "Recons. EvCam",
}

#: Orden en que se muestran las familias en la matriz de la pestaña 1.
FAMILY_ORDER = ["event_cam", "emergent", "reconstruction",
                "fibers", "anemometer", "imu"]

SESSION_MANIFEST = "session_manifest.json"

# ---------------------------------------------------------------------------
# Cámara de eventos (Prophesee / Metavision)
# ---------------------------------------------------------------------------
#: Biases heredados de main_v14.1.py. NO cambiar sin medir: son voltajes.
EVENT_BIAS_DEFAULTS = {
    "bias_diff": 299,
    "bias_diff_on": 374,
    "bias_diff_off": 221,
    "bias_refr": 1500,
}

#: (mínimo, máximo, ayuda) de cada bias, tal como estaban en el programa previo.
EVENT_BIAS_RANGES = {
    "bias_diff": (250, 350, "Nivel base de referencia. ¡Cuidado!"),
    "bias_diff_on": (300, 600, "Menor valor = más sensible a subidas de luz (ruido verde)"),
    "bias_diff_off": (100, 290, "Menor valor = más sensible a bajadas de luz (ruido rojo)"),
    "bias_refr": (1300, 1800, "Tiempo muerto del píxel tras un evento (µs)"),
}

EVENT_ACCUM_US_DEFAULT = 10000      # ventana de integración Δt
EVENT_ACCUM_US_RANGE = (200, 100000)
EVENT_DISPLAY_FPS_DEFAULT = 20
EVENT_DISPLAY_FPS_RANGE = (1, 200)
EVENT_SENSOR_SIZE = (640, 480)      # se corrige al conectar con el HAL real
EVENT_SYNC_MODES = ["STANDALONE", "MASTER", "SLAVE"]

#: Caudal crudo por modelo de sensor de eventos, en MB/s.
#:
#: Es el dato que alimenta el presupuesto de disco cuando la cámara todavía no
#: ha medido su propia tasa. ``tipico`` es lo que se ve mirando una escena de
#: cúpula con turbulencia; ``pico`` es el techo del enlace, que solo se alcanza
#: con la escena saturada de movimiento. Cada evento EVT3 ocupa ~4 bytes.
#:
#: La SilkyEvCam VGA (PPS3MVCD, Gen3.1 VGA) está limitada por el USB 3.0 y por
#: el propio lector del sensor: ~66 MEv/s de pico, que son ~265 MB/s, pero
#: sostenido por USB se queda bastante por debajo.
EVENT_MODEL_RATES = {
    "SilkyEvCam VGA": {"ancho": 640, "alto": 480, "enlace": "USB 3.0",
                       "mev_s_tipico": 15.0, "mev_s_pico": 66.0},
    "Gen4 HD (IMX636)": {"ancho": 1280, "alto": 720, "enlace": "USB 3.0",
                         "mev_s_tipico": 50.0, "mev_s_pico": 1066.0},
    "IMX637 Ethernet": {"ancho": 1280, "alto": 720, "enlace": "Ethernet",
                        "mev_s_tipico": 75.0, "mev_s_pico": 1066.0},
}
#: Modelo que se asume mientras el HAL no diga otra cosa.
EVENT_DEFAULT_MODEL = "SilkyEvCam VGA"
#: Bytes por evento en el archivo .raw (codificación EVT3).
EVENT_BYTES_PER_EVENT = 4.0
#: Bytes por evento del formato del simulador (x,y,p,t sin comprimir).
EVENT_SIM_BYTES_PER_EVENT = 12.0

# ---------------------------------------------------------------------------
# Cámara Emergent (25GigE)
# ---------------------------------------------------------------------------
#: Buffers encolados en la cámara. Más buffers = más tolerancia a que el host
#: se distraiga un instante sin perder frames.
EMERGENT_BUFFER_COUNT = 64
EMERGENT_FRAME_TIMEOUT_MS = 200
#: Slots del anillo de escritura a disco (por cámara). A 1604x1100 Mono12Packed
#: (~2,6 MB/frame) 256 slots son ~680 MB: sobra margen y nunca se asigna
#: memoria durante la ráfaga.
EMERGENT_RING_SLOTS = 256
#: Frames que el hilo escritor junta en una sola llamada a write().
EMERGENT_WRITE_BATCH = 32

# ---------------------------------------------------------------------------
# Fibras ópticas (Opsens CoreSens)
# ---------------------------------------------------------------------------
#: El controlador expone un canal de control (SCPI en ASCII) y otro de datos.
FIBER_DEFAULT_IP = "192.168.1.50"
FIBER_CONTROL_PORT = 50910
FIBER_DATA_PORT = 50911
#: Paquete de datos: [slot][relleno][canal][relleno][float32 little-endian]
FIBER_PACKET_SIZE = 8
#: Módulos de doble canal por chasis (hasta 13 en el rack de 4U).
FIBER_MAX_MODULES = 13
FIBER_CHANNELS_PER_MODULE = 2
#: Tasas de muestreo admitidas. Del datasheet IMP0160 CoreSens Rev3.8:
#: módulo WLX-2 hasta 500 Hz; WLX-2S y GSX-2 hasta 1000 Hz por canal.
FIBER_SAMPLING_RATES = [1, 10, 50, 100, 250, 500, 1000]
FIBER_MODULE_MAX_HZ = {"WLX-2": 500, "WLX-2S": 1000, "GSX-2": 1000}
FIBER_DEFAULT_MODULE = "GSX-2"

# ---------------------------------------------------------------------------
# Anemómetro ultrasónico ToF (en construcción)
# ---------------------------------------------------------------------------
#: El instrumento aún no existe: se deja la estructura y un simulador para
#: poder probar la pestaña y el disparo síncrono sin hardware.
ANEMO_DEFAULT_BAUD = 921600
ANEMO_BAUD_OPTIONS = ["115200", "921600", "2000000"]
ANEMO_DEFAULT_HZ = 400
ANEMO_HZ_RANGE = (1, 5000)
ANEMO_PACKET_SIZE = 16              # provisional, ajustar cuando exista el firmware

# ---------------------------------------------------------------------------
# IMU / GPS (ESP32)
# ---------------------------------------------------------------------------
IMU_PACKET_SIZE = 18
IMU_HEADER_IMU = 0xBBAA
IMU_HEADER_GPS = 0xBBCC
IMU_HEADER_COORD = 0xBBDD
IMU_HEADER_ATM = 0xFFEE             # sello de época UTC absoluta
IMU_ACCEL_SCALE = 4096.0            # LSB/g
IMU_GYRO_SCALE = 65.5               # LSB/(°/s)
IMU_BAUD_OPTIONS = ["921600", "115200", "2000000"]
IMU_DEFAULT_BAUD = 921600
IMU_WRITE_CHUNK = 8192              # bytes acumulados antes de tocar el disco

# ---------------------------------------------------------------------------
# Grabación
# ---------------------------------------------------------------------------
#: **Un archivo por toma y por dispositivo.** Sin cortes, sin costuras.
#:
#: Decisión tomada con el equipo para la campaña del VLT: se va una sola vez a
#: tomar datos y no hay margen para perder eventos en el corte de un bloque.
#: En la cámara de eventos ese corte es real (el HAL cierra un ``.raw`` y abre
#: otro, y entre las dos llamadas el sensor sigue disparando); en la Emergent
#: no lo es, porque el que escribe es NECTAR.
#:
#: El riesgo que queda —quedarse sin cierre limpio por corte de energía o
#: cuelgue— se cubre con ``CHECKPOINT_PERIOD_S`` y con el volcado incremental,
#: no partiendo el archivo.
SINGLE_FILE_DEFAULT = True

#: Duración de cada bloque *si* se activa el modo por bloques (apagado por
#: omisión). Existe por si alguna vez conviene, no para el uso normal.
CHUNK_OPTIONS = [
    ("10 Minutos", 600),
    ("15 Minutos", 900),
    ("30 Minutos", 1800),
    ("1 Hora", 3600),
    ("2 Horas", 7200),
]
CHUNK_DEFAULT_S = 1800

#: Cada cuánto se sella un punto de control en disco (offset, cuenta, reloj).
#: Si la toma muere sin cierre, esto dice hasta dónde el archivo es válido.
CHECKPOINT_PERIOD_S = 5.0

#: Margen de espacio libre por debajo del cual la sesión se detiene sola.
#: Acordado en 2 GB: alcanza para cerrar archivos, sellar el manifiesto y
#: dejar el sistema operativo respirando.
MIN_FREE_BYTES = 2 * 1024 ** 3
#: Cada cuánto se consulta el espacio libre durante la grabación.
FREE_SPACE_CHECK_PERIOD_S = 2.0

#: La Emergent reserva espacio por adelantado en tramos de este tamaño, para
#: que el archivo quede contiguo y para fallar temprano si el disco no da.
FALLOCATE_STEP_BYTES = 8 * 1024 ** 3

#: Sistemas de archivos que no admiten los tamaños que genera una toma larga.
#: FAT32 (``vfat``/``msdos``) corta a 4 GB sin aviso útil: se rechaza.
FILESYSTEMS_REJECTED = {"vfat", "msdos", "fat", "fat32"}
#: Sistemas de archivos de red o de usuario: funcionan, pero no al caudal de
#: una Emergent. Se avisa, no se bloquea.
FILESYSTEMS_WARNED = {"nfs", "nfs4", "cifs", "smbfs", "smb3", "fuseblk",
                      "sshfs", "ntfs", "ntfs3", "exfat"}

#: Estadísticas: cada cuánto los workers emiten su telemetría.
STATS_PERIOD_S = 0.5

#: Archivo que se deja en toda carpeta escrita en modo demostración.
DEMO_MARKER_NAME = "ESTO_ES_SIMULACION_NO_SON_DATOS.txt"
DEMO_MARKER_TEXT = (
    "Los archivos de esta carpeta NO provienen de instrumentos reales.\n"
    "Fueron generados por el modo de demostración de NECTAR (--demo) y solo\n"
    "sirven para ensayar la operación. No los uses para ningún análisis.\n")


def force_demo() -> bool:
    """True si se arrancó con ``--demo``.

    Fuera de ``--demo`` **no existen dispositivos simulados**: si el hardware
    no aparece, se muestra el error del hardware. Esto es deliberado. Un
    simulador que aparece solo puede pasar desapercibido en una noche de
    campaña y dejar una carpeta llena de datos inventados con aspecto de
    medición; es el peor fallo posible de un programa de adquisición.
    """
    return os.environ.get("NECTAR_FORCE_DEMO", "") not in ("", "0", "false")


def simulations_allowed() -> bool:
    """Único permiso para crear, conectar o escribir un dispositivo simulado."""
    return force_demo()
