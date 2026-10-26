#!/usr/bin/env python3
"""NECTAR · punto de entrada.

Neuromorphic & Emergent Camera Tomography for Atmospheric Research
Proyecto PASTAM · ANID FONDEF IDeA I+D · PUCV / ESO Paranal

Uso
---
::

    python main.py                      # normal: usa el hardware que encuentre
    python main.py --demo               # fuerza el modo simulado en todo
    python main.py --base-dir /datos    # carpeta base de las sesiones
    python main.py --check              # comprueba dependencias y sale

El modo ``--demo`` no es un adorno: permite ensayar el disparo síncrono de los
seis instrumentos, la estructura de carpetas y las once pestañas completas sin
tener el banco óptico montado. Es lo que conviene usar para aprender el
programa antes de la campaña.

**Fuera de ``--demo`` no existen dispositivos simulados.** Si el hardware no
aparece se muestra el error del hardware, y punto. Una cámara simulada que
aparece sola puede pasar desapercibida en una noche de campaña y dejar una
carpeta llena de datos inventados con aspecto de medición; es el peor fallo
posible de un programa de adquisición. Y lo que sí se graba en ``--demo``
queda marcado: carpeta con sufijo ``_DEMO``, archivo testigo dentro y
``simulado: true`` en todos los metadatos.
"""

from __future__ import annotations

import argparse
import os
import sys


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="nectar",
        description="NECTAR · plataforma de adquisición del proyecto PASTAM")
    parser.add_argument("--demo", action="store_true",
                        help="fuerza el modo simulado en todos los dispositivos")
    parser.add_argument("--base-dir", default=None,
                        help="carpeta base donde se crean las sesiones")
    parser.add_argument("--check", action="store_true",
                        help="informa qué dependencias hay y sale")
    parser.add_argument("--tab", type=int, default=None,
                        help="abre directamente una pestaña (1-11)")
    parser.add_argument("--autotest", action="store_true",
                        help="construye la ventana entera sin mostrarla, "
                             "comprueba las 11 pestañas y sale")
    return parser.parse_args(argv)


def autotest() -> int:
    """Construye las once pestañas sin abrir ventana y dice si alguna falla.

    Para qué sirve: los errores de orden de construcción —usar un atributo
    antes de crearlo— no se ven importando módulos, solo al instanciar de
    verdad. Esta comprobación hace exactamente lo que hace el programa al
    arrancar (construir, descubrir, bloquear, refrescar) pero en modo
    invisible, así que se puede correr después de cualquier cambio y antes de
    salir a terreno.

    Se ejecuta sin hardware a propósito: lo que se comprueba es que el
    programa aguante que **no haya nada conectado**, que es el caso en el que
    se rompió una vez.
    """
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    try:
        from PyQt6.QtWidgets import QApplication
    except ImportError:
        print("PyQt6 no está instalado: no se puede hacer la comprobación.")
        return 1

    from nectar import config
    from nectar.core.device_manager import DeviceManager
    from nectar.core.global_recorder import GlobalRecorder
    from nectar.core.session_manager import SessionManager
    from nectar.ui.main_window import TAB_SPECS

    application = QApplication([])            # noqa: F841 (hace falta que viva)
    sessions = SessionManager()
    recorder = GlobalRecorder(sessions)
    manager = DeviceManager(recorder)

    print(f"\n{config.APP_NAME} · comprobación de la interfaz\n{'=' * 52}")
    fallos = []
    for _key, label, tab_class, _family in TAB_SPECS:
        try:
            tab = tab_class(manager, recorder, sessions)
            for metodo in ("detect", "refresh"):
                if hasattr(tab, metodo):
                    getattr(tab, metodo)()
            if hasattr(tab, "set_locked"):
                tab.set_locked(True)
                tab.set_locked(False)
            print(f"  ✔ {label}")
        except Exception as exc:
            import traceback
            fallos.append((label, traceback.format_exc()))
            print(f"  ✖ {label}: {type(exc).__name__}: {exc}")

    print("=" * 52)
    if fallos:
        for label, tb in fallos:
            print(f"\n----- {label} -----\n{tb}")
        print(f"{len(fallos)} pestaña(s) no construyen.")
        return 1
    print(f"Las {len(TAB_SPECS)} pestañas construyen, descubren, se bloquean "
          f"y se refrescan sin hardware conectado.")
    return 0


