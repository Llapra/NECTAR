#!/usr/bin/env bash
# ===========================================================================
# NECTAR · instalación del entorno virtual venv-NECTAR
# ===========================================================================
#
# Qué hace, paso a paso (nada más que esto):
#
#   1. Busca dónde está instalado el SDK de Metavision (la parte de Python de
#      OpenEB) y con qué versión de Python fue compilado.
#   2. Crea el entorno virtual venv-NECTAR DENTRO de esta carpeta, con ESA
#      versión de Python. Si ya existe, lo reutiliza.
#   3. Instala ahí las dependencias de requirements.txt.
#   4. Le enseña al entorno dónde está el SDK, con un archivo de texto de una
#      línea (metavision.pth). Eso es lo que hace que NECTAR, main_v14.1.py y
#      los programas de prueba vean el SDK sin instalarlo de nuevo.
#   5. Comprueba que el SDK importa y que el HAL ve la cámara, SIN abrirla.
#   6. Corre "python main.py --check".
#
# Qué NO hace:
#   - No usa sudo ni toca nada del sistema.
#   - No borra nada. Si algo no cuadra, se detiene y dice qué hacer.
#   - No reinstala el SDK de Metavision: el SDK ya está instalado en el
#     sistema y se reutiliza.
#
# Uso (desde cualquier carpeta):
#
#     bash ~/Codigos/Programas_para_github/NECTAR/instalar_venv.sh
#
# Sirve igual en el PC de desarrollo y en Tatarskii.
# ===========================================================================

set -u

# --- ir a la carpeta de NECTAR (la de este script) -------------------------
CARPETA="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$CARPETA" || { echo "No pude entrar a $CARPETA"; exit 1; }
VENV="$CARPETA/venv-NECTAR"

linea() { printf '%s\n' "------------------------------------------------------------"; }
paso()  { echo; linea; echo "$1"; linea; }
falla() { echo; echo "✖ $1"; echo; exit 1; }

echo
echo "NECTAR · instalación de venv-NECTAR"
echo "Carpeta: $CARPETA"

# Si hay un entorno activado, se avisa: no molesta (el script usa rutas
# completas), pero conviene saberlo para no confundirse después.
if [ -n "${VIRTUAL_ENV:-}" ]; then
    echo
    echo "Aviso: tienes activado otro entorno ($VIRTUAL_ENV)."
    echo "No importa para este script; al final te digo cómo activar el nuevo."
fi

# ===========================================================================
paso "1/6 · Buscando el SDK de Metavision en el sistema"
# ===========================================================================
SDK_DIR=""
for prefijo in /usr/local /usr /opt/prophesee /opt/metavision; do
    for sub in "lib/python3.12/dist-packages" "lib/python3.12/site-packages" \
               "lib/python3.13/dist-packages" "lib/python3.11/dist-packages" \
               "lib/python3/dist-packages"; do
        if [ -d "$prefijo/$sub/metavision_core" ]; then
            SDK_DIR="$prefijo/$sub"
            break 2
        fi
    done
done
if [ -n "${NECTAR_METAVISION_PATH:-}" ] && [ -d "$NECTAR_METAVISION_PATH/metavision_core" ]; then
    SDK_DIR="$NECTAR_METAVISION_PATH"
fi

if [ -z "$SDK_DIR" ]; then
    falla "No encontré el SDK de Metavision (metavision_core) en las carpetas
  habituales: /usr/local/lib/python3.X/dist-packages, /usr/lib/python3/dist-packages.
  En este PC el SDK no está instalado, o está en otro sitio.
  Si está en otro sitio, dime la carpeta o corre:
      NECTAR_METAVISION_PATH=/la/carpeta bash instalar_venv.sh"
fi
echo "✔ SDK encontrado en: $SDK_DIR"

# Con qué Python fue compilado: está en el nombre del módulo nativo del HAL,
# por ejemplo metavision_hal.cpython-312-x86_64-linux-gnu.so -> Python 3.12.
# El entorno TIENE que ser de esa misma versión, o el SDK no importa.
SO="$(ls "$SDK_DIR"/metavision_hal.cpython-*.so 2>/dev/null | head -n 1)"
if [ -n "$SO" ]; then
    ETIQUETA="$(basename "$SO" | sed -E 's/^metavision_hal\.cpython-([0-9])([0-9]+)-.*$/\1.\2/')"
    PYTHON="python$ETIQUETA"
    echo "✔ El SDK fue compilado para Python $ETIQUETA ($(basename "$SO"))"
else
    PYTHON="python3"
    echo "Aviso: no pude leer la versión de Python del SDK; uso python3."
fi

if ! command -v "$PYTHON" >/dev/null 2>&1; then
    falla "Necesito $PYTHON (la versión con la que se compiló el SDK) y no está
  instalado en este sistema."
fi
echo "✔ Intérprete base: $(command -v "$PYTHON") ($("$PYTHON" --version 2>&1))"

