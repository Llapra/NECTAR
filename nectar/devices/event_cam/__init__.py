"""Cámara neuromórfica de eventos (Prophesee / Metavision · SilkyEvCam)."""

from .biases import SHARED_BIASES, BiasSet
from .simulation import EventSimulator, SimConfig
from .worker import HAS_METAVISION, EventCamWorker, discover_event_cams

__all__ = ["SHARED_BIASES", "BiasSet", "EventSimulator", "SimConfig",
           "HAS_METAVISION", "EventCamWorker", "discover_event_cams"]
