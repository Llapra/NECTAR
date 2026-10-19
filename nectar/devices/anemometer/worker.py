"""Worker del termo-anemómetro ultrasónico de tiempo de vuelo (ToF).

Estado: **el instrumento todavía no existe** — se está construyendo sobre una
FPGA Arty S7 (Spartan-7) que emite pulsos acústicos a 32,8 kHz y mide el
tiempo de vuelo por UART. Así que este módulo deja la estructura completa
(descubrimiento, conexión, frecuencia de muestreo, grabación, participación en
el disparo síncrono) y deliberadamente **no** fija un formato de paquete
binario que después no calce con el firmware.

Para no quedar atado a una suposición, soporta tres modos de ingesta:

``crudo``
    Vuelca al disco exactamente los bytes que llegan por el puerto, sin
    interpretar nada. Es el modo seguro: sirva el formato que sirva, los datos
    quedan guardados y se pueden re-parsear después.

``lineas``
    Espera líneas de texto terminadas en ``\\n`` (por ejemplo
    ``t_up_ns,t_down_ns`` o ``tof_ns,temp_c``). Es lo que suele emitir un
    prototipo de FPGA en su primera versión, y permite graficar en vivo sin
    esperar a tener un protocolo binario definitivo.

``binario``
    Paquetes de tamaño fijo (``config.ANEMO_PACKET_SIZE``) que se guardan y se
    cuentan. Cuando el firmware defina su estructura, solo hay que escribir el
    desempaquetado en ``parse_packet()``.

La física que se va a usar cuando haya datos
--------------------------------------------
La velocidad del sonido en aire seco depende casi solo de la temperatura::

    c = sqrt(gamma · R · T)   con gamma = 1,4 y R = 287,05 J/(kg·K)

De ahí, midiendo el tiempo de vuelo en los dos sentidos entre dos
transductores separados una distancia ``d``::

    c = (d/2) · (1/t_ida + 1/t_vuelta)        -> temperatura
    v = (d/2) · (1/t_ida - 1/t_vuelta)        -> viento a lo largo del eje

La ventaja sobre la fibra óptica es que mide la temperatura del *camino
óptico* sin inercia térmica: no hay un sensor que tenga que llegar al
equilibrio. Las funciones ``sound_speed_to_temperature`` y ``tof_to_wind``
implementan eso y ya están listas para cuando lleguen los tiempos reales.
"""

from __future__ import annotations

import math
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ... import config
from ...core.disk_stream import ByteStreamWriter
from ...core.time_sync import TIME_BASE
from .. import serial_ports
from ..base import DeviceState, DeviceWorker

try:
    import serial
    import serial.tools.list_ports
    HAS_SERIAL = True
except ImportError:
    HAS_SERIAL = False

#: Constantes del aire seco.
GAMMA = 1.4
R_AIR = 287.05
KELVIN = 273.15

MODE_RAW = "crudo"
MODE_LINES = "lineas"
MODE_BINARY = "binario"
MODES = [MODE_RAW, MODE_LINES, MODE_BINARY]


def sound_speed_to_temperature(c: float) -> float:
    """°C a partir de la velocidad del sonido (aire seco)."""
    if c <= 0:
        return float("nan")
    return (c * c) / (GAMMA * R_AIR) - KELVIN


def temperature_to_sound_speed(celsius: float) -> float:
    return math.sqrt(GAMMA * R_AIR * (celsius + KELVIN))


def tof_to_wind(t_up_s: float, t_down_s: float, distance_m: float
                ) -> Tuple[float, float]:
    """Devuelve ``(temperatura_C, viento_m_s)`` a partir de los dos ToF.

    Es la medición desacoplada: la semisuma de los inversos da la velocidad
    del sonido (y con ella la temperatura) y la semidiferencia da la
    componente de viento sobre el eje de los transductores.
    """
    if t_up_s <= 0 or t_down_s <= 0 or distance_m <= 0:
        return float("nan"), float("nan")
    c = (distance_m / 2.0) * (1.0 / t_up_s + 1.0 / t_down_s)
    wind = (distance_m / 2.0) * (1.0 / t_up_s - 1.0 / t_down_s)
    return sound_speed_to_temperature(c), wind


