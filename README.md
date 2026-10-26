# NECTAR

**N**euromorphic & **E**mergent **C**amera **T**omography for **A**tmospheric **R**esearch

Plataforma unificada de adquisición, sincronización y control instrumental del
proyecto **PASTAM** (*Passive Sensor Tracking and Analysis*), financiado por
**ANID FONDEF IDeA I+D**, desarrollado en la **PUCV**.

**Autor:** Eduardo Llantén · Instituto de Física, Pontificia Universidad Católica de Valparaíso.

**Versión:** 1.0.0 · [registro de cambios](CHANGELOG.md)

---

## Índice

1. [Qué es NECTAR y qué problema resuelve](#1-qué-es-nectar-y-qué-problema-resuelve)
2. [Instalación](#2-instalación)
3. [Arrancar el programa](#3-arrancar-el-programa)
4. [La idea central: la pestaña 1 manda](#4-la-idea-central-la-pestaña-1-manda)
5. [La base de tiempo](#5-la-base-de-tiempo)
6. [Dónde queda cada archivo](#6-dónde-queda-cada-archivo)
7. [Formatos de archivo](#7-formatos-de-archivo)
8. [Tutorial de uso](#8-tutorial-de-uso)
9. [Las once pestañas, una por una](#9-las-once-pestañas-una-por-una)
10. [Arquitectura del código](#10-arquitectura-del-código)
11. [Rendimiento: por qué el programa aguanta el caudal](#11-rendimiento-por-qué-el-programa-aguanta-el-caudal)
12. [Cómo agregar un instrumento nuevo](#12-cómo-agregar-un-instrumento-nuevo)
13. [Atajos de teclado](#13-atajos-de-teclado)
14. [Solución de problemas](#14-solución-de-problemas)
15. [Qué falta y qué sigue](#15-qué-falta-y-qué-sigue)
16. [Garantías de integridad de los datos](#16-garantías-de-integridad-de-los-datos)

---

## 1. Qué es NECTAR y qué problema resuelve

### El problema físico

Los telescopios gigantes —el VLT y el futuro ELT, con su espejo primario de 39
metros— necesitan óptica adaptativa de rendimiento extremo. Pero una fracción
crítica de la degradación del frente de onda no viene de la atmósfera libre a
kilómetros de altura: viene del **aire confinado dentro de la propia cúpula**
(*dome seeing*) y de la capa superficial.

Esa turbulencia rompe las aproximaciones clásicas de Kolmogorov: el volumen
cerrado impone fronteras geométricas (hay que medir la escala interna `l₀` y la
externa `L₀`), no se distribuye igual en vertical que en horizontal (exponentes
de Hurst anisotrópicos) y no hay un viento laminar dominante que arrastre los
vórtices intactos, así que la hipótesis de turbulencia congelada de Taylor solo
vale en cierto rango, que hay que cuantificar.

### Lo que mide el instrumento

Cámaras y sondas apuntando a **blancos pasivos retrorreflectantes** fijados en
las paredes internas de la cúpula. Cada blanco tiene una grilla de 10 × 10
manchas y bordes de damero, que desacoplan dos fenómenos distintos:

| Estructura del blanco | Fenómeno | Métrica |
|---|---|---|
| Grilla de 100 manchas | gradientes de fase desvían los rayos | deriva del baricentro (*spot wander*) → ángulo de arribo, `Cn²`, `l₀`, `L₀`, viento 3D |
| Bordes de damero | perturbaciones de amplitud e irradiancia | índice de centelleo de Charnotskii `σ²_I` |

### Lo que hace este software

NECTAR es el **frontend de adquisición**: captura, sincroniza, monitorea y
empaqueta los datos de todos los instrumentos a la vez, para que después las
redes neuronales (extracción de series y estimación de parámetros
atmosféricos) y los modelos tomográficos trabajen sobre un dataset coherente.

Los instrumentos que coordina:

| Instrumento | Enlace | Régimen | Caudal aproximado |
|---|---|---|---|
| Cámara Emergent HB-1800-S-M | 25GigE (fibra, SFP28) | 662 fps a 1604×1100 · 12 bits | ~1,67 GB/s por cámara |
| Cámara de eventos (SilkyEvCam / Prophesee) | USB 3.0 | asíncrona, 10–50 MEv/s, marca de µs | 80–200 MB/s |
| Fibras ópticas Opsens CoreSens | Ethernet (SCPI + binario) | hasta 1 kHz por canal, 0,01 °C | ~0,26 MB/s |
| Termo-anemómetro ToF | FPGA Arty S7 por UART | >400 Hz | decenas de KB/s |
| IMU + GPS (ESP32) | serie hasta 2 Mbps | cientos de Hz + PPS | decenas de KB/s |

### De dónde viene

NECTAR **unifica y reemplaza** dos programas anteriores:

* `pastam_cam` — control de la cámara Emergent en Python/PyQt6. Su capa de
  acceso al eSDK (`evt/`) se migró **intacta** porque ya estaba validada contra
  `EmergentCameraC.dll` real.
* `main_v14.1.py` — monolito de ~2150 líneas para cámara de eventos + IMU/GPS
  + reconstrucción + topografía (repositorio
  [EvCam-IMU-GPS](https://github.com/Llapra/EvCam-IMU-GPS)). Se conservó todo
  lo que funcionaba y se repartió en módulos.

Y antes de esos hubo dos programas en C++/Qt (*Turbumeter* para el arreglo de
cámaras y un cliente para la interrogadora de fibras). De ellos se rescató lo
que importaba: el protocolo real del acondicionador de fibras.

---

## 2. Instalación

### Requisitos

* **Python 3.11 o 3.12.** No usar una versión más nueva: los módulos de
  Metavision para Ubuntu 24.04 solo existen para 3.11 y 3.12. Ubuntu 24.04 trae
  3.12 de fábrica, y con esa versión está probado.
* **Linux (Ubuntu 24.04) o Windows.** El PC de campaña del laboratorio usa
  Ubuntu 24.04 con kernel 6.17 congelado (ver más abajo por qué).
* Para la campaña: equipo con RAM amplia (128 GB en el equipo previsto) y
  **SSD NVMe rápido**, que es lo que de verdad limita la duración de una toma.

Hay dos piezas que **no se instalan con pip** porque son software del
fabricante: el soporte de la cámara de eventos (OpenEB + plugin de CenturyArks)
y el eSDK de Emergent. Las instrucciones de cada sistema las incluyen.

### Opción A · Ubuntu 24.04 (PC de laboratorio)

**1. Descargar el programa.** Cada programa vive en su propia carpeta dentro
de `~/Codigos`:

```bash
cd ~/Codigos
git clone https://github.com/Llapra/NECTAR.git
cd NECTAR
```

**2. Cámara de eventos: OpenEB + plugin SilkyEvCam** (una sola vez por PC).

OpenEB es la parte libre y gratuita de Metavision (Prophesee) y trae los tres
módulos que usa NECTAR: `metavision_core`, `metavision_hal` y
`metavision_sdk_core`. El SDK completo de Metavision 5 es de pago y **no hace
falta**.

```bash
curl -L https://propheseeai.jfrog.io/artifactory/api/security/keypair/prophesee-gpg/public > /tmp/propheseeai.jfrog.op.asc
sudo cp /tmp/propheseeai.jfrog.op.asc /etc/apt/trusted.gpg.d
sudo add-apt-repository 'https://propheseeai.jfrog.io/artifactory/openeb-debian/'
sudo apt update
sudo apt install metavision-openeb libcanberra-gtk-module mesa-utils ffmpeg
```

La SilkyEvCam no es una cámara de Prophesee: necesita además el **plugin de
CenturyArks**. Para OpenEB 5.3 corresponde el plugin **v5.2.0 para Ubuntu**
(sirve para VGA/Gen31, HD y HD-Lite), descargable desde
<https://centuryarks.com/en/download/>. Se descomprime y se ejecuta su
instalador:

```bash
cd SilkyEvCam_Plugin_Installer_for_ubuntu_v5.2.0
chmod +x CA_Silky_installer.sh
./CA_Silky_installer.sh
```

El instalador deja el plugin en `/usr/lib/CenturyArks/hal/plugins`, define
`MV_HAL_PLUGIN_PATH` y agrega las reglas USB `ca_device.rules` (para usar la
cámara sin `sudo`). Hay que abrir una terminal nueva para que la variable
aparezca.

Prophesee no soporta actualizar sus paquetes con `apt upgrade`, y el plugin
está hecho para esta versión. Por eso se congelan:

```bash
sudo apt-mark hold $(dpkg -l | awk '/^ii/ && /metavision/ {print $2}')
```

**3. Entorno virtual `venv-NECTAR`** (dentro de la carpeta del programa).

Se crea con `--system-site-packages` para que el entorno **vea** los módulos de
Metavision instalados en el sistema. Sin esa opción, NECTAR no encuentra la
cámara de eventos.

```bash
python3 -m venv ~/Codigos/NECTAR/venv-NECTAR --system-site-packages
echo 'export PYTHONNOUSERSITE=true' >> ~/.bashrc
source ~/.bashrc
source ~/Codigos/NECTAR/venv-NECTAR/bin/activate
python -m pip install --upgrade pip
```

**4. Librerías de Python.** Primero las que pide OpenEB y después las de
NECTAR, en ese orden, para que pip respete las versiones que fija OpenEB:

```bash
python -m pip install -r /usr/share/metavision/python_requirements/requirements_openeb.txt
python -m pip install -r requirements.txt
```

**5. Comprobar.**

```bash
python -c "import metavision_core, metavision_hal, metavision_sdk_core; print('Metavision OK')"
python main.py --check
```

Con la SilkyEvCam conectada a un puerto USB 3.0, `silkyevcam_platform_info`
muestra modelo, número de serie y firmware, y `metavision_viewer` abre una
ventana con los eventos en vivo.

**6. Cámara Emergent (eSDK para Linux).** Emergent publica un eSDK para
**Ubuntu 24.04 con kernel 6.17** (descarga con cuenta en
<https://emergentvisiontec.com/downloads/>). Su instalador trae los drivers de
la tarjeta de red y los compila para ese kernel exacto, por eso el kernel del
PC de laboratorio está congelado en 6.17. Las cámaras HB-1800-S de 25GigE
necesitan una tarjeta **NVIDIA/Mellanox ConnectX de 25G (SFP28)** en una ranura
PCIe de al menos x8; la red de la placa madre no sirve. NECTAR busca la
librería `libEmergentCameraC.so` en `/opt/EVT/eSDK/lib`, `/opt/EVT/lib` y en la
carpeta que indique la variable `EMERGENT_DIR`.

> **Estado:** el camino del eSDK en Linux está escrito pero **todavía no se ha
> probado con la cámara real**. Ver la sección 15.

### Opción B · Windows

```bat
python -m venv venv-NECTAR
venv-NECTAR\Scripts\activate
pip install -r requirements.txt
python main.py --check
```

**Cámara de eventos:** instalador de Metavision para Windows más el plugin de
CenturyArks (mismo sitio de descargas).

**Cámara Emergent:** eSDK 2.49 instalado en `C:\EVT\eSDK` y la variable de
entorno:

```bat
setx EMERGENT_DIR C:\EVT
```

> **Nota sobre las DLL en Windows.** Desde Python 3.8, `LoadLibrary` ya no
> busca dependencias en `PATH`, así que no basta con que el instalador del SDK
> lo haya configurado. `nectar/devices/emergent/_ffi.py` registra las carpetas
> del SDK con `os.add_dll_directory` y mantiene vivos los *cookies* que devuelve
> (si se liberan, Windows deja de encontrar las DLL).

### Qué dice `--check`

`--check` imprime una lista con ✔ y ✖ por dependencia, diciendo para qué sirve
cada una. Solo `PyQt6` y `numpy` son obligatorias: lo que falte de la lista
opcional desactiva su función y el programa lo avisa en la interfaz. Sin
OpenEB, las cámaras de eventos funcionan solo en **modo simulado**; sin eSDK,
las cámaras Emergent funcionan solo en **modo demostración** (con el generador
de patrones sintéticos que ya traía `pastam_cam`).

### Actualizar NECTAR

Las versiones nuevas se publican en GitHub; nunca hay que copiar carpetas a
mano. En el PC de laboratorio:

```bash
cd ~/Codigos/NECTAR
git pull
```

`git pull` baja solo lo que cambió. El `venv-NECTAR` y los datos no se tocan.
Si la versión nueva cambió el `requirements.txt`, después del pull:

```bash
source venv-NECTAR/bin/activate
python -m pip install -r requirements.txt
```

Para una campaña conviene fijar la versión ya probada, por ejemplo la 1.0.0:

```bash
git checkout v1.0.0
```

y para volver a la más reciente, `git checkout main` y `git pull`. Qué cambió
en cada versión está en [CHANGELOG.md](CHANGELOG.md). Cada sesión guarda en su
manifiesto la versión del programa que la grabó.

---

## 3. Arrancar el programa

Con el entorno activado y dentro de la carpeta del programa:

```bash
cd ~/Codigos/NECTAR
source venv-NECTAR/bin/activate
python main.py
```

Opciones:

| Opción | Para qué |
|---|---|
| `--demo` | Fuerza el modo simulado en **todos** los instrumentos, incluso si hay hardware conectado. |
| `--base-dir RUTA` | Carpeta base donde se crean las sesiones. Se puede cambiar después desde la pestaña 1. |
| `--check` | Informe de dependencias y salir. |
| `--tab N` | Abre directamente la pestaña N (1–11). |

### Fuera de `--demo` no existen dispositivos simulados

Esto es una regla dura y conviene entender por qué. Si el hardware no aparece,
el programa **muestra el error del hardware** y no ofrece nada más: sin SDK de
Metavision, sin eSDK de Emergent, sin permisos sobre el puerto serie, lo que
sea. No aparece una `EvCam1` con el LED verde que en realidad es un generador
de ruido de Poisson.

La razón: una cámara simulada que aparece sola puede pasar desapercibida en una
noche de campaña y dejar una carpeta llena de datos inventados con aspecto de
medición. Es el peor fallo posible de un programa de adquisición, porque no se
descubre hasta el análisis, cuando ya no hay forma de volver a tomar los datos.

Hay tres cerrojos, no uno:

1. los descubridores de cada familia solo ofrecen simulados con `--demo`;
2. `DeviceWorker.can_write()` rechaza el armado de un worker simulado fuera de
   `--demo`, por si alguno se creara por otra vía;
3. dentro de `--demo`, todo lo que se escribe queda marcado: la carpeta de
   sesión lleva sufijo `_DEMO`, dentro queda un archivo
   `ESTO_ES_SIMULACION_NO_SON_DATOS.txt`, cada archivo lleva `_SIMULADO` en el
   nombre y los metadatos dicen `"simulado": true` con un campo `AVISO`.

### El modo demo no es un adorno

Con `--demo` funcionan de verdad:

* el generador poissoniano de eventos sobre una grilla de 10 × 10 manchas con
  deriva (el *spot wander* que produce la turbulencia);
* el generador de patrones de la cámara Emergent;
* seis canales de fibra con fluctuación térmica correlacionada;
* el IMU con un modo mecánico a ~23 Hz y un PPS por segundo (que **ancla la
  hora UTC**, igual que el GPS real);
* el anemómetro emitiendo tiempos de vuelo coherentes con ~14 °C y ~1,5 m/s.

O sea que el disparo síncrono de seis instrumentos, la estructura de carpetas,
el presupuesto de disco, el asistente de enfoque y las once pestañas se pueden
ensayar completos sin tocar el banco óptico. **Conviene aprender el programa
así antes de la campaña.**

---

## 4. La idea central: la pestaña 1 manda

No hay un cable de disparo común entre una cámara USB, una cámara de 25GigE, un
acondicionador de fibras por Ethernet y un ESP32 por puerto serie. Lo que sí se
puede garantizar es que **todos empezaron a grabar en el mismo instante
conocido**, y que ese instante quedó escrito con el reloj monotónico del host.
Eso es sincronización por software, y es suficiente para alinear las series
después, que es lo que necesita el análisis tomográfico.

(El sincronismo exacto por hardware sigue existiendo donde el hardware lo
permite: dos cámaras de eventos con el cable Hirose se sincronizan en
MASTER/SLAVE, y eso se configura en la pestaña 2.)

### El protocolo de 5 fases

**Fase 1 · Verificación.**
Se listan los instrumentos marcados para participar y en estado armable. Se
calcula cuánto va a ocupar la sesión **y cuánto va a ocupar el archivo más
grande de todos**, y se valida el volumen de destino: que exista, que se pueda
escribir, que no sea FAT32 (donde todo archivo muere a los 4 GB sin aviso
útil), y que el espacio alcance dejando el margen de seguridad de 2 GB. Si algo
no cuadra se dice **antes** de tocar ningún instrumento, con el motivo exacto.
Si no hay ningún instrumento listo, el disparo se rechaza.

**Fase 2 · Armado.**
Se crea la carpeta de la sesión **con el árbol completo por adelantado** —una
carpeta por familia y una por dispositivo, todas creadas ya— y se manda
`arm_recording()` a cada worker con la suya. Crear el árbol entero ahora, y no
al primer dato de cada instrumento, es lo que convierte "el disco está montado
en solo lectura" en un error que aparece con todos los equipos parados, en vez
de a los diez minutos y solo en la cámara que llegó tarde.

El armado real ocurre **en el hilo de cada instrumento** (confirmar el anillo
de memoria página por página, abrir descriptores), porque reservar cientos de
megabytes en el hilo de la interfaz la dejaría congelada. El orquestador espera
a que *todos* reporten `ARMED`, con un tope de 20 segundos. Si alguno no llega,
se cancela el armado de todos: es preferible no grabar que grabar un dataset
cojo sin saberlo.

**Fase 3 · Disparo atómico.**
Se fija T0 con el reloj monotónico del host y se despacha `start_recording(t0)`
a todos los workers armados, uno tras otro y sin nada en medio. Cada
instrumento estampa ese T0 en sus metadatos.

**Fase 4 · Vigilancia.**
Mientras se graba, cada dos segundos se mira el espacio libre del destino. Al
bajar del margen de 2 GB la sesión **se detiene sola y ordenadamente**, con
tiempo para cerrar archivos y sellar el manifiesto. Llenar un disco a la fuerza
no deja "todo menos el final": deja archivos truncados sin cierre y, a veces,
un sistema que no responde.

**Fase 5 · Parada y sellado.**
Se manda `stop_recording()` a todos y **se espera a que cada uno confirme que
cerró su archivo** antes de sellar `session_manifest.json`. Los resúmenes por
instrumento —frames perdidos, bytes escritos, errores— llegan de forma
asíncrona desde los hilos de cada uno, y sellar antes dejaba el manifiesto sin
la información que más importa. Al final se añade además un **inventario** de
lo que de verdad quedó en disco: no lo que los instrumentos creen que grabaron,
sino los archivos que existen y su tamaño.

### Durante la grabación, la interfaz se congela

Cambiar un bias, mover un ROI o quitar un instrumento a mitad de toma deja los
metadatos ya escritos describiendo otra cosa, y eso no se reconstruye después.
Así que mientras hay una sesión armada, grabando o sellando: las demás pestañas
quedan deshabilitadas, las casillas de inclusión y los alias no
se pueden tocar, y el botón de quitar dispositivo no responde (con el motivo en
la barra de estado). Lo único vivo es la pestaña 1, que es donde está DETENER.

### Si un instrumento falla a mitad de camino

Los demás siguen grabando: cada uno vive en su propio hilo y ese es el
requisito de aislamiento del dossier. Pero **no en silencio**:

* el LED de ese equipo pasa a **rojo parpadeante** y se queda así hasta que el
  operador pulse «Reconocer alarmas» —no se apaga solo aunque el equipo se
  recupere, porque esa toma ya tiene un agujero—;
* el suceso queda escrito en la bitácora de la sesión con su hora exacta y su
  desfase respecto a T0;
* queda anotado en el manifiesto, en `alarmas` y en el resumen de ese
  dispositivo.

Los estados de error siguen distinguiendo **recuperable** (el worker reintenta
solo, LED ámbar) de **fatal** (requiere intervención, LED rojo, sale del
disparo global).

---

## 5. La base de tiempo

Tres relojes distintos conviven en el sistema:

* la cámara Emergent estampa cada frame con su contador interno
  (`GevTimestamp`), que no tiene relación con la hora del mundo;
* la cámara de eventos entrega marcas en microsegundos relativas al inicio de
  su stream;
* el ESP32 cuenta `micros()` desde que arrancó, pero además recibe el **PPS del
  GPS**, que sí está anclado a UTC.

La regla de NECTAR (`nectar/core/time_sync.py`):

1. **El reloj monotónico del host manda.** Cada muestra de cada instrumento se
   estampa al llegar con `time.perf_counter_ns()`: monotónico, inmune a NTP y
   al cambio de hora, y barato de leer.
2. **T0 es el instante del disparo global**, y queda escrito en el manifiesto.
   Todos los tiempos relativos del dataset se miden desde ahí.
3. **UTC entra como un ancla, no como base.** Cuando el IMU recibe la primera
   trama de coordenadas válida junto a un PPS, registra el par
   *(época UTC, reloj del host)*. Desde ese momento **cualquier** marca
   monotónica de **cualquier** instrumento se puede convertir a UTC con el
   mismo offset.

Eso resuelve explícitamente lo que en la especificación original quedaba
implícito: el reloj del host es la columna vertebral y el GPS aporta el offset
absoluto. El estado del ancla se ve en la pestaña 1 y en la 7; **sin ancla el
dataset queda con tiempos relativos a T0 y no se puede cruzar con la telemetría
del telescopio**, así que conviene verificarlo antes de una toma.

---

## 6. Dónde queda cada archivo

Cuando la pestaña 1 dispara una grabación:

```
<destino>/NECTAR_<sesión>_<AAAAMMDD>_<HHMMSS>/
├── session_manifest.json          qué había, T0, ancla UTC, resúmenes, inventario
├── nectar_sesion.log              la bitácora completa de esta toma
├── EvCam/
│   ├── EvCam1/
│   │   ├── EvCam1_20260917_021500.raw             ← UN archivo por toma
│   │   ├── EvCam1_20260917_021500_meta.json       biases, ROI, T0, modelo
│   │   └── EvCam1_20260917_021500_checkpoint.json hasta dónde es válido
│   └── EvCam2/…
├── Emergent/
│   ├── EmerCam1/
│   │   ├── EmerCam1_20260917_021500.raw
│   │   ├── EmerCam1_20260917_021500_frames.csv
│   │   ├── EmerCam1_20260917_021500_meta.json
│   │   └── EmerCam1_20260917_021500_checkpoint.json
│   └── EmerCam2/…
├── Fibras/
│   └── Fibras1/
│       ├── Fibras1_20260917_021500.csv     (una fila por barrido)
│       ├── Fibras1_20260917_021500.bin     (crudo, re-parseable)
│       └── Fibras1_20260917_021500_meta.json
├── Anemometro/
│   └── Anemometro1/Anemometro1_20260917_021500.txt
├── IMU_GPS/
│   └── IMU1/IMU1_20260917_021500.bin + _meta.json
└── Reconstruccion/
    └── Recon_EvCam1/Recon_EvCam1_20260917_021500.mp4
```

Una carpeta por **familia**, y dentro una por **dispositivo**. Todo el árbol se
crea **al armar**, antes de que nadie escriba un byte.

**Un archivo por toma y por instrumento.** No hay sufijo `_bNNN` salvo que
alguien active a propósito el modo por bloques en la pestaña 1. En la cámara de
eventos esto no es un capricho: cortar un `.raw` obliga a cerrar y reabrir el
canal de grabación del HAL de Metavision, y entre esas dos llamadas el sensor
sigue disparando. Esos eventos se pierden y no hay forma de evitarlo con su
API. Con un solo archivo no hay costura.

En modo demostración el nombre de la carpeta lleva `_DEMO`, dentro queda un
archivo testigo y cada archivo de datos lleva `_SIMULADO`.

Cuando una pestaña graba **por su cuenta** (su propio botón GRABAR), se usa la
misma estructura pero colgando de la carpeta que esa pestaña tenga
configurada:

```
<carpeta de la pestaña>/NECTAR_EvCam_20260917_031000/
├── EvCam1/…
└── EvCam2/…
```

### Ya no hay área de preparación intermedia

La primera versión escribía en un SSD intermedio y un hilo movía cada bloque
cerrado al destino. Se quitó. **Se escribe directo donde el operador eligió**,
que además es lo que ve en pantalla. Una copia intermedia agrega un punto de
fallo, duplica el espacio necesario y deja datos a medio camino si el programa
muere; y el equipo de campaña tiene discos rápidos, así que no compraba nada.

Antes de aceptar una carpeta de destino, el programa la valida: que exista, que
se pueda escribir, qué sistema de archivos hay debajo y cuánto espacio queda.
Un destino en FAT32 se rechaza de plano. Y está el botón **⏱ Probar
velocidad**, que escribe 512 MB de verdad y reporta el caudal sostenido, el
peor bloque y el percentil 99 — el peor bloque es la cifra que importa, porque
un promedio bueno con un bloque de 900 ms esconde una pérdida de frames.

---

## 7. Formatos de archivo

### `.raw` de la cámara Emergent

Los bytes tal como los entregó la cámara, concatenados, sin encabezado. Se
interpretan con su `_meta.json`:

```python
import json, numpy as np

meta = json.load(open("EmerCam1_..._meta.json", encoding="utf-8"))
geometry = meta["geometria"]
n = meta["resumen"]["frames_escritos"]

data = np.fromfile("EmerCam1_....raw", dtype=np.uint8)
data = data.reshape(n, geometry["frame_bytes"])

# Mono12 (2 bytes por píxel, valores 0..4095)
frame0 = data[0].view(np.uint16).reshape(geometry["height"], geometry["width"])
```

Para `Mono12Packed` (1,5 bytes por píxel) usar
`nectar.core.pixels.to_array`, que ya hace el desempaquetado.

### `_frames.csv` (índice temporal, una fila por frame)

```
indice,frame_id,timestamp_dispositivo_ns,host_ns,host_rel_ms,offset_bytes
0,1,184467440737,1464389166191,0.000000,0
1,2,184467441247,1464389167701,1.510000,3528800
```

La columna `offset_bytes` dice en qué byte del `.raw` empieza cada frame, lo
que permite saltar directo a uno sin leer los anteriores, y cruzarla con
`_checkpoint.json` para saber cuál fue el último frame íntegro si la toma
murió sin cerrar.

`frame_id` es el contador de 16 bits de GVSP (no usa el 0: tras 65535 vuelve a
1). Sin este archivo, un `.raw` es una pila de bytes que solo se puede
interpretar de memoria; **con** él se puede comprobar si hubo saltos y alinear
la grabación con cualquier otra señal medida en paralelo.

### `_meta.json`

Geometría, formato de píxel, todos los parámetros de la cámara en el momento de
grabar, ROI con sus incrementos, temperatura del sensor, T0, estado del reloj,
alias e identidad del equipo, y el resumen: frames aceptados, escritos,
perdidos por disco, caudal medio, peor `write()` y ocupación máxima del anillo.

### `_checkpoint.json` (el seguro contra el corte de energía)

Cada cinco segundos, mientras se graba, se sella un archivo pequeño con el
offset en bytes válido, la cuenta de frames o eventos, el reloj del host y el
desfase respecto a T0 — y **ese** archivito sí se fuerza a disco con `fsync`.
Cuesta nada porque son unos cientos de bytes, no el flujo.

Para qué sirve: si la toma muere sin cierre limpio (corte de luz, cuelgue), el
`.raw` sigue siendo legible hasta donde alcanzó a bajar del caché, pero nadie
sabe dónde está ese punto. El checkpoint lo dice exactamente, en vez de dejar
que alguien lo adivine un año después.

```json
{
  "archivo": "EvCam1_20260917_021500.raw",
  "bytes_validos": 41238528,
  "eventos_contados": 10309632,
  "host_ns": 1464389166191,
  "t0_host_ns": 1464380000000,
  "desfase_t0_s": 9.166,
  "duracion_s": 9.166,
  "hora_local": "2026-09-17 02:15:09"
}
```

### `.raw` de la cámara de eventos

Con hardware real es el **formato EVT3 nativo** que escribe el HAL de
Metavision (`log_raw_data`), totalmente compatible con las herramientas de
Prophesee. NECTAR **no le toca un byte**: todo lo que necesita registrar va en
un `_meta.json` hermano. Meterle campos extra al `.raw` lo rompería o, peor, lo
dejaría abriéndose en unas versiones del SDK y no en otras.

El `_meta.json` de la cámara de eventos lleva: biases aplicados, Δt de
integración, modo de sincronismo, modelo de sensor, número de serie (y si se
pudo leer o no), alias del equipo, T0, desfase respecto a T0 y el ROI.

Una advertencia sobre el ROI que conviene tener escrita: **las coordenadas de
los eventos siguen siendo absolutas respecto del sensor completo**. Un evento
del centro de un ROI que empieza en x=200 se graba con x≈520, no con x≈120.
Eso es lo correcto y es lo que hace que el archivo se siga abriendo sin
sorpresas en el visor de Metavision, pero hay que saberlo al analizar.

En modo simulado es un formato propio, declarado en el encabezado del archivo
para que nadie confunda una grabación simulada con una real:

```
NECTAR_SIM_EV1 width=640 height=480 record=12B:x_u16,y_u16,p_i16,pad_i16,t_i32_us
```

### `.csv` de las fibras

Una fila por **barrido** (un instante de muestreo con todos los canales):

```
host_ns,t_rel_s,t_unix,barrido,ch1_C,ch2_C,ch3_C,ch4_C,ch5_C,ch6_C
1587952069076,0.332172,1789668525.33,0,13.3792,14.0276,14.3133,14.0851,14.3594,15.1710
```

El equipo entrega las muestras canal por canal, una tras otra, y un paquete TCP
puede traer decenas de barridos completos; el programa reconstruye el barrido
(cuando el índice de canal deja de crecer, se cerró un instante). El `.bin` que
lo acompaña guarda los paquetes crudos de 8 bytes, para poder re-parsear la
sesión si algún día cambia la interpretación.

### `.bin` del IMU/GPS

Flujo de paquetes de 18 bytes con cabecera de 2 bytes:

| Cabecera | Contenido | Formato |
|---|---|---|
| `0xBBAA` | IMU: `micros()` + ax, ay, az, gx, gy, gz | `<H L hhhhhh` |
| `0xBBCC` | PPS del GPS: `micros()` del flanco | `<H L` |
| `0xBBDD` | coordenada + fecha/hora UTC + validez | `<H f f H B B B B B B` |
| `0xFFEE` | **ATM**: sello de época UTC absoluta (raíz del archivo) | `<H d` |

Escalas: **4096 LSB/g** y **65,5 LSB/(°/s)** (rango ±8 g y ±500 °/s).

El paquete ATM es la pieza que ancla el archivo completo a UTC: lo escribe el
worker en cuanto llega el primer PPS válido, y con él cualquier muestra
posterior se pasa a hora absoluta sumando su tiempo relativo. La pestaña 9 lo
decodifica todo.

### `.mp4` de la reconstrucción

Video de la imagen integrada, códec `mp4v`, 640×480, a los fps configurados.

### `session_manifest.json`

El resumen de la sesión completa: programa y versión, nombre, fechas, destino,
si fue modo demostración, modo de escritura, margen de disco, retrato del
volumen (sistema de archivos, espacio, punto de montaje), presupuesto
calculado, duración pedida y real, T0 monotónico, estado del reloj y del ancla
UTC, ficha de cada instrumento armado (etiqueta, alias, identidad, familia,
caudal estimado, descriptor del equipo), las alarmas si las hubo, el resumen
final por instrumento y —al cerrar— el **inventario**: la lista de los archivos
que de verdad quedaron en la carpeta con su tamaño, que es la comprobación que
no depende de lo que los instrumentos crean que grabaron.

---

## 8. Tutorial de uso

### 8.1 Primera vez (modo demo, sin hardware)

```bash
python main.py --demo
```

1. **Pestaña ① Adquisición.** Arriba, `📁 Elegir carpeta base`: elige dónde se
   van a guardar las sesiones. Escribe un nombre en **Sesión** (por ejemplo
   `Prueba`) y deja marcada la marca de tiempo automática.

2. **Pestaña ② EvCam.** Pulsa `⟳ DETECTAR` y después `+ AGREGAR EVCAM`.
   Aparece un panel con la vista previa de la cámara simulada: se ve la grilla
   de 10 × 10 manchas moviéndose (eso es el *spot wander*). Agrega una segunda
   cámara: aparece **debajo**, con su propia visualización a tamaño real. Baja
   con la rueda del mouse. Mueve los biases en la columna derecha y fíjate que
   la tasa de eventos cambia en **las dos** cámaras: los biases son
   compartidos a propósito.

3. **Pestaña ③ Emergent.** `⟳ DETECTAR` → `+ AGREGAR EMERGENT`. Aparecen los
   controles de la cámara en la columna derecha, **generados a partir de lo
   que la cámara reporta**: exposición, ganancia, ROI, formato de píxel. Los
   marcados con `⟳` reinician el stream al cambiarlos.

4. **Pestañas ⑤ Fibras, ⑥ Anemómetro, ⑦ IMU-GPS.** Lo mismo: detectar y
   agregar. En Fibras vas a ver seis fibras con su temperatura refrescándose
   una vez por segundo. En IMU-GPS vas a ver el contador de PPS subir y, al
   segundo o dos, el recuadro **Ancla UTC** ponerse verde: ese es el GPS
   fijando la hora absoluta de toda la sesión.

5. **Vuelve a la pestaña ①.** Ahora las tarjetas de familia tienen LED verde y
   un contador `incluidos/total`. Despliega alguna con el triángulo `▽` para
   ver los dispositivos concretos; desmarca uno y fíjate que su LED se pone
   gris y el presupuesto de caudal baja: eso es excluirlo de la toma **sin**
   desconectarlo.

6. **Mira el panel central.** Dice cuántos instrumentos están listos, cuántos
   MB/s suman, cuánto espacio libre hay y **en cuánto tiempo se llena el
   disco** a ese caudal. Si eliges una duración que no cabe, el número se pone
   rojo y el disparo se va a rechazar en la fase 1.

7. **Elige el tiempo de grabación** abajo a la izquierda (o déjalo en "Sin
   límite") y la duración de cada bloque.

8. **Pulsa `● INICIAR`.** En el monitor vas a ver las cuatro fases pasar, el
   T0 en nanosegundos, y después el cronómetro corriendo. El botón ahora dice
   `■ DETENER`.

9. **Detén la grabación** (o espera a que se cumpla el tiempo) y abre la
   carpeta de destino. Ahí está el árbol completo con una carpeta por familia,
   y el `session_manifest.json` con todo lo que pasó.

10. **Pestaña ⑨ Conversor IMU.** `📂 ABRIR CARPETA COMPLETA` y elige
    `…/IMU_GPS/IMU1`. Decodifica todos los bloques, muestra la aceleración, el
    giróscopo, el espectro de vibración (vas a ver el pico del modo simulado a
    ~23 Hz) y el RMS en el tiempo. Exporta a `.csv` para comprobar.

### 8.2 Una campaña real, paso a paso

**Antes de salir al telescopio**

1. `python main.py --check` — confirma que están los SDK y las dependencias.
2. Arranca sin `--demo`, elige la carpeta de destino y pulsa **⏱ Probar
   velocidad**. Anota el **peor bloque**: es la cifra que decide si ese disco
   aguanta. Si el veredicto sale en ámbar o rojo, cambia de disco ahora y no en
   la cúpula.
3. Conecta cada instrumento en su pestaña, uno por uno, confirmando que su LED
   se pone verde antes de pasar al siguiente. Ponle a cada uno su **alias**
   («Norte», «Pilar M2»): queda guardado para todas las campañas siguientes.

**Puesta a punto óptica**

4. **Pestaña ② EvCam**: ajusta la ventana de integración Δt y los biases hasta
   que las manchas se vean nítidas y el ruido de fondo sea tolerable.
5. **Pestaña ⑧ Enfoque**: elige la cámara, modo Manual, y pulsa `▶ ACTIVAR
   ASISTENTE`. Gira el lente despacio siguiendo la flecha grande. Primero
   estarás en etapa gruesa (EPR) y la barra «zona gruesa» irá subiendo; al
   llegar al 100 % se enciende la etapa fina y aparecen HFE en dB y la tasa.
   Sigue girando hasta que diga `✔ FOCO ALCANZADO · te pasaste un poco`, y
   entonces **retrocede muy poco**. El asistente es causal: para saber que
   había un pico hay que haberlo pasado.
6. **Pestaña ④ Reconstrucción**: mira el blanco como imagen de intensidad para
   confirmar el encuadre. τ ≈ 0,1 s es un buen punto de partida.
7. **Pestaña ③ Emergent**: ajusta exposición y ganancia; si necesitas más fps,
   activa `ROI en el sensor`, arrastra el rectángulo sobre la imagen y pulsa
   `Aplicar` (la cámara reporta el nuevo máximo de tasa sola). Si también
   quieres recortar la EvCam, es el mismo gesto en la pestaña ② — y ahí
   comprueba que el rótulo diga el porcentaje del sensor y no
   `⚠ ROI NO aplicado`.

**Verificaciones de tiempo y espacio**

8. **Pestaña ⑦ IMU-GPS**: confirma que el ancla UTC está en verde. Si no,
   espera a que el GPS fije; sin ancla el dataset no se puede datar en
   absoluto.
9. **Pestaña ①**: revisa el presupuesto. Si vas a grabar dos horas, pon el
   límite de tiempo y comprueba que «Necesario para la duración» esté en verde
   y que «Archivo mayor previsto» no sea absurdo para el sistema de archivos
   que muestra la línea «Volumen de destino».

**La toma**

10. Nombre de sesión descriptivo (por ejemplo `Tomografia_Cupula_Target01`).
11. Duración y tamaño de bloque. Bloques de 10–30 minutos son un buen
    compromiso: archivos manejables y poca pérdida si algo se corta.
12. Excluye lo que no vaya a participar (casillas de la pestaña 1).
13. `● INICIAR` (o **F5**).
14. Durante la toma, vigila en el monitor: **perdidos en el enlace** y
    **perdidos por disco** tienen que quedar en cero, y los bloques en tránsito
    no deberían acumularse.
15. `■ DETENER` (o **F6**) al terminar.

**Después**

16. Revisa que no haya quedado ninguna **alarma** encendida en la pestaña 1.
    Si la hay, el LED de ese equipo está rojo parpadeando y el motivo está en
    `nectar_sesion.log` con su hora exacta.
17. Abre `session_manifest.json`: el resumen por instrumento y, al final, el
    **inventario** de los archivos que de verdad quedaron y su tamaño.
18. Pestaña ⑨ para convertir el IMU al formato que use tu pipeline de análisis.

---

## 9. Las once pestañas, una por una

### ① Adquisición

**Función.** Dar inicio conjunto a todos los instrumentos conectados que estén
listos, y decidir dónde se guarda todo. Manda por sobre las demás pestañas.

**Qué hay.**

* *Barra superior*: carpeta base de destino, nombre de la sesión, marca de
  tiempo automática.
* *Columnas laterales*: una **tarjeta por familia** (EvCam, Emergent,
  Reconstrucción a la izquierda; Fibras, Anemómetro, IMU-GPS a la derecha).
  Cada tarjeta tiene un LED resumen, un contador `incluidos/total` y un
  triángulo `▽` que despliega la lista de dispositivos concretos, cada uno con
  su mini-LED, su casilla para incluirlo o excluirlo, su estado y su caudal.
  El nombre de la familia es un botón: lleva a su pestaña.
* *Panel central superior*: **estado de la sesión y presupuesto de
  almacenamiento** — instrumentos listos, caudal agregado, espacio libre,
  autonomía a ese caudal, espacio necesario para la duración pedida, estado del
  reloj (T0) y del ancla UTC, el sistema de archivos del destino, el **tamaño
  proyectado del archivo mayor** y el contador de alarmas. Es la información
  que hace falta *antes* de apretar el botón, y la que no se puede calcular a
  ojo con seis instrumentos cuyos caudales difieren en cuatro órdenes de
  magnitud.
* *Botón central*: `● INICIAR`, que al pulsarlo se convierte en `■ DETENER`.
  Debajo aparece «Reconocer alarmas» solo si hay alguna.
* *Monitor*: fase actual, cronómetro, GB escritos y un registro con lo que pasa
  en cada instrumento.
* *Abajo a la izquierda*: **tiempo de grabación**. Se ve una sola cosa a la
  vez: o el rótulo `SIN LÍMITE`, o un campo de minutos, y un botón que cambia
  de uno a otro. Antes había un desplegable *y* una casilla de minutos, y una
  mandaba sobre la otra según una regla que había que recordar; con seis
  instrumentos armados a las tres de la mañana eso es una trampa. Debajo, la
  casilla «Partir en bloques», apagada por omisión y con el aviso de lo que
  cuesta encenderla.
* *Abajo a la derecha*: **cuatro miniaturas** — dos cámaras de eventos y dos
  Emergent. Están limitadas a 4 fps y decimadas antes de convertir, así que
  casi no consumen cómputo: son para confirmar que las cámaras apuntan a donde
  deben, no para analizar.
* *En la barra superior*: **⏱ Probar velocidad**, que escribe 512 MB de verdad
  en el destino y da el veredicto contra el caudal que los instrumentos
  conectados van a pedir.

**Cuándo el LED se pone verde.** Cuando el dispositivo está de verdad en
condiciones de grabar (`READY` o `STREAMING`) **y** está incluido en la toma.
Un dispositivo excluido a mano se ve gris aunque el equipo esté perfecto.

**Cuándo se pone rojo parpadeando.** Cuando ese instrumento **falló durante una
grabación**. No se apaga solo aunque el equipo se recupere: esa toma ya tiene
un agujero y hay que saberlo. Se baja con «Reconocer alarmas».

**El alias.** Junto a cada dispositivo hay un campo de texto: escribe ahí el
nombre que uses en el banco («Norte», «Pilar M2», «Fibra del espejo»). Se
guarda anclado a la identidad física del equipo —número de serie, puerto o
IP— así que vuelve a aparecer en la próxima campaña. La etiqueta (`EvCam1`)
sigue nombrando la carpeta, para que renombrar en pantalla no parta la
trazabilidad de lo ya grabado.

---

### ② EvCam

**Función.** Cámaras neuromórficas de eventos. Es la parte del programa
anterior que ya funcionaba bien, replicada con sus valores calibrados.

**Lo que cambió.** Antes había exactamente dos cámaras cableadas en la
interfaz. Ahora es **modular aditiva**: cada cámara que se agrega suma su panel
a una columna desplazable, con su visualización **a tamaño real**. Se pueden
conectar cinco y se bajan con la rueda.

**Biases compartidos.** Un solo juego de voltajes para todas las cámaras de
evento. Es lo correcto físicamente: si dos cámaras miran el mismo blanco para
comparar sus series, tienen que tener el mismo umbral de detección.

| Bias | Rango | Qué hace |
|---|---|---|
| `bias_diff` | 250–350 (def. 299) | nivel de referencia del comparador. Se ajusta al final, y poco. |
| `bias_diff_on` | 300–600 (def. 374) | umbral para subidas de luz. Bajarlo = más sensible, más ruido verde. |
| `bias_diff_off` | 100–290 (def. 221) | umbral para bajadas de luz. Bajarlo = más ruido rojo. |
| `bias_refr` | 1300–1800 (def. 1500) | periodo refractario del píxel (µs). Subirlo filtra ruido térmico. |

**ROI: ahora recorta de verdad.** En la primera versión el ROI solo filtraba
la vista previa — el sensor seguía disparando en todo el cuadro, el flujo
llegaba entero por el USB y el `.raw` grabado contenía todo. Era, en la
práctica, un ROI que no servía para nada de lo que un ROI sirve.

Ahora se programa en el sensor con la facilidad `I_ROI` del HAL: se arrastra el
rectángulo rojo sobre la imagen y se pulsa **Aplicar ROI**. Los píxeles de
fuera dejan de generar eventos, la tasa baja de verdad, baja la ocupación del
enlace y el archivo contiene solo la región elegida. El rectángulo dibujado se
ajusta antes de mandarlo al múltiplo que el sensor acepta, y el rótulo de al
lado dice el estado **leído de la cámara**, no el de la casilla: si el HAL
rechazó el recorte, aparece en rojo `⚠ ROI NO aplicado en el sensor`. Si se
graba con un ROI pedido y no aplicado, se levanta alarma.

(Las coordenadas de los eventos siguen siendo absolutas del sensor completo:
ver §7.)

**Otros controles.** Ventana de integración Δt (200–100 000 µs), FPS de
pantalla (solo afecta a la visualización), modo de sincronismo por hardware
(STANDALONE / MASTER / SLAVE), volcado de telemetría al registro y el **modelo
de sensor** con sus tasas típica y de pico, que es lo que alimenta el
presupuesto de disco mientras la cámara no haya medido la suya. La SilkyEvCam
VGA por USB 3.0 no genera lo mismo que un IMX636 por Ethernet, ni de lejos, y
asumir el caudal del sensor equivocado arruina el presupuesto de una noche.

**Qué graba.** El flujo crudo de eventos en **un** `.raw` por toma. Con
hardware real, EVT3 nativo del HAL, sin tocar un byte, más su `_meta.json` y su
`_checkpoint.json` al lado. Se puede grabar desde acá (carpeta propia) o desde
la pestaña 1 (carpeta de la sesión).

---

### ③ Emergent

**Función.** Cámaras de fotogramas HB-1800-S-M por 25GigE. Misma filosofía
aditiva y misma disposición de controles que la pestaña 2.

**Los controles se generan solos.** No están escritos en el código: al
conectar, el programa le pregunta a la cámara qué parámetros GenICam soporta y
con qué mínimo, máximo y paso. Eso evita el problema del programa Qt anterior,
que tenía "1600×1096" y el tope de exposición escritos a mano cuando el sensor
es de 1604×1100. Si la cámara dice que el ancho va de 16 en 16, el control va
de 16 en 16, y no se puede pedir un valor que la cámara recorte en silencio.

Los parámetros que cambian el tamaño del frame (ROI, formato de píxel) están
marcados con `⟳`: se aplican parando el stream, aplicando y volviendo a
montarlo, todo dentro del hilo de adquisición. Están bloqueados mientras se
graba, porque el anillo de escritura está dimensionado para una geometría
concreta.

**La grabación va directo al SSD.** Es el cambio de fondo respecto a
`pastam_cam`, que grababa a RAM y volcaba al final:

|  | `pastam_cam` (antes) | NECTAR (ahora) |
|---|---|---|
| Camino de grabación | RAM completa → volcado al final | anillo de RAM → hilo escritor → SSD continuo |
| Límite de duración | ~75 s con 128 GB | el tamaño del SSD |
| Entre dos tomas | esperar el volcado | nada |
| RAM usada por cámara | toda la ráfaga | ~900 MB de anillo |
| Si el disco no alcanza | no aplica | cuenta *frames perdidos por disco* |

**Telemetría por cámara.** fps, geometría y MB por frame, frames recibidos,
**perdidos en el enlace** (por saltos del `frame_id` de GVSP), **perdidos por
disco**, temperatura del sensor, ocupación máxima del anillo y caudal real al
SSD.

**Controles de vista previa.** Decimación (1 de cada N píxeles) y estirado de
contraste por percentiles. Desempaquetar 1604×1100 en 12 bits cuesta
milisegundos, así que la conversión ocurre en un hilo aparte y decimada; no
afecta a lo que se graba.

---

### ④ Reconstrucción

**Función.** Ver el blanco como lo vería una cámara normal, integrando el flujo
de eventos. Era la pestaña de enfoque de `main_v14.1.py`.

**La física.** Cada evento dice que el logaritmo de la irradiancia del píxel
cruzó un umbral: `+c_on` si subió, `-c_off` si bajó. Sumarlos reconstruye
`log I`, y exponenciando se recupera la intensidad. Al integrador se le agrega
un decaimiento exponencial con constante `τ`, que es literalmente la memoria
visual de la imagen:

* `τ` corto (≈10 ms): solo se ve lo que se está moviendo ahora.
* `τ` largo (≈1 s): el blanco estático se ve estable, pero deja estelas.

Para la grilla de 100 manchas conviene un valor intermedio; por defecto 100 ms.

**No abre la cámara: se deriva de la que ya está abierta.** Este era un fallo
de fondo de la primera versión: la reconstrucción abría **su propio**
`EventsIterator` sobre la misma cámara. Contra el simulador funcionaba; contra
hardware real no puede funcionar, porque el HAL de Metavision entrega la cámara
en exclusiva y el segundo que la pida se encuentra el dispositivo ocupado — en
el mejor caso la reconstrucción no arranca, y en el peor le quita la cámara a
la grabación.

Ahora hay un solo lector —el de la pestaña 2— y la reconstrucción se suscribe a
su flujo. Además de correcto es mejor: **funciona mientras la cámara graba**,
que es justamente cuando se quiere mirar. Si no hay ninguna EvCam conectada, la
pestaña lo dice y espera; no inventa una.

**Controles.** `τ`, `c_on`, `c_off`, Δt de integración y fps del video.

**Qué graba.** El **video** de la reconstrucción (`.mp4`). Los datos crudos ya
los guarda la pestaña 2; acá lo que tiene valor es el registro visual de lo que
vio el operador. El video se escribe al ritmo configurado **independientemente
de la interfaz**: una pestaña en segundo plano no deja el video vacío.

---

### ⑤ Fibras

**Función.** Termometría óptica de alta velocidad: el *ground truth*
micrometeorológico contra el que se valida el `Cn²` óptico.

**Cómo está cableado.** Las fibras no llegan al PC: llegan al **acondicionador
de señal** (Opsens CoreSens), que las interroga ópticamente y publica las
temperaturas ya convertidas. El PC habla con ese equipo por Ethernet, con un
canal de **control** en comandos tipo SCPI y otro de **datos** en binario. Por
eso la unidad que se agrega acá es el acondicionador, y cada fibra es un canal
dentro de él: la lista numerada de fibras se despliega dentro del panel del
equipo y **se arma sola**, descubriendo qué (módulo, canal) están publicando.
Si hay más de un acondicionador, se agregan más equipos.

**Protocolo.** Cada muestra son 8 bytes:
`[slot/módulo][relleno][canal][relleno][float32 little-endian]`.
Comandos: `measure:start infi`, `measure:stop`,
`system0101:samplingrate <hz>`, `systems:measure:rate <hz>`, `*IDN?`.

**Frecuencias admitidas** (datasheet IMP0160 CoreSens Rev3.8): cada módulo de
doble canal corre **hasta 1 kHz por canal**, con techo según el módulo:

| Módulo | Tecnología | Máximo |
|---|---|---|
| `WLX-2` | interferometría de luz blanca (WLPI) | **500 Hz** |
| `WLX-2S` | WLPI, versión rápida | **1000 Hz** |
| `GSX-2` | banda prohibida de semiconductor (SCBG, sondas OTG) | **1000 Hz** |

No se puede forzar cualquier frecuencia: el equipo acepta un conjunto discreto
(1, 10, 50, 100, 250, 500, 1000 Hz) con ese techo. La pestaña ofrece la lista
válida y además deja escribir un valor a mano, pero lo **valida**: si excede el
máximo del módulo, se recorta al legal más cercano y queda dicho en el
registro, en vez de mandar un comando que el equipo ignoraría en silencio.
Un chasis acepta 1 a 13 módulos (2 a 26 canales), y hasta 1300 canales en
configuración multi-chasis por EtherCAT.

**Qué se ve.** Una fila por fibra con su número, su temperatura y su tasa real
de captación; curvas de temperatura en el tiempo; barras con todos los canales;
y el diagnóstico de turbulencia: temperatura media, rango entre sondas y la
**función de estructura térmica** `D_T(r) = ⟨[T(x) − T(x+r)]²⟩` entre sondas
vecinas, que es lo que dice si existe el subrango inercial de Kolmogorov dentro
del volumen confinado.

La cifra grande se refresca **una vez por segundo**, aunque se mida a 1 kHz: a
esa velocidad los dígitos serían ilegibles. La grabación guarda todas las
muestras.

---

### ⑥ Anemómetro

**Función.** Termo-anemómetro ultrasónico de tiempo de vuelo. **El instrumento
está en construcción** (FPGA Arty S7, pulsos a 32,8 kHz), así que esta pestaña
deja armada toda la estructura sin inventar un protocolo que después no calce
con el firmware.

**Tres modos de ingesta.**

* `crudo` — vuelca los bytes tal como llegan. Sirva el formato que sirva, los
  datos quedan guardados y se pueden re-interpretar.
* `lineas` — espera texto `t_ida_ns,t_vuelta_ns` terminado en salto de línea,
  que es lo que suele emitir un prototipo. Con eso ya se calcula y se grafica
  temperatura y viento en vivo.
* `binario` — paquetes de tamaño fijo. Cuando el firmware defina su
  estructura, lo único que hay que escribir es `parse_packet()` en el worker.

**La física que se aplica.** En aire seco `c = √(γ·R·T)` con γ = 1,4 y
R = 287,05 J/(kg·K). Midiendo el tiempo de vuelo en los dos sentidos entre
transductores separados una distancia `d`:

```
c = (d/2) · (1/t_ida + 1/t_vuelta)   →  temperatura
v = (d/2) · (1/t_ida − 1/t_vuelta)   →  viento sobre el eje
```

La ventaja sobre la fibra óptica es que mide la temperatura del **camino
óptico** sin inercia térmica: no hay un sensor que tenga que llegar al
equilibrio con el aire.

**Controles.** Velocidad del puerto, modo de ingesta, frecuencia deseada,
separación entre transductores (entra directo en el cálculo, hay que medirla
bien en el montaje) y un panel de referencia con `c` a 14 °C y el tiempo de
vuelo esperado, para verificar el orden de magnitud cuando lleguen los primeros
datos.

---

### ⑦ IMU-GPS

**Función.** Doble: mide **vibración** (que contamina el wander de las manchas
y hay que poder descontarla) y es el **reloj absoluto** de todo NECTAR.

**Lo que cambió.** Antes había dos ESP32 cableados en la interfaz; ahora cada
módulo agrega su propio recuadro con lo que sabe hacer: frecuencia de paquetes,
caudal, errores de sincronismo, contador de PPS, coordenada del GPS,
aceleración y giróscopo en vivo, vibración RMS y una curva de las tres
componentes de aceleración.

**El protocolo binario está intacto**: paquetes de 18 bytes, resincronización
por búsqueda de huella (salta directo al próximo candidato a cabecera en vez de
avanzar byte a byte) y volcado por bloques de 8 KB.

**Formato de grabación.**

* *Normal*: parsea por paquete y escribe el sello ATM de época UTC al inicio.
* *RAW*: vuelca el flujo serie sin interpretar. Más rápido y no puede perder
  nada por un error de sincronismo, pero el archivo no lleva el sello UTC.
* *Esperar el primer PPS antes de grabar* (recomendado): el archivo arranca con
  la época absoluta, que es lo que permite pasar todas las muestras a UTC.

**El recuadro del ancla** es lo primero que hay que mirar antes de una toma.

---

### ⑧ Enfoque

**Función.** Asistir el enfoque de la cámara de eventos. Implementa el método
de **Li et al.** completo, en dos etapas, tal como está en el documento del
proyecto, más el observador cinemático que hace falta para un lente sin
encoder.

**Por qué no es como enfocar una cámara normal.** Una cámara convencional se
enfoca maximizando el gradiente espacial de un fotograma. Un sensor de eventos
no entrega fotogramas y se comporta al revés: **desenfocado genera más
eventos**, porque el ensanchamiento del disco de Airy reparte cada transición
sobre muchos píxeles y cada uno dispara.

#### Etapa ① · Aproximación gruesa por razón de polaridad (EPR)

Se cuentan los eventos positivos y negativos en ventanas de 10 ms y se calcula
`R(t) = P/N`. La clave es que **no depende de la magnitud absoluta** de
eventos, así que resiste que el giro manual sea desparejo y que el ruido
térmico del fotorreceptor suba y baje — que es donde la tasa total sola (Lin
et al.) falla con múltiples óptimos locales.

`R(t)` se suaviza con un núcleo gaussiano y se mira el signo de su derivada:

* `dR/dt > 0` → `↻ Mantén el sentido de giro actual` (verde)
* `dR/dt < 0` → `↺ Sentido incorrecto: invierte el giro` (rojo)
* `|dR/dt| ≈ 0` → `… Gira el anillo lentamente` (amarillo)

El **área acumulada del tramo de derivada positiva más grande** es lo que marca
la vecindad del foco. La barra «zona gruesa» de la pestaña muestra ese avance;
al llegar al 100 % se pasa a la etapa fina y **recién ahí** se enciende la
FFT. En etapa gruesa el espectro ni se calcula: contar es barato.

#### Etapa ② · Ajuste fino acoplado (ER + HFE)

**Tasa de eventos (ER).** Con los bordes enfocados la dispersión espacial
colapsa: el óptimo es un **mínimo**.

**Energía de alta frecuencia (HFE).** Se acumula el cuadro de eventos, se
normaliza logarítmicamente, se aplica la DFT 2D y se integra la energía
cuadrática bajo una **máscara anular** con `r_in = 0,24` y `r_out = 0,71` (los
valores del paper), en decibelios:

```
E_HFE = (1/WH) · Σ |F(u,v)|² · M(u,v)
HFE_dB = 10 · log10(E_HFE)
```

Por debajo de `r_in` vive la iluminación global continua; por encima de
`r_out`, el ruido térmico píxel a píxel. Entre los dos, los bordes.

> **Un añadido que el paper no menciona y que hace falta.** Antes de la
> transformada se aplica una **ventana de Hann 2D**. La DFT supone que la
> imagen se repite, así que el salto entre el borde derecho y el izquierdo del
> cuadro actúa como un borde infinitamente nítido e inyecta energía de banda
> ancha ajena al enfoque. Midiendo sobre una grilla sintética de manchas
> gaussianas, sin ventana la curva cae correctamente de 47,8 dB a 23,8 dB entre
> σ = 1 px y σ = 3 px pero **vuelve a subir** a 48,0 dB en σ = 8 px, porque con
> las manchas muy difusas la única estructura nítida que queda es el propio
> borde del cuadro. Un operador guiado por esa curva giraría hacia el
> desenfoque creyendo que mejora. Con la ventana: 39,8 → 16,2 → −15,1 dB,
> monótona.

**Criterio de acoplamiento.** Para declarar foco, el mínimo de ER y el máximo
de HFE tienen que caer dentro de una tolerancia temporal. Un mínimo de ER sin
pico de HFE no es un enfoque: es la escena apagándose. El paper fija 10 ms
porque ahí el tiempo está mapeado a pasos de un motor con barrido uniforme; con
un lente girado a mano la métrica se muestrea cada decenas de milisegundos y
exigir 10 ms sería exigir que las dos caigan en la misma muestra, así que en
modo manual la tolerancia se relaja a unas pocas muestras del propio buffer y
la pestaña dice cuál está usando.

#### El observador cinemático (memoria corta, 200–400 ms)

Sobre un buffer circular de las últimas muestras se calcula la pendiente por
regresión lineal — no por diferencia entre extremos, porque el giro manual no
es uniforme y una muestra ruidosa no puede decidir la dirección.

**Detección de rebase**, con tres defensas contra el falso positivo, que es el
error caro (si el programa dice «enfocado» con el lente a medio camino, el
operador le cree y la toma sale borrosa):

1. hacen falta al menos 8 muestras de etapa fina antes de permitir cualquier
   veredicto de foco — si no, las dos primeras muestras son por definición el
   mejor valor visto y diría «detente» enseguida;
2. el pico tiene que **sobresalir** al menos 3 dB sobre el peor valor de la
   etapa fina para contar como pico y no como ruido;
3. la caída desde el pico tiene que sostenerse **tres muestras seguidas**. La
   curva de HFE tiene mínimos locales reales —un blanco periódico los produce,
   se comprobó midiendo— y una sola caída no puede decidir.

Cuando se confirma: `✔ FOCO ALCANZADO · te pasaste un poco: retrocede una
fracción mínima`. Si la nitidez vuelve a subir, el estado retrocede a etapa
fina en vez de quedarse cantando victoria.

#### Dónde corre el cálculo

**En su propio hilo.** En la primera versión la DFT se calculaba dentro del
bucle que consume el iterador de Metavision, y ese iterador no espera a nadie:
mientras el hilo está en la FFT, los eventos se acumulan en el buffer del SDK.
Con la cámara tranquila no se nota; con 20 MEv/s y el disco grabando, sí — y
justo cuando se está enfocando antes de una toma, que es cuando hace falta que
la cámara vaya fina.

Ahora el lazo de adquisición solo copia el bloque a un buzón de un hueco **si
el calculador está libre**; si está ocupado no copia nada y esa muestra se
pierde, que es lo correcto: el enfoque necesita muestras recientes, no todas.
Con el asistente apagado el lazo ni siquiera copia: el costo es cero.

#### Manual o motorizado

El selector de la pestaña cambia entre los dos:

* **Manual** — el programa dice hacia dónde girar y avisa del rebase.
* **Motorizado** — el mismo criterio conduce una búsqueda automática: barrido
  grueso anotando EPR, vuelta a la vecindad del foco, barrido fino anotando ER
  y HFE, y posicionamiento final. Todo aproximándose **siempre desde el mismo
  lado** para compensar el juego mecánico del anillo, que es lo que se olvida
  y arruina la repetibilidad.

El protocolo del controlador es deliberadamente simple, en texto terminado en
salto de línea, para que quepa en un ESP32 y se pueda depurar con un monitor
serie: `MOVE <pasos>`, `POS?`, `HOME`, `STOP`, respuestas `OK` / `ERR <texto>`.

**Los motores todavía no existen en el banco.** La lógica está escrita y
probada; los controles aparecen deshabilitados con el motivo, no simulando que
funcionan.

**Con cámara simulada** aparece un deslizador de desenfoque artificial, que
ensancha las manchas y sube la tasa igual que el desenfoque real: sirve para
ensayar el asistente completo sin lente.

---

### ⑨ Conversor IMU

**Función.** Decodificar los binarios del IMU, analizarlos y exportarlos.

**Carpeta completa, no archivo por archivo.** Una toma real se graba en bloques
de N minutos; acá se elige la carpeta y se decodifican todos en orden,
encadenando el eje temporal como si fuera un solo archivo continuo.

**Se fue el gráfico de deriva temporal, entró el de vibración.** La deriva del
oscilador del ESP32 frente al PPS era un dato de puesta en marcha: servía para
verificar que el firmware estampaba bien los tiempos. Ya está verificado y en
campaña no aporta. Lo que sí importa dentro de una cúpula es la **vibración
mecánica**. Ahora se muestra:

* la **densidad espectral de potencia** de |a| con la media quitada (o sea sin
  gravedad ni inclinación estática), en log-log, donde un modo mecánico aparece
  como un pico limpio — se calcula por promediado de Welch con ventana de
  Hann, sin depender de SciPy;
* los **picos dominantes** con su frecuencia y su amplitud RMS;
* el **RMS deslizante**, para ver si la vibración aparece y desaparece durante
  la toma (una bomba que arranca, la cúpula rotando, alguien caminando por la
  plataforma).

**Formatos de salida.** `.csv`, `.dat` (texto tabulado), `.mat` (requiere
SciPy), `.npz` y `.parquet` (requiere pandas y pyarrow). Cada fila lleva el
tiempo relativo, el tiempo Unix absoluto y la hora UTC legible cuando el
archivo tiene sello ATM.

---

### ⑩ Topografía y Enlace

**Función.** Perfil del terreno entre dos estaciones a partir de un modelo de
elevación digital (GeoTIFF). Se replica tal cual del programa anterior, porque
ya funcionaba bien.

**Qué produce.** El **perfil de elevación** a lo largo de la línea que une las
dos estaciones, con la línea de vista superpuesta (lo que dice si hay
obstrucción), y un **mapa de sombras** (*hillshade*) del relieve con la traza
dibujada encima.

**Detalles que importan.** La distancia se calcula sobre el elipsoide WGS84
(geodésica real, no aproximación plana); las coordenadas se reproyectan al
sistema del propio GeoTIFF; los huecos de datos (`nodata`) se interpolan
linealmente para que no abran un agujero en el perfil. Además informa el
**margen mínimo de la línea de vista**: si es negativo, el enlace está
obstruido y dice por cuántos metros.

El botón `📡 Tomar la del GPS conectado` copia la última coordenada recibida
por el módulo IMU/GPS de la pestaña 7.

---

### ⑪ NN1

**Función.** Reservada para el primer modelo de la línea de redes neuronales:
el que tome lo que NECTAR graba —frames de la Emergent, flujo de eventos,
series de las fibras— y produzca la estimación que hoy se hace a mano.

Está **vacía a propósito**, con el rótulo `NN1` en el centro. Se dejó creada
desde ahora, y no cuando haya modelo, por una razón práctica: la posición de
las pestañas y los atajos se aprenden con el uso, y mover la numeración a mitad
de campaña obliga a reaprender la ventana justo cuando menos tiempo hay.

Cuando llegue el modelo, aquí entran la selección de la sesión o archivo de
entrada, la elección de arquitectura (las candidatas anotadas en el proyecto
son CNN para lo espacial, Transformer para el relacionamiento tensorial y SNN
si resulta eficiente sobre eventos), los parámetros de inferencia, el estado de
la GPU y la salida del modelo.

---

## 10. Arquitectura del código

### Árbol

```
NECTAR/
├── main.py                       punto de entrada, CLI, comprobación de dependencias
├── requirements.txt
├── README.md
└── nectar/
    ├── config.py                 TODAS las constantes: rutas, biases, puertos, tasas
    │
    ├── core/                     MOTOR DE DATOS (no sabe nada de Qt widgets)
    │   ├── time_sync.py          reloj monotónico global + ancla UTC
    │   ├── session_manager.py    carpetas de sesión, árbol previo y manifiesto
    │   ├── storage.py            inspección del volumen, fallocate, fsync, banco
    │   ├── logbook.py            bitácora en disco (del programa y de la sesión)
    │   ├── aliases.py            etiquetas estables y alias que persisten
    │   ├── device_registry.py    catálogo de familias de instrumento
    │   ├── device_manager.py     plantel vivo de dispositivos conectados
    │   ├── global_recorder.py    protocolo de disparo síncrono de 5 fases
    │   ├── frame_hub.py          buzón de un hueco para la vista previa
    │   ├── disk_stream.py        anillo permanente + hilo escritor + checkpoints
    │   ├── pixels.py             desempaquetado de formatos GVSP (de pastam_cam)
    │   └── algorithms/
    │       ├── jit_render.py     núcleos Numba: render e integración de eventos
    │       ├── focus_metrics.py  EPR, HFE 2D-DFT anular y el motor de 2 etapas
    │       └── imu_decode.py     decodificador IMU, vibración y exportación
    │
    ├── devices/                  CAPA DE HARDWARE (un paquete por familia)
    │   ├── base.py               DeviceWorker y DeviceState
    │   ├── event_cam/            biases, simulación, ROI por HAL, enfoque, worker
    │   │   ├── roi.py            recorte real en el sensor (I_ROI)
    │   │   ├── focus_worker.py   la DFT del enfoque, en su propio hilo
    │   │   └── focus_motor.py    controlador de pasos y búsqueda automática
    │   ├── emergent/             _ffi, api, params, demo (de pastam_cam) + worker
    │   │   └── roi.py            recorte GenICam con ajuste por incrementos
    │   ├── fibers/               protocol (SCPI), parser, worker
    │   ├── anemometer/           worker (estructura + simulador)
    │   ├── imu/                  worker
    │   └── reconstruction/       worker (integrador de Euler, flujo derivado)
    │
    └── ui/                       CAPA DE PRESENTACIÓN
        ├── theme.py              toda la paleta y la hoja de estilo
        ├── main_window.py        arma las once pestañas y el estado compartido
        ├── widgets/              LED, tarjetas, visores, gráficos, piezas comunes
        └── tabs/                 base_family_tab.py + las once pestañas
```

### Las tres capas

**`core/` no importa nada de `ui/`.** Es el motor: relojes, carpetas, buffers,
escritura a disco, algoritmos. Se puede usar sin interfaz (los scripts de
prueba lo hacen).

**`devices/` depende de `core/` y de `PyQt6.QtCore`** (solo por las señales y
los hilos), nunca de widgets.

**`ui/` depende de las dos**, y las pestañas no se hablan entre ellas: hablan
con `SessionManager`, `GlobalRecorder` y `DeviceManager`. Por eso conectar una
cámara en la pestaña 2 hace aparecer su LED en la pestaña 1 sin que ninguna
sepa de la existencia de la otra.

### El contrato `DeviceWorker`

Todo instrumento implementa la misma interfaz (`devices/base.py`):

```python
class DeviceWorker(QObject):
    # señales hacia la interfaz
    state_changed      = pyqtSignal(object)     # DeviceState
    stats_updated      = pyqtSignal(dict)       # telemetría instantánea
    log_message        = pyqtSignal(str)
    error_occurred     = pyqtSignal(str, bool)  # (mensaje, es_fatal)
    recording_finished = pyqtSignal(dict)       # resumen
    preview_ready      = pyqtSignal(object)     # imagen lista para pintar
    info_ready         = pyqtSignal(dict)       # datos estáticos del equipo

    def connect_device(self) -> bool: ...
    def disconnect_device(self) -> None: ...
    def start_stream(self) / stop_stream(self) -> None: ...
    def arm_recording(self, directory, prefix, duration_s,
                      chunk_duration_s=None) -> bool: ...
    def start_recording(self, t0_ns) -> None: ...
    def stop_recording(self) -> None: ...
    def estimated_mb_s(self) -> float: ...
```

### Modelo de hilos: QObject + moveToThread

La especificación original decía en un lado que `DeviceWorker` hereda de
`QObject` y en otro que "cada sensor vive en su propio `QThread` heredando de
`DeviceWorker`". Son cosas distintas y había que elegir. NECTAR elige la que Qt
recomienda oficialmente:

* `DeviceWorker` **es un QObject** que se mueve a su propio `QThread` con
  `moveToThread()`. No se subclasifica `QThread` (el antipatrón que usaban
  `CameraWorker` e `IMUWorker` en `main_v14.1.py`).
* Todo lo que va **hacia** la interfaz viaja por señales de Qt. Nunca se toca un
  widget desde un hilo de dispositivo.
* Todo lo que va **hacia** el dispositivo usa primitivas de `threading`
  (`Event`, `Lock`), **no** la cola de eventos de Qt. Esto es deliberado y es
  la parte que suele hacerse mal: mientras el lazo de captura está bloqueado en
  `get_frame()` o en `ser.read()`, el hilo no procesa eventos de Qt, así que un
  `invokeMethod` quedaría encolado hasta el próximo frame. Con banderas
  atómicas la orden se ve en la siguiente iteración del lazo, siempre.

> **Detalle de PyQt que importa.** Las señales que cruzan de hilo se conectan
> siempre a **métodos ligados de un QObject** de la interfaz, nunca a lambdas:
> Qt determina el hilo de ejecución por el receptor, y con un lambda el slot
> correría en el hilo del worker. Pintar desde ahí revienta. Cuando hace falta
> saber quién emitió, se usa `self.sender()`.

### Estados de dispositivo

```
DISCONNECTED → CONNECTING → READY/STREAMING → ARMED → RECORDING
                                 ↓
                    ERROR_RECOVERABLE (reintenta solo, LED ámbar)
                    ERROR_FATAL       (requiere intervención, LED rojo)
```

Distinguir error recuperable de fatal es lo que permite que un cable de red que
parpadea no saque a la cámara de la sesión, mientras que un SDK ausente sí la
saque.

### Modelo de grabación

`arm_recording() → start_recording() → stop_recording()`, y por omisión **un
archivo por toma**: `chunk_duration_s=None`. El modo por bloques sigue
existiendo —se activa con una casilla en la pestaña 1— y entonces cada worker
rota su archivo por su cuenta sin que nadie llame a `stop_recording()` en
medio; se declara con `supports_chunking`.

En la Emergent, rotar **no reasigna nada**: el anillo es del worker, no del
archivo, y el cambio lo hace el hilo escritor entre dos lotes. En la cámara de
eventos, en cambio, rotar cuesta los milisegundos que el HAL tarda en cerrar un
`.raw` y abrir el siguiente, y esos eventos se pierden. Por eso el valor por
omisión es no rotar.

---

## 11. Rendimiento: por qué el programa aguanta el caudal

### Cero asignaciones en el camino crítico

El lazo de captura de la cámara Emergent corre hasta 662 veces por segundo y no
asigna memoria ni toca el sistema de archivos. Por frame hace: recibir →
comparar `frame_id` → un `memcpy` al anillo → (a veces) un `memcpy` al buzón de
vista previa → reencolar el buffer. Las órdenes de grabación se leen como
banderas atómicas y la duración del bloque queda cacheada al armar, para no
tomar un cerrojo por frame.

### El anillo de escritura es permanente

`core/disk_stream.py` reserva `np.empty((slots, frame_bytes))` —256 slots, unos
900 MB con 1604×1100 Mono12— y lo **prefaultea**: toca un byte cada 4096 para
que el sistema operativo confirme cada página *antes* de la ráfaga. Sin eso,
los fallos de página se pagarían justo cuando no se puede perder tiempo.

Y se reserva **una sola vez**, al armar la primera toma. En la primera versión
cada bloque creaba un escritor nuevo, lo que significaba reservar y confirmar
900 MB *durante* la grabación, con el hilo de captura esperando: a 662 fps eso
se traducía en frames perdidos en cada corte. Ahora el anillo vive mientras la
cámara esté conectada y cambiar de archivo es cerrar un descriptor y abrir
otro, dentro del hilo escritor y entre dos lotes.

El hilo escritor junta hasta 32 slots **contiguos** y los manda en una sola
llamada a `write()`, saliendo directo de la memoria del anillo
(`block.reshape(-1).data`, que es una vista, no una copia). Si el SSD se queda
atrás y el anillo se llena, `submit()` devuelve `False` y se cuenta el frame
como *perdido por disco*: nunca bloquea la adquisición ni deja crecer la
latencia sin límite.

### El caudal se mantiene plano en tomas largas

Después de cada lote se le pide al núcleo que empiece a bajar ese tramo al
disco (`sync_file_range`, que vuelve enseguida y no es un `fsync`) y se suelta
del caché de páginas lo que ya bajó (`fadvise DONTNEED`). Sin esto, una toma de
cientos de GB infla el caché hasta que el núcleo lo vacía de golpe, y ese
vaciado cae justo encima de la captura. Con esto el caudal queda plano y, de
paso, lo que un corte de energía se llevaría son segundos en vez de minutos.

El espacio se pide además por adelantado en tramos de 8 GB (`fallocate`), lo
que mantiene el archivo contiguo en el SSD y hace que la falta de espacio
aparezca temprano; al cerrar se recorta al tamaño realmente escrito.

### Si el disco deja de aceptar datos

El escritor no se limita a contar errores en silencio: marca el flujo como
caído, deja de aceptar frames (`submit()` devuelve `False` siempre) y llama al
`on_failure` del dispositivo, que levanta la alarma y lleva la sesión a
detenerse. Un escritor muerto que sigue aceptando frames es la peor forma de
perder una noche de medición: todo parece ir bien hasta que se abre la carpeta.

### La vista previa nunca se atrasa

Tres barreras, heredadas de `pastam_cam` y formalizadas en `core/frame_hub.py`:

1. **El hilo de captura solo copia cuando toca** según la tasa de pantalla. A
   600 fps de captura y 60 de pantalla, 9 de cada 10 frames ni se copian.
2. **El buzón tiene un solo hueco.** Si se deposita dos veces antes de que la
   pantalla lea, el segundo pisa al primero. No hay cola que pueda crecer.
3. **Contrapresión**: no se emite una imagen nueva hasta que la interfaz acusó
   recibo de la anterior. Y si el acuse no llega en un segundo (pestaña oculta,
   widget destruido), una válvula libera el buzón y lo cuenta, para que una
   vista previa congelada no se confunda con una cámara colgada.

Resultado: si la pantalla se atrasa se pierden cuadros *de vista previa*, que es
exactamente lo que se quiere. La grabación va por otro camino.

Además, la conversión de píxeles (desempaquetar Mono12Packed de 1604×1100
cuesta milisegundos) ocurre en un **hilo aparte** que solo ve el último frame
publicado, decimado antes de convertir.

### Bypass del GIL con Numba

Todo cómputo sobre matrices de eventos está en funciones `@njit(nogil=True,
fastmath=True)`: renderizado, integración de Euler y acumulación de densidad.
Liberan el GIL mientras corren, así el hilo de la interfaz sigue respondiendo.
Se compilan al arrancar el programa (`warmup()`), no en el primer frame, para
que no haya un tirón justo cuando la cámara empieza a entregar datos.

Si Numba no está instalado, hay un camino vectorizado en NumPy: más lento, pero
el programa arranca.

### El presupuesto de disco

Cada worker estima su caudal con la configuración **actual** (no con un número
fijo): la Emergent multiplica bytes por frame por los fps medidos; la de eventos
multiplica la tasa medida por los bytes por evento del formato en uso; las
fibras multiplican canales por frecuencia. La pestaña 1 suma todo y lo divide
por el espacio libre. Ese es el número que dice si una campaña de dos horas es
viable.

---

## 12. Cómo agregar un instrumento nuevo

Esta parte importa porque el dossier apunta a más instrumentos de los que hay
hoy. Agregar uno son **tres archivos y una línea**, y no hay que tocar la
ventana principal ni la lógica del disparo síncrono.

**1. El worker** — `nectar/devices/mi_sensor/worker.py`:

```python
from ..base import DeviceState, DeviceWorker


class MiSensorWorker(DeviceWorker):
    family = "mi_sensor"
    family_label = "Mi sensor"
    supports_chunking = True
    nominal_mb_s = 0.5

    def connect_device(self) -> bool:
        self.start_thread()
        return True

    def disconnect_device(self) -> None:
        self.stop_recording()
        self.stop_thread()
        self._set_state(DeviceState.DISCONNECTED)

    def run(self) -> None:
        self._set_state(DeviceState.STREAMING)
        while not self.should_stop:
            datos = leer_del_hardware()

            # atender órdenes de grabación (el patrón es siempre este)
            if self._consume_arm_request():
                self._armed = True
                self._set_state(DeviceState.ARMED)
            if self._consume_start_request():
                abrir_archivo(self.record_target["directory"])
                self._recording = True
                self._set_state(DeviceState.RECORDING)
            if self._consume_stop_request():
                cerrar_archivo()
                self._recording = False
                self._set_state(DeviceState.STREAMING)
                self._publish_recording_summary({...})

            self._emit_stats({"hz": 100, "mb_s": 0.5})


def discover_mi_sensor():
    return [{"id": "mis_1", "label": "MiSensor1", "simulated": False}]
```

**2. Registrarlo** — una línea en `nectar/devices/__init__.py`:

```python
REGISTRY.register(DeviceType(
    family="mi_sensor", label="Mi sensor",
    worker_factory=MiSensorWorker, discover=discover_mi_sensor,
    throughput_mb_s=MiSensorWorker.nominal_mb_s,
    notes="Lo que mide este instrumento."))
```

Con esto **ya aparece** en la matriz de la pestaña 1, entra al disparo síncrono
de 5 fases, suma su caudal al presupuesto, recibe su carpeta precreada en
`<sesión>/<familia>/<dispositivo>/`, hereda el bloqueo durante la grabación y
su resumen entra al manifiesto antes de sellarlo.

Tres cosas que el worker nuevo debe respetar:

* nombrar sus archivos con `self.record_stem(indice)` — así hereda gratis el
  sufijo de bloque solo cuando corresponde, la marca `_SIMULADO` y la marca de
  tiempo común;
* acompañarlos con `self.record_metadata(...)` — alias, identidad, T0, reloj y
  el aviso de simulación;
* llamar a `self.raise_recording_alarm(motivo)` si se cae grabando, para que el
  LED se ponga rojo parpadeante y quede en la bitácora.

**3. Su pestaña** (opcional) — `nectar/ui/tabs/tab_mi_sensor.py`:

```python
from .base_family_tab import DevicePanel, FamilyTab


class MiSensorPanel(DevicePanel):
    def refresh_stats(self, stats):
        ...


class MiSensorTab(FamilyTab):
    family = "mi_sensor"
    family_label = "Mi sensor"

    def create_panel(self, worker):
        return MiSensorPanel(worker)

    def build_side_panel(self):
        return mis_controles_propios()
```

`FamilyTab` ya trae el selector de detectar/agregar, la columna desplazable
aditiva, el bloque de grabación independiente con selector de carpeta, la
consola y la regla de que durante una sesión global manda la pestaña 1. Después
se agrega su línea en `TAB_SPECS`, en `nectar/ui/main_window.py`.

Las dos convenciones en `config.py`: `FAMILY_DIRS["mi_sensor"] = "MiSensor"`
(nombre de la carpeta) y `FAMILY_LABELS["mi_sensor"] = "Mi sensor"` (rótulo en
la matriz).

---

## 13. Atajos de teclado

| Tecla | Acción |
|---|---|
| **F4** | Detectar dispositivos en todas las familias |
| **F5** | Disparo global (va a la pestaña 1 y arranca) |
| **F6** | Detener la grabación global |

---

## 14. Solución de problemas

| Síntoma | Causa probable y qué hacer |
|---|---|
| `ImportError: DLL load failed while importing QtCore` | PyQt6 6.8+ con el Python de Anaconda. Instalar exactamente `PyQt6==6.7.1` y `PyQt6-Qt6==6.7.1`. |
| No aparece **ninguna** cámara de eventos | El registro de la pestaña dice el motivo exacto. Lo más común: falta el Metavision SDK o no lo ve este intérprete (`python -c "import metavision_core"`). Fuera de `--demo` el programa **no** ofrece una cámara simulada en su lugar: eso es deliberado. |
| No aparece **ninguna** Emergent | Falta el eSDK 2.49, falta `cffi`, o la variable `EMERGENT_DIR` no apunta al SDK. `python main.py --check` lo dice. |
| `Capa del eSDK no disponible` | Falta `cffi`: `pip install cffi`. |
| La Emergent conecta pero no entrega frames | Sin *jumbo frames* el enlace de 25 GbE no rinde: subir el MTU de la NIC. El programa ya sube `GevSCPSPacketSize` al máximo que reporta la cámara. |
| Suben los **frames perdidos por disco** | El SSD no sigue el caudal. Recortar el ROI, usar `Mono12Packed` (25 % menos de datos), o escribir a un disco más rápido. |
| Suben los **frames perdidos en el enlace** | Problema de red, no de disco: cable, MTU, o CPU saturada en el host. |
| `No hay memoria para el anillo` | Reducir `EMERGENT_RING_SLOTS` en `config.py` o recortar el ROI. |
| El ancla UTC nunca se pone verde | El GPS no tiene fijación: revisar la antena y esperar. Sin ancla se puede grabar, pero los tiempos quedan relativos a T0. |
| Muchos `errores de sincronismo` en el IMU | Velocidad del puerto mal configurada, o cable largo. Probar el modo RAW para descartar el parseo. |
| Las fibras conectan pero no llegan datos | El canal de control acepta, el de datos no: revisar el puerto de datos y que `measure:start` haya sido aceptado (aparece en el registro). |
| "No armaron a tiempo: …" al disparar | Un instrumento no confirmó `ARMED` en 20 s. Revisar su pestaña: probablemente esté en error recuperable. No se grabó nada, a propósito. |
| La vista previa se ve congelada pero la grabación sigue | Es el comportamiento correcto: la pantalla pierde cuadros antes que la grabación. Bajar los FPS de pantalla o la decimación. |
| La pestaña de topografía dice que faltan librerías | `pip install rasterio pyproj shapely`. |
| No se puede exportar a `.mat` o `.parquet` | `pip install scipy` / `pip install pandas pyarrow`. |
| «El destino está formateado como VFAT» | FAT32 corta todo archivo a 4 GB sin aviso útil. Reformatear el disco a ext4 (o NTFS si tiene que leerse en Windows) antes de la campaña. |
| La sesión se detuvo sola con «DISCO CASI LLENO» | Es el vigilante haciendo su trabajo: quedaban menos de 2 GB libres y cortó con margen para cerrar los archivos. Todo lo grabado hasta ahí está íntegro y sellado. |
| Un LED quedó **rojo parpadeando** después de la toma | Ese instrumento falló mientras se grababa. El motivo y la hora exacta están en `nectar_sesion.log`, dentro de la carpeta de la sesión. Se baja con «Reconocer alarmas», pero conviene mirar antes qué pasó. |
| `⚠ ROI NO aplicado en el sensor` en la pestaña 2 | Esa versión del SDK no expone `I_ROI` de ninguna de las formas conocidas, o la cámara no admite recorte. Se está grabando el cuadro completo: el aviso es literal. |
| La toma murió sin cerrar (corte de luz, cuelgue) | El `.raw` es legible hasta donde alcanzó a bajar. El `_checkpoint.json` de al lado dice exactamente cuántos bytes son válidos y hasta qué instante. |
| Las pestañas están grises y no responden | Hay una grabación en curso: la interfaz se congela a propósito. El botón DETENER está en la pestaña 1. |

---

## 15. Qué falta y qué sigue

Lo que está declarado pero pendiente, sin sorpresas escondidas:

* **Prueba con la cámara Emergent real transmitiendo.** La capa del eSDK está
  validada contra `EmergentCameraC.dll` (todos los símbolos resuelven y la
  enumeración encuentra la cámara del laboratorio) y el camino completo está
  probado contra la cámara de demostración, pero falta la prueba de extremo a
  extremo con la cámara real en streaming, que requiere tomar control exclusivo
  del equipo.
* **eSDK de Emergent en Linux.** El PC de campaña usa Ubuntu 24.04; falta
  instalar el eSDK para Ubuntu 24.04 / kernel 6.17 con la tarjeta ConnectX de
  25G y repetir la validación (símbolos, enumeración y streaming) con
  `libEmergentCameraC.so`.
* **Cámara de eventos en el PC de campaña.** OpenEB 5.3 y el plugin SilkyEvCam
  v5.2.0 están instalados y `metavision_core` importa; falta la prueba con la
  cámara conectada.
* **Anemómetro ToF**: existe la estructura, los tres modos de ingesta y la
  física; falta el firmware de la FPGA y, con él, escribir `parse_packet()`.
* **Motores de la lente** (pestaña 8): el método completo de dos etapas
  funciona y guía el enfoque manual. Para el modo motorizado están escritos el
  protocolo de mando, la compensación de juego mecánico y la rutina de búsqueda
  automática; falta el hardware. Mientras no exista, los controles aparecen
  deshabilitados con el motivo, no simulando que funcionan.
* **Seguimiento de centroides AoA en tiempo real**: hoy el wander se calcula
  fuera de línea sobre los `.raw`. El módulo de métricas ya tiene la
  acumulación de densidad que haría falta como entrada.
* **Índice de centelleo de Charnotskii** sobre los bordes de damero: mismo
  caso, pendiente de implementar en vivo.
* **Sincronismo por hardware entre familias distintas**: hoy el disparo entre
  familias es por software (T0 del host). Un PPS común distribuido por TTL a
  todos los instrumentos permitiría bajar la incertidumbre de milisegundos a
  microsegundos.

---

## 16. Garantías de integridad de los datos

Esta sección es el resumen de lo que el programa garantiza y de lo que no.
Vale la pena leerla antes de una campaña, porque describe exactamente qué pasa
en cada forma conocida de que una toma salga mal.

### Lo que no puede pasar

| Riesgo | Qué lo impide |
|---|---|
| Grabar datos simulados creyendo que son reales | Fuera de `--demo` no se crean dispositivos simulados; `can_write()` los rechaza; y en `--demo` todo queda marcado en la carpeta, en el nombre del archivo y en los metadatos. |
| Perder eventos en el corte de un bloque | Un archivo por toma, por omisión. Sin corte no hay costura. |
| Que el SSD deje de aceptar datos y nadie se entere | El escritor marca el flujo como caído, deja de aceptar frames, levanta la alarma y la sesión se detiene. |
| Llenar el disco a la fuerza | Vigilancia cada 2 s con auto-parada al llegar al margen de 2 GB, con tiempo para cerrar limpiamente. |
| Grabar en un volumen que no aguanta el archivo | Validación del destino antes de armar: FAT32 se rechaza; el tamaño proyectado del archivo mayor se compara contra el techo del sistema de archivos. |
| Descubrir a los diez minutos que el destino era de solo lectura | Todo el árbol de la sesión se crea al armar, con todos los equipos parados. |
| Que un parámetro cambie a mitad de toma y los metadatos mientan | La interfaz se congela mientras se graba: biases, ROI, tasas, inclusión y retiro de dispositivos. |
| Que un fallo a las 03:40 no deje rastro | Bitácora en disco dentro de la propia carpeta de la sesión, con hora local y desfase respecto a T0. |
| Que el manifiesto se selle sin las cuentas de cada instrumento | El sellado espera a que todos confirmen el cierre, y añade un inventario de lo que de verdad quedó escrito. |
| Que el ROI parezca aplicado y no lo esté | El estado del ROI se lee del sensor, no de la casilla; si el HAL lo rechaza se avisa en rojo y se levanta alarma al grabar. |
| Que una cámara se llame distinto en cada campaña | La etiqueta se ancla a la identidad física del equipo y se guarda en disco, junto con el alias que le pongas. |

### El riesgo que queda, y cómo se acota

**Corte de energía o cuelgue antes de cerrar el archivo.** El `.raw` es un
formato de flujo, no un contenedor indexado, así que un archivo truncado sigue
siendo legible hasta donde alcanzó a bajar. Lo que se pierde son los segundos
que estaban en el caché del sistema, no la toma entera. Y el
`_checkpoint.json` —sellado con `fsync` cada cinco segundos— dice exactamente
hasta dónde el archivo es válido.

Para cerrar del todo ese riesgo hace falta algo que el software no puede dar:
**alimentación respaldada con UPS para el computador de adquisición**. Con UPS,
el único escenario de pérdida que queda es un cuelgue del programa, y contra
eso los puntos de control ya cubren.

**Copiar la toma después.** Un archivo de cientos de GB no se parte entre
discos portátiles y su verificación tarda lo mismo que copiarlo. Es una
molestia logística real, pero es posterior a la medición y no debe decidir la
arquitectura de captura.

---

## Créditos

**NECTAR** · plataforma de adquisición del proyecto PASTAM
ANID FONDEF IDeA I+D · Instituto de Física, Pontificia Universidad Católica de Valparaíso.

**Autor:** Eduardo Llantén.

Software previo del que NECTAR hereda: `pastam_cam` (control de la cámara
Emergent) y [EvCam-IMU-GPS](https://github.com/Llapra/EvCam-IMU-GPS) v14.1
(cámara de eventos, IMU/GPS, reconstrucción y topografía).

### Agradecimientos

A **Pablo Garrido**, coautor de EvCam-IMU-GPS. La adquisición de la cámara de
eventos, la telemetría IMU/GPS, la reconstrucción en vivo y la pestaña de
topografía de NECTAR nacen de ese trabajo conjunto.

Uso académico y de investigación.
Contacto: eduardo.llanten.p@mail.pucv.cl
