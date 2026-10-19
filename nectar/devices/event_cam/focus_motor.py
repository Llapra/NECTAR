"""Enfoque motorizado: controlador del anillo y búsqueda automática.

Estado honesto de este módulo
-----------------------------
Los motores del lente **todavía no existen** en el banco. Lo que hay aquí es
la mitad que sí se puede escribir y probar sin ellos: el protocolo de mando,
la compensación de juego mecánico y la rutina de búsqueda en dos etapas que
usa el mismo ``FocusEngine`` que guía al operador en modo manual.

Cuando el controlador aparezca, lo único que hay que escribir es el transporte
—abrir el puerto y mandar las cadenas— y eso ya está aislado en
``SerialFocusMotor``. Mientras tanto, la pestaña muestra los controles
deshabilitados con el motivo, en vez de fingir que funcionan.

Protocolo propuesto
-------------------
Deliberadamente simple, en texto, terminado en ``\\n``, para que se pueda
depurar con un monitor serie y para que el firmware quepa en un ESP32::

    MOVE <pasos>     mueve relativo; negativo = sentido contrario
    POS?             responde  POS <pasos>
    HOME             vuelve al tope mecánico y pone el contador a cero
    STOP             detiene el movimiento en curso
    OK / ERR <texto> respuesta de cada comando

El juego mecánico (*backlash*) del anillo de enfoque es la parte que se
olvida y arruina la repetibilidad: al invertir el sentido, los primeros pasos
no mueven la óptica. Por eso ``FocusMotor`` siempre se aproxima al objetivo
**desde el mismo lado**, pasándose adrede y volviendo. Es el mismo truco que
usan las monturas de telescopio, y por la misma razón.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Callable, Optional

try:
    import serial                       # type: ignore
    HAS_SERIAL = True
except ImportError:
    HAS_SERIAL = False


@dataclass
class MotorStatus:
    connected: bool = False
    position: int = 0
    moving: bool = False
    port: str = ""
    error: str = ""


class SerialFocusMotor:
    """Transporte serie hacia el controlador del anillo de enfoque."""

    #: Pasos que se pasa de largo al invertir el sentido, para volver siempre
    #: aproximándose desde el mismo lado.
    BACKLASH_STEPS = 40

    def __init__(self, port: str = "", baudrate: int = 115200,
                 timeout: float = 1.0) -> None:
        self.port = port
        self.baudrate = int(baudrate)
        self.timeout = float(timeout)
        self._serial = None
        self._lock = threading.Lock()
        self._position = 0
        self._last_direction = 0
        self.status = MotorStatus(port=port)

    # ------------------------------------------------------------------
    def connect(self) -> bool:
        if not HAS_SERIAL:
            self.status.error = ("pyserial no está instalado: "
                                 "pip install pyserial")
            return False
        if not self.port:
            self.status.error = "No hay puerto seleccionado."
            return False
        try:
            self._serial = serial.Serial(self.port, self.baudrate,
                                         timeout=self.timeout)
        except Exception as exc:
            self.status.error = f"{type(exc).__name__}: {exc}"
            self.status.connected = False
            return False
        time.sleep(0.3)                 # los ESP32 se reinician al abrir
        self.status.connected = True
        self.status.error = ""
        self.query_position()
        return True

    def disconnect(self) -> None:
        with self._lock:
            if self._serial is not None:
                try:
                    self._serial.close()
                except Exception:
                    pass
            self._serial = None
        self.status.connected = False

    # ------------------------------------------------------------------
    def _command(self, text: str) -> str:
        with self._lock:
            if self._serial is None:
                return "ERR sin conexión"
            try:
                self._serial.write((text + "\n").encode("ascii"))
                return self._serial.readline().decode("ascii",
                                                      errors="replace").strip()
            except Exception as exc:
                self.status.error = f"{type(exc).__name__}: {exc}"
                return f"ERR {exc}"

    def query_position(self) -> int:
        reply = self._command("POS?")
        if reply.startswith("POS"):
            try:
                self._position = int(reply.split()[1])
            except (IndexError, ValueError):
                pass
        self.status.position = self._position
        return self._position

    def move(self, steps: int) -> bool:
        """Mueve ``steps`` pasos, compensando el juego mecánico.

        Si el sentido cambia respecto del movimiento anterior, primero se pasa
        de largo ``BACKLASH_STEPS`` y después se vuelve: así el objetivo se
        alcanza siempre desde el mismo lado y la posición es repetible.
        """
        steps = int(steps)
        if steps == 0:
            return True
        direction = 1 if steps > 0 else -1
        self.status.moving = True
        try:
            if self._last_direction and direction != self._last_direction:
                overshoot = -direction * self.BACKLASH_STEPS
                if self._command(f"MOVE {overshoot}").startswith("ERR"):
                    return False
                if self._command(f"MOVE {-overshoot}").startswith("ERR"):
                    return False
            reply = self._command(f"MOVE {steps}")
            if reply.startswith("ERR"):
                return False
            self._last_direction = direction
            self._position += steps
            self.status.position = self._position
            return True
        finally:
            self.status.moving = False

    def stop(self) -> None:
        self._command("STOP")
        self.status.moving = False

    def home(self) -> bool:
        reply = self._command("HOME")
        if reply.startswith("ERR"):
            return False
        self._position = 0
        self._last_direction = 0
        self.status.position = 0
        return True


class AutoFocusRunner:
    """Búsqueda automática en dos etapas, usando el mismo motor de decisión.

    Funciona igual que la guía manual, con la diferencia de que quien gira el
    anillo es el motor y de que el tiempo se puede mapear a posición: ahí la
    tolerancia de acoplamiento de 10 ms del paper vuelve a significar lo que
    significa en el paper.

    La rutina es:

    1. **Barrido grueso.** Se recorre el recorrido en pasos grandes anotando
       la razón de polaridad. El tramo de mayor área bajo la derivada positiva
       marca la vecindad del foco.
    2. **Vuelta a la vecindad** y barrido fino en pasos pequeños, anotando ER
       y HFE en dB.
    3. **Posicionamiento final** en el punto donde los dos estimadores
       coinciden, aproximándose desde el mismo lado que en el barrido para que
       el juego mecánico no se coma la corrección.

    Corre en su propio hilo y se puede abortar. Publica el progreso por un
    callback, no por señales de Qt, para no atar este módulo a la interfaz.
    """

    def __init__(self, motor: SerialFocusMotor, engine,
                 on_progress: Optional[Callable[[dict], None]] = None) -> None:
        self.motor = motor
        self.engine = engine
        self.on_progress = on_progress
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.best_position: Optional[int] = None

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self, coarse_step: int = 40, fine_step: int = 5,
              span: int = 1200, settle_s: float = 0.35) -> bool:
        if self.running or not self.motor.status.connected:
            return False
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, args=(coarse_step, fine_step, span, settle_s),
            name="autofocus", daemon=True)
        self._thread.start()
        return True

    def abort(self) -> None:
        self._stop.set()
        self.motor.stop()

    def _emit(self, **fields) -> None:
        if self.on_progress is not None:
            try:
                self.on_progress(dict(fields))
            except Exception:
                pass

    def _run(self, coarse_step: int, fine_step: int, span: int,
             settle_s: float) -> None:
        self.engine.reset()
        start_position = self.motor.query_position()
        samples = []

        steps = max(1, span // max(1, coarse_step))
        self._emit(fase="grueso", total=steps, hecho=0)
        for index in range(steps):
            if self._stop.is_set():
                self._emit(fase="abortado")
                return
            self.motor.move(coarse_step)
            time.sleep(settle_s)
            report = getattr(self.engine, "last_report", None)
            samples.append((self.motor.status.position, report))
            self._emit(fase="grueso", total=steps, hecho=index + 1,
                       posicion=self.motor.status.position)

        # La vecindad del foco es donde el motor de decisión pasó a etapa fina.
        target = start_position
        for position, report in samples:
            if report is not None and getattr(report, "stage", "") != "grueso":
                target = position
                break

        self._emit(fase="volviendo", posicion=target)
        self.motor.move(target - self.motor.status.position - 200)
        self.motor.move(200)            # aproximación desde un solo lado
        time.sleep(settle_s)

        best_value = float("-inf")
        best_position = self.motor.status.position
        fine_steps = max(1, (2 * 200) // max(1, fine_step))
        self._emit(fase="fino", total=fine_steps, hecho=0)
        for index in range(fine_steps):
            if self._stop.is_set():
                self._emit(fase="abortado")
                return
            self.motor.move(fine_step)
            time.sleep(settle_s)
            report = getattr(self.engine, "last_report", None)
            value = getattr(report, "hfe_db", float("-inf")) if report else \
                float("-inf")
            if value > best_value:
                best_value = value
                best_position = self.motor.status.position
            self._emit(fase="fino", total=fine_steps, hecho=index + 1,
                       hfe_db=value, posicion=self.motor.status.position)

        self._emit(fase="posicionando", posicion=best_position)
        self.motor.move(best_position - self.motor.status.position - 100)
        self.motor.move(100)
        self.best_position = best_position
        self._emit(fase="listo", posicion=best_position, hfe_db=best_value)
