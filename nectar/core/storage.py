"""Todo lo que hay que saber del disco antes de confiarle una toma.

Este módulo existe porque el fallo más caro de una campaña no es que el
programa se caiga: es que grabe cuatro horas sobre un volumen que no podía
aguantarlas y nadie se entere hasta volver al laboratorio.

Contiene tres cosas, separadas a propósito:

**Inspección.** ``inspect_target()`` dice qué sistema de archivos hay debajo
de una ruta, cuánto espacio libre queda, si se puede escribir ahí y cuál es el
tamaño máximo de archivo que ese sistema admite. Lo consume la validación de
destino de la pestaña 1, antes de armar nada.

**Volcado incremental.** ``push_to_disk()`` y ``drop_cache()`` empujan al
disco lo ya escrito sin bloquear al escritor y sueltan esas páginas del caché
del sistema. Sin esto, una toma de cientos de GB deja el caché de páginas
creciendo hasta que el núcleo decide vaciarlo de golpe, y ese vaciado cae
justo encima del camino de captura. Con esto, el caudal queda plano y lo que
un corte de energía se llevaría son segundos, no minutos.

**Reserva anticipada.** ``allocate()`` pide el espacio por adelantado
(``fallocate``), lo que mantiene el archivo contiguo en el SSD y hace que la
falta de espacio se descubra al armar y no a mitad de la toma.

Las tres degradan con elegancia: en un sistema que no ofrezca la llamada,
la función no hace nada y devuelve ``False``, nunca levanta.
"""

from __future__ import annotations

import errno
import os
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .. import config

# ---------------------------------------------------------------------------
# Tamaño máximo de archivo por sistema de archivos, en bytes.
# Solo se listan los que imponen un techo que una toma puede alcanzar.
# ---------------------------------------------------------------------------
MAX_FILE_SIZE = {
    "vfat": 4 * 1024 ** 3 - 1,
    "msdos": 4 * 1024 ** 3 - 1,
    "fat": 4 * 1024 ** 3 - 1,
    "fat32": 4 * 1024 ** 3 - 1,
    # ext4 con bloques de 4 KiB: 16 TiB. Muy por encima de cualquier toma.
    "ext4": 16 * 1024 ** 4,
    "ext3": 2 * 1024 ** 4,
}


