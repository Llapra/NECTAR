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

## [1.0.3] · 2026-10-26

Versión con las cámaras y las fibras funcionando, probada en Kolmogorov
(Windows). Pendiente: probarla con las cámaras en el PC de campaña (Ubuntu).

### Cámara de eventos
* Pausa de 1 ms en el lector de Metavision cuando no hay datos: la vista en
  vivo deja de ir a saltos y no se pierden eventos.
* NECTAR agrega por sí solo la carpeta del plugin de CenturyArks a
  `MV_HAL_PLUGIN_PATH` si falta.
* El diagnóstico avisa si al SDK le falta una librería de Python (por ejemplo
  `h5py`, ahora incluida en `requirements.txt`).

### Emergent
* Solo se puede armar la grabación después de recibir un cuadro real, y el
  tamaño por cuadro se toma de ese cuadro.
* Los controles validan rango y paso; arreglo para valores mayores a 32 bits.
* Selector de FPS de la vista previa (30/20); contraste automático apagado por
  omisión.

### Fibras
* Protocolo adaptado al CoreSens2 del laboratorio: IP 10.0.10.2, control 2001,
  datos 2003, clave de acceso, tasa confirmada con `rate?`, 3 módulos.
* Pestaña de fibras rehecha, con una fila por canal.

### Grabación
* Si un instrumento incluido no está listo, la sesión no arranca y dice cuál.

### Nuevo
* Pruebas automáticas en `tests/` y diagnóstico de fibras en `tools/`.

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
