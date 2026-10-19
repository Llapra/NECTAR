"""Decodificación y análisis de los binarios del IMU/GPS (ESP32).

Formato del archivo
-------------------
Flujo de paquetes de 18 bytes, cada uno con una cabecera de 2 bytes que dice
qué es:

======  =============================  ==========================================
0xBBAA  ``<H L hhhhhh``                IMU: micros() + ax,ay,az,gx,gy,gz crudos
0xBBCC  ``<H L``                       PPS del GPS: micros() del flanco
0xBBDD  ``<H f f H B B B B B B``        Coordenada + fecha/hora UTC + validez
0xFFEE  ``<H d``                       ATM: sello de época UTC absoluta (raíz)
======  =============================  ==========================================

Las escalas (``4096 LSB/g`` y ``65.5 LSB/(°/s)``) corresponden al rango
configurado en el firmware: ±8 g y ±500 °/s.

El paquete ATM es la pieza que ancla todo el archivo a UTC: lo escribe el
worker en cuanto llega el primer PPS válido, de modo que cualquier muestra
posterior se puede pasar a hora absoluta sumando su tiempo relativo.
"""

from __future__ import annotations

import datetime
import struct
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import numpy as np

from ... import config

PACKET_SIZE = config.IMU_PACKET_SIZE
ACCEL_SCALE = config.IMU_ACCEL_SCALE
GYRO_SCALE = config.IMU_GYRO_SCALE

_IMU_STRUCT = struct.Struct("<Lhhhhhh")
_PPS_STRUCT = struct.Struct("<L")
_COORD_STRUCT = struct.Struct("<ffHBBBBBB")
_ATM_STRUCT = struct.Struct("<d")


@dataclass
class ImuRecord:
    """Resultado de decodificar uno o varios archivos .bin."""

    source: str = ""
    t: np.ndarray = field(default_factory=lambda: np.empty(0))
    ax: np.ndarray = field(default_factory=lambda: np.empty(0))
    ay: np.ndarray = field(default_factory=lambda: np.empty(0))
    az: np.ndarray = field(default_factory=lambda: np.empty(0))
    gx: np.ndarray = field(default_factory=lambda: np.empty(0))
    gy: np.ndarray = field(default_factory=lambda: np.empty(0))
    gz: np.ndarray = field(default_factory=lambda: np.empty(0))
    pps: np.ndarray = field(default_factory=lambda: np.empty(0))
    root_atm: Optional[float] = None
    packets: int = 0
    pps_count: int = 0
    discarded_bytes: int = 0
    files: List[str] = field(default_factory=list)

    @property
    def duration_s(self) -> float:
        return float(self.t[-1]) if self.t.size else 0.0

    @property
    def rate_hz(self) -> float:
        return self.packets / self.duration_s if self.duration_s > 0 else 0.0

    @property
    def empty(self) -> bool:
        return self.packets == 0 and self.pps_count == 0

    def utc_of(self, t_rel: float) -> Optional[float]:
        return None if self.root_atm is None else self.root_atm + t_rel

    def summary_lines(self) -> List[str]:
        lines = [
            f"Archivos leídos: {len(self.files)}",
            f"Paquetes IMU: {self.packets}",
            f"Pulsos PPS: {self.pps_count}",
            f"Bytes descartados: {self.discarded_bytes}",
            f"Duración: {self.duration_s:.2f} s",
            f"Frecuencia media: {self.rate_hz:.1f} Hz",
        ]
        if self.root_atm is not None:
            stamp = datetime.datetime.fromtimestamp(
                self.root_atm, tz=datetime.timezone.utc)
            lines.append(f"Sello ATM (raíz UTC): {stamp:%Y-%m-%d %H:%M:%S} UTC")
        else:
            lines.append("Sello ATM: ausente (tiempos relativos)")
        return lines


