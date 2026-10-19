"""Gestor del plantel de dispositivos (el "modular aditivo" por dentro).

Todas las pestañas de familia (EvCam, Emergent, Fibras, Anemómetro, IMU) y la
pestaña 1 tienen que ver **los mismos** objetos worker: si la pestaña 2
conecta una segunda cámara de eventos, el LED correspondiente tiene que
aparecer en la pestaña 1 sin que nadie los sincronice a mano.

Este gestor es ese punto único: crea, conecta, desconecta y elimina workers, y
avisa por señales cuando el plantel cambia. Además los registra en el
orquestador del disparo global y encamina sus logs a la consola común.

Por eso "conectar una cámara más" es una sola llamada a ``add()``: la pestaña
de esa familia agrega su panel, la pestaña 1 agrega su fila con LED, y el
presupuesto de disco suma su caudal. Nada más hay que tocar.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from PyQt6.QtCore import QObject, pyqtSignal

from .. import config
from ..devices.base import DeviceState, DeviceWorker
from .aliases import ALIASES
from .device_registry import REGISTRY
from .global_recorder import GlobalRecorder
from .logbook import LOG


class DeviceManager(QObject):
    """Plantel vivo de dispositivos, compartido por todas las pestañas."""

    device_added = pyqtSignal(object)        # DeviceWorker
    device_removed = pyqtSignal(str)         # device_id
    roster_changed = pyqtSignal()
    device_state_changed = pyqtSignal(str, object)   # (device_id, DeviceState)
    log = pyqtSignal(str)
    #: (familia, motivo) cuando el descubrimiento falla. La pestaña lo muestra.
    discovery_failed = pyqtSignal(str, str)
    #: Se levanta cuando alguien intenta tocar el plantel durante una toma.
    locked_action = pyqtSignal(str)

    def __init__(self, recorder: Optional[GlobalRecorder] = None,
                 parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self.recorder = recorder
        self._workers: Dict[str, DeviceWorker] = {}
        #: Último descubrimiento por familia, para poblar los desplegables.
        self._discovered: Dict[str, List[Dict[str, Any]]] = {}
        #: Último error de descubrimiento por familia.
        self._errors: Dict[str, str] = {}

    # ------------------------------------------------------------------
    # Descubrimiento
    # ------------------------------------------------------------------
    def discover(self, family: str) -> List[Dict[str, Any]]:
        """Busca dispositivos de una familia y **dice qué pasó**.

        Si el descubrimiento falla, el motivo se muestra tal cual. Es lo que
        el usuario pidió: fuera del modo ``--demo``, cuando no hay hardware
        tiene que aparecer el error del hardware, no un dispositivo inventado
        que parece funcionar.
        """
        device_type = REGISTRY.get(family)
        name = device_type.label if device_type else family
        try:
            found = REGISTRY.discover(family)
            self._errors.pop(family, None)
        except Exception as exc:
            message = str(exc) or f"{type(exc).__name__}"
            self._errors[family] = message
            self._discovered[family] = []
            LOG.warn(f"[{name}] descubrimiento fallido: {message}")
            self.log.emit(f"[{name}] ✖ {message}")
            self.discovery_failed.emit(family, message)
            return []

        self._discovered[family] = found
        if found:
            self.log.emit(f"[{name}] {len(found)} dispositivo(s) detectado(s): "
                          + ", ".join(str(d.get("label", d.get("id")))
                                      for d in found))
            if any(d.get("simulated") for d in found):
                self.log.emit(f"[{name}] ⚠ MODO DEMOSTRACIÓN: estos "
                              f"dispositivos son simulados.")
        else:
            reason = ("sin dispositivos detectados" if config.simulations_allowed()
                      else "sin dispositivos detectados (no hay hardware "
                           "conectado o el sistema no lo ve)")
            self.log.emit(f"[{name}] {reason}")
        return found

    def discovery_error(self, family: str) -> str:
        """Último motivo por el que falló el descubrimiento de una familia."""
        return self._errors.get(family, "")

    def discovered(self, family: str) -> List[Dict[str, Any]]:
        if family not in self._discovered:
            return self.discover(family)
        return self._discovered[family]

    def discover_all(self) -> None:
        for family in REGISTRY.families():
            self.discover(family)

    def rename(self, device_id: str, alias: str) -> None:
        """Pone o cambia el alias de un equipo. Persiste entre sesiones."""
        worker = self._workers.get(device_id)
        if worker is None:
            return
        worker.set_alias(alias)
        self.roster_changed.emit()

    # ------------------------------------------------------------------
    # Alta y baja
    # ------------------------------------------------------------------
    def add(self, family: str, descriptor: Dict[str, Any],
            connect: bool = True, label: Optional[str] = None) -> Optional[DeviceWorker]:
        """Crea un worker y lo suma al plantel.

        La etiqueta se autonumera dentro de la familia (EvCam1, EvCam2, …) si
        no se pasa una: es la que da nombre a la carpeta de grabación, así que
        conviene que sea estable y legible.
        """
        if self.locked:
            self.locked_action.emit(
                "No se agregan dispositivos con una grabación en curso.")
            return None
        if descriptor.get("simulated") and not config.simulations_allowed():
            self.log.emit("✖ Dispositivo simulado rechazado: el programa no "
                          "está en modo --demo.")
            return None

        device_id = str(descriptor.get("id") or f"{family}_{len(self._workers)}")
        if device_id in self._workers:
            self.log.emit(f"{device_id} ya está en el plantel.")
            return self._workers[device_id]

        # La etiqueta se ancla a la identidad física del equipo y se guarda en
        # disco, para que la misma cámara se llame igual en todas las
        # campañas. El alias que el operador le ponga viaja con ella.
        alias = ""
        if label is None:
            taken = {worker.label for worker in self._workers.values()}
            label, alias, origin = ALIASES.label_for(family, descriptor, taken)
            if origin == "ninguno":
                self.log.emit(
                    f"[{label}] Aviso: no se pudo leer una identidad estable "
                    f"(serial, puerto o IP). El nombre puede cambiar entre "
                    f"sesiones; ponle un alias para reconocerlo.")
        if any(worker.label == label for worker in self._workers.values()):
            label = self.next_label(family)

        try:
            worker = REGISTRY.create_worker(family, device_id, label, descriptor)
        except Exception as exc:
            self.log.emit(f"No se pudo crear {device_id}: {exc}")
            return None

        if alias:
            worker.alias = alias
        worker.log_message.connect(self.log.emit)
        worker.error_occurred.connect(
            lambda message, fatal: self.log.emit(
                ("[ERROR FATAL] " if fatal else "[aviso] ") + message))
        worker.state_changed.connect(
            lambda state, wid=device_id: self._on_state(wid, state))

        self._workers[device_id] = worker
        if self.recorder is not None:
            self.recorder.register(worker)

        self.device_added.emit(worker)
        self.roster_changed.emit()

        if connect:
            worker.connect_device()
        self._rebind_reconstructions()
        return worker

    # ------------------------------------------------------------------
    def _rebind_reconstructions(self) -> None:
        """Engancha cada reconstrucción al flujo de su cámara de eventos.

        La reconstrucción no abre la cámara: se suscribe a la que ya está
        abierta. Esta es la costura que lo hace automático, de modo que
        conectar una EvCam en la pestaña 2 haga aparecer su reconstrucción sin
        que nadie tenga que emparejarlas a mano.
        """
        cameras = self.by_family("event_cam")
        if not cameras:
            return
        for recon in self.by_family("reconstruction"):
            if getattr(recon, "has_source", False):
                continue
            wanted = getattr(recon, "source_label", "")
            serial = getattr(recon, "serial", "")
            match = None
            for camera in cameras:
                if serial and getattr(camera, "serial", "") == serial:
                    match = camera
                    break
                if wanted and camera.label == wanted:
                    match = camera
                    break
            if match is None and len(cameras) == 1:
                match = cameras[0]
            if match is not None:
                try:
                    recon.bind_source(match)
                except Exception as exc:
                    self.log.emit(f"No se pudo enlazar {recon.label}: {exc}")

    def free_event_cameras(self) -> List[DeviceWorker]:
        """Cámaras de eventos conectadas y disponibles como fuente."""
        return [w for w in self.by_family("event_cam")
                if w.state is not DeviceState.DISCONNECTED]

    def bind_reconstruction(self, recon_id: str, camera_id: str) -> bool:
        """Cambia a mano la cámara de origen de una reconstrucción."""
        recon = self._workers.get(recon_id)
        camera = self._workers.get(camera_id)
        if recon is None or camera is None:
            return False
        if not hasattr(recon, "bind_source"):
            return False
        recon.bind_source(camera)
        return True

    def next_label(self, family: str) -> str:
        """Siguiente etiqueta libre de la familia."""
        device_type = REGISTRY.get(family)
        stem = {
            "event_cam": "EvCam",
            "emergent": "EmerCam",
            "reconstruction": "Recon",
            "fibers": "Fibras",
            "anemometer": "Anemometro",
            "imu": "IMU",
        }.get(family, (device_type.label if device_type else family))
        existing = {worker.label for worker in self._workers.values()}
        index = 1
        while f"{stem}{index}" in existing:
            index += 1
        return f"{stem}{index}"

    @property
    def locked(self) -> bool:
        """True mientras una sesión está armada, grabando o sellando.

        Durante ese rato el plantel es intocable: quitar un instrumento a
        mitad de toma deja su archivo a medias y el manifiesto mintiendo sobre
        lo que se grabó.
        """
        return bool(self.recorder is not None and self.recorder.busy)

    def remove(self, device_id: str, force: bool = False) -> None:
        if self.locked and not force:
            worker = self._workers.get(device_id)
            name = worker.display_name if worker else device_id
            self.locked_action.emit(
                f"No se puede retirar {name}: hay una grabación en curso. "
                f"Detén la sesión primero.")
            return
        worker = self._workers.pop(device_id, None)
        if worker is None:
            return
        try:
            worker.disconnect_device()
        except Exception as exc:
            self.log.emit(f"Error desconectando {worker.label}: {exc}")
        if self.recorder is not None:
            self.recorder.unregister(device_id)
        self.device_removed.emit(device_id)
        self.roster_changed.emit()
        self.log.emit(f"{worker.label} retirado del plantel.")

    def remove_family(self, family: str) -> None:
        for device_id in [wid for wid, worker in self._workers.items()
                          if worker.family == family]:
            self.remove(device_id)

    def shutdown(self) -> None:
        """Desconecta todo. Se llama al cerrar la ventana."""
        for device_id in list(self._workers):
            self.remove(device_id, force=True)

    # ------------------------------------------------------------------
    # Consultas
    # ------------------------------------------------------------------
    def get(self, device_id: str) -> Optional[DeviceWorker]:
        return self._workers.get(device_id)

    def all(self) -> List[DeviceWorker]:
        return list(self._workers.values())

    def by_family(self, family: str) -> List[DeviceWorker]:
        return [worker for worker in self._workers.values()
                if worker.family == family]

    def count(self, family: Optional[str] = None) -> int:
        if family is None:
            return len(self._workers)
        return len(self.by_family(family))

    def ready_count(self, family: Optional[str] = None) -> int:
        workers = self.all() if family is None else self.by_family(family)
        return sum(1 for worker in workers if worker.is_armable)

    def recording(self) -> bool:
        return any(worker.is_recording for worker in self._workers.values())

    # ------------------------------------------------------------------
    def _on_state(self, device_id: str, state: DeviceState) -> None:
        self.device_state_changed.emit(device_id, state)
