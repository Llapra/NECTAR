"""Algoritmos acelerados: render de eventos, métricas de foco, IMU."""

from .focus_metrics import (FocusAdvice, FocusEngine, FocusReport,
                            FocusTracker, event_polarity_ratio, event_rate,
                            hfe_2d_dft, hfe_energy_db,
                            normalize_log, tenengrad)
from .jit_render import (HAS_NUMBA, accumulate_counts, integrate_events,
                         render_events, warmup)

__all__ = [
    "FocusAdvice", "FocusEngine", "FocusReport", "FocusTracker",
    "event_polarity_ratio", "event_rate", "hfe_2d_dft", "hfe_energy_db",
    "normalize_log", "tenengrad",
    "HAS_NUMBA", "accumulate_counts", "integrate_events", "render_events",
    "warmup",
]
