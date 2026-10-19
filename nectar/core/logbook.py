"""Bitácora en disco: lo que pasó, con hora, aunque nadie estuviera mirando.

La consola de la pestaña 1 sirve mientras alguien la mira. En una campaña
nocturna de varias horas nadie la mira todo el rato, y si a las 03:40 una
cámara se cayó y se recuperó sola, eso tiene que quedar escrito en algún
lado o el dato de esa noche queda sin explicación.

Este módulo escribe dos veces lo mismo:

* un **registro rodante del programa** en ``~/.nectar/nectar.log``, que
  sobrevive a las sesiones y sirve para ver qué pasó ayer;
* un **registro de la sesión** dentro de la propia carpeta de la sesión,
  para que el archivo viaje con los datos. Si un año después alguien abre la
  carpeta de una toma, ahí está la historia completa de esa toma.

Las líneas llevan tres relojes, porque los tres hacen falta: la hora local
para conversar con una persona, el reloj monotónico del host para medir
intervalos sin que un ajuste de hora los deforme, y el desfase respecto a T0
para ubicar el suceso dentro de la toma.

Escribir nunca puede tumbar una grabación: todo va dentro de ``try``, y si el
archivo falla se sigue adelante sin él.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Optional, TextIO

from .. import config
from .time_sync import TIME_BASE

#: Tamaño a partir del cual el registro del programa se rota.
ROTATE_BYTES = 8 * 1024 * 1024


class Logbook:
    """Registro de texto con marca de tiempo triple, seguro entre hilos."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._program: Optional[TextIO] = None
        self._session: Optional[TextIO] = None
        self._session_path: Optional[Path] = None
        self._echo = None           # callback opcional hacia la consola
        self._open_program()

    # ------------------------------------------------------------------
    def _open_program(self) -> None:
        try:
            config.STATE_DIR.mkdir(parents=True, exist_ok=True)
            path = config.PROGRAM_LOG
            if path.exists() and path.stat().st_size > ROTATE_BYTES:
                backup = path.with_suffix(".log.1")
                try:
                    backup.unlink(missing_ok=True)
                    path.rename(backup)
                except OSError:
                    pass
            self._program = open(path, "a", encoding="utf-8")
            self._raw(self._program,
                      f"\n===== {config.APP_NAME} {config.APP_VERSION} "
                      f"iniciado {time.strftime('%Y-%m-%d %H:%M:%S')} =====")
        except OSError:
            self._program = None

    # ------------------------------------------------------------------
    def attach_session(self, session_dir: Path) -> Optional[Path]:
        """Abre el registro dentro de la carpeta de una sesión nueva."""
        with self._lock:
            self._close_session_locked()
            try:
                path = Path(session_dir) / config.SESSION_LOG_NAME
                self._session = open(path, "a", encoding="utf-8")
                self._session_path = path
                self._raw(self._session,
                          f"===== Sesión {Path(session_dir).name} · "
                          f"{config.APP_NAME} {config.APP_VERSION} =====")
                return path
            except OSError:
                self._session = None
                self._session_path = None
                return None

    def detach_session(self) -> None:
        with self._lock:
            self._close_session_locked()

    def _close_session_locked(self) -> None:
        if self._session is not None:
            try:
                self._raw(self._session,
                          f"===== Fin del registro de sesión "
                          f"{time.strftime('%Y-%m-%d %H:%M:%S')} =====")
                self._session.flush()
                self._session.close()
            except OSError:
                pass
        self._session = None
        self._session_path = None

    @property
    def session_path(self) -> Optional[Path]:
        return self._session_path

    # ------------------------------------------------------------------
    def set_echo(self, callback) -> None:
        """Repite cada línea hacia la consola de la interfaz."""
        self._echo = callback

    # ------------------------------------------------------------------
    def write(self, message: str, level: str = "INFO") -> None:
        """Escribe una línea en los dos registros."""
        now_ns = TIME_BASE.now_ns()
        t0 = TIME_BASE.t0_ns
        relative = f"T0{(now_ns - t0) / 1e9:+11.3f}s" if t0 else "  ---------- "
        line = (f"{time.strftime('%Y-%m-%d %H:%M:%S')} "
                f"{relative} [{level:<5}] {message}")
        with self._lock:
            for handle in (self._program, self._session):
                self._raw(handle, line)
        if self._echo is not None:
            try:
                self._echo(message)
            except Exception:
                pass

    def info(self, message: str) -> None:
        self.write(message, "INFO")

    def warn(self, message: str) -> None:
        self.write(message, "AVISO")

    def error(self, message: str) -> None:
        self.write(message, "ERROR")
        self.flush()

    def critical(self, message: str) -> None:
        """Error que compromete los datos: se sella en disco de inmediato."""
        self.write(message, "GRAVE")
        self.sync()

    # ------------------------------------------------------------------
    @staticmethod
    def _raw(handle: Optional[TextIO], line: str) -> None:
        if handle is None:
            return
        try:
            handle.write(line + "\n")
        except (OSError, ValueError):
            pass

    def flush(self) -> None:
        with self._lock:
            for handle in (self._program, self._session):
                if handle is not None:
                    try:
                        handle.flush()
                    except (OSError, ValueError):
                        pass

    def sync(self) -> None:
        """Vacía y fuerza al disco. Para lo que no se puede perder."""
        import os
        with self._lock:
            for handle in (self._program, self._session):
                if handle is None:
                    continue
                try:
                    handle.flush()
                    os.fsync(handle.fileno())
                except (OSError, ValueError):
                    pass

    def close(self) -> None:
        self.flush()
        with self._lock:
            self._close_session_locked()
            if self._program is not None:
                try:
                    self._program.close()
                except OSError:
                    pass
            self._program = None


#: Bitácora única del programa.
LOG = Logbook()
