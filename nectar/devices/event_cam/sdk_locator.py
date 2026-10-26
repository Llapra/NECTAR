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


def _try_import() -> Tuple[bool, str]:
    try:
        import metavision_core.event_io          # noqa: F401
        import metavision_hal                    # noqa: F401
        return True, ""
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"


def _missing_dependency() -> Optional[str]:
    """Paquete de pip que le falta al SDK, o ``None``.

    El SDK puede estar en el camino y aun así no importar porque uno de sus
    propios imports no está en el entorno (``metavision_core.event_io`` importa
    ``h5py``, por ejemplo). Ese caso no tiene nada que ver con rutas ni con la
    versión de Python, y se arregla con un ``pip install``.
    """
    try:
        import metavision_core.event_io          # noqa: F401
        import metavision_hal                    # noqa: F401
    except ModuleNotFoundError as exc:
        name = (exc.name or "").split(".")[0]
        if name and not name.startswith("metavision"):
            return name
    except Exception:
        pass
    return None


def plugin_dirs() -> List[Path]:
    """Carpetas de plugins del HAL de terceros (CenturyArks · SilkyEvCam)."""
    if sys.platform.startswith("win"):
        bases = [r"C:\Program Files\CenturyArks\plugins",
                 r"C:\Program Files (x86)\CenturyArks\plugins"]
    else:
        bases = ["/usr/lib/CenturyArks/hal/plugins",
                 "/usr/local/lib/CenturyArks/hal/plugins"]
    return [Path(base) for base in bases]


def ensure_plugin_path() -> List[Path]:
    """Agrega a ``MV_HAL_PLUGIN_PATH`` los plugins instalados que falten.

    El HAL solo trae los plugins de Prophesee; el de la SilkyEvCam lo busca en
    ``MV_HAL_PLUGIN_PATH``. Si el programa se lanza desde un proceso que no
    heredó esa variable (un IDE abierto antes de instalar el plugin, un acceso
    directo, una terminal vieja), ``DeviceDiscovery`` devuelve una lista vacía
    sin ningún error, aunque ``metavision_viewer`` vea la cámara. Devuelve las
    carpetas que se agregaron.
    """
    current = [p for p in os.environ.get("MV_HAL_PLUGIN_PATH", "").split(os.pathsep)
               if p.strip()]
    known = {os.path.normcase(os.path.normpath(p)) for p in current}
    added: List[Path] = []
    for path in plugin_dirs():
        try:
            if not path.is_dir():
                continue
        except OSError:
            continue
        if os.path.normcase(os.path.normpath(str(path))) not in known:
            current.append(str(path))
            added.append(path)
    if added:
        os.environ["MV_HAL_PLUGIN_PATH"] = os.pathsep.join(current)
    return added


def dll_dirs() -> List[Path]:
    """Carpetas con las DLL del SDK y de los plugins (solo Windows)."""
    if not sys.platform.startswith("win"):
        return []
    dirs: List[Path] = []
    for base in (r"C:\Program Files\Prophesee", r"C:\Program Files\Metavision",
                 r"C:\Prophesee"):
        dirs.append(Path(base) / "bin")
        dirs.append(Path(base) / "third_party" / "bin")
    dirs.append(Path(r"C:\Program Files\CenturyArks\bin"))
    return dirs


#: Cookies de add_dll_directory: si se liberan, Windows quita la carpeta.
_dll_directory_cookies: list = []


def ensure_dll_path() -> List[Path]:
    """Agrega al ``PATH`` del proceso las carpetas de DLL del SDK que falten.

    El HAL carga los plugins con ``LoadLibrary``, que resuelve sus
    dependencias (libusb, boost, las DLL de Metavision) por el ``PATH``. Un
    proceso abierto antes de instalar Metavision —típicamente la terminal de
    un VS Code que lleva días abierto— hereda un ``PATH`` sin esas carpetas: el
    plugin de la SilkyEvCam no carga, el HAL dice "no plugin found" y la lista
    de cámaras sale vacía sin ningún error. Devuelve las carpetas agregadas.
    """
    current = [p for p in os.environ.get("PATH", "").split(os.pathsep) if p]
    known = {os.path.normcase(os.path.normpath(p)) for p in current}
    added: List[Path] = []
    for path in dll_dirs():
        try:
            if not path.is_dir():
                continue
        except OSError:
            continue
        if hasattr(os, "add_dll_directory"):
            try:
                _dll_directory_cookies.append(os.add_dll_directory(str(path)))
            except OSError:
                pass
        if os.path.normcase(os.path.normpath(str(path))) not in known:
            current.append(str(path))
            added.append(path)
    if added:
        os.environ["PATH"] = os.pathsep.join(current)
    return added


def ensure_on_path() -> Tuple[bool, str, Optional[Path]]:
    """Deja el SDK importable si se puede. ``(ok, motivo, carpeta_añadida)``.

    ``carpeta_añadida`` es ``None`` cuando el import funcionaba ya de por sí.
    """
    # Antes de importar: el HAL lee MV_HAL_PLUGIN_PATH al buscar cámaras, y
    # los plugins necesitan las DLL del SDK en el PATH para cargar.
    ensure_plugin_path()
    ensure_dll_path()
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

    Distingue los tres casos que en la práctica se confunden: el SDK no está
    instalado, está instalado pero este intérprete no lo ve, y está instalado y
    visible pero compilado para otra versión de Python.
    """
    ok, error, added = ensure_on_path()
    if ok and added is not None:
        return (f"SDK de Metavision encontrado en {added} y agregado al "
                f"camino de este intérprete.")
    if ok:
        return "SDK de Metavision disponible."

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
    missing = _missing_dependency()
    if missing:
        mensaje.append(
            f"El SDK se encuentra bien; lo que falta es «{missing}», una "
            f"librería de Python que el propio SDK importa. Instálala en este "
            f"entorno y vuelve a arrancar el programa:\n"
            f"    \"{interpreter}\" -m pip install {missing}")
    elif venv:
        site = (Path(sys.prefix) / "lib" / _python_tag() / "site-packages")
        mensaje.append(
            "Estás en un entorno virtual creado sin acceso a los paquetes del "
            "sistema, que es la causa habitual. La solución menos invasiva es "
            "añadir la carpeta del SDK al entorno con un archivo .pth:\n"
            f"    echo {sdk_dir} > {site}/metavision.pth\n"
            "y volver a arrancar el programa. (La alternativa es rehacer el "
            "entorno con: python3 -m venv --system-site-packages .venv, pero "
            "eso obliga a reinstalar las dependencias.)")
    else:
        mensaje.append(
            "La carpeta existe pero el import falla igual. Suele significar "
            "que el SDK se compiló para otra versión de Python: comprueba que "
            f"la instalación corresponde a {_python_tag()}.")
    return "\n".join(mensaje)
