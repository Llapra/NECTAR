"""Capa CFFI sobre EmergentCameraC.dll.

Basado en el wrapper `EVT_Py` que trae el eSDK
(C:/EVT/eSDK/Examples/EVT_Py), extendido con las funciones que el wrapper
original no expone y que este programa necesita:

  * EVT_CameraGetUInt32ParamInc  -> paso/cuantizacion de los parametros
  * EVT_CameraGetInt32Param      -> SensTemp, PtpOffset
  * EVT_CameraGet/SetBoolParam   -> AutoExposure
  * EVT_CameraGetStringParam     -> modelo, firmware, numero de serie
  * EVT_CameraGetEnumParamRange  -> lista de opciones de un enum
  * campos timestamp / nsecs / stride de EmergentFrame

Se mantiene aparte del SDK instalado para no modificar C:/EVT.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Optional

from cffi import FFI

# --------------------------------------------------------------------------
# Codigos de error (emergenterrors.h). Los <256 vienen de errno.h de Windows.
# --------------------------------------------------------------------------
EVT_SUCCESS = 0
EVT_ERROR_AGAIN = 11          # EAGAIN: no hay mas frames en cola
EVT_ERROR_NOMEM = 12
EVT_ERROR_ENODEV = 19
EVT_ERROR_INVAL = 22
EVT_ERROR_TIMEDOUT = 138      # ETIMEDOUT en Windows

ERROR_NAMES = {
    0: "EVT_SUCCESS",
    2: "EVT_ENOENT (no existe el archivo o directorio)",
    3: "EVT_ERROR_SRCH",
    4: "EVT_ERROR_INTR",
    5: "EVT_ERROR_IO (error de E/S)",
    9: "EVT_ERROR_BADF",
    10: "EVT_ERROR_ECHILD (error al crear hilo)",
    11: "EVT_ERROR_AGAIN (reintentar / cola vacia)",
    12: "EVT_ERROR_NOMEM (sin memoria)",
    13: "EVT_ERROR_ACCES (sin permisos)",
    14: "EVT_ERROR_FAULT (direccion invalida)",
    19: "EVT_ERROR_ENODEV (no existe el dispositivo)",
    22: "EVT_ERROR_INVAL (argumento invalido)",
    27: "EVT_ERROR_FBIG (archivo demasiado grande)",
    103: "EVT_ERROR_ALREADY",
    119: "EVT_ERROR_NOBUFS (sin buffers)",
    138: "EVT_ERROR_TIMEDOUT (timeout)",
    260: "EVT_ERROR_NOT_SUPPORTED (no soportado)",
    270: "EVT_ERROR_DEVICE_CONNECTED_ALRD (la camara ya esta abierta)",
    271: "EVT_ERROR_DEVICE_NOT_CONNECTED (la camara no esta abierta)",
    272: "EVT_ERROR_DEVICE_LOST_CONNECTION (se perdio la conexion)",
    280: "EVT_ERROR_GENICAM_ERROR",
    281: "EVT_ERROR_GENICAM_NOT_MATCH (parametro no encontrado)",
    283: "EVT_ERROR_GENICAM_OUT_OF_RANGE (valor fuera de rango)",
    290: "EVT_ERROR_SOCK (fallo de socket)",
    300: "EVT_ERROR_GVCP_ACK",
    310: "EVT_ERROR_GVSP_DATA_CORRUPT (datos corruptos en el stream)",
    340: "EVT_ERROR_NIC_LIB_INIT (fallo al iniciar la libreria de la NIC)",
    500: "EVT_ERROR_OS_OBTAIN_ADAPTER",
    600: "EVT_ERROR_SDK",
    0x7FFFFFFF: "EVT_GENERAL_ERROR",
}

# Constantes del eSDK
EVT_FRAME_BUFFER_ZERO_COPY = 1
EVT_INFINITE = -1
ENUM_STR_MAX_SIZE = 512


class EvtError(Exception):
    """Error devuelto por una funcion del eSDK."""

    def __init__(self, func: str, code: int):
        self.func = func
        self.code = code
        name = ERROR_NAMES.get(code, "desconocido")
        super().__init__(f"{func}: error {code} - {name}")


_CDEF_DEVICE_INFO = """
    const unsigned int IPV4_BYTES_SIZE = 16;
    const unsigned int NIC_FRIENDLY_NAME_LEN = 128;
    const unsigned int NIC_DESCRIPTION_LEN = 128;
    const unsigned int NIC_ADAPTER_NAME_LEN = 128;

    typedef struct
    {
        unsigned int vendorID;
        const char* name;
        const char* fullName;
    } NicVendorInfo;

    const unsigned int NIC_VENDOR_DATA_SIZE = 1024;

    typedef struct
    {
        char ip4Address[IPV4_BYTES_SIZE];
        char ip4AddressBroadcast[IPV4_BYTES_SIZE];
        char friendlyName[NIC_FRIENDLY_NAME_LEN];
        char description[NIC_DESCRIPTION_LEN];
        char adapterName[NIC_ADAPTER_NAME_LEN];
        unsigned int subnetMask;
        const struct NicVendorInfo* nicVendorInfo;
        unsigned char vendorData[NIC_VENDOR_DATA_SIZE];
    } NetworkInterfaceController;

    typedef struct
    {
        char currentIp[16];
        char macAddress[18];
        char serialNumber[16];
    } LabviewDeviceInfo;

    typedef struct
    {
        int countDevice;
        uint64_t privateData;
    } LabviewDeviceSet;
