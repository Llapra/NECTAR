"""Contrato común de todo el hardware de NECTAR.

Decisión de arquitectura: QObject + moveToThread
------------------------------------------------
La especificación original decía en un lado que ``DeviceWorker`` hereda de
``QObject`` y en otro que "cada sensor vive en su propio ``QThread``
heredando de ``DeviceWorker``". Son cosas distintas y había que elegir una,
porque el ciclo de vida cambia. Aquí se elige la que Qt recomienda
oficialmente:

* ``DeviceWorker`` **es un QObject** que se mueve a un ``QThread`` propio con
  ``moveToThread()``. No se subclasifica ``QThread`` (el antipatrón que usaban
  ``CameraWorker`` e ``IMUWorker`` en ``main_v14.1.py``).
* Toda la comunicación *hacia* la interfaz va por señales de Qt. Nunca se
  toca un widget desde un hilo de dispositivo.
* Toda la comunicación *hacia* el dispositivo va por primitivas de
  ``threading`` (``Event``, ``Lock``), **no** por la cola de eventos de Qt.
  Esto es deliberado y es la parte que suele hacerse mal: mientras el lazo de
  captura está bloqueado en ``get_frame()`` o en ``ser.read()``, el hilo no
  procesa eventos de Qt, así que un ``invokeMethod`` se quedaría encolado
  hasta el próximo frame. Con banderas atómicas la orden se ve en la
  siguiente iteración del lazo, siempre.

Modelo de grabación
-------------------
``arm_recording() -> start_recording() -> stop_recording()`` describe bien la
ráfaga de la cámara Emergent (reservar, disparar, volcar). Pero hay
dispositivos que graban **en continuo con rollover de bloques** (la cámara de
eventos abre un ``.raw`` nuevo cada N minutos sin que nadie llame a
``stop_recording()`` en medio). Eso queda como detalle interno de cada worker,
y se declara con ``supports_chunking``: quien implemente el próximo
dispositivo no tiene que reinventar la lógica ni preguntarse dónde encaja.

Errores
-------
``DeviceState`` distingue error recuperable de error fatal, en vez de tener un
único ``ERROR``:

* ``ERROR_RECOVERABLE``: el worker sigue vivo y reintenta (cable de red que
  parpadea, timeout de un frame). El LED se pone ámbar.
* ``ERROR_FATAL``: hace falta intervención (no está el SDK, la cámara la tomó
  otro proceso). El LED se pone rojo y el dispositivo sale del disparo global.

En los dos casos el resto de los dispositivos sigue grabando: es el requisito
de aislamiento del dossier.
"""

from __future__ import annotations

import threading
import time
from enum import Enum, auto
from pathlib import Path
from typing import Any, Dict, Optional

from PyQt6.QtCore import QObject, QThread, pyqtSignal

from .. import config
from ..core.aliases import ALIASES, identity_of
from ..core.logbook import LOG
from ..core.time_sync import TIME_BASE


class DeviceState(Enum):
    """Estados posibles de cualquier instrumento."""

    DISCONNECTED = auto()
    CONNECTING = auto()
    READY = auto()
    STREAMING = auto()
    ARMED = auto()
    RECORDING = auto()
    ERROR_RECOVERABLE = auto()
    ERROR_FATAL = auto()


#: Texto que se muestra en la matriz de la pestaña 1.
STATE_LABELS = {
    DeviceState.DISCONNECTED: "APAGADO",
    DeviceState.CONNECTING: "CONECTANDO",
    DeviceState.READY: "LISTO",
    DeviceState.STREAMING: "STREAMING",
    DeviceState.ARMED: "ARMADO",
    DeviceState.RECORDING: "GRABANDO",
    DeviceState.ERROR_RECOVERABLE: "REINTENTANDO",
    DeviceState.ERROR_FATAL: "ERROR",
}

#: Color del LED por estado. Verde solo cuando de verdad puede grabar.
STATE_COLORS = {
    DeviceState.DISCONNECTED: "#4a4a4a",
    DeviceState.CONNECTING: "#00aaaa",
    DeviceState.READY: "#00cc44",
    DeviceState.STREAMING: "#00ff55",
    DeviceState.ARMED: "#ffcc00",
    DeviceState.RECORDING: "#ff3333",
    DeviceState.ERROR_RECOVERABLE: "#ff9900",
    DeviceState.ERROR_FATAL: "#cc0000",
}

