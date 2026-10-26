#!/usr/bin/env python3
"""Banco de pruebas de la camara de eventos. Un solo archivo, sin NECTAR.

Para que existe
---------------
NECTAR se cierra con violacion de segmento al arrancar con la camara
conectada. Una violacion de segmento ocurre dentro del codigo nativo del SDK,
no en Python: no deja traza y se lleva el programa entero, asi que no se puede
depurar desde dentro de un programa con once pestanas. Hay que aislarla.

Este programa hace **solo** lo de la camara de eventos y nada mas. No importa
nada de NECTAR a proposito: asi, lo que falle aqui es de la camara o del SDK, y
no de alguna otra parte del programa grande.

Lo que permite decidir
----------------------
1. **Que llamada del SDK rompe.** El listado de camaras y la apertura se hacen
   por separado y a mano, con un boton cada uno, y hay cuatro rutas de apertura
   para elegir. Si se cierra al listar, el problema esta en la enumeracion; si
   se cierra al abrir por la ruta C y no por la A, esta en la ruta C.

2. **Si la vista va en tiempo real o se va atrasando.** Hay un medidor de
   retraso: compara el reloj de pared con la marca de tiempo del ultimo evento
   recibido. Si crece sin parar, el flujo se esta acumulando en algun lado; si
   se queda en unas decenas de milisegundos, va al dia.

3. **Que nombres tiene de verdad la API de esta version del SDK.** El boton de
   listar imprime los atributos reales de los objetos que devuelve el HAL. Eso
   es un dato que hace falta y que no se puede adivinar desde otra maquina.

El registro se escribe **tambien en un archivo**, renglon por renglon y con
vaciado inmediato. Es lo unico que sobrevive a una violacion de segmento: la
ventana desaparece y la terminal se puede perder, pero el archivo queda con la
ultima linea escrita antes del cierre, que es justo la que dice donde fue.

Uso
---
::

    cd ~/Codigos/Programas_para_github/NECTAR
    .venv/bin/python probar_evcam.py

No escribe ningun dato de medicion y no graba nada: solo mira. El unico archivo
que crea es su propio registro.
"""

from __future__ import annotations

import gc
import os
import sys
import threading
import time
import traceback
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtWidgets import (QApplication, QCheckBox, QComboBox, QFormLayout,
                             QGroupBox, QHBoxLayout, QLabel, QMainWindow,
                             QPlainTextEdit, QPushButton, QSpinBox,
                             QVBoxLayout, QWidget)

# ---------------------------------------------------------------------------
# Localizar el SDK de Metavision
# ---------------------------------------------------------------------------
# Esto hace falta y la primera version de este banco de pruebas no lo tenia,
# con lo cual no servia para nada: el SDK de Prophesee se instala **fuera** de
# pip (en Linux, en /usr/local/lib/pythonX.Y/dist-packages), y un entorno
# virtual creado sin --system-site-packages no ve esas carpetas. NECTAR
# funciona porque su modulo `sdk_locator` agrega la carpeta a sys.path al
# arrancar; este programa, por ser independiente de NECTAR a proposito, no
# heredaba ese arreglo y se encontraba con "No module named 'metavision_hal'"
# aunque el SDK estuviera instalado y la camara enchufada.
#
# Asi que la busqueda se repite aqui, en version corta y sin importar nada de
# NECTAR, para que el programa siga siendo autonomo.

_SDK_LISTO: Optional[Tuple[bool, str, Optional[str]]] = None


def _carpetas_candidatas() -> List[str]:
    """Donde se instala de verdad el SDK, por orden de probabilidad."""
    etiqueta = f"python{sys.version_info.major}.{sys.version_info.minor}"
    candidatas: List[str] = []

    # Una variable de entorno manda sobre todo lo demas, para una instalacion
    # en un sitio raro o compilada a mano.
    for variable in ("NECTAR_METAVISION_PATH", "MV_SDK_PYTHON_PATH"):
        valor = os.environ.get(variable, "").strip()
        if valor:
            candidatas.extend(p for p in valor.split(os.pathsep) if p)

    if sys.platform.startswith("win"):
        for base in (r"C:\Program Files\Prophesee",
                     r"C:\Program Files\Metavision", r"C:\Prophesee"):
            candidatas.append(os.path.join(base, "lib", "python3",
                                           "site-packages"))
            candidatas.append(os.path.join(base, "lib", "site-packages"))
    else:
        for prefijo in ("/usr/local", "/usr", "/opt/prophesee",
                        "/opt/metavision"):
            candidatas.append(os.path.join(prefijo, "lib", etiqueta,
                                           "dist-packages"))
            candidatas.append(os.path.join(prefijo, "lib", etiqueta,
                                           "site-packages"))
            candidatas.append(os.path.join(prefijo, "lib", "python3",
                                           "dist-packages"))

    unicas: List[str] = []
    for ruta in candidatas:
        if ruta not in unicas:
            unicas.append(ruta)
    return unicas


def _probar_import() -> Tuple[bool, str]:
    """Importa los dos modulos del SDK por separado, para no confundirlos.

    Separados a proposito: si se importan juntos, que falte `h5py` —que es una
    dependencia interna de `metavision_core.event_io`, no del SDK ni de este
    programa— se lee como "el SDK no esta". Ese error exacto ya costo una
    sesion de depuracion.
    """
    fallos = []
    for nombre in ("metavision_hal", "metavision_core.event_io"):
        try:
            __import__(nombre)
        except Exception as exc:
            fallos.append(f"{nombre}: {type(exc).__name__}: {exc}")
    return (not fallos), " | ".join(fallos)