"""

_CDEF_SYSTEM = """
    typedef void* Camera_Handle;

    typedef struct {
        bool disableHeartbeat;
        unsigned long long lineReorderCPUMask;
        int gpuDirectDeviceId;
    } CameraOpenParams;

    void EVT_CameraOpenParams_Init(CameraOpenParams* params);

    int EVT_ListDevices(LabviewDeviceSet* labviewDeviceSet);
    int EVT_GetDeviceInfo(LabviewDeviceSet* labviewDeviceSet,
                          LabviewDeviceInfo* labviewDeviceInfo, int index);
    void EVT_DeviceInfoSetCleanup(LabviewDeviceSet* labviewDeviceSet);

    int EVT_CameraOpen(Camera_Handle* cameraHandle,
                       LabviewDeviceSet* labviewDeviceSet, int index);
    int EVT_CameraOpen_Ex(Camera_Handle* cameraHandle,
                          LabviewDeviceSet* labviewDeviceSet, int index,
                          const CameraOpenParams* params);
    int EVT_CameraClose(Camera_Handle cameraHandle);
"""

_CDEF_CAMERA = """
    typedef struct EmergentFrame
    {
        int pixel_type;
        unsigned int size_x;
        unsigned int size_y;
        int stride;
        unsigned int offset_x;
        unsigned int offset_y;
        unsigned int padding_x;
        unsigned int padding_y;
        unsigned int trailer_size_y;
        unsigned short frame_id;
        unsigned char* imagePtr;
        unsigned int bufferSize;
        unsigned long long timestamp;
        unsigned long long nsecs;
        int convertColor;
        int convertBitDepth;
        unsigned char privateData[256];
    } EmergentFrame;

    int EVT_CameraOpenStream(Camera_Handle camera);
    int EVT_CameraCloseStream(Camera_Handle camera);

    int EVT_AllocateFrameBuffer(Camera_Handle camera, EmergentFrame* frame, int bufferType);
    int EVT_ReleaseFrameBuffer(Camera_Handle camera, EmergentFrame* frame);
    int EVT_CameraQueueFrame(Camera_Handle camera, EmergentFrame* frame);
    int EVT_CameraGetFrame(Camera_Handle camera, EmergentFrame* frame, int timeout);

    int EVT_CameraGetUInt32Param(Camera_Handle camera, const char* name, unsigned int* val);
    int EVT_CameraSetUInt32Param(Camera_Handle camera, const char* name, unsigned int val);
    int EVT_CameraGetUInt32ParamMax(Camera_Handle camera, const char* name, unsigned int* max);
    int EVT_CameraGetUInt32ParamMin(Camera_Handle camera, const char* name, unsigned int* min);
    int EVT_CameraGetUInt32ParamInc(Camera_Handle camera, const char* name, unsigned int* inc);

    /* Nota: EVT_CameraSetInt32Param esta declarada en EmergentCameraC.h pero
       NO se exporta en la DLL, asi que no se declara aqui. Los parametros
       con signo que usa este programa (SensTemp, PtpOffset) son de solo
       lectura. */
    int EVT_CameraGetInt32Param(Camera_Handle camera, const char* name, int* val);
    int EVT_CameraGetInt32ParamMax(Camera_Handle camera, const char* name, int* max);
    int EVT_CameraGetInt32ParamMin(Camera_Handle camera, const char* name, int* min);
    int EVT_CameraGetInt32ParamInc(Camera_Handle camera, const char* name, unsigned int* inc);

    int EVT_CameraGetBoolParam(Camera_Handle camera, const char* name, unsigned char* val);
    int EVT_CameraSetBoolParam(Camera_Handle camera, const char* name, unsigned char val);

    int EVT_CameraGetStringParam(Camera_Handle camera, const char* name, char* buffer,
                                 unsigned long bufferSize, unsigned long* valueSize);

    int EVT_CameraGetStringEnumParam(Camera_Handle camera, const char* name, char* buffer,
                                     unsigned long bufferSize, unsigned long* valueSize);
    int EVT_CameraSetEnumParam(Camera_Handle camera, const char* name, const char* buffer);
    int EVT_CameraGetValueEnumParam(Camera_Handle camera, const char* name, unsigned int* value);
    int EVT_CameraGetEnumParamRange(Camera_Handle camera, const char* name, char* buffer,
                                    unsigned long bufferSize, unsigned long* valueSize);

    int EVT_CameraExecuteCommand(Camera_Handle camera, const char* name);
