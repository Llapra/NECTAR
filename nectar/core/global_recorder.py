"""Orquestador del disparo síncrono global (la pestaña 1 manda).

Protocolo de 5 fases
--------------------
**Fase 1 · Verificación.** Se listan los dispositivos marcados para participar
y en estado armable. Se suma su caudal, se calcula cuánto va a ocupar la
sesión y cuánto va a ocupar el archivo más grande, y se valida el volumen de
destino: que exista, que se pueda escribir, que no sea FAT32 (donde todo
archivo muere a los 4 GB), y que el espacio alcance dejando el margen de
seguridad. Si algo no cuadra, se dice antes de tocar ningún instrumento.

**Fase 2 · Armado.** Se crea la carpeta de la sesión **con el árbol completo
por adelantado** y se manda ``arm_recording()`` a cada worker con su carpeta.
El armado real ocurre en el hilo de cada dispositivo (reservar el anillo,
confirmar páginas, abrir descriptores); el orquestador espera a que *todos*
reporten ``ARMED``, con un tope de tiempo. Si alguno no llega se cancela el
armado de todos: es preferible no grabar que grabar un dataset cojo sin
saberlo.

**Fase 3 · Disparo atómico.** Se fija T0 con el reloj monotónico del host
(``TIME_BASE.mark_zero()``) y se despacha ``start_recording(t0)`` a todos los
workers ya armados, uno tras otro sin nada en medio. Cada dispositivo estampa
ese T0 en sus metadatos, que es lo que permite alinear después frames
discretos, eventos asíncronos y lecturas térmicas de 1 kHz.

**Fase 4 · Vigilancia.** Mientras se graba, cada par de segundos se mira el
espacio libre del destino. Al bajar del margen de seguridad la sesión se
detiene sola y ordenadamente, con tiempo para cerrar archivos y sellar el
manifiesto. Llenar un disco a la fuerza no deja "todo menos el final": deja
archivos truncados sin cierre y, a veces, un sistema que no responde.

**Fase 5 · Parada y sellado.** Se manda ``stop_recording()`` a todos y **se
espera a que cada uno confirme que cerró su archivo** antes de sellar el
manifiesto. Esto es distinto de la primera versión, que sellaba de inmediato:
los resúmenes por dispositivo —frames perdidos, bytes escritos, errores—
llegan de forma asíncrona desde los hilos de cada instrumento, y sellar antes
dejaba el manifiesto sin la información que más importa.

Qué pasa si un dispositivo falla a mitad de camino: los demás siguen. Pero no
en silencio: se levanta una alarma que deja el LED de ese equipo en rojo
parpadeante hasta que el operador la reconoce, queda escrito en la bitácora y
queda anotado en el manifiesto. Ese es el requisito de aislamiento del
dossier, con la trazabilidad que le faltaba.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from PyQt6.QtCore import QObject, QTimer, pyqtSignal

from .. import config
from ..devices.base import DeviceState, DeviceWorker
from . import storage
from .logbook import LOG
from .session_manager import SessionManager
from .time_sync import TIME_BASE

#: Cuánto se espera a que todos los dispositivos confirmen el armado.
ARM_TIMEOUT_S = 20.0
#: Cuánto se espera a que todos cierren sus archivos antes de sellar.
SEAL_TIMEOUT_S = 90.0

PHASE_IDLE = "inactivo"
PHASE_VERIFY = "verificacion"
PHASE_ARM = "armado"
PHASE_TRIGGER = "disparo"
PHASE_RECORDING = "grabando"
PHASE_STOPPING = "parando"
PHASE_SEALING = "sellando"

#: Fases durante las cuales la interfaz queda bloqueada: no se cambian
#: parámetros, no se excluyen dispositivos y no se retira nada del plantel.
LOCKED_PHASES = (PHASE_ARM, PHASE_TRIGGER, PHASE_RECORDING,
                 PHASE_STOPPING, PHASE_SEALING)


@dataclass
class BudgetReport:
    """Presupuesto de almacenamiento que se muestra en la pestaña 1."""

    devices: int = 0
    total_mb_s: float = 0.0
    free_bytes: int = 0
    duration_s: Optional[float] = None
    needed_bytes: float = 0.0
    largest_file_bytes: float = 0.0
    largest_file_device: str = ""
    seconds_until_full: Optional[float] = None
    ok: bool = True
    detail: List[str] = field(default_factory=list)
    problems: List[str] = field(default_factory=list)
    filesystem: str = ""

    @property
    def free_gb(self) -> float:
        return self.free_bytes / 1e9

    @property
    def usable_bytes(self) -> int:
        """Libre menos el margen de seguridad: lo que de verdad se puede usar."""
        return max(0, self.free_bytes - config.MIN_FREE_BYTES)

    @property
    def needed_gb(self) -> float:
        return self.needed_bytes / 1e9


class GlobalRecorder(QObject):
    """Coordina el armado, el disparo y la vigilancia de la sesión."""

    phase_changed = pyqtSignal(str, str)       # (fase, detalle)
    log = pyqtSignal(str)
    session_started = pyqtSignal(dict)
    session_finished = pyqtSignal(dict)
    progress = pyqtSignal(float, object)       # (transcurrido_s, total_s|None)
    failed = pyqtSignal(str)
    #: True mientras la interfaz debe estar congelada.
    lock_changed = pyqtSignal(bool)
    #: Un dispositivo se cayó grabando: (device_id, mensaje).
    device_alarm = pyqtSignal(str, str)

    def __init__(self, session_manager: SessionManager,
                 parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self.sessions = session_manager
        self._workers: Dict[str, DeviceWorker] = {}
        self._active: List[DeviceWorker] = []
        self._phase = PHASE_IDLE
        self._locked = False
        self._t0_ns: Optional[int] = None
        self._started_at: Optional[float] = None
        self._duration_s: Optional[float] = None
        self._chunk_duration_s: Optional[float] = None
        self._stop_reason = ""
        self._seal_deadline: Optional[float] = None
        self._last_free_check = 0.0
        self._alarms: Dict[str, str] = {}

        self._arm_deadline: Optional[float] = None
        self._arm_timer = QTimer(self)
        self._arm_timer.setInterval(100)
        self._arm_timer.timeout.connect(self._check_arming)

        self._seal_timer = QTimer(self)
        self._seal_timer.setInterval(150)
        self._seal_timer.timeout.connect(self._check_sealing)

        self._tick = QTimer(self)
        self._tick.setInterval(250)
        self._tick.timeout.connect(self._on_tick)

    # ------------------------------------------------------------------
    # Registro de dispositivos
    # ------------------------------------------------------------------
    def register(self, worker: DeviceWorker) -> None:
        self._workers[worker.device_id] = worker
        worker.recording_alarm.connect(self._on_device_alarm)

    def unregister(self, device_id: str) -> None:
        worker = self._workers.pop(device_id, None)
        if worker is not None:
            try:
                worker.recording_alarm.disconnect(self._on_device_alarm)
            except (TypeError, RuntimeError):
                pass

    def workers(self) -> List[DeviceWorker]:
        return list(self._workers.values())

    def by_family(self, family: str) -> List[DeviceWorker]:
        return [w for w in self._workers.values() if w.family == family]

    @property
    def phase(self) -> str:
        return self._phase

    @property
    def recording(self) -> bool:
        return self._phase in (PHASE_TRIGGER, PHASE_RECORDING)

    @property
    def busy(self) -> bool:
        """True desde que se aprieta INICIAR hasta que el manifiesto se sella."""
        return self._phase in LOCKED_PHASES

    @property
    def locked(self) -> bool:
        return self._locked

    @property
    def t0_ns(self) -> Optional[int]:
        return self._t0_ns

    @property
    def alarms(self) -> Dict[str, str]:
        return dict(self._alarms)

    def clear_alarm(self, device_id: str) -> None:
        self._alarms.pop(device_id, None)
        worker = self._workers.get(device_id)
        if worker is not None:
            worker.clear_recording_alarm()

    def _on_device_alarm(self, message: str) -> None:
        worker = self.sender()
        device_id = getattr(worker, "device_id", "")
        if device_id:
            self._alarms[device_id] = message
        self.log.emit(f"⚠ ALARMA · {message}")
        self.device_alarm.emit(device_id, message)

    def candidates(self) -> List[DeviceWorker]:
        """Dispositivos que entrarían al próximo disparo global."""
        result = []
        for worker in self._workers.values():
            if not worker.include_in_session:
                continue
            registry_type = None
            try:
                from .device_registry import REGISTRY
                registry_type = REGISTRY.get(worker.family)
            except Exception:
                pass
            if registry_type is not None and not registry_type.recordable:
                continue
            if worker.is_armable:
                result.append(worker)
        return result

    # ------------------------------------------------------------------
    # Presupuesto de disco
    # ------------------------------------------------------------------
    def budget(self, duration_s: Optional[float] = None) -> BudgetReport:
        report = BudgetReport(duration_s=duration_s)
        candidates = self.candidates()
        report.devices = len(candidates)
        for worker in candidates:
            rate = worker.estimated_mb_s()
            report.total_mb_s += rate
            report.detail.append(f"{worker.display_name}: {rate:.1f} MB/s")
            projected = worker.projected_bytes(duration_s)
            if projected > report.largest_file_bytes:
                report.largest_file_bytes = projected
                report.largest_file_device = worker.display_name

        info = storage.inspect_target(self.sessions.base_dir)
        report.free_bytes = info.free_bytes
        report.filesystem = info.filesystem

        if report.total_mb_s > 0:
            report.seconds_until_full = report.usable_bytes / (
                report.total_mb_s * 1e6)
            if duration_s:
                report.needed_bytes = report.total_mb_s * 1e6 * duration_s
                report.ok = report.needed_bytes <= report.usable_bytes

        if info.max_file_bytes and report.largest_file_bytes > info.max_file_bytes:
            report.ok = False
            report.problems.append(
                f"{report.largest_file_device} generaría un archivo de "
                f"{report.largest_file_bytes / 1e9:.1f} GB y "
                f"{info.filesystem} no admite más de "
                f"{info.max_file_bytes / 1e9:.1f} GB.")
        return report

    # ------------------------------------------------------------------
    # Fase 1 y 2
    # ------------------------------------------------------------------
    def fire(self, session_name: str = "", duration_s: Optional[float] = None,
             auto_timestamp: bool = True,
             chunk_duration_s: Optional[float] = None) -> bool:
        """Lanza las fases 1 y 2. El disparo real ocurre al confirmar el armado."""
        if self.busy:
            self.log.emit("Ya hay una grabación global en curso.")
            return False

        # ---------------- Fase 1: verificación --------------------------
        self._set_phase(PHASE_VERIFY, "Comprobando dispositivos y destino")
        candidates = self.candidates()
        from .device_registry import REGISTRY
        unavailable = []
        for worker in self._workers.values():
            device_type = REGISTRY.get(worker.family)
            if (worker.include_in_session and
                    (device_type is None or device_type.recordable) and
                    worker not in candidates):
                unavailable.append(worker.display_name)
        if unavailable:
            self._set_phase(PHASE_IDLE, "")
            self.failed.emit("No están listos para grabar: " + ", ".join(unavailable)
                             + ". Comprueba que reciben datos o exclúyelos de la sesión.")
            return False
        if not candidates:
            self._set_phase(PHASE_IDLE, "")
            self.failed.emit("Ningún dispositivo listo: no hay nada que grabar.")
            return False

        simulated = [w for w in candidates if getattr(w, "simulated", False)]
        if simulated and not config.simulations_allowed():
            self._set_phase(PHASE_IDLE, "")
            self.failed.emit(
                "Hay dispositivos simulados en la lista y el programa no está "
                "en modo --demo. No se graba nada.")
            return False
        if simulated:
            self.log.emit(
                f"MODO DEMOSTRACIÓN · {len(simulated)} dispositivo(s) simulado(s). "
                f"Los datos de esta sesión NO son mediciones.")

        report = self.budget(duration_s)
        self.log.emit(
            f"Fase 1 · {len(candidates)} dispositivo(s) listo(s), "
            f"{report.total_mb_s:,.1f} MB/s en total, "
            f"{report.free_gb:,.1f} GB libres en destino "
            f"({report.filesystem or 'sistema de archivos desconocido'}).")
        if report.seconds_until_full is not None:
            self.log.emit(
                f"Fase 1 · Con ese caudal el destino llega al margen de "
                f"{config.MIN_FREE_BYTES / 1e9:.0f} GB en "
                f"{report.seconds_until_full / 60:.1f} min.")

        verdict = storage.validate_destination(
            self.sessions.base_dir,
            needed_bytes=report.needed_bytes or None,
            largest_file_bytes=report.largest_file_bytes or None,
            create=True)
        for message in verdict.messages:
            self.log.emit(f"Fase 1 · {message}")
        for problem in report.problems:
            self.log.emit(f"Fase 1 · ✖ {problem}")
        if not verdict.ok or not report.ok:
            self._set_phase(PHASE_IDLE, "")
            reason = "; ".join(verdict.errors + report.problems) or \
                (f"El destino no alcanza: hacen falta {report.needed_gb:.1f} GB "
                 f"y hay {report.usable_bytes / 1e9:.1f} GB utilizables.")
            self.failed.emit(reason)
            return False

        # ---------------- Fase 2: armado --------------------------------
        self._duration_s = duration_s
        self._chunk_duration_s = chunk_duration_s
        self._alarms.clear()
        for worker in candidates:
            worker.clear_recording_alarm()

        modo = ("un archivo por toma" if not chunk_duration_s
                else f"bloques de {chunk_duration_s / 60:.0f} min")
        try:
            session_dir = self.sessions.create_session(
                name=session_name,
                auto_timestamp=auto_timestamp,
                devices=[w.describe() for w in candidates],
                duration_s=duration_s,
                tree=[(w.family, w.label) for w in candidates],
                extra={
                    "modo_escritura": modo,
                    "margen_disco_bytes": config.MIN_FREE_BYTES,
                    "destino_volumen": (verdict.info.as_dict()
                                        if verdict.info else {}),
                    "presupuesto": {
                        "mb_s_total": round(report.total_mb_s, 2),
                        "necesario_bytes": report.needed_bytes,
                        "archivo_mayor_bytes": report.largest_file_bytes,
                        "archivo_mayor_dispositivo": report.largest_file_device,
                    },
                })
        except OSError as exc:
            self._set_phase(PHASE_IDLE, "")
            self.failed.emit(f"No se pudo crear la carpeta de sesión: {exc}")
            return False

        self.log.emit(f"Fase 2 · Sesión creada: {session_dir}")
        self.log.emit(f"Fase 2 · Modo de escritura: {modo}.")
        self._set_phase(PHASE_ARM, "Armando dispositivos")

        self._active = []
        for worker in candidates:
            target = self.sessions.device_dir(worker.family, worker.label)
            accepted = worker.arm_recording(
                directory=target,
                prefix=worker.label,
                duration_s=duration_s,
                chunk_duration_s=chunk_duration_s)
            if accepted:
                self._active.append(worker)
                self.log.emit(f"Fase 2 · {worker.display_name} → "
                              f"{target.parent.name}/{target.name}/")
            else:
                self.log.emit(f"Fase 2 · {worker.display_name} rechazó el "
                              f"armado: queda fuera.")

        if not self._active:
            self.sessions.close_session({"resultado": "armado_fallido"})
            self._set_phase(PHASE_IDLE, "")
            self.failed.emit("Ningún dispositivo aceptó el armado.")
            return False

        self._arm_deadline = time.monotonic() + ARM_TIMEOUT_S
        self._arm_timer.start()
        return True

    # ------------------------------------------------------------------
    # Fase 3
    # ------------------------------------------------------------------
    def _check_arming(self) -> None:
        pending = [w for w in self._active if w.state is not DeviceState.ARMED]
        broken = [w for w in pending if w.state in (DeviceState.ERROR_FATAL,
                                                    DeviceState.DISCONNECTED)]
        for worker in broken:
            self.log.emit(f"Fase 2 · {worker.display_name} cayó durante el armado.")
            self._active.remove(worker)
        pending = [w for w in self._active if w.state is not DeviceState.ARMED]

        if pending and time.monotonic() < (self._arm_deadline or 0):
            self._set_phase(PHASE_ARM,
                            f"Esperando {len(pending)} dispositivo(s)")
            return

        self._arm_timer.stop()

        if pending:
            names = ", ".join(w.display_name for w in pending)
            self.log.emit(f"Fase 2 · Tiempo agotado esperando: {names}")
            for worker in self._active:
                worker.cancel_arm()
            self.sessions.close_session({"resultado": "armado_incompleto",
                                         "pendientes": names})
            self._active = []
            self._set_phase(PHASE_IDLE, "")
            self.failed.emit(f"No armaron a tiempo: {names}. No se grabó nada.")
            return

        if not self._active:
            self.sessions.close_session({"resultado": "sin_dispositivos"})
            self._set_phase(PHASE_IDLE, "")
            self.failed.emit("Todos los dispositivos cayeron durante el armado.")
            return

        self._trigger()

    def _trigger(self) -> None:
        """Fase 3: el instante cero. Cuanto menos haya aquí, mejor."""
        self._set_phase(PHASE_TRIGGER, "Disparo síncrono")
        t0 = TIME_BASE.mark_zero()
        self._t0_ns = t0
        for worker in self._active:
            worker.start_recording(t0)

        self._started_at = time.monotonic()
        self._last_free_check = self._started_at
        utc = TIME_BASE.to_utc(t0)
        stamp = "" if utc is None else \
            f" · UTC {time.strftime('%H:%M:%S', time.gmtime(utc))}"
        self.log.emit(f"*** DISPARO SÍNCRONO · T0 monotónico = {t0} ns{stamp} ***")
        if utc is None:
            self.log.emit(
                "Aviso: sin ancla UTC del GPS. Los tiempos quedan relativos a "
                "T0 y anclados al reloj del host (ver el manifiesto).")

        self.sessions.update_manifest(
            t0_host_ns=t0,
            reloj=TIME_BASE.snapshot(),
            dispositivos_grabando=[w.describe() for w in self._active])

        self._set_phase(PHASE_RECORDING, "Grabación en curso")
        self.session_started.emit({
            "session_dir": str(self.sessions.session_dir),
            "t0_ns": t0,
            "devices": [w.display_name for w in self._active],
            "duration_s": self._duration_s,
        })
        self._tick.start()

    # ------------------------------------------------------------------
    # Fase 4 · vigilancia
    # ------------------------------------------------------------------
    def _on_tick(self) -> None:
        if self._started_at is None:
            return
        elapsed = time.monotonic() - self._started_at
        self.progress.emit(elapsed, self._duration_s)

        now = time.monotonic()
        if now - self._last_free_check >= config.FREE_SPACE_CHECK_PERIOD_S:
            self._last_free_check = now
            if self._check_free_space():
                return

        if self._duration_s and elapsed >= self._duration_s:
            self.log.emit("Duración programada cumplida.")
            self.stop_all(reason="duracion_cumplida")

    def _check_free_space(self) -> bool:
        """True si se detuvo la sesión por falta de espacio."""
        free = self.sessions.disk_free_bytes()
        if free > config.MIN_FREE_BYTES:
            return False
        LOG.critical(
            f"Espacio libre en destino: {free / 1e9:.2f} GB, por debajo del "
            f"margen de {config.MIN_FREE_BYTES / 1e9:.0f} GB. "
            f"Deteniendo la sesión para cerrar los archivos limpiamente.")
        self.log.emit(
            f"⚠ DISCO CASI LLENO ({free / 1e9:.2f} GB). Se detiene la toma "
            f"ahora, con margen para cerrar todo correctamente.")
        self.stop_all(reason="margen_de_disco_alcanzado")
        return True

    # ------------------------------------------------------------------
    # Fase 5 · parada y sellado
    # ------------------------------------------------------------------
    def stop_all(self, reason: str = "manual") -> Dict[str, Any]:
        """Pide la parada y espera a que cada archivo quede cerrado."""
        if self._phase not in (PHASE_ARM, PHASE_TRIGGER, PHASE_RECORDING):
            return {}
        self._tick.stop()
        self._arm_timer.stop()
        self._stop_reason = reason
        self._set_phase(PHASE_STOPPING, "Cerrando archivos")
        LOG.info(f"Parada de la sesión solicitada ({reason}).")

        for worker in self._active:
            try:
                worker.stop_recording()
            except Exception as exc:
                self.log.emit(f"Fase 5 · {worker.display_name}: error al parar "
                              f"({exc})")

        self._seal_deadline = time.monotonic() + SEAL_TIMEOUT_S
        self._set_phase(PHASE_SEALING, "Esperando el cierre de los archivos")
        self._seal_timer.start()
        return {}

    def _check_sealing(self) -> None:
        pending = [w for w in self._active if w.is_recording]
        overdue = time.monotonic() > (self._seal_deadline or 0)
        if pending and not overdue:
            self._set_phase(PHASE_SEALING,
                            f"Cerrando {len(pending)} archivo(s)")
            return

        self._seal_timer.stop()
        if pending:
            names = ", ".join(w.display_name for w in pending)
            LOG.error(f"Tiempo agotado esperando el cierre de: {names}. "
                      f"El manifiesto se sella igual, con la advertencia.")
            self.log.emit(f"⚠ No confirmaron el cierre: {names}")
        self._seal(pending)

    def _seal(self, pending: List[DeviceWorker]) -> None:
        elapsed = 0.0 if self._started_at is None \
            else time.monotonic() - self._started_at

        summary: Dict[str, Any] = {
            "motivo": self._stop_reason,
            "duracion_real_s": round(elapsed, 3),
            "t0_host_ns": self._t0_ns,
            "modo_escritura": ("archivo_unico" if not self._chunk_duration_s
                               else f"bloques_{int(self._chunk_duration_s)}s"),
            "alarmas": dict(self._alarms),
            "sin_confirmar_cierre": [w.label for w in pending],
            "dispositivos": {},
        }
        for worker in self._active:
            record = dict(worker.last_stats)
            record["resumen_grabacion"] = worker.last_recording_summary
            summary["dispositivos"][worker.label] = record
            # Ahora sí: el resumen por dispositivo entra al manifiesto antes
            # de sellarlo, que era justo lo que faltaba.
            self.sessions.add_device_summary(worker.device_id, record)

        session_dir = self.sessions.close_session(summary)
        summary["session_dir"] = str(session_dir) if session_dir else None

        self._active = []
        self._started_at = None
        self._t0_ns = None
        self._stop_reason = ""
        TIME_BASE.clear_zero()
        self._set_phase(PHASE_IDLE, "")
        self.log.emit(f"Sesión cerrada ({summary['motivo']}). "
                      f"Duración: {elapsed:.1f} s")
        if self._alarms:
            self.log.emit(
                f"⚠ La sesión terminó con {len(self._alarms)} alarma(s): "
                f"revisa el registro antes de dar los datos por buenos.")
        self.session_finished.emit(summary)

    # ------------------------------------------------------------------
    def _set_phase(self, phase: str, detail: str) -> None:
        self._phase = phase
        locked = phase in LOCKED_PHASES
        if locked != self._locked:
            self._locked = locked
            self.lock_changed.emit(locked)
        self.phase_changed.emit(phase, detail)
