"""Puerta de entrada al SDK de Metavision: listar y abrir cámaras de eventos.

La regla de este módulo, desde la versión 1.0.2
-----------------------------------------------
**Se llama al SDK exactamente como lo hace el programa anterior del
laboratorio (``main_v14.1.py``), que funciona con la SilkyEvCam en el PC del
laboratorio.** Nada de inventiva en la parte que toca el hardware.

Concretamente:

* **Abrir es una sola llamada, una sola vez:**
  ``EventsIterator(input_path=..., delta_t=...)``. Sin cascada de intentos.

* **Listar es solo** ``DeviceDiscovery.list()``, que devuelve cadenas
  ``integrador:plugin:serial`` y está comprobado en ese PC. Buscar cámaras
  **nunca** abre una cámara.

* **El dispositivo del HAL** (biases, sincronismo, recorte, grabación) se mira
  por dentro del iterador, leyendo atributos y nada más, igual que el programa
  anterior. Si no está, no se intenta conseguirlo por otra vía: se informa, y
  la grabación se niega a armarse con el motivo escrito.

Por qué la versión 1.0.1 se caía
--------------------------------
La 1.0.1 intentó ser más lista y se cayó con **violación de segmento** (una
caída dentro del código nativo del SDK, que se lleva el programa entero). Tenía
tres defectos, los tres del programa y ninguno del SDK ni de la instalación:

1. **Abría la cámara más de una vez.** Probaba primero
   ``DeviceDiscovery.open()`` y, si algo fallaba a medias, volvía a abrir con
   ``EventsIterator`` mientras la primera apertura seguía viva. Abrir dos
   veces el mismo dispositivo USB en el mismo proceso es lo que tumba al
   código nativo. El aviso del HAL salía dos veces antes de la caída: era la
   cámara abriéndose dos veces.

2. **Podía inventar cámaras.** Daba prioridad a
   ``DeviceDiscovery.list_available_sources()``, que devuelve objetos cuyos
   campos cambian entre versiones del SDK. Si no encontraba el campo del
   número de serie, usaba el texto del objeto (``<CameraDescription object at
   0x7f...>``) como serial; como cada objeto tiene otra dirección de memoria,
   una cámara física aparecía como dos.

3. **Abría la cámara durante la búsqueda** (una "sonda" para confirmar que
   había cámara cuando el listado salía vacío). La búsqueda se repite en varias
   pestañas al arrancar, así que eso era abrir varias veces sin que nadie lo
   pidiera.

Las tres cosas se fueron. Lo que queda es lo que ya funcionaba.

Grabación
---------
En el PC del laboratorio el iterador **no deja ver** el dispositivo del HAL
(ni ``reader.device`` ni ``_reader.device``), y sin dispositivo no hay canal
para escribir el ``.raw``. El programa anterior tiene exactamente la misma
limitación —usa las mismas líneas— y la salta en silencio. Aquí se dice.

La forma que documenta Prophesee para tener el dispositivo y grabar es
``initiate_device()`` + ``EventsIterator.from_device()``, que abre la cámara
una sola vez. **No se usa todavía**: primero se prueba con la cámara real en el
banco de pruebas (``probar_evcam.py``, ruta E) y solo si funciona entra aquí.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

# Rutas de apertura que puede informar ``open_camera``.
RUTA_ITERADOR_CON_HAL = "iterador_con_hal"
RUTA_ITERADOR_SIN_HAL = "iterador_sin_hal"

#: Facilidades del HAL que NECTAR usa, con para qué sirve cada una.
FACILIDADES = (
    ("get_i_events_stream", "grabación del .raw"),
    ("get_i_ll_biases", "biases"),
    ("get_i_camera_synchronization", "sincronismo maestro/esclavo"),
    ("get_i_roi", "recorte en el sensor"),
    ("get_i_hw_identification", "identificación del equipo"),
)


@dataclass
class CameraInfo:
    """Una cámara vista por el HAL, ya normalizada."""

    serial: str
    integrator: str = ""
    plugin: str = ""
    #: La cadena tal como la devolvió el SDK, para el registro.
    raw: str = ""
    #: Qué método del SDK la encontró.
    source: str = ""

    @property
    def label_hint(self) -> str:
        partes = [p for p in (self.integrator, self.plugin) if p]
        return " · ".join(partes) if partes else "cámara de eventos"


@dataclass
class OpenResult:
    """Resultado de abrir una cámara."""

    iterator: Any = None
    device: Any = None
    ruta: str = RUTA_ITERADOR_SIN_HAL
    #: La llamada exacta que se hizo al SDK, y lo que se encontró. Va al
    #: registro de la pestaña.
    notas: List[str] = field(default_factory=list)

    @property
    def resumen(self) -> str:
        if self.device is not None:
            return ("abierta como en main_v14.1, y el dispositivo del HAL es "
                    "accesible: biases, sincronismo y grabación disponibles "
                    "según lo que exponga esta cámara")
        return ("abierta como en main_v14.1. El dispositivo del HAL NO es "
                "accesible en esta versión del SDK: la vista en vivo funciona, "
                "pero no se aplican biases ni sincronismo y no se puede grabar "
                "el .raw (el programa anterior tiene la misma limitación)")


# ---------------------------------------------------------------------------
# Listado
# ---------------------------------------------------------------------------
def _parse_descriptor(texto: str) -> Optional[CameraInfo]:
    """Interpreta ``integrador:plugin:serial``.

    Solo acepta **cadenas** con ese formato o un serial pelado. Nunca se
    interpreta el texto de un objeto: ese fue el origen de las cámaras
    fantasma de la 1.0.1.
    """
    bruto = str(texto).strip()
    if not bruto or bruto.startswith("<"):
        return None
    partes = [p.strip() for p in bruto.split(":")]
    serial = partes[-1]
    if not serial:
        return None
    if len(partes) >= 3:
        return CameraInfo(serial=serial, integrator=partes[0],
                          plugin=":".join(partes[1:-1]), raw=bruto)
    if len(partes) == 2:
        return CameraInfo(serial=serial, integrator=partes[0], raw=bruto)
    return CameraInfo(serial=serial, raw=bruto)


def list_cameras() -> List[CameraInfo]:
    """Cámaras de eventos conectadas, una por número de serie.

    Usa ``DeviceDiscovery.list()``, que devuelve cadenas y está comprobado en
    el PC del laboratorio. Si esa versión del SDK no lo tiene, usa
    ``list_serial_numbers()``, que también devuelve cadenas. Los métodos que
    devuelven objetos no se usan para sacar números de serie.

    **No abre ninguna cámara.**

    La deduplicación es necesaria de verdad: en la SilkyEvCam, ``list()``
    devuelve el mismo serial dos veces.
    """
    from metavision_hal import DeviceDiscovery           # type: ignore

    encontradas: List[CameraInfo] = []
    for metodo in ("list", "list_serial_numbers"):
        funcion = getattr(DeviceDiscovery, metodo, None)
        if funcion is None:
            continue
        resultado = list(funcion())
        for elemento in resultado:
            if not isinstance(elemento, (str, bytes)):
                continue
            if isinstance(elemento, bytes):
                elemento = elemento.decode("utf-8", errors="replace")
            info = _parse_descriptor(elemento)
            if info is None:
                continue
            info.source = f"DeviceDiscovery.{metodo}()"
            encontradas.append(info)
        # El primer método que existe manda, aunque devuelva una lista vacía:
        # una lista vacía de un método que funciona significa "no hay
        # cámaras", y no es motivo para probar otro.
        break

    unicas: List[CameraInfo] = []
    vistos = set()
    for info in encontradas:
        if info.serial in vistos:
            continue
        vistos.add(info.serial)
        unicas.append(info)
    return unicas


def has_listing() -> bool:
    """``True`` si esta versión del SDK tiene algún método de listado usable."""
    try:
        from metavision_hal import DeviceDiscovery       # type: ignore
    except Exception:
        return False
    return any(getattr(DeviceDiscovery, metodo, None) is not None
               for metodo in ("list", "list_serial_numbers"))


def listing_report() -> str:
    """Qué devuelve el listado del HAL. Para ``--check``.

    Solo llama a los métodos que devuelven cadenas. Los que devuelven objetos
    se mencionan pero no se llaman: no aportan nada que ``list()`` no dé, y
    sus campos cambian entre versiones.
    """
    try:
        from metavision_hal import DeviceDiscovery       # type: ignore
    except Exception as exc:
        return f"  metavision_hal no importable: {type(exc).__name__}: {exc}"

    lineas = []
    for metodo in ("list", "list_serial_numbers"):
        funcion = getattr(DeviceDiscovery, metodo, None)
        if funcion is None:
            lineas.append(f"  DeviceDiscovery.{metodo}() · no existe en esta "
                          f"versión del SDK")
            continue
        try:
            resultado = list(funcion())
        except Exception as exc:
            lineas.append(f"  DeviceDiscovery.{metodo}() · "
                          f"{type(exc).__name__}: {exc}")
            continue
        lineas.append(f"  DeviceDiscovery.{metodo}() · {len(resultado)} "
                      f"entrada(s): {resultado}")
    try:
        camaras = list_cameras()
        if camaras:
            lineas.append("  → cámaras distintas: "
                          + ", ".join(f"{c.serial} ({c.label_hint})"
                                      for c in camaras))
        else:
            lineas.append("  → ninguna cámara de eventos conectada")
    except Exception as exc:
        lineas.append(f"  → el listado falló: {type(exc).__name__}: {exc}")
    return "\n".join(lineas)


# ---------------------------------------------------------------------------
# Apertura
# ---------------------------------------------------------------------------
def _device_from_iterator(iterator: Any) -> Any:
    """El dispositivo del HAL, si el iterador lo deja ver. Solo lee atributos.

    Son las mismas dos formas que prueba ``main_v14.1.py``. No se llama a
    ningún método: solo se leen atributos, que no puede abrir ni tocar nada.
    """
    for atributo in ("reader", "_reader"):
        try:
            lector = getattr(iterator, atributo, None)
        except Exception:
            continue
        if lector is None:
            continue
        try:
            device = getattr(lector, "device", None)
        except Exception:
            device = None
        if device is not None:
            return device
    return None


def open_camera(input_path: str = "", delta_t_us: int = 10000) -> OpenResult:
    """Abre una cámara de eventos. **Una sola llamada al SDK.**

    ``input_path`` vacío significa "la primera cámara disponible", que es lo
    que usa el programa anterior y lo que está comprobado en el PC del
    laboratorio. Con un número de serie abre esa cámara en concreto, que hace
    falta cuando hay dos conectadas.

    Si la llamada falla, la excepción del SDK sube tal cual: no se reintenta
    con otra llave dentro de esta función. Reintentar es decisión del lazo del
    worker, que espera un segundo entre intentos y solo después de haber
    soltado todo lo anterior.
    """
    from metavision_core.event_io import EventsIterator   # type: ignore

    etiqueta = f"'{input_path}'" if input_path else "'' (primera disponible)"
    llamada = f"EventsIterator(input_path={etiqueta}, delta_t={delta_t_us})"
    iterator = EventsIterator(input_path=input_path, delta_t=delta_t_us)

    resultado = OpenResult(iterator=iterator, notas=[f"{llamada} ✔"])
    device = _device_from_iterator(iterator)
    if device is not None:
        resultado.device = device
        resultado.ruta = RUTA_ITERADOR_CON_HAL
        resultado.notas.append("dispositivo del HAL accesible desde el "
                               "iterador")
    else:
        resultado.ruta = RUTA_ITERADOR_SIN_HAL
        resultado.notas.append("dispositivo del HAL NO accesible desde el "
                               "iterador (ni .reader.device ni "
                               "._reader.device)")
    return resultado