"""


def build_ffi() -> FFI:
    ffi = FFI()
    ffi.cdef(_CDEF_DEVICE_INFO)
    ffi.cdef(_CDEF_SYSTEM)
    ffi.cdef(_CDEF_CAMERA)
    return ffi


#: Objeto FFI compartido. Solo contiene declaraciones, no tiene estado.
ffi = build_ffi()


def library_name() -> str:
    """Nombre del archivo de la librería del eSDK en esta plataforma."""
    if sys.platform.startswith("win"):
        return "EmergentCameraC.dll"
    if sys.platform == "darwin":
        return "libEmergentCameraC.dylib"
    return "libEmergentCameraC.so"


def library_candidates() -> list:
    """Rutas donde vive la librería del eSDK, por orden de probabilidad.

    El eSDK de Emergent se instala en sitios distintos según la plataforma, y
    la primera versión de este módulo solo miraba las rutas de Windows. En
    Linux el resultado era un mensaje pidiendo una DLL que en Linux no existe,
    lo que manda a buscar el problema donde no está.
    """
    name = library_name()
    env = os.environ.get("EMERGENT_DIR")
    candidates = []
    if env:
        base = Path(env)
        candidates += [base / "eSDK" / "bin" / name,
                       base / "eSDK" / "lib" / name,
                       base / "bin" / name,
                       base / "lib" / name,
                       base / name]
    if sys.platform.startswith("win"):
        candidates += [Path("C:/EVT/eSDK/bin") / name,
                       Path("C:/Program Files/EVT/eSDK/bin") / name]
    else:
        candidates += [Path("/opt/EVT/eSDK/lib") / name,
                       Path("/opt/EVT/lib") / name,
                       Path("/usr/local/lib") / name,
                       Path("/usr/lib") / name,
                       Path("/usr/lib/x86_64-linux-gnu") / name]
    return candidates


def default_dll_path() -> Optional[Path]:
    """Ruta a la librería del eSDK, deducida de EMERGENT_DIR y del sistema."""
    for path in library_candidates():
        try:
            if path.is_file():
                return path
        except OSError:
            continue
    return None


def missing_library_message() -> str:
    """Por qué no se encontró el eSDK, con las rutas que se revisaron."""
    name = library_name()
    revisadas = "\n  ".join(str(p) for p in library_candidates())
    plataforma = ("Windows" if sys.platform.startswith("win")
                  else "macOS" if sys.platform == "darwin" else "Linux")
    return (
        f"No se encontró la librería del eSDK de Emergent ({name}) en este "
        f"sistema ({plataforma}).\n"
        f"Rutas revisadas:\n  {revisadas}\n"
        f"Instala el eSDK de Emergent, o define EMERGENT_DIR apuntando a la "
        f"carpeta donde está instalado.")


def sdk_search_dirs(bin_dir: Path) -> list:
    """Carpetas donde viven las DLL de las que depende el eSDK.

    EmergentCameraC.dll arrastra EmergentCamera, EmergentGenICam y
    EmergentGigEVision, que a su vez dependen de las librerias de GenICam y de
    ffmpeg, repartidas en varias carpetas de la instalacion.
    """
    esdk = bin_dir.parent            # .../eSDK
    evt = esdk.parent                # .../EVT
    candidates = [
        bin_dir,
        esdk / "GenICam" / "bin" / "Win64_x64",
        esdk / "genicam" / "bin" / "Win64_x64",
        esdk / "ffmpeg" / "bin",
        evt / "eNIC" / "bin",
    ]

    found = []
    seen = set()
    for path in candidates:
        try:
            if not path.is_dir():
                continue
            key = str(path.resolve()).lower()
        except OSError:
            continue
        if key not in seen:
            seen.add(key)
            found.append(path)
    return found


#: Mantiene vivos los cookies de add_dll_directory: al liberarse, Windows
#: quitaria la carpeta de la ruta de busqueda.
_dll_directory_cookies = []


def load_library(dll_path: str | os.PathLike):
    """Carga EmergentCameraC.dll y devuelve el objeto lib de CFFI."""
    dll_path = Path(dll_path)
    if not dll_path.is_file():
        raise FileNotFoundError(f"No se encontro la libreria del eSDK: {dll_path}")

    # Python 3.8+ llama a SetDefaultDllDirectories al arrancar, lo que deja de
    # buscar dependencias en PATH. Hay que declarar las carpetas del SDK
    # explicitamente aunque el instalador las haya puesto en PATH.
    if hasattr(os, "add_dll_directory"):
        for directory in sdk_search_dirs(dll_path.parent):
            try:
                _dll_directory_cookies.append(os.add_dll_directory(str(directory)))
            except OSError:
                pass

    try:
        return ffi.dlopen(str(dll_path))
    except OSError as exc:
        extra = ("Comprueba que la instalacion del eSDK este completa."
                 if sys.platform.startswith("win") else
                 "Suele faltar alguna libreria de la que depende el eSDK: "
                 "comprueba con 'ldd' sobre ese archivo, y que la carpeta este "
                 "en LD_LIBRARY_PATH o en /etc/ld.so.conf.d/.")
        raise OSError(
            f"No se pudo cargar {dll_path}: {exc}\n"
            f"Falta alguna libreria de la que depende el eSDK. {extra}"
        ) from exc
