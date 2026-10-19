"""Worker del módulo IMU + GPS (ESP32).

Es el ``IMUWorker`` de ``main_v14.1.py`` reordenado, con el protocolo binario
intacto: paquetes de 18 bytes con cabecera, resincronización por búsqueda de
huella, volcado por bloques de 8 KB y modo RAW.

Lo importante que cambia
------------------------
El PPS del GPS ya no sirve solo para escribir el sello ATM dentro del archivo:
ahora también **ancla el reloj global de NECTAR**. En cuanto llega la primera
trama de coordenadas válida, el worker llama a
``TIME_BASE.set_utc_anchor(unix_ts, host_ns)``, y desde ese momento *todos* los
instrumentos —cámaras, fibras, anemómetro— pueden convertir sus marcas
monotónicas a UTC con el mismo offset. Ese era el hueco que quedaba sin
especificar en la arquitectura original, y es lo que da validez científica a
un dataset multi-instrumento.
"""

from __future__ import annotations

import datetime
import struct
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from PyQt6.QtCore import pyqtSignal

from ... import config
from ...core.time_sync import TIME_BASE
from .. import serial_ports
from ..base import DeviceState, DeviceWorker

try:
    import serial
    import serial.tools.list_ports
    HAS_SERIAL = True
except ImportError:
    HAS_SERIAL = False

_COORD = struct.Struct("<ffHBBBBBB")
#: Cuerpo de un paquete IMU: micros() + acelerómetro + giróscopo.
_IMU_LIVE = struct.Struct("<Lhhhhhh")


def list_serial_ports() -> List[str]:
    """Nombres de los puertos con hardware detrás. Vacío si no se puede mirar."""
    try:
        ports, _ = serial_ports.list_ports()
    except RuntimeError:
        return []
    return [port.device for port in ports]


def discover_imu() -> List[Dict[str, Any]]:
    """Un descriptor por puerto serie **con hardware detrás**.

    No devuelve los ``/dev/ttyS*`` heredados del chipset, que el núcleo crea
    siempre haya o no algo enchufado: ofrecerlos hacía aparecer 32 "IMU"
    disponibles sin un solo cable conectado. Ver ``devices/serial_ports.py``.
    """
    if config.simulations_allowed():
        return [{
            "id": "imu_sim",
            "label": "IMU1",
            "port": "SIM",
            "baudrate": config.IMU_DEFAULT_BAUD,
            "simulated": True,
        }]

    ports, hidden = serial_ports.list_ports()
    if not ports:
        nota = serial_ports.hidden_note(hidden)
        raise RuntimeError(serial_ports.no_ports_note()
                           + (f" ({nota})" if nota else ""))

    found: List[Dict[str, Any]] = []
    for index, port in enumerate(ports, start=1):
        descriptor = port.as_descriptor("imu", index, "IMU")
        descriptor["baudrate"] = config.IMU_DEFAULT_BAUD
        found.append(descriptor)
    return found


