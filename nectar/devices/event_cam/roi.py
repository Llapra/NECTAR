"""ROI de la cámara de eventos: recorte en el sensor, con el HAL.

El problema que esto arregla
----------------------------
En la primera versión de NECTAR el ROI de la cámara de eventos era **solo
visual**: se filtraban los eventos antes de pintarlos, y nada más. El sensor
seguía disparando en todo el cuadro, el flujo seguía llegando entero por el
USB y el ``.raw`` grabado contenía todo. Es decir, no servía para lo único
que un ROI tiene que servir: bajar el volumen de datos y quedarse con la
región que interesa.

Aquí el recorte se programa en el sensor con la facilidad ``I_ROI`` del HAL de
Metavision. Los píxeles de fuera **dejan de generar eventos**, así que baja la
tasa, baja la ocupación del USB y el archivo contiene solo la región elegida.

Dos advertencias que conviene tener escritas
--------------------------------------------
1. Las coordenadas de los eventos **siguen siendo absolutas** respecto del
   sensor completo. Un evento del centro de un ROI que empieza en x=200 se
   graba con x≈520, no con x≈120. Eso es lo correcto y además es lo que hace
   que el archivo se siga abriendo sin sorpresas en el visor de Metavision;
   pero hay que saberlo al analizar, y por eso el ROI queda escrito en el
   ``_meta.json`` que acompaña a cada ``.raw``.

2. La API de ``I_ROI`` cambió entre versiones del SDK (``set_window`` con un
   objeto ``Window``, ``set_ROIs`` con una lista, ``set_ROI`` con cuatro
   enteros). Aquí se prueban todas las formas conocidas en orden y se informa
   cuál funcionó, en vez de atarse a una versión concreta del SDK. Si ninguna
   funciona se dice claramente que esa cámara no admite recorte por hardware,
   que es información útil, en lugar de fingir que el recorte se aplicó.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

#: Algunos sensores exigen que el recorte caiga en múltiplos de N píxeles.
#: El HAL no siempre lo reporta, así que se usa un valor conservador que
#: funciona en los sensores Gen3/Gen4 de Prophesee.
DEFAULT_STEP = 4


@dataclass
class EventRoi:
    """Recorte pedido y recorte efectivamente aplicado."""

    x: int = 0
    y: int = 0
    width: int = 0
    height: int = 0
    enabled: bool = False
    sensor_width: int = 0
    sensor_height: int = 0
    applied: bool = False
    method: str = ""
    error: str = ""

    @property
    def rect(self) -> Tuple[int, int, int, int]:
        return (self.x, self.y, self.width, self.height)

    @property
    def pixel_fraction(self) -> float:
        """Fracción del sensor que queda activa. Escala la tasa esperada.

        Devuelve 1.0 si el recorte está pedido pero **no** programado en el
        sensor. Es la diferencia que importa para el presupuesto de disco: un
        recorte solo visual no baja ni un byte el caudal, porque los píxeles
        de fuera siguen disparando. Descontarlo sería prometer un archivo más
        chico del que se va a escribir, y el presupuesto de la pestaña 1 se
        quedaría corto justo en la toma larga.
        """
        total = self.sensor_width * self.sensor_height
        if not total or not self.enabled or not self.applied:
            return 1.0
        return max(0.0, min(1.0, (self.width * self.height) / total))

    def as_dict(self) -> Dict[str, Any]:
        return {
            "activo": self.enabled,
            "x": self.x, "y": self.y,
            "ancho": self.width, "alto": self.height,
            "sensor_ancho": self.sensor_width,
            "sensor_alto": self.sensor_height,
            "fraccion_pixeles": round(self.pixel_fraction, 4),
            "aplicado_en_hardware": self.applied,
            "solo_visual": bool(self.enabled and not self.applied),
            "metodo_hal": self.method,
            "error": self.error,
            "nota_coordenadas": (
                "Los eventos del .raw conservan coordenadas absolutas del "
                "sensor completo; el recorte no las desplaza."),
            "nota_solo_visual": (
                "ATENCION: el recorte no se programó en el sensor. Este .raw "
                "contiene el CUADRO COMPLETO; el rectángulo de arriba fue "
                "solo una ayuda de encuadre en la pantalla."
                if (self.enabled and not self.applied) else ""),
        }


def snap(value: int, step: int = DEFAULT_STEP) -> int:
    step = max(1, int(step))
    return int(max(0, (int(value) // step) * step))


def clamp_window(x: int, y: int, width: int, height: int,
                 sensor_width: int, sensor_height: int,
                 step: int = DEFAULT_STEP) -> Tuple[int, int, int, int]:
    """Lleva un rectángulo cualquiera a uno que el sensor pueda aceptar."""
    width = max(step, snap(width, step))
    height = max(step, snap(height, step))
    width = min(width, sensor_width)
    height = min(height, sensor_height)
    x = min(max(0, snap(x, step)), max(0, sensor_width - width))
    y = min(max(0, snap(y, step)), max(0, sensor_height - height))
    return x, y, width, height


def _window_class():
    """La clase ``Window`` del HAL, si esta versión del SDK la tiene."""
    try:
        from metavision_hal import I_ROI            # type: ignore
        return getattr(I_ROI, "Window", None)
    except Exception:
        return None


def apply_event_roi(device, x: int, y: int, width: int, height: int,
                    sensor_width: int, sensor_height: int) -> EventRoi:
    """Programa el recorte en el sensor. Devuelve qué se aplicó y cómo.

    ``device`` es el dispositivo del HAL (``i_device``), no el iterador.
    """
    x, y, width, height = clamp_window(x, y, width, height,
                                       sensor_width, sensor_height)
    result = EventRoi(x=x, y=y, width=width, height=height, enabled=True,
                      sensor_width=sensor_width, sensor_height=sensor_height)

    facility = None
    try:
        facility = device.get_i_roi()
    except Exception as exc:
        result.error = f"el HAL no expone I_ROI: {exc}"
        return result
    if facility is None:
        result.error = "el HAL no expone I_ROI en esta cámara"
        return result

    window_class = _window_class()
    attempts = []
    if window_class is not None:
        attempts.append(("set_window(Window)",
                         lambda: facility.set_window(
                             window_class(x, y, width, height))))
        attempts.append(("set_windows([Window])",
                         lambda: facility.set_windows(
                             [window_class(x, y, width, height)])))
        attempts.append(("set_ROIs([Window])",
                         lambda: facility.set_ROIs(
                             [window_class(x, y, width, height)])))
    attempts.append(("set_window(x,y,w,h)",
                     lambda: facility.set_window(x, y, width, height)))
    attempts.append(("set_ROI(x,y,w,h)",
                     lambda: facility.set_ROI(x, y, width, height)))

    last_error = ""
    for name, call in attempts:
        try:
            call()
        except Exception as exc:
            last_error = f"{name}: {type(exc).__name__}: {exc}"
            continue
        result.method = name
        result.applied = True
        break

    if not result.applied:
        result.error = (f"ninguna forma conocida de I_ROI funcionó "
                        f"({last_error})")
        return result

    try:
        facility.enable(True)
    except Exception as exc:
        result.error = f"recorte fijado pero no se pudo activar: {exc}"
        result.applied = False
    return result


def disable_event_roi(device, sensor_width: int,
                      sensor_height: int) -> EventRoi:
    """Devuelve el sensor al cuadro completo."""
    result = EventRoi(x=0, y=0, width=sensor_width, height=sensor_height,
                      enabled=False, sensor_width=sensor_width,
                      sensor_height=sensor_height)
    try:
        facility = device.get_i_roi()
        if facility is not None:
            facility.enable(False)
            result.applied = True
            result.method = "enable(False)"
    except Exception as exc:
        result.error = f"{type(exc).__name__}: {exc}"
    return result
