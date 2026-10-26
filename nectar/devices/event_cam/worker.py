"""Worker de la cámara de eventos (Prophesee / Metavision · SilkyEvCam).

Es la evolución de ``CameraWorker`` de ``main_v14.1.py``: se conserva todo lo
que funcionaba bien y se reordena bajo la interfaz ``DeviceWorker``.

Lo que se conserva tal cual
---------------------------
* El lazo sobre ``EventsIterator`` parametrizado por la ventana de integración
  Δt (``accumulation_time_us``).
* El juego de biases y sus rangos (ahora compartido entre todas las cámaras).
* El renderizado con el núcleo Numba y la decimación "1 de cada N" para la
  vista previa.
* La contrapresión con la interfaz: no se genera una imagen nueva hasta que la
  anterior fue pintada.

Lo que cambia
-------------
* Ya no hereda de ``QThread``: es un ``QObject`` movido a su hilo.
* **Un archivo por toma.** Se graba directo a la carpeta de destino, sin área
  intermedia y sin cortes. Cortar un ``.raw`` obliga a cerrar y reabrir el
  canal del HAL, y entre las dos llamadas el sensor sigue disparando: esos
  eventos se pierden. Con un solo archivo no hay costura. El modo por bloques
  queda disponible, apagado por omisión.
* **Metadatos al lado, nunca dentro.** El ``.raw`` conserva exactamente el
  formato de Metavision y se abre con sus herramientas; todo lo que NECTAR
  necesita registrar (T0, biases, ROI, modelo, alias del equipo) va en un
  ``_meta.json`` hermano, más un ``_checkpoint.json`` que se sella cada pocos
  segundos y dice hasta dónde el archivo es válido si la toma muere sin
  cerrar.
* **El ROI se programa en el sensor** con la facilidad ``I_ROI`` del HAL, no
  en la pantalla: baja la tasa de verdad y el archivo sale recortado.
* **El enfoque corre en otro hilo.** El lazo de adquisición, a lo sumo, copia
  un bloque de eventos al buzón del calculador cuando este está libre.
* **Otros módulos se derivan de este flujo** en vez de abrir la cámara otra
  vez (ver ``add_consumer``), que es lo que necesita la reconstrucción.
* El modo simulación genera eventos de verdad (ver ``simulation.py``), pero
  solo existe con ``--demo`` y lo que escribe queda marcado como simulado.
"""

from __future__ import annotations

import os
import sys
import threading
import time
import weakref
from pathlib import Path
from typing import Any, Dict, List, Optional

import numpy as np
from PyQt6.QtCore import pyqtSignal

from ... import config
from ...core.algorithms.jit_render import render_events
from ...core.disk_stream import ByteStreamWriter
from ...core.frame_hub import FrameHub
from ...core.time_sync import TIME_BASE
from ..base import DeviceState, DeviceWorker
from . import sdk_locator
from .biases import SHARED_BIASES, BiasSet
from .focus_worker import FocusComputer
from .roi import EventRoi, apply_event_roi, clamp_window, disable_event_roi
from .simulation import EventSimulator, SimConfig

# ---------------------------------------------------------------------------
# Detección del SDK
# ---------------------------------------------------------------------------
# El SDK de Prophesee se instala fuera de pip (en Linux, normalmente en
# /usr/local/lib/pythonX.Y/dist-packages), y un entorno virtual no ve esas
# carpetas. ``sdk_locator`` las busca y las agrega antes de rendirse, para que
# "no está el SDK" signifique de verdad que no está y no "está pero este
# intérprete no lo ve".
_SDK_OK, _SDK_ERROR, _SDK_DIR = sdk_locator.ensure_on_path()
if _SDK_OK:
    from metavision_core.event_io import EventsIterator      # type: ignore
    from metavision_hal import DeviceDiscovery               # type: ignore
    HAS_METAVISION = True
else:
    HAS_METAVISION = False


def metavision_status() -> str:
    """Diagnóstico legible del estado del SDK. Lo usa ``--check``."""
    return sdk_locator.diagnose()


def metavision_sdk_dir():
    """Carpeta del SDK que se agregó al camino, si hubo que agregar alguna."""
    return _SDK_DIR


#: Pausa cuando la cámara no tiene datos listos. Corta comparada con el Δt
#: mínimo (200 µs se consumen igual: el HAL guarda los paquetes mientras tanto).
_IDLE_PAUSE_S = 0.001
_fine_timer_users = 0
_fine_timer_lock = threading.Lock()


def _fine_timer(enable: bool) -> None:
    """Pide (o suelta) el temporizador de 1 ms de Windows.

    Por omisión Windows despierta los ``sleep`` cada ~15.6 ms, y la pausa de
    ``_pause_when_idle`` duraría eso. Se pide solo mientras hay cámaras
    transmitiendo, con un contador para varias cámaras a la vez.
    """
    global _fine_timer_users
    if not sys.platform.startswith("win"):
        return
    with _fine_timer_lock:
        try:
            import ctypes
            if enable:
                if _fine_timer_users == 0:
                    ctypes.windll.winmm.timeBeginPeriod(1)
                _fine_timer_users += 1
            elif _fine_timer_users > 0:
                _fine_timer_users -= 1
                if _fine_timer_users == 0:
                    ctypes.windll.winmm.timeEndPeriod(1)
        except Exception:
            pass


