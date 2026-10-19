"""Generador de eventos sintéticos para la cámara neuromórfica.

``main_v14.1.py`` tenía un modo simulación que en realidad no generaba nada
(``print`` y ``sleep``), así que sin hardware no se podía probar ni la
interfaz ni el disparo síncrono. Aquí el simulador genera de verdad un flujo
poissoniano de eventos sobre un blanco parecido al real: una grilla de
10 x 10 manchas retrorreflectantes con deriva (el *spot wander* que produce la
turbulencia) y un borde de damero.

No pretende ser físicamente exacto: pretende que la pestaña, las métricas de
enfoque, el renderizado JIT y la grabación se puedan ejercitar completos.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Optional, Tuple

import numpy as np


@dataclass
class SimConfig:
    width: int = 640
    height: int = 480
    grid: int = 10                  # manchas por lado
    spot_sigma: float = 2.2         # radio de la mancha, en píxeles
    wander_px: float = 1.8          # amplitud del wander (turbulencia)
    wander_hz: float = 3.0
    base_rate: float = 2.5e6        # eventos/s con el blanco enfocado
    noise_rate: float = 1.2e5       # ruido de fondo, eventos/s
    defocus: float = 0.0            # 0 = enfocado; >0 ensancha las manchas


class EventSimulator:
    """Produce lotes de eventos con la misma estructura que Metavision.

    Devuelve un ``np.ndarray`` estructurado con campos ``x``, ``y``, ``p``,
    ``t``, igual que el ``EventsIterator`` real, así el worker no necesita
    saber si está contra hardware o contra el simulador.
    """

    DTYPE = np.dtype([("x", np.uint16), ("y", np.uint16),
                      ("p", np.int16), ("t", np.int64)])

    def __init__(self, config: Optional[SimConfig] = None,
                 seed: Optional[int] = None) -> None:
        self.cfg = config or SimConfig()
        self._rng = np.random.default_rng(seed)
        self._t0 = time.perf_counter()
        self._spots = self._build_spots()
        self.sensitivity = 1.0      # la mueve el bias_diff_on
        self._roi: Optional[Tuple[int, int, int, int]] = None

    # ------------------------------------------------------------------
    def _build_spots(self) -> np.ndarray:
        """Centros de la grilla 10 x 10, con margen respecto al borde."""
        cfg = self.cfg
        margin_x = cfg.width * 0.15
        margin_y = cfg.height * 0.15
        xs = np.linspace(margin_x, cfg.width - margin_x, cfg.grid)
        ys = np.linspace(margin_y, cfg.height - margin_y, cfg.grid)
        grid_x, grid_y = np.meshgrid(xs, ys)
        return np.stack([grid_x.ravel(), grid_y.ravel()], axis=1)

    def set_defocus(self, value: float) -> None:
        """0 = enfocado. Al desenfocar se ensancha la mancha y sube la tasa.

        Es el comportamiento real del sensor: desenfocado genera *más*
        eventos, porque el disco de Airy reparte cada transición sobre más
        píxeles. Sirve para probar la pestaña 8 sin lente.
        """
        self.cfg.defocus = max(0.0, float(value))

    def resize(self, width: int, height: int) -> None:
        self.cfg.width = int(width)
        self.cfg.height = int(height)
        self._spots = self._build_spots()

    # ------------------------------------------------------------------
    def set_roi(self, x: int, y: int, width: int, height: int) -> None:
        """Imita el recorte del sensor: fuera del ROI no se generan eventos.

        Importa que el simulador se comporte igual que el hardware en esto,
        porque si no el ensayo del modo ``--demo`` enseñaría algo distinto de
        lo que va a pasar en la cúpula. Las coordenadas siguen siendo
        absolutas, como en el sensor real.
        """
        self._roi = (int(x), int(y), int(width), int(height))

    def clear_roi(self) -> None:
        self._roi = None

    def _apply_roi(self, events: np.ndarray) -> np.ndarray:
        roi = getattr(self, "_roi", None)
        if roi is None or events.size == 0:
            return events
        x0, y0, width, height = roi
        inside = ((events["x"] >= x0) & (events["x"] < x0 + width) &
                  (events["y"] >= y0) & (events["y"] < y0 + height))
        return events[inside]

    # ------------------------------------------------------------------
    def generate(self, delta_t_us: int) -> np.ndarray:
        """Lote de eventos correspondiente a una ventana de ``delta_t_us``."""
        cfg = self.cfg
        elapsed = time.perf_counter() - self._t0
        delta_s = delta_t_us / 1e6

        # Desenfocar ensancha la mancha y multiplica la tasa.
        sigma = cfg.spot_sigma * (1.0 + cfg.defocus * 3.0)
        rate = cfg.base_rate * (1.0 + cfg.defocus * 2.0) * self.sensitivity
        n_spot = self._rng.poisson(rate * delta_s)
        n_noise = self._rng.poisson(cfg.noise_rate * delta_s)
        total = int(n_spot + n_noise)
        if total <= 0:
            return np.empty(0, dtype=self.DTYPE)

        events = np.empty(total, dtype=self.DTYPE)

        # --- eventos de las manchas ------------------------------------
        if n_spot > 0:
            which = self._rng.integers(0, len(self._spots), size=n_spot)
            centers = self._spots[which]
            # Wander común (movimiento global del frente de onda) más un
            # término independiente por mancha: así las correlaciones
            # espaciales no son triviales, como en la turbulencia real.
            phase = 2 * np.pi * cfg.wander_hz * elapsed
            common = np.array([np.sin(phase), np.cos(phase * 0.83)]) * cfg.wander_px
            jitter = self._rng.normal(0.0, cfg.wander_px * 0.4, size=(n_spot, 2))
            spread = self._rng.normal(0.0, sigma, size=(n_spot, 2))
            positions = centers + common + jitter + spread
            events["x"][:n_spot] = np.clip(positions[:, 0], 0, cfg.width - 1)
            events["y"][:n_spot] = np.clip(positions[:, 1], 0, cfg.height - 1)
            events["p"][:n_spot] = self._rng.integers(0, 2, size=n_spot)

        # --- ruido de fondo --------------------------------------------
        if n_noise > 0:
            events["x"][n_spot:] = self._rng.integers(0, cfg.width, size=n_noise)
            events["y"][n_spot:] = self._rng.integers(0, cfg.height, size=n_noise)
            events["p"][n_spot:] = self._rng.integers(0, 2, size=n_noise)

        # --- marcas de tiempo en microsegundos, ordenadas ---------------
        base_us = int(elapsed * 1e6)
        offsets = np.sort(self._rng.integers(0, max(delta_t_us, 1), size=total))
        events["t"] = base_us + offsets
        return self._apply_roi(events)

    # ------------------------------------------------------------------
    def to_raw_bytes(self, events: np.ndarray) -> bytes:
        """Serializa el lote para poder grabarlo en modo simulado.

        Formato ``NECTAR_SIM_EV1``: registros de 12 bytes
        ``<uint16 x><uint16 y><int16 p><int16 relleno><int32 t_us>``.
        No es EVT3: es un formato propio, declarado en el encabezado del
        archivo, para que nadie confunda una grabación simulada con una real.
        """
        if events.size == 0:
            return b""
        block = np.empty(events.size, dtype=np.dtype([
            ("x", np.uint16), ("y", np.uint16), ("p", np.int16),
            ("pad", np.int16), ("t", np.int32)]))
        block["x"] = events["x"]
        block["y"] = events["y"]
        block["p"] = events["p"]
        block["pad"] = 0
        block["t"] = (events["t"] - events["t"][0]).astype(np.int32)
        return block.tobytes()

    @staticmethod
    def raw_header(width: int, height: int) -> bytes:
        return (f"NECTAR_SIM_EV1 width={width} height={height} "
                f"record=12B:x_u16,y_u16,p_i16,pad_i16,t_i32_us\n").encode()

    # ------------------------------------------------------------------
    @property
    def geometry(self) -> Tuple[int, int]:
        return self.cfg.width, self.cfg.height
