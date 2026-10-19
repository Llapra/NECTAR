"""Las diez pestañas de NECTAR, en el orden en que se ven."""

from .base_family_tab import DevicePanel, FamilyTab
from .tab_acquisition import AcquisitionTab
from .tab_anemometer import AnemometerTab
from .tab_converter import ConverterTab
from .tab_emergent import EmergentTab
from .tab_evcam import EvCamTab
from .tab_fibers import FibersTab
from .tab_focus import FocusTab
from .tab_imu import ImuTab
from .tab_reconstruction import ReconstructionTab
from .tab_topography import TopographyTab

__all__ = [
    "DevicePanel", "FamilyTab",
    "AcquisitionTab", "EvCamTab", "EmergentTab", "ReconstructionTab",
    "FibersTab", "AnemometerTab", "ImuTab", "FocusTab", "ConverterTab",
    "TopographyTab",
]