def decode_stream(raw: bytes, record: ImuRecord,
                  time_offset: float = 0.0,
                  t0_override: Optional[int] = None) -> float:
    """Decodifica un bloque de bytes acumulando sobre ``record``.

    Devuelve el último tiempo relativo visto, para poder concatenar el bloque
    siguiente de una carpeta con varios archivos sin que el eje temporal se
    reinicie en cada uno.
    """
    t_list, ax, ay, az = [], [], [], []
    gx, gy, gz, pps = [], [], [], []

    t0 = t0_override
    index = 0
    limit = len(raw) - PACKET_SIZE
    discarded = 0
    packets = 0
    pps_count = 0
    last_t = time_offset

    while index <= limit:
        header = int.from_bytes(raw[index:index + 2], "little")

        if header == config.IMU_HEADER_IMU:
            micros, rax, ray, raz, rgx, rgy, rgz = _IMU_STRUCT.unpack_from(
                raw, index + 2)
            if t0 is None:
                t0 = micros
            t_rel = (micros - t0) / 1e6 + time_offset
            t_list.append(t_rel)
            ax.append(rax / ACCEL_SCALE)
            ay.append(ray / ACCEL_SCALE)
            az.append(raz / ACCEL_SCALE)
            gx.append(rgx / GYRO_SCALE)
            gy.append(rgy / GYRO_SCALE)
            gz.append(rgz / GYRO_SCALE)
            packets += 1
            last_t = t_rel
            index += PACKET_SIZE

        elif header == config.IMU_HEADER_GPS:
            (micros,) = _PPS_STRUCT.unpack_from(raw, index + 2)
            if t0 is None:
                t0 = micros
            t_rel = (micros - t0) / 1e6 + time_offset
            pps.append(t_rel)
            pps_count += 1
            index += PACKET_SIZE

        elif header == config.IMU_HEADER_COORD:
            index += PACKET_SIZE      # la posición no entra en las series

        elif header == config.IMU_HEADER_ATM:
            (unix_ts,) = _ATM_STRUCT.unpack_from(raw, index + 2)
            if record.root_atm is None:
                record.root_atm = unix_ts
            index += PACKET_SIZE

        else:
            # Resincronización: saltar al próximo candidato a cabecera en vez
            # de avanzar byte a byte.
            nxt = min((pos for pos in (
                raw.find(b"\xaa\xbb", index + 1),
                raw.find(b"\xcc\xbb", index + 1),
                raw.find(b"\xdd\xbb", index + 1),
                raw.find(b"\xee\xff", index + 1),
            ) if pos != -1), default=-1)
            if nxt == -1:
                discarded += len(raw) - index
                break
            discarded += nxt - index
            index = nxt

    def _append(existing: np.ndarray, values: list) -> np.ndarray:
        block = np.asarray(values, dtype=np.float64)
        return block if existing.size == 0 else np.concatenate([existing, block])

    record.t = _append(record.t, t_list)
    record.ax = _append(record.ax, ax)
    record.ay = _append(record.ay, ay)
    record.az = _append(record.az, az)
    record.gx = _append(record.gx, gx)
    record.gy = _append(record.gy, gy)
    record.gz = _append(record.gz, gz)
    record.pps = _append(record.pps, pps)
    record.packets += packets
    record.pps_count += pps_count
    record.discarded_bytes += discarded
    return last_t


def decode_file(path: str | Path, record: Optional[ImuRecord] = None,
                time_offset: float = 0.0) -> ImuRecord:
    """Decodifica un único .bin."""
    path = Path(path)
    record = record or ImuRecord(source=str(path))
    with open(path, "rb") as handle:
        raw = handle.read()
    decode_stream(raw, record, time_offset=time_offset)
    record.files.append(path.name)
    return record


def decode_folder(folder: str | Path, pattern: str = "*.bin",
                  progress: Optional[Callable[[int, int, str], None]] = None,
                  continuous_time: bool = True) -> ImuRecord:
    """Decodifica una carpeta completa de bloques, en orden.

    Es lo que hace falta para una toma real: la grabación se parte en bloques
    de N minutos y el análisis quiere la serie completa. Con
    ``continuous_time`` los bloques se encadenan en un solo eje temporal.
    """
    folder = Path(folder)
    files = sorted(folder.glob(pattern))
    record = ImuRecord(source=str(folder))
    offset = 0.0
    for position, path in enumerate(files, start=1):
        if progress:
            progress(position, len(files), path.name)
        with open(path, "rb") as handle:
            raw = handle.read()
        last = decode_stream(raw, record, time_offset=offset)
        record.files.append(path.name)
        if continuous_time:
            offset = last
    return record


