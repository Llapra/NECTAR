import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import unittest
from unittest.mock import Mock

import numpy as np
from PyQt6.QtWidgets import QApplication

from nectar.core.frame_hub import FrameHub
from nectar.core.pixels import to_display_u8
from nectar.devices.emergent.api import ParamInfo, PixelFormat
from nectar.devices.emergent.worker import EmergentWorker, EVT_ERROR_TIMEDOUT
from nectar.devices.base import DeviceState
from nectar.ui.widgets.common import SliderSpin
from nectar.ui.tabs.tab_emergent import EmergentTab


class EmergentControlsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def worker(self, restart=False):
        worker = EmergentWorker("test", "test")
        worker._camera = Mock()
        worker._camera.param_info.return_value = ParamInfo("Gain", 4, 4, 20, 4)
        worker._param_cache = [dict(name="Gain", kind="uint32", restart=restart)]
        worker._read_params = Mock()
        return worker

    def test_live_control_is_applied_even_without_frames(self):
        worker = self.worker()
        worker.set_param("Gain", 12)

        def timeout(_):
            worker._stop_event.set()
            return EVT_ERROR_TIMEDOUT, None

        worker._camera.get_frame.side_effect = timeout
        worker._grab_loop()
        worker._camera.set_uint32.assert_called_once_with("Gain", 12)
        worker._read_params.assert_called_once()

    def test_recording_geometry_comes_from_received_frame(self):
        worker = self.worker()
        worker._set_state(DeviceState.STREAMING)
        worker._geometry = dict(frame_bytes=0)
        self.assertFalse(worker.is_armable)
        frame = Mock(width=8, height=4, stride=8, pixel_format=PixelFormat.MONO8,
                     frame_id=1, timestamp=123)
        frame.buffer_view.return_value = np.zeros(32, dtype=np.uint8)
        worker._camera.get_frame.return_value = (0, frame)
        worker._camera.queue_frame.side_effect = lambda _: worker._stop_event.set()
        worker._service_recording = Mock()
        worker._grab_loop()
        self.assertEqual(worker._geometry["frame_bytes"], 32)
        self.assertTrue(worker.is_armable)

    def test_rejects_invalid_or_newly_out_of_range_values(self):
        for value in (3, 9, 24):
            worker = self.worker()
            worker.set_param("Gain", value)
            worker._apply_pending()
            worker._camera.set_uint32.assert_not_called()
            worker._read_params.assert_called_once()

    def test_restart_control_waits_until_stream_is_stopped(self):
        worker = self.worker(restart=True)
        worker.set_param("Gain", 12)
        worker._grab_loop()
        worker._camera.set_uint32.assert_not_called()
        worker._apply_pending()
        worker._camera.set_uint32.assert_called_once_with("Gain", 12)

    def test_slider_and_typed_values_follow_camera_increment(self):
        control = SliderSpin(4, 23, 4, step=4, snap_to_step=True)
        values = []
        control.value_changed.connect(values.append)
        control.slider.setValue(11)
        self.assertEqual(control.value(), 8)
        control.spin.setValue(23)
        self.assertEqual(control.value(), 20)
        self.assertEqual(values, [8, 20])

    def test_uint32_control_supports_bandwidth_above_signed_int(self):
        tab = Mock()
        widget = EmergentTab._build_param_widget(tab, dict(
            name="StreamBytesPerSecond", kind="uint32", minimum=1000000,
            maximum=3000000000, increment=4, value=1000000000))
        widget.setValue(2999999999)
        tab._apply_param.assert_called_once_with("StreamBytesPerSecond", 2999999996)

    def test_preview_is_limited_to_requested_rate(self):
        for fps in (20, 30):
            hub = FrameHub(fps)
            times = [i / 600 for i in range(600)]
            count = sum(hub.wants_frame(now=t) for t in times)
            self.assertLessEqual(count, fps + 1)
            self.assertGreaterEqual(count, fps - 1)

    def test_fixed_preview_scale_preserves_brightness_changes(self):
        raw = np.full(64, 256, dtype=np.uint16)
        low = to_display_u8(raw.view(np.uint8), 8, 8, PixelFormat.MONO12)
        high = to_display_u8((raw * 2).view(np.uint8), 8, 8, PixelFormat.MONO12)
        self.assertEqual(int(high.mean()), 2 * int(low.mean()))


if __name__ == "__main__":
    unittest.main()