def discover_anemometers() -> List[Dict[str, Any]]:
    """Puertos serie **con hardware detrás**, o el motivo de que no haya.

    Mientras el instrumento esté en construcción, la unidad simulada de
    ``--demo`` es la única que de verdad entrega datos. Fuera de ``--demo`` no
    se ofrece nada que no esté enchufado: los ``/dev/ttyS*`` heredados del
    chipset quedan fuera (ver ``devices/serial_ports.py``).
    """
    if config.simulations_allowed():
        return [{
            "id": "anemo_sim",
            "label": "Anemometro1",
            "port": "SIM",
            "baudrate": config.ANEMO_DEFAULT_BAUD,
            "mode": MODE_LINES,
            "simulated": True,
        }]

    ports, hidden = serial_ports.list_ports()
    if not ports:
        nota = serial_ports.hidden_note(hidden)
        raise RuntimeError(serial_ports.no_ports_note()
                           + (f" ({nota})" if nota else ""))

    found: List[Dict[str, Any]] = []
    for index, port in enumerate(ports, start=1):
        descriptor = port.as_descriptor("anemo", index, "Anemometro")
        descriptor["baudrate"] = config.ANEMO_DEFAULT_BAUD
        descriptor["mode"] = MODE_LINES
        found.append(descriptor)
    return found


