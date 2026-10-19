"""Camara simulada.

Permite abrir, configurar, previsualizar y grabar sin hardware conectado, con
la misma interfaz que `evt.api.Camera`. Sirve para probar el programa cuando
las fibras estan desconectadas y para reproducir problemas sin ocupar el
equipo.

Se activa con:  python -m pastam_cam --demo
"""

from __future__ import annotations

import threading
import time
from typing import Dict, List, Optional, Tuple

import numpy as np

from ._ffi import EVT_ERROR_TIMEDOUT, EVT_SUCCESS, EvtError
from .api import DeviceInfo, ParamInfo, PixelFormat

_SENSOR_WIDTH = 1600
_SENSOR_HEIGHT = 1100

#: Tope de generacion para no quemar un nucleo entero simulando 662 fps.
_MAX_SIMULATED_FPS = 400.0

_PIXEL_FORMATS = {
    "Mono8": PixelFormat.MONO8,
    "Mono12": PixelFormat.MONO12,
    "Mono12Packed": PixelFormat.MONO12_PACKED,
}


class DemoFrame:
    """Mismo contrato que `evt.api.Frame`."""

    __slots__ = ("width", "height", "stride", "frame_id", "timestamp", "nsecs",
                 "buffer_size", "pixel_format", "_data")

    def __init__(self, width: int, height: int, stride: int,
                 pixel_format: PixelFormat, data: np.ndarray):
        self.width = width
        self.height = height
        self.stride = stride
        self.pixel_format = pixel_format
        self._data = data
        self.buffer_size = int(data.size)
        self.frame_id = 0
        self.timestamp = 0
        self.nsecs = 0

    def buffer_view(self) -> np.ndarray:
        return self._data


def _pack_mono12(image: np.ndarray) -> np.ndarray:
    """uint16 (0..4095) -> Mono12Packed (2 pixeles en 3 bytes)."""
    height, width = image.shape
    pairs = width // 2
    p0 = image[:, 0 : pairs * 2 : 2].astype(np.uint16)
    p1 = image[:, 1 : pairs * 2 : 2].astype(np.uint16)

    out = np.empty((height, pairs, 3), dtype=np.uint8)
    out[:, :, 0] = (p0 >> 4).astype(np.uint8)
    out[:, :, 1] = (((p1 & 0x0F) << 4) | (p0 & 0x0F)).astype(np.uint8)
    out[:, :, 2] = (p1 >> 4).astype(np.uint8)
    return out.reshape(height, pairs * 3)