#: Estados en los que el dispositivo puede entrar a un disparo global.
ARMABLE_STATES = (DeviceState.READY, DeviceState.STREAMING)


class DeviceWorker(QObject):
    """Base abstracta de todo dispositivo.

    Un worker se crea en el hilo de la interfaz, se le llama
    ``start_thread()`` y desde ese momento su lazo ``run()`` corre en su propio
    hilo. Los métodos de control se pueden llamar desde cualquier hilo.
    """

    # --- señales hacia la interfaz -------------------------------------
    state_changed = pyqtSignal(object)        # DeviceState
    stats_updated = pyqtSignal(dict)          # telemetría instantánea
    log_message = pyqtSignal(str)
    error_occurred = pyqtSignal(str, bool)    # (mensaje, es_fatal)
    recording_finished = pyqtSignal(dict)     # resumen del bloque/sesión
    preview_ready = pyqtSignal(object)        # np.ndarray listo para pintar
    info_ready = pyqtSignal(dict)             # datos estáticos del equipo
    #: El dispositivo se cayó **mientras grababa**. Es distinto de un error
    #: cualquiera: significa que esta toma ya tiene un agujero, y la pestaña 1
    #: lo muestra con el LED rojo parpadeando hasta que el operador lo
    #: reconoce. No se apaga solo aunque el equipo se recupere.
    recording_alarm = pyqtSignal(str)

    # --- identidad de la familia (la sobreescribe cada subclase) -------
    family = "generic"
    family_label = "Dispositivo"
    #: El worker abre y cierra bloques por su cuenta durante la grabación.
    supports_chunking = False
    #: Caudal nominal aproximado, en MB/s. Alimenta el presupuesto de disco.
    nominal_mb_s = 0.0
    #: Se pone en True en los workers que corren contra un simulador. Fuera de
    #: ``--demo`` un worker simulado no se crea y, si se creara, no arma.
    simulated = False

    def __init__(self, device_id: str, label: str,
                 descriptor: Optional[Dict[str, Any]] = None) -> None:
        super().__init__()
        self.device_id = str(device_id)
        self.label = str(label)
        self.descriptor: Dict[str, Any] = dict(descriptor or {})

        #: Clave estable del equipo (serial, puerto, IP) y de dónde salió.
        #: Es lo que ancla la etiqueta y el alias entre campañas.
        self.identity_key, self.identity_source = identity_of(
            self.family, self.descriptor)
        self.alias: str = ALIASES.alias(self.identity_key)

        #: La pestaña 1 puede excluir un dispositivo del disparo global sin
        #: desconectarlo (la casilla junto al LED).
        self.include_in_session = True

        #: Se levanta si el dispositivo falla durante una grabación y no se
        #: baja solo: esa toma quedó comprometida y hay que saberlo.
        self.recording_fault: str = ""

        self._state = DeviceState.DISCONNECTED
        self._state_lock = threading.Lock()

        # --- banderas de control (seguras entre hilos) -----------------
        self._stop_event = threading.Event()
        self._arm_request = threading.Event()
        self._arm_cancel = threading.Event()
        self._rec_start_request = threading.Event()
        self._rec_stop_request = threading.Event()
        self._armed = False
        self._recording = False
        self._control_lock = threading.Lock()

        # --- destino de la grabación -----------------------------------
        self._record_dir: Optional[Path] = None
        self._record_prefix: str = self.label
        self._record_duration: Optional[float] = None
        #: None = un solo archivo por toma (lo normal). Un número = modo por
        #: bloques de esa duración, que hay que pedir a propósito.
        self._chunk_duration_s: Optional[float] = None
        self._t0_ns: Optional[int] = None

        self._last_stats: Dict[str, Any] = {}
        self._last_recording_summary: Dict[str, Any] = {}
        self._thread: Optional[QThread] = None

    # ------------------------------------------------------------------
    # Infraestructura de hilo
    # ------------------------------------------------------------------
    def start_thread(self) -> None:
        """Mueve el worker a su hilo y arranca ``run()``."""
        if self._thread is not None:
            return
        self._stop_event.clear()
        thread = QThread()
        thread.setObjectName(f"{self.family}-{self.device_id}")
        self.moveToThread(thread)
        thread.started.connect(self.run)
        self._thread = thread
        thread.start()

    def stop_thread(self, timeout_ms: int = 4000) -> None:
        """Pide la parada y espera a que el hilo termine."""
        self._stop_event.set()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.quit()
            if not thread.wait(timeout_ms):
                thread.terminate()
                thread.wait(500)

    @property
    def thread_running(self) -> bool:
        return self._thread is not None and self._thread.isRunning()

    def run(self) -> None:
        """Lazo principal del dispositivo. Lo implementa cada subclase."""
        raise NotImplementedError

    # ------------------------------------------------------------------
    # Estado
    # ------------------------------------------------------------------
    @property
    def state(self) -> DeviceState:
        with self._state_lock:
            return self._state

    def _set_state(self, state: DeviceState) -> None:
        with self._state_lock:
            if self._state is state:
                return
            self._state = state
        self.state_changed.emit(state)

    @property
    def is_armable(self) -> bool:
        return self.state in ARMABLE_STATES

    @property
    def is_recording(self) -> bool:
        return self._recording

    @property
    def state_label(self) -> str:
        return STATE_LABELS.get(self.state, "?")

    @property
    def state_color(self) -> str:
        return STATE_COLORS.get(self.state, "#4a4a4a")

    # ------------------------------------------------------------------
    # Utilidades para las subclases
    # ------------------------------------------------------------------
    def _log(self, message: str) -> None:
        text = f"[{self.display_name}] {message}"
        LOG.info(text)
        self.log_message.emit(text)

    def _fail(self, message: str, fatal: bool = False) -> None:
        """Marca un error. Si ocurre grabando, además levanta la alarma.

        La distinción importa: el mismo timeout de red es una molestia con la
        cámara parada y un agujero en los datos con la cámara grabando.
        """
        self._set_state(DeviceState.ERROR_FATAL if fatal
                        else DeviceState.ERROR_RECOVERABLE)
        text = f"[{self.display_name}] {message}"
        if fatal:
            LOG.error(text)
        else:
            LOG.warn(text)
        self.error_occurred.emit(text, fatal)
        if self._recording or self._armed:
            self.raise_recording_alarm(message)

    def raise_recording_alarm(self, message: str) -> None:
        """Deja constancia de que esta toma quedó comprometida."""
        if not self.recording_fault:
            self.recording_fault = message
        LOG.critical(f"[{self.display_name}] FALLO DURANTE LA GRABACIÓN: "
                     f"{message}")
        self.recording_alarm.emit(f"[{self.display_name}] {message}")

    def clear_recording_alarm(self) -> None:
        """Solo el operador la baja, desde la interfaz."""
        self.recording_fault = ""

    # ------------------------------------------------------------------
    # Nombre visible
    # ------------------------------------------------------------------
    @property
    def display_name(self) -> str:
        """Lo que se muestra: alias si lo hay, etiqueta si no.

        La etiqueta sigue nombrando la carpeta aunque el alias cambie, para
        que renombrar un equipo en pantalla no parta la trazabilidad de los
        datos ya grabados.
        """
        return f"{self.alias} · {self.label}" if self.alias else self.label

    def set_alias(self, alias: str) -> None:
        self.alias = str(alias).strip()
        ALIASES.set_alias(self.identity_key, self.alias)
        self._log(f"Alias del equipo: "
                  f"{self.alias or '(sin alias)'} [{self.identity_source}]")

    def _emit_stats(self, payload: Dict[str, Any]) -> None:
        payload = dict(payload)
        payload.setdefault("device_id", self.device_id)
        payload.setdefault("label", self.label)
        payload.setdefault("nombre", self.display_name)
        payload.setdefault("family", self.family)
        payload.setdefault("estado", self.state_label)
        payload.setdefault("alarma", self.recording_fault)
        self._last_stats = payload
        self.stats_updated.emit(payload)

    @property
    def last_stats(self) -> Dict[str, Any]:
        return dict(self._last_stats)

    @property
    def should_stop(self) -> bool:
        return self._stop_event.is_set()

    # ------------------------------------------------------------------
    # Contrato de conexión (a implementar)
    # ------------------------------------------------------------------
    def connect_device(self) -> bool:
        raise NotImplementedError

    def disconnect_device(self) -> None:
        raise NotImplementedError

    def start_stream(self) -> None:
        """Vista previa en vivo, sin grabar. Por defecto no hace falta nada."""
        return None

    def stop_stream(self) -> None:
        return None

    # ------------------------------------------------------------------
    # Contrato de grabación
    # ------------------------------------------------------------------
    def arm_recording(self, directory: str | Path, prefix: Optional[str] = None,
                      duration_s: Optional[float] = None,
                      chunk_duration_s: Optional[float] = None) -> bool:
        """Fase 2 del disparo global: pedir al worker que reserve todo.

        Devuelve ``True`` si el pedido fue *aceptado*, no si ya terminó: el
        armado real (crear carpetas, abrir descriptores, confirmar el anillo de
        memoria) ocurre dentro del hilo del dispositivo, porque reservar y
        prefaultear cientos de megabytes en el hilo de la interfaz la dejaría
        congelada. El worker avisa que terminó emitiendo
        ``state_changed(DeviceState.ARMED)``, y el orquestador espera esa
        confirmación antes de disparar.

        ``chunk_duration_s=None`` significa **un solo archivo por toma**, que
        es el modo normal.
        """
        if not self.can_write():
            return False
        if not self.is_armable:
            return False
        with self._control_lock:
            self._record_dir = Path(directory)
            self._record_prefix = prefix or self.label
            self._record_duration = duration_s
            self._chunk_duration_s = chunk_duration_s
        try:
            self._record_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            self._fail(f"No se pudo crear la carpeta de grabación: {exc}", fatal=False)
            return False
        self._arm_cancel.clear()
        self._arm_request.set()
        return True

    def can_write(self) -> bool:
        """Último cortafuegos contra grabar datos inventados.

        Un dispositivo simulado solo puede escribir en modo ``--demo``, y en
        ese modo todo lo que escribe queda marcado (carpeta ``_DEMO``, archivo
        testigo, ``simulado: true`` en los metadatos). Fuera de ``--demo`` ni
        siquiera debería existir el worker; esta comprobación está por si
        alguna vez se crea uno por otra vía.
        """
        if getattr(self, "simulated", False) and not config.simulations_allowed():
            self._fail("Dispositivo simulado: no puede grabar fuera del modo "
                       "--demo. No se escribió nada.", fatal=True)
            return False
        return True

    def start_recording(self, t0_ns: Optional[int] = None) -> None:
        """Fase 3: disparo atómico. Debe ser lo más corto posible."""
        self._t0_ns = t0_ns if t0_ns is not None else TIME_BASE.now_ns()
        self._rec_stop_request.clear()
        self._rec_start_request.set()

    def stop_recording(self) -> None:
        """Fase 4: parar la ingesta y despachar el volcado."""
        self._rec_start_request.clear()
        self._rec_stop_request.set()

    def cancel_arm(self) -> None:
        """Deshace un armado que no llegó a disparar."""
        self._arm_request.clear()
        self._arm_cancel.set()

    # ------------------------------------------------------------------
    # Presupuesto de disco
    # ------------------------------------------------------------------
    def estimated_mb_s(self) -> float:
        """Caudal estimado con la configuración actual.

        La pestaña 1 suma esto sobre los dispositivos armados para decir
        cuánto aguanta el SSD antes de llenarse.
        """
        return self.nominal_mb_s

    def projected_bytes(self, duration_s: Optional[float]) -> float:
        """Tamaño que alcanzaría **el archivo de esta toma**.

        Con un archivo único por toma, esta cifra es la que se compara con el
        techo del sistema de archivos de destino. Sin duración pedida no hay
        proyección posible: se devuelve 0 y quien pregunta decide qué hacer.
        """
        if not duration_s:
            return 0.0
        return self.estimated_mb_s() * 1e6 * float(duration_s)

    # ------------------------------------------------------------------
    def describe(self) -> Dict[str, Any]:
        """Ficha del dispositivo para el manifiesto de la sesión."""
        return {
            "device_id": self.device_id,
            "etiqueta": self.label,
            "alias": self.alias,
            "identidad": self.identity_key,
            "origen_identidad": self.identity_source,
            "familia": self.family,
            "estado": self.state_label,
            "simulado": bool(getattr(self, "simulated", False)),
            "incluido_en_sesion": self.include_in_session,
            "mb_s_estimado": round(self.estimated_mb_s(), 3),
            "descriptor": self.descriptor,
        }

    @property
    def last_recording_summary(self) -> Dict[str, Any]:
        """Resumen del último cierre. Lo recoge el manifiesto de la sesión."""
        return dict(self._last_recording_summary)

    def _publish_recording_summary(self, summary: Dict[str, Any]) -> None:
        summary = dict(summary)
        summary.setdefault("device_id", self.device_id)
        summary.setdefault("etiqueta", self.label)
        summary.setdefault("alias", self.alias)
        if self.recording_fault:
            summary["fallo_durante_grabacion"] = self.recording_fault
        self._last_recording_summary = summary
        self.recording_finished.emit(summary)

    # ------------------------------------------------------------------
    def _sleep(self, seconds: float) -> None:
        """Espera interrumpible: no retrasa una orden de parada."""
        self._stop_event.wait(seconds)

    def _wait_for_start_request(self, timeout: float = 0.1) -> bool:
        return self._rec_start_request.wait(timeout)

    def _consume_arm_request(self) -> bool:
        """True una sola vez, cuando hay un armado pendiente por atender."""
        if self._arm_request.is_set():
            self._arm_request.clear()
            return True
        return False

    def _consume_arm_cancel(self) -> bool:
        if self._arm_cancel.is_set():
            self._arm_cancel.clear()
            return True
        return False

    def record_stem(self, index: int = 0) -> str:
        """Nombre base del archivo de esta toma, sin extensión.

        Tres cosas quedan garantizadas para todos los instrumentos por igual:
        el sufijo ``_bNNN`` aparece **solo** si alguien pidió el modo por
        bloques (con un archivo por toma sobra y confunde), los datos
        simulados llevan ``_SIMULADO`` en el propio nombre del archivo, y la
        marca de tiempo es la misma en todos porque se toma al abrir.
        """
        target = self.record_target
        prefix = target["prefix"] or self.label
        if getattr(self, "simulated", False):
            prefix = f"{prefix}_SIMULADO"
        stamp = time.strftime("%Y%m%d_%H%M%S")
        suffix = f"_b{index:03d}" if target["chunk_duration_s"] else ""
        return f"{prefix}_{stamp}{suffix}"

    def record_metadata(self, **extra: Any) -> Dict[str, Any]:
        """Metadatos comunes que acompañan a cualquier archivo grabado."""
        document: Dict[str, Any] = {
            "programa": f"{config.APP_NAME} {config.APP_VERSION}",
            "dispositivo": self.label,
            "alias": self.alias,
            "identidad": self.identity_key,
            "familia": self.family,
            "simulado": bool(getattr(self, "simulated", False)),
            "AVISO": ("DATOS SIMULADOS, NO SON UNA MEDICIÓN"
                      if getattr(self, "simulated", False) else ""),
            "t0_host_ns": self._t0_ns,
            "reloj": TIME_BASE.snapshot(),
            "creado": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        document.update(extra)
        return document

    @property
    def record_target(self) -> Dict[str, Any]:
        """Parámetros del armado pendiente/vigente, leídos con cerrojo."""
        with self._control_lock:
            return {
                "directory": self._record_dir,
                "prefix": self._record_prefix,
                "duration_s": self._record_duration,
                "chunk_duration_s": self._chunk_duration_s,
            }

    def _consume_start_request(self) -> bool:
        if self._rec_start_request.is_set():
            self._rec_start_request.clear()
            return True
        return False

    def _consume_stop_request(self) -> bool:
        if self._rec_stop_request.is_set():
            self._rec_stop_request.clear()
            return True
        return False


class SimulatedMixin:
    """Marca los workers que corren contra un simulador.

    El modo simulado permite ensayar el disparo síncrono de seis instrumentos,
    la estructura de carpetas y las diez pestañas sin tener el banco óptico
    montado, que es exactamente lo que hay que hacer antes de viajar a un
    observatorio. Pero vive detrás de una puerta cerrada:

    * fuera de ``python main.py --demo`` los descubridores no ofrecen
      dispositivos simulados: si el hardware no aparece, se muestra el error
      del hardware;
    * ``DeviceWorker.can_write()`` rechaza el armado de un worker simulado
      fuera de ``--demo``, por si alguno se crease por otra vía;
    * dentro de ``--demo``, todo lo que se escribe queda marcado: la carpeta
      de sesión lleva ``_DEMO``, se deja un archivo testigo y los metadatos
      dicen ``simulado: true``.

    El anemómetro, que todavía no existe como hardware, solo tiene este modo.
    """

    simulated = True

    @staticmethod
    def sim_label(label: str) -> str:
        return f"{label} (SIM)"