def _pause_when_idle(reader) -> bool:
    """Hace que el lector de Metavision duerma cuando no hay datos.

    ``RawReaderBase._run`` de Metavision 4 pregunta ``poll_buffer()`` en un
    bucle sin pausa: con la SilkyEvCam son ~700 000 vueltas por segundo para
    ~270 paquetes. Ese bucle se queda con el GIL el 64 % del tiempo (medido),
    la interfaz no alcanza a pintar y la vista en vivo se ve a saltos de hasta
    ~100 ms. Con una pausa de 1 ms cuando ``poll_buffer()`` dice 0 el bloqueo
    baja al 18 % y no se pierde ningún evento. Si el SDK cambia y el lector no
    tiene esa forma, no se toca nada. Devuelve True si pidió el temporizador
    fino (quien llama lo suelta con ``_fine_timer(False)``).
    """
    original = getattr(type(reader), "_run", None)
    stream = getattr(reader, "i_events_stream", None)
    if original is None or stream is None or not hasattr(stream, "poll_buffer"):
        return False

    # Referencia débil: si la función guardara al reader (o a su stream), el
    # reader quedaría en un ciclo, el HAL no soltaría la cámara al cerrarla y
    # la siguiente apertura fallaría con "Failed to open camera".
    reader_ref = weakref.ref(reader)

    def _run_with_pause():
        current = reader_ref()
        if current is None or current._decode_done:
            return False
        if current.i_events_stream.poll_buffer() == 0:
            time.sleep(_IDLE_PAUSE_S)
            return True
        return original(current)

    reader._run = _run_with_pause
    _fine_timer(True)
    return True


def _list_serials() -> List[str]:
    """Seriales de las cámaras conectadas, en cualquier versión del SDK.

    Metavision 5 / OpenEB 5 lo llama ``list_serial_numbers()``; Metavision 4
    (el instalador de Windows, 4.6.x) solo tiene ``list()``. Las dos devuelven
    cadenas que ``EventsIterator(input_path=...)`` acepta tal cual.
    """
    lister = getattr(DeviceDiscovery, "list_serial_numbers", None)
    if lister is None:
        lister = DeviceDiscovery.list
    return [str(serial) for serial in lister()]


def _discovery_context() -> str:
    """Con qué intérprete y qué SDK se buscó: para comparar con una consola
    donde la cámara sí aparece."""
    import metavision_hal                                    # type: ignore
    lines = [f"Intérprete: {sys.executable}",
             f"Directorio de trabajo: {os.getcwd()}",
             f"metavision_hal: {getattr(metavision_hal, '__file__', '?')}"]
    try:
        sources = DeviceDiscovery.list_available_sources()
        lines.append(f"Fuentes que ve el HAL: {len(sources)}")
    except Exception as exc:
        lines.append(f"Fuentes que ve el HAL: error {exc}")
    plugins = [p for p in sdk_locator.plugin_dirs() if p.is_dir()]
    for path in plugins:
        try:
            names = sorted(f.name for f in path.iterdir())
        except OSError as exc:
            names = [f"error {exc}"]
        lines.append(f"Plugins en {path}: {', '.join(names) or '(ninguno)'}")
    if not plugins:
        lines.append("No hay carpeta de plugins de CenturyArks instalada.")
    return "\n".join(lines)


def discover_event_cams() -> List[Dict[str, Any]]:
    """Cámaras de eventos conectadas al bus USB.

    Fuera de ``--demo`` devuelve **solo cámaras reales**. Si el SDK no está o
    no hay ninguna conectada, levanta con el motivo: es preferible un error
    claro a una ``EvCam1`` verde que en realidad es un generador de ruido de
    Poisson. En ``--demo`` devuelve dos simuladas y nada más.
    """
    if config.simulations_allowed():
        return [{
            "id": f"evcam_sim_{index}",
            "label": f"EvCam{index}",
            "serial": f"SIM{index:04d}",
            "modelo": config.EVENT_DEFAULT_MODEL,
            "simulated": True,
        } for index in (1, 2)]

    if not HAS_METAVISION:
        raise RuntimeError(sdk_locator.diagnose())

    serials = _list_serials()
    if not serials:
        # El HAL devuelve una lista vacía, sin error, tanto si no hay cámara
        # como si otro programa la tiene abierta o falta el plugin. Mejor
        # decir las causas reales que un "no hay hardware" a secas.
        plugin_path = os.environ.get("MV_HAL_PLUGIN_PATH", "") or "(vacía)"
        raise RuntimeError(
            "El SDK de Metavision no ve ninguna cámara de eventos. Causas "
            "habituales:\n"
            "  1. Metavision Studio o metavision_viewer la tienen abierta: "
            "una cámara de eventos solo la puede usar un programa a la vez. "
            "Ciérralos y pulsa DETECTAR.\n"
            "  2. El cable USB no está conectado o la cámara no tiene "
            "alimentación.\n"
            "  3. Falta el plugin del fabricante. MV_HAL_PLUGIN_PATH = "
            f"{plugin_path}\n"
            + _discovery_context())
    return [{
        "id": f"evcam_{serial}",
        "label": f"EvCam{index}",
        "serial": serial,
        "modelo": config.EVENT_DEFAULT_MODEL,
        "simulated": False,
    } for index, serial in enumerate(serials, start=1)]