def asegurar_sdk(log=None) -> Tuple[bool, str, Optional[str]]:
    """Deja el SDK importable si se puede. ``(ok, motivo, carpeta_agregada)``.

    El resultado queda guardado: se puede llamar tantas veces como se quiera.
    """
    global _SDK_LISTO
    if _SDK_LISTO is not None:
        return _SDK_LISTO

    def decir(texto: str) -> None:
        if log is not None:
            log(texto)

    ok, error = _probar_import()
    if ok:
        _SDK_LISTO = (True, "", None)
        decir("SDK de Metavision importable sin tocar nada.")
        return _SDK_LISTO

    decir(f"El SDK no se importa de entrada: {error}")
    for carpeta in _carpetas_candidatas():
        if not os.path.isdir(os.path.join(carpeta, "metavision_core")):
            continue
        decir(f"Encontre metavision_core en {carpeta}; lo agrego al camino.")
        if carpeta not in sys.path:
            # Al final, para que nada de lo que ya funciona quede tapado por
            # una carpeta del sistema.
            sys.path.append(carpeta)
        ok, error_despues = _probar_import()
        if ok:
            _SDK_LISTO = (True, "", carpeta)
            decir("Ahora si importa.")
            return _SDK_LISTO
        decir(f"Sigue sin importar: {error_despues}")
        error = error_despues or error
        break

    interprete = sys.executable
    en_venv = sys.prefix != getattr(sys, "base_prefix", sys.prefix)
    mensaje = [f"No se pudo usar el SDK de Metavision.",
               f"Interprete: {interprete}",
               f"Error: {error}"]
    if "h5py" in error:
        mensaje.append("Lo que falta es h5py, que NO es del SDK ni de este "
                       "programa: lo importa metavision_core.event_io por "
                       "dentro. Con el entorno activado: pip install h5py")
    else:
        # Este aviso va siempre, en entorno virtual o no: el interprete
        # equivocado es la causa mas probable y la mas dificil de ver, porque
        # el sintoma ("no existe metavision_hal") parece un problema del SDK o
        # de la camara y no del entorno.
        mensaje.append(
            "Revisa con que interprete corres este programa: tiene que ser "
            "el mismo con el que corre NECTAR. Un entorno virtual creado sin "
            "--system-site-packages no ve el SDK, que se instala fuera de "
            "pip.")
        if en_venv:
            mensaje.append(f"Estas en un entorno virtual ({sys.prefix}).")
        mensaje.append("Comprueba tambien que el SDK corresponda a "
                       f"python{sys.version_info.major}."
                       f"{sys.version_info.minor}.")
    _SDK_LISTO = (False, "\n".join(mensaje), None)
    decir(_SDK_LISTO[1])
    return _SDK_LISTO

# ---------------------------------------------------------------------------
# Visualizacion
# ---------------------------------------------------------------------------
# Las mismas opciones que usa el visor anterior del laboratorio
# (main_v14.1.py), para que la comparacion sea justa:
#   - row-major: la imagen se indexa [fila, columna], como los arreglos de
#     NumPy, en vez de [x, y]. Sin esto la imagen sale transpuesta.
#   - antialias apagado: suavizar bordes no tiene sentido pintando eventos
#     sueltos y cuesta tiempo.
#   - useOpenGL: manda el pintado a la tarjeta grafica.
try:
    import pyqtgraph as pg
    try:
        import OpenGL                                      # noqa: F401
        HAY_OPENGL = True
    except Exception:
        HAY_OPENGL = False
    pg.setConfigOptions(imageAxisOrder="row-major", antialias=False,
                        useOpenGL=HAY_OPENGL)
    HAY_PYQTGRAPH = True
except Exception:
    HAY_PYQTGRAPH = False
    HAY_OPENGL = False

# ---------------------------------------------------------------------------
# Pintado de los eventos
# ---------------------------------------------------------------------------
# Verde para polaridad positiva (subida de luz), rojo para negativa (bajada).
# Es el mismo criterio de color del programa anterior.
try:
    from numba import njit

    @njit(cache=True, nogil=True)
    def pintar(lienzo, xs, ys, ps):                        # pragma: no cover
        for indice in range(xs.shape[0]):
            fila = ys[indice]
            columna = xs[indice]
            if ps[indice] == 1:
                lienzo[fila, columna, 1] = 255
            else:
                lienzo[fila, columna, 0] = 255
    HAY_NUMBA = True
except Exception:
    HAY_NUMBA = False

    def pintar(lienzo, xs, ys, ps):
        """Respaldo vectorizado si numba no esta. Mas lento pero igual."""
        positivos = ps == 1
        lienzo[ys[positivos], xs[positivos], 1] = 255
        lienzo[ys[~positivos], xs[~positivos], 0] = 255


