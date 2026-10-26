"""Encontrar el SDK de Metavision aunque no esté en el camino del intérprete.

El problema real, que muerde a todo el mundo
--------------------------------------------
El SDK de Prophesee (openeb o Metavision SDK) se instala **fuera** de
``pip``: en Linux normalmente en ``/usr/local/lib/python3.X/dist-packages`` y
en Windows bajo ``C:\\Program Files\\Prophesee``. Un entorno virtual creado con
``python -m venv`` **no ve esas carpetas**, porque por omisión aísla el
intérprete de los paquetes del sistema.

El resultado es desconcertante: ``python3 -c "import metavision_core"`` desde
la terminal funciona, pero el mismo import dentro del entorno virtual del
programa falla, y el programa informa "el SDK no está disponible" cuando el SDK
sí está instalado. Es un error de ruta, no de instalación, y el mensaje
genérico no ayudaba a distinguirlos.

Qué hace este módulo
--------------------
Antes de rendirse, busca ``metavision_core`` en las carpetas donde el SDK se
instala de verdad y, si lo encuentra, la agrega a ``sys.path`` y reintenta el
import. Si funciona, el programa arranca con las cámaras disponibles y deja
constancia de qué carpeta añadió. Si no funciona, el diagnóstico dice
exactamente qué se encontró y qué falta, con el comando para arreglarlo.

No es magia escondida: todo lo que hace queda en ``diagnose()``, que se
imprime en ``python main.py --check`` y se muestra en el registro de la
pestaña cuando el descubrimiento falla.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import List, Optional, Tuple


def _python_tag() -> str:
    return f"python{sys.version_info.major}.{sys.version_info.minor}"


def candidate_dirs() -> List[Path]:
    """Carpetas donde el SDK de Prophesee se instala, por orden de probabilidad.

    La variable ``NECTAR_METAVISION_PATH`` manda sobre todo lo demás, para el
    caso de una instalación en un sitio raro o compilada a mano.
    """
    tag = _python_tag()
    candidates: List[Path] = []

    override = os.environ.get("NECTAR_METAVISION_PATH", "").strip()
    if override:
        candidates.extend(Path(p) for p in override.split(os.pathsep) if p)

    if sys.platform.startswith("win"):
        for base in (r"C:\Program Files\Prophesee",
                     r"C:\Program Files\Metavision",
                     r"C:\Prophesee"):
            candidates.append(Path(base) / "lib" / "python3" / "site-packages")
            candidates.append(Path(base) / "lib" / "site-packages")
    else:
        # openeb / Metavision SDK instalados con `make install` van a
        # /usr/local; los paquetes .deb oficiales van a /usr.
        for prefix in ("/usr/local", "/usr", "/opt/prophesee", "/opt/metavision"):
            candidates.append(Path(prefix) / "lib" / tag / "dist-packages")
            candidates.append(Path(prefix) / "lib" / tag / "site-packages")
            candidates.append(Path(prefix) / "lib" / "python3" / "dist-packages")

    # Sin duplicados, conservando el orden.
    unique: List[Path] = []
    seen = set()
    for path in candidates:
        key = str(path)
        if key not in seen:
            seen.add(key)
            unique.append(path)
    return unique


def find_sdk_dir() -> Optional[Path]:
    """Primera carpeta candidata que contenga ``metavision_core``."""
    for path in candidate_dirs():
        try:
            if (path / "metavision_core").is_dir():
                return path
        except OSError:
            continue
    return None


#: Paquetes de pip que el SDK necesita por dentro y que NECTAR no usa
#: directamente. Van acá porque su ausencia se manifiesta como un
#: ``ModuleNotFoundError`` dentro del SDK, que es muy fácil de leer como "no
#: está el SDK" cuando el SDK sí está.
DEPENDENCIAS_DEL_SDK = {
    "h5py": ("metavision_core.event_io lo importa para leer y escribir "
             "archivos HDF5 de eventos"),
    "scipy": "varios módulos de metavision_core lo usan para filtrado",
    "cv2": ("algunos módulos del SDK lo usan para mostrar imágenes "
            "(paquete de pip: opencv-python)"),
    "pandas": "metavision_core lo usa para los resúmenes de eventos",
}

#: Nombre del módulo → nombre del paquete de pip, cuando no coinciden.
PAQUETE_PIP = {"cv2": "opencv-python"}


def _try_import() -> Tuple[bool, str]:
    """Importa los dos módulos del SDK **por separado**.

    La versión anterior los importaba juntos en un mismo ``try``, así que
    cualquier fallo producía el mismo mensaje genérico. Eso costó una sesión
    entera de depuración: faltaba ``h5py`` —una dependencia interna de
    ``metavision_core.event_io``, no de NECTAR— y el programa informaba "el
    SDK de Metavision no está disponible" con el SDK perfectamente instalado.

    Ahora cada módulo se reporta por su nombre y el error se devuelve tal
    como vino, para que ``diagnose()`` pueda distinguir los casos.
    """
    fallos = []
    for nombre in ("metavision_hal", "metavision_core.event_io"):
        try:
            __import__(nombre)
        except Exception as exc:
            fallos.append(f"{nombre}: {type(exc).__name__}: {exc}")
    if not fallos:
        return True, ""
    return False, " | ".join(fallos)


def _missing_dependency(error: str) -> Optional[str]:
    """Módulo faltante que el error delata, si es una dependencia del SDK.

    Busca el nombre entre comillas de un ``ModuleNotFoundError``. Si el módulo
    que falta es uno de los que el SDK necesita por dentro, lo devuelve; si el
    que falta es el propio SDK, no, porque ése es un caso distinto.
    """
    if "ModuleNotFoundError" not in error and "No module named" not in error:
        return None
    for nombre in DEPENDENCIAS_DEL_SDK:
        if f"'{nombre}'" in error or f"named {nombre}" in error:
            return nombre
    return None


def dependency_hint(error: str) -> str:
    """Instrucción concreta para una dependencia interna del SDK que falta."""
    modulo = _missing_dependency(error)
    if modulo is None:
        return ""
    paquete = PAQUETE_PIP.get(modulo, modulo)
    motivo = DEPENDENCIAS_DEL_SDK[modulo]
    return (
        f"El SDK de Metavision **sí está instalado y visible**, pero le falta "
        f"una dependencia interna: el módulo '{modulo}'.\n"
        f"No es un paquete de NECTAR: {motivo}.\n"
        f"Con el entorno virtual activado:\n"
        f"    pip install {paquete}\n"
        f"y vuelve a arrancar el programa.")


def ensure_on_path() -> Tuple[bool, str, Optional[Path]]:
    """Deja el SDK importable si se puede. ``(ok, motivo, carpeta_añadida)``.

    ``carpeta_añadida`` es ``None`` cuando el import funcionaba ya de por sí.
    """
    ok, error = _try_import()
    if ok:
        return True, "", None

    sdk_dir = find_sdk_dir()
    if sdk_dir is None:
        return False, error, None

    # Se agrega al final: nada de lo que ya funciona puede quedar tapado por
    # una carpeta del sistema.
    if str(sdk_dir) not in sys.path:
        sys.path.append(str(sdk_dir))
    ok, error_after = _try_import()
    if ok:
        return True, "", sdk_dir
    return False, error_after or error, sdk_dir


def diagnose() -> str:
    """Explicación en castellano de por qué el SDK no está disponible.

    Distingue los cuatro casos que en la práctica se confunden:

    1. el SDK no está instalado;
    2. está instalado pero este intérprete no lo ve (el caso del entorno
       virtual sin acceso a los paquetes del sistema);
    3. está instalado y visible pero le falta una dependencia interna, por
       ejemplo ``h5py`` — el caso que hizo perder una sesión entera;
    4. está instalado y visible pero compilado para otra versión de Python.
    """
    ok, error, added = ensure_on_path()
    if ok and added is not None:
        return (f"SDK de Metavision encontrado en {added} y agregado al "
                f"camino de este intérprete.")
    if ok:
        return "SDK de Metavision disponible."

    # Caso 3 antes que los demás: si lo que falta es una dependencia interna,
    # hablar de rutas y de entornos virtuales solo desorienta.
    pista = dependency_hint(error)
    if pista:
        return f"{pista}\nIntérprete: {sys.executable}\nError: {error}"

    sdk_dir = find_sdk_dir()
    interpreter = sys.executable
    venv = sys.prefix != getattr(sys, "base_prefix", sys.prefix)

    if sdk_dir is None:
        revisadas = "\n  ".join(str(p) for p in candidate_dirs()[:6])
        return (
            "El SDK de Metavision no está instalado, o está en una carpeta que "
            "no conozco.\n"
            f"Intérprete: {interpreter}\n"
            f"Carpetas revisadas:\n  {revisadas}\n"
            "Si está en otro sitio, indícalo con la variable "
            "NECTAR_METAVISION_PATH.\n"
            f"Error del import: {error}")

    mensaje = [
        f"El SDK de Metavision **sí está instalado** en {sdk_dir}, pero este "
        f"intérprete no lo puede importar.",
        f"Intérprete: {interpreter}",
        f"Error: {error}",
    ]
    if venv:
        site = (Path(sys.prefix) / "lib" / _python_tag() / "site-packages")
        mensaje.append(
            "Estás en un entorno virtual creado sin acceso a los paquetes del "
            "sistema, que es la causa habitual. La solución menos invasiva es "
            "añadir la carpeta del SDK al entorno con un archivo .pth:\n"
            f"    echo {sdk_dir} > {site}/metavision.pth\n"
            "y volver a arrancar el programa. (Lo mismo, y todo lo demás, "
            "lo hace: bash instalar_venv.sh)")
    else:
        mensaje.append(
            "La carpeta existe pero el import falla igual. Suele significar "
            "que el SDK se compiló para otra versión de Python: comprueba que "
            f"la instalación corresponde a {_python_tag()}.")
    return "\n".join(mensaje)