class DemoCamera:
    """Camara falsa que genera un patron animado."""

    def __init__(self, info: DeviceInfo):
        self.info = info
        self._lock = threading.RLock()
        self._closed = False
        self._stream_open = False
        self._queue: List[DemoFrame] = []
        self._allocated: List[DemoFrame] = []

        self._numeric: Dict[str, ParamInfo] = {
            "Width": ParamInfo("Width", _SENSOR_WIDTH, 128, _SENSOR_WIDTH, 16),
            "Height": ParamInfo("Height", _SENSOR_HEIGHT, 64, _SENSOR_HEIGHT, 4),
            "OffsetX": ParamInfo("OffsetX", 0, 0, 0, 16),
            "OffsetY": ParamInfo("OffsetY", 0, 0, 0, 4),
            "FrameRate": ParamInfo("FrameRate", 60, 1, 662, 1),
            "Exposure": ParamInfo("Exposure", 2000, 5, 1_000_000, 1),
            "Gain": ParamInfo("Gain", 256, 0, 4095, 1),
            "Offset": ParamInfo("Offset", 0, 0, 255, 1),
            "LineTime": ParamInfo("LineTime", 1, 1, 4096, 1),
            "AcquisitionFrameCount": ParamInfo("AcquisitionFrameCount", 1, 1, 65535, 1),
            "GevSCPSPacketSize": ParamInfo("GevSCPSPacketSize", 8192, 576, 9000, 4),
            "StreamBytesPerSecond": ParamInfo(
                "StreamBytesPerSecond", 1_000_000_000, 1_000_000, 3_000_000_000, 1
            ),
            "AutoExpSet": ParamInfo("AutoExpSet", 512, 0, 1023, 1),
            "AutoExpIGain": ParamInfo("AutoExpIGain", 8, 0, 255, 1),
        }
        self._enums: Dict[str, Tuple[str, List[str]]] = {
            "PixelFormat": ("Mono12", list(_PIXEL_FORMATS)),
            "AcquisitionMode": ("Continuous", ["Continuous", "MultiFrame"]),
            "TriggerMode": ("Off", ["Off", "On"]),
            "TriggerSource": ("Software", ["Software", "Hardware"]),
        }
        self._bools: Dict[str, bool] = {"AutoExposure": False}
        self._strings: Dict[str, str] = {
            "DeviceModelName": "HB-1800-S-M-SIM",
            "DeviceVendorName": "Emergent Vision Technologies (simulada)",
            "DeviceSerialNumber": info.serial,
            "DeviceVersion": "1.0-sim",
            "DeviceFirmwareVersion": "0.0.0-sim",
        }

        self._pattern: Optional[np.ndarray] = None
        self._pattern_shape: Tuple[int, int] = (0, 0)
        self._phase = 0
        self._next_due = 0.0
        self._frame_id = 0
        self._t0 = time.perf_counter_ns()

    # ------------------------------------------------------------------
    # Parametros
    # ------------------------------------------------------------------
    def _snap(self, info: ParamInfo, value: int) -> int:
        value = max(info.minimum, min(info.maximum, int(value)))
        if info.increment > 1:
            steps = round((value - info.minimum) / info.increment)
            value = info.minimum + steps * info.increment
            value = min(value, info.maximum)
        return int(value)

    def get_uint32(self, name: str) -> int:
        with self._lock:
            if name not in self._numeric:
                raise EvtError("EVT_CameraGetUInt32Param", 281)
            return self._numeric[name].value

    def set_uint32(self, name: str, value: int) -> None:
        with self._lock:
            if name not in self._numeric:
                raise EvtError("EVT_CameraSetUInt32Param", 281)
            info = self._numeric[name]
            info.value = self._snap(info, value)
            if name in ("Width", "Height"):
                self._sync_offsets()
                self._pattern = None

    def get_int32(self, name: str) -> int:
        with self._lock:
            if name == "SensTemp":
                return 38
            raise EvtError("EVT_CameraGetInt32Param", 281)

    def set_int32(self, name: str, value: int) -> None:
        raise EvtError("EVT_CameraSetInt32Param", 281)

    def get_bool(self, name: str) -> bool:
        with self._lock:
            if name not in self._bools:
                raise EvtError("EVT_CameraGetBoolParam", 281)
            return self._bools[name]

    def set_bool(self, name: str, value: bool) -> None:
        with self._lock:
            if name not in self._bools:
                raise EvtError("EVT_CameraSetBoolParam", 281)
            self._bools[name] = bool(value)

    def param_info(self, name: str, signed: bool = False) -> ParamInfo:
        with self._lock:
            if name not in self._numeric:
                raise EvtError("EVT_CameraGetUInt32Param", 281)
            info = self._numeric[name]
            return ParamInfo(info.name, info.value, info.minimum,
                             info.maximum, info.increment)

    def get_enum(self, name: str) -> str:
        with self._lock:
            if name not in self._enums:
                raise EvtError("EVT_CameraGetStringEnumParam", 281)
            return self._enums[name][0]

    def set_enum(self, name: str, value: str) -> None:
        with self._lock:
            if name not in self._enums:
                raise EvtError("EVT_CameraSetEnumParam", 281)
            current, options = self._enums[name]
            if value not in options:
                raise EvtError("EVT_CameraSetEnumParam", 283)
            self._enums[name] = (value, options)

    def enum_options(self, name: str) -> List[str]:
        with self._lock:
            if name not in self._enums:
                raise EvtError("EVT_CameraGetEnumParamRange", 281)
            return list(self._enums[name][1])

    def get_string(self, name: str) -> str:
        with self._lock:
            if name not in self._strings:
                raise EvtError("EVT_CameraGetStringParam", 281)
            return self._strings[name]

    def execute(self, command: str) -> None:
        if command in ("AcquisitionStart", "AcquisitionStop",
                       "GevTimestampControlLatch", "TriggerSoftware"):
            return
        raise EvtError("EVT_CameraExecuteCommand", 281)

    def has_param(self, name: str) -> bool:
        with self._lock:
            return (name in self._numeric or name in self._enums
                    or name in self._bools)

    def _sync_offsets(self) -> None:
        width = self._numeric["Width"].value
        height = self._numeric["Height"].value
        off_x = self._numeric["OffsetX"]
        off_y = self._numeric["OffsetY"]
        off_x.maximum = max(0, _SENSOR_WIDTH - width)
        off_y.maximum = max(0, _SENSOR_HEIGHT - height)
        off_x.value = min(off_x.value, off_x.maximum)
        off_y.value = min(off_y.value, off_y.maximum)

    # ------------------------------------------------------------------
    # Stream
    # ------------------------------------------------------------------
    @property
    def stream_open(self) -> bool:
        return self._stream_open

    @property
    def closed(self) -> bool:
        return self._closed

    def open_stream(self) -> None:
        self._stream_open = True
        self._next_due = 0.0

    def close_stream(self) -> None:
        self._stream_open = False
        self._queue.clear()

    def _geometry(self) -> Tuple[int, int, PixelFormat]:
        with self._lock:
            width = self._numeric["Width"].value
            height = self._numeric["Height"].value
            fmt = _PIXEL_FORMATS[self._enums["PixelFormat"][0]]
        return width, height, fmt

    @staticmethod
    def _row_bytes(width: int, fmt: PixelFormat) -> int:
        if fmt == PixelFormat.MONO8:
            return width
        if fmt == PixelFormat.MONO12_PACKED:
            return (width // 2) * 3
        return width * 2

    def allocate_frame(self) -> DemoFrame:
        width, height, fmt = self._geometry()
        stride = self._row_bytes(width, fmt)
        data = np.zeros(stride * height, dtype=np.uint8)
        frame = DemoFrame(width, height, stride, fmt, data)
        self._allocated.append(frame)
        return frame

    def release_frame(self, frame: DemoFrame) -> None:
        if frame in self._allocated:
            self._allocated.remove(frame)

    def release_all_frames(self) -> None:
        self._allocated.clear()
        self._queue.clear()

    def queue_frame(self, frame: DemoFrame) -> None:
        self._queue.append(frame)

    def _build_pattern(self, width: int, height: int) -> np.ndarray:
        """Patron base ancho; cada frame es una ventana que se desplaza."""
        span = width + 256
        yy, xx = np.mgrid[0:height, 0:span]
        ramp = (xx * (4095.0 / max(1, span - 1))).astype(np.float32)
        bars = 500.0 * np.sin(xx / 18.0).astype(np.float32)
        cx, cy = span * 0.5, height * 0.5
        radius = np.sqrt((xx - cx) ** 2 + (yy - cy) ** 2)
        blob = 1200.0 * np.exp(-(radius ** 2) / (2.0 * (height * 0.22) ** 2))
        image = np.clip(ramp * 0.6 + bars + blob, 0, 4095)
        return image.astype(np.uint16)

    def get_frame(self, timeout_ms: int) -> Tuple[int, Optional[DemoFrame]]:
        if not self._stream_open or not self._queue:
            time.sleep(min(timeout_ms, 20) / 1000.0)
            return EVT_ERROR_TIMEDOUT, None

        with self._lock:
            fps = min(float(self._numeric["FrameRate"].value), _MAX_SIMULATED_FPS)
        period = 1.0 / max(1.0, fps)

        now = time.perf_counter()
        if self._next_due == 0.0:
            self._next_due = now
        delay = self._next_due - now
        if delay > 0:
            if delay > timeout_ms / 1000.0:
                time.sleep(timeout_ms / 1000.0)
                return EVT_ERROR_TIMEDOUT, None
            time.sleep(delay)
        self._next_due = max(time.perf_counter(), self._next_due + period)

        width, height, fmt = self._geometry()
        frame = self._queue.pop(0)

        if frame.width != width or frame.height != height or frame.pixel_format != fmt:
            return EVT_ERROR_TIMEDOUT, None

        if self._pattern is None or self._pattern_shape != (height, width):
            self._pattern = self._build_pattern(width, height)
            self._pattern_shape = (height, width)

        self._phase = (self._phase + 3) % 256
        window = self._pattern[:, self._phase : self._phase + width]

        if fmt == PixelFormat.MONO8:
            payload = (window >> 4).astype(np.uint8)
        elif fmt == PixelFormat.MONO12_PACKED:
            payload = _pack_mono12(window)
        else:
            payload = window.astype("<u2").view(np.uint8)

        flat = payload.reshape(-1)
        size = min(flat.size, frame._data.size)
        frame._data[:size] = flat[:size]

        self._frame_id = 1 if self._frame_id >= 65535 else self._frame_id + 1
        frame.frame_id = self._frame_id
        frame.timestamp = time.perf_counter_ns() - self._t0
        return EVT_SUCCESS, frame

    def drain(self, timeout_ms: int = 100, limit: int = 512) -> int:
        return 0

    def close(self) -> None:
        self.close_stream()
        self.release_all_frames()
        self._closed = True


class DemoContext:
    """Sustituto de `EvtContext` sin hardware."""

    def __init__(self, count: int = 2):
        self.dll_path = None
        self._count = max(1, count)
        self._cameras: List[DemoCamera] = []

    def list_devices(self) -> List[DeviceInfo]:
        return [
            DeviceInfo(
                index=i,
                serial=f"SIM-{i + 1:03d}",
                ip=f"10.0.{i + 1}.2",
                mac=f"00:0A:47:00:00:{i + 1:02X}",
                _device_set=None,
            )
            for i in range(self._count)
        ]

    def open_camera(self, device: DeviceInfo, disable_heartbeat: bool = False) -> DemoCamera:
        camera = DemoCamera(device)
        self._cameras.append(camera)
        return camera

    def close_camera(self, camera: DemoCamera) -> None:
        camera.close()
        if camera in self._cameras:
            self._cameras.remove(camera)

    def close_all(self) -> None:
        for camera in list(self._cameras):
            self.close_camera(camera)
