"""API de alto nivel sobre el eSDK de Emergent Vision Technologies.

Modelo de hilos
---------------
GigE Vision usa dos canales independientes: GVCP para control (parametros,
comandos) y GVSP para el stream de imagenes. Por eso:

  * Las operaciones de control (get/set de parametros, comandos) se serializan
    con `_control_lock`, de modo que dos llamadas de control nunca se solapen.
  * Las operaciones de stream (`queue_frame`, `get_frame`) NO toman ese lock:
    corren en el hilo de adquisicion sin competir con la GUI.

Esto permite mover un slider de exposicion mientras el loop de captura corre a
maxima velocidad, sin bloquear ninguno de los dos.
"""

from __future__ import annotations

import ctypes
import threading
from dataclasses import dataclass, field
from enum import IntEnum
from pathlib import Path
from typing import Any, List, Optional, Tuple

import numpy as np

from ._ffi import (
    ENUM_STR_MAX_SIZE,
    EVT_ERROR_AGAIN,
    EVT_ERROR_TIMEDOUT,
    EVT_FRAME_BUFFER_ZERO_COPY,
    EVT_SUCCESS,
    EvtError,
    default_dll_path,
    missing_library_message,
    ffi,
    load_library,
)


class PixelFormat(IntEnum):
    """Formatos GVSP (emergentgigevisiondef.h)."""

    UNKNOWN = 0
    MONO8 = 17301505
    MONO10 = 17825795
    MONO10_PACKED = 17563652
    MONO12 = 17825797
    MONO12_PACKED = 17563654
    BAYGB8 = 17301514
    BAYGB10 = 17825806
    BAYGB10_PACKED = 17563688
    BAYGB12 = 17825810
    BAYGB12_PACKED = 17563692
    BAYGR8 = 17301512
    BAYGR10 = 17825804
    BAYGR10_PACKED = 17563686
    BAYGR12 = 17825808
    BAYGR12_PACKED = 17563690
    BAYRG8 = 17301513
    BAYRG10 = 17825805
    BAYRG10_PACKED = 17563687
    BAYRG12 = 17825809
    BAYRG12_PACKED = 17563691
    BAYBG8 = 17301515
    BAYBG10 = 17825807
    BAYBG10_PACKED = 17563689
    BAYBG12 = 17825811
    BAYBG12_PACKED = 17563693
    RGB8 = 35127316
    BGR8 = 35127317
    RGB10 = 36700184
    RGB12 = 36700186
    BGR10 = 36700185
    BGR12 = 36700187
    YUV411_PACKED = 34340894
    YUV422_PACKED = 34603039
    YUV444_PACKED = 35127328

    @classmethod
    def coerce(cls, value: int) -> "PixelFormat":
        try:
            return cls(value)
        except ValueError:
            return cls.UNKNOWN


@dataclass
class ParamInfo:
    """Valor actual y rango de un parametro numerico de la camara."""

    name: str
    value: int
    minimum: int
    maximum: int
    increment: int = 1

    @property
    def span(self) -> int:
        return max(0, self.maximum - self.minimum)


@dataclass
class DeviceInfo:
    """Una camara detectada en la red."""

    index: int
    serial: str
    ip: str
    mac: str
    _device_set: Any = field(repr=False, default=None)

    @property
    def label(self) -> str:
        return f"{self.serial}  ·  {self.ip}"