# ---------------------------------------------------------------------------
# Registro que sobrevive a un cierre brusco
# ---------------------------------------------------------------------------
class Registro:
    """Escribe en pantalla y en disco, vaciando despues de cada linea.

    El vaciado inmediato (``flush``) es el punto importante. Sin el, las
    ultimas lineas se quedan en la memoria intermedia del sistema y una
    violacion de segmento se las lleva: el archivo termina justo antes de la
    linea que interesa, que es la que dice donde ocurrio.
    """

    def __init__(self, ruta: str) -> None:
        self.ruta = ruta
        self._archivo = None
        self._cerrojo = threading.Lock()
        self._oyentes = []
        try:
            self._archivo = open(ruta, "w", encoding="utf-8")
            self._archivo.write(
                f"Banco de pruebas de la camara de eventos · "
                f"{time.strftime('%Y-%m-%d %H:%M:%S')}\n"
                f"Interprete: {sys.executable}\n"
                f"{'=' * 70}\n")
            self._archivo.flush()
        except OSError as exc:
            print(f"No se pudo abrir el registro en disco: {exc}")

    def suscribir(self, funcion) -> None:
        self._oyentes.append(funcion)

    def __call__(self, texto: str) -> None:
        marca = time.strftime("%H:%M:%S")
        linea = f"[{marca}] {texto}"
        with self._cerrojo:
            print(linea, flush=True)
            if self._archivo is not None:
                try:
                    self._archivo.write(linea + "\n")
                    self._archivo.flush()
                    os.fsync(self._archivo.fileno())
                except OSError:
                    pass
        for funcion in self._oyentes:
            try:
                funcion(linea)
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Rutas de apertura
# ---------------------------------------------------------------------------
#: (clave, texto para el menu). El orden es de menos invasiva a mas.
RUTAS = [
    ("A", "A · iterador directo, primera disponible  (igual que main_v14.1)"),
    ("B", "B · iterador directo con numero de serie"),
    ("C", "C · dispositivo del HAL (serie) + from_device"),
    ("D", "D · dispositivo del HAL (primera) + from_device"),
    ("E", "E · initiate_device (primera) + from_device  (la de Prophesee)"),
]

FACILIDADES = (
    ("get_i_events_stream", "grabacion del .raw"),
    ("get_i_ll_biases", "biases"),
    ("get_i_camera_synchronization", "sincronismo maestro/esclavo"),
    ("get_i_roi", "recorte en el sensor"),
    ("get_i_hw_identification", "identificacion del equipo"),
    ("get_i_geometry", "geometria del sensor"),
)


def listar_camaras(log: Registro) -> List[str]:
    """Prueba cada forma de listar del HAL e informa TODO lo que devuelve.

    Imprime tambien los atributos reales de los objetos ``CameraDescription``.
    Ese dato es el que falta para escribir bien el codigo de NECTAR: los
    nombres de esos campos cambian entre versiones del SDK y no se pueden
    adivinar desde otra maquina.
    """
    seriales: List[str] = []
    listo, motivo, _carpeta = asegurar_sdk(log)
    if not listo:
        for linea in motivo.splitlines():
            log(f"  {linea}")
        return seriales
    try:
        from metavision_hal import DeviceDiscovery         # type: ignore
    except Exception as exc:
        log(f"metavision_hal no se puede importar: {type(exc).__name__}: {exc}")
        return seriales

    for metodo in ("list", "list_serial_numbers", "list_available_sources",
                   "list_available_sources_local"):
        funcion = getattr(DeviceDiscovery, metodo, None)
        if funcion is None:
            log(f"  DeviceDiscovery.{metodo}() · no existe en esta version")
            continue
        log(f"  llamando a DeviceDiscovery.{metodo}() ...")
        try:
            resultado = list(funcion())
        except Exception as exc:
            log(f"  DeviceDiscovery.{metodo}() · ERROR "
                f"{type(exc).__name__}: {exc}")
            continue
        log(f"  DeviceDiscovery.{metodo}() · {len(resultado)} entrada(s)")
        for indice, elemento in enumerate(resultado):
            if isinstance(elemento, str):
                log(f"      [{indice}] cadena: {elemento!r}")
                partes = elemento.split(":")
                if partes:
                    seriales.append(partes[-1].strip())
                continue
            # Objeto: hay que averiguar como se llaman sus campos.
            log(f"      [{indice}] objeto {type(elemento).__name__}")
            campos = [nombre for nombre in dir(elemento)
                      if not nombre.startswith("_")]
            log(f"           atributos: {campos}")
            for nombre in campos:
                try:
                    valor = getattr(elemento, nombre)
                    if callable(valor):
                        valor = valor()
                    log(f"           {nombre} = {valor!r}")
                    if nombre in ("serial", "serial_number") and valor:
                        seriales.append(str(valor).strip())
                except Exception as exc:
                    log(f"           {nombre} ✖ {type(exc).__name__}: {exc}")

    # Sin duplicados, conservando el orden.
    unicos: List[str] = []
    for serial in seriales:
        if serial and serial not in unicos:
            unicos.append(serial)
    log(f"  numeros de serie distintos encontrados: {unicos}")
    return unicos


