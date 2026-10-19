"""Gestor de sesiones: quién decide dónde se guarda cada byte.

La pestaña 1 manda sobre todas las demás. Cuando se dispara una grabación
global, el ``SessionManager`` crea una carpeta con fecha y hora y **todo el
árbol completo por adelantado**, antes de que ningún instrumento intente
escribir:

    <destino>/NECTAR_<sesion>_<AAAAMMDD>_<HHMMSS>/
    ├── session_manifest.json      <- qué había conectado, T0, ancla UTC, resumen
    ├── nectar_sesion.log          <- la bitácora completa de esta toma
    ├── EvCam/
    │   ├── EvCam1/EvCam1_<sello>.raw + _meta.json + _checkpoint.json
    │   └── EvCam2/...
    ├── Emergent/
    │   ├── EmerCam1/EmerCam1_<sello>.raw + _frames.csv + _meta.json
    │   └── EmerCam2/...
    ├── Fibras/
    ├── Anemometro/
    ├── IMU_GPS/
    └── Reconstruccion/

Crear el árbol entero al armar, y no al primer dato de cada instrumento, no
es cosmético: es lo que convierte "el disco de destino está montado en modo
lectura" en un error que aparece **antes** del disparo, con todos los equipos
todavía parados, en vez de a los diez minutos y solo en la cámara que llegó
tarde.

**Ya no hay staging.** Antes se escribía en un SSD intermedio y un hilo movía
los bloques al destino. Se quitó: el equipo de campaña tiene discos rápidos,
y una copia intermedia agrega un punto de fallo, duplica el espacio necesario
y deja datos a medio camino si el programa muere. Se escribe directo donde el
operador eligió, que además es lo que él ve en pantalla.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from .. import config
from .logbook import LOG
from .time_sync import TIME_BASE

_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


def safe_name(text: str, fallback: str = "sesion") -> str:
    """Convierte un texto libre en algo que el sistema de archivos acepte."""
    cleaned = _SAFE.sub("_", (text or "").strip()).strip("_")
    return cleaned or fallback


def timestamp(fmt: str = "%Y%m%d_%H%M%S") -> str:
    return time.strftime(fmt)


class SessionManager:
    """Crea y describe la carpeta de una sesión de medición."""

    def __init__(self, base_dir: Optional[str] = None) -> None:
        self._lock = threading.RLock()
        self.base_dir = Path(base_dir or config.DEFAULT_BASE_DIR)
        self.session_dir: Optional[Path] = None
        self.session_name: Optional[str] = None
        self.started_unix: Optional[float] = None
        self._manifest: Dict[str, Any] = {}
        self._sealed = False

    # ------------------------------------------------------------------
    # Configuración
    # ------------------------------------------------------------------
    def set_base_dir(self, path: str | Path) -> None:
        self.base_dir = Path(path)

    @property
    def active(self) -> bool:
        return self.session_dir is not None

    # ------------------------------------------------------------------
    # Ciclo de vida de la sesión
    # ------------------------------------------------------------------
    def create_session(self, name: str = "", auto_timestamp: bool = True,
                       devices: Optional[list] = None,
                       duration_s: Optional[float] = None,
                       tree: Optional[List[tuple]] = None,
                       extra: Optional[Dict[str, Any]] = None) -> Path:
        """Crea la carpeta de sesión y el árbol completo. Devuelve la ruta.

        ``tree`` es una lista de ``(familia, etiqueta)``: se crea una carpeta
        por cada una **ahora**, de modo que un problema de permisos o de
        volumen se descubra acá y no a mitad de la toma.
        """
        with self._lock:
            stem = safe_name(name, "Sesion")
            folder = f"{config.APP_NAME}_{stem}"
            if config.force_demo():
                # Que nunca se confunda una carpeta de ensayo con una de datos.
                folder = f"{folder}_DEMO"
            if auto_timestamp:
                folder = f"{folder}_{timestamp()}"
            session = self.base_dir / folder

            suffix = 1
            while session.exists():
                suffix += 1
                session = self.base_dir / f"{folder}_{suffix:02d}"

            session.mkdir(parents=True, exist_ok=False)

            self.session_dir = session
            self.session_name = session.name
            self.started_unix = time.time()
            self._sealed = False

            created: List[str] = []
            for family, label in (tree or []):
                target = self._device_dir_locked(family, label)
                created.append(str(target.relative_to(session)))

            if config.force_demo():
                self._mark_demo_locked(session)

            log_path = LOG.attach_session(session)
            LOG.info(f"Sesión creada en {session}")
            if created:
                LOG.info("Árbol precreado: " + ", ".join(created))

            self._manifest = {
                "programa": f"{config.APP_NAME} {config.APP_VERSION}",
                "descripcion": config.APP_SUBTITLE,
                "sesion": session.name,
                "modo_demostracion": config.force_demo(),
                "creada_local": time.strftime("%Y-%m-%d %H:%M:%S"),
                "creada_unix": self.started_unix,
                "destino": str(session),
                "registro": log_path.name if log_path else None,
                "duracion_solicitada_s": duration_s,
                "escritura": "archivo_unico" if config.SINGLE_FILE_DEFAULT
                else "por_bloques",
                "carpetas_precreadas": created,
                "dispositivos_armados": devices or [],
                "reloj": TIME_BASE.snapshot(),
            }
            if extra:
                self._manifest.update(extra)
            self._flush_manifest_locked()
            return session

    def _mark_demo_locked(self, session: Path) -> None:
        """Deja constancia imborrable de que esos datos son inventados."""
        try:
            (session / config.DEMO_MARKER_NAME).write_text(
                config.DEMO_MARKER_TEXT, encoding="utf-8")
        except OSError:
            pass

    # ------------------------------------------------------------------
    def _device_dir_locked(self, family: str, device_label: str) -> Path:
        family_name = config.FAMILY_DIRS.get(family, safe_name(family, "Otros"))
        root = self.session_dir if self.session_dir is not None else self.base_dir
        target = root / family_name / safe_name(device_label, "dispositivo")
        target.mkdir(parents=True, exist_ok=True)
        return target

    def device_dir(self, family: str, device_label: str) -> Path:
        """Carpeta de un dispositivo dentro de la sesión activa.

        Si no hay sesión activa cae en la carpeta base: nunca devuelve None,
        para que un worker no tenga que decidir qué hacer sin carpeta.
        """
        with self._lock:
            return self._device_dir_locked(family, device_label)

    def standalone_dir(self, base: str | Path, family: str,
                       device_label: str, with_session_folder: bool = True) -> Path:
        """Carpeta para una grabación independiente, lanzada desde su pestaña.

        Mantiene el mismo orden que la sesión global (familia/dispositivo) pero
        colgando de la carpeta que esa pestaña tenga configurada.
        """
        root = Path(base)
        if with_session_folder:
            marca = "_DEMO" if config.force_demo() else ""
            root = root / (f"{config.APP_NAME}_"
                           f"{config.FAMILY_DIRS.get(family, family)}"
                           f"{marca}_{timestamp()}")
        target = root / safe_name(device_label, "dispositivo")
        target.mkdir(parents=True, exist_ok=True)
        if config.force_demo():
            self._mark_demo_locked(root)
        return target

    # ------------------------------------------------------------------
    # Manifiesto
    # ------------------------------------------------------------------
    def update_manifest(self, **fields: Any) -> None:
        with self._lock:
            if self._sealed:
                return
            self._manifest.update(fields)
            self._flush_manifest_locked()

    def add_device_summary(self, device_id: str, summary: Dict[str, Any]) -> None:
        """Guarda el resumen que emite un dispositivo al cerrar su archivo.

        El manifiesto **no se sella hasta que estos resúmenes llegaron**: son
        los que llevan las cuentas de frames perdidos, bytes escritos y
        errores de cada instrumento, y sin ellos la sesión queda documentada a
        medias justo en lo que más importa.
        """
        with self._lock:
            self._manifest.setdefault("resumen_dispositivos", {})[device_id] = summary
            self._flush_manifest_locked()

    def _flush_manifest_locked(self) -> None:
        if self.session_dir is None:
            return
        path = self.session_dir / config.SESSION_MANIFEST
        try:
            temporary = path.with_suffix(".json.tmp")
            with open(temporary, "w", encoding="utf-8") as handle:
                json.dump(self._manifest, handle, indent=2, ensure_ascii=False)
                handle.flush()
                os.fsync(handle.fileno())
            temporary.replace(path)
        except OSError:
            pass  # el manifiesto no puede tumbar una grabación en curso

    def close_session(self, summary: Optional[Dict[str, Any]] = None) -> Optional[Path]:
        """Cierra la sesión, sella el manifiesto y devuelve su carpeta."""
        with self._lock:
            if self.session_dir is None:
                return None
            self._manifest["cerrada_local"] = time.strftime("%Y-%m-%d %H:%M:%S")
            self._manifest["cerrada_unix"] = time.time()
            if self.started_unix:
                self._manifest["duracion_real_s"] = round(
                    time.time() - self.started_unix, 3)
            self._manifest["reloj"] = TIME_BASE.snapshot()
            if summary:
                self._manifest["resumen"] = summary
            self._manifest["inventario"] = self._inventory_locked()
            self._flush_manifest_locked()
            self._sealed = True
            path = self.session_dir
            LOG.info(f"Manifiesto sellado en {path / config.SESSION_MANIFEST}")
            LOG.sync()
            LOG.detach_session()
            self.session_dir = None
            self.session_name = None
            self.started_unix = None
            return path

    def _inventory_locked(self) -> Dict[str, Any]:
        """Lista lo que de verdad quedó escrito en disco.

        Es la comprobación final: no lo que los instrumentos creen que
        grabaron, sino los archivos que existen y su tamaño.
        """
        if self.session_dir is None:
            return {}
        files: List[Dict[str, Any]] = []
        total = 0
        try:
            for path in sorted(self.session_dir.rglob("*")):
                if not path.is_file():
                    continue
                try:
                    size = path.stat().st_size
                except OSError:
                    continue
                total += size
                files.append({
                    "ruta": str(path.relative_to(self.session_dir)),
                    "bytes": size,
                })
        except OSError:
            pass
        return {"archivos": files, "cantidad": len(files), "bytes_totales": total}

    # ------------------------------------------------------------------
    def disk_free_bytes(self, path: Optional[str | Path] = None) -> int:
        """Espacio libre en el volumen de destino, para el presupuesto."""
        import shutil
        target = Path(path or self.session_dir or self.base_dir)
        probe = target
        while not probe.exists() and probe != probe.parent:
            probe = probe.parent
        try:
            return shutil.disk_usage(str(probe)).free
        except OSError:
            return 0
