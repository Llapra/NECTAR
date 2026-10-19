"""Nombres de dispositivo que no cambian entre sesiones.

El problema que resuelve
------------------------
La etiqueta de un dispositivo (``EvCam1``, ``EmerCam2``) da nombre a la
carpeta donde se graban sus datos. Si esa etiqueta se asigna por orden de
aparición, basta con que una cámara arranque medio segundo más tarde, o que
se conecte en otro puerto USB, para que la que ayer era ``EvCam1`` hoy sea
``EvCam2``. Al analizar, "la cámara de la esquina norte" deja de ser
identificable y no hay forma de saberlo mirando los archivos.

Aquí se ancla la etiqueta a la **identidad física** del equipo —número de
serie, puerto serie, dirección IP— y se guarda en disco. La cámara con el
serial ``00042003`` será ``EvCam1`` en todas las campañas, aparezca cuando
aparezca.

Encima de eso va el **alias**: el nombre que el operador le pone en la
interfaz ("Norte", "Pilar M2", "Fibra del espejo"). El alias es lo que se ve
en la pantalla y lo que queda escrito en los metadatos; la etiqueta sigue
siendo la que nombra la carpeta, porque tiene que ser un nombre seguro para
el sistema de archivos y no cambiar aunque el operador cambie de idea.

Si el número de serie no se puede leer —cosa que pasa con estas cámaras— se
cae al identificador del puerto y se deja constancia, para que en la interfaz
quede claro de dónde salió el nombre en vez de fingir una certeza que no hay.
"""

from __future__ import annotations

import json
import threading
import time
from typing import Any, Dict, Optional

from .. import config

#: Prefijo de etiqueta por familia. Es lo que se ve en el nombre de carpeta.
LABEL_STEMS = {
    "event_cam": "EvCam",
    "emergent": "EmerCam",
    "reconstruction": "Recon",
    "fibers": "Fibras",
    "anemometer": "Anemometro",
    "imu": "IMU",
}

#: Campos del descriptor que sirven como identidad física, por orden de
#: preferencia. El primero que traiga un valor útil manda.
IDENTITY_FIELDS = ["serial", "mac", "ip", "port", "id"]


def identity_of(family: str, descriptor: Dict[str, Any]) -> tuple[str, str]:
    """Clave estable del dispositivo y de dónde salió.

    Devuelve ``(clave, origen)``. ``origen`` es el nombre del campo que se
    usó, para poder decirlo en la interfaz: no es lo mismo un nombre anclado
    al número de serie que uno anclado al puerto USB, que cambia de sitio.
    """
    for field in IDENTITY_FIELDS:
        value = str(descriptor.get(field, "") or "").strip()
        if value and value not in ("0.0.0.0", "SIM", "unknown"):
            return f"{family}:{field}:{value}", field
    return f"{family}:sin_identidad", "ninguno"


class AliasStore:
    """Catálogo persistente de etiquetas y alias, con cerrojo."""

    def __init__(self, path=None) -> None:
        self._path = path or config.ALIASES_FILE
        self._lock = threading.RLock()
        self._data: Dict[str, Dict[str, Any]] = {}
        self._load()

    # ------------------------------------------------------------------
    def _load(self) -> None:
        try:
            with open(self._path, "r", encoding="utf-8") as handle:
                loaded = json.load(handle)
            if isinstance(loaded, dict):
                self._data = {str(k): dict(v) for k, v in loaded.items()
                              if isinstance(v, dict)}
        except (OSError, ValueError):
            self._data = {}

    def _save_locked(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self._path.with_suffix(".json.tmp")
            with open(temporary, "w", encoding="utf-8") as handle:
                json.dump(self._data, handle, indent=2, ensure_ascii=False)
            temporary.replace(self._path)
        except OSError:
            pass    # el catálogo de nombres no puede impedir trabajar

    # ------------------------------------------------------------------
    def entry(self, key: str) -> Dict[str, Any]:
        with self._lock:
            return dict(self._data.get(key, {}))

    def label_for(self, family: str, descriptor: Dict[str, Any],
                  taken: Optional[set] = None) -> tuple[str, str, str]:
        """Etiqueta estable, alias y origen de la identidad.

        ``taken`` son las etiquetas ya en uso en esta ejecución: si la
        guardada choca con otra (dos equipos que comparten identidad, o un
        catálogo traído de otro computador), se busca el siguiente número
        libre y se vuelve a anclar.
        """
        key, origin = identity_of(family, descriptor)
        taken = taken or set()
        stem = LABEL_STEMS.get(family, family)

        with self._lock:
            record = self._data.get(key)
            label = str(record.get("etiqueta", "")) if record else ""
            if not label or (label in taken):
                index = 1
                while f"{stem}{index}" in taken or self._label_claimed(
                        f"{stem}{index}", key):
                    index += 1
                label = f"{stem}{index}"

            record = dict(record or {})
            record.update({
                "etiqueta": label,
                "familia": family,
                "origen_identidad": origin,
                "visto_ultima_vez": time.strftime("%Y-%m-%d %H:%M:%S"),
                "descriptor": {k: descriptor.get(k) for k in IDENTITY_FIELDS
                               if descriptor.get(k)},
            })
            record.setdefault("alias", "")
            self._data[key] = record
            self._save_locked()
            return label, str(record.get("alias", "")), origin

    def _label_claimed(self, label: str, except_key: str) -> bool:
        for key, record in self._data.items():
            if key != except_key and record.get("etiqueta") == label:
                return True
        return False

    # ------------------------------------------------------------------
    def alias(self, key: str) -> str:
        return str(self.entry(key).get("alias", ""))

    def set_alias(self, key: str, alias: str) -> None:
        """Guarda el nombre que el operador le puso al equipo."""
        with self._lock:
            record = self._data.setdefault(key, {})
            record["alias"] = str(alias).strip()
            record["alias_editado"] = time.strftime("%Y-%m-%d %H:%M:%S")
            self._save_locked()

    def all_entries(self) -> Dict[str, Dict[str, Any]]:
        with self._lock:
            return {k: dict(v) for k, v in self._data.items()}

    def forget(self, key: str) -> None:
        with self._lock:
            if self._data.pop(key, None) is not None:
                self._save_locked()


#: Catálogo único del programa.
ALIASES = AliasStore()
