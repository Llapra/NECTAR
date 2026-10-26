"""Regresiones para el lector entregado en el notebook, sin ejecutar sus visores."""
import ast
import contextlib
import io
from pathlib import Path
import tempfile
import unittest

import numpy as np
import pandas as pd

from nectar.devices.fibers.protocol import PACKET


class AnalysisBinaryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        source = Path(__file__).resolve().parents[1] / "analisis/revisar_medicion.py"
        module = ast.parse(source.read_text(encoding="utf-8"))
        function = next(n for n in module.body if isinstance(n, ast.FunctionDef)
                        and n.name == "comprobar_binario_fibras")
        isolated = ast.Module(body=[function], type_ignores=[])
        scope = dict(Path=Path, np=np, pd=pd)
        exec(compile(ast.fix_missing_locations(isolated), str(source), "exec"), scope)
        cls.decode = staticmethod(scope[function.name])

    def read_payload(self, payload):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "fibers.bin"
            path.write_bytes(payload)
            with contextlib.redirect_stdout(io.StringIO()):
                return self.decode(path)

    def test_aligned_and_partial_record_at_start_or_end(self):
        records = b"".join(PACKET.pack(1, 0, 1, 0, value)
                           for value in (21.0, 21.1, 21.2, 21.3))
        for prefix, suffix in ((b"", b""), (b"\x00\xff\x7f\x47", b""),
                               (b"", b"\x01\x00\x01\x00")):
            with self.subTest(prefix=prefix, suffix=suffix):
                result = self.read_payload(prefix + records + suffix)
                np.testing.assert_allclose(result.temperatura_C, [21, 21.1, 21.2, 21.3])

    def test_internal_corruption_is_not_silently_discarded(self):
        record = PACKET.pack(1, 0, 1, 0, 21)
        with self.assertRaises(ValueError):
            self.read_payload(record*4 + b"x" + record*4)
