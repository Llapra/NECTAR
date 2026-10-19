"""Reconstrucción de intensidad a partir del flujo de eventos.

Es el ``ReconWorker`` de ``main_v14.1.py``, que resolvía un problema concreto:
una cámara de eventos no entrega imágenes, entrega cambios. Para enfocar a
ojo, o para mirar el blanco como lo vería una cámara normal, hay que
reconstruir la intensidad.

La física
---------
Cada evento significa que el logaritmo de la irradiancia de ese píxel cruzó
un umbral: ``+c_on`` si subió, ``-c_off`` si bajó. Integrando esos saltos se
recupera ``log I`` y exponenciando se recupera ``I``:

.. math::

    \\log I(x,y,t) = \\log I(x,y,t_0) + \\sum_i \\pm c

El problema del integrador puro es que acumula error para siempre (un píxel
que disparó una vez de más queda brillante eternamente). Por eso se agrega un
decaimiento exponencial hacia el gris, con constante de tiempo ``tau``:

.. math::

    \\log I \\leftarrow \\log I \\cdot e^{-\\Delta t / \\tau}

``tau`` es, literalmente, la memoria visual de la reconstrucción: corto
(≈10 ms) muestra solo lo que se mueve ahora; largo (≈1 s) deja estelas pero da
una imagen más estable del blanco estático. Para enfocar la grilla de 10 x 10
manchas conviene un valor intermedio.

Este worker participa del disparo global como cualquier otro dispositivo, pero
lo que graba no son datos crudos: es el **video** de la reconstrucción (MP4).
Los datos crudos ya los está guardando la pestaña 2; aquí lo que tiene valor
es el registro visual de lo que vio el operador.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import numpy as np

from ... import config
from ...core.algorithms.jit_render import integrate_events
from ...core.time_sync import TIME_BASE
from ..base import DeviceState, DeviceWorker
from ..event_cam.simulation import EventSimulator, SimConfig
from ..event_cam.worker import HAS_METAVISION, discover_event_cams

try:
    import cv2
    HAS_CV2 = True
except ImportError:
    HAS_CV2 = False


def discover_reconstruction() -> List[Dict[str, Any]]:
    """Una entrada por cámara de eventos disponible.

    La reconstrucción no es un instrumento: es una vista derivada de una
    cámara concreta, así que se ofrece una por cámara.
    """
    try:
        cameras = discover_event_cams()
    except Exception as exc:
        # El motivo completo ya lo informa la pestaña ②; repetirlo entero
        # aquí solo llena el registro con el mismo párrafo dos veces.
        primera = str(exc).strip().splitlines()[0]
        raise RuntimeError(
            f"Depende de una cámara de eventos, y no hay ninguna: {primera} "
            f"(el detalle está en el registro de la pestaña ② EvCam)") from exc

    entries: List[Dict[str, Any]] = []
    for camera in cameras:
        entries.append({
            "id": f"recon_{camera['id']}",
            "label": f"Recon_{camera['label']}",
            "serial": camera.get("serial", ""),
            "source_label": camera["label"],
            "simulated": bool(camera.get("simulated", False)),
        })
    return entries


class ReconstructionWorker(DeviceWorker):
    """Integrador de Euler sobre el flujo de eventos, con salida en video."""

    family = "reconstruction"
    family_label = "Recons. EvCam"
    supports_chunking = False
    #: MJPG 640x480 a 30 fps ronda los 3 MB/s.
    nominal_mb_s = 3.0

    def __init__(self, device_id: str, label: str,
                 descriptor: Optional[Dict[str, Any]] = None) -> None:
        super().__init__(device_id, label, descriptor)
        self.serial: str = self.descriptor.get("serial", "")
        self.simulated: bool = bool(self.descriptor.get("simulated", False))
        width, height = config.EVENT_SENSOR_SIZE
        self.width, self.height = width, height

        # Parámetros físicos del integrador (los mueve la pestaña 4).
        self.tau = 0.1
        self.c_on = 0.15
        self.c_off = 0.15
        self.accumulation_time_us = 16666      # ~60 Hz de integración
        self.display_fps = 30

        #: Cámara de la que se deriva el flujo. La reconstrucción **no** abre
        #: la cámara por su cuenta: se suscribe a la que ya está abierta en la
        #: pestaña 2. Ver ``EventCamWorker.add_consumer``.
        self.source_label: str = self.descriptor.get("source_label", "")
        self._source = None
        self._mailbox: Optional[tuple] = None
        self._mailbox_lock = threading.Lock()
        self._mailbox_ready = threading.Condition(self._mailbox_lock)
        self._mailbox_busy = False
        self._next_accept = 0.0

        self._simulator: Optional[EventSimulator] = None
        self._video = None
        self._video_path: Optional[Path] = None
        self._frames_written = 0
        self._rate_mev_s = 0.0
        self._events_window = 0
        self._reopen = False
        self._gui_ready = True

    # ------------------------------------------------------------------
    # Suscripción al flujo de una cámara ya abierta
    # ------------------------------------------------------------------
    def bind_source(self, worker) -> None:
        """Engancha la reconstrucción al flujo de una ``EvCam`` conectada."""
        if self._source is worker:
            return
        self.unbind_source()
        self._source = worker
        self.source_label = getattr(worker, "label", "")
        self.width = getattr(worker, "width", self.width)
        self.height = getattr(worker, "height", self.height)
        worker.add_consumer(self)
        self._reopen = True
        self._log(f"Reconstruyendo desde {worker.display_name} "
                  f"(flujo derivado, sin abrir la cámara otra vez)")

    def unbind_source(self) -> None:
        if self._source is not None:
            try:
                self._source.remove_consumer(self)
            except Exception:
                pass
        self._source = None
        self._reopen = True

    @property
    def has_source(self) -> bool:
        return self._source is not None

    # --- protocolo de consumidor (lo llama el hilo de la cámara) -------
    def wants_sample(self, now: float) -> bool:
        if now < self._next_accept:
            return False
        with self._mailbox_lock:
            return not self._mailbox_busy

    def submit(self, events, delta_t_s: float, now: float) -> bool:
        """Recibe una copia del bloque. Se ejecuta en el hilo de la cámara.

        Solo copia y avisa: todo el trabajo de integración ocurre en el hilo
        propio de la reconstrucción.
        """
        with self._mailbox_lock:
            if self._mailbox_busy:
                return False
            self._mailbox = (events.copy(), float(delta_t_s))
            self._mailbox_busy = True
            self._next_accept = now + 1.0 / max(1, self.display_fps)
            self._mailbox_ready.notify()
        return True

    # ------------------------------------------------------------------
    def set_physics(self, tau: float, c_on: float, c_off: float) -> None:
        self.tau = max(0.001, float(tau))
        self.c_on = max(0.001, float(c_on))
        self.c_off = max(0.001, float(c_off))

    def set_accumulation(self, delta_t_us: int) -> None:
        value = int(max(200, min(100000, delta_t_us)))
        if value != self.accumulation_time_us:
            self.accumulation_time_us = value
            self._reopen = True

    def acknowledge(self) -> None:
        """La interfaz avisa que ya pintó el frame anterior."""
        self._gui_ready = True

    def connect_device(self) -> bool:
        self.start_thread()
        return True

    def disconnect_device(self) -> None:
        self.stop_recording()
        self.unbind_source()
        self.stop_thread()
        self._set_state(DeviceState.DISCONNECTED)

    def estimated_mb_s(self) -> float:
        return self.nominal_mb_s if HAS_CV2 else 0.0

    # ------------------------------------------------------------------
    def run(self) -> None:
        self._set_state(DeviceState.CONNECTING)
        while not self.should_stop:
            try:
                self._open_source()
                self._set_state(DeviceState.STREAMING)
                self._loop()
            except Exception as exc:
                self._fail(f"{type(exc).__name__}: {exc}", fatal=False)
                self._sleep(1.0)
            finally:
                self._close_source()
            if self.should_stop:
                break
        self._set_state(DeviceState.DISCONNECTED)

    def _open_source(self) -> None:
        """Prepara la fuente. **Nunca abre la cámara.**

        Si no hay una ``EvCam`` conectada a la que suscribirse, la
        reconstrucción se queda esperando y lo dice. Antes abría su propio
        ``EventsIterator``, lo que con hardware real le habría quitado la
        cámara a la grabación, o simplemente habría fallado.
        """
        self._reopen = False
        if self._source is not None:
            self.width = getattr(self._source, "width", self.width)
            self.height = getattr(self._source, "height", self.height)
            return
        if self.simulated and config.simulations_allowed():
            self._simulator = EventSimulator(
                SimConfig(width=self.width, height=self.height))
            self._log("Reconstrucción en modo simulado (--demo)")
            return
        raise RuntimeError(
            "Sin cámara de origen. Conecta una EvCam en la pestaña ② y "
            "selecciónala aquí como fuente: la reconstrucción se deriva del "
            "flujo ya abierto, no abre la cámara otra vez.")

    def _close_source(self) -> None:
        self._finish_recording(reason="cierre")
        self._simulator = None

    def _iterate(self):
        if self._source is not None:
            # Flujo derivado: se espera a que la cámara deje un bloque en el
            # buzón. Si no llega nada en medio segundo se devuelve un bloque
            # vacío, para que el decaimiento del integrador siga corriendo y
            # la imagen no se congele con la última estela.
            empty = np.empty(0, dtype=EventSimulator.DTYPE)
            while not self.should_stop and not self._reopen:
                with self._mailbox_lock:
                    if self._mailbox is None:
                        self._mailbox_ready.wait(0.5)
                    payload, self._mailbox = self._mailbox, None
                if payload is None:
                    yield empty
                    continue
                try:
                    yield payload[0]
                finally:
                    with self._mailbox_lock:
                        self._mailbox_busy = False
            return
        if self.simulated:
            assert self._simulator is not None
            period = self.accumulation_time_us / 1e6
            while not self.should_stop and not self._reopen:
                start = time.perf_counter()
                yield self._simulator.generate(self.accumulation_time_us)
                remaining = period - (time.perf_counter() - start)
                if remaining > 0:
                    time.sleep(remaining)

    # ------------------------------------------------------------------
    def _loop(self) -> None:
        # Memoria estática: reservar una vez y reutilizar. Asignar arreglos de
        # 640x480 float32 en cada ventana haría trabajar al recolector de
        # basura justo en el camino crítico.
        log_i = np.zeros((self.height, self.width), dtype=np.float32)
        intensity = np.zeros((self.height, self.width), dtype=np.float32)

        last_visual = time.perf_counter()
        window_start = last_visual
        next_stats = window_start + config.STATS_PERIOD_S

        for events in self._iterate():
            now = time.perf_counter()
            n_events = int(len(events)) if events is not None else 0
            self._events_window += n_events

            # --- decaimiento exponencial ------------------------------
            dt_s = self.accumulation_time_us * 1e-6
            log_i *= float(np.exp(-dt_s / self.tau))

            # --- integración de los saltos de contraste ---------------
            if n_events:
                integrate_events(log_i, events["x"], events["y"], events["p"],
                                 self.c_on, self.c_off)

            self._service_recording(now)

            # El ritmo de render manda sobre la grabación de video; la
            # contrapresión de la interfaz solo decide si además se *emite* el
            # frame a la pantalla. Si se mezclaran las dos cosas, una pestaña
            # en segundo plano dejaría el video vacío, que es justo lo que no
            # puede pasar durante una campaña.
            visual_period = 1.0 / max(1, self.display_fps)
            if (now - last_visual) >= visual_period:
                np.exp(log_i, out=intensity)

                # Normalización robusta por percentiles, sobre una submatriz:
                # calcular percentiles sobre 640x480 completo en cada frame es
                # innecesario y se nota.
                sample = intensity[::4, ::4]
                low, high = np.percentile(sample, (1.0, 99.0))
                if high > low:
                    normalized = np.clip((intensity - low) / (high - low) * 255.0,
                                         0, 255).astype(np.uint8)
                else:
                    normalized = np.zeros((self.height, self.width),
                                          dtype=np.uint8)

                frame = np.stack((normalized,) * 3, axis=-1)
                self._write_video_frame(frame)
                if self._gui_ready:
                    self._gui_ready = False
                    self.preview_ready.emit(frame)
                last_visual = now

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

    # ------------------------------------------------------------------
    # Grabación de video
    # ------------------------------------------------------------------
    def _service_recording(self, now: float) -> None:
        if self._consume_arm_request():
            if not HAS_CV2:
                self._fail("OpenCV no está instalado: no se puede grabar video.",
                           fatal=False)
            else:
                self._armed = True
                self._set_state(DeviceState.ARMED)
                self._log("Armada (video MP4).")

        if self._consume_arm_cancel():
            self._armed = False
            self._set_state(DeviceState.STREAMING)

        if self._consume_start_request():
            if self._open_video():
                self._recording = True
                self._set_state(DeviceState.RECORDING)

        if self._consume_stop_request():
            self._finish_recording(reason="parada")

    def _open_video(self) -> bool:
        if not HAS_CV2:
            return False
        target = self.record_target
        directory = Path(target["directory"] or ".")
        try:
            directory.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            self._fail(f"Carpeta inaccesible: {exc}")
            return False
        prefix = target["prefix"] or self.label
        stamp = time.strftime("%Y%m%d_%H%M%S")
        self._video_path = directory / f"{prefix}_{stamp}.mp4"
        # mp4v es el códec que acepta el contenedor .mp4 en las tres
        # plataformas. El programa anterior pedía MJPG y FFmpeg caía de vuelta
        # a mp4v con una advertencia en consola; se pide directo el que va.
        fourcc = cv2.VideoWriter_fourcc(*"mp4v")
        self._video = cv2.VideoWriter(str(self._video_path), fourcc,
                                      float(self.display_fps), (640, 480))
        if not self._video.isOpened():
            self._fail("OpenCV no pudo abrir el archivo de video.")
            self._video = None
            return False
        self._frames_written = 0
        self._log(f"Grabando video: {self._video_path.name}")
        return True

    def _write_video_frame(self, frame: np.ndarray) -> None:
        if not self._recording or self._video is None:
            return
        try:
            contiguous = np.ascontiguousarray(frame)
            bgr = cv2.cvtColor(contiguous, cv2.COLOR_RGB2BGR)
            if bgr.shape[1] != 640 or bgr.shape[0] != 480:
                bgr = cv2.resize(bgr, (640, 480))
            self._video.write(bgr)
            self._frames_written += 1
        except Exception as exc:
            self._log(f"Error escribiendo video: {exc}")

    def _finish_recording(self, reason: str = "") -> None:
        if not self._recording and self._video is None:
            return
        was_recording = self._recording
        self._recording = False
        self._armed = False
        if self._video is not None:
            try:
                self._video.release()
            except Exception:
                pass
            self._video = None
        if self.thread_running and not self.should_stop:
            self._set_state(DeviceState.STREAMING)
        if was_recording:
            self._publish_recording_summary({
                "dispositivo": self.label,
                "familia": self.family,
                "motivo": reason,
                "archivo": self._video_path.name if self._video_path else "",
                "frames": self._frames_written,
                "tau": self.tau,
                "c_on": self.c_on,
                "c_off": self.c_off,
                "t0_host_ns": self._t0_ns,
            })
            self._log(f"Video cerrado: {self._frames_written} frames")

    # ------------------------------------------------------------------
    def _stats_payload(self) -> Dict[str, Any]:
        return {
            "tasa_mev_s": round(self._rate_mev_s, 3),
            "tau_s": self.tau,
            "c_on": self.c_on,
            "c_off": self.c_off,
            "delta_t_us": self.accumulation_time_us,
            "grabando": self._recording,
            "frames_video": self._frames_written,
            "archivo": self._video_path.name if self._video_path else "",
            "simulado": self.simulated,
            "opencv": HAS_CV2,
            "fuente": self.source_label,
            "con_fuente": self.has_source,
        }
