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

## [1.0.2] · 2026-10-21

Arreglo de la caída de la 1.0.1 con la cámara de eventos conectada
("Violación de segmento"). La causa estaba en el programa, no en el SDK ni en
la instalación: la 1.0.1 llamaba al SDK de formas que el visor anterior del
laboratorio (`main_v14.1.py`) nunca usa, y que no se habían probado con la
cámara real.

### Arreglado

* **La cámara se abría más de una vez.** La 1.0.1 probaba una cascada de
  aperturas (`DeviceDiscovery.open()`, luego `EventsIterator`, luego otra
  llave) y, si una fallaba a medias, la siguiente volvía a abrir la cámara con
  la anterior todavía viva. Ahora abrir es **una sola llamada**,
  `EventsIterator(input_path=..., delta_t=...)`, exactamente como en
  `main_v14.1.py`. Con una sola cámara conectada se usa "la primera
  disponible" (`input_path=""`), que es la forma comprobada en el PC del
  laboratorio.

* **El listado podía inventar cámaras.** La 1.0.1 daba prioridad a
  `list_available_sources()`, cuyos objetos cambian de campos entre versiones
  del SDK; sin encontrar el número de serie usaba el texto del objeto, y una
  cámara aparecía como dos. Ahora solo se usa `DeviceDiscovery.list()`, que
  devuelve cadenas `integrador:plugin:serial` y está comprobado.

* **Buscar cámaras abría la cámara.** La búsqueda se repite en varias pestañas
  al arrancar; una "sonda" de la 1.0.1 abría la cámara en cada una. Ahora
  buscar no abre nada.

* **La vista en vivo de la cámara de eventos podía atrasarse.** El worker
  emitía cada cuadro sin esperar a que la pantalla terminara de pintar el
  anterior; si la pantalla iba más lenta que 60 cuadros por segundo, las
  señales se encolaban en Qt y lo que se veía quedaba cada vez más atrás de la
  realidad. Ahora hay contrapresión, como la bandera `gui_ready` de
  `main_v14.1.py`: no se pinta el cuadro siguiente hasta que la pantalla acusa
  recibo del actual. (La cámara Emergent ya lo tenía; la de eventos no.)

* **El cierre llamaba métodos de objetos nativos por si acaso.** Ahora se
  sueltan las referencias y nada más, como en `main_v14.1.py`.

* `--check` decía "✖ metavision_core" y más abajo "SDK disponible": la lista
  se comprobaba antes de agregar la carpeta del SDK al camino. Ahora se agrega
  primero, y además `--check` dice qué intérprete y qué entorno virtual se
  están usando.

### Cambiado

* Sin dispositivo del HAL, el recorte de la vista queda como recorte **solo
  visual** (igual que en `main_v14.1.py`), marcado así en los metadatos.

### Limitación conocida

* Con la versión del SDK del PC de desarrollo, el iterador no deja ver el
  dispositivo del HAL (`reader.device`), así que no se aplican los biases ni
  se puede grabar el `.raw`. `main_v14.1.py` tiene la misma limitación con las
  mismas líneas. La grabación se niega a armarse y lo dice. La vía que
  documenta Prophesee para grabar (`initiate_device` + `from_device`) se prueba
  primero en `probar_evcam.py` (ruta E) antes de entrar a NECTAR.

### Instalación

* `instalar_venv.sh` crea el entorno `venv-NECTAR` dentro de la carpeta del
  programa, instala las dependencias y enlaza el SDK de Metavision con un
  archivo `.pth`. `.gitignore` ignora el entorno y el registro del banco de
  pruebas.

---

## [1.0.1] · 2026-10-21

Arreglo de la conexión y la visualización de la cámara de eventos. La 1.0.0 no
detectaba la SilkyEvCam en el PC del laboratorio, y además no habría podido
grabar con ella aunque la hubiera detectado.

### Arreglado

* **La cámara de eventos no se detectaba.** El descubrimiento llamaba a
  `DeviceDiscovery.list_serial_numbers()`, método que no existe en todas las
  versiones del SDK de Metavision; donde no existe, la llamada levantaba
  `AttributeError` y NECTAR concluía que no había ninguna cámara conectada.
  Ahora se prueban en orden `list_available_sources()`, `list()` y
  `list_serial_numbers()`, se entiende el formato `integrador:plugin:serial`,
  y si ninguno devuelve nada se comprueba abriendo "la primera disponible"
  antes de afirmar que no hay cámara.

* **La misma cámara aparecía dos veces.** `DeviceDiscovery.list()` devuelve el
  mismo número de serie repetido en la SilkyEvCam (el HAL la alcanza por dos
  vías). NECTAR habría ofrecido `EvCam1` y `EvCam2` para una sola cámara
  física, y la segunda habría fallado al abrir por dispositivo ocupado. El
  listado se deduplica por número de serie.

* **La grabación era imposible aunque la vista en vivo funcionara.** El
  dispositivo del HAL se buscaba *dentro* del iterador de eventos
  (`iterator.reader.device`), atributo que tampoco existe en todas las
  versiones. Sin dispositivo no hay biases, ni sincronismo, ni `I_ROI`, ni
  `i_events_stream` — y `i_events_stream` es lo único capaz de escribir el
  `.raw`. Ahora el dispositivo se abre primero con `DeviceDiscovery.open()` y
  el iterador se construye sobre él con `EventsIterator.from_device()`, con
  una cascada de respaldos (nuevo módulo `nectar/devices/event_cam/hal.py`).
  La ruta de apertura y lo que quedó disponible se escriben en el registro de
  la pestaña.

* **La grabación se niega a armarse, en vez de fallar al disparar.** Si el HAL
  no expone `i_events_stream`, el error aparece al armar —cuando todavía se
  puede cambiar de puerto USB o reenchufar la cámara— y no en el disparo,
  cuando la ventana de observación ya se perdió.

* **El diagnóstico del SDK confundía dos problemas distintos.**
  `metavision_core.event_io` importa `h5py` por dentro; al faltar ese paquete
  NECTAR informaba "el SDK de Metavision no está disponible" con el SDK
  perfectamente instalado y la cámara enchufada. Ahora los dos módulos del SDK
  se importan por separado y el diagnóstico nombra el módulo que falta con su
  `pip install` exacto.

* **El caudal esperado se descontaba por un recorte que no existía.** Un ROI
  pedido pero no programado en el sensor dejaba `pixel_fraction` reducido, así
  que el presupuesto de disco prometía un archivo más chico del que se iba a
  escribir. Ahora un recorte no aplicado no descuenta nada.

### Agregado

* `h5py` y `PyOpenGL` en `requirements.txt`, con la aclaración de que `h5py`
  es una dependencia **interna del SDK de Metavision**, no de NECTAR.
* Recorte **solo visual** como respaldo cuando el sensor no admite `I_ROI`:
  sirve para encuadrar, y queda escrito en el `_meta.json` del `.raw`
  (`solo_visual: true`) con la advertencia de que el archivo contiene el
  cuadro completo.
* `python main.py --check` informa ahora el listado de cámaras según el HAL,
  el estado de `h5py` y `PyOpenGL`, y cómo se va a pintar la vista en vivo.

### Cambiado

* `useOpenGL=True` en pyqtgraph (pintado por GPU), con respaldo silencioso al
  pintado por CPU si falta PyOpenGL.
* Los cuadros por segundo de la pantalla pasan de 20 a 60, como en el visor
  anterior del laboratorio. Solo afecta a la pantalla; el flujo de eventos no
  se toca.

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
