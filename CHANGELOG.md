# Registro de cambios de NECTAR

Todas las versiones de NECTAR, de la más nueva a la más antigua.
Numeración **MAYOR.MENOR.PARCHE**:

* **PARCHE** (1.0.0 → 1.0.1): se arregla un error, nada más cambia.
* **MENOR** (1.0.1 → 1.1.0): se agrega algo nuevo y todo lo anterior sigue
  funcionando igual.
* **MAYOR** (1.x → 2.0.0): cambia algo que rompe la compatibilidad, por
  ejemplo el formato de los archivos que graba.

La versión vive en un solo lugar, `nectar/config.py` (`APP_VERSION`), y queda
escrita en el título de la ventana, en la bitácora y en el manifiesto de cada
sesión.

---

## [1.0.0] · 2026-10-19

Primera versión pública.

### Incluye

* Disparo síncrono global desde la pestaña 1 (protocolo de 5 fases), con base
  de tiempo común y ancla UTC por el PPS del GPS.
* Once pestañas: Adquisición, EvCam, Emergent, Reconstrucción, Fibras,
  Anemómetro, IMU-GPS, Enfoque, Conversor IMU, Topografía y Enlace, NN1.
* Cámara de eventos (SilkyEvCam / Prophesee) vía OpenEB / Metavision, con
  biases, ROI y grabación `.raw` (EVT3).
* Cámara Emergent HB-1800-S-M vía eSDK (Windows), con grabación directa al SSD,
  `_frames.csv`, `_meta.json` y `_checkpoint.json`.
* Fibras ópticas Opsens CoreSens (SCPI + binario), IMU/GPS por serie,
  estructura del anemómetro ToF.
* Asistente de enfoque en dos etapas (EPR, luego ER + HFE).
* Modo `--demo` con todos los instrumentos simulados y todo lo grabado marcado
  como simulación; fuera de `--demo` no existen dispositivos simulados.
* Instalación documentada para Ubuntu 24.04 (OpenEB 5.3 + plugin SilkyEvCam
  v5.2.0, entorno `venv-NECTAR`) y para Windows.

### Pendiente (ver README, sección 15)

* Prueba de extremo a extremo con la cámara Emergent real, y validación del
  eSDK en Linux (Ubuntu 24.04 / kernel 6.17, tarjeta ConnectX de 25G).
* Prueba de la SilkyEvCam en el PC de campaña.
* Firmware del anemómetro ToF y su `parse_packet()`.
* Motores de la lente para el enfoque motorizado.
* Seguimiento de centroides e índice de centelleo en tiempo real.
* Sincronismo por hardware entre familias de instrumentos.
