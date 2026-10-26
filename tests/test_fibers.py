import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import unittest
import io
import csv
from unittest.mock import Mock, patch
import numpy as np
from PyQt6.QtWidgets import QApplication

from nectar.core.time_sync import TIME_BASE
from nectar.devices.base import DeviceState
from nectar.devices.fibers import protocol
from nectar.devices.fibers.parser import FiberParser
from nectar.devices.fibers.worker import FiberWorker
from nectar.ui.tabs.tab_fibers import FiberPanel


class FiberTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_fragmented_stream_and_invalid_temperature(self):
        parser = FiberParser()
        payload = b"".join(protocol.PACKET.pack(protocol.slot_channel(i)[0], 0,
                             protocol.slot_channel(i)[1], 0,
                             20.0 + i if i < 4 else 65535.0) for i in range(6))
        for offset in range(0, len(payload), 3):
            parser.feed(payload[offset:offset + 3], 123)
        self.assertEqual(parser.total_samples, 6)
        self.assertEqual(len(parser.channels), 6)
        self.assertEqual(parser.channels[3].last_value, 23)
        self.assertTrue(np.isnan(parser.channels[4].last_value))
        self.assertIsNone(parser.channels[4].as_dict()["min"])

    def test_invalid_reading_does_not_poison_subsequent_statistics(self):
        parser = FiberParser()
        for value in (65535.0, 22.0, 24.0):
            parser.feed(protocol.PACKET.pack(1, 0, 1, 0, value), 123)
        self.assertEqual(parser.channels[0].mean, 23.0)

    def test_rate_uses_seconds_and_requires_confirmation(self):
        self.assertEqual(protocol.cmd_rate(1000), b"rate 0.001\n")
        worker = FiberWorker("test", "Fibras")
        worker._ask = Mock(side_effect=["", "0.002"])
        worker._configure_rate(500)
        self.assertEqual(worker.sampling_rate, 500)
        worker._ask = Mock(return_value="Err -170")
        with self.assertRaises(RuntimeError):
            worker._configure_rate(1000)
        self.assertEqual(worker.sampling_rate, 500)

    def test_control_keeps_multiple_responses_from_one_tcp_packet(self):
        worker = FiberWorker("test", "Fibras")
        worker._control = Mock()
        worker._control.recv.side_effect = [b"\x04\r\n0.001\x04\n"]
        self.assertEqual(worker._read_response(), "")
        self.assertEqual(worker._read_response(), "0.001")

    def test_rate_skips_late_empty_ack(self):
        worker = FiberWorker("test", "Fibras")
        worker._ask = Mock(side_effect=["", ""])
        worker._read_response = Mock(return_value="0.001")
        worker._configure_rate(1000)
        self.assertEqual(worker.sampling_rate, 1000)

    def test_preview_does_not_write_measurements(self):
        worker = FiberWorker("test", "Fibras")
        with patch.object(worker, "_open_chunk") as open_chunk:
            worker._service_recording(1)
            worker._record_samples(b"12345678", [(0, 20.0, 1)], 1)
            open_chunk.assert_not_called()
        self.assertIsNone(worker._csv_handle)
        self.assertIsNone(worker._raw_writer)

    def test_recording_preserves_grouped_channels_without_false_sweeps(self):
        worker = FiberWorker("test", "Fibras")
        worker._recording = True
        worker._csv_handle = io.StringIO()
        worker._raw_writer = Mock()
        samples = [(0, 21.1, 100), (0, 21.2, 100),
                   (1, float("nan"), 100), (1, float("nan"), 100),
                   (0, 21.3, 200)]
        worker._record_samples(b"raw", samples, 100)
        rows = list(csv.reader(io.StringIO(worker._csv_handle.getvalue())))
        self.assertEqual(len(rows), 5)
        self.assertEqual([r[4] for r in rows], ["1", "1", "2", "2", "1"])
        self.assertEqual([r[5] for r in rows], ["0", "1", "0", "1", "2"])
        self.assertEqual([r[6] for r in rows], ["21.1", "21.2", "", "", "21.3"])
        self.assertEqual([r[7] for r in rows], ["1", "1", "0", "0", "1"])
        self.assertEqual(worker._rows_written, 5)
        worker._raw_writer.submit.assert_called_once_with(b"raw")

    def test_stale_values_are_not_presented_as_current_temperature(self):
        worker = FiberWorker("test", "Fibras")
        worker.parser.feed(protocol.PACKET.pack(1, 0, 1, 0, 22),
                           TIME_BASE.now_ns() - 4_000_000_000)
        channel = worker._stats_payload()["canales"][0]
        self.assertIsNone(channel["valor"])
        self.assertEqual(channel["estado"], "Sin datos recientes")

    def test_panel_has_six_rows_before_first_sample(self):
        worker = FiberWorker("test", "Fibras")
        panel = FiberPanel(worker)
        self.assertEqual(len(panel._rows), 6)
        panel.refresh_stats(worker._stats_payload())
        worker._set_state(DeviceState.ERROR_RECOVERABLE)
        panel.refresh_state()
        self.assertEqual(panel._rows[0].status.text(), "Sin conexión")
        panel.close()


if __name__ == "__main__":
    unittest.main()
