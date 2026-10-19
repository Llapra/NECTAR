"""Escritura continua a disco: anillo permanente + hilo escritor.

Por qué esto y no el buffer de RAM de ``pastam_cam``
---------------------------------------------------
``pastam_cam`` grababa primero a RAM y volcaba a disco al terminar. Es
correcto para ráfagas cortas, pero pone un techo duro a la duración: con
1,67 GB/s de la Emergent, 128 GB de RAM son ~75 segundos de grabación y hay
que esperar el volcado antes de la siguiente toma.

En NECTAR el equipo de campaña tiene SSD NVMe rápido, así que se escribe en
continuo y la RAM queda solo como amortiguador, manteniendo la propiedad que
importa: **en el camino crítico de adquisición no se asigna memoria ni se
toca el sistema de archivos.**

::

    hilo de adquisicion                    hilo escritor
    -------------------                    -------------
    submit(raw) ──► slot libre del anillo  ──► write() de N slots seguidos
                    (un solo memcpy)            (una syscall por lote)

Qué cambió respecto de la primera versión
-----------------------------------------
**El anillo es permanente.** Antes, cada bloque creaba un ``DiskStreamWriter``
nuevo, lo que significaba reservar y confirmar ~900 MB de memoria *durante la
grabación*, con el hilo de captura esperando. A 662 fps eso se traducía en
frames perdidos en cada corte. Ahora el anillo se reserva una vez al armar y
vive hasta que se desconecta la cámara; cambiar de archivo es cerrar un
descriptor y abrir otro, y ocurre **dentro del hilo escritor**, entre dos
lotes, sin que la captura se entere.

**Un archivo por toma, por omisión.** Sin cortes no hay costuras. Lo que
sostiene esa decisión son las tres cosas que siguen.

**Volcado incremental.** Después de cada lote se le pide al núcleo que empiece
a bajar ese tramo al disco (``sync_file_range``) y se suelta del caché lo que
ya bajó (``fadvise DONTNEED``). Sin esto, una toma de cientos de GB infla el
caché de páginas hasta que el núcleo lo vacía de golpe, y ese vaciado cae
encima de la captura. Con esto, lo que un corte de energía se llevaría son
segundos, no minutos.

**Puntos de control.** Cada pocos segundos se sella un ``_checkpoint.json``
pequeño —offset en bytes, frames escritos, reloj del host, desfase respecto a
T0— y **ese** archivito sí se fuerza a disco con ``fsync``. Cuesta nada
porque son cientos de bytes. Si la toma muere sin cierre limpio, el punto de
control dice exactamente hasta dónde el archivo es válido, en vez de dejar
que alguien lo adivine un año después.

**Reserva anticipada.** El espacio se pide por tramos con ``fallocate``, lo
que mantiene el archivo contiguo y hace que la falta de espacio aparezca
temprano. Al cerrar se recorta al tamaño realmente escrito.

**El fallo escala.** Si el disco se llena o se desconecta, el escritor no se
limita a contar errores en silencio: marca el flujo como caído y llama al
``on_failure`` del dispositivo, que lo lleva a ERROR y detiene la sesión. Un
escritor muerto que sigue aceptando frames es la peor forma de perder una
noche de medición.
"""

from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import numpy as np

from .. import config
from . import storage


@dataclass
class StreamStats:
    """Contadores del escritor. Se leen desde la GUI, se escriben aquí."""

    submitted: int = 0          # frames aceptados en el anillo
    written: int = 0            # frames ya en disco
    dropped: int = 0            # frames rechazados por anillo lleno
    bytes_written: int = 0
    write_calls: int = 0
    max_occupancy: int = 0      # slots ocupados en el peor momento
    last_write_mb_s: float = 0.0
    worst_write_ms: float = 0.0
    checkpoints: int = 0
    files: int = 0
    errors: int = 0
    last_error: str = ""

    def as_dict(self) -> Dict[str, Any]:
        return {
            "frames_aceptados": self.submitted,
            "frames_escritos": self.written,
            "frames_perdidos_por_disco": self.dropped,
            "bytes_escritos": self.bytes_written,
            "llamadas_write": self.write_calls,
            "ocupacion_maxima_anillo": self.max_occupancy,
            "caudal_ultimo_mb_s": round(self.last_write_mb_s, 2),
            "peor_write_ms": round(self.worst_write_ms, 2),
            "puntos_de_control": self.checkpoints,
            "archivos": self.files,
            "errores": self.errors,
            "ultimo_error": self.last_error,
        }


