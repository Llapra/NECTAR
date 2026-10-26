#!/usr/bin/env python3
"""Diagnóstico de la cámara de eventos: dónde se rompe la cadena.

No modifica NECTAR ni toca la configuración de la cámara. Solo mira, en el
mismo orden en que lo hace el programa, y dice en qué eslabón se corta:

  1. qué intérprete de Python se está usando y si es un entorno virtual;
  2. si el SDK de Metavision se puede importar, y desde dónde;
  3. qué dice la variable MV_HAL_PLUGIN_PATH (el HAL carga los plugins de
     cada familia de sensor desde ahí; si falta, el SDK importa pero no ve
     ninguna cámara);
  4. qué devuelven las distintas formas de listar cámaras del HAL;
  5. **si la cámara se puede abrir de verdad** y entregar eventos — que es la
     prueba que de verdad importa, porque es lo que hace el programa viejo;
  6. qué facilidades del HAL expone esta cámara (biases, sincronismo,
     grabación cruda, ROI por hardware);
  7. si pyqtgraph puede usar OpenGL para la visualización.

Uso:

    cd ~/Codigos/Programas_para_github/NECTAR
    python3 diagnostico_evcam.py

Y si usas el entorno virtual, con el intérprete del entorno:

    .venv/bin/python diagnostico_evcam.py
"""

from __future__ import annotations

import os
import sys
import time


def titulo(texto: str) -> None:
    print(f"\n{'=' * 64}\n{texto}\n{'=' * 64}")


def dato(etiqueta: str, valor) -> None:
    print(f"  {etiqueta:.<34} {valor}")


# ---------------------------------------------------------------------------
titulo("1 · INTÉRPRETE")
dato("ejecutable", sys.executable)
dato("versión", sys.version.split()[0])
en_venv = sys.prefix != getattr(sys, "base_prefix", sys.prefix)
dato("¿entorno virtual?", "sí" if en_venv else "no")
if en_venv:
    dato("prefijo del entorno", sys.prefix)
    dato("prefijo del sistema", getattr(sys, "base_prefix", "?"))
    print("\n  Aviso: un entorno virtual creado sin --system-site-packages no\n"
          "  ve el SDK de Metavision, que se instala fuera de pip.")


# ---------------------------------------------------------------------------
titulo("2 · SDK DE METAVISION")
try:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from nectar.devices.event_cam import sdk_locator
    ok, error, agregada = sdk_locator.ensure_on_path()
    dato("localizador de NECTAR", "importable")
    if agregada:
        dato("carpeta agregada al camino", agregada)
except Exception as exc:
    print(f"  No se pudo usar el localizador de NECTAR: "
          f"{type(exc).__name__}: {exc}")

modulos = {}
for nombre in ("metavision_core", "metavision_core.event_io", "metavision_hal",
               "metavision_sdk_core", "metavision_sdk_base"):
    try:
        modulo = __import__(nombre, fromlist=["__file__"])
        modulos[nombre] = getattr(modulo, "__file__", "(sin archivo)")
        dato(nombre, "✔")
    except Exception as exc:
        modulos[nombre] = None
        dato(nombre, f"✖ {type(exc).__name__}: {exc}")

for nombre in ("metavision_core", "metavision_hal"):
    if modulos.get(nombre):
        print(f"\n  {nombre} cargado desde:\n    {modulos[nombre]}")

HAY_SDK = modulos.get("metavision_core.event_io") and modulos.get("metavision_hal")
if not HAY_SDK:
    print("\n  Sin el SDK no se puede seguir. Las pruebas 4 a 6 se omiten.")


# ---------------------------------------------------------------------------
titulo("3 · VARIABLES DE ENTORNO DEL HAL")
for variable in ("MV_HAL_PLUGIN_PATH", "MV_SDK_PATH", "METAVISION_PLUGIN_PATH",
                 "LD_LIBRARY_PATH", "PYTHONPATH"):
    valor = os.environ.get(variable)
    dato(variable, valor if valor else "(sin definir)")

rutas_plugin = [
    "/usr/local/lib/metavision/hal/plugins",
    "/usr/lib/metavision/hal/plugins",
    "/usr/local/lib/hal/plugins",
]
print("\n  Carpetas de plugins del HAL que existen en el sistema:")
alguna = False
for ruta in rutas_plugin:
    if os.path.isdir(ruta):
        alguna = True
        try:
            contenido = sorted(os.listdir(ruta))
        except OSError as exc:
            contenido = [f"(no se pudo listar: {exc})"]
        print(f"    ✔ {ruta}")
        for nombre in contenido[:12]:
            print(f"        {nombre}")
if not alguna:
    print("    (ninguna de las rutas habituales existe)")