class Frame:
    """Descriptor de un frame recibido.

    `img_ptr` apunta a memoria del SDK, no del runtime de Python. La vista
    numpy que entrega `buffer_view()` es de copia cero y solo es valida hasta
    que el frame se vuelve a encolar con `queue_frame`.
    """

    __slots__ = (
        "_ffi_frame",
        "width",
        "height",
        "stride",
        "frame_id",
        "timestamp",
        "nsecs",
        "buffer_size",
        "img_ptr",
        "pixel_format",
    )

    def __init__(self, ffi_frame):
        self._ffi_frame = ffi_frame
        self.width = int(ffi_frame.size_x)
        self.height = int(ffi_frame.size_y)
        self.stride = int(ffi_frame.stride)
        self.frame_id = int(ffi_frame.frame_id)
        self.timestamp = int(ffi_frame.timestamp)
        self.nsecs = int(ffi_frame.nsecs)
        self.buffer_size = int(ffi_frame.bufferSize)
        self.img_ptr = int(ffi.cast("unsigned long long", ffi_frame.imagePtr))
        self.pixel_format = PixelFormat.coerce(int(ffi_frame.pixel_type))

    def buffer_view(self) -> np.ndarray:
        """Vista uint8 de copia cero sobre el buffer del SDK."""
        if not self.img_ptr or self.buffer_size <= 0:
            return np.empty(0, dtype=np.uint8)
        raw = (ctypes.c_ubyte * self.buffer_size).from_address(self.img_ptr)
        return np.frombuffer(raw, dtype=np.uint8)