class DiskStreamWriter:
    """Anillo permanente de frames con volcado continuo a un archivo.

    Un objeto por cámara, no por archivo: el anillo sobrevive a los cambios de
    archivo. Se usa así::

        writer = DiskStreamWriter(frame_bytes=..., on_failure=...)
        writer.prefault()               # una vez, al armar
        writer.open(path, metadata)     # abre el archivo y arranca el hilo
        writer.submit(raw, ...)         # por cada frame
        writer.request_rotate(path2, m) # opcional, modo por bloques
        summary = writer.close()        # cierra todo
    """

    def __init__(self, frame_bytes: int,
                 slots: int = 256, batch: int = 32,
                 write_index: bool = True,
                 preallocate_step: int = config.FALLOCATE_STEP_BYTES,
                 checkpoint_period_s: float = config.CHECKPOINT_PERIOD_S,
                 on_failure: Optional[Callable[[str], None]] = None) -> None:
        self.frame_bytes = int(frame_bytes)
        self.slots = max(8, int(slots))
        self.batch = max(1, min(int(batch), self.slots // 2))
        self.write_index = write_index
        self.preallocate_step = int(preallocate_step)
        self.checkpoint_period_s = float(checkpoint_period_s)
        self.on_failure = on_failure

        # --- anillo (se reserva una sola vez) ---------------------------
        self._ring = np.empty((self.slots, self.frame_bytes), dtype=np.uint8)
        self._frame_ids = np.zeros(self.slots, dtype=np.uint64)
        self._device_ts = np.zeros(self.slots, dtype=np.uint64)
        self._host_ns = np.zeros(self.slots, dtype=np.uint64)
        self._sizes = np.zeros(self.slots, dtype=np.uint32)

        self._head = 0          # siguiente slot a escribir (productor)
        self._tail = 0          # siguiente slot a volcar (consumidor)
        self._lock = threading.Lock()
        self._data_ready = threading.Condition(self._lock)

        self._running = False
        self._closing = False
        self._failed = False
        self._thread: Optional[threading.Thread] = None

        # --- archivo vigente --------------------------------------------
        self.path: Optional[Path] = None
        self.metadata: Dict[str, Any] = {}
        self._handle = None
        self._index_handle = None
        self._file_bytes = 0        # bytes escritos en el archivo vigente
        self._allocated = 0         # bytes reservados por fallocate
        self._synced = 0            # bytes ya empujados al disco
        self._file_frames = 0
        self._rotate_request: Optional[tuple] = None
        self._closed_files: List[Dict[str, Any]] = []

        self.stats = StreamStats()
        self.first_host_ns: Optional[int] = None
        self.last_host_ns: Optional[int] = None
        self.started_unix: Optional[float] = None
        self.t0_ns: Optional[int] = None

    # ------------------------------------------------------------------
    @property
    def ring_bytes(self) -> int:
        return int(self._ring.nbytes)

    @property
    def failed(self) -> bool:
        return self._failed

    @property
    def running(self) -> bool:
        return self._running

    @property
    def file_bytes(self) -> int:
        return self._file_bytes

    def prefault(self) -> None:
        """Confirma cada página del anillo ahora, no durante la ráfaga.

        Sin esto, los fallos de página se pagarían justo cuando no se puede
        perder tiempo. Tocar un byte cada 4096 basta para que el sistema
        operativo respalde la página.
        """
        flat = self._ring.reshape(-1)
        flat[::4096] = 0
        if flat.size:
            flat[-1] = 0

    # ------------------------------------------------------------------
    # Ciclo de vida
    # ------------------------------------------------------------------
    def open(self, path: str | Path, metadata: Optional[Dict[str, Any]] = None,
             t0_ns: Optional[int] = None) -> None:
        """Abre el primer archivo y arranca el hilo escritor."""
        if self._running:
            return
        self.path = Path(path)
        self.metadata = dict(metadata or {})
        self.t0_ns = t0_ns
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # buffering=0: el sistema operativo ya amortigua; una segunda copia en
        # el buffer de Python solo agrega latencia y memoria.
        self._handle = open(self.path, "wb", buffering=0)
        self._file_bytes = 0
        self._allocated = 0
        self._synced = 0
        self._file_frames = 0
        self._preallocate_locked()
        self._open_index()

        self._running = True
        self._closing = False
        self._failed = False
        self.started_unix = time.time()
        self.stats.files += 1
        self._thread = threading.Thread(target=self._writer_loop,
                                        name=f"disk-{self.path.stem}",
                                        daemon=True)
        self._thread.start()

    def _open_index(self) -> None:
        self._index_handle = None
        if not self.write_index or self.path is None:
            return
        try:
            index_path = self.path.with_name(self.path.stem + "_frames.csv")
            self._index_handle = open(index_path, "w", encoding="utf-8",
                                      newline="")
            self._index_handle.write(
                "indice,frame_id,timestamp_dispositivo_ns,host_ns,"
                "host_rel_ms,offset_bytes\n")
        except OSError:
            self._index_handle = None

    def submit(self, raw: np.ndarray, frame_id: int = 0, device_ts: int = 0,
               host_ns: int = 0) -> bool:
        """Copia un frame al anillo. False si el disco no alcanza.

        Es lo único que se ejecuta en el hilo de adquisición: un memcpy y
        cuatro asignaciones de enteros. Si el flujo cayó devuelve False
        siempre, para que el dispositivo cuente la pérdida en vez de creer que
        está grabando.
        """
        with self._lock:
            if not self._running or self._failed:
                return False
            occupancy = self._head - self._tail
            if occupancy >= self.slots:
                self.stats.dropped += 1
                return False
            index = self._head % self.slots
            if occupancy > self.stats.max_occupancy:
                self.stats.max_occupancy = occupancy

        size = min(int(raw.size), self.frame_bytes)
        destination = self._ring[index]
        destination[:size] = raw[:size]

        with self._lock:
            self._frame_ids[index] = frame_id
            self._device_ts[index] = device_ts
            self._host_ns[index] = host_ns
            self._sizes[index] = size
            self._head += 1
            self.stats.submitted += 1
            if self.first_host_ns is None:
                self.first_host_ns = host_ns
            self.last_host_ns = host_ns
            self._data_ready.notify()
        return True

    def request_rotate(self, path: str | Path,
                       metadata: Optional[Dict[str, Any]] = None) -> None:
        """Pide cambiar de archivo (modo por bloques, apagado por omisión).

        El cambio lo hace el hilo escritor entre dos lotes: el anillo no se
        toca, no se reserva memoria y la captura no se detiene. Solo hay un
        instante sin escritura, del orden del tiempo de cerrar y abrir dos
        descriptores.
        """
        with self._lock:
            self._rotate_request = (Path(path), dict(metadata or {}))
            self._data_ready.notify()

    def close(self, reason: str = "", drain: bool = True,
              timeout: float = 60.0) -> Dict[str, Any]:
        """Cierra el archivo y devuelve el resumen.

        Con ``drain=True`` espera a que el escritor vacíe el anillo, que es lo
        que se quiere al parar una grabación: los frames ya aceptados no se
        tiran.
        """
        with self._lock:
            if not self._running:
                return self._summary(reason)
            self._closing = True
            self._data_ready.notify_all()

        if self._thread is not None:
            self._thread.join(timeout if drain else 1.0)
        self._thread = None

        with self._lock:
            self._running = False

        self._close_current_file(reason=reason)
        summary = self._summary(reason)
        summary["archivos_cerrados"] = list(self._closed_files)
        return summary

    # ------------------------------------------------------------------
    # Hilo escritor
    # ------------------------------------------------------------------
    def _writer_loop(self) -> None:
        ring = self._ring
        next_checkpoint = time.perf_counter() + self.checkpoint_period_s
        while True:
            pending_rotate = None
            with self._lock:
                if (self._head == self._tail and not self._closing
                        and self._rotate_request is None):
                    # Espera con tope para que los puntos de control sigan
                    # saliendo aunque el instrumento no entregue nada.
                    self._data_ready.wait(0.2)
                available = self._head - self._tail
                if available <= 0:
                    # El anillo está vacío: recién ahora se puede cambiar de
                    # archivo o cerrar, con la certeza de que no queda ningún
                    # frame aceptado sin escribir.
                    if self._rotate_request is not None:
                        pending_rotate = self._rotate_request
                        self._rotate_request = None
                    elif self._closing:
                        return
                    start = run = 0
                else:
                    start = self._tail % self.slots
                    # Solo el tramo contiguo hasta el final del anillo: así el
                    # write() sale de memoria continua sin copiar nada.
                    run = min(available, self.batch, self.slots - start)

            if run > 0 and not self._flush_run(ring, start, run):
                return

            if pending_rotate is not None:
                self._rotate_now(*pending_rotate)

            now = time.perf_counter()
            if now >= next_checkpoint:
                self._checkpoint()
                next_checkpoint = now + self.checkpoint_period_s

    def _flush_run(self, ring: np.ndarray, start: int, run: int) -> bool:
        """Escribe un tramo contiguo del anillo. False si el flujo cayó."""
        block = ring[start:start + run]
        nbytes = int(block.nbytes)
        indices = list(range(self._tail, self._tail + run))
        offset = self._file_bytes

        self._ensure_allocated(nbytes)

        tick = time.perf_counter()
        try:
            # reshape(-1) sobre un tramo contiguo es una vista, no una copia:
            # el write() sale directo de la memoria del anillo.
            self._handle.write(block.reshape(-1).data)
        except Exception as exc:        # disco lleno, desconectado, E/S
            self._raise_failure(f"escritura: {type(exc).__name__}: {exc}")
            return False

        elapsed = time.perf_counter() - tick
        self._file_bytes += nbytes
        self._file_frames += run

        # Empujar al disco lo recién escrito y soltar del caché lo que ya
        # bajó. Ninguna de las dos bloquea.
        try:
            fd = self._handle.fileno()
            storage.push_to_disk(fd, offset, nbytes)
            behind = self._file_bytes - 64 * 1024 * 1024
            if behind > self._synced:
                storage.drop_cache(fd, self._synced, behind - self._synced)
                self._synced = behind
        except (OSError, ValueError):
            pass

        self._write_index_rows(indices, offset)

        with self._lock:
            self.stats.written += run
            self.stats.bytes_written += nbytes
            self.stats.write_calls += 1
            if elapsed > 0:
                self.stats.last_write_mb_s = nbytes / elapsed / 1e6
            if elapsed * 1000 > self.stats.worst_write_ms:
                self.stats.worst_write_ms = elapsed * 1000
            self._tail += run
        return True

    def _ensure_allocated(self, incoming: int) -> None:
        """Extiende la reserva del archivo si el próximo lote no cabe."""
        if self.preallocate_step <= 0 or self._handle is None:
            return
        if self._file_bytes + incoming <= self._allocated:
            return
        try:
            fd = self._handle.fileno()
            if storage.allocate(fd, self._allocated, self.preallocate_step):
                self._allocated += self.preallocate_step
            else:
                self.preallocate_step = 0      # no lo admite: no insistir
        except OSError as exc:
            # ENOSPC acá es exactamente lo que se quiere detectar temprano.
            self._raise_failure(f"reserva de espacio: {exc}")

    def _preallocate_locked(self) -> None:
        if self.preallocate_step <= 0 or self._handle is None:
            return
        try:
            if storage.allocate(self._handle.fileno(), 0, self.preallocate_step):
                self._allocated = self.preallocate_step
            else:
                self.preallocate_step = 0
        except OSError as exc:
            self._raise_failure(f"reserva inicial de espacio: {exc}")

    def _write_index_rows(self, indices, offset: int) -> None:
        if self._index_handle is None:
            return
        origin = self.first_host_ns or 0
        rows = []
        position = offset
        for absolute in indices:
            slot = absolute % self.slots
            host = int(self._host_ns[slot])
            rel_ms = (host - origin) / 1e6 if origin else 0.0
            rows.append(f"{absolute},{int(self._frame_ids[slot])},"
                        f"{int(self._device_ts[slot])},{host},"
                        f"{rel_ms:.6f},{position}\n")
            position += int(self._sizes[slot]) or self.frame_bytes
        try:
            self._index_handle.write("".join(rows))
        except (OSError, ValueError):
            pass

    # ------------------------------------------------------------------
    def _checkpoint(self) -> None:
        """Sella en disco hasta dónde el archivo es válido.

        Es lo que convierte "se cortó la luz" en "perdí los últimos segundos"
        en vez de "no sé qué tengo". Cuesta un ``fsync`` sobre unos cientos de
        bytes, no sobre el flujo.
        """
        if self.path is None:
            return
        host_ns = int(self.last_host_ns or 0)
        document = {
            "archivo": self.path.name,
            "bytes_validos": self._file_bytes,
            "frames_escritos": self._file_frames,
            "frames_totales_sesion": self.stats.written,
            "host_ns": host_ns,
            "t0_host_ns": self.t0_ns,
            "desfase_t0_s": ((host_ns - self.t0_ns) / 1e9
                             if (self.t0_ns and host_ns) else None),
            "hora_local": time.strftime("%Y-%m-%d %H:%M:%S"),
            "frame_bytes": self.frame_bytes,
            "errores": self.stats.errors,
        }
        try:
            if self._index_handle is not None:
                self._index_handle.flush()
                os.fsync(self._index_handle.fileno())
        except (OSError, ValueError):
            pass
        try:
            path = self.path.with_name(self.path.stem + "_checkpoint.json")
            temporary = path.with_suffix(".json.tmp")
            with open(temporary, "w", encoding="utf-8") as handle:
                json.dump(document, handle, indent=2, ensure_ascii=False)
                handle.flush()
                os.fsync(handle.fileno())
            temporary.replace(path)
            self.stats.checkpoints += 1
        except OSError:
            pass

    # ------------------------------------------------------------------
    def _rotate_now(self, path: Path, metadata: Dict[str, Any]) -> None:
        """Cierra el archivo vigente y abre el siguiente. En el hilo escritor."""
        self._close_current_file(reason="rollover")
        self.path = Path(path)
        if metadata:
            self.metadata = dict(metadata)
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._handle = open(self.path, "wb", buffering=0)
        except OSError as exc:
            self._raise_failure(f"no se pudo abrir el bloque siguiente: {exc}")
            return
        self._file_bytes = 0
        self._allocated = 0
        self._synced = 0
        self._file_frames = 0
        self._preallocate_locked()
        self._open_index()
        self.stats.files += 1

    def _close_current_file(self, reason: str = "") -> None:
        """Cierra descriptores, recorta la reserva sobrante y sella metadatos."""
        if self._handle is None:
            return
        written = self._file_bytes
        frames = self._file_frames
        try:
            self._handle.flush()
        except (OSError, ValueError):
            pass
        try:
            fd = self._handle.fileno()
            if self._allocated > written:
                storage.truncate_to(fd, written)
            os.fsync(fd)
        except (OSError, ValueError):
            pass
        try:
            self._handle.close()
        except (OSError, ValueError):
            pass
        self._handle = None

        if self._index_handle is not None:
            try:
                self._index_handle.flush()
                os.fsync(self._index_handle.fileno())
                self._index_handle.close()
            except (OSError, ValueError):
                pass
            self._index_handle = None

        self._checkpoint()
        record = {
            "archivo": self.path.name if self.path else "",
            "bytes": written,
            "frames": frames,
            "motivo": reason,
            "cerrado": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        self._closed_files.append(record)
        self._write_meta(record, reason)

    def _raise_failure(self, message: str) -> None:
        """Marca el flujo como caído y avisa hacia arriba.

        Se llama desde el hilo escritor. El ``on_failure`` del dispositivo
        debe limitarse a levantar una bandera o emitir una señal de Qt.
        """
        with self._lock:
            self._failed = True
            self._closing = True
            self.stats.errors += 1
            self.stats.last_error = message
        if self.on_failure is not None:
            try:
                self.on_failure(message)
            except Exception:
                pass

    # ------------------------------------------------------------------
    def _summary(self, reason: str = "") -> Dict[str, Any]:
        elapsed = 0.0
        if self.first_host_ns is not None and self.last_host_ns is not None:
            elapsed = (self.last_host_ns - self.first_host_ns) / 1e9
        fps = (self.stats.written - 1) / elapsed if elapsed > 0 else 0.0
        summary = self.stats.as_dict()
        summary.update({
            "archivo": self.path.name if self.path else "",
            "motivo": reason,
            "duracion_s": round(elapsed, 6),
            "fps_medido": round(fps, 3),
            "mb_s_medio": round(self.stats.bytes_written / elapsed / 1e6, 2)
            if elapsed > 0 else 0.0,
            "anillo_slots": self.slots,
            "anillo_mb": round(self.ring_bytes / 1e6, 1),
            "flujo_caido": self._failed,
        })
        return summary

    def _write_meta(self, record: Dict[str, Any], reason: str) -> None:
        if self.path is None:
            return
        document = dict(self.metadata)
        document.update({
            "archivo_principal": self.path.name,
            "frame_bytes": self.frame_bytes,
            "modo": "archivo_unico" if self.stats.files <= 1 else "bloque",
            "creado": time.strftime("%Y-%m-%d %H:%M:%S"),
            "cierre": record,
            "resumen": self._summary(reason),
        })
        try:
            meta_path = self.path.with_name(self.path.stem + "_meta.json")
            with open(meta_path, "w", encoding="utf-8") as handle:
                json.dump(document, handle, indent=2, ensure_ascii=False)
                handle.flush()
                os.fsync(handle.fileno())
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Escritor de flujos de bytes (fibras, IMU, anemómetro)
# ---------------------------------------------------------------------------
@dataclass
class ByteStreamStats:
    bytes_written: int = 0
    blocks: int = 0
    dropped_blocks: int = 0
    checkpoints: int = 0
    errors: int = 0
    last_error: str = ""


class ByteStreamWriter:
    """Escritor asíncrono para instrumentos de caudal bajo o medio.

    Los sensores serie y de socket no entregan bloques de tamaño fijo, así que
    el amortiguador es una cola de ``bytes`` con tope: si el disco se atasca se
    descartan bloques y se cuenta, en vez de dejar que la memoria crezca hasta
    que el sistema operativo mate el proceso.

    Comparte con ``DiskStreamWriter`` las tres garantías que importan: puntos
    de control periódicos con ``fsync``, vaciado incremental y escalado del
    fallo hacia el dispositivo.
    """

    def __init__(self, path: str | Path, max_queue_bytes: int = 64 * 1024 * 1024,
                 header: Optional[bytes] = None,
                 checkpoint_period_s: float = config.CHECKPOINT_PERIOD_S,
                 metadata: Optional[Dict[str, Any]] = None,
                 t0_ns: Optional[int] = None,
                 on_failure: Optional[Callable[[str], None]] = None) -> None:
        self.path = Path(path)
        self.max_queue_bytes = int(max_queue_bytes)
        self.header = header
        self.checkpoint_period_s = float(checkpoint_period_s)
        self.metadata = dict(metadata or {})
        self.t0_ns = t0_ns
        self.on_failure = on_failure

        self._queue: list = []
        self._queued_bytes = 0
        self._lock = threading.Lock()
        self._ready = threading.Condition(self._lock)
        self._running = False
        self._closing = False
        self._failed = False
        self._thread: Optional[threading.Thread] = None
        self._handle = None
        self.stats = ByteStreamStats()
        self.started_unix: Optional[float] = None

    @property
    def failed(self) -> bool:
        return self._failed

    def start(self) -> None:
        if self._running:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = open(self.path, "wb", buffering=1024 * 1024)
        if self.header:
            self._handle.write(self.header)
            self.stats.bytes_written += len(self.header)
        self._running = True
        self._closing = False
        self._failed = False
        self.started_unix = time.time()
        self._thread = threading.Thread(target=self._loop,
                                        name=f"bytes-{self.path.stem}",
                                        daemon=True)
        self._thread.start()

    def submit(self, block: bytes) -> bool:
        if not block:
            return True
        with self._lock:
            if not self._running or self._failed:
                return False
            if self._queued_bytes + len(block) > self.max_queue_bytes:
                self.stats.dropped_blocks += 1
                return False
            self._queue.append(block)
            self._queued_bytes += len(block)
            self._ready.notify()
        return True

    def _loop(self) -> None:
        next_checkpoint = time.perf_counter() + self.checkpoint_period_s
        while True:
            with self._lock:
                if not self._queue and not self._closing:
                    self._ready.wait(0.2)
                if not self._queue and self._closing:
                    return
                pending, self._queue = self._queue, []
                self._queued_bytes = 0

            if pending:
                payload = b"".join(pending)
                try:
                    self._handle.write(payload)
                except Exception as exc:
                    self._raise_failure(f"escritura: {type(exc).__name__}: {exc}")
                    return
                with self._lock:
                    self.stats.bytes_written += len(payload)
                    self.stats.blocks += len(pending)

            now = time.perf_counter()
            if now >= next_checkpoint:
                self._checkpoint()
                next_checkpoint = now + self.checkpoint_period_s

    def _checkpoint(self) -> None:
        try:
            self._handle.flush()
            os.fsync(self._handle.fileno())
        except (OSError, ValueError, AttributeError):
            return
        document = {
            "archivo": self.path.name,
            "bytes_validos": self.stats.bytes_written,
            "bloques": self.stats.blocks,
            "bloques_descartados": self.stats.dropped_blocks,
            "t0_host_ns": self.t0_ns,
            "hora_local": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        try:
            path = self.path.with_name(self.path.stem + "_checkpoint.json")
            temporary = path.with_suffix(".json.tmp")
            with open(temporary, "w", encoding="utf-8") as handle:
                json.dump(document, handle, indent=2, ensure_ascii=False)
                handle.flush()
                os.fsync(handle.fileno())
            temporary.replace(path)
            self.stats.checkpoints += 1
        except OSError:
            pass

    def _raise_failure(self, message: str) -> None:
        with self._lock:
            self._failed = True
            self._closing = True
            self.stats.errors += 1
            self.stats.last_error = message
        if self.on_failure is not None:
            try:
                self.on_failure(message)
            except Exception:
                pass

    def stop(self, timeout: float = 20.0) -> Dict[str, Any]:
        with self._lock:
            if not self._running:
                return self.summary()
            self._closing = True
            self._ready.notify_all()
        if self._thread is not None:
            self._thread.join(timeout)
        self._thread = None
        with self._lock:
            self._running = False
        if self._handle is not None:
            try:
                self._handle.flush()
                os.fsync(self._handle.fileno())
            except (OSError, ValueError):
                pass
            self._checkpoint()
            try:
                self._handle.close()
            except (OSError, ValueError):
                pass
            self._handle = None
        summary = self.summary()
        self._write_meta(summary)
        return summary

    def summary(self) -> Dict[str, Any]:
        return {
            "archivo": self.path.name,
            "bytes_escritos": self.stats.bytes_written,
            "bloques": self.stats.blocks,
            "bloques_descartados": self.stats.dropped_blocks,
            "puntos_de_control": self.stats.checkpoints,
            "errores": self.stats.errors,
            "ultimo_error": self.stats.last_error,
            "flujo_caido": self._failed,
        }

    def _write_meta(self, summary: Dict[str, Any]) -> None:
        if not self.metadata:
            return
        document = dict(self.metadata)
        document.update({
            "archivo_principal": self.path.name,
            "creado": time.strftime("%Y-%m-%d %H:%M:%S"),
            "resumen": summary,
        })
        try:
            meta_path = self.path.with_name(self.path.stem + "_meta.json")
            with open(meta_path, "w", encoding="utf-8") as handle:
                json.dump(document, handle, indent=2, ensure_ascii=False)
        except OSError:
            pass
