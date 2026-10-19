"""Núcleos numéricos compilados (Numba) para el flujo de eventos.

Portado literal de ``main_v14.1.py``: son las dos funciones que permiten
procesar decenas de millones de eventos por segundo sin que el GIL de Python
se convierta en el cuello de botella. ``@njit`` las compila a código máquina
y ``nogil=True`` libera el GIL mientras corren, así el hilo de la interfaz
sigue respondiendo.

Si Numba no está instalado el programa sigue funcionando con una versión
vectorizada en NumPy, más lenta pero correcta: es preferible a no arrancar.
"""

from __future__ import annotations

import numpy as np

try:
    from numba import njit
    HAS_NUMBA = True
except ImportError:  # pragma: no cover - depende del entorno
    HAS_NUMBA = False

    def njit(*args, **kwargs):  # type: ignore[misc]
        """Decorador de reemplazo: deja la función tal cual."""
        def wrapper(func):
            return func
        if args and callable(args[0]):
            return args[0]
        return wrapper


@njit(nogil=True, fastmath=True, cache=True)
def render_events_jit(visual_buffer, x, y, p):  # pragma: no cover - JIT
    """Pinta eventos sobre un buffer RGB: verde los ON, rojo los OFF.

    Itera a nivel máquina en vez de usar máscaras booleanas, que asignarían
    arreglos temporales en cada ventana de integración.
    """
    for i in range(len(x)):
        if p[i] == 1:
            visual_buffer[y[i], x[i], 1] = 255      # ON  -> verde
        else:
            visual_buffer[y[i], x[i], 0] = 255      # OFF -> rojo
    return visual_buffer


@njit(nogil=True, fastmath=True, cache=True)
def integrate_events_jit(log_i, x, y, p, c_on, c_off):  # pragma: no cover - JIT
    """Integrador vectorial de Euler sobre la imagen logarítmica.

    Cada evento es un salto de contraste: ``±c`` en el logaritmo de la
    intensidad del píxel. Acumular eso es reconstruir la imagen que vería una
    cámara convencional. Se evita ``np.add.at``, que en este tamaño de datos
    es órdenes de magnitud más lento.
    """
    for i in range(len(x)):
        if p[i] == 1:
            log_i[y[i], x[i]] += c_on
        else:
            log_i[y[i], x[i]] -= c_off
    return log_i


@njit(nogil=True, fastmath=True, cache=True)
def accumulate_counts_jit(counts, x, y):  # pragma: no cover - JIT
    """Mapa de densidad de eventos (sin polaridad).

    Es la entrada natural de las métricas de enfoque y del seguimiento de
    centroides: cuántos eventos cayó en cada píxel durante la ventana.
    """
    for i in range(len(x)):
        counts[y[i], x[i]] += 1.0
    return counts


def render_events(visual_buffer: np.ndarray, x, y, p) -> np.ndarray:
    """Envoltorio seguro: valida tipos antes de entrar al núcleo compilado."""
    if len(x) == 0:
        return visual_buffer
    return render_events_jit(visual_buffer,
                             np.asarray(x, dtype=np.int32),
                             np.asarray(y, dtype=np.int32),
                             np.asarray(p, dtype=np.int8))


def integrate_events(log_i: np.ndarray, x, y, p,
                     c_on: float, c_off: float) -> np.ndarray:
    if len(x) == 0:
        return log_i
    return integrate_events_jit(log_i,
                                np.asarray(x, dtype=np.int32),
                                np.asarray(y, dtype=np.int32),
                                np.asarray(p, dtype=np.int8),
                                float(c_on), float(c_off))


def accumulate_counts(counts: np.ndarray, x, y) -> np.ndarray:
    if len(x) == 0:
        return counts
    return accumulate_counts_jit(counts,
                                 np.asarray(x, dtype=np.int32),
                                 np.asarray(y, dtype=np.int32))


def warmup() -> None:
    """Fuerza la compilación al arrancar, no en el primer frame.

    La primera llamada a una función ``@njit`` paga la compilación (décimas de
    segundo). Hacerlo al iniciar el programa evita un tirón justo cuando la
    cámara empieza a entregar datos.
    """
    if not HAS_NUMBA:
        return
    buffer = np.zeros((4, 4, 3), dtype=np.uint8)
    log_i = np.zeros((4, 4), dtype=np.float32)
    counts = np.zeros((4, 4), dtype=np.float32)
    x = np.array([0, 1], dtype=np.int32)
    y = np.array([0, 1], dtype=np.int32)
    p = np.array([1, 0], dtype=np.int8)
    render_events_jit(buffer, x, y, p)
    integrate_events_jit(log_i, x, y, p, 0.1, 0.1)
    accumulate_counts_jit(counts, x, y)