# ---------------------------------------------------------------------------
titulo("4 · LISTADO DE CÁMARAS SEGÚN EL HAL")
seriales = []
if HAY_SDK:
    from metavision_hal import DeviceDiscovery           # type: ignore
    for metodo in ("list_serial_numbers", "list", "list_available_sources",
                   "list_available_sources_local"):
        funcion = getattr(DeviceDiscovery, metodo, None)
        if funcion is None:
            dato(f"DeviceDiscovery.{metodo}()", "(no existe en esta versión)")
            continue
        try:
            resultado = list(funcion())
            dato(f"DeviceDiscovery.{metodo}()",
                 resultado if resultado else "[]  ← lista VACÍA")
            if metodo == "list_serial_numbers":
                seriales = resultado
        except Exception as exc:
            dato(f"DeviceDiscovery.{metodo}()",
                 f"✖ {type(exc).__name__}: {exc}")

    print("\n  Esto es lo que NECTAR usa hoy para decidir si hay cámaras.")
    print("  Si sale vacío, NECTAR no ofrece ninguna — y ahí está el problema,")
    print("  porque el programa viejo abre la cámara igual (prueba 5).")


# ---------------------------------------------------------------------------
titulo("5 · ABRIR LA CÁMARA DE VERDAD (la prueba que importa)")
if HAY_SDK:
    from metavision_core.event_io import EventsIterator   # type: ignore

    # Se prueba con cadena vacía, que en el SDK significa "la primera cámara
    # disponible". Es exactamente lo que hace main_v14.1.py.
    candidatos = [""] + [s for s in seriales if s]
    for candidato in candidatos:
        etiqueta = f"'{candidato}'" if candidato else "'' (primera disponible)"
        print(f"\n  Intentando abrir con input_path={etiqueta}")
        iterador = None
        try:
            iterador = EventsIterator(input_path=candidato, delta_t=10000)
            try:
                alto, ancho = iterador.get_size()
                print(f"    ✔ abierta · geometría {ancho}x{alto}")
            except Exception as exc:
                print(f"    ✔ abierta · get_size() falló: {exc}")

            total = 0
            lotes = 0
            inicio = time.time()
            for eventos in iterador:
                total += len(eventos)
                lotes += 1
                if lotes >= 20 or (time.time() - inicio) > 3.0:
                    break
            transcurrido = max(1e-6, time.time() - inicio)
            print(f"    ✔ {lotes} lote(s), {total:,} eventos en "
                  f"{transcurrido:.2f} s  ({total / transcurrido / 1e6:.3f} MEv/s)")
            if total == 0:
                print("    ⚠ cero eventos: la cámara abre pero no dispara."
                      " Tapa y destapa el lente mientras corre esta prueba.")

            # --- facilidades del HAL -----------------------------------
            dispositivo = None
            for atributo in ("reader", "_reader"):
                lector = getattr(iterador, atributo, None)
                if lector is not None:
                    dispositivo = getattr(lector, "device", None)
                    if dispositivo is not None:
                        break
            print(f"    dispositivo del HAL: "
                  f"{'accesible' if dispositivo is not None else 'NO accesible'}")
            if dispositivo is not None:
                for facilidad in ("get_i_ll_biases",
                                  "get_i_camera_synchronization",
                                  "get_i_events_stream",
                                  "get_i_roi",
                                  "get_i_hw_identification",
                                  "get_i_geometry"):
                    funcion = getattr(dispositivo, facilidad, None)
                    if funcion is None:
                        print(f"      {facilidad:.<34} (no existe)")
                        continue
                    try:
                        objeto = funcion()
                        print(f"      {facilidad:.<34} "
                              f"{'✔ ' + type(objeto).__name__ if objeto else '✖ devolvió None'}")
                        if facilidad == "get_i_hw_identification" and objeto:
                            for getter in ("get_serial", "get_integrator",
                                           "get_sensor_info"):
                                try:
                                    print(f"        {getter}() = "
                                          f"{getattr(objeto, getter)()}")
                                except Exception as exc:
                                    print(f"        {getter}() ✖ {exc}")
                        if facilidad == "get_i_roi" and objeto:
                            print(f"        métodos: "
                                  f"{[m for m in dir(objeto) if not m.startswith('_')]}")
                    except Exception as exc:
                        print(f"      {facilidad:.<34} ✖ {exc}")
            break        # con una que abra, alcanza
        except Exception as exc:
            print(f"    ✖ {type(exc).__name__}: {exc}")
        finally:
            del iterador


# ---------------------------------------------------------------------------
titulo("6 · VISUALIZACIÓN")
try:
    import pyqtgraph as pg
    dato("pyqtgraph", getattr(pg, "__version__", "sí"))
    try:
        import OpenGL                                     # noqa: F401
        dato("PyOpenGL (aceleración por GPU)", "✔ disponible")
    except Exception:
        dato("PyOpenGL (aceleración por GPU)",
             "✖ falta — pip install PyOpenGL")
except Exception as exc:
    dato("pyqtgraph", f"✖ {exc}")
try:
    import numba
    dato("numba (renderizado JIT)", numba.__version__)
except Exception as exc:
    dato("numba (renderizado JIT)", f"✖ {exc}")

titulo("FIN")
print("Copia y pega toda esta salida en el chat.\n")