class Camera:
    """Una camara abierta."""

    def __init__(self, context: "EvtContext", handle, info: DeviceInfo):
        self._ctx = context
        self._handle = handle
        self._lib = context._lib
        self.info = info
        self._control_lock = threading.RLock()
        self._closed = False
        self._stream_open = False
        self._allocated: List[Frame] = []

    # ------------------------------------------------------------------
    # Utilidades internas
    # ------------------------------------------------------------------
    def _call(self, func_name: str, *args) -> None:
        res = getattr(self._lib, func_name)(*args)
        if res is not None and res != EVT_SUCCESS:
            raise EvtError(func_name, int(res))

    @staticmethod
    def _cstr(text: str):
        return ffi.new("char[]", text.encode("ascii"))

    # ------------------------------------------------------------------
    # Parametros numericos
    # ------------------------------------------------------------------
    def get_uint32(self, name: str) -> int:
        with self._control_lock:
            out = ffi.new("unsigned int*")
            self._call("EVT_CameraGetUInt32Param", self._handle, self._cstr(name), out)
            return int(out[0])

    def set_uint32(self, name: str, value: int) -> None:
        with self._control_lock:
            self._call("EVT_CameraSetUInt32Param", self._handle, self._cstr(name), int(value))

    def get_int32(self, name: str) -> int:
        with self._control_lock:
            out = ffi.new("int*")
            self._call("EVT_CameraGetInt32Param", self._handle, self._cstr(name), out)
            return int(out[0])

    # No hay set_int32: EVT_CameraSetInt32Param aparece en el header del eSDK
    # pero no esta exportada en EmergentCameraC.dll. Los parametros con signo
    # que necesita este programa son de solo lectura.

    def get_bool(self, name: str) -> bool:
        with self._control_lock:
            out = ffi.new("unsigned char*")
            self._call("EVT_CameraGetBoolParam", self._handle, self._cstr(name), out)
            return bool(out[0])

    def set_bool(self, name: str, value: bool) -> None:
        with self._control_lock:
            self._call("EVT_CameraSetBoolParam", self._handle, self._cstr(name), 1 if value else 0)

    def param_info(self, name: str, signed: bool = False) -> ParamInfo:
        """Valor + rango + incremento de un parametro numerico.

        Los limites dependen de otros parametros (bajar FrameRate abre el rango
        de Exposure, por ejemplo), asi que hay que releerlos despues de cada
        cambio en vez de cachearlos.
        """
        with self._control_lock:
            ctype = "int*" if signed else "unsigned int*"
            prefix = "EVT_CameraGetInt32Param" if signed else "EVT_CameraGetUInt32Param"
            cname = self._cstr(name)

            cur = ffi.new(ctype)
            self._call(prefix, self._handle, cname, cur)

            lo = ffi.new(ctype)
            self._call(prefix + "Min", self._handle, cname, lo)

            hi = ffi.new(ctype)
            self._call(prefix + "Max", self._handle, cname, hi)

            inc = ffi.new("unsigned int*")
            try:
                self._call(prefix + "Inc", self._handle, cname, inc)
                step = max(1, int(inc[0]))
            except EvtError:
                step = 1

            return ParamInfo(name, int(cur[0]), int(lo[0]), int(hi[0]), step)

    # ------------------------------------------------------------------
    # Enumeraciones y cadenas
    # ------------------------------------------------------------------
    def get_enum(self, name: str) -> str:
        with self._control_lock:
            buf = ffi.new("char[]", ENUM_STR_MAX_SIZE)
            self._call(
                "EVT_CameraGetStringEnumParam",
                self._handle, self._cstr(name), buf, ENUM_STR_MAX_SIZE, ffi.NULL,
            )
            return ffi.string(buf).decode("ascii", "replace")

    def set_enum(self, name: str, value: str) -> None:
        with self._control_lock:
            self._call("EVT_CameraSetEnumParam", self._handle, self._cstr(name), self._cstr(value))

    def enum_options(self, name: str) -> List[str]:
        """Opciones validas de un enum, en el orden que las reporta la camara."""
        with self._control_lock:
            buf = ffi.new("char[]", ENUM_STR_MAX_SIZE)
            self._call(
                "EVT_CameraGetEnumParamRange",
                self._handle, self._cstr(name), buf, ENUM_STR_MAX_SIZE, ffi.NULL,
            )
            raw = ffi.string(buf).decode("ascii", "replace")
        return [item.strip() for item in raw.split(",") if item.strip()]

    def get_string(self, name: str) -> str:
        with self._control_lock:
            buf = ffi.new("char[]", ENUM_STR_MAX_SIZE)
            self._call(
                "EVT_CameraGetStringParam",
                self._handle, self._cstr(name), buf, ENUM_STR_MAX_SIZE, ffi.NULL,
            )
            return ffi.string(buf).decode("ascii", "replace").strip()

    def execute(self, command: str) -> None:
        with self._control_lock:
            self._call("EVT_CameraExecuteCommand", self._handle, self._cstr(command))

    def has_param(self, name: str) -> bool:
        """True si la camara expone el parametro (de cualquier tipo)."""
        for getter in (self.get_uint32, self.get_enum, self.get_int32, self.get_bool):
            try:
                getter(name)
                return True
            except EvtError:
                continue
        return False

    # ------------------------------------------------------------------
    # Stream
    # ------------------------------------------------------------------
    def open_stream(self) -> None:
        if self._stream_open:
            return
        self._call("EVT_CameraOpenStream", self._handle)
        self._stream_open = True

    def close_stream(self) -> None:
        if not self._stream_open:
            return
        self._call("EVT_CameraCloseStream", self._handle)
        self._stream_open = False

    @property
    def stream_open(self) -> bool:
        return self._stream_open

    def allocate_frame(self) -> Frame:
        """Reserva un buffer con la geometria y formato actuales de la camara."""
        with self._control_lock:
            width = self.get_uint32("Width")
            height = self.get_uint32("Height")
            out = ffi.new("unsigned int*")
            self._call("EVT_CameraGetValueEnumParam", self._handle,
                       self._cstr("PixelFormat"), out)
            pixel_format = int(out[0])

        ffi_frame = ffi.new("EmergentFrame*")
        ffi_frame.size_x = width
        ffi_frame.size_y = height
        ffi_frame.pixel_type = pixel_format
        self._call("EVT_AllocateFrameBuffer", self._handle, ffi_frame,
                   EVT_FRAME_BUFFER_ZERO_COPY)

        frame = Frame(ffi_frame)
        self._allocated.append(frame)
        return frame

    def release_frame(self, frame: Frame) -> None:
        self._call("EVT_ReleaseFrameBuffer", self._handle, frame._ffi_frame)
        if frame in self._allocated:
            self._allocated.remove(frame)

    def release_all_frames(self) -> None:
        for frame in list(self._allocated):
            try:
                self.release_frame(frame)
            except EvtError:
                pass
        self._allocated.clear()

    def queue_frame(self, frame: Frame) -> None:
        self._call("EVT_CameraQueueFrame", self._handle, frame._ffi_frame)

    def get_frame(self, timeout_ms: int) -> Tuple[int, Optional[Frame]]:
        """Espera un frame. Devuelve (codigo_error, frame).

        No lanza excepcion: en el loop de captura los timeouts son normales y
        levantar una excepcion por cada uno seria carisimo. El descriptor que
        se recibe es independiente de los buffers encolados, tal como hacen los
        ejemplos del SDK (`evtFrameRecv`); despues hay que reencolarlo con
        `queue_frame`.
        """
        ffi_frame = ffi.new("EmergentFrame*")
        res = int(self._lib.EVT_CameraGetFrame(self._handle, ffi_frame, int(timeout_ms)))
        if res != EVT_SUCCESS:
            return res, None
        return EVT_SUCCESS, Frame(ffi_frame)

    def drain(self, timeout_ms: int = 100, limit: int = 512) -> int:
        """Vacia la cola de frames pendientes tras detener la adquisicion."""
        drained = 0
        for _ in range(limit):
            err, _frame = self.get_frame(timeout_ms)
            if err in (EVT_ERROR_AGAIN, EVT_ERROR_TIMEDOUT) or err != EVT_SUCCESS:
                break
            drained += 1
        return drained

    # ------------------------------------------------------------------
    def close(self) -> None:
        if self._closed:
            return
        try:
            self.release_all_frames()
        finally:
            try:
                self.close_stream()
            finally:
                self._call("EVT_CameraClose", self._handle)
                self._closed = True

    @property
    def closed(self) -> bool:
        return self._closed