# ===========================================================================
paso "2/6 · Entorno virtual venv-NECTAR"
# ===========================================================================
if [ -x "$VENV/bin/python" ]; then
    VERSION_VENV="$("$VENV/bin/python" -c 'import sys; print(f"{sys.version_info[0]}.{sys.version_info[1]}")')"
    VERSION_BASE="$("$PYTHON" -c 'import sys; print(f"{sys.version_info[0]}.{sys.version_info[1]}")')"
    if [ "$VERSION_VENV" != "$VERSION_BASE" ]; then
        falla "venv-NECTAR ya existe pero es de Python $VERSION_VENV, y el SDK
  necesita Python $VERSION_BASE. No lo borro yo: bórralo tú y vuelve a correr
  este script:
      rm -rf \"$VENV\""
    fi
    echo "✔ venv-NECTAR ya existe (Python $VERSION_VENV): lo reutilizo."
else
    echo "Creando $VENV ..."
    if ! "$PYTHON" -m venv "$VENV"; then
        falla "No se pudo crear el entorno. En Ubuntu / Pop!_OS suele faltar el
  paquete del módulo venv. Instálalo (pide tu contraseña) y vuelve a correr:
      sudo apt install $PYTHON-venv"
    fi
    echo "✔ venv-NECTAR creado."
fi

PY="$VENV/bin/python"

# ===========================================================================
paso "3/6 · Instalando las dependencias (requirements.txt)"
# ===========================================================================
echo "Esto puede tardar unos minutos la primera vez."
"$PY" -m pip install --upgrade pip || falla "No se pudo actualizar pip. ¿Hay internet?"

# Si OpenEB se instaló con apt (como en Tatarskii), trae su propia lista de
# librerías de Python con versiones fijadas. Va PRIMERO, para que pip respete
# esas versiones al instalar después las de NECTAR. Si OpenEB se compiló a mano
# (como en el PC de desarrollo), este archivo no existe y el paso se salta.
REQ_OPENEB="/usr/share/metavision/python_requirements/requirements_openeb.txt"
if [ -f "$REQ_OPENEB" ]; then
    echo "Instalando primero las librerías que pide OpenEB ($REQ_OPENEB) ..."
    "$PY" -m pip install -r "$REQ_OPENEB" \
        || falla "pip no pudo instalar $REQ_OPENEB. Copia el error de arriba y pásamelo."
else
    echo "(No hay lista de requisitos de OpenEB en $REQ_OPENEB: se salta.)"
fi

"$PY" -m pip install -r "$CARPETA/requirements.txt" \
    || falla "pip no pudo instalar requirements.txt. Copia el error de arriba y pásamelo."
echo "✔ Dependencias instaladas."

# ===========================================================================
paso "4/6 · Enlazando el SDK de Metavision al entorno"
# ===========================================================================
# Un archivo .pth es una lista de carpetas que Python agrega a su camino al
# arrancar. Se agregan AL FINAL: lo que esté instalado dentro de venv-NECTAR
# siempre tiene prioridad, y del sistema solo se usa lo que falte (el SDK).
SITE="$("$PY" -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])')"
echo "$SDK_DIR" > "$SITE/metavision.pth"
echo "✔ Escrito $SITE/metavision.pth"
echo "  con el contenido: $SDK_DIR"

# ===========================================================================
paso "5/6 · Comprobando el SDK y la cámara (sin abrirla)"
# ===========================================================================
"$PY" - <<'PYEOF'
import os, sys
print(f"Intérprete: {sys.executable}")
ok = True
for nombre in ("metavision_hal", "metavision_core.event_io"):
    try:
        __import__(nombre)
        print(f"✔ import {nombre}")
    except Exception as exc:
        ok = False
        print(f"✖ import {nombre}: {type(exc).__name__}: {exc}")
if not ok:
    print("\nEl SDK no importa. Copia estas líneas y pásamelas.")
    sys.exit(1)

plugins = os.environ.get("MV_HAL_PLUGIN_PATH", "")
print(f"MV_HAL_PLUGIN_PATH = {plugins or '(sin definir)'}")

from metavision_hal import DeviceDiscovery
funcion = getattr(DeviceDiscovery, "list", None)
if funcion is None:
    print("Esta versión del SDK no tiene DeviceDiscovery.list(); NECTAR "
          "abrirá 'la primera disponible'.")
else:
    entradas = list(funcion())
    seriales = []
    for entrada in entradas:
        serial = str(entrada).split(":")[-1].strip()
        if serial and serial not in seriales:
            seriales.append(serial)
    if seriales:
        print(f"✔ Cámaras de eventos conectadas: {', '.join(seriales)}")
    else:
        print("Aviso: el HAL no ve ninguna cámara de eventos. Si está "
              "enchufada, revisa MV_HAL_PLUGIN_PATH.")
PYEOF
[ $? -eq 0 ] || falla "La comprobación del SDK falló (mira las líneas de arriba)."

# ===========================================================================
paso "6/6 · python main.py --check"
# ===========================================================================
"$PY" "$CARPETA/main.py" --check

echo
linea
echo "LISTO. venv-NECTAR está en:"
echo "    $VENV"
echo
echo "Para usarlo, cada vez que abras una terminal nueva:"
echo "    cd \"$CARPETA\""
echo "    source venv-NECTAR/bin/activate"
echo "    python main.py"
echo
echo "En VS Code: Ctrl+Shift+P → \"Python: Select Interpreter\" → elige"
echo "    $VENV/bin/python"
linea