class EventCamWorker(DeviceWorker):
    """Una cámara de eventos. Se instancia una por cámara conectada."""

    family = "event_cam"
    family_label = "EvCam"
    supports_chunking = True
    #: Caudal típico del flujo crudo de eventos (dossier: 80-200 MB/s).
    nominal_mb_s = 120.0

    #: Métricas de enfoque para la pestaña 8 (solo si ``focus_enabled``).
    focus_metrics_ready = pyqtSignal(dict)

    def __init__(self, device_id: str, label: str,
                 descriptor: Optional[Dict[str, Any]] = None,
                 biases: Optional[BiasSet] = None) -> None:
        super().__init__(device_id, label, descriptor)
        self.serial: str = self.descriptor.get("serial", "")
        #: Sin descriptor explícito se asume **real**. Equivocarse hacia el
        #: hardware da un error visible; equivocarse hacia el simulador da
        #: datos falsos con aspecto de medición.
        self.simulated: bool = bool(self.descriptor.get("simulated", False))
        self.model: str = self.descriptor.get("modelo",
                                              config.EVENT_DEFAULT_MODEL)
        self.biases = biases or SHARED_BIASES

        width, height = config.EVENT_SENSOR_SIZE
        self.width = width
        self.height = height

        self.accumulation_time_us = config.EVENT_ACCUM_US_DEFAULT
        self.display_fps = config.EVENT_DISPLAY_FPS_DEFAULT
        self.sync_mode = "STANDALONE"
        self.show_log = False
        self.focus_enabled = False

        #: Recorte vigente. Cuando ``applied`` es True está programado en el
        #: sensor y el ``.raw`` sale recortado de verdad.
        self.roi = EventRoi(x=0, y=0, width=width, height=height,
                            enabled=False, sensor_width=width,
                            sensor_height=height)
        self._roi_request: Optional[tuple] = None

        self.hub = FrameHub(display_fps=self.display_fps)

        # --- estado interno del lazo -----------------------------------
        self._iterator = None
        self._fine_timer_held = False    # pidió el temporizador de 1 ms
        self._device = None              # dispositivo del HAL
        self._events_stream = None       # ``i_events_stream`` del HAL
        self._biases_facility = None
        self._simulator: Optional[EventSimulator] = None
        self._sim_writer: Optional[ByteStreamWriter] = None
        self._focus: Optional[FocusComputer] = None

        self._chunk_index = 0
        self._chunk_path: Optional[Path] = None
        self._chunk_started = 0.0
        self._chunk_summaries: List[Dict[str, Any]] = []
        self._record_started_ns = 0
        self._record_events = 0
        self._next_checkpoint = 0.0
        self._write_failure = ""

        self._events_total = 0
        self._events_window = 0
        self._rate_mev_s = 0.0
        self._bytes_written = 0
        self._reopen = False

        #: Consumidores del flujo (reconstrucción, y lo que venga). Ver
        #: ``add_consumer``.
        self._consumers: List[Any] = []
        self._consumers_lock = threading.Lock()

        self.biases.subscribe(self._on_bias_changed)

    # ------------------------------------------------------------------
    # Derivación del flujo hacia otros módulos
    # ------------------------------------------------------------------
    def add_consumer(self, consumer: Any) -> None:
        """Deja que otro módulo mire el mismo flujo de eventos.

        Esto arregla un problema de fondo de la primera versión: la pestaña de
        reconstrucción abría **su propio** ``EventsIterator`` sobre la misma
        cámara. Contra el simulador funcionaba; contra hardware real no puede
        funcionar, porque el HAL de Metavision entrega la cámara en exclusiva
        y el segundo que la pida se encuentra con el dispositivo ocupado. En
        el mejor caso la reconstrucción no arranca, y en el peor le quita la
        cámara a la grabación.

        Ahora hay un solo lector —este— y quien quiera los eventos se suscribe.
        Además de correcto es mejor: la reconstrucción funciona **mientras** la
        cámara graba, que es justamente cuando se quiere mirar.

        El contrato del consumidor son dos métodos, los mismos que usa el
        calculador de enfoque: ``wants_sample(now)`` para decir si está libre y
        ``submit(events, delta_t_s, now)`` para recibir una copia. Si no está
        libre no se le copia nada: el lazo de adquisición no espera a nadie.
        """
        with self._consumers_lock:
            if consumer not in self._consumers:
                self._consumers.append(consumer)
        self._log(f"Consumidor del flujo conectado "
                  f"({getattr(consumer, 'label', type(consumer).__name__)})")

    def remove_consumer(self, consumer: Any) -> None:
        with self._consumers_lock:
            if consumer in self._consumers:
                self._consumers.remove(consumer)

    @property
    def consumer_count(self) -> int:
        with self._consumers_lock:
            return len(self._consumers)

    def _feed_consumers(self, events: np.ndarray, delta_t_s: float,
                        now: float) -> None:
        if not self._consumers:
            return
        with self._consumers_lock:
            consumers = list(self._consumers)
        for consumer in consumers:
            try:
                if consumer.wants_sample(now):
                    consumer.submit(events, delta_t_s, now)
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Configuración desde la interfaz (seguro desde el hilo GUI)
    # ------------------------------------------------------------------
    def _on_bias_changed(self, name: str, value: int) -> None:
        facility = self._biases_facility
        if facility is not None:
            try:
                facility.set(name, value)
            except Exception as exc:
                if self.show_log:
                    self._log(f"bias {name} rechazado por el HAL: {exc}")
        if self._simulator is not None and name == "bias_diff_on":
            # En el simulador el umbral ON modula la tasa, igual que en el
            # sensor real: umbral más bajo, más eventos.
            self._simulator.sensitivity = max(0.05, 1000.0 / (value + 1))

    def set_accumulation(self, delta_t_us: int) -> None:
        low, high = config.EVENT_ACCUM_US_RANGE
        value = int(max(low, min(high, delta_t_us)))
        if value != self.accumulation_time_us:
            self.accumulation_time_us = value
            self._reopen = True          # Δt se aplica al reabrir el iterador

    def set_display_fps(self, fps: int) -> None:
        self.display_fps = int(fps)
        self.hub.set_display_fps(fps)

    def set_roi(self, enabled: bool, coords=None) -> None:
        """Programa el recorte **en el sensor**, no solo en la pantalla.

        Esta es la corrección de fondo respecto de la primera versión: antes
        el ROI filtraba los eventos justo antes de pintarlos y el archivo
        grabado seguía conteniendo el cuadro completo. Ahora se manda a la
        facilidad ``I_ROI`` del HAL, los píxeles de fuera dejan de disparar, la
        tasa baja de verdad y el ``.raw`` contiene solo la región elegida.

        El cambio se encola para el hilo del dispositivo: tocar el HAL desde
        el hilo de la interfaz mientras el iterador está leyendo es pedir un
        cuelgue.
        """
        if self._recording:
            self._log("No se cambia el ROI durante una grabación.")
            return
        if enabled and coords:
            x, y, width, height = (int(v) for v in coords)
            self._roi_request = ("set", x, y, width, height)
        elif enabled:
            self._roi_request = ("set", self.roi.x, self.roi.y,
                                 self.roi.width, self.roi.height)
        else:
            self._roi_request = ("clear", 0, 0, 0, 0)

    def snap_roi(self, x: int, y: int, width: int, height: int):
        """Ajusta un rectángulo dibujado a uno que el sensor pueda aceptar."""
        return clamp_window(x, y, width, height, self.width, self.height)

    def set_sync_mode(self, mode: str) -> None:
        self.sync_mode = mode if mode in config.EVENT_SYNC_MODES else "STANDALONE"
        self._reopen = True

    def set_focus_enabled(self, enabled: bool) -> None:
        """Enciende o apaga la tubería de enfoque.

        Apagada cuesta exactamente cero: el lazo de adquisición ni siquiera
        copia los eventos. Es el control desacoplado que pide el método.
        """
        enabled = bool(enabled)
        if enabled == self.focus_enabled:
            return
        self.focus_enabled = enabled
        if enabled:
            if self._focus is None:
                self._focus = FocusComputer(self.width, self.height,
                                            on_report=self._emit_focus)
            self._focus.set_geometry(self.width, self.height)
            self._focus.reset()
            self._focus.start()
            self._log("Modo enfoque activado (cálculo en hilo aparte).")
        elif self._focus is not None:
            self._focus.stop()
            self._log("Modo enfoque desactivado.")

    def set_focus_motorized(self, motorized: bool) -> None:
        if self._focus is not None:
            self._focus.set_motorized(motorized)

    def _emit_focus(self, payload: Dict[str, Any]) -> None:
        """Puente del hilo de cálculo a la interfaz, por señal de Qt."""
        payload.setdefault("device_id", self.device_id)
        payload.setdefault("label", self.label)
        payload.setdefault("nombre", self.display_name)
        self.focus_metrics_ready.emit(payload)

    def set_simulated_defocus(self, value: float) -> None:
        if self._simulator is not None:
            self._simulator.set_defocus(value)

    # ------------------------------------------------------------------
    # Conexión
    # ------------------------------------------------------------------
    def connect_device(self) -> bool:
        """Con esta cámara conectar y abrir el stream es lo mismo."""
        if self.simulated and not config.simulations_allowed():
            self._fail("Cámara simulada fuera del modo --demo: no se conecta.",
                       fatal=True)
            return False
        if not self.simulated and not HAS_METAVISION:
            self._fail(sdk_locator.diagnose(), fatal=True)
            return False
        self.start_thread()
        return True

    def disconnect_device(self) -> None:
        self.stop_recording()
        if self._focus is not None:
            self._focus.stop()
            self._focus = None
        self.stop_thread()
        self.biases.unsubscribe(self._on_bias_changed)
        self._set_state(DeviceState.DISCONNECTED)

    def estimated_mb_s(self) -> float:
        """Caudal esperado, medido si se puede y por modelo si no.

        EVT3 comprime a unos 4 bytes por evento; el formato del simulador usa
        12 bytes fijos, así que el presupuesto de disco tiene que distinguir
        los dos casos o se equivoca por un factor de tres.

        Sin medición todavía se usa la tabla de ``config.EVENT_MODEL_RATES``,
        escalada por la fracción del sensor que el ROI deja activa: recortar a
        un cuarto del cuadro es recortar el caudal en la misma proporción, y
        el presupuesto de la pestaña 1 tiene que reflejarlo antes de disparar,
        no después.
        """
        bytes_per_event = (config.EVENT_SIM_BYTES_PER_EVENT if self.simulated
                           else config.EVENT_BYTES_PER_EVENT)
        if self._rate_mev_s > 0:
            return max(0.1, self._rate_mev_s * bytes_per_event)
        profile = config.EVENT_MODEL_RATES.get(self.model, {})
        typical = float(profile.get("mev_s_tipico", 0.0))
        if typical > 0:
            return max(0.1, typical * bytes_per_event * self.roi.pixel_fraction)
        return self.nominal_mb_s

    def model_profile(self) -> Dict[str, Any]:
        """Ficha del modelo: resolución, enlace y tasas típica y de pico."""
        profile = dict(config.EVENT_MODEL_RATES.get(self.model, {}))
        profile["modelo"] = self.model
        bytes_per_event = (config.EVENT_SIM_BYTES_PER_EVENT if self.simulated
                           else config.EVENT_BYTES_PER_EVENT)
        for key, target in (("mev_s_tipico", "mb_s_tipico"),
                            ("mev_s_pico", "mb_s_pico")):
            if profile.get(key):
                profile[target] = round(profile[key] * bytes_per_event, 1)
        return profile

    # ------------------------------------------------------------------
    # Lazo principal
    # ------------------------------------------------------------------
    def run(self) -> None:
        self._set_state(DeviceState.CONNECTING)
        while not self.should_stop:
            try:
                self._open_source()
                self._set_state(DeviceState.STREAMING)
                self._stream_loop()
            except Exception as exc:
                self._fail(f"{type(exc).__name__}: {exc}", fatal=False)
                self._sleep(1.0)
            finally:
                self._close_source()
            if self.should_stop:
                break
        self._set_state(DeviceState.DISCONNECTED)

    # ------------------------------------------------------------------
    def _open_source(self) -> None:
        self._reopen = False
        if self.simulated or not HAS_METAVISION:
            self.simulated = True
            if self._simulator is None:
                self._simulator = EventSimulator(
                    SimConfig(width=self.width, height=self.height))
                self._on_bias_changed("bias_diff_on",
                                      self.biases.get("bias_diff_on"))
            self._log(f"Modo simulado ({self.width}x{self.height}, "
                      f"Δt={self.accumulation_time_us} µs)")
            return

        self._iterator = EventsIterator(input_path=self.serial or "",
                                        delta_t=self.accumulation_time_us)
        # El HAL cuelga del reader del iterador; el nombre del atributo cambió
        # entre versiones del SDK, así que se prueban las dos formas.
        device = None
        for attribute in ("reader", "_reader"):
            reader = getattr(self._iterator, attribute, None)
            if reader is not None:
                if not self._fine_timer_held:
                    self._fine_timer_held = _pause_when_idle(reader)
                else:
                    _pause_when_idle(reader)
                    _fine_timer(False)      # ya se tenía: no contar dos veces
                device = getattr(reader, "device", None)
                if device is not None:
                    break
        self._device = device
        try:
            geometry = self._iterator.get_size()
            if geometry:
                self.height, self.width = int(geometry[0]), int(geometry[1])
        except Exception:
            pass
        self.roi.sensor_width = self.width
        self.roi.sensor_height = self.height
        if device is not None:
            self._configure_device(device)
            if self.roi.enabled:
                # Reabrir el iterador borra el recorte: hay que reponerlo.
                self._apply_roi_now(self.roi.x, self.roi.y,
                                    self.roi.width, self.roi.height)
        if self._focus is not None:
            self._focus.set_geometry(self.width, self.height)
        self._log(f"Conectada ({self.width}x{self.height}, "
                  f"Δt={self.accumulation_time_us} µs, sync={self.sync_mode})")

    def _configure_device(self, device) -> None:
        """Aplica biases, modo de sincronismo y abre el canal de grabación."""
        try:
            self._biases_facility = device.get_i_ll_biases()
            for name, value in self.biases.as_dict().items():
                try:
                    self._biases_facility.set(name, value)
                except Exception as exc:
                    self._log(f"bias {name}={value} rechazado: {exc}")
        except Exception as exc:
            self._log(f"Sin acceso a los biases: {exc}")

        try:
            sync = device.get_i_camera_synchronization()
            if sync is not None:
                if self.sync_mode == "MASTER":
                    sync.set_mode_master()
                elif self.sync_mode == "SLAVE":
                    sync.set_mode_slave()
                else:
                    sync.set_mode_standalone()
        except Exception as exc:
            self._log(f"Sincronismo por hardware no disponible: {exc}")

        try:
            self._events_stream = device.get_i_events_stream()
        except Exception as exc:
            self._events_stream = None
            self._log(f"Sin canal de grabación cruda: {exc}")

        # --- identificación del equipo ---------------------------------
        # El número de serie no siempre se puede leer en estas cámaras. Eso no
        # es un error del programa, pero sí tiene que verse en la interfaz:
        # el alias del equipo se ancla a esa identidad, y si el ancla es el
        # puerto USB en vez del serial, conviene saberlo.
        identity: Dict[str, Any] = {
            "serial": self.serial,
            "serial_legible": False,
            "modelo": self.model,
            "origen_identidad": self.identity_source,
            "alias": self.alias,
        }
        try:
            info = device.get_i_hw_identification()
            if info is not None:
                for key, getter in (("serial", "get_serial"),
                                    ("integrador", "get_integrator"),
                                    ("sensor", "get_sensor_info")):
                    try:
                        value = getattr(info, getter)()
                    except Exception:
                        continue
                    if value:
                        identity[key] = str(value)
                        if key == "serial":
                            identity["serial_legible"] = True
                            self.serial = str(value)
        except Exception as exc:
            identity["error"] = f"{type(exc).__name__}: {exc}"
        if not identity["serial_legible"]:
            self._log("No se pudo leer el número de serie de la cámara. "
                      "La identidad del equipo queda anclada a "
                      f"'{self.identity_source}'; ponle un alias para "
                      f"reconocerla sin ambigüedad.")
        identity["sensor_ancho"] = self.width
        identity["sensor_alto"] = self.height
        identity["roi"] = self.roi.as_dict()
        self.info_ready.emit(identity)

    # ------------------------------------------------------------------
    def _apply_roi_now(self, x: int, y: int, width: int, height: int) -> None:
        """Programa el recorte en el sensor. Corre en el hilo del dispositivo."""
        if self.simulated:
            x, y, width, height = clamp_window(x, y, width, height,
                                               self.width, self.height)
            self.roi = EventRoi(x=x, y=y, width=width, height=height,
                                enabled=True, sensor_width=self.width,
                                sensor_height=self.height, applied=True,
                                method="simulador")
            if self._simulator is not None:
                self._simulator.set_roi(x, y, width, height)
            self._log(f"ROI del simulador: {width}x{height} en ({x},{y})")
            return
        if self._device is None:
            self._log("No hay dispositivo HAL: el ROI no se puede aplicar.")
            return
        result = apply_event_roi(self._device, x, y, width, height,
                                 self.width, self.height)
        self.roi = result
        if result.applied:
            self._log(f"ROI activo en el sensor: {result.width}x{result.height} "
                      f"en ({result.x},{result.y}) · {result.method} · "
                      f"{result.pixel_fraction * 100:.1f}% de los píxeles")
        else:
            self._fail(f"El ROI NO se aplicó en el sensor: {result.error}. "
                       f"La grabación seguiría con el cuadro completo.",
                       fatal=False)

    def _clear_roi_now(self) -> None:
        if self.simulated:
            self.roi = EventRoi(x=0, y=0, width=self.width, height=self.height,
                                enabled=False, sensor_width=self.width,
                                sensor_height=self.height, applied=True,
                                method="simulador")
            if self._simulator is not None:
                self._simulator.clear_roi()
        elif self._device is not None:
            self.roi = disable_event_roi(self._device, self.width, self.height)
        self._log("ROI desactivado: cuadro completo.")

    def _service_roi(self) -> None:
        request, self._roi_request = self._roi_request, None
        if request is None:
            return
        kind, x, y, width, height = request
        if kind == "clear":
            self._clear_roi_now()
        else:
            self._apply_roi_now(x, y, width, height)

    def _close_source(self) -> None:
        self._finish_recording(reason="cierre_de_stream")
        if self._fine_timer_held:
            _fine_timer(False)
            self._fine_timer_held = False
        self._iterator = None
        self._device = None
        self._events_stream = None
        self._biases_facility = None

    # ------------------------------------------------------------------
    def _iterate(self):
        """Unifica el iterador real y el simulado en un mismo bucle."""
        if self.simulated:
            assert self._simulator is not None
            period = self.accumulation_time_us / 1e6
            while not self.should_stop and not self._reopen:
                start = time.perf_counter()
                yield self._simulator.generate(self.accumulation_time_us)
                # El simulador debe respetar el ritmo del Δt para que las
                # tasas mostradas signifiquen algo.
                remaining = period - (time.perf_counter() - start)
                if remaining > 0:
                    time.sleep(remaining)
        else:
            for events in self._iterator:
                yield events
                if self.should_stop or self._reopen:
                    break

    def _stream_loop(self) -> None:
        """Lazo de adquisición. Aquí solo va lo que no se puede mover.

        Todo lo caro salió de acá: la DFT del enfoque vive en su propio hilo y
        el lazo, a lo sumo, le entrega una copia de un bloque cuando ese hilo
        está libre. El filtrado espacial del ROI también salió, porque ahora lo
        hace el sensor.
        """
        visual = np.zeros((self.height, self.width, 3), dtype=np.uint8)
        delta_t_s = self.accumulation_time_us / 1e6

        window_start = time.perf_counter()
        next_stats = window_start + config.STATS_PERIOD_S

        for events in self._iterate():
            now = time.perf_counter()
            host_ns = TIME_BASE.now_ns()
            n_events = int(len(events)) if events is not None else 0
            self._events_total += n_events
            self._events_window += n_events

            # ---------------- control de grabación ---------------------
            self._service_recording(events, now, host_ns)

            # ---------------- cambios de ROI pedidos desde la GUI ------
            if self._roi_request is not None and not self._recording:
                self._service_roi()

            # ---------------- vista previa -----------------------------
            # Cada Δt se pinta sobre el mismo cuadro, y el cuadro se manda a
            # la pantalla al ritmo de display_fps. Antes solo se pintaba el Δt
            # que coincidía con el refresco y se tiraban los demás: a 20 fps
            # con Δt=10 ms se veía el 20 % del tiempo y el movimiento saltaba.
            if visual.shape[:2] != (self.height, self.width):
                visual = np.zeros((self.height, self.width, 3), dtype=np.uint8)
            if n_events:
                sample = self._decimate(events)
                if sample.size:
                    render_events(visual, sample["x"], sample["y"], sample["p"])
            if self.hub.wants_frame(now):
                self.preview_ready.emit(visual.copy())
                # Contrapresión: hasta que la interfaz acuse recibo no se
                # manda otro cuadro, para que no se encolen y la vista no
                # quede atrasada respecto de la cámara.
                self.hub.hold()
                visual.fill(0)

            # ---------------- enfoque: solo una copia ------------------
            if (self.focus_enabled and n_events and self._focus is not None
                    and self._focus.wants_sample(now)):
                self._focus.submit(events, delta_t_s, now)

            # ---------------- derivación a otros módulos ---------------
            if n_events:
                self._feed_consumers(events, delta_t_s, now)

            # ---------------- telemetría -------------------------------
            if now >= next_stats:
                elapsed = now - window_start
                self._rate_mev_s = (self._events_window / elapsed / 1e6
                                    if elapsed > 0 else 0.0)
                self._emit_stats(self._stats_payload())
                window_start = now
                self._events_window = 0
                next_stats = now + config.STATS_PERIOD_S

            if self.should_stop or self._reopen:
                break

    def _decimate(self, events: np.ndarray) -> np.ndarray:
        """Submuestrea para la pantalla.

        A 20 MEv/s no tiene sentido pintar todos los eventos: con ~10 000 por
        cuadro la imagen ya es la misma y el costo baja tres órdenes. Ya no se
        filtra por ROI acá: si hay recorte, los eventos de fuera directamente
        no existen porque el sensor no los generó.
        """
        step = max(1, len(events) // 10000)
        return events[::step]

    # ------------------------------------------------------------------
    # Grabación
    # ------------------------------------------------------------------
    def _service_recording(self, events, now: float, host_ns: int) -> None:
        # --- armado ----------------------------------------------------
        if self._consume_arm_request():
            target = self.record_target
            try:
                Path(target["directory"]).mkdir(parents=True, exist_ok=True)
                self._chunk_index = 0
                self._chunk_summaries = []
                self._write_failure = ""
                self._armed = True
                self._set_state(DeviceState.ARMED)
                modo = ("un solo archivo" if not target["chunk_duration_s"]
                        else f"bloques de "
                             f"{target['chunk_duration_s'] / 60:.0f} min")
                self._log(f"Armada · {modo} · carpeta lista.")
            except Exception as exc:
                self._fail(f"No se pudo armar: {exc}")

        if self._consume_arm_cancel():
            self._armed = False
            self._set_state(DeviceState.STREAMING)

        # --- disparo ---------------------------------------------------
        if self._consume_start_request():
            if self._open_file(now, host_ns):
                self._recording = True
                self._record_started_ns = host_ns
                self._record_events = 0
                self._set_state(DeviceState.RECORDING)
                offset_ms = (self._t0_ns and (host_ns - self._t0_ns) / 1e6) or 0.0
                self._log(f"Grabando (offset respecto a T0: {offset_ms:+.2f} ms)")

        if self._recording:
            self._record_events += int(len(events)) if events is not None else 0

        # --- escritura del flujo simulado -------------------------------
        if self._recording and self.simulated and self._sim_writer is not None \
                and events is not None and len(events):
            payload = self._simulator.to_raw_bytes(events) if self._simulator else b""
            if payload:
                if self._sim_writer.submit(payload):
                    self._bytes_written += len(payload)
                elif self._sim_writer.failed and not self._write_failure:
                    self._write_failure = self._sim_writer.stats.last_error
                    self.raise_recording_alarm(
                        f"escritura detenida: {self._write_failure}")

        # --- punto de control ------------------------------------------
        # El HAL escribe el .raw por su cuenta y no dice cuánto lleva, así que
        # el punto de control se construye desde afuera: tamaño del archivo,
        # eventos contados y reloj. Es lo que permite saber hasta dónde es
        # válido un archivo que no llegó a cerrarse.
        if self._recording and now >= self._next_checkpoint:
            self._next_checkpoint = now + config.CHECKPOINT_PERIOD_S
            self._write_checkpoint(host_ns)

        # --- modo por bloques: apagado por omisión ----------------------
        chunk_s = self.record_target["chunk_duration_s"]
        if self._recording and chunk_s and (now - self._chunk_started) >= chunk_s:
            self._close_file(reason="rollover")
            self._open_file(now, host_ns)

        # --- parada ----------------------------------------------------
        if self._consume_stop_request():
            self._finish_recording(reason="parada")

    # ------------------------------------------------------------------
    def _file_name(self) -> str:
        target = self.record_target
        prefix = target["prefix"] or self.label
        if self.simulated:
            prefix = f"{prefix}_SIMULADO"
        stamp = time.strftime("%Y%m%d_%H%M%S")
        if target["chunk_duration_s"]:
            return f"{prefix}_{stamp}_b{self._chunk_index:03d}.raw"
        return f"{prefix}_{stamp}.raw"

    def _sidecar_metadata(self, host_ns: int) -> Dict[str, Any]:
        """Metadatos que van **al lado** del ``.raw``, nunca dentro.

        El ``.raw`` de Metavision tiene su propia cabecera y su propio visor.
        Meterle campos extra lo rompería o, peor, lo dejaría abriéndose en
        algunas versiones y no en otras. Así que todo lo que NECTAR necesita
        registrar —T0, biases, ROI, modelo, alias del equipo— va en un
        ``_meta.json`` hermano, y el ``.raw`` se queda intacto y legible por
        cualquier herramienta de Prophesee.
        """
        return {
            "programa": f"{config.APP_NAME} {config.APP_VERSION}",
            "dispositivo": self.label,
            "alias": self.alias,
            "identidad": self.identity_key,
            "origen_identidad": self.identity_source,
            "familia": self.family,
            "modelo": self.model,
            "serial": self.serial,
            "simulado": self.simulated,
            "AVISO": ("DATOS SIMULADOS, NO SON UNA MEDICIÓN"
                      if self.simulated else ""),
            "sensor": {"ancho": self.width, "alto": self.height},
            "roi": self.roi.as_dict(),
            "biases": self.biases.as_dict(),
            "delta_t_us": self.accumulation_time_us,
            "modo_sincronismo": self.sync_mode,
            "formato": "EVT3 (.raw de Metavision, sin modificar)",
            "t0_host_ns": self._t0_ns,
            "inicio_host_ns": host_ns,
            "desfase_t0_ms": ((host_ns - self._t0_ns) / 1e6
                              if (self._t0_ns and host_ns) else None),
            "reloj": TIME_BASE.snapshot(),
            "creado": time.strftime("%Y-%m-%d %H:%M:%S"),
        }

    def _write_sidecar(self, host_ns: int) -> None:
        if self._chunk_path is None:
            return
        import json
        path = self._chunk_path.with_name(self._chunk_path.stem + "_meta.json")
        try:
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(self._sidecar_metadata(host_ns), handle,
                          indent=2, ensure_ascii=False)
        except OSError as exc:
            self._log(f"No se pudo escribir los metadatos: {exc}")

    def _write_checkpoint(self, host_ns: int) -> None:
        if self._chunk_path is None:
            return
        import json
        import os as _os
        try:
            size = self._chunk_path.stat().st_size
        except OSError:
            size = 0
        document = {
            "archivo": self._chunk_path.name,
            "bytes_validos": size,
            "eventos_contados": self._record_events,
            "host_ns": host_ns,
            "t0_host_ns": self._t0_ns,
            "desfase_t0_s": ((host_ns - self._t0_ns) / 1e9
                             if (self._t0_ns and host_ns) else None),
            "duracion_s": ((host_ns - self._record_started_ns) / 1e9
                           if self._record_started_ns else 0.0),
            "hora_local": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        path = self._chunk_path.with_name(
            self._chunk_path.stem + "_checkpoint.json")
        try:
            temporary = path.with_suffix(".json.tmp")
            with open(temporary, "w", encoding="utf-8") as handle:
                json.dump(document, handle, indent=2, ensure_ascii=False)
                handle.flush()
                _os.fsync(handle.fileno())
            temporary.replace(path)
        except OSError:
            pass

    def _open_file(self, now: float, host_ns: int) -> bool:
        target = self.record_target
        directory = Path(target["directory"] or ".")
        try:
            directory.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            self._fail(f"Carpeta de grabación inaccesible: {exc}", fatal=True)
            return False

        self._chunk_path = directory / self._file_name()
        self._chunk_started = now
        self._next_checkpoint = now + config.CHECKPOINT_PERIOD_S

        try:
            if self.simulated:
                self._sim_writer = ByteStreamWriter(
                    self._chunk_path,
                    header=EventSimulator.raw_header(self.width, self.height),
                    on_failure=lambda msg: self.raise_recording_alarm(
                        f"escritura detenida: {msg}"))
                self._sim_writer.start()
            elif self._events_stream is not None:
                self._events_stream.log_raw_data(str(self._chunk_path))
            else:
                self._fail("El HAL no expone canal de grabación cruda: la "
                           "cámara no puede grabar.", fatal=True)
                return False
        except Exception as exc:
            self._fail(f"No se pudo abrir el archivo: {exc}", fatal=True)
            return False

        self._write_sidecar(host_ns)
        if self.roi.enabled and not self.roi.applied:
            self.raise_recording_alarm(
                "se pidió un ROI pero el sensor no lo aceptó: se está "
                "grabando el cuadro completo")
        self._log(f"Archivo abierto → {self._chunk_path.name}")
        return True

    def _close_file(self, reason: str = "") -> None:
        if self._chunk_path is None:
            return
        summary: Dict[str, Any] = {"archivo": self._chunk_path.name,
                                   "motivo": reason}
        try:
            if self.simulated and self._sim_writer is not None:
                summary.update(self._sim_writer.stop())
                self._sim_writer = None
            elif self._events_stream is not None:
                self._events_stream.stop_log_raw_data()
        except Exception as exc:
            self._log(f"Error cerrando el archivo: {exc}")
        try:
            summary["bytes_escritos"] = self._chunk_path.stat().st_size
        except OSError:
            pass
        summary["eventos_contados"] = self._record_events
        self._write_checkpoint(TIME_BASE.now_ns())

        self._chunk_summaries.append(summary)
        self._chunk_index += 1
        self._chunk_path = None

    def _finish_recording(self, reason: str = "") -> None:
        if not self._recording and self._chunk_path is None:
            return
        self._close_file(reason=reason)
        was_recording = self._recording
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
                "eventos_totales": self._events_total,
                "eventos_de_la_toma": self._record_events,
                "tasa_media_mev_s": round(self._rate_mev_s, 3),
                "simulado": self.simulated,
                "modelo": self.model,
                "roi": self.roi.as_dict(),
                "biases": self.biases.as_dict(),
                "delta_t_us": self.accumulation_time_us,
                "fallo_escritura": self._write_failure,
                "t0_host_ns": self._t0_ns,
            }
            self._publish_recording_summary(summary)
            self._log(f"Grabación cerrada: {len(self._chunk_summaries)} "
                      f"archivo(s), {self._record_events:,} eventos")

    # ------------------------------------------------------------------
    def _stats_payload(self) -> Dict[str, Any]:
        return {
            "tasa_mev_s": round(self._rate_mev_s, 3),
            "eventos_totales": self._events_total,
            "mb_s": round(self.estimated_mb_s(), 1),
            "delta_t_us": self.accumulation_time_us,
            "fps_pantalla": self.display_fps,
            "grabando": self._recording,
            "archivo": self._chunk_path.name if self._chunk_path else "",
            "archivos_cerrados": len(self._chunk_summaries),
            "simulado": self.simulated,
            "modelo": self.model,
            "roi": self.roi.rect if self.roi.enabled else None,
            "roi_en_hardware": self.roi.applied and self.roi.enabled,
            "roi_fraccion": round(self.roi.pixel_fraction, 3),
            "sync": self.sync_mode,
            "ancho": self.width,
            "alto": self.height,
            "enfoque": self.focus_enabled,
            "fallo_escritura": self._write_failure,
        }