def abrir_camara(ruta: str, serial: str, delta_t_us: int,
                 log: Registro) -> Tuple[Any, Any]:
    """Abre la camara por la ruta pedida. Devuelve ``(iterador, dispositivo)``.

    Cada llamada al SDK se anuncia en el registro **antes** de hacerla. Si el
    programa se cierra de golpe, la ultima linea del archivo dice exactamente
    cual fue la llamada que lo tumbo. Es la unica forma de ubicar una violacion
    de segmento sin un depurador.
    """
    listo, motivo, _carpeta = asegurar_sdk(log)
    if not listo:
        raise RuntimeError(motivo)
    from metavision_core.event_io import EventsIterator     # type: ignore

    dispositivo = None

    if ruta in ("C", "D", "E"):
        if ruta == "E":
            # La forma que documenta Prophesee para tener el dispositivo y
            # grabar: initiate_device abre la camara UNA vez y devuelve el
            # dispositivo del HAL; from_device construye el iterador encima,
            # sin volver a abrirla.
            from metavision_core.event_io.raw_reader import (  # type: ignore
                initiate_device)
            log("  -> initiate_device(path='')  (primera disponible)")
            dispositivo = initiate_device(path="")
            log(f"  <- devolvio {type(dispositivo).__name__}")
        else:
            from metavision_hal import DeviceDiscovery      # type: ignore
            llave = serial if ruta == "C" else ""
            etiqueta = f"'{llave}'" if llave else "'' (primera disponible)"
            log(f"  -> DeviceDiscovery.open({etiqueta})")
            dispositivo = DeviceDiscovery.open(llave)
            log(f"  <- devolvio {type(dispositivo).__name__}")
        if dispositivo is None:
            raise RuntimeError("la apertura del dispositivo devolvio None")

        constructor = getattr(EventsIterator, "from_device", None)
        if constructor is None:
            raise RuntimeError("EventsIterator.from_device no existe en esta "
                               "version del SDK")
        log(f"  -> EventsIterator.from_device(device=..., "
            f"delta_t={delta_t_us})")
        iterador = constructor(device=dispositivo, delta_t=delta_t_us)
        log("  <- iterador construido sobre el dispositivo")
        return iterador, dispositivo

    llave = serial if ruta == "B" else ""
    etiqueta = f"'{llave}'" if llave else "'' (primera disponible)"
    log(f"  -> EventsIterator(input_path={etiqueta}, delta_t={delta_t_us})")
    iterador = EventsIterator(input_path=llave, delta_t=delta_t_us)
    log("  <- iterador construido")

    # Se mira si el dispositivo asoma por dentro del iterador, sin depender de
    # que este: en esta version del SDK no esta, y es parte de lo que hay que
    # confirmar.
    for atributo in ("reader", "_reader"):
        lector = getattr(iterador, atributo, None)
        if lector is None:
            continue
        candidato = getattr(lector, "device", None)
        if candidato is not None:
            log(f"  dispositivo del HAL encontrado en "
                f"iterador.{atributo}.device")
            dispositivo = candidato
            break
    if dispositivo is None:
        log("  dispositivo del HAL NO accesible desde el iterador "
            "(ni .reader.device ni ._reader.device)")
    return iterador, dispositivo


def informar_facilidades(dispositivo: Any, log: Registro) -> None:
    """Dice que facilidades del HAL responden en esta camara."""
    if dispositivo is None:
        log("  sin dispositivo del HAL: no hay biases, ni sincronismo, ni "
            "recorte en el sensor, ni grabacion del .raw")
        return
    for getter, para_que in FACILIDADES:
        funcion = getattr(dispositivo, getter, None)
        if funcion is None:
            log(f"  {getter:34} no existe            ({para_que})")
            continue
        log(f"  -> {getter}()")
        try:
            objeto = funcion()
        except Exception as exc:
            log(f"  {getter:34} ERROR {type(exc).__name__}: {exc}")
            continue
        estado = type(objeto).__name__ if objeto is not None else "None"
        log(f"  {getter:34} {estado:20} ({para_que})")
        if getter == "get_i_hw_identification" and objeto is not None:
            for lectura in ("get_serial", "get_integrator", "get_sensor_info"):
                try:
                    log(f"       {lectura}() = "
                        f"{getattr(objeto, lectura)()!r}")
                except Exception as exc:
                    log(f"       {lectura}() ✖ {exc}")


