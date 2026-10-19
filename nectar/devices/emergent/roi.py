"""ROI de la cámara Emergent: recorte real en el sensor, no en la pantalla.

Por qué esto es su propio módulo
--------------------------------
Un ROI dibujado sobre la vista previa no sirve de nada: la cámara sigue
enviando el cuadro completo, el enlace de 25 GbE sigue igual de cargado y el
archivo grabado sigue teniendo el mismo tamaño. El ROI que importa es el que
se programa en el sensor por GenICam: ahí sí baja el volumen de datos, sube
la tasa de cuadros posible y el ``.raw`` contiene solo la región de interés.

La parte delicada son los **incrementos**. Los sensores no aceptan cualquier
número: ``OffsetX`` puede moverse de a 4 u 8 píxeles, ``Width`` de a 16, y
además hay mínimos y máximos. Si se le manda un valor que no respeta el
incremento, la cámara lo rechaza (y algunos modelos lo aceptan y lo redondean
en silencio, que es peor). Por eso todo lo que entra por aquí se ajusta al
múltiplo válido más cercano **antes** de mandarlo, usando los rangos que la
propia cámara reporta, no números escritos a mano.

El otro detalle es el **orden**. Para achicar hay que reducir primero el
tamaño y después mover el origen; para agrandar, al revés. Si se hace al
revés, un paso intermedio deja el rectángulo fuera del sensor y la cámara
rechaza el comando. Eso es lo que resuelve ``apply_roi``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

#: Nombres GenICam del recorte. Son los del estándar SFNC, que la HB-1800
#: respeta.
ROI_PARAMS = ("OffsetX", "OffsetY", "Width", "Height")


@dataclass
class Axis:
    """Rango admisible de un parámetro de recorte, según la cámara."""

    value: int = 0
    minimum: int = 0
    maximum: int = 0
    increment: int = 1

    def snap(self, requested: int) -> int:
        """Ajusta un valor al múltiplo válido más cercano dentro del rango."""
        step = max(1, int(self.increment))
        value = int(requested)
        value = max(self.minimum, min(self.maximum, value))
        # Los incrementos se cuentan desde el mínimo, no desde cero.
        offset = value - self.minimum
        value = self.minimum + (offset // step) * step
        if value < self.minimum:
            value = self.minimum
        if value > self.maximum:
            value = self.minimum + ((self.maximum - self.minimum) // step) * step
        return int(value)


@dataclass
class RoiGeometry:
    """Recorte vigente más los rangos de cada eje."""

    offset_x: Axis
    offset_y: Axis
    width: Axis
    height: Axis
    sensor_width: int = 0
    sensor_height: int = 0

    @property
    def rect(self) -> Tuple[int, int, int, int]:
        return (self.offset_x.value, self.offset_y.value,
                self.width.value, self.height.value)

    @property
    def is_full_frame(self) -> bool:
        return (self.offset_x.value == 0 and self.offset_y.value == 0
                and self.width.value >= self.sensor_width
                and self.height.value >= self.sensor_height)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "offset_x": self.offset_x.value,
            "offset_y": self.offset_y.value,
            "ancho": self.width.value,
            "alto": self.height.value,
            "sensor_ancho": self.sensor_width,
            "sensor_alto": self.sensor_height,
            "cuadro_completo": self.is_full_frame,
            "incrementos": {
                "offset_x": self.offset_x.increment,
                "offset_y": self.offset_y.increment,
                "ancho": self.width.increment,
                "alto": self.height.increment,
            },
        }

    def snap_rect(self, x: int, y: int, width: int,
                  height: int) -> Tuple[int, int, int, int]:
        """Lleva un rectángulo cualquiera al rectángulo válido más cercano.

        Es lo que se llama cuando el operador suelta el rectángulo que
        arrastró sobre la vista previa: el dibujo es libre, el resultado tiene
        que ser algo que la cámara acepte.
        """
        snapped_w = self.width.snap(max(self.width.minimum, int(width)))
        snapped_h = self.height.snap(max(self.height.minimum, int(height)))
        # El origen no puede empujar el recorte fuera del sensor.
        max_x = max(self.offset_x.minimum,
                    (self.sensor_width or self.width.maximum) - snapped_w)
        max_y = max(self.offset_y.minimum,
                    (self.sensor_height or self.height.maximum) - snapped_h)
        snapped_x = self.offset_x.snap(min(int(x), max_x))
        snapped_y = self.offset_y.snap(min(int(y), max_y))
        return snapped_x, snapped_y, snapped_w, snapped_h


def _axis(camera, name: str) -> Axis:
    """Lee valor, mínimo, máximo e incremento de un parámetro de la cámara."""
    axis = Axis()
    try:
        axis.value = int(camera.get_uint32(name))
    except Exception:
        return axis
    try:
        info = camera.param_info(name)
        axis.minimum = int(getattr(info, "minimum", 0))
        axis.maximum = int(getattr(info, "maximum", axis.value))
        axis.increment = max(1, int(getattr(info, "increment", 1)))
    except Exception:
        axis.minimum, axis.maximum, axis.increment = 0, axis.value, 1
    return axis


def read_roi_geometry(camera) -> Optional[RoiGeometry]:
    """Retrato del recorte tal como lo reporta la cámara.

    Devuelve ``None`` si la cámara no expone los parámetros de recorte, que es
    información útil por sí misma: significa que en ese modelo no se puede
    recortar por hardware y no hay que fingir que sí.
    """
    try:
        offset_x = _axis(camera, "OffsetX")
        offset_y = _axis(camera, "OffsetY")
        width = _axis(camera, "Width")
        height = _axis(camera, "Height")
    except Exception:
        return None
    if width.maximum <= 0 or height.maximum <= 0:
        return None

    sensor_width = width.maximum
    sensor_height = height.maximum
    for name, holder in (("SensorWidth", "w"), ("WidthMax", "w"),
                         ("SensorHeight", "h"), ("HeightMax", "h")):
        try:
            value = int(camera.get_uint32(name))
        except Exception:
            continue
        if value <= 0:
            continue
        if holder == "w":
            sensor_width = max(sensor_width, value)
        else:
            sensor_height = max(sensor_height, value)

    return RoiGeometry(offset_x=offset_x, offset_y=offset_y,
                       width=width, height=height,
                       sensor_width=sensor_width, sensor_height=sensor_height)


def apply_roi(camera, geometry: RoiGeometry, x: int, y: int,
              width: int, height: int) -> Tuple[int, int, int, int]:
    """Programa el recorte en el sensor y devuelve lo que quedó de verdad.

    El orden de los cuatro comandos no es indiferente: si se agranda el ancho
    antes de mover el origen hacia la izquierda, hay un instante en que el
    rectángulo se sale del sensor y la cámara rechaza el comando. Por eso se
    lleva primero el origen a cero, se fija el tamaño y recién después se
    coloca el origen definitivo.

    Devuelve el rectángulo releído de la cámara, no el pedido: si el sensor
    ajustó algo, lo que vale es lo que el sensor dice.
    """
    target = geometry.snap_rect(x, y, width, height)
    snapped_x, snapped_y, snapped_w, snapped_h = target

    # 1. Origen a cero: deja sitio para cualquier tamaño.
    camera.set_uint32("OffsetX", geometry.offset_x.snap(0))
    camera.set_uint32("OffsetY", geometry.offset_y.snap(0))
    # 2. Tamaño.
    camera.set_uint32("Width", snapped_w)
    camera.set_uint32("Height", snapped_h)
    # 3. Origen definitivo.
    camera.set_uint32("OffsetX", snapped_x)
    camera.set_uint32("OffsetY", snapped_y)

    updated = read_roi_geometry(camera)
    return updated.rect if updated is not None else target


def full_frame(camera, geometry: RoiGeometry) -> Tuple[int, int, int, int]:
    """Vuelve al cuadro completo del sensor."""
    return apply_roi(camera, geometry, 0, 0,
                     geometry.sensor_width or geometry.width.maximum,
                     geometry.sensor_height or geometry.height.maximum)
