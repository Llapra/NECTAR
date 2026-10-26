"""Worker de la cámara Emergent HB-1800-S-M (25GigE).

Envuelve la capa ``evt/`` portada de ``pastam_cam`` en la interfaz
``DeviceWorker``, y cambia una cosa fundamental respecto al programa original:
**la grabación va directo al SSD en vez de a RAM.**

Por qué se cambió el modelo de grabación
----------------------------------------
``pastam_cam`` reservaba un bloque de RAM, grababa la ráfaga completa ahí y
volcaba a disco al terminar. Es lo correcto cuando el disco es lento: elimina
la latencia del sistema de archivos del camino crítico. Pero pone un techo
duro: a 1,67 GB/s por cámara, 128 GB de RAM son ~75 segundos de grabación, y
después hay que esperar el volcado antes de la siguiente toma.

El equipo de campaña tiene SSD NVMe de alta velocidad, así que en NECTAR se
escribe en continuo y la RAM queda como amortiguador:

* el anillo de ``DiskStreamWriter`` (256 slots ≈ 680 MB por cámara) se reserva
  y se confirma página por página **una sola vez**, al armar la primera toma,
  y sobrevive a todas las siguientes: cambiar de archivo no reserva memoria;
* el lazo de captura hace un único ``memcpy`` por frame al slot libre;
* un hilo escritor junta 32 frames y los manda al SSD en una sola llamada;
* si el SSD no alcanza, se cuentan los frames perdidos por disco en vez de
  bloquear la adquisición o dejar crecer la latencia sin límite;
* si el SSD **deja de aceptar datos** (se llenó, se desconectó), el escritor no
  se limita a contar errores: marca el flujo como caído, levanta la alarma del
  dispositivo y la sesión se detiene. Un escritor muerto que sigue aceptando
  frames es la peor forma de perder una noche de medición.

Lo que se conserva de ``pastam_cam``
------------------------------------
* La separación de canales GVCP (control, con cerrojo reentrante) y GVSP
  (datos, sin cerrojos).
* El patrón de buffers de los ejemplos del eSDK: N buffers encolados,
  recepción en un descriptor aparte y reencolado.
* El conteo de frames perdidos por saltos del ``frame_id`` de 16 bits.
* El generador dinámico de controles: los rangos y el paso los reporta la
  cámara, no están escritos en el código.
* La reconfiguración en caliente: cambiar ROI o formato de píxel para el
  stream, aplica y vuelve a montar, todo dentro del hilo de adquisición.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import numpy as np

from ... import config
from ...core import pixels
from ...core.disk_stream import DiskStreamWriter
from ...core.frame_hub import FrameHub, FrameMeta
from ...core.time_sync import TIME_BASE
from ..base import DeviceState, DeviceWorker
from .roi import RoiGeometry, read_roi_geometry, apply_roi

# ---------------------------------------------------------------------------
# La capa del SDK se importa con cuidado: necesita cffi y, en modo real, la
# DLL del eSDK. Que falte no puede impedir que el programa arranque.
# ---------------------------------------------------------------------------
try:
    from ._ffi import (EVT_ERROR_AGAIN, EVT_ERROR_TIMEDOUT, EVT_SUCCESS,
                       EvtError)
    from .api import Camera, DeviceInfo, EvtContext, PixelFormat
    from .demo import DemoContext
    from . import params as evt_params
    HAS_EVT_LAYER = True
    EVT_IMPORT_ERROR = ""
except Exception as exc:        # cffi ausente, o SDK incompleto
    HAS_EVT_LAYER = False
    EVT_IMPORT_ERROR = f"{type(exc).__name__}: {exc}"
    EVT_SUCCESS, EVT_ERROR_AGAIN, EVT_ERROR_TIMEDOUT = 0, 11, 138

    class EvtError(Exception):      # type: ignore[no-redef]
        pass


def emergent_available() -> bool:
    """True si la capa del eSDK se pudo importar (aunque sea para el demo)."""
    return HAS_EVT_LAYER


def discover_emergent() -> List[Dict[str, Any]]:
    """Enumera cámaras Emergent en la red.

    Fuera de ``--demo`` devuelve **solo cámaras reales**: si no hay ninguna, o
    si falta el eSDK, devuelve la lista vacía y deja que el error del hardware
    se vea tal cual. Antes rellenaba con cámaras de demostración, y eso es
    justamente lo que no puede pasar en campaña: una "EmerCam1" verde que en
    realidad es un generador de patrones.
    """
    devices: List[Dict[str, Any]] = []
    if config.simulations_allowed():
        for index in (1, 2):
            devices.append({
                "id": f"emergent_demo_{index}",
                "label": f"EmerCam{index}",
                "serial": f"DEMO{index:04d}",
                "ip": "0.0.0.0",
                "mac": "",
                "model": "HB-1800-S-M (demo)",
                "simulated": True,
            })
        return devices

    if not HAS_EVT_LAYER:
        raise RuntimeError(
            f"No se puede hablar con las cámaras Emergent: la capa del eSDK "
            f"no cargó ({EVT_IMPORT_ERROR}). Instala cffi y el eSDK de "
            f"Emergent. Para ensayar sin hardware: python main.py --demo")

    context = EvtContext()
    try:
        for index, info in enumerate(context.list_devices(), start=1):
            devices.append({
                "id": f"emergent_{getattr(info, 'serial', '') or index}",
                "label": f"EmerCam{index}",
                "serial": getattr(info, "serial", ""),
                "ip": getattr(info, "ip", ""),
                "mac": getattr(info, "mac", ""),
                "model": getattr(info, "model", ""),
                "simulated": False,
            })
    finally:
        try:
            context.close_all()
        except Exception:
            pass
    return devices


class EmergentWorker(DeviceWorker):
    """Una cámara Emergent. Se instancia una por cámara."""

    family = "emergent"
    family_label = "Emergent"
    supports_chunking = True
    #: 1604 x 1100 a 12 bits y 662 fps sostenidos ≈ 1,67 GB/s.
    nominal_mb_s = 1670.0

    def __init__(self, device_id: str, label: str,
                 descriptor: Optional[Dict[str, Any]] = None) -> None:
        super().__init__(device_id, label, descriptor)
        #: Sin descriptor explícito se asume **real**: equivocarse hacia el
        #: lado del hardware produce un error visible; equivocarse hacia el
        #: lado del simulador produce datos falsos que parecen buenos.
        self.simulated = bool(self.descriptor.get("simulated", False))
        self.display_fps = 30
        self.preview_step = 2           # decimación espacial de la vista previa
        self.auto_contrast = False
        self.buffer_count = config.EMERGENT_BUFFER_COUNT

        self.hub = FrameHub(display_fps=self.display_fps)

        self._context = None
        self._camera = None
        self._geometry: Dict[str, Any] = {}
        self._stream_has_frame = False
        self._param_cache: List[Dict[str, Any]] = []

        self._pending: List[Callable[[], None]] = []
        self._pending_lock = threading.Lock()
        self._restart = threading.Event()

        #: Escritor permanente: el anillo se reserva una vez y sobrevive a los
        #: cambios de archivo. Antes se creaba uno por bloque, lo que obligaba
        #: a reservar ~900 MB **durante** la grabación.
        self._writer: Optional[DiskStreamWriter] = None
        self._writer_frame_bytes = 0
        self._writer_failure = ""
        self._chunk_index = 0
        self._chunk_path: Optional[Path] = None
        self._chunk_started = 0.0
        self._chunk_seconds: Optional[float] = None
        self._chunk_summaries: List[Dict[str, Any]] = []

        #: Geometría de ROI vigente, leída de la propia cámara (GenICam).
        self._roi: Optional[RoiGeometry] = None

        self._received = 0
        self._dropped_net = 0       # saltos del frame_id (red / cámara)
        self._dropped_disk = 0      # el SSD no alcanzó
        self._errors = 0
        self._fps = 0.0
        self._sensor_temp: Optional[int] = None

        self._preview_thread: Optional[threading.Thread] = None
        self._preview_stop = threading.Event()

    # ------------------------------------------------------------------
    # Conexión
    # ------------------------------------------------------------------
    @property
    def is_armable(self) -> bool:
        return (super().is_armable and self._stream_has_frame
                and self._geometry.get("frame_bytes", 0) > 0)

    def connect_device(self) -> bool:
        if self.simulated and not config.simulations_allowed():
            self._fail("Cámara simulada fuera del modo --demo: no se conecta.",
                       fatal=True)
            return False
        if not HAS_EVT_LAYER:
            self._fail(f"Capa del eSDK no disponible ({EVT_IMPORT_ERROR}). "
                       f"Instala cffi y el eSDK de Emergent.", fatal=True)
            return False
        self.start_thread()
        return True

    def disconnect_device(self) -> None:
        self.stop_recording()
        self.stop_thread()
        self._set_state(DeviceState.DISCONNECTED)

    def estimated_mb_s(self) -> float:
        frame_bytes = self._geometry.get("frame_bytes", 0)
        if frame_bytes and self._fps > 0:
            return frame_bytes * self._fps / 1e6
        if frame_bytes:
            # Sin medición todavía: usar la tasa configurada en la cámara.
            configured = self._param_value("FrameRate") or 0
            if configured:
                return frame_bytes * configured / 1e6
        return self.nominal_mb_s

    # ------------------------------------------------------------------
    # Parámetros (los consume el generador dinámico de controles del GUI)
    # ------------------------------------------------------------------
    def snapshot_params(self) -> List[Dict[str, Any]]:
        """Catálogo vigente de parámetros, con rango, paso y valor actual."""
        return list(self._param_cache)

    def _param_value(self, name: str):
        for entry in self._param_cache:
            if entry["name"] == name:
                return entry.get("value")
        return None

    def _read_params(self) -> None:
        camera = self._camera
        if camera is None:
            return
        catalog: List[Dict[str, Any]] = []
        for spec in evt_params.supported_params(camera):
            entry: Dict[str, Any] = {
                "name": spec.name,
                "label": spec.label,
                "kind": spec.kind,
                "group": spec.group,
                "unit": spec.unit,
                "restart": spec.restart,
                "tooltip": spec.tooltip,
            }
            try:
                if spec.kind == "uint32":
                    info = camera.param_info(spec.name)
                    entry.update(value=camera.get_uint32(spec.name),
                                 minimum=info.minimum, maximum=info.maximum,
                                 increment=max(1, getattr(info, "increment", 1)))
                elif spec.kind == "int32":
                    entry.update(value=camera.get_int32(spec.name))
                elif spec.kind == "bool":
                    entry.update(value=camera.get_bool(spec.name))
                elif spec.kind == "enum":
                    entry.update(value=camera.get_enum(spec.name),
                                 options=camera.enum_options(spec.name))
            except EvtError:
                continue
            catalog.append(entry)
        self._param_cache = catalog
        self._roi = read_roi_geometry(camera)
        self.info_ready.emit({
            "parametros": catalog,
            "informacion": dict(evt_params.read_device_info(camera)),
            "geometria": dict(self._geometry),
            "roi": self._roi.as_dict() if self._roi is not None else None,
        })

    # ------------------------------------------------------------------
    # ROI por hardware
    # ------------------------------------------------------------------
    def roi_geometry(self) -> Optional[RoiGeometry]:
        """Recorte vigente con los rangos e incrementos de la cámara."""
        return self._roi

    def set_roi(self, x: int, y: int, width: int, height: int) -> None:
        """Programa el recorte **en el sensor**, no en la pantalla.

        Se encola para el hilo de adquisición y se aplica parando y volviendo
        a montar el stream, porque cambiar el tamaño del frame invalida los
        buffers ya reservados. Al volver, el ``.raw`` contiene solo el recorte
        y el caudal baja de verdad.
        """
        if self._recording:
            self._log("No se cambia el ROI durante una grabación.")
            return

        def action(camera=None):
            camera = camera or self._camera
            if camera is None or self._roi is None:
                return
            rect = apply_roi(camera, self._roi, x, y, width, height)
            self._log(f"ROI aplicado en el sensor: x={rect[0]} y={rect[1]} "
                      f"{rect[2]}x{rect[3]} px")

        with self._pending_lock:
            self._pending.append(action)
        self._restart.set()

    def clear_roi(self) -> None:
        """Vuelve al cuadro completo del sensor."""
        if self._roi is None:
            return
        self.set_roi(0, 0,
                     self._roi.sensor_width or self._roi.width.maximum,
                     self._roi.sensor_height or self._roi.height.maximum)

    def set_param(self, name: str, value: Any) -> None:
        """Encola un cambio de parámetro para aplicarlo en el hilo correcto.

        Los parámetros marcados ``restart`` invalidan los buffers ya
        reservados (cambian el tamaño del frame), así que se aplican parando y
        volviendo a montar el stream, nunca en caliente.
        """
        spec = next((entry for entry in self._param_cache
                     if entry["name"] == name), None)
        if spec is None:
            return

        def action(camera=None):
            camera = camera or self._camera
            if camera is None:
                return
            if spec["kind"] == "uint32":
                info = camera.param_info(name)
                requested = int(value)
                step = max(1, info.increment)
                if not (info.minimum <= requested <= info.maximum) or \
                        (requested - info.minimum) % step:
                    self._log(f"{name}: valor {requested} inválido; rango "
                              f"{info.minimum}–{info.maximum}, paso {step}.")
                    return
                camera.set_uint32(name, requested)
            elif spec["kind"] == "bool":
                camera.set_bool(name, bool(value))
            elif spec["kind"] == "enum":
                camera.set_enum(name, str(value))

        if self._recording:
            self._log("No se cambian parámetros durante una grabación.")
            return

        with self._pending_lock:
            self._pending.append(action)
        if spec["restart"]:
            self._restart.set()

    # ------------------------------------------------------------------
    # Lazo principal
    # ------------------------------------------------------------------
    def run(self) -> None:
        self._set_state(DeviceState.CONNECTING)
        try:
            self._open_camera()
        except Exception as exc:
            self._fail(f"No se pudo abrir la cámara: {exc}", fatal=True)
            return

        self._start_preview_thread()
        try:
            while not self.should_stop:
                try:
                    self._stream_once()
                except EvtError as exc:
                    self._fail(f"eSDK: {exc}", fatal=False)
                    self._sleep(1.0)
                except Exception as exc:
                    self._fail(f"{type(exc).__name__}: {exc}", fatal=False)
                    self._sleep(1.0)
                if not self._restart.is_set():
                    # Salida limpia del stream sin pedido de reinicio: si no
                    # hay orden de parada, se vuelve a montar de todos modos
                    # (la cámara pudo haber cortado el enlace).
                    if self.should_stop:
                        break
                self._restart.clear()
        finally:
            self._stop_preview_thread()
            self._close_camera()
            self._set_state(DeviceState.DISCONNECTED)

    # ------------------------------------------------------------------
    def _open_camera(self) -> None:
        if self.simulated:
            self._context = DemoContext(count=2)
        else:
            self._context = EvtContext()

        devices = self._context.list_devices()
        if not devices:
            raise RuntimeError("No hay cámaras Emergent visibles en la red.")

        serial = self.descriptor.get("serial", "")
        info = next((d for d in devices if getattr(d, "serial", "") == serial),
                    devices[0])
        self._camera = self._context.open_camera(info)

        # Sin jumbo frames el enlace de 25 GbE no rinde.
        applied = evt_params.maximize_packet_size(self._camera)
        if applied:
            self._log(f"Tamaño de paquete GVSP: {applied} bytes")

        self._read_params()
        self._sensor_temp = evt_params.read_sensor_temperature(self._camera)
        self._log(f"Cámara abierta ({'demo' if self.simulated else 'real'})")

    def _close_camera(self) -> None:
        self._finish_recording(reason="cierre")
        if self._writer is not None:
            # El anillo permanente se libera solo al desconectar la cámara.
            self._writer.close(reason="cierre_de_camara")
            self._writer = None
            self._writer_frame_bytes = 0
        try:
            if self._camera is not None and self._context is not None:
                self._context.close_camera(self._camera)
        except Exception:
            pass
        try:
            if self._context is not None:
                self._context.close_all()
        except Exception:
            pass
        self._camera = None
        self._context = None

    # ------------------------------------------------------------------
    def _stream_once(self) -> None:
        """Monta el stream, corre el lazo de captura y desmonta al salir."""
        camera = self._camera
        if camera is None:
            return

        self._apply_pending()

        frames = []
        self._stream_has_frame = False
        camera.open_stream()
        try:
            for _ in range(self.buffer_count):
                frame = camera.allocate_frame()
                frames.append(frame)
                camera.queue_frame(frame)

            reference = frames[0]
            self._geometry = {
                "width": reference.width,
                "height": reference.height,
                "stride": reference.stride,
                "pixel_format": reference.pixel_format,
                "pixel_format_name": getattr(reference.pixel_format, "name",
                                             str(reference.pixel_format)),
                "frame_bytes": reference.buffer_size,
            }
            self._log(f"Stream montado: {reference.width}x{reference.height}, "
                      f"{self._geometry['pixel_format_name']}, "
                      f"{reference.buffer_size / 1e6:.2f} MB/frame")

            camera.execute("AcquisitionStart")
            self._set_state(DeviceState.STREAMING)
            try:
                self._grab_loop()
            finally:
                try:
                    camera.execute("AcquisitionStop")
                except EvtError:
                    pass
                camera.drain()
        finally:
            self._stream_has_frame = False
            for frame in frames:
                try:
                    camera.release_frame(frame)
                except EvtError:
                    pass
            try:
                camera.close_stream()
            except EvtError:
                pass

    def _grab_loop(self) -> None:
        camera = self._camera
        hub = self.hub
        previous_id = 0
        first = True

        window_start = time.perf_counter()
        window_frames = 0
        next_stats = window_start + config.STATS_PERIOD_S
        next_temp = window_start + 5.0

        while not self.should_stop and not self._restart.is_set():
            # Los cambios de geometría se atienden después de desmontar;
            # los controles en vivo deben ejecutarse aunque siga llegando vídeo.
            self._apply_pending(live_only=True)
            code, frame = camera.get_frame(config.EMERGENT_FRAME_TIMEOUT_MS)

            if code != EVT_SUCCESS or frame is None:
                if code not in (EVT_ERROR_TIMEDOUT, EVT_ERROR_AGAIN):
                    self._errors += 1
                # Sin frame igual hay que atender órdenes: si la cámara dejó
                # de entregar, una orden de parada no puede quedar esperando.
                self._service_recording(time.perf_counter(), 0)
                continue

            host_ns = TIME_BASE.now_ns()
            self._received += 1
            window_frames += 1

            # --- saltos de secuencia GVSP ------------------------------
            # El id es de 16 bits y no usa el 0: tras 65535 vuelve a 1.
            frame_id = frame.frame_id
            if first:
                first = False
            else:
                expected = previous_id + 1
                if expected > 65535:
                    expected = 1
                if frame_id != expected:
                    gap = frame_id - expected
                    if gap < 0:
                        gap += 65535
                    self._dropped_net += gap
            previous_id = frame_id

            raw = frame.buffer_view()
            # Algunos eSDK dejan bufferSize en cero al reservar y lo completan
            # únicamente al recibir. El tamaño para grabar sale del frame real.
            if raw.size <= 0:
                camera.queue_frame(frame)
                continue
            if not self._stream_has_frame:
                self._geometry.update(
                    width=frame.width, height=frame.height, stride=frame.stride,
                    pixel_format=frame.pixel_format,
                    pixel_format_name=getattr(frame.pixel_format, "name", str(frame.pixel_format)),
                    frame_bytes=int(raw.nbytes))
                self._stream_has_frame = True
                self._log(f"Flujo confirmado: {frame.width}x{frame.height}, "
                          f"{raw.nbytes} bytes/frame; lista para armar.")

            # --- grabación: un solo memcpy al anillo -------------------
            if self._recording and self._writer is not None:
                if not self._writer.submit(raw, frame_id=frame_id,
                                           device_ts=frame.timestamp,
                                           host_ns=host_ns):
                    self._dropped_disk += 1

            # --- vista previa: otro memcpy, solo cuando toca -----------
            if hub.wants_frame():
                hub.publish(raw, FrameMeta(
                    frame_id=frame_id, device_ts=frame.timestamp,
                    host_ns=host_ns, width=frame.width, height=frame.height,
                    stride=frame.stride, pixel_format=frame.pixel_format))

            # --- devolver el buffer a la cámara ------------------------
            try:
                camera.queue_frame(frame)
            except EvtError:
                self._errors += 1

            # --- control y telemetría ----------------------------------
            now = time.perf_counter()
            self._service_recording(now, host_ns)

            if now >= next_temp:
                next_temp = now + 5.0
                self._sensor_temp = evt_params.read_sensor_temperature(camera)

            if now >= next_stats:
                elapsed = now - window_start
                self._fps = window_frames / elapsed if elapsed > 0 else 0.0
                self._emit_stats(self._stats_payload())
                window_start = now
                window_frames = 0
                next_stats = now + config.STATS_PERIOD_S

    def _apply_pending(self, live_only: bool = False) -> None:
        with self._pending_lock:
            if live_only and self._restart.is_set():
                return
            actions, self._pending = self._pending, []
        for action in actions:
            try:
                action()
            except EvtError as exc:
                self._log(f"Parámetro rechazado: {exc}")
        if actions:
            self._read_params()

    # ------------------------------------------------------------------
    # Vista previa en su propio hilo
    # ------------------------------------------------------------------
    def _start_preview_thread(self) -> None:
        self._preview_stop.clear()
        self._preview_thread = threading.Thread(
            target=self._preview_loop, name=f"preview-{self.device_id}",
            daemon=True)
        self._preview_thread.start()

    def _stop_preview_thread(self) -> None:
        self._preview_stop.set()
        if self._preview_thread is not None:
            self._preview_thread.join(1.5)
            self._preview_thread = None

    def _preview_loop(self) -> None:
        """Convierte el crudo a 8 bits fuera del lazo de captura.

        Desempaquetar Mono12Packed de 1604x1100 cuesta milisegundos; hacerlo
        dentro del lazo de captura sería meter esa latencia entre dos frames a
        662 fps. Aquí se hace en un hilo aparte que solo ve el último frame
        publicado en el buzón.
        """
        while not self._preview_stop.is_set():
            payload = self.hub.take()
            if payload is None:
                time.sleep(0.005)
                continue
            raw, meta = payload
            try:
                image = pixels.to_display_u8(
                    raw, meta.width, meta.height, meta.pixel_format,
                    stride=meta.stride, step=self.preview_step,
                    auto_contrast=self.auto_contrast)
                self.preview_ready.emit(image)
            except Exception:
                # Un frame mal formado no puede tumbar la vista previa.
                time.sleep(0.01)

    # ------------------------------------------------------------------
    # Grabación
    # ------------------------------------------------------------------
    def _service_recording(self, now: float, host_ns: int) -> None:
        """Atiende las órdenes de grabación. Corre una vez por frame.

        A 662 fps esto se ejecuta 662 veces por segundo dentro del lazo de
        captura, así que no puede asignar memoria ni tomar cerrojos: las
        banderas son ``threading.Event`` (una lectura atómica) y la duración
        del bloque queda cacheada al armar, en vez de releer el destino de
        grabación en cada iteración.

        El cambio de archivo tampoco bloquea: se **pide** al hilo escritor y
        él lo hace entre dos lotes. El anillo no se toca.
        """
        if self._consume_arm_request():
            self._arm_writer()

        if self._consume_arm_cancel():
            self._release_writer(discard=True)
            self._armed = False
            self._set_state(DeviceState.STREAMING)

        if self._consume_start_request():
            if self._writer is None:
                self._arm_writer()
            if self._writer is not None:
                self._recording = True
                self._set_state(DeviceState.RECORDING)
                offset_ms = ((host_ns - self._t0_ns) / 1e6
                             if (self._t0_ns and host_ns) else 0.0)
                self._log(f"Grabando a disco (offset respecto a T0: "
                          f"{offset_ms:+.2f} ms)")

        # --- modo por bloques: apagado por omisión ----------------------
        chunk_s = self._chunk_seconds
        if self._recording and chunk_s and (now - self._chunk_started) >= chunk_s:
            self._request_rollover()

        # --- el escritor cayó: esto es fatal para la toma ---------------
        if self._writer is not None and self._writer.failed and self._recording:
            self._finish_recording(reason="fallo_de_escritura")

        if self._consume_stop_request():
            self._finish_recording(reason="parada")

    # ------------------------------------------------------------------
    def _record_file(self) -> Path:
        """Nombre del archivo de esta toma.

        Con un archivo por toma no hay sufijo de bloque; solo aparece cuando
        alguien activó a propósito el modo por bloques.
        """
        target = self.record_target
        directory = Path(target["directory"] or ".")
        directory.mkdir(parents=True, exist_ok=True)
        prefix = target["prefix"] or self.label
        if self.simulated:
            prefix = f"{prefix}_SIMULADO"
        stamp = time.strftime("%Y%m%d_%H%M%S")
        if target["chunk_duration_s"]:
            return directory / f"{prefix}_{stamp}_b{self._chunk_index:03d}.raw"
        return directory / f"{prefix}_{stamp}.raw"

    def _record_metadata(self) -> Dict[str, Any]:
        return {
            "programa": f"{config.APP_NAME} {config.APP_VERSION}",
            "dispositivo": self.label,
            "alias": self.alias,
            "identidad": self.identity_key,
            "familia": self.family,
            "simulado": self.simulated,
            "AVISO": ("DATOS SIMULADOS, NO SON UNA MEDICIÓN"
                      if self.simulated else ""),
            "geometria": {k: (v if not hasattr(v, "name") else v.name)
                          for k, v in self._geometry.items()},
            "roi": self._roi.as_dict() if self._roi is not None else None,
            "parametros_camara": {entry["name"]: entry.get("value")
                                  for entry in self._param_cache},
            "temperatura_sensor_c": self._sensor_temp,
            "t0_host_ns": self._t0_ns,
            "reloj": TIME_BASE.snapshot(),
        }

    def _ensure_writer(self, frame_bytes: int) -> bool:
        """Crea el escritor y confirma su anillo. Solo la primera vez.

        Esta es la parte cara —reservar ~680 MB y tocar cada página— y por eso
        ocurre en el armado y **nunca** durante la grabación. Si la geometría
        cambia (otro ROI, otro formato de píxel) el anillo se rehace, pero eso
        también pasa con la cámara parada.
        """
        if self._writer is not None and self._writer_frame_bytes == frame_bytes:
            return True
        if self._writer is not None:
            self._writer.close(reason="cambio_de_geometria")
            self._writer = None
        try:
            writer = DiskStreamWriter(
                frame_bytes=frame_bytes,
                slots=config.EMERGENT_RING_SLOTS,
                batch=config.EMERGENT_WRITE_BATCH,
                on_failure=self._on_writer_failure)
            writer.prefault()
        except MemoryError:
            need = frame_bytes * config.EMERGENT_RING_SLOTS / 1e9
            self._fail(f"No hay memoria para el anillo ({need:.1f} GB). "
                       f"Reduce el ROI o baja EMERGENT_RING_SLOTS.", fatal=False)
            return False
        self._writer = writer
        self._writer_frame_bytes = frame_bytes
        self._log(f"Anillo de escritura reservado: "
                  f"{writer.ring_bytes / 1e6:.0f} MB "
                  f"({config.EMERGENT_RING_SLOTS} slots)")
        return True

    def _arm_writer(self) -> None:
        """Confirma el anillo y abre el archivo de la toma."""
        frame_bytes = self._geometry.get("frame_bytes", 0)
        if not frame_bytes:
            self._fail("Todavía no hay geometría: el stream no está montado.")
            return
        if not self._ensure_writer(frame_bytes):
            return
        assert self._writer is not None
        if self._writer.running:
            return

        self._writer_failure = ""
        self._chunk_index = 0
        self._chunk_summaries = []
        path = self._record_file()
        try:
            self._writer.open(path, self._record_metadata(), t0_ns=self._t0_ns)
        except OSError as exc:
            self._fail(f"No se pudo abrir el archivo de salida: {exc}",
                       fatal=True)
            return

        self._chunk_path = path
        self._chunk_started = time.perf_counter()
        # Se cachea acá para no releer ``record_target`` (que toma un cerrojo)
        # en cada frame del lazo de captura.
        self._chunk_seconds = self.record_target["chunk_duration_s"]
        self._armed = True
        self._set_state(DeviceState.ARMED)
        modo = "un solo archivo" if not self._chunk_seconds else \
            f"bloques de {self._chunk_seconds / 60:.0f} min"
        self._log(f"Armada · {modo} · {path.name}")

    def _request_rollover(self) -> None:
        """Pide el cambio de bloque sin detener nada (modo por bloques)."""
        if self._writer is None:
            return
        self._chunk_index += 1
        path = self._record_file()
        self._writer.request_rotate(path, self._record_metadata())
        self._chunk_path = path
        self._chunk_started = time.perf_counter()

    def _on_writer_failure(self, message: str) -> None:
        """Lo llama el hilo escritor cuando el disco deja de aceptar datos.

        Aquí no se hace trabajo: se levanta la bandera y se avisa. El lazo de
        captura la ve en la próxima vuelta y cierra la grabación como
        corresponde. Lo que no puede pasar —y pasaba antes— es que el escritor
        muera y la cámara siga entregando frames a un ``submit()`` que los
        acepta y los tira.
        """
        self._writer_failure = message
        self.raise_recording_alarm(f"escritura a disco detenida: {message}")

    def _close_writer(self, reason: str = "") -> Dict[str, Any]:
        if self._writer is None:
            return {}
        summary = self._writer.close(reason=reason)
        self._chunk_summaries.append(summary)
        self._chunk_path = None
        self._log(f"Archivo cerrado: {summary.get('frames_escritos', 0)} frames, "
                  f"{summary.get('mb_s_medio', 0)} MB/s medios, "
                  f"peor write {summary.get('peor_write_ms', 0)} ms")
        return summary

    def _release_writer(self, discard: bool = False) -> None:
        """Deshace un armado que no llegó a disparar."""
        if self._writer is None:
            return
        path = self._chunk_path
        self._writer.close(reason="armado_cancelado")
        self._chunk_path = None
        if discard and path is not None:
            # El archivo vacío de un armado cancelado solo confunde después.
            for suffix in ("", "_frames.csv", "_meta.json", "_checkpoint.json"):
                try:
                    candidate = (path if not suffix
                                 else path.with_name(path.stem + suffix))
                    candidate.unlink(missing_ok=True)
                except OSError:
                    pass

    def _finish_recording(self, reason: str = "") -> None:
        if not self._recording and self._writer is None:
            return
        was_recording = self._recording
        self._close_writer(reason=reason)
        self._recording = False
        self._armed = False
        if self.thread_running and not self.should_stop:
            self._set_state(DeviceState.STREAMING)
        if was_recording:
            summary = {
                "dispositivo": self.label,
                "familia": self.family,
                "motivo": reason,
                "archivos": self._chunk_summaries,
                "frames_recibidos": self._received,
                "frames_perdidos_red": self._dropped_net,
                "frames_perdidos_disco": self._dropped_disk,
                "fps_medio": round(self._fps, 2),
                "roi": self._roi.as_dict() if self._roi is not None else None,
                "simulado": self.simulated,
                "fallo_escritura": self._writer_failure,
                "t0_host_ns": self._t0_ns,
            }
            self._publish_recording_summary(summary)
            self._chunk_summaries = []

    # ------------------------------------------------------------------
    def _stats_payload(self) -> Dict[str, Any]:
        writer_stats = self._writer.stats.as_dict() if self._writer else {}
        return {
            "fps": round(self._fps, 1),
            "frames_recibidos": self._received,
            "perdidos_red": self._dropped_net,
            "perdidos_disco": self._dropped_disk,
            "errores": self._errors,
            "mb_s": round(self.estimated_mb_s(), 1),
            "temperatura_c": self._sensor_temp,
            "grabando": self._recording,
            "archivo": self._chunk_path.name if self._chunk_path else "",
            "gb_archivo": round((self._writer.file_bytes if self._writer else 0)
                                / 1e9, 3),
            "simulado": self.simulated,
            "ancho": self._geometry.get("width", 0),
            "alto": self._geometry.get("height", 0),
            "roi": self._roi.rect if self._roi is not None else None,
            "roi_recortado": (not self._roi.is_full_frame
                              if self._roi is not None else False),
            "formato": self._geometry.get("pixel_format_name", ""),
            "frame_mb": round(self._geometry.get("frame_bytes", 0) / 1e6, 3),
            "fallo_escritura": self._writer_failure,
            "disco": writer_stats,
        }