class ImuWorker(DeviceWorker):
    """Un ESP32 con IMU (acelerómetro + giróscopo) y GPS con PPS."""

    family = "imu"
    family_label = "IMU-GPS"
    supports_chunking = True
    #: 18 bytes por paquete a unos cientos de Hz: decenas de KB/s.
    nominal_mb_s = 0.02

    #: Telemetría específica que consume la pestaña 7.
    pps_tick = pyqtSignal(int)
    coordinate_ready = pyqtSignal(float, float)

    def __init__(self, device_id: str, label: str,
                 descriptor: Optional[Dict[str, Any]] = None) -> None:
        super().__init__(device_id, label, descriptor)
        self.port: str = self.descriptor.get("port", "SIM")
        self.baudrate: int = int(self.descriptor.get(
            "baudrate", config.IMU_DEFAULT_BAUD))
        #: Sin descriptor explícito se asume **real**: un IMU simulado que
        #: aparece solo grabaría vibraciones inventadas.
        self.simulated: bool = bool(self.descriptor.get("simulated", False))
        #: Modo RAW: se vuelca el flujo serie tal cual, sin parsear.
        self.raw_mode = False
        #: Espera el primer PPS válido antes de empezar a grabar.
        self.wait_for_pps = True

        self._serial = None
        self._handle = None
        self._chunk_path: Optional[Path] = None
        self._chunk_index = 0
        self._chunk_started = 0.0
        self._chunk_summaries: List[Dict[str, Any]] = []
        self._write_buffer = bytearray()
        self._pending_arm_wait = False

        self.pps_count = 0
        self.last_lat: Optional[float] = None
        self.last_lon: Optional[float] = None
        self.utc_fixed: Optional[float] = None
        #: Última muestra decodificada, para la vista en vivo de la pestaña 7.
        self.last_accel = (0.0, 0.0, 0.0)
        self.last_gyro = (0.0, 0.0, 0.0)
        self._mag_window: List[float] = []

        self._packets_window = 0
        self._bytes_window = 0
        self._sync_errors = 0
        self._hz = 0.0
        self._bps = 0.0
        self._packets_total = 0

    # ------------------------------------------------------------------
    def configure(self, port: str, baudrate: int, simulated: bool = False) -> None:
        self.port = port
        self.baudrate = int(baudrate)
        self.simulated = bool(simulated) or port == "SIM" or not HAS_SERIAL

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
        return self.nominal_mb_s

    # ------------------------------------------------------------------
    def run(self) -> None:
        self._set_state(DeviceState.CONNECTING)
        while not self.should_stop:
            try:
                if self.simulated:
                    self._log("Modo simulado (IMU + PPS sintéticos)")
                    self._set_state(DeviceState.STREAMING)
                    self._loop_simulated()
                else:
                    self._open_serial()
                    self._set_state(DeviceState.STREAMING)
                    self._loop_serial()
            except Exception as exc:
                self._fail(f"{type(exc).__name__}: {exc}", fatal=False)
                self._sleep(2.0)
            finally:
                self._close_serial()
            if self.should_stop:
                break
        self._set_state(DeviceState.DISCONNECTED)

    def _open_serial(self) -> None:
        if not HAS_SERIAL:
            raise RuntimeError("pyserial no está instalado")
        self._serial = serial.Serial(self.port, self.baudrate, timeout=0.02)
        # Secuencia de reset del ESP32: sin esto el primer arranque llega con
        # el buffer sucio y se pierde el sincronismo de los primeros paquetes.
        self._serial.dtr = False
        self._serial.rts = False
        time.sleep(0.05)
        self._serial.dtr = True
        self._serial.rts = True
        time.sleep(0.1)
        self._serial.reset_input_buffer()
        self._log(f"Conectado a {self.port} @ {self.baudrate} bps")

    def _close_serial(self) -> None:
        self._finish_recording(reason="cierre")
        if self._serial is not None:
            try:
                self._serial.close()
            except Exception:
                pass
            self._serial = None

    # ------------------------------------------------------------------
    def _loop_serial(self) -> None:
        buffer = bytearray()
        window_start = time.perf_counter()
        next_stats = window_start + 1.0

        while not self.should_stop:
            now = time.perf_counter()
            processed = False

            waiting = self._serial.in_waiting if self._serial else 0
            if waiting:
                chunk = self._serial.read(waiting)
                if chunk:
                    self._bytes_window += len(chunk)
                    processed = True
                    if self._recording and self.raw_mode and self._handle:
                        # Modo RAW: el flujo entra al disco sin interpretar.
                        self._handle.write(chunk)
                    else:
                        buffer.extend(chunk)

            if not self.raw_mode:
                self._consume_packets(buffer)

            self._service_recording(now)

            if now >= next_stats:
                elapsed = now - window_start
                self._hz = self._packets_window / elapsed if elapsed > 0 else 0.0
                self._bps = self._bytes_window / elapsed if elapsed > 0 else 0.0
                self._emit_stats(self._stats_payload())
                window_start = now
                self._packets_window = 0
                self._bytes_window = 0
                next_stats = now + 1.0

            if not processed:
                self._sleep(0.001)

    def _consume_packets(self, buffer: bytearray) -> None:
        size = config.IMU_PACKET_SIZE
        while len(buffer) >= size:
            header = struct.unpack_from("<H", buffer, 0)[0]

            if header not in (config.IMU_HEADER_IMU, config.IMU_HEADER_GPS,
                              config.IMU_HEADER_COORD):
                # Resincronización por búsqueda de huella: saltar directo al
                # próximo candidato en vez de avanzar byte a byte.
                candidates = [position for position in (
                    buffer.find(b"\xaa\xbb", 1),
                    buffer.find(b"\xcc\xbb", 1),
                    buffer.find(b"\xdd\xbb", 1),
                ) if position != -1]
                if candidates:
                    jump = min(candidates)
                    self._sync_errors += jump
                    del buffer[:jump]
                else:
                    self._sync_errors += max(0, len(buffer) - 1)
                    del buffer[:-1]
                continue

            packet = bytes(buffer[:size])
            del buffer[:size]
            host_ns = TIME_BASE.now_ns()

            if header == config.IMU_HEADER_IMU:
                self._packets_window += 1
                self._packets_total += 1
                self._decode_live(packet)
                self._stage(packet)

            elif header == config.IMU_HEADER_GPS:
                self.pps_count += 1
                self.pps_tick.emit(self.pps_count)
                self._stage(packet)

            elif header == config.IMU_HEADER_COORD:
                self._handle_coord(packet, host_ns)

    def _handle_coord(self, packet: bytes, host_ns: int) -> None:
        try:
            (latitude, longitude, year, month, day,
             hour, minute, second, valid) = _COORD.unpack_from(packet, 2)
        except struct.error:
            self._sync_errors += 1
            return

        self.last_lat, self.last_lon = float(latitude), float(longitude)
        self.coordinate_ready.emit(self.last_lat, self.last_lon)

        if valid != 1:
            self._stage(packet)
            return

        try:
            stamp = datetime.datetime(year, month, day, hour, minute, second,
                                      tzinfo=datetime.timezone.utc)
        except ValueError:
            self._stage(packet)
            return
        unix_ts = stamp.timestamp()

        # --- ancla UTC global de NECTAR --------------------------------
        first_anchor = not TIME_BASE.utc_available()
        TIME_BASE.set_utc_anchor(unix_ts, host_ns, source=f"gps_pps:{self.label}")
        if first_anchor:
            self.utc_fixed = unix_ts
            self._log(f"Ancla UTC fijada: {stamp:%H:%M:%S} UTC "
                      f"(todos los instrumentos ya pueden datarse)")

        # Si estábamos esperando el PPS para empezar a grabar, este es el
        # momento: el archivo arranca con el sello de época absoluta.
        if self._pending_arm_wait:
            self._pending_arm_wait = False
            if self._open_chunk(time.perf_counter()):
                self._recording = True
                self._set_state(DeviceState.RECORDING)
                self._stage(struct.pack("<Hd8x", config.IMU_HEADER_ATM, unix_ts))
                self._log("Grabando con época UTC sellada en el archivo.")

        self._stage(packet)

    def _decode_live(self, packet: bytes) -> None:
        """Desempaqueta la muestra para poder mostrarla en vivo.

        Son 18 bytes a unos cientos de hercios: desempaquetarlos cuesta nada
        y permite que la pestaña 7 muestre la aceleración real y la vibración
        RMS en vez de solo contar paquetes. La grabación sigue guardando el
        binario crudo tal cual, sin pasar por acá.
        """
        try:
            (_micros, ax, ay, az, gx, gy, gz) = _IMU_LIVE.unpack_from(packet, 2)
        except struct.error:
            return
        self.last_accel = (ax / config.IMU_ACCEL_SCALE,
                           ay / config.IMU_ACCEL_SCALE,
                           az / config.IMU_ACCEL_SCALE)
        self.last_gyro = (gx / config.IMU_GYRO_SCALE,
                          gy / config.IMU_GYRO_SCALE,
                          gz / config.IMU_GYRO_SCALE)
        magnitude = (self.last_accel[0] ** 2 + self.last_accel[1] ** 2
                     + self.last_accel[2] ** 2) ** 0.5
        self._mag_window.append(magnitude)
        if len(self._mag_window) > 512:
            del self._mag_window[:len(self._mag_window) - 512]

    def _vibration_rms(self) -> float:
        """RMS de la aceleración con la media quitada: vibración real.

        Restar la media descuenta la gravedad y cualquier inclinación
        estática, que si no dominarían el número y lo volverían inútil.
        """
        if len(self._mag_window) < 8:
            return 0.0
        values = self._mag_window
        mean = sum(values) / len(values)
        return (sum((value - mean) ** 2 for value in values) / len(values)) ** 0.5

    def _stage(self, packet: bytes) -> None:
        """Acumula en RAM y toca el disco cada 8 KB, no en cada paquete."""
        if not self._recording or self._handle is None:
            return
        self._write_buffer.extend(packet)
        if len(self._write_buffer) >= config.IMU_WRITE_CHUNK:
            try:
                self._handle.write(self._write_buffer)
            except OSError as exc:
                self._log(f"Error escribiendo: {exc}")
            self._write_buffer.clear()

    # ------------------------------------------------------------------
    def _loop_simulated(self) -> None:
        """IMU sintético a ~400 Hz con un PPS por segundo."""
        import numpy as np
        rng = np.random.default_rng(7)
        micros = 0
        window_start = time.perf_counter()
        next_stats = window_start + 1.0
        next_pps = window_start + 1.0
        period = 0.02
        rate_hz = 400

        while not self.should_stop:
            now = time.perf_counter()
            batch = max(1, int(rate_hz * period))
            for _ in range(batch):
                micros += int(1e6 / rate_hz)
                # Vibración de banco óptico: un modo a ~23 Hz más ruido.
                phase = 2 * np.pi * 23.0 * (micros / 1e6)
                ax = int((0.02 * np.sin(phase) + rng.normal(0, 0.004)) * config.IMU_ACCEL_SCALE)
                ay = int((0.015 * np.cos(phase * 1.31) + rng.normal(0, 0.004)) * config.IMU_ACCEL_SCALE)
                az = int((1.0 + rng.normal(0, 0.004)) * config.IMU_ACCEL_SCALE)
                gx = int(rng.normal(0, 0.05) * config.IMU_GYRO_SCALE)
                gy = int(rng.normal(0, 0.05) * config.IMU_GYRO_SCALE)
                gz = int(rng.normal(0, 0.05) * config.IMU_GYRO_SCALE)
                packet = struct.pack("<HLhhhhhh", config.IMU_HEADER_IMU,
                                     micros, ax, ay, az, gx, gy, gz)
                self._packets_window += 1
                self._packets_total += 1
                self._bytes_window += len(packet)
                self._decode_live(packet)
                self._stage(packet)

            if now >= next_pps:
                next_pps = now + 1.0
                self.pps_count += 1
                self.pps_tick.emit(self.pps_count)
                self._stage(struct.pack("<HL12x", config.IMU_HEADER_GPS, micros))
                coord = struct.pack("<HffHBBBBBB",
                                    config.IMU_HEADER_COORD,
                                    -33.036, -71.596,
                                    *time.gmtime()[:3],
                                    *time.gmtime()[3:6], 1)
                self._handle_coord(coord, TIME_BASE.now_ns())

            self._service_recording(now)

            if now >= next_stats:
                elapsed = now - window_start
                self._hz = self._packets_window / elapsed if elapsed > 0 else 0.0
                self._bps = self._bytes_window / elapsed if elapsed > 0 else 0.0
                self._emit_stats(self._stats_payload())
                window_start = now
                self._packets_window = 0
                self._bytes_window = 0
                next_stats = now + 1.0

            self._sleep(period)

    # ------------------------------------------------------------------
    # Grabación
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
            self._pending_arm_wait = False
            self._set_state(DeviceState.STREAMING)

        if self._consume_start_request():
            if self.wait_for_pps and not self.raw_mode \
                    and not TIME_BASE.utc_available():
                # Se abre el archivo recién cuando llegue el PPS, para que la
                # primera línea del binario sea la época absoluta.
                self._pending_arm_wait = True
                self._log("Esperando el primer PPS válido del GPS…")
            elif self._open_chunk(now):
                self._recording = True
                self._set_state(DeviceState.RECORDING)
                anchor = TIME_BASE.anchor
                if anchor is not None:
                    self._stage(struct.pack("<Hd8x", config.IMU_HEADER_ATM,
                                            anchor.to_utc(TIME_BASE.now_ns())))
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
            self._chunk_path = (directory /
                                f"{self.record_stem(self._chunk_index)}.bin")
            self._handle = open(self._chunk_path, "wb", buffering=0)
        except OSError as exc:
            self._fail(f"No se pudo crear el archivo: {exc}")
            return False
        self._chunk_started = now
        self._write_sidecar()
        self._log(f"Archivo: {self._chunk_path.name}")
        return True

    def _write_sidecar(self) -> None:
        """Metadatos al lado del ``.bin``: sin esto es una pila de bytes.

        El formato del ESP32 (18 bytes por paquete, cabeceras 0xBBAA/0xBBCC/
        0xBBDD/0xFFEE y las escalas del acelerómetro y del giróscopo) hay que
        conocerlo para leer el archivo. Dejarlo escrito al lado, y no solo en
        el código, es lo que hace que estos datos se puedan usar dentro de un
        año sin este programa.
        """
        if self._chunk_path is None:
            return
        import json
        document = self.record_metadata(
            formato="18 B por paquete, little-endian",
            cabeceras={
                "imu": hex(config.IMU_HEADER_IMU),
                "pps": hex(config.IMU_HEADER_GPS),
                "coordenada": hex(config.IMU_HEADER_COORD),
                "sello_utc": hex(config.IMU_HEADER_ATM),
            },
            escalas={"acelerometro_lsb_por_g": config.IMU_ACCEL_SCALE,
                     "giroscopo_lsb_por_grado_s": config.IMU_GYRO_SCALE},
            modo_crudo=self.raw_mode,
            puerto=self.port, baudios=self.baudrate,
            espero_pps=self.wait_for_pps)
        try:
            path = self._chunk_path.with_name(
                self._chunk_path.stem + "_meta.json")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(document, handle, indent=2, ensure_ascii=False)
        except OSError as exc:
            self._log(f"No se pudo escribir los metadatos: {exc}")

    def _close_chunk(self, reason: str = "") -> None:
        if self._handle is None:
            return
        try:
            if self._write_buffer:
                self._handle.write(self._write_buffer)
                self._write_buffer.clear()
            self._handle.close()
        except OSError:
            pass
        self._handle = None

        summary: Dict[str, Any] = {"motivo": reason}
        if self._chunk_path is not None:
            summary["archivo"] = self._chunk_path.name
            try:
                summary["bytes"] = self._chunk_path.stat().st_size
            except OSError:
                pass
        self._chunk_summaries.append(summary)
        self._chunk_index += 1
        self._chunk_path = None

    def _finish_recording(self, reason: str = "") -> None:
        if not self._recording and self._handle is None:
            self._pending_arm_wait = False
            return
        was_recording = self._recording
        self._close_chunk(reason=reason)
        self._recording = False
        self._armed = False
        self._pending_arm_wait = False
        if self.thread_running and not self.should_stop:
            self._set_state(DeviceState.STREAMING)
        if was_recording:
            self._publish_recording_summary({
                "dispositivo": self.label,
                "familia": self.family,
                "motivo": reason,
                "bloques": self._chunk_summaries,
                "paquetes": self._packets_total,
                "pps": self.pps_count,
                "errores_sincronismo": self._sync_errors,
                "modo_raw": self.raw_mode,
                "simulado": self.simulated,
                "t0_host_ns": self._t0_ns,
            })
            self._chunk_summaries = []

    # ------------------------------------------------------------------
    def _stats_payload(self) -> Dict[str, Any]:
        return {
            "hz": round(self._hz, 1),
            "bytes_s": int(self._bps),
            "mb_s": round(self.estimated_mb_s(), 4),
            "errores_sincronismo": self._sync_errors,
            "pps": self.pps_count,
            "paquetes": self._packets_total,
            "lat": self.last_lat,
            "lon": self.last_lon,
            "ax_g": round(self.last_accel[0], 4),
            "ay_g": round(self.last_accel[1], 4),
            "az_g": round(self.last_accel[2], 4),
            "gx_dps": round(self.last_gyro[0], 3),
            "gy_dps": round(self.last_gyro[1], 3),
            "gz_dps": round(self.last_gyro[2], 3),
            "vibracion_rms_g": round(self._vibration_rms(), 5),
            "utc_anclado": TIME_BASE.utc_available(),
            "grabando": self._recording,
            "esperando_pps": self._pending_arm_wait,
            "modo_raw": self.raw_mode,
            "puerto": self.port,
            "simulado": self.simulated,
        }