def check_dependencies() -> int:
    """Informe de dependencias. Devuelve 0 si están las obligatorias."""
    import importlib

    required = [
        ("PyQt6", "interfaz gráfica", True),
        ("numpy", "cálculo numérico", True),
    ]
    optional = [
        ("numba", "compilación JIT del renderizado de eventos", False),
        ("pyqtgraph", "visores e imágenes aceleradas, gráficos en vivo", False),
        ("cv2", "grabación de video de la reconstrucción (opencv-python)", False),
        ("matplotlib", "gráficos de análisis y topografía", False),
        ("serial", "puertos serie del IMU y del anemómetro (pyserial)", False),
        ("cffi", "enlace con el eSDK de la cámara Emergent", False),
        ("tifffile", "guardar imágenes TIFF de 16 bits", False),
        ("PIL", "guardar imágenes PNG de 16 bits (pillow)", False),
        ("scipy", "exportar a .mat", False),
        ("pandas", "exportar a .parquet", False),
        ("rasterio", "leer GeoTIFF en la pestaña de topografía", False),
        ("pyproj", "geodesia en la pestaña de topografía", False),
        ("shapely", "geometría en la pestaña de topografía", False),
        ("h5py", "lo importa el SDK de Metavision (metavision_core)", False),
        ("metavision_core", "SDK de la cámara de eventos (Prophesee)", False),
    ]

    print(f"\nNECTAR · comprobación de dependencias\n{'=' * 52}")
    missing_required = []
    for group, title in ((required, "OBLIGATORIAS"), (optional, "OPCIONALES")):
        print(f"\n{title}")
        for module, purpose, _ in group:
            try:
                importlib.import_module(module)
                mark = "✔"
            except Exception:
                mark = "✖"
                if group is required:
                    missing_required.append(module)
            print(f"  {mark} {module:18} {purpose}")

    # --- diagnóstico de los SDK, que no se instalan con pip -----------
    print(f"\n{'=' * 52}\nSDK DE LAS CÁMARAS")
    try:
        from nectar.devices.event_cam import sdk_locator
        print("\n  Metavision (cámara de eventos):")
        for line in sdk_locator.diagnose().splitlines():
            print(f"    {line}")
    except Exception as exc:
        print(f"    no se pudo diagnosticar: {exc}")
    try:
        from nectar.devices.emergent._ffi import (default_dll_path,
                                                  missing_library_message)
        print("\n  eSDK de Emergent:")
        path = default_dll_path()
        if path is not None:
            print(f"    encontrado en {path}")
        else:
            for line in missing_library_message().splitlines():
                print(f"    {line}")
    except Exception as exc:
        print(f"    no se pudo diagnosticar: {exc}")

    # --- puertos serie -------------------------------------------------
    print(f"\n{'=' * 52}\nPUERTOS SERIE")
    try:
        from nectar.devices import serial_ports
        ports, hidden = serial_ports.list_ports()
        if ports:
            for port in ports:
                print(f"    ✔ {port.label}")
        else:
            print(f"    {serial_ports.no_ports_note()}")
        note = serial_ports.hidden_note(hidden)
        if note:
            print(f"    {note}")
    except Exception as exc:
        print(f"    {exc}")

    print(f"\n{'=' * 52}")
    if missing_required:
        print("Faltan dependencias obligatorias: "
              + ", ".join(missing_required))
        print("Instala todo con:  pip install -r requirements.txt")
        return 1
    print("Las dependencias obligatorias están. Lo que falte de la lista "
          "opcional solo desactiva la función que indica.")
    return 0


def main(argv=None) -> int:
    args = parse_args(argv)

    if args.check:
        return check_dependencies()

    if args.autotest:
        return autotest()

    try:
        from PyQt6.QtWidgets import QApplication
    except ImportError:
        print("PyQt6 no está instalado.\n"
              "  pip install -r requirements.txt\n"
              "Para ver el estado completo de las dependencias: "
              "python main.py --check")
        return 1

    if args.demo:
        # Lo leen los descubridores de cada familia y el gestor de sesiones.
        os.environ["NECTAR_FORCE_DEMO"] = "1"
        print("=" * 62)
        print("  MODO DEMOSTRACIÓN · los datos de esta sesión serán FALSOS")
        print("=" * 62)

    from nectar import config
    from nectar.ui.main_window import MainWindow

    application = QApplication(sys.argv)
    application.setApplicationName(config.APP_NAME)
    application.setApplicationVersion(config.APP_VERSION)

    window = MainWindow(base_dir=args.base_dir, force_demo=args.demo)
    window.show()

    if args.tab and 1 <= args.tab <= window.tabs.count():
        window.tabs.setCurrentIndex(args.tab - 1)

    return application.exec()


if __name__ == "__main__":
    sys.exit(main())
