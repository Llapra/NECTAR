"""Piezas de interfaz reutilizables, compartidas por las diez pestañas."""

from .common import (BigValue, DeviceSelectorRow, LabeledControl, LogConsole,
                     Placeholder, SavePathSelector, ScrollColumn, Section,
                     SliderSpin, ValueRow)
from .device_card import DeviceRow, FamilyCard
from .image_view import HAS_PYQTGRAPH, ImageVisor
from .led import LedIndicator
from .mini_preview import MiniPreview, MiniPreviewGrid
from .plots import BarPlot, TimeSeriesPlot, color_for

__all__ = [
    "BigValue", "DeviceSelectorRow", "LabeledControl", "LogConsole",
    "Placeholder", "SavePathSelector", "ScrollColumn", "Section", "SliderSpin",
    "ValueRow", "DeviceRow", "FamilyCard", "ImageVisor", "HAS_PYQTGRAPH",
    "LedIndicator", "MiniPreview", "MiniPreviewGrid", "BarPlot",
    "TimeSeriesPlot", "color_for",
]
