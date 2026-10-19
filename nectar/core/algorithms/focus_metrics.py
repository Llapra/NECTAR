"""Métricas de enfoque para la cámara de eventos.

La física del asunto
--------------------
Una cámara convencional se enfoca midiendo el gradiente espacial de un
fotograma: cuanto más nítido el borde, mayor el gradiente. Un sensor de
eventos no entrega fotogramas, y además se comporta al revés de lo que uno
esperaría: **desenfocado genera *más* eventos**, porque el ensanchamiento del
disco de Airy reparte cada transición de luz sobre muchos píxeles vecinos y
cada uno de ellos dispara.

De ahí las dos métricas complementarias que implementa este módulo:

1. **Event Rate (ER).** Apuntando a un blanco estático (o en micro-vibración),
   el enfoque concentra la señal en bordes geométricos nítidos y la tasa de
   eventos cae. El óptimo es un *mínimo* local de ER. Es barata y responde en
   milisegundos, pero es sensible a que cambie la iluminación.

2. **Energía de Alta Frecuencia (HFE) por DFT 2D.** Sobre el marco de eventos
   acumulado se normaliza logarítmicamente::

       I_norm(x,y) = log(1 + I(x,y)) / log(1 + max(I)) * 255

   se aplica la transformada de Fourier 2D y se calcula qué fracción de la
   potencia espectral vive en las frecuencias espaciales altas. Enfocado =
   *máximo* de HFE. Es más robusta que ER frente a cambios de brillo y da una
   curva unimodal, que es lo que permite decir "sigue girando en la misma
   dirección".

Sobre esas dos métricas se construyen dos cosas distintas:

* ``FocusEngine`` — el método completo de Li et al. en dos etapas: primero la
  razón de polaridad (EPR) para acotar la vecindad del foco sin pagar ninguna
  FFT, y solo entonces el acoplamiento ER + HFE en dB con máscara anular. Es
  el que usa la pestaña 8 y el que conduce la búsqueda motorizada.
* ``FocusTracker`` — el seguidor simple de una sola métrica que había en la
  primera versión. Se conserva porque sirve para cualquier curva unimodal
  (por ejemplo, ajustar a ojo cualquier otro parámetro) y porque no arrastra
  el estado de dos etapas.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Deque, Optional, Tuple

import numpy as np

#: Fracción del radio de Nyquist por encima de la cual se considera
#: "alta frecuencia". 0,25 deja fuera la iluminación de fondo y el tamaño
#: global de las manchas, y deja dentro los bordes.
HFE_CUTOFF = 0.25


def event_rate(n_events: int, delta_t_us: float) -> float:
    """Tasa de eventos en MEv/s. ``delta_t_us`` es la ventana de integración."""
    if delta_t_us <= 0:
        return 0.0
    return n_events / delta_t_us          # eventos/µs == MEv/s


def normalize_log(frame: np.ndarray) -> np.ndarray:
    """Normalización logarítmica del marco de eventos acumulado.

    El sensor responde al logaritmo de la irradiancia, así que comprimir el
    conteo de la misma forma iguala el peso de las zonas brillantes y oscuras
    antes de mirar el espectro.
    """
    frame = np.asarray(frame, dtype=np.float32)
    peak = float(frame.max())
    if peak <= 0:
        return np.zeros_like(frame)
    return np.log1p(frame) / np.log1p(peak) * 255.0


#: Radios de la máscara anular del paper (Li et al.), en frecuencia espacial
#: normalizada. Por debajo de ``r_in`` vive la iluminación global continua;
#: por encima de ``r_out``, el ruido térmico píxel a píxel. Entre los dos,
#: los bordes, que es lo único que responde al enfoque.
HFE_R_IN = 0.24
HFE_R_OUT = 0.71

#: Caché de máscaras anulares por (alto, ancho, r_in, r_out). Construir la
#: máscara cuesta más que la propia FFT si se rehace en cada cuadro, y las
#: formas se repiten siempre.
_MASK_CACHE: dict = {}


def annular_mask(height: int, width: int, r_in: float = HFE_R_IN,
                 r_out: float = HFE_R_OUT) -> np.ndarray:
    """Máscara paso-banda anular sobre el espectro centrado.

    El radio se normaliza como en el paper::

        r(u,v) = sqrt( ((u - W/2)/W)^2 + ((v - H/2)/H)^2 )

    con lo que la esquina del espectro queda en r = 0,707: por eso
    ``r_out = 0,71`` significa "hasta el borde", y el filtro es en la práctica
    un paso-alto con el ruido de la esquina apenas recortado.
    """
    key = (int(height), int(width), float(r_in), float(r_out))
    cached = _MASK_CACHE.get(key)
    if cached is not None:
        return cached
    rows = (np.arange(height, dtype=np.float32) - height / 2.0) / float(height)
    cols = (np.arange(width, dtype=np.float32) - width / 2.0) / float(width)
    radius = np.sqrt(rows[:, None] ** 2 + cols[None, :] ** 2)
    mask = ((radius >= r_in) & (radius <= r_out))
    if len(_MASK_CACHE) > 16:
        _MASK_CACHE.clear()
    _MASK_CACHE[key] = mask
    return mask


_WINDOW_CACHE: dict = {}


def hann_window(height: int, width: int) -> np.ndarray:
    """Ventana de Hann 2D, cacheada por forma."""
    key = (int(height), int(width))
    cached = _WINDOW_CACHE.get(key)
    if cached is not None:
        return cached
    window = np.outer(np.hanning(height), np.hanning(width)).astype(np.float32)
    if len(_WINDOW_CACHE) > 16:
        _WINDOW_CACHE.clear()
    _WINDOW_CACHE[key] = window
    return window


def hfe_energy_db(frame: np.ndarray, r_in: float = HFE_R_IN,
                  r_out: float = HFE_R_OUT,
                  window: bool = True) -> Tuple[float, float]:
    """Energía de alta frecuencia del cuadro acumulado, lineal y en dB.

    Es la métrica fina del método de Li et al., implementada al pie de la
    letra: normalización logarítmica, DFT 2D, máscara anular y energía
    cuadrática integrada dividida por el número de píxeles::

        E_HFE = (1/WH) · Σ |F(u,v)|² · M(u,v)
        HFE_dB = 10 · log10(E_HFE)

    Con un añadido que el paper no menciona y que hace falta
    -------------------------------------------------------
    Antes de la transformada se aplica una **ventana de Hann 2D**. La DFT
    supone que la imagen se repite periódicamente, así que el salto entre el
    borde derecho y el izquierdo del cuadro se comporta como un borde
    infinitamente nítido e inyecta energía de banda ancha que no tiene nada
    que ver con el enfoque.

    No es un detalle académico: midiendo sobre una grilla sintética de manchas
    gaussianas, sin ventana la curva HFE cae correctamente de 47,8 dB a
    23,8 dB entre σ = 1 px y σ = 3 px, pero **vuelve a subir** hasta 48,0 dB
    en σ = 8 px, porque con las manchas muy difusas la única estructura nítida
    que queda es el propio borde del cuadro. Un operador guiado por esa curva
    giraría el anillo hacia el desenfoque creyendo que mejora. Con la ventana
    de Hann la misma serie da 39,8 → 16,2 → −15,1 dB: monótona, como tiene que
    ser.

    Devuelve ``(E_HFE, HFE_dB)``. Con un cuadro vacío devuelve ``(0, -inf)``,
    que el seguimiento descarta en vez de tratarlo como un mínimo real.
    """
    image = normalize_log(frame)
    if image.size == 0 or not np.any(image):
        return 0.0, float("-inf")
    if window:
        image = image * hann_window(image.shape[0], image.shape[1])

    spectrum = np.fft.fftshift(np.fft.fft2(image))
    power = np.abs(spectrum) ** 2
    mask = annular_mask(image.shape[0], image.shape[1], r_in, r_out)
    energy = float(power[mask].sum()) / float(image.size)
    if energy <= 0:
        return 0.0, float("-inf")
    return energy, float(10.0 * np.log10(energy))


def event_polarity_ratio(positives: int, negatives: int) -> float:
    """Razón de polaridad R(t) = P/N, la métrica gruesa.

    Se independiza de la magnitud absoluta de eventos, que es justamente lo
    que la hace robusta frente a la velocidad no uniforme de un giro manual y
    frente al ruido térmico del fotorreceptor. Con N = 0 se devuelve 0: no hay
    razón que calcular y el seguimiento lo trata como muestra inválida.
    """
    if negatives <= 0:
        return 0.0
    return float(positives) / float(negatives)


def gaussian_smooth(values: np.ndarray, sigma: float = 1.5) -> np.ndarray:
    """Convolución con un núcleo gaussiano 1D, con bordes replicados.

    Es el ``R(t) * G_σ(t)`` del método. Se implementa a mano para no arrastrar
    scipy como dependencia obligatoria en un equipo de campaña.
    """
    values = np.asarray(values, dtype=np.float64)
    if values.size < 3 or sigma <= 0:
        return values
    radius = max(1, int(round(3.0 * sigma)))
    x = np.arange(-radius, radius + 1, dtype=np.float64)
    kernel = np.exp(-0.5 * (x / sigma) ** 2)
    kernel /= kernel.sum()
    padded = np.pad(values, radius, mode="edge")
    return np.convolve(padded, kernel, mode="valid")


def positive_derivative_area(values: np.ndarray) -> float:
    """Mayor área bajo la derivada positiva de una curva.

    Es el criterio de zona gruesa: se buscan los tramos continuos donde
    dR/dt > 0 y se toma el de mayor integral, que por el teorema fundamental
    del cálculo es simplemente ``R(final) - R(inicio)`` de ese tramo. El
    máximo de esas subidas delimita la vecindad del foco.
    """
    values = np.asarray(values, dtype=np.float64)
    if values.size < 3:
        return 0.0
    derivative = np.diff(values)
    best = current = 0.0
    for step in derivative:
        if step > 0:
            current += step
            best = max(best, current)
        else:
            current = 0.0
    return float(best)


def hfe_2d_dft(frame: np.ndarray, cutoff: float = HFE_CUTOFF) -> float:
    """Fracción de potencia espectral en altas frecuencias (0..1).

    Se normaliza por la potencia total, de modo que subir la ganancia o la
    iluminación no mueve la métrica: solo la nitidez lo hace.
    """
    image = normalize_log(frame)
    if image.size == 0 or not np.any(image):
        return 0.0

    spectrum = np.fft.fftshift(np.fft.fft2(image))
    power = np.abs(spectrum) ** 2
    total = float(power.sum())
    if total <= 0:
        return 0.0

    height, width = image.shape
    cy, cx = height / 2.0, width / 2.0
    yy, xx = np.ogrid[:height, :width]
    # Radio normalizado: 1.0 en el borde del espectro (Nyquist).
    radius = np.sqrt(((yy - cy) / cy) ** 2 + ((xx - cx) / cx) ** 2)
    high = float(power[radius >= cutoff].sum())
    return high / total


def tenengrad(frame: np.ndarray) -> float:
    """Métrica de gradiente (Sobel) como referencia cruzada.

    Sirve para la reconstrucción de intensidad, donde sí hay algo parecido a
    un fotograma, y como control de sanidad de la HFE.
    """
    image = np.asarray(frame, dtype=np.float32)
    if image.ndim != 2 or min(image.shape) < 3:
        return 0.0
    gx = np.diff(image, axis=1)[:-1, :]
    gy = np.diff(image, axis=0)[:, :-1]
    return float(np.mean(gx * gx + gy * gy))


# ---------------------------------------------------------------------------
# Seguimiento temporal y consejo de dirección
# ---------------------------------------------------------------------------
DIRECTION_HOLD = "enfocado"
DIRECTION_KEEP = "seguir"
DIRECTION_REVERSE = "invertir"
DIRECTION_UNKNOWN = "sin_datos"


@dataclass
class FocusAdvice:
    """Lo que la pestaña de enfoque le muestra al operador."""

    direction: str = DIRECTION_UNKNOWN
    message: str = "Esperando datos…"
    metric: float = 0.0
    best_metric: float = 0.0
    #: Distancia relativa al mejor valor visto, 0..1 (1 = estamos en el óptimo).
    quality: float = 0.0
    trend: float = 0.0
    in_focus: bool = False


class FocusTracker:
    """Decide si hay que seguir girando el lente, invertir, o parar.

    Funciona con cualquiera de las dos métricas indicando ``maximize``:
    HFE se maximiza, Event Rate se minimiza.
    """

    def __init__(self, history: int = 24, maximize: bool = True,
                 focus_tolerance: float = 0.02) -> None:
        self.maximize = maximize
        self.focus_tolerance = focus_tolerance
        self._values: Deque[float] = deque(maxlen=history)
        self._best: Optional[float] = None
        self._last_direction = DIRECTION_KEEP

    def reset(self) -> None:
        self._values.clear()
        self._best = None
        self._last_direction = DIRECTION_KEEP

    def set_mode(self, maximize: bool) -> None:
        if maximize != self.maximize:
            self.maximize = maximize
            self.reset()

    def _score(self, value: float) -> float:
        """Convierte la métrica a "más grande es mejor"."""
        return value if self.maximize else -value

    def update(self, value: float) -> FocusAdvice:
        value = float(value)
        self._values.append(value)
        score = self._score(value)
        if self._best is None or score > self._score(self._best):
            self._best = value

        if len(self._values) < 4:
            return FocusAdvice(direction=DIRECTION_UNKNOWN,
                               message="Acumulando muestras…",
                               metric=value,
                               best_metric=self._best or value)

        # Se trabaja sobre el "puntaje" (más grande = mejor) para que el mismo
        # razonamiento valga para HFE, que se maximiza, y para Event Rate, que
        # se minimiza.
        window = np.array([self._score(v) for v in self._values],
                          dtype=np.float64)
        recent = window[-min(9, window.size):]
        x = np.arange(recent.size, dtype=np.float64)
        scale = float(np.mean(np.abs(window))) or 1.0

        # --- tendencia lineal ------------------------------------------
        slope, intercept = np.polyfit(x, recent, 1)
        trend = float(slope) / scale
        # El ruido se mide sobre los *residuos* del ajuste, no sobre la
        # desviación estándar cruda: una rampa limpia tiene desviación grande
        # y ruido cero, y confundir las dos cosas hacía que una subida franca
        # se informara como "ya está enfocado".
        residuals = recent - (float(slope) * x + float(intercept))
        noise = float(np.std(residuals)) / scale
        threshold = max(2.0 * noise, 0.004)

        best_score = self._score(self._best)
        quality = 0.0
        if best_score != 0:
            quality = max(0.0, min(1.0, score / best_score))

        # --- curvatura: la curva de foco es unimodal --------------------
        # Cerca del óptimo, la métrica se comporta como una parábola. Ajustar
        # grado 2 y localizar el vértice responde directamente la pregunta del
        # operador: ¿el máximo está adelante, ya lo pasé, o estoy en él?
        vertex: Optional[float] = None
        last_x = recent.size - 1
        if recent.size >= 6:
            quad = np.polyfit(x, recent, 2)
            quad_a, quad_b = float(quad[0]), float(quad[1])
            if quad_a < 0:                      # cóncava: hay un máximo
                candidate = -quad_b / (2.0 * quad_a)
                if -1.0 <= candidate <= recent.size + 1.0:
                    # La parábola solo se cree si la caída desde el vértice
                    # hasta ahora es mayor que el ruido del propio ajuste. Sin
                    # esta prueba, una meseta con ruido se lee como un óptimo
                    # recién pasado y el consejo manda invertir sin motivo.
                    fitted = np.polyval(quad, x)
                    noise2 = float(np.std(recent - fitted)) / scale
                    drop = (float(np.polyval(quad, candidate))
                            - float(np.polyval(quad, last_x))) / scale
                    if candidate >= last_x - 0.5 or drop > max(3.0 * noise2, 0.02):
                        vertex = candidate

        advice: FocusAdvice

        if vertex is not None:
            if vertex >= last_x - 0.5:
                if quality >= 1.0 - self.focus_tolerance:
                    advice = FocusAdvice(
                        direction=DIRECTION_HOLD,
                        message="✔ Enfocado: no muevas el lente",
                        metric=value, best_metric=self._best,
                        quality=quality, trend=trend, in_focus=True)
                else:
                    advice = FocusAdvice(
                        direction=DIRECTION_KEEP,
                        message="↻ Casi: sigue girando muy despacio",
                        metric=value, best_metric=self._best,
                        quality=quality, trend=trend)
            else:
                advice = FocusAdvice(
                    direction=DIRECTION_REVERSE,
                    message="↺ Te pasaste el óptimo: gira al lado contrario",
                    metric=value, best_metric=self._best,
                    quality=quality, trend=trend)
        elif abs(trend) <= threshold and quality >= 1.0 - self.focus_tolerance:
            advice = FocusAdvice(direction=DIRECTION_HOLD,
                                 message="✔ Enfocado: no muevas el lente",
                                 metric=value, best_metric=self._best,
                                 quality=quality, trend=trend, in_focus=True)
        elif trend > threshold:
            advice = FocusAdvice(direction=DIRECTION_KEEP,
                                 message="↻ Sigue girando en la misma dirección",
                                 metric=value, best_metric=self._best,
                                 quality=quality, trend=trend)
        elif trend < -threshold:
            advice = FocusAdvice(direction=DIRECTION_REVERSE,
                                 message="↺ Vas para el lado equivocado: invierte",
                                 metric=value, best_metric=self._best,
                                 quality=quality, trend=trend)
        else:
            advice = FocusAdvice(direction=self._last_direction,
                                 message="… Métrica plana: gira un poco más",
                                 metric=value, best_metric=self._best,
                                 quality=quality, trend=trend)

        if advice.direction in (DIRECTION_KEEP, DIRECTION_REVERSE):
            self._last_direction = advice.direction
        return advice

    @property
    def history(self) -> Tuple[float, ...]:
        return tuple(self._values)


# ---------------------------------------------------------------------------
# Motor de enfoque en dos etapas (Li et al.) con observador cinemático
# ---------------------------------------------------------------------------
STAGE_IDLE = "reposo"
STAGE_COARSE = "grueso"
STAGE_FINE = "fino"
STAGE_FOCUSED = "optimo"

#: Tolerancia de acoplamiento del paper: los dos estimadores tienen que caer
#: a menos de 10 ms uno del otro. Vale tal cual en modo motorizado, donde el
#: barrido es uniforme y el tiempo se mapea a pasos del motor.
COUPLING_TOLERANCE_S = 0.010

#: Ventana de memoria corta del observador cinemático (200 a 400 ms).
MEMORY_WINDOW_S = 0.30
#: Área bajo la derivada positiva de EPR a partir de la cual se considera que
#: ya se está en la vecindad del foco y conviene encender la DFT 2D.
COARSE_AREA_THRESHOLD = 0.12
#: Historia larga sobre la que se busca la zona gruesa.
COARSE_WINDOW_S = 8.0

#: Colores que la pestaña usa para el indicador, según el método:
#: verde = sigue, rojo = invierte, amarillo = gira más, azul = foco.
COLOR_KEEP = "verde"
COLOR_REVERSE = "rojo"
COLOR_SLOW = "amarillo"
COLOR_FOCUS = "azul"


@dataclass
class FocusSample:
    """Una muestra del observador: un instante con sus cuatro métricas."""

    t: float = 0.0              # segundos, reloj monotónico
    epr: float = 0.0            # R(t) = P/N
    event_rate: float = 0.0     # MEv/s
    hfe_db: float = float("-inf")
    events: int = 0


@dataclass
class FocusReport:
    """Todo lo que la pestaña de enfoque necesita para una actualización."""

    stage: str = STAGE_IDLE
    direction: str = DIRECTION_UNKNOWN
    color: str = COLOR_SLOW
    message: str = "Esperando datos…"

    epr: float = 0.0
    epr_slope: float = 0.0
    event_rate: float = 0.0
    er_slope: float = 0.0
    hfe_db: float = float("-inf")
    hfe_slope: float = 0.0

    coarse_area: float = 0.0
    in_coarse_zone: bool = False

    best_hfe_db: float = float("-inf")
    best_age_s: float = 0.0
    coupled: bool = False
    coupling_gap_s: float = float("inf")
    in_focus: bool = False
    samples: int = 0

    def as_dict(self) -> dict:
        return {
            "etapa": self.stage,
            "direccion": self.direction,
            "color": self.color,
            "mensaje": self.message,
            "epr": round(self.epr, 4),
            "epr_pendiente": round(self.epr_slope, 4),
            "tasa_mev_s": round(self.event_rate, 4),
            "hfe_db": (None if self.hfe_db == float("-inf")
                       else round(self.hfe_db, 3)),
            "hfe_pendiente": round(self.hfe_slope, 4),
            "area_gruesa": round(self.coarse_area, 4),
            "zona_gruesa": self.in_coarse_zone,
            "acoplado": self.coupled,
            "enfocado": self.in_focus,
        }


def _slope(times: np.ndarray, values: np.ndarray) -> float:
    """Pendiente por regresión lineal, en unidades de la métrica por segundo.

    Es el estimador del método: la derivada temporal de la métrica activa
    sobre el buffer de memoria corta. Se usa regresión y no una diferencia
    entre extremos porque el giro manual no es uniforme y una sola muestra
    ruidosa no puede decidir la dirección.
    """
    if times.size < 3:
        return 0.0
    span = float(times[-1] - times[0])
    if span <= 0:
        return 0.0
    finite = np.isfinite(values)
    if finite.sum() < 3:
        return 0.0
    t = times[finite] - times[finite][0]
    v = values[finite]
    variance = float(((t - t.mean()) ** 2).sum())
    if variance <= 0:
        return 0.0
    return float(((t - t.mean()) * (v - v.mean())).sum() / variance)


class FocusEngine:
    """Enfoque en dos etapas con guía direccional para lente manual.

    Implementa el método de Li et al. tal como está en el documento del
    proyecto, con el añadido que hacía falta para un lente **sin encoder**: un
    observador cinemático sobre un buffer circular de memoria corta, que
    convierte una formulación pensada para motores en una instrucción que un
    operador puede seguir con las manos.

    Etapa gruesa (EPR)
        Se calcula ``R(t) = P/N`` en ventanas de ``Δt_c`` (10 ms por omisión),
        se suaviza con un núcleo gaussiano y se mira el signo de su derivada.
        Positiva significa "vas hacia el foco". El área acumulada del tramo de
        derivada positiva más grande es lo que decide que ya estamos en la
        vecindad del foco y se puede encender la etapa fina, que es la cara.

    Etapa fina (ER + HFE)
        Se busca a la vez el **mínimo** de tasa de eventos y el **máximo** de
        energía de alta frecuencia en dB. Los dos tienen que coincidir dentro
        de una tolerancia temporal para declarar foco: un mínimo de ER sin pico
        de HFE es un desvanecimiento de la escena, no un enfoque.

    Sobre la tolerancia de acoplamiento
        El paper fija 10 ms porque ahí el tiempo está mapeado a pasos de un
        motor con barrido uniforme. Con un lente girado a mano la métrica se
        muestrea cada decenas de milisegundos y exigir 10 ms sería exigir que
        las dos métricas caigan en la misma muestra. Por eso en modo manual la
        tolerancia se relaja a unas pocas muestras y se dice cuál se usó, en
        vez de copiar un número que en este montaje no significa lo mismo.
    """

    def __init__(self, memory_window_s: float = MEMORY_WINDOW_S,
                 coarse_window_s: float = COARSE_WINDOW_S,
                 motorized: bool = False,
                 coarse_area_threshold: float = COARSE_AREA_THRESHOLD,
                 hfe_tolerance_db: float = 0.4,
                 smoothing_sigma: float = 1.5) -> None:
        self.memory_window_s = float(memory_window_s)
        self.coarse_window_s = float(coarse_window_s)
        self.motorized = bool(motorized)
        self.coarse_area_threshold = float(coarse_area_threshold)
        self.hfe_tolerance_db = float(hfe_tolerance_db)
        self.smoothing_sigma = float(smoothing_sigma)

        self._fifo: Deque[FocusSample] = deque()
        self._coarse: Deque[FocusSample] = deque()
        self._stage = STAGE_COARSE
        self._best_hfe = float("-inf")
        self._best_hfe_t = 0.0
        self._best_er = float("inf")
        self._best_er_t = 0.0
        self._peak_seen = False
        self._last_direction = DIRECTION_UNKNOWN
        self._fine_samples = 0
        self._worst_hfe = float("inf")
        self._below_streak = 0

    # ------------------------------------------------------------------
    def reset(self) -> None:
        self._fifo.clear()
        self._coarse.clear()
        self._stage = STAGE_COARSE
        self._best_hfe = float("-inf")
        self._best_hfe_t = 0.0
        self._best_er = float("inf")
        self._best_er_t = 0.0
        self._peak_seen = False
        self._last_direction = DIRECTION_UNKNOWN
        self._fine_samples = 0
        self._worst_hfe = float("inf")
        self._below_streak = 0

    def set_motorized(self, motorized: bool) -> None:
        if bool(motorized) != self.motorized:
            self.motorized = bool(motorized)
            self.reset()

    @property
    def stage(self) -> str:
        return self._stage

    @property
    def needs_spectrum(self) -> bool:
        """True cuando conviene gastar una FFT 2D.

        En etapa gruesa la FFT no aporta y cuesta: el método la enciende solo
        al entrar en la vecindad del foco. Es la diferencia entre un enfoque
        que corre al margen de la adquisición y uno que la estorba.
        """
        return self._stage in (STAGE_FINE, STAGE_FOCUSED)

    @property
    def coupling_tolerance_s(self) -> float:
        if self.motorized:
            return COUPLING_TOLERANCE_S
        # Manual: unas pocas muestras del propio buffer.
        if len(self._fifo) >= 2:
            span = self._fifo[-1].t - self._fifo[0].t
            step = span / max(1, len(self._fifo) - 1)
            return max(COUPLING_TOLERANCE_S, 3.0 * step)
        return 0.1

    # ------------------------------------------------------------------
    def update(self, t: float, positives: int, negatives: int,
               delta_t_s: float, hfe_db: float = float("-inf")) -> FocusReport:
        """Incorpora una ventana de eventos y devuelve el consejo vigente."""
        total = int(positives) + int(negatives)
        rate = (total / delta_t_s / 1e6) if delta_t_s > 0 else 0.0
        sample = FocusSample(t=float(t),
                             epr=event_polarity_ratio(positives, negatives),
                             event_rate=rate, hfe_db=float(hfe_db),
                             events=total)
        self._fifo.append(sample)
        self._coarse.append(sample)
        self._prune()
        return self._evaluate(sample)

    def _prune(self) -> None:
        if not self._fifo:
            return
        now = self._fifo[-1].t
        while self._fifo and (now - self._fifo[0].t) > self.memory_window_s:
            self._fifo.popleft()
        while self._coarse and (now - self._coarse[0].t) > self.coarse_window_s:
            self._coarse.popleft()

    # ------------------------------------------------------------------
    def _evaluate(self, sample: FocusSample) -> FocusReport:
        report = FocusReport(stage=self._stage, epr=sample.epr,
                             event_rate=sample.event_rate,
                             hfe_db=sample.hfe_db,
                             samples=len(self._fifo))

        if len(self._fifo) < 4:
            report.message = "Acumulando muestras…"
            report.color = COLOR_SLOW
            return report

        times = np.array([s.t for s in self._fifo], dtype=np.float64)
        epr_values = np.array([s.epr for s in self._fifo], dtype=np.float64)
        er_values = np.array([s.event_rate for s in self._fifo], dtype=np.float64)
        hfe_values = np.array([s.hfe_db for s in self._fifo], dtype=np.float64)

        smoothed_epr = gaussian_smooth(epr_values, self.smoothing_sigma)
        report.epr_slope = _slope(times, smoothed_epr)
        report.er_slope = _slope(times, er_values)
        report.hfe_slope = _slope(times, hfe_values)

        # --- área de derivada positiva sobre la historia larga ----------
        coarse_epr = np.array([s.epr for s in self._coarse], dtype=np.float64)
        report.coarse_area = positive_derivative_area(
            gaussian_smooth(coarse_epr, self.smoothing_sigma))
        report.in_coarse_zone = report.coarse_area >= self.coarse_area_threshold

        # --- mejores valores vistos -------------------------------------
        if np.isfinite(sample.hfe_db) and sample.hfe_db > self._best_hfe:
            self._best_hfe = sample.hfe_db
            self._best_hfe_t = sample.t
        if sample.event_rate > 0 and sample.event_rate < self._best_er:
            self._best_er = sample.event_rate
            self._best_er_t = sample.t
        report.best_hfe_db = self._best_hfe
        report.best_age_s = max(0.0, sample.t - self._best_hfe_t)

        gap = abs(self._best_hfe_t - self._best_er_t)
        report.coupling_gap_s = gap
        report.coupled = (np.isfinite(self._best_hfe)
                          and self._best_er < float("inf")
                          and gap <= self.coupling_tolerance_s)

        # --- transición de etapa ----------------------------------------
        if self._stage == STAGE_COARSE and report.in_coarse_zone:
            self._stage = STAGE_FINE
            self._peak_seen = False
        report.stage = self._stage

        if self._stage == STAGE_COARSE:
            self._advise_coarse(report, epr_values)
        else:
            self._advise_fine(report, sample)
        return report

    # ------------------------------------------------------------------
    def _advise_coarse(self, report: FocusReport, epr_values: np.ndarray) -> None:
        """Estado A: aproximación gruesa guiada por el signo de dR/dt."""
        scale = float(np.mean(np.abs(epr_values))) or 1.0
        epsilon = max(0.02 * scale, 1e-3)

        if report.epr_slope > epsilon:
            report.direction = DIRECTION_KEEP
            report.color = COLOR_KEEP
            report.message = "↻ Mantén el sentido de giro actual"
        elif report.epr_slope < -epsilon:
            report.direction = DIRECTION_REVERSE
            report.color = COLOR_REVERSE
            report.message = "↺ Sentido incorrecto: invierte el giro"
        else:
            report.direction = self._last_direction
            report.color = COLOR_SLOW
            report.message = "… Gira el anillo de enfoque lentamente"
        if report.direction in (DIRECTION_KEEP, DIRECTION_REVERSE):
            self._last_direction = report.direction

    #: Muestras finas mínimas antes de permitir un veredicto de foco. Sin
    #: esto, las dos primeras muestras de la etapa fina son por definición el
    #: mejor valor visto y el programa diría "detente" con el lente a medio
    #: camino.
    MIN_FINE_SAMPLES = 8
    #: Cuántas muestras seguidas por debajo del pico hacen falta para declarar
    #: rebase. La curva de HFE tiene mínimos locales —un blanco periódico los
    #: produce de verdad, se comprobó midiendo—, así que una sola caída no
    #: puede decidir.
    OVERSHOOT_STREAK = 3
    #: El pico tiene que sobresalir al menos esto sobre el peor valor de la
    #: etapa fina para considerarse un pico y no un accidente del ruido.
    MIN_PROMINENCE_DB = 3.0

    def _advise_fine(self, report: FocusReport, sample: FocusSample) -> None:
        """Estados B y C: ajuste fino y detección de rebase.

        Con tres defensas contra el falso positivo, que es el error caro: si
        el programa dice "enfocado" con el lente a medio camino, el operador
        le cree y la toma sale borrosa.
        """
        if not np.isfinite(sample.hfe_db):
            report.direction = DIRECTION_UNKNOWN
            report.color = COLOR_SLOW
            report.message = "Sin señal espectral: revisa la iluminación"
            return

        self._fine_samples += 1
        if sample.hfe_db < self._worst_hfe:
            self._worst_hfe = sample.hfe_db

        below_peak = self._best_hfe - sample.hfe_db
        prominence = (self._best_hfe - self._worst_hfe
                      if np.isfinite(self._worst_hfe) else 0.0)
        # Tolerancia adaptada al recorrido real de la métrica: 0,4 dB es
        # razonable en una curva de 5 dB y ridículo en una de 50.
        tolerance = max(self.hfe_tolerance_db, 0.05 * max(0.0, prominence))
        mature = self._fine_samples >= self.MIN_FINE_SAMPLES

        if below_peak > tolerance:
            self._below_streak += 1
        else:
            self._below_streak = 0
            if self._stage == STAGE_FOCUSED:
                # La nitidez volvió a subir: no era el óptimo. Se retrocede de
                # etapa en vez de quedarse cantando victoria.
                self._stage = STAGE_FINE

        if report.hfe_slope > 0:
            # El pico estricto se registra con la nitidez, no con la tasa. Si
            # se exigiera además que ER bajara, un cambio de iluminación
            # durante el giro dejaría el detector de rebase sin armar y el
            # operador se pasaría el foco sin aviso.
            if mature and prominence >= self.MIN_PROMINENCE_DB:
                self._peak_seen = True
            report.direction = DIRECTION_KEEP
            report.color = COLOR_KEEP
            if report.er_slope <= 0:
                report.message = "↻ Zona de foco: continúa el ajuste micrométrico"
            else:
                report.message = ("↻ Nitidez subiendo, pero la tasa no "
                                  "acompaña: revisa la iluminación")
        elif (self._peak_seen and report.hfe_slope < 0
              and self._below_streak >= self.OVERSHOOT_STREAK):
            # Rebase confirmado: hubo un pico prominente y la nitidez lleva
            # varias muestras seguidas por debajo de él.
            self._stage = STAGE_FOCUSED
            report.stage = STAGE_FOCUSED
            report.in_focus = True
            report.direction = DIRECTION_REVERSE
            report.color = COLOR_FOCUS
            report.message = ("✔ FOCO ALCANZADO · te pasaste un poco: "
                              "retrocede una fracción mínima")
        elif mature and self._peak_seen and below_peak <= tolerance:
            report.in_focus = report.coupled
            report.direction = DIRECTION_HOLD
            report.color = COLOR_FOCUS if report.coupled else COLOR_KEEP
            report.message = ("✔ Foco: detente" if report.coupled else
                              "↻ Casi: gira muy despacio (falta acoplar ER y HFE)")
        elif report.hfe_slope < 0:
            report.direction = DIRECTION_REVERSE
            report.color = COLOR_REVERSE
            report.message = ("↺ La nitidez cae: invierte el giro" if mature
                              else "↺ La nitidez cae: prueba al otro lado")
        else:
            report.direction = self._last_direction
            report.color = COLOR_SLOW
            report.message = "… Gira un poco más para que la métrica se mueva"
        report.stage = self._stage
        if report.direction in (DIRECTION_KEEP, DIRECTION_REVERSE):
            self._last_direction = report.direction

    # ------------------------------------------------------------------
    def history(self) -> Tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
        """(t, EPR, ER, HFE_dB) del buffer largo, para los gráficos."""
        if not self._coarse:
            empty = np.zeros(0, dtype=np.float64)
            return empty, empty, empty, empty
        times = np.array([s.t for s in self._coarse], dtype=np.float64)
        return (times - times[0],
                np.array([s.epr for s in self._coarse], dtype=np.float64),
                np.array([s.event_rate for s in self._coarse], dtype=np.float64),
                np.array([s.hfe_db for s in self._coarse], dtype=np.float64))
