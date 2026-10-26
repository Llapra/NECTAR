"""Worker del acondicionador de fibras ópticas (Opsens CoreSens).

Un worker representa **un acondicionador** (el chasis o la unidad
stand-alone), no una fibra: es el equipo el que tiene la IP y los dos sockets.
Cada fibra es un canal dentro de él, y la pestaña 5 despliega una fila por
canal descubierto. Si hay más de un acondicionador, se crean más workers: la
lógica aditiva funciona igual en los dos niveles.

Esto sustituye al cliente en C++ (``programa/temp``) que había en el
laboratorio, conservando su protocolo exacto y agregando lo que faltaba:
marca de tiempo del host por muestra, grabación estructurada, participación en
el disparo síncrono global y validación de las tasas contra el datasheet.
"""

from __future__ import annotations

import socket
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import numpy as np

from ... import config
from ...core.disk_stream import ByteStreamWriter
from ...core.time_sync import TIME_BASE
from ..base import DeviceState, DeviceWorker
from . import protocol
from .parser import FiberParser, structure_function


#: Dirección que se va a sondear al pulsar DETECTAR. La pestaña 5 la actualiza
#: cuando el operador cambia el campo de IP, para que "detectar" signifique
#: "mirar ahí" y no "mirar donde diga el archivo de configuración".
_TARGET = {
    "ip": config.FIBER_DEFAULT_IP,
    "control_port": config.FIBER_CONTROL_PORT,
    "data_port": config.FIBER_DATA_PORT,
    "module_type": config.FIBER_DEFAULT_MODULE,
}

#: Cuánto se espera a que el acondicionador conteste al sondeo. Corto a
#: propósito: el equipo está en la red local del laboratorio y contesta en
#: milisegundos o no está. El sondeo corre en el hilo de la interfaz, así que
#: cada décima de más es una décima de ventana congelada al arrancar.
PROBE_TIMEOUT_S = 0.6


def _valid_port(value) -> Optional[int]:
    """Un puerto TCP o None. Sin excepciones: quien llama decide."""
    try:
        port = int(value)
    except (TypeError, ValueError):
        return None
    return port if 1 <= port <= 65535 else None


def set_target(ip: str, control_port: Optional[int] = None,
               data_port: Optional[int] = None,
               module_type: Optional[str] = None) -> None:
    """Fija a qué equipo apunta el próximo DETECTAR.

    Lo que no sea utilizable se ignora y se conserva el valor anterior: un
    campo en blanco o un puerto fuera de rango no puede dejar al programa
    sondeando una dirección sin sentido y culpando después al cable.
    """
    if ip and str(ip).strip():
        _TARGET["ip"] = str(ip).strip()
    port = _valid_port(control_port)
    if port is not None:
        _TARGET["control_port"] = port
    port = _valid_port(data_port)
    if port is not None:
        _TARGET["data_port"] = port
    if module_type and str(module_type).strip():
        _TARGET["module_type"] = str(module_type).strip()


def probe(ip: str, port: int, timeout: float = PROBE_TIMEOUT_S) -> bool:
    """True si hay algo escuchando en esa IP y puerto."""
    try:
        with socket.create_connection((ip, int(port)), timeout=timeout):
            return True
    except OSError:
        return False


def discover_fibers() -> List[Dict[str, Any]]:
    """Sondea el acondicionador en su IP y lo ofrece **solo si contesta**.

    El equipo no se autodescubre: se declara por IP. Pero declarar no es
    detectar, y la primera versión informaba "1 dispositivo detectado" sin
    haber tocado la red — o sea, decía que había un acondicionador aunque no
    hubiera ni cable. Aquí se abre una conexión TCP al canal de control antes
    de ofrecerlo. Si nadie contesta, se dice eso y no se ofrece nada.

    La IP se puede cambiar en el panel lateral de la pestaña 5 y volver a
    pulsar DETECTAR; y siempre queda la opción de agregarlo a mano, para el
    caso de un equipo que tarda en levantar.
    """
    if config.simulations_allowed():
        return [{
            "id": "fibers_sim",
            "label": "Fibras_SIM",
            "ip": "127.0.0.1",
            "control_port": config.FIBER_CONTROL_PORT,
            "data_port": config.FIBER_DATA_PORT,
            "module_type": config.FIBER_DEFAULT_MODULE,
            "simulated": True,
            "channels": 6,
        }]

    ip = str(_TARGET["ip"]).strip()
    control_port = _valid_port(_TARGET["control_port"])
    if not ip or control_port is None:
        raise RuntimeError(
            f"La dirección del acondicionador no es utilizable "
            f"(IP='{ip}', puerto de control={_TARGET['control_port']}). "
            f"Corrígela en el panel lateral de la pestaña ⑤.")
    if not probe(ip, control_port):
        raise RuntimeError(
            f"El acondicionador de fibras no contesta en {ip}:{control_port} "
            f"(sondeo de {PROBE_TIMEOUT_S:.1f} s). Comprueba el cable, que el "
            f"equipo esté encendido y que la IP del panel lateral sea la "
            f"correcta; después vuelve a pulsar DETECTAR.")

    return [{
        "id": f"fibers_{ip}",
        "label": "Fibras_01",
        "ip": ip,
        "control_port": control_port,
        "data_port": int(_TARGET["data_port"]),
        "module_type": _TARGET["module_type"],
        "simulated": False,
    }]