# ---------------------------------------------------------------------------
# Hilo de adquisicion
# ---------------------------------------------------------------------------
class Adquisicion:
    """Lee la camara en su propio hilo y deja **un solo** cuadro a la vista.

    Aqui esta la decision que importa para el tiempo real. Lo natural seria
    emitir una senal de Qt por cada cuadro, pero las senales entre hilos se
    encolan: si la interfaz pinta mas lento de lo que el hilo produce, la cola
    crece y lo que se ve en pantalla es cada vez mas viejo. El retraso no se
    nota al principio y despues ya es de segundos.

    Asi que no hay cola. El hilo escribe siempre en el **mismo** espacio
    (``_cuadro``) y la interfaz, con su propio reloj, se lleva lo que haya en
    ese momento. Si la interfaz va lenta, se pierden cuadros intermedios, que
    es exactamente lo que se quiere: ver lo ultimo, no ver todo con atraso.

    El retraso se mide de verdad y no se supone: cada evento trae su marca de
    tiempo ``t`` en microsegundos desde que arranco el flujo. Comparando esa
    marca con el reloj de pared desde el primer evento se sabe cuanto esta
    atrasado lo que se recibe. Si ese numero crece sin parar, el flujo se esta
    acumulando dentro del SDK o del USB.
    """

    def __init__(self, log: Registro) -> None:
        self.log = log
        self.ancho = 640
        self.alto = 480

        self._hilo: Optional[threading.Thread] = None
        self._parar = threading.Event()
        self._cerrojo = threading.Lock()
        self._cuadro: Optional[np.ndarray] = None
        self._cuadro_nuevo = False
        self._estado: Dict[str, Any] = {}
        self._iterador = None
        self._dispositivo = None

        self.delta_t_us = 10000
        self.fps_pantalla = 60
        self.maximo_eventos_pintados = 10000

        #: Prueba de grabacion pedida desde la interfaz. Se atiende DENTRO del
        #: hilo de adquisicion, igual que en main_v14.1: log_raw_data se llama
        #: desde el mismo hilo que itera la camara.
        self._pedido_grabacion = threading.Event()
        self.carpeta_grabacion = os.path.dirname(os.path.abspath(__file__))
        self.segundos_grabacion = 5.0

    def pedir_grabacion(self) -> None:
        self._pedido_grabacion.set()

    # ------------------------------------------------------------------
    def arrancar(self, ruta: str, serial: str) -> None:
        if self._hilo is not None and self._hilo.is_alive():
            self.log("Ya hay una adquisicion corriendo.")
            return
        self._parar.clear()
        self._hilo = threading.Thread(
            target=self._correr, args=(ruta, serial), daemon=True,
            name="adquisicion-evcam")
        self._hilo.start()

    def detener(self) -> None:
        self._parar.set()
        hilo = self._hilo
        if hilo is not None:
            # Se espera de verdad: soltar la camara mientras el hilo todavia
            # itera sobre ella es una receta para una violacion de segmento.
            hilo.join(timeout=5.0)
            if hilo.is_alive():
                self.log("AVISO: el hilo de adquisicion no termino en 5 s. "
                         "No se suelta la camara para no cerrarla mientras "
                         "alguien la esta leyendo.")
                return
        self._hilo = None
        self._soltar()

    def _soltar(self) -> None:
        if self._iterador is None and self._dispositivo is None:
            return          # nada que soltar: no se anuncia un cierre falso
        # Solo se sueltan las referencias, en este orden: primero el iterador
        # (que lee del dispositivo) y despues el dispositivo. No se llama a
        # ningun metodo de cierre "por si acaso": llamar metodos de objetos
        # nativos que no sabemos si existen es exactamente lo que se quiere
        # evitar. Es lo que hacen main_v14.1 y los ejemplos de Prophesee: dejar
        # que el objeto deje de existir y que el SDK libere la camara.
        self.log("  -> soltando el iterador")
        self._iterador = None
        gc.collect()
        if self._dispositivo is not None:
            self.log("  -> soltando el dispositivo del HAL")
            self._dispositivo = None
            gc.collect()
        self.log("Camara soltada.")

    # ------------------------------------------------------------------
    def tomar_cuadro(self) -> Optional[np.ndarray]:
        """Se lleva el ultimo cuadro, si hay uno nuevo. No bloquea al hilo."""
        with self._cerrojo:
            if not self._cuadro_nuevo:
                return None
            self._cuadro_nuevo = False
            return self._cuadro

    def estado(self) -> Dict[str, Any]:
        with self._cerrojo:
            return dict(self._estado)

    # ------------------------------------------------------------------
    def _correr(self, ruta: str, serial: str) -> None:
        nombre = dict(RUTAS).get(ruta, ruta)
        self.log(f"Abriendo por la ruta {nombre}")
        try:
            iterador, dispositivo = abrir_camara(ruta, serial,
                                                 self.delta_t_us, self.log)
        except Exception as exc:
            self.log(f"NO se pudo abrir: {type(exc).__name__}: {exc}")
            for linea in traceback.format_exc().splitlines():
                self.log(f"    {linea}")
            with self._cerrojo:
                self._estado = {"error": f"{type(exc).__name__}: {exc}"}
            return

        self._iterador = iterador
        self._dispositivo = dispositivo

        try:
            geometria = iterador.get_size()
            self.alto, self.ancho = int(geometria[0]), int(geometria[1])
            self.log(f"Geometria del sensor: {self.ancho}x{self.alto}")
        except Exception as exc:
            self.log(f"get_size() fallo ({exc}); se asume "
                     f"{self.ancho}x{self.alto}")

        informar_facilidades(dispositivo, self.log)
        self.log("Leyendo eventos. La vista deberia moverse ya.")

        lienzo = np.zeros((self.alto, self.ancho, 3), dtype=np.uint8)
        periodo_pintado = 1.0 / max(1, self.fps_pantalla)

        eventos_total = 0
        eventos_ventana = 0
        cuadros_pintados = 0
        t_primer_evento_us: Optional[int] = None
        reloj_primer_evento = 0.0
        ultimo_pintado = 0.0
        ventana_inicio = time.perf_counter()
        retraso_ms = 0.0
        lotes_vacios = 0

        # Estado de la prueba de grabacion.
        canal = None              # i_events_stream, si la camara lo expone
        archivo_grabacion = ""
        fin_grabacion = 0.0
        eventos_grabados = 0
        self._pedido_grabacion.clear()

        try:
            for eventos in iterador:
                if self._parar.is_set():
                    break
                ahora = time.perf_counter()
                cantidad = 0 if eventos is None else int(len(eventos))
                eventos_total += cantidad
                eventos_ventana += cantidad

                # --- prueba de grabacion: se atiende aqui, en este hilo --
                if self._pedido_grabacion.is_set() and not archivo_grabacion:
                    self._pedido_grabacion.clear()
                    canal, archivo_grabacion = self._empezar_grabacion(
                        dispositivo)
                    if archivo_grabacion:
                        fin_grabacion = ahora + self.segundos_grabacion
                        eventos_grabados = 0
                if archivo_grabacion:
                    eventos_grabados += cantidad
                    if ahora >= fin_grabacion:
                        self._terminar_grabacion(canal, archivo_grabacion,
                                                 eventos_grabados)
                        canal, archivo_grabacion = None, ""

                # --- retraso real, medido con las marcas de tiempo ------
                if cantidad:
                    lotes_vacios = 0
                    t_ultimo_us = int(eventos["t"][-1])
                    if t_primer_evento_us is None:
                        t_primer_evento_us = t_ultimo_us
                        reloj_primer_evento = ahora
                    transcurrido_pared_ms = (ahora - reloj_primer_evento) * 1e3
                    transcurrido_camara_ms = (
                        (t_ultimo_us - t_primer_evento_us) / 1e3)
                    retraso_ms = transcurrido_pared_ms - transcurrido_camara_ms
                else:
                    lotes_vacios += 1

                # --- pintado: solo cuando toca, y siempre el ultimo -----
                if cantidad and (ahora - ultimo_pintado) >= periodo_pintado:
                    ultimo_pintado = ahora
                    paso = max(1, cantidad // self.maximo_eventos_pintados)
                    muestra = eventos[::paso]
                    if lienzo.shape[:2] != (self.alto, self.ancho):
                        lienzo = np.zeros((self.alto, self.ancho, 3),
                                          dtype=np.uint8)
                    lienzo.fill(0)
                    if muestra.size:
                        pintar(lienzo, muestra["x"].astype(np.int64),
                               muestra["y"].astype(np.int64),
                               muestra["p"].astype(np.int64))
                    cuadros_pintados += 1
                    with self._cerrojo:
                        # copy(): el lienzo se reutiliza en el proximo cuadro y
                        # la interfaz no puede quedarse mirando memoria que
                        # este hilo esta a punto de borrar.
                        self._cuadro = lienzo.copy()
                        self._cuadro_nuevo = True

                # --- telemetria cada medio segundo ----------------------
                lapso = ahora - ventana_inicio
                if lapso >= 0.5:
                    with self._cerrojo:
                        self._estado = {
                            "mev_s": eventos_ventana / lapso / 1e6,
                            "eventos_total": eventos_total,
                            "fps_pintados": cuadros_pintados / lapso,
                            "retraso_ms": retraso_ms,
                            "lotes_vacios": lotes_vacios,
                            "ancho": self.ancho,
                            "alto": self.alto,
                        }
                    eventos_ventana = 0
                    cuadros_pintados = 0
                    ventana_inicio = ahora
        except Exception as exc:
            self.log(f"El lazo de lectura se corto: {type(exc).__name__}: "
                     f"{exc}")
            for linea in traceback.format_exc().splitlines():
                self.log(f"    {linea}")
        finally:
            if archivo_grabacion:
                # Se corto el lazo a mitad de una grabacion: se cierra igual,
                # para que el archivo quede valido.
                self._terminar_grabacion(canal, archivo_grabacion,
                                         eventos_grabados)
            self.log(f"Lazo terminado. {eventos_total:,} eventos en total.")

    # ------------------------------------------------------------------
    def _empezar_grabacion(self, dispositivo) -> Tuple[Any, str]:
        """Pide al HAL que escriba el .raw. Devuelve ``(canal, ruta)``.

        Es la misma llamada que usa main_v14.1 para grabar:
        ``device.get_i_events_stream().log_raw_data(ruta)``. Si esta prueba
        deja un archivo con datos, grabar funciona por esta ruta de apertura.
        """
        if dispositivo is None:
            self.log("GRABACION: no hay dispositivo del HAL con esta ruta de "
                     "apertura, asi que no hay canal para escribir el .raw. "
                     "Prueba la ruta E.")
            return None, ""
        try:
            self.log("GRABACION: -> dispositivo.get_i_events_stream()")
            canal = dispositivo.get_i_events_stream()
        except Exception as exc:
            self.log(f"GRABACION: get_i_events_stream() fallo: "
                     f"{type(exc).__name__}: {exc}")
            return None, ""
        if canal is None:
            self.log("GRABACION: get_i_events_stream() devolvio None: esta "
                     "camara no expone el canal de grabacion.")
            return None, ""
        nombre = f"prueba_grabacion_{time.strftime('%Y%m%d_%H%M%S')}.raw"
        ruta = os.path.join(self.carpeta_grabacion, nombre)
        try:
            self.log(f"GRABACION: -> log_raw_data('{ruta}')")
            canal.log_raw_data(ruta)
        except Exception as exc:
            self.log(f"GRABACION: log_raw_data() fallo: "
                     f"{type(exc).__name__}: {exc}")
            return None, ""
        self.log(f"GRABACION: escribiendo {self.segundos_grabacion:.0f} s ...")
        return canal, ruta

    def _terminar_grabacion(self, canal, ruta: str, eventos: int) -> None:
        try:
            self.log("GRABACION: -> stop_log_raw_data()")
            canal.stop_log_raw_data()
        except Exception as exc:
            self.log(f"GRABACION: stop_log_raw_data() fallo: "
                     f"{type(exc).__name__}: {exc}")
        try:
            tamano = os.path.getsize(ruta)
        except OSError:
            tamano = -1
        if tamano > 0:
            self.log(f"GRABACION OK: {os.path.basename(ruta)} · "
                     f"{tamano / 1e6:.2f} MB · {eventos:,} eventos vistos "
                     f"mientras grababa. Grabar FUNCIONA por esta ruta.")
        elif tamano == 0:
            self.log(f"GRABACION: el archivo {os.path.basename(ruta)} quedo "
                     f"VACIO (0 bytes). El canal existe pero no escribio.")
        else:
            self.log(f"GRABACION: el archivo {os.path.basename(ruta)} NO se "
                     f"creo.")


# ---------------------------------------------------------------------------
# Ventana
# ---------------------------------------------------------------------------
class Ventana(QMainWindow):

    def __init__(self, log: Registro) -> None:
        super().__init__()
        self.log = log
        self.adquisicion = Adquisicion(log)
        self.setWindowTitle("Banco de pruebas · camara de eventos")
        self.resize(1180, 680)

        central = QWidget()
        self.setCentralWidget(central)
        columnas = QHBoxLayout(central)

        # --- vista -----------------------------------------------------
        if HAY_PYQTGRAPH:
            self._graficos = pg.GraphicsLayoutWidget()
            self._graficos.setBackground("#000000")
            vista = self._graficos.addViewBox()
            vista.setAspectLocked(True)
            vista.invertY(True)
            vista.setMenuEnabled(False)
            self._imagen = pg.ImageItem(
                np.zeros((480, 640, 3), dtype=np.uint8))
            vista.addItem(self._imagen)
            columnas.addWidget(self._graficos, stretch=3)
        else:
            self._imagen = None
            columnas.addWidget(QLabel("pyqtgraph no esta instalado: no hay "
                                      "vista previa."), stretch=3)

        # --- panel -----------------------------------------------------
        panel = QVBoxLayout()
        columnas.addLayout(panel, stretch=2)

        caja_camara = QGroupBox("Camara")
        formulario = QFormLayout(caja_camara)

        self.boton_listar = QPushButton("Listar camaras del HAL")
        self.boton_listar.clicked.connect(self._listar)
        formulario.addRow(self.boton_listar)

        self.combo_serial = QComboBox()
        self.combo_serial.setEditable(True)
        self.combo_serial.addItem("")
        formulario.addRow("Numero de serie", self.combo_serial)

        self.combo_ruta = QComboBox()
        for _clave, texto in RUTAS:
            self.combo_ruta.addItem(texto)
        formulario.addRow("Ruta de apertura", self.combo_ruta)

        self.campo_delta_t = QSpinBox()
        self.campo_delta_t.setRange(200, 100000)
        self.campo_delta_t.setSingleStep(1000)
        self.campo_delta_t.setValue(10000)
        self.campo_delta_t.setSuffix(" us")
        formulario.addRow("Ventana de integracion", self.campo_delta_t)

        self.campo_fps = QSpinBox()
        self.campo_fps.setRange(1, 200)
        self.campo_fps.setValue(60)
        self.campo_fps.setSuffix(" cuadros/s")
        formulario.addRow("Pintado de pantalla", self.campo_fps)

        self.casilla_opengl = QCheckBox("pintar con OpenGL (GPU)")
        self.casilla_opengl.setChecked(HAY_OPENGL)
        self.casilla_opengl.setEnabled(False)
        self.casilla_opengl.setToolTip(
            "Se decide al arrancar el programa, porque pyqtgraph lo lee una "
            "sola vez. Si esta apagado, falta PyOpenGL.")
        formulario.addRow(self.casilla_opengl)

        panel.addWidget(caja_camara)

        botones = QHBoxLayout()
        self.boton_conectar = QPushButton("CONECTAR")
        self.boton_conectar.clicked.connect(self._conectar)
        self.boton_desconectar = QPushButton("DESCONECTAR")
        self.boton_desconectar.clicked.connect(self._desconectar)
        self.boton_desconectar.setEnabled(False)
        botones.addWidget(self.boton_conectar)
        botones.addWidget(self.boton_desconectar)
        panel.addLayout(botones)

        # Prueba de grabacion: escribe 5 s de .raw en la carpeta de este
        # programa (los .raw estan en .gitignore, no se suben a GitHub) y dice
        # cuanto peso el archivo. Es la forma directa de saber si grabar
        # funciona con la ruta de apertura elegida.
        self.boton_grabar = QPushButton("PROBAR GRABACION (5 s)")
        self.boton_grabar.setEnabled(False)
        self.boton_grabar.clicked.connect(self._probar_grabacion)
        panel.addWidget(self.boton_grabar)

        caja_estado = QGroupBox("Como va")
        vertical = QVBoxLayout(caja_estado)
        # El interprete a la vista: correr este programa con un entorno
        # distinto del de NECTAR es un error facil de cometer y muy difícil de
        # ver, porque el sintoma ("no existe metavision_hal") parece un
        # problema del SDK.
        self.etiqueta_interprete = QLabel(f"interprete: {sys.executable}")
        self.etiqueta_interprete.setWordWrap(True)
        self.etiqueta_sdk = QLabel("SDK de Metavision: comprobando ...")
        self.etiqueta_sdk.setWordWrap(True)
        vertical.addWidget(self.etiqueta_interprete)
        vertical.addWidget(self.etiqueta_sdk)
        self.etiqueta_tasa = QLabel("—")
        self.etiqueta_retraso = QLabel("—")
        self.etiqueta_retraso.setWordWrap(True)
        vertical.addWidget(self.etiqueta_tasa)
        vertical.addWidget(self.etiqueta_retraso)
        panel.addWidget(caja_estado)

        self.consola = QPlainTextEdit()
        self.consola.setReadOnly(True)
        self.consola.setMaximumBlockCount(4000)
        panel.addWidget(self.consola, stretch=1)

        panel.addWidget(QLabel(f"Registro en disco:\n{log.ruta}"))

        # El registro llega a la consola por el reloj de la interfaz y no
        # directamente, porque las lineas se escriben desde el hilo de
        # adquisicion y tocar un widget de Qt desde otro hilo es exactamente
        # la clase de cosa que hace caer un programa con una violacion de
        # segmento. Van a una lista y la interfaz las recoge.
        self._pendientes: List[str] = []
        self._cerrojo_pendientes = threading.Lock()
        log.suscribir(self._encolar_linea)

        self._reloj = QTimer(self)
        self._reloj.timeout.connect(self._refrescar)
        self._reloj.start(16)        # ~60 veces por segundo

        log(f"pyqtgraph: {'si' if HAY_PYQTGRAPH else 'NO'} · "
            f"OpenGL: {'si' if HAY_OPENGL else 'NO'} · "
            f"numba: {'si' if HAY_NUMBA else 'NO (respaldo vectorizado)'}")

        # El SDK se busca ahora, al arrancar, y no al pulsar un boton: que el
        # entorno no vea el SDK es un problema del entorno, no de la camara, y
        # tiene que verse antes de empezar a sacar conclusiones sobre el
        # hardware.
        listo, motivo, carpeta = asegurar_sdk(log)
        if listo:
            donde = f" (camino agregado: {carpeta})" if carpeta else ""
            self.etiqueta_sdk.setText(f"SDK de Metavision: disponible{donde}")
            log("Orden sugerido: (1) 'Listar camaras del HAL'; (2) CONECTAR "
                "con la ruta A, que es la de main_v14.1 y la de NECTAR 1.0.2; "
                "(3) DESCONECTAR y CONECTAR con la ruta E, y pulsar 'PROBAR "
                "GRABACION (5 s)'. Las rutas C y D son las que usaba la 1.0.1 "
                "y no hace falta probarlas.")
        else:
            self.etiqueta_sdk.setText("SDK de Metavision: NO DISPONIBLE · "
                                      "mira el registro")
            self.boton_conectar.setEnabled(False)
            self.boton_listar.setEnabled(False)
            for linea in motivo.splitlines():
                log(f"  {linea}")

    # ------------------------------------------------------------------
    def _encolar_linea(self, linea: str) -> None:
        with self._cerrojo_pendientes:
            self._pendientes.append(linea)

    def _listar(self) -> None:
        self.log("=" * 60)
        self.log("Listando camaras del HAL. Si el programa se cierra aqui, "
                 "el problema esta en la enumeracion.")
        try:
            seriales = listar_camaras(self.log)
        except Exception as exc:
            self.log(f"El listado fallo: {type(exc).__name__}: {exc}")
            return
        actual = self.combo_serial.currentText()
        self.combo_serial.clear()
        self.combo_serial.addItem("")
        for serial in seriales:
            self.combo_serial.addItem(serial)
        if actual:
            self.combo_serial.setCurrentText(actual)
        elif seriales:
            self.combo_serial.setCurrentText(seriales[0])

    def _conectar(self) -> None:
        clave = RUTAS[self.combo_ruta.currentIndex()][0]
        serial = self.combo_serial.currentText().strip()
        if clave in ("B", "C") and not serial:
            self.log("Las rutas B y C necesitan un numero de serie. Pulsa "
                     "'Listar camaras del HAL' o escribelo a mano.")
            return
        self.adquisicion.delta_t_us = self.campo_delta_t.value()
        self.adquisicion.fps_pantalla = self.campo_fps.value()
        self.log("=" * 60)
        self.adquisicion.arrancar(clave, serial)
        self.boton_conectar.setEnabled(False)
        self.boton_desconectar.setEnabled(True)
        self.boton_listar.setEnabled(False)
        self.boton_grabar.setEnabled(True)

    def _desconectar(self) -> None:
        self.log("Desconectando ...")
        self.adquisicion.detener()
        self.boton_conectar.setEnabled(True)
        self.boton_desconectar.setEnabled(False)
        self.boton_listar.setEnabled(True)
        self.boton_grabar.setEnabled(False)

    def _probar_grabacion(self) -> None:
        self.log("=" * 60)
        self.log("Prueba de grabacion pedida.")
        self.adquisicion.pedir_grabacion()

    # ------------------------------------------------------------------
    def _refrescar(self) -> None:
        with self._cerrojo_pendientes:
            lineas, self._pendientes = self._pendientes, []
        for linea in lineas:
            self.consola.appendPlainText(linea)

        cuadro = self.adquisicion.tomar_cuadro()
        if cuadro is not None and self._imagen is not None:
            self._imagen.setImage(cuadro, autoLevels=False)

        estado = self.adquisicion.estado()
        if not estado:
            return
        if "error" in estado:
            self.etiqueta_tasa.setText(f"error: {estado['error']}")
            return

        self.etiqueta_tasa.setText(
            f"{estado['mev_s']:.3f} MEv/s · "
            f"{estado['eventos_total']:,} eventos · "
            f"{estado['fps_pintados']:.0f} cuadros/s pintados · "
            f"sensor {estado['ancho']}x{estado['alto']}")

        # El signo importa y dice dos cosas distintas:
        #   positivo = el reloj de la camara viene por detras del de pared,
        #              o sea los eventos llegan tarde: hay atraso real.
        #   negativo = los lotes llegan mas rapido que su propio tiempo, que
        #              es lo que pasa cuando el SDK suelta de golpe lo que
        #              tenia guardado: se esta poniendo al dia tras un atasco.
        # Los dos casos son el mismo problema visto en momentos distintos, y
        # ninguno de los dos se puede ver a ojo en la pantalla.
        retraso = estado["retraso_ms"]
        magnitud = abs(retraso)
        if magnitud < 150:
            juicio = "va al dia"
        elif retraso > 0:
            juicio = ("ATRASO: los eventos llegan tarde, el flujo se esta "
                      "acumulando")
        else:
            juicio = ("lotes guardados entregados de golpe: viene de un "
                      "atasco y se esta poniendo al dia")
        self.etiqueta_retraso.setText(
            f"desfase entre el reloj de la camara y el de pared: "
            f"{retraso:+.0f} ms\n{juicio}\n"
            f"lotes vacios seguidos: {estado['lotes_vacios']}")

    def closeEvent(self, evento):                          # noqa: N802
        """Soltar la camara ANTES de que la ventana muera.

        Si el proceso termina con el hilo de adquisicion todavia iterando
        sobre la camara, el SDK libera sus objetos nativos por debajo de un
        hilo que sigue usandolos. Eso es, otra vez, una violacion de
        segmento — esta vez al cerrar en vez de al abrir.
        """
        try:
            self.adquisicion.detener()
        except Exception as exc:
            self.log(f"Error al cerrar: {exc}")
        super().closeEvent(evento)


def main() -> int:
    ruta_registro = os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        "probar_evcam_registro.txt")
    log = Registro(ruta_registro)
    aplicacion = QApplication(sys.argv)
    ventana = Ventana(log)
    ventana.show()
    return aplicacion.exec()


if __name__ == "__main__":
    sys.exit(main())