# ---------------------------------------------------------------------------
# Análisis de vibración (reemplaza al gráfico de deriva temporal)
# ---------------------------------------------------------------------------
@dataclass
class VibrationAnalysis:
    """Lo que se grafica en la pestaña 9.

    La deriva del reloj era un dato de puesta en marcha que ya no aporta nada
    en campaña. Lo que sí importa en una cúpula es la vibración mecánica: si
    la estructura o el banco óptico está vibrando a alguna frecuencia, eso
    contamina el wander de los spots y hay que poder verlo y descontarlo.
    """

    freqs: np.ndarray = field(default_factory=lambda: np.empty(0))
    psd: np.ndarray = field(default_factory=lambda: np.empty(0))
    rms_t: np.ndarray = field(default_factory=lambda: np.empty(0))
    rms: np.ndarray = field(default_factory=lambda: np.empty(0))
    sample_rate: float = 0.0
    dominant_hz: float = 0.0
    dominant_amplitude: float = 0.0
    rms_global: float = 0.0
    peaks: List[Dict[str, float]] = field(default_factory=list)

    @property
    def valid(self) -> bool:
        return self.freqs.size > 0


def analyze_vibration(record: ImuRecord, window_s: float = 1.0,
                      max_points: int = 1 << 20) -> VibrationAnalysis:
    """Densidad espectral de potencia y RMS por ventana de la aceleración.

    Se trabaja sobre el módulo de aceleración menos su media (se descuenta la
    gravedad y cualquier inclinación estática), que es la señal de vibración
    propiamente dicha.
    """
    result = VibrationAnalysis()
    if record.t.size < 16:
        return result

    t = record.t[:max_points]
    ax = record.ax[:max_points]
    ay = record.ay[:max_points]
    az = record.az[:max_points]

    span = float(t[-1] - t[0])
    if span <= 0:
        return result
    fs = (t.size - 1) / span
    result.sample_rate = fs

    magnitude = np.sqrt(ax * ax + ay * ay + az * az)
    signal = magnitude - float(np.mean(magnitude))
    result.rms_global = float(np.sqrt(np.mean(signal ** 2)))

    # --- PSD por promediado de Welch, a mano para no depender de SciPy ------
    segment = int(min(max(256, fs * 4), signal.size))
    segment = 1 << int(np.floor(np.log2(max(segment, 16))))
    step = max(segment // 2, 1)
    window = np.hanning(segment)
    scale = 1.0 / (fs * float(np.sum(window ** 2)))

    accumulator = np.zeros(segment // 2 + 1, dtype=np.float64)
    blocks = 0
    for start in range(0, signal.size - segment + 1, step):
        chunk = signal[start:start + segment] * window
        spectrum = np.fft.rfft(chunk)
        accumulator += (np.abs(spectrum) ** 2) * scale
        blocks += 1
    if blocks == 0:
        return result
    psd = accumulator / blocks
    psd[1:-1] *= 2.0                      # energía del lado negativo

    freqs = np.fft.rfftfreq(segment, d=1.0 / fs)
    result.freqs = freqs
    result.psd = psd

    if freqs.size > 2:
        # Se ignora la componente continua y el primer bin (deriva lenta).
        search = psd[2:]
        peak_index = int(np.argmax(search)) + 2
        result.dominant_hz = float(freqs[peak_index])
        result.dominant_amplitude = float(np.sqrt(psd[peak_index] * (freqs[1] - freqs[0])))

        # Picos ordenados: sirven para nombrar modos mecánicos.
        order = np.argsort(search)[::-1][:5]
        seen: List[float] = []
        for raw_index in order:
            index = int(raw_index) + 2
            frequency = float(freqs[index])
            if any(abs(frequency - previous) < (freqs[1] - freqs[0]) * 3
                   for previous in seen):
                continue
            seen.append(frequency)
            result.peaks.append({
                "hz": frequency,
                "psd": float(psd[index]),
                "g_rms": float(np.sqrt(psd[index] * (freqs[1] - freqs[0]))),
            })

    # --- RMS deslizante ----------------------------------------------------
    samples = max(int(window_s * fs), 8)
    usable = (signal.size // samples) * samples
    if usable >= samples:
        blocks_2d = signal[:usable].reshape(-1, samples)
        result.rms = np.sqrt(np.mean(blocks_2d ** 2, axis=1))
        result.rms_t = t[:usable:samples][:result.rms.size]
    return result


# ---------------------------------------------------------------------------
# Exportación
# ---------------------------------------------------------------------------
SUPPORTED_FORMATS = ["csv", "dat", "mat", "npz", "parquet"]

COLUMNS = ["t_rel_s", "t_unix_s", "t_utc", "ax_g", "ay_g", "az_g",
           "gx_dps", "gy_dps", "gz_dps"]


def export_record(record: ImuRecord, path: str | Path, fmt: str = "csv",
                  include_utc: bool = True) -> Path:
    """Escribe el registro decodificado en el formato pedido.

    ``csv`` y ``dat`` (texto delimitado por tabulaciones) no necesitan nada
    extra; ``mat`` usa SciPy, ``parquet`` usa pandas + pyarrow. Si falta la
    dependencia, se avisa con un error claro en vez de fallar a medias.
    """
    path = Path(path)
    fmt = fmt.lower().lstrip(".")
    if fmt not in SUPPORTED_FORMATS:
        raise ValueError(f"Formato no soportado: {fmt}")
    path.parent.mkdir(parents=True, exist_ok=True)

    unix = (record.root_atm + record.t) if record.root_atm is not None \
        else np.full(record.t.shape, np.nan)

    if fmt in ("csv", "dat"):
        delimiter = "," if fmt == "csv" else "\t"
        with open(path, "w", encoding="utf-8", newline="") as handle:
            handle.write(delimiter.join(COLUMNS) + "\n")
            for i in range(record.t.size):
                if record.root_atm is not None and include_utc:
                    stamp = datetime.datetime.fromtimestamp(
                        float(unix[i]), tz=datetime.timezone.utc)
                    human = stamp.strftime("%Y-%m-%d %H:%M:%S.%f")
                    absolute = f"{unix[i]:.6f}"
                else:
                    human, absolute = "", ""
                handle.write(delimiter.join([
                    f"{record.t[i]:.6f}", absolute, human,
                    f"{record.ax[i]:.6f}", f"{record.ay[i]:.6f}", f"{record.az[i]:.6f}",
                    f"{record.gx[i]:.4f}", f"{record.gy[i]:.4f}", f"{record.gz[i]:.4f}",
                ]) + "\n")
        return path

    if fmt == "npz":
        np.savez_compressed(path, t=record.t, t_unix=unix,
                            ax=record.ax, ay=record.ay, az=record.az,
                            gx=record.gx, gy=record.gy, gz=record.gz,
                            pps=record.pps,
                            root_atm=np.array([record.root_atm
                                               if record.root_atm else np.nan]))
        return path

    if fmt == "mat":
        try:
            from scipy.io import savemat
        except ImportError as exc:
            raise RuntimeError("Exportar a .mat necesita SciPy "
                               "(pip install scipy)") from exc
        savemat(str(path), {
            "t": record.t, "t_unix": unix,
            "ax": record.ax, "ay": record.ay, "az": record.az,
            "gx": record.gx, "gy": record.gy, "gz": record.gz,
            "pps": record.pps,
            "root_atm": record.root_atm if record.root_atm else np.nan,
        })
        return path

    if fmt == "parquet":
        try:
            import pandas as pd
        except ImportError as exc:
            raise RuntimeError("Exportar a .parquet necesita pandas y pyarrow "
                               "(pip install pandas pyarrow)") from exc
        frame = pd.DataFrame({
            "t_rel_s": record.t, "t_unix_s": unix,
            "ax_g": record.ax, "ay_g": record.ay, "az_g": record.az,
            "gx_dps": record.gx, "gy_dps": record.gy, "gz_dps": record.gz,
        })
        frame.to_parquet(path, index=False)
        return path

    raise ValueError(fmt)