class FiberWorker(DeviceWorker):
    """Acondicionador de fibras: control SCPI + flujo binario de muestras."""

    family = "fibers"
    family_label = "Fibras"
    supports_chunking = True
    #: 6 canales a 1 kHz en float32 ≈ 0,26 MB/s (dossier).
    nominal_mb_s = 0.3

    def __init__(self, device_id: str, label: str,
                 descriptor: Optional[Dict[str, Any]] = None) -> None:
        super().__init__(device_id, label, descriptor)
        self.ip: str = self.descriptor.get("ip", config.FIBER_DEFAULT_IP)
        self.control_port: int = int(self.descriptor.get(
            "control_port", config.FIBER_CONTROL_PORT))
        self.data_port: int = int(self.descriptor.get(
            "data_port", config.FIBER_DATA_PORT))
        self.module_type: str = self.descriptor.get(
            "module_type", config.FIBER_DEFAULT_MODULE)
        self.simulated: bool = bool(self.descriptor.get("simulated", False))
        self.sim_channels: int = int(self.descriptor.get("channels", 6))

        self.sampling_rate = 1000
        self.requested_rate = 1000

        self.parser = FiberParser()
        self._control: Optional[socket.socket] = None
        self._data: Optional[socket.socket] = None
        self._control_rx = bytearray()

        self._raw_writer: Optional[ByteStreamWriter] = None
        self._csv_handle = None
        self._csv_path: Optional[Path] = None
        self._raw_path: Optional[Path] = None
        self._chunk_index = 0
        self._chunk_started = 0.0
        self._chunk_summaries: List[Dict[str, Any]] = []
        self._rows_written = 0
        self._channel_sample_indices: Dict[int, int] = {}

        self._bytes_window = 0
        self._bytes_total = 0
        self._throughput_kb_s = 0.0
        self._pending_rate: Optional[int] = None
        self._sim_phase = 0.0

    # ------------------------------------------------------------------
    # Configuración
    # ------------------------------------------------------------------
    def set_sampling_rate(self, hz: int) -> None:
        """Pide una tasa; se valida contra el módulo antes de mandarla."""
        if self._recording or self._armed:
            self._log("Detén la grabación o cancela el armado antes de cambiar la frecuencia.")
            return
        valid, warning = protocol.clamp_rate(int(hz), self.module_type)
        self.requested_rate = int(hz)
        if warning:
            self._log(warning)
        self._pending_rate = valid

    def set_module_type(self, module_type: str) -> None:
        if module_type in protocol.MODULE_TYPES:
            self.module_type = module_type
            self.set_sampling_rate(self.sampling_rate)

    def allowed_rates(self) -> List[int]:
        return protocol.allowed_rates(self.module_type)

    def channel_count(self) -> int:
        return len(self.parser.channels)

    def estimated_mb_s(self) -> float:
        channels = max(1, self.channel_count())
        # CSV + binario crudo: ~8 bytes por muestra de binario y ~14 de texto.
        return channels * self.sampling_rate * 22 / 1e6

    # ------------------------------------------------------------------
    # Conexión
    # ------------------------------------------------------------------
    def connect_device(self) -> bool:
        self.start_thread()
        return True

    def disconnect_device(self) -> None:
        self.stop_recording()
        self.stop_thread()
        self._set_state(DeviceState.DISCONNECTED)

    def run(self) -> None:
        self._set_state(DeviceState.CONNECTING)
        while not self.should_stop:
            try:
                if self.simulated:
                    self._log(f"Modo simulado: {self.sim_channels} fibras "
                              f"a {self.sampling_rate} Hz")
                    self._set_state(DeviceState.STREAMING)
                    self._loop_simulated()
                else:
                    self._open_sockets()
                    self._set_state(DeviceState.STREAMING)
                    self._loop_real()
            except Exception as exc:
                self._fail(f"{type(exc).__name__}: {exc}", fatal=False)
                self._sleep(2.0)
            finally:
                self._close_sockets()
            if self.should_stop:
                break
        self._set_state(DeviceState.DISCONNECTED)

    # ------------------------------------------------------------------
    def _open_sockets(self) -> None:
        self._control_rx.clear()
        self._control = socket.create_connection((self.ip, self.control_port),
                                                 timeout=3.0)
        self._control.settimeout(0.1)
        self._log(f"Control conectado a {self.ip}:{self.control_port}")

        banner = self._read_response(line=True)
        if not banner.startswith("Key:"):
            raise RuntimeError(f"No se recibió la clave CoreSens2: {banner!r}")
        key = banner.split(":", 1)[1].strip()
        if not key.isdigit():
            raise RuntimeError(f"Clave de datos inválida: {banner!r}")

        identity = self._ask(protocol.cmd_identify())
        if identity:
            self._log(f"Equipo: {identity.strip()}")

        valid, warning = protocol.clamp_rate(self.requested_rate, self.module_type)
        if warning:
            self._log(warning)
        self._configure_rate(valid)
        if self._pending_rate == valid:
            self._pending_rate = None

        self._data = socket.create_connection((self.ip, self.data_port),
                                              timeout=3.0)
        self._data.settimeout(0.2)
        prompt = bytearray()
        while len(prompt) < 5:
            part = self._data.recv(5 - len(prompt))
            if not part:
                raise RuntimeError("El canal de datos cerró durante la conexión")
            prompt.extend(part)
        if bytes(prompt) != b"Key?:":
            raise RuntimeError(f"Solicitud de clave inesperada: {bytes(prompt)!r}")
        self._data.sendall((key + "\n").encode("ascii"))
        self.parser.reset()
        self._check_reply(self._ask(protocol.cmd_start(0)))
        self._log(f"Datos conectados a {self.ip}:{self.data_port} "
                  f"a {self.sampling_rate} Hz")

    def _close_sockets(self) -> None:
        self._finish_recording(reason="cierre")
        if self._control is not None:
            try:
                self._send_control(protocol.cmd_stop())
            except Exception:
                pass
            try:
                self._control.close()
            except Exception:
                pass
            self._control = None
        if self._data is not None:
            try:
                self._data.close()
            except Exception:
                pass
            self._data = None

    def _send_control(self, command: bytes) -> None:
        if self._control is None:
            return
        self._control.sendall(command)

    def _ask(self, command: bytes, timeout: float = 0.5) -> str:
        if self._control is None:
            return ""
        # measure:start publica también el encabezado de canales. Consumir
        # respuestas pendientes antes de asociar una respuesta al comando.
        self._control.settimeout(0.02)
        try:
            while self._control.recv(4096):
                pass
        except socket.timeout:
            pass
        finally:
            self._control.settimeout(0.1)
        self._control_rx.clear()
        self._send_control(command)
        return self._read_response(timeout=timeout)

    def _read_response(self, timeout: float = 1.0, line: bool = False) -> str:
        deadline = time.perf_counter() + timeout
        delimiter = b"\n" if line else b"\x04"
        while time.perf_counter() < deadline:
            if delimiter in self._control_rx:
                response, _, remainder = self._control_rx.partition(delimiter)
                self._control_rx = bytearray(remainder)
                return response.decode(errors="replace").strip()
            try:
                data = self._control.recv(4096)
                if not data:
                    raise ConnectionError("El conversor cerró el canal de control")
                self._control_rx.extend(data)
            except socket.timeout:
                continue
        raise TimeoutError("El conversor no completó su respuesta")

    @staticmethod
    def _check_reply(reply: str) -> None:
        if reply.lower().startswith("err"):
            raise RuntimeError(f"El conversor rechazó el comando: {reply}")

    def _configure_rate(self, hz: int) -> None:
        self._check_reply(self._ask(protocol.cmd_rate(hz)))
        response = self._ask(protocol.cmd_rate_query())
        # Un ACK/encabezado tardío de measure:start puede preceder a rate?.
        for _ in range(2):
            if response and not response.startswith("Sy"):
                break
            response = self._read_response()
        self._check_reply(response)
        period = float(response)
        if not np.isfinite(period) or period <= 0:
            raise RuntimeError(f"Período de muestreo inválido: {response!r}")
        self.sampling_rate = int(round(1.0 / period))
        self._log(f"Frecuencia confirmada por el conversor: {self.sampling_rate} Hz")

    # ------------------------------------------------------------------
    def _loop_real(self) -> None:
        window_start = time.perf_counter()
        next_stats = window_start + 1.0

        while not self.should_stop:
            now = time.perf_counter()

            if self._pending_rate is not None:
                rate = self._pending_rate
                self._pending_rate = None
                try:
                    self._configure_rate(rate)
                except (OSError, ValueError, RuntimeError) as exc:
                    self._log(f"No se pudo cambiar la tasa: {exc}")

            block = b""
            try:
                block = self._data.recv(65536) if self._data else b""
                if not block:
                    raise RuntimeError("El conversor cerró el canal de datos")
            except socket.timeout:
                block = b""
            except OSError as exc:
                raise RuntimeError(f"Canal de datos caído: {exc}") from exc

            if block:
                host_ns = TIME_BASE.now_ns()
                self._bytes_window += len(block)
                self._bytes_total += len(block)
                samples = self.parser.feed(block, host_ns)
                self._record_samples(block, samples, host_ns)
            else:
                # Sin datos: igual hay que atender órdenes de grabación.
                self._service_recording(now)

            self._service_recording(now)

            if now >= next_stats:
                elapsed = now - window_start
                self._throughput_kb_s = self._bytes_window / elapsed / 1024 \
                    if elapsed > 0 else 0.0
                self.parser.refresh_rates(elapsed)
                self._emit_stats(self._stats_payload())
                window_start = now
                self._bytes_window = 0
                next_stats = now + 1.0

    def _loop_simulated(self) -> None:
        """Genera temperaturas plausibles: deriva lenta + turbulencia + ruido."""
        window_start = time.perf_counter()
        next_stats = window_start + 1.0
        rng = np.random.default_rng(1234)
        base = 14.0 + rng.normal(0.0, 0.4, size=self.sim_channels)
        period = 0.05      # se emiten lotes de 50 ms

        while not self.should_stop:
            now = time.perf_counter()
            if self._pending_rate is not None:
                self.sampling_rate = self._pending_rate
                self._pending_rate = None
                self._log(f"Tasa simulada: {self.sampling_rate} Hz")

            count = max(1, int(self.sampling_rate * period))
            self._sim_phase += period
            payload = bytearray()
            for _ in range(count):
                # Fluctuación térmica correlacionada entre sondas vecinas más
                # un término propio: así la función de estructura no sale nula.
                common = 0.05 * np.sin(2 * np.pi * 0.2 * self._sim_phase)
                for channel in range(self.sim_channels):
                    slot, chan = protocol.slot_channel(channel)
                    value = float(base[channel] + common
                                  + rng.normal(0.0, 0.012))
                    payload += protocol.PACKET.pack(slot, 0, chan, 0, value)

            host_ns = TIME_BASE.now_ns()
            block = bytes(payload)
            self._bytes_window += len(block)
            self._bytes_total += len(block)
            samples = self.parser.feed(block, host_ns)
            self._record_samples(block, samples, host_ns)
            self._service_recording(now)

            if now >= next_stats:
                elapsed = now - window_start
                self._throughput_kb_s = self._bytes_window / elapsed / 1024 \
                    if elapsed > 0 else 0.0
                self.parser.refresh_rates(elapsed)
                self._emit_stats(self._stats_payload())
                window_start = now
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
            self._log("Armada.")

        if self._consume_arm_cancel():
            self._armed = False
            self._set_state(DeviceState.STREAMING)

        if self._consume_start_request():
            if self._open_chunk(now):
                self._recording = True
                self._set_state(DeviceState.RECORDING)
                self._log("Grabando temperaturas.")

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

        stem = self.record_stem(self._chunk_index)

        self._csv_path = directory / f"{stem}.csv"
        self._raw_path = directory / f"{stem}.bin"

        # CoreSens2 entrega lotes por canal, no barridos intercalados.
        # Formato largo: una fila identifica una muestra de un canal.
        columns = ["host_ns", "t_rel_s", "t_unix", "muestra", "canal",
                   "indice_canal", "temperatura_C", "valida"]
        try:
            self._csv_handle = open(self._csv_path, "w", encoding="utf-8",
                                    newline="")
            self._csv_handle.write(",".join(columns) + "\n")
            self._raw_writer = ByteStreamWriter(
                self._raw_path,
                metadata=self.record_metadata(
                    formato="crudo del acondicionador, 8 B por muestra",
                    formato_csv="muestras_por_canal_v2",
                    tiempo_csv="recepcion_host_del_bloque_TCP; no timestamp individual del sensor",
                    canales=[state.index + 1 for state in self.parser.ordered()],
                    csv_hermano=self._csv_path.name),
                t0_ns=self._t0_ns,
                on_failure=lambda msg: self.raise_recording_alarm(
                    f"escritura detenida: {msg}"))
            self._raw_writer.start()
        except OSError as exc:
            self._fail(f"No se pudo abrir los archivos: {exc}")
            return False

        self._chunk_started = now
        self._rows_written = 0
        self._channel_sample_indices = {}
        self._log(f"Archivo: {self._csv_path.name} "
                  "(una muestra por fila, canal explícito)")
        return True

    def _record_samples(self, block: bytes, samples, host_ns: int) -> None:
        """Conserva cada muestra y su canal, aunque lleguen lotes agrupados.

        host_ns identifica la recepción TCP. No se inventan tiempos de sensor
        ni se emparejan como simultáneas muestras de canales distintos.
        """
        if not self._recording:
            return
        if self._raw_writer is not None:
            self._raw_writer.submit(block)
        if self._csv_handle is None or not samples:
            return

        rows = []
        for index, value, sample_host_ns in samples:
            utc = TIME_BASE.to_utc(sample_host_ns)
            sequence = self._channel_sample_indices.get(index, 0)
            self._channel_sample_indices[index] = sequence + 1
            valid = bool(np.isfinite(value))
            rows.append(",".join([
                str(sample_host_ns), f"{TIME_BASE.rel_s(sample_host_ns):.9f}",
                f"{utc:.6f}" if utc is not None else "",
                str(self._rows_written + len(rows)), str(index + 1), str(sequence),
                f"{value:.9g}" if valid else "", "1" if valid else "0"]) + "\n")
        try:
            self._csv_handle.write("".join(rows))
            self._rows_written += len(rows)
        except OSError as exc:
            self.raise_recording_alarm(f"No se pudo escribir el CSV: {exc}")

    def _close_chunk(self, reason: str = "") -> None:
        summary: Dict[str, Any] = {"motivo": reason,
                                   "filas_csv": self._rows_written}
        if self._csv_handle is not None:
            try:
                self._csv_handle.flush()
                self._csv_handle.close()
            except OSError:
                pass
            self._csv_handle = None
        if self._raw_writer is not None:
            summary.update(self._raw_writer.stop())
            self._raw_writer = None

        if self._csv_path is not None:
            summary["archivo_csv"] = self._csv_path.name
        self._chunk_summaries.append(summary)
        self._chunk_index += 1
        self._csv_path = None
        self._raw_path = None

    def _finish_recording(self, reason: str = "") -> None:
        if not self._recording and self._csv_handle is None:
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
                "canales": self.parser.snapshot(),
                "tasa_hz": self.sampling_rate,
                "modulo": self.module_type,
                "simulado": self.simulated,
                "t0_host_ns": self._t0_ns,
            })
            self._chunk_summaries = []

    # ------------------------------------------------------------------
    def _stats_payload(self) -> Dict[str, Any]:
        channels = self.parser.snapshot()
        now_ns = TIME_BASE.now_ns()
        for channel in channels:
            state = self.parser.channels[int(channel["indice"])]
            stale = (now_ns - state.last_host_ns) > 3_000_000_000
            valid = np.isfinite(state.last_value) and not stale
            channel["estado"] = ("Sin datos recientes" if stale else
                                 "Activa" if valid else "Lectura inválida")
            if not valid:
                channel["valor"] = None
            if stale:
                channel["hz"] = 0.0
        values = np.array([c["valor"] if c["valor"] is not None else np.nan
                           for c in channels])
        payload: Dict[str, Any] = {
            "canales": channels,
            "n_canales": len(channels),
            "tasa_hz": self.sampling_rate,
            "modulo": self.module_type,
            "kb_s": round(self._throughput_kb_s, 1),
            "mb_s": round(self.estimated_mb_s(), 3),
            "muestras_totales": self.parser.total_samples,
            "paquetes_malformados": self.parser.malformed,
            "grabando": self._recording,
            "filas_csv": self._rows_written,
            "simulado": self.simulated,
        }
        if values.size:
            finite = values[np.isfinite(values)]
            if finite.size:
                payload["temp_media_c"] = round(float(np.mean(finite)), 3)
                payload["estructura"] = structure_function(values)
        return payload
