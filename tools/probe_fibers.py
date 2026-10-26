"""Consultas de diagnóstico; no escribe archivos de medición."""
import threading
from nectar.devices.fibers.worker import FiberWorker


def main():
    worker = FiberWorker("probe", "CoreSens2")
    worker.log_message.connect(print)

    def show(stats):
        print("Frecuencia confirmada:", stats["tasa_hz"], "Hz")
        for channel in stats["canales"]:
            print(channel["etiqueta"], channel["valor"],
                  channel["hz"], "Hz", channel["estado"])

    worker.stats_updated.connect(show)
    timers = [threading.Timer(2.5, lambda: worker.set_sampling_rate(500)),
              threading.Timer(5, lambda: worker.set_sampling_rate(1000)),
              threading.Timer(8, worker._stop_event.set)]
    try:
        worker._open_sockets()
        for timer in timers:
            timer.start()
        worker._loop_real()
        assert worker._raw_writer is None and worker._csv_handle is None
        assert len(worker.parser.channels) == 6
    finally:
        for timer in timers:
            timer.cancel()
        worker._close_sockets()


if __name__ == "__main__":
    main()