class AnemometerWorker(DeviceWorker):
    """Termo-anemómetro ToF. Estructura lista, formato por definir."""

    family = "anemometer"
    family_label = "Anemómetro"
    supports_chunking = True
    nominal_mb_s = 0.05

    def __init__(self, device_id: str, label: str,
                 descriptor: Optional[Dict[str, Any]] = None) -> None:
        super().__init__(device_id, label, descriptor)
        self.port: str = self.descriptor.get("port", "SIM")
        self.baudrate: int = int(self.descriptor.get(
            "baudrate", config.ANEMO_DEFAULT_BAUD))
        self.mode: str = self.descriptor.get("mode", MODE_LINES)
        self.simulated: bool = bool(self.descriptor.get("simulated", False))
        self.target_hz: int = config.ANEMO_DEFAULT_HZ
        #: Separación entre transductores, en metros. Se mide en el montaje.
        self.path_length_m: float = 0.20

        self._serial = None
        self._writer: Optional[ByteStreamWriter] = None
        self._chunk_path: Optional[Path] = None
        self._chunk_index = 0
        self._chunk_started = 0.0
        self._chunk_summaries: List[Dict[str, Any]] = []

        self._buffer = bytearray()
        self._samples_total = 0
        self._samples_window = 0
        self._bytes_window = 0
        self._hz = 0.0
        self._bps = 0.0
        self.last_values: Dict[str, float] = {}
        self._sim_phase = 0.0

    # ------------------------------------------------------------------
    def configure(self, port: str, baudrate: int, mode: str,
                  simulated: Optional[bool] = None) -> None:
        self.port = port
        self.baudrate = int(baudrate)
        self.mode = mode if mode in MODES else MODE_RAW
        if simulated is None:
            simulated = port == "SIM" or not HAS_SERIAL
        self.simulated = bool(simulated)

    def set_target_hz(self, hz: int) -> None:
        low, high = config.ANEMO_HZ_RANGE
        self.target_hz = int(max(low, min(high, hz)))

    def connect_device(self) -> bool:
        self.start_thread()
        return True

    def disconnect_device(self) -> None:
        self.stop_recording()
        self.stop_thread()
        self._set_state(DeviceState.DISCONNECTED)

    def estimated_mb_s(self) -> float:
        if self._bps > 0:
            return self._bps / 1e6
        return max(self.nominal_mb_s,
                   self.target_hz * config.ANEMO_PACKET_SIZE / 1e6)

    # ------------------------------------------------------------------
    def parse_packet(self, packet: bytes) -> Dict[str, float]:
        """Punto de extensión: desempaquetar el formato binario definitivo.

        Hoy solo cuenta bytes. Cuando el firmware de la Arty S7 fije su
        estructura, esta es la única función que hay que escribir; todo lo
        demás (grabación, disparo síncrono, carpetas, GUI) ya funciona.
        """
        return {}

    def parse_line(self, line: str) -> Dict[str, float]:
        """Interpreta una línea de texto del prototipo.

        Acepta valores separados por coma o punto y coma. Si vienen dos
        números se asumen los dos tiempos de vuelo en nanosegundos y se
        calculan temperatura y viento; si viene uno solo se guarda como ToF.
        """
        cleaned = line.strip().replace(";", ",")
        if not cleaned or cleaned.startswith("#"):
            return {}
        pieces = [piece for piece in cleaned.split(",") if piece.strip()]
        numbers: List[float] = []
        for piece in pieces:
            try:
                numbers.append(float(piece))
            except ValueError:
                continue
        if not numbers:
            return {}

        values: Dict[str, float] = {}
        if len(numbers) >= 2:
            t_up_s = numbers[0] * 1e-9
            t_down_s = numbers[1] * 1e-9
            temperature, wind = tof_to_wind(t_up_s, t_down_s, self.path_length_m)
            values.update(tof_ida_ns=numbers[0], tof_vuelta_ns=numbers[1],
                          temperatura_c=temperature, viento_m_s=wind)
            if len(numbers) >= 3:
                values["extra"] = numbers[2]
        else:
            values["tof_ns"] = numbers[0]
        return values

    # ------------------------------------------------------------------
    def run(self) -> None:
        self._set_state(DeviceState.CONNECTING)
        if not self.simulated and not HAS_SERIAL:
            self._fail("pyserial no está instalado", fatal=True)
            return
        while not self.should_stop:
            try:
                if self.simulated:
                    self._log(f"Modo simulado a {self.target_hz} Hz "
                              f"(el instrumento real está en construcción)")
                    self._set_state(DeviceState.STREAMING)
                    self._loop_simulated()
                else:
                    self._serial = serial.Serial(self.port, self.baudrate,
                                                 timeout=0.02)
                    self._log(f"Conectado a {self.port} @ {self.baudrate} bps "
                              f"(modo {self.mode})")
                    self._set_state(DeviceState.STREAMING)
                    self._loop_serial()
            except Exception as exc:
                self._fail(f"{type(exc).__name__}: {exc}", fatal=False)
                self._sleep(2.0)
            finally:
                self._close()
            if self.should_stop:
                break
        self._set_state(DeviceState.DISCONNECTED)

    def _close(self) -> None:
        self._finish_recording(reason="cierre")
        if self._serial is not None:
            try:
                self._serial.close()
            except Exception:
                pass
            self._serial = None

    # ------------------------------------------------------------------
    def _loop_serial(self) -> None:
        window_start = time.perf_counter()
        next_stats = window_start + 1.0

        while not self.should_stop:
            now = time.perf_counter()
            processed = False
            waiting = self._serial.in_waiting if self._serial else 0
            if waiting:
                chunk = self._serial.read(waiting)
                if chunk:
                    processed = True
                    self._bytes_window += len(chunk)
                    self._ingest(chunk)

            self._service_recording(now)

            if now >= next_stats:
                elapsed = now - window_start
                self._hz = self._samples_window / elapsed if elapsed > 0 else 0.0
                self._bps = self._bytes_window / elapsed if elapsed > 0 else 0.0
                self._emit_stats(self._stats_payload())
                window_start = now
                self._samples_window = 0
                self._bytes_window = 0
                next_stats = now + 1.0

            if not processed:
                self._sleep(0.002)

    def _loop_simulated(self) -> None:
        """Emite líneas ``t_ida,t_vuelta`` coherentes con ~14 °C y 1,5 m/s."""
        window_start = time.perf_counter()
        next_stats = window_start + 1.0
        period = 0.05

        while not self.should_stop:
            now = time.perf_counter()
            count = max(1, int(self.target_hz * period))
            lines: List[str] = []
            for _ in range(count):
                self._sim_phase += 1.0 / max(self.target_hz, 1)
                temperature = 14.0 + 0.3 * math.sin(2 * math.pi * 0.05
                                                    * self._sim_phase)
                wind = 1.5 + 0.4 * math.sin(2 * math.pi * 0.7 * self._sim_phase)
                c = temperature_to_sound_speed(temperature)
                t_up = self.path_length_m / (c + wind)
                t_down = self.path_length_m / (c - wind)
                lines.append(f"{t_up * 1e9:.1f},{t_down * 1e9:.1f}")

            payload = ("\n".join(lines) + "\n").encode()
            self._bytes_window += len(payload)
            self._ingest(payload)
            self._service_recording(now)

            if now >= next_stats:
                elapsed = now - window_start
                self._hz = self._samples_window / elapsed if elapsed > 0 else 0.0
                self._bps = self._bytes_window / elapsed if elapsed > 0 else 0.0
                self._emit_stats(self._stats_payload())
                window_start = now
                self._samples_window = 0
                self._bytes_window = 0
                next_stats = now + 1.0

            self._sleep(period)

    # ------------------------------------------------------------------
    def _ingest(self, chunk: bytes) -> None:
        """Guarda el crudo y, según el modo, además interpreta."""
        if self._recording and self._writer is not None:
            self._writer.submit(chunk)

        if self.mode == MODE_RAW:
            self._samples_window += len(chunk)
            self._samples_total += len(chunk)
            return

        if self.mode == MODE_BINARY:
            self._buffer.extend(chunk)
            size = config.ANEMO_PACKET_SIZE
            while len(self._buffer) >= size:
                packet = bytes(self._buffer[:size])
                del self._buffer[:size]
                values = self.parse_packet(packet)
                if values:
                    self.last_values = values
                self._samples_window += 1
                self._samples_total += 1
            return

        # MODE_LINES
        self._buffer.extend(chunk)
        while True:
            position = self._buffer.find(b"\n")
            if position == -1:
                break
            line = bytes(self._buffer[:position]).decode(errors="replace")
            del self._buffer[:position + 1]
            values = self.parse_line(line)
            if values:
                self.last_values = values
                self._samples_window += 1
                self._samples_total += 1

    # ------------------------------------------------------------------
    def _service_recording(self, now: float) -> None:
        if self._consume_arm_request():
            self._chunk_index = 0
            self._chunk_summaries = []
            self._armed = True
            self._set_state(DeviceState.ARMED)
            self._log("Armado.")

        if self._consume_arm_cancel():
            self._armed = False
            self._set_state(DeviceState.STREAMING)

        if self._consume_start_request():
            if self._open_chunk(now):
                self._recording = True
                self._set_state(DeviceState.RECORDING)
                self._log("Grabando.")

        target = self.record_target
        chunk_s = target["chunk_duration_s"]
        if self._recording and chunk_s and (now - self._chunk_started) >= chunk_s:
            self._close_chunk(reason="rollover")
            self._open_chunk(now)

        if self._consume_stop_request():
            self._finish_recording(reason="parada")

    def _open_chunk(self, now: float) -> bool:
        target = self.record_target
        directory = Path(target["directory"] or ".")
        try:
            directory.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            self._fail(f"Carpeta inaccesible: {exc}")
            return False
        extension = "txt" if self.mode == MODE_LINES else "bin"
        self._chunk_path = (directory /
                            f"{self.record_stem(self._chunk_index)}.{extension}")
        header = (f"# {config.APP_NAME} anemometro ToF · modo={self.mode} "
                  f"· hz_objetivo={self.target_hz} "
                  f"· d={self.path_length_m} m "
                  f"· t0_host_ns={self._t0_ns}\n").encode() \
            if self.mode == MODE_LINES else None
        self._writer = ByteStreamWriter(
            self._chunk_path, header=header,
            metadata=self.record_metadata(
                modo=self.mode, hz_objetivo=self.target_hz,
                longitud_camino_m=self.path_length_m),
            t0_ns=self._t0_ns,
            on_failure=lambda msg: self.raise_recording_alarm(
                f"escritura detenida: {msg}"))
        self._writer.start()
        self._chunk_started = now
        self._log(f"Archivo: {self._chunk_path.name}")
        return True

    def _close_chunk(self, reason: str = "") -> None:
        if self._writer is None:
            return
        summary = self._writer.stop()
        summary["motivo"] = reason
        self._writer = None
        self._chunk_summaries.append(summary)
        self._chunk_index += 1
        self._chunk_path = None

    def _finish_recording(self, reason: str = "") -> None:
        if not self._recording and self._writer is None:
            return
        was_recording = self._recording
        self._close_chunk(reason=reason)
        self._recording = False
        self._armed = False
        if self.thread_running and not self.should_stop:
            self._set_state(DeviceState.STREAMING)
        if was_recording:
            self._publish_recording_summary({
                "dispositivo": self.label,
                "familia": self.family,
                "motivo": reason,
                "bloques": self._chunk_summaries,
                "muestras": self._samples_total,
                "modo": self.mode,
                "hz_objetivo": self.target_hz,
                "simulado": self.simulated,
                "t0_host_ns": self._t0_ns,
            })
            self._chunk_summaries = []

    # ------------------------------------------------------------------
    def _stats_payload(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "hz": round(self._hz, 1),
            "hz_objetivo": self.target_hz,
            "bytes_s": int(self._bps),
            "mb_s": round(self.estimated_mb_s(), 4),
            "muestras": self._samples_total,
            "modo": self.mode,
            "puerto": self.port,
            "grabando": self._recording,
            "simulado": self.simulated,
            "camino_m": self.path_length_m,
        }
        payload.update({f"ultimo_{key}": value
                        for key, value in self.last_values.items()})
        return payload