# ---------------------------------------------------------------------------
# Inspección del volumen
# ---------------------------------------------------------------------------
def _mount_table() -> List[Tuple[str, str, str]]:
    """(punto_de_montaje, tipo, dispositivo) leído de /proc/mounts."""
    table: List[Tuple[str, str, str]] = []
    try:
        with open("/proc/mounts", "r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                parts = line.split()
                if len(parts) >= 3:
                    # Los espacios vienen escapados como \040.
                    mount = parts[1].replace("\\040", " ")
                    table.append((mount, parts[2], parts[0]))
    except OSError:
        pass
    return table


def filesystem_of(path: str | Path) -> Tuple[str, str, str]:
    """Sistema de archivos que sostiene ``path``.

    Devuelve ``(punto_de_montaje, tipo, dispositivo)``. Si no se puede
    averiguar (Windows, /proc no montado), devuelve cadenas vacías: quien
    llama debe tratar "no sé" como "no bloquear", nunca como "está mal".
    """
    probe = Path(path).resolve()
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    best: Tuple[str, str, str] = ("", "", "")
    best_len = -1
    target = str(probe)
    for mount, fstype, device in _mount_table():
        if target == mount or target.startswith(
                mount if mount.endswith(os.sep) else mount + os.sep):
            if len(mount) > best_len:
                best_len, best = len(mount), (mount, fstype, device)
    return best


@dataclass
class TargetInfo:
    """Retrato del volumen de destino en un instante."""

    path: Path
    exists: bool = False
    writable: bool = False
    free_bytes: int = 0
    total_bytes: int = 0
    mountpoint: str = ""
    filesystem: str = ""
    device: str = ""
    max_file_bytes: Optional[int] = None
    error: str = ""

    @property
    def free_gb(self) -> float:
        return self.free_bytes / 1e9

    def as_dict(self) -> Dict[str, object]:
        return {
            "ruta": str(self.path),
            "existe": self.exists,
            "escribible": self.writable,
            "libre_bytes": self.free_bytes,
            "total_bytes": self.total_bytes,
            "punto_montaje": self.mountpoint,
            "sistema_archivos": self.filesystem,
            "dispositivo": self.device,
            "tamano_max_archivo": self.max_file_bytes,
        }


def inspect_target(path: str | Path) -> TargetInfo:
    """Todo lo que se puede saber del destino sin escribir en él."""
    target = Path(path)
    info = TargetInfo(path=target)

    probe = target
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    info.exists = target.exists()

    try:
        usage = shutil.disk_usage(str(probe))
        info.free_bytes = usage.free
        info.total_bytes = usage.total
    except OSError as exc:
        info.error = f"{type(exc).__name__}: {exc}"

    info.mountpoint, info.filesystem, info.device = filesystem_of(probe)
    info.max_file_bytes = MAX_FILE_SIZE.get(info.filesystem.lower())
    info.writable = os.access(str(probe), os.W_OK | os.X_OK)
    return info


@dataclass
class Verdict:
    """Resultado de validar un destino. ``ok`` decide si se puede armar."""

    ok: bool = True
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    info: Optional[TargetInfo] = None

    def add_error(self, message: str) -> None:
        self.errors.append(message)
        self.ok = False

    def add_warning(self, message: str) -> None:
        self.warnings.append(message)

    @property
    def messages(self) -> List[str]:
        return [f"✖ {m}" for m in self.errors] + [f"⚠ {m}" for m in self.warnings]


def validate_destination(path: str | Path,
                         needed_bytes: Optional[float] = None,
                         largest_file_bytes: Optional[float] = None,
                         create: bool = False) -> Verdict:
    """Decide si se puede grabar en ``path`` y lo dice en castellano.

    ``needed_bytes`` es lo que va a ocupar la sesión completa (suma de todos
    los instrumentos por la duración pedida); ``largest_file_bytes`` es lo que
    va a ocupar **el archivo más grande de todos**, que es lo que choca contra
    el techo de FAT32 aunque el volumen tenga terabytes libres.
    """
    verdict = Verdict()
    target = Path(path)

    if create:
        try:
            target.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            verdict.add_error(f"No se pudo crear la carpeta de destino: {exc}")
            return verdict

    info = inspect_target(target)
    verdict.info = info

    if not info.exists and not create:
        verdict.add_error(f"La carpeta de destino no existe: {target}")
        return verdict

    if not info.writable:
        verdict.add_error(
            f"No hay permiso de escritura en {info.mountpoint or target}.")

    fstype = (info.filesystem or "").lower()
    if fstype in config.FILESYSTEMS_REJECTED:
        verdict.add_error(
            f"El destino está formateado como {fstype.upper()}, que corta "
            f"todo archivo a 4 GB. Una toma larga se perdería en silencio. "
            f"Usa un volumen ext4, XFS o NTFS.")
    elif fstype in config.FILESYSTEMS_WARNED:
        verdict.add_warning(
            f"El destino es {fstype.upper()}: funciona, pero no alcanza el "
            f"caudal de un NVMe local. Verifícalo con el banco de pruebas de "
            f"escritura antes de una toma larga.")

    if info.free_bytes <= config.MIN_FREE_BYTES:
        verdict.add_error(
            f"Quedan {info.free_gb:.2f} GB libres, por debajo del margen de "
            f"seguridad de {config.MIN_FREE_BYTES / 1e9:.0f} GB.")

    if needed_bytes:
        usable = info.free_bytes - config.MIN_FREE_BYTES
        if needed_bytes > usable:
            verdict.add_error(
                f"La sesión necesita {needed_bytes / 1e9:.1f} GB y solo hay "
                f"{max(0.0, usable / 1e9):.1f} GB utilizables "
                f"(libres menos el margen de "
                f"{config.MIN_FREE_BYTES / 1e9:.0f} GB).")

    if largest_file_bytes and info.max_file_bytes:
        if largest_file_bytes > info.max_file_bytes:
            verdict.add_error(
                f"El archivo más grande de la sesión llegaría a "
                f"{largest_file_bytes / 1e9:.1f} GB y este sistema de archivos "
                f"({fstype}) no admite más de "
                f"{info.max_file_bytes / 1e9:.1f} GB por archivo.")

    return verdict


# ---------------------------------------------------------------------------
# Volcado incremental y reserva de espacio
# ---------------------------------------------------------------------------
#: ``sync_file_range`` existe solo en Linux y solo desde Python 3.?; se
#: comprueba una vez al importar en vez de en cada llamada.
_HAS_SYNC_RANGE = hasattr(os, "sync_file_range")
_HAS_FADVISE = hasattr(os, "posix_fadvise")
_HAS_FALLOCATE = hasattr(os, "posix_fallocate")

_SYNC_WRITE = getattr(os, "SYNC_FILE_RANGE_WRITE", 2)


def push_to_disk(fd: int, offset: int, nbytes: int) -> bool:
    """Manda al disco un tramo ya escrito **sin esperar** a que termine.

    Es ``sync_file_range`` con ``SYNC_FILE_RANGE_WRITE``: le dice al núcleo
    "empieza a bajar estas páginas", y vuelve enseguida. No es un ``fsync``:
    no garantiza durabilidad por sí solo, pero evita que el caché de páginas
    crezca sin control durante una toma larga y que el vaciado se acumule
    hasta caer todo junto encima de la captura.
    """
    if not _HAS_SYNC_RANGE or nbytes <= 0:
        return False
    try:
        os.sync_file_range(fd, offset, nbytes, _SYNC_WRITE)
        return True
    except (OSError, ValueError):
        return False


def drop_cache(fd: int, offset: int, nbytes: int) -> bool:
    """Suelta del caché de páginas un tramo que ya está en el disco.

    Los datos de una toma no se van a volver a leer durante la toma, así que
    ocupar memoria con ellos solo le quita sitio a los anillos de captura.
    """
    if not _HAS_FADVISE or nbytes <= 0:
        return False
    try:
        os.posix_fadvise(fd, offset, nbytes, os.POSIX_FADV_DONTNEED)
        return True
    except (OSError, ValueError):
        return False


def allocate(fd: int, offset: int, length: int) -> bool:
    """Reserva espacio de verdad para un tramo del archivo.

    Devuelve ``False`` si el sistema no ofrece la llamada o si no hay espacio.
    El segundo caso es justamente lo que se quiere saber temprano.
    """
    if not _HAS_FALLOCATE or length <= 0:
        return False
    try:
        os.posix_fallocate(fd, offset, length)
        return True
    except OSError as exc:
        if exc.errno in (errno.EOPNOTSUPP, errno.EINVAL, errno.ENOSYS):
            return False
        raise
    except (ValueError, AttributeError):
        return False


def truncate_to(fd: int, size: int) -> bool:
    """Recorta el archivo al tamaño realmente escrito.

    Se llama al cerrar, para devolver el espacio que se había reservado por
    adelantado y no se llegó a usar.
    """
    try:
        os.ftruncate(fd, size)
        return True
    except OSError:
        return False


# ---------------------------------------------------------------------------
# Banco de pruebas de escritura
# ---------------------------------------------------------------------------
def benchmark_write(directory: str | Path, total_mb: int = 512,
                    block_mb: int = 8,
                    progress=None) -> Dict[str, object]:
    """Mide el caudal real de escritura del destino, escribiendo de verdad.

    No hay forma de saber si un volumen aguanta una Emergent salvo
    escribiéndole. Esto escribe ``total_mb`` en bloques de ``block_mb`` con el
    mismo patrón que usa el escritor de verdad (un ``write()`` grande por
    bloque, sin buffer de Python), fuerza el vaciado al final y borra el
    archivo. Devuelve el caudal sostenido y el peor bloque, que es la cifra
    que de verdad importa: un promedio bueno con un bloque de 900 ms esconde
    una pérdida de frames.
    """
    target = Path(directory)
    target.mkdir(parents=True, exist_ok=True)
    probe = target / f".nectar_benchmark_{int(time.time())}.tmp"

    block = os.urandom(block_mb * 1024 * 1024)
    blocks = max(1, int(total_mb // block_mb))
    per_block: List[float] = []

    result: Dict[str, object] = {
        "carpeta": str(target),
        "mb_escritos": blocks * block_mb,
        "bloque_mb": block_mb,
    }
    info = inspect_target(target)
    result["sistema_archivos"] = info.filesystem
    result["libre_gb"] = round(info.free_gb, 2)

    handle = None
    try:
        handle = open(probe, "wb", buffering=0)
        fd = handle.fileno()
        start = time.perf_counter()
        written = 0
        for index in range(blocks):
            tick = time.perf_counter()
            handle.write(block)
            # Mismo tratamiento que en grabación: empujar y soltar caché, para
            # que la medición refleje el comportamiento real y no la velocidad
            # de la memoria RAM.
            push_to_disk(fd, written, len(block))
            if written > 64 * 1024 * 1024:
                drop_cache(fd, 0, written - 64 * 1024 * 1024)
            written += len(block)
            per_block.append(time.perf_counter() - tick)
            if progress is not None and (index % 8 == 0):
                try:
                    progress((index + 1) / blocks)
                except Exception:
                    pass
        os.fsync(fd)
        elapsed = time.perf_counter() - start

        result["segundos"] = round(elapsed, 3)
        result["mb_s_medio"] = round(written / elapsed / 1e6, 1) if elapsed else 0.0
        worst = max(per_block) if per_block else 0.0
        result["peor_bloque_ms"] = round(worst * 1000, 2)
        result["mb_s_peor_bloque"] = round(block_mb / worst, 1) if worst else 0.0
        ordered = sorted(per_block)
        p99 = ordered[min(len(ordered) - 1, int(len(ordered) * 0.99))]
        result["p99_bloque_ms"] = round(p99 * 1000, 2)
        result["ok"] = True
    except OSError as exc:
        result["ok"] = False
        result["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        if handle is not None:
            try:
                handle.close()
            except OSError:
                pass
        try:
            probe.unlink(missing_ok=True)
        except OSError:
            pass
    return result


def human_bytes(value: float) -> str:
    """Bytes en la unidad que se lee de un vistazo."""
    magnitude = float(value)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(magnitude) < 1000.0 or unit == "TB":
            return f"{magnitude:,.1f} {unit}" if unit != "B" else f"{magnitude:,.0f} B"
        magnitude /= 1000.0
    return f"{magnitude:,.1f} TB"