class _DeviceSetHandle:
    """Mantiene viva la memoria del set de dispositivos del SDK."""

    def __init__(self, lib, ffi_set):
        self._lib = lib
        self.ffi_set = ffi_set

    def __del__(self):
        try:
            self._lib.EVT_DeviceInfoSetCleanup(self.ffi_set)
        except Exception:
            pass


class EvtContext:
    """Punto de entrada al eSDK."""

    def __init__(self, dll_path: str | Path | None = None):
        path = Path(dll_path) if dll_path else default_dll_path()
        if path is None:
            raise FileNotFoundError(missing_library_message())
        self.dll_path = Path(path)
        self._lib = load_library(self.dll_path)
        self._cameras: List[Camera] = []

    def list_devices(self) -> List[DeviceInfo]:
        ffi_set = ffi.new("LabviewDeviceSet*")
        res = int(self._lib.EVT_ListDevices(ffi_set))
        if res != EVT_SUCCESS:
            raise EvtError("EVT_ListDevices", res)

        handle = _DeviceSetHandle(self._lib, ffi_set)

        devices: List[DeviceInfo] = []
        for index in range(int(ffi_set.countDevice)):
            ffi_info = ffi.new("LabviewDeviceInfo*")
            res = int(self._lib.EVT_GetDeviceInfo(ffi_set, ffi_info, index))
            if res != EVT_SUCCESS:
                continue
            devices.append(
                DeviceInfo(
                    index=index,
                    serial=ffi.string(ffi_info.serialNumber).decode("ascii", "replace"),
                    ip=ffi.string(ffi_info.currentIp).decode("ascii", "replace"),
                    mac=ffi.string(ffi_info.macAddress).decode("ascii", "replace"),
                    _device_set=handle,
                )
            )
        return devices

    def open_camera(self, device: DeviceInfo, disable_heartbeat: bool = False) -> Camera:
        params = ffi.new("CameraOpenParams*")
        self._lib.EVT_CameraOpenParams_Init(params)
        params.disableHeartbeat = disable_heartbeat

        handle_ptr = ffi.new("Camera_Handle*")
        res = int(
            self._lib.EVT_CameraOpen_Ex(
                handle_ptr, device._device_set.ffi_set, device.index, params
            )
        )
        if res != EVT_SUCCESS:
            raise EvtError("EVT_CameraOpen_Ex", res)

        camera = Camera(self, handle_ptr[0], device)
        self._cameras.append(camera)
        return camera

    def close_camera(self, camera: Camera) -> None:
        camera.close()
        if camera in self._cameras:
            self._cameras.remove(camera)

    def close_all(self) -> None:
        for camera in list(self._cameras):
            try:
                self.close_camera(camera)
            except Exception:
                pass
