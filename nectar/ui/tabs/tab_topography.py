"""Pestaña 10 · Topografía y enlace: perfil del terreno entre dos estaciones.

Se replica tal cual del programa anterior, porque ya funcionaba bien: se carga
un modelo de elevación digital (GeoTIFF), se dan las coordenadas de las dos
estaciones y se obtienen dos cosas:

* el **perfil de elevación** a lo largo de la línea que las une, con la línea
  de vista superpuesta, que es lo que dice si hay obstrucción entre los dos
  puntos;
* un **mapa de sombras (hillshade)** del relieve de la zona, con la traza
  dibujada encima.

La distancia se calcula sobre el elipsoide WGS84 (geodésica real, no una
aproximación plana), las coordenadas se reproyectan al sistema del propio
GeoTIFF, y los huecos de datos del DEM se interpolan linealmente antes de
graficar para que un ``nodata`` no abra un agujero en el perfil.

Necesita ``rasterio``, ``pyproj`` y ``shapely``. Si no están instalados, la
pestaña lo dice y explica cómo instalarlos, en vez de fallar al abrir.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import numpy as np
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (QApplication, QFileDialog, QHBoxLayout, QLabel,
                             QLineEdit, QPushButton, QVBoxLayout, QWidget)

from .. import theme
from ..widgets.common import LogConsole, Section, ValueRow

try:
    import matplotlib
    matplotlib.use("QtAgg")
    import matplotlib.patheffects as path_effects
    import matplotlib.pyplot as plt
    from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg as Canvas
    from matplotlib.backends.backend_qtagg import NavigationToolbar2QT as Toolbar
    from matplotlib.colors import LightSource
    HAS_MATPLOTLIB = True
except Exception:
    HAS_MATPLOTLIB = False

try:
    import rasterio
    from rasterio.windows import from_bounds
    from pyproj import Geod, Transformer
    from shapely.geometry import LineString
    HAS_GEO = True
except Exception:
    HAS_GEO = False


class TopographyTab(QWidget):
    """Perfil topográfico y mapa de sombras entre dos estaciones."""

    def __init__(self, manager, recorder, sessions,
                 parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.manager = manager
        self.sessions = sessions
        self.dem_path: Optional[str] = None
        self._figure = None
        self._canvas = None
        self._toolbar = None

        self._build()

    # ------------------------------------------------------------------
    def _build(self) -> None:
        root = QHBoxLayout(self)
        root.setContentsMargins(8, 6, 8, 8)
        root.setSpacing(8)

        left = QWidget()
        left.setFixedWidth(350)
        layout = QVBoxLayout(left)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        # --- 1. DEM -----------------------------------------------------
        dem = Section("1 · Modelo de elevación digital (DEM)")
        self.btn_load = QPushButton("📂 CARGAR ARCHIVO .TIF")
        self.btn_load.setStyleSheet(theme.button_style("warn"))
        self.btn_load.clicked.connect(self._load_dem)
        dem.add(self.btn_load)
        self.lbl_dem = QLabel("Ningún archivo cargado")
        self.lbl_dem.setWordWrap(True)
        self.lbl_dem.setStyleSheet(
            f"color: {theme.TEXT_FAINT}; font-style: italic; font-size: 10px;")
        dem.add(self.lbl_dem)
        self.row_crs = dem.add_row("Sistema de coordenadas", "—")
        self.row_size = dem.add_row("Tamaño de la grilla", "—")
        self.row_res = dem.add_row("Resolución", "—")
        layout.addWidget(dem)

        # --- 2. coordenadas ---------------------------------------------
        coords = Section("2 · Coordenadas del enlace")
        coords.add(QLabel("📍 Estación 1 (origen):"))
        self.txt_name_a = QLineEdit("Estación Cámara")
        self.txt_name_a.setStyleSheet(
            f"background-color: {theme.BG_INPUT}; color: {theme.OK_BRIGHT};")
        coords.add(self.txt_name_a)
        row_a = QHBoxLayout()
        self.txt_lat_a = QLineEdit("-33.036000")
        self.txt_lon_a = QLineEdit("-71.596000")
        row_a.addWidget(QLabel("Lat:"))
        row_a.addWidget(self.txt_lat_a)
        row_a.addWidget(QLabel("Lon:"))
        row_a.addWidget(self.txt_lon_a)
        coords.add_layout(row_a)

        coords.add(QLabel("🎯 Estación 2 (destino):"))
        self.txt_name_b = QLineEdit("Estación Target")
        self.txt_name_b.setStyleSheet(
            f"background-color: {theme.BG_INPUT}; color: {theme.ACCENT};")
        coords.add(self.txt_name_b)
        row_b = QHBoxLayout()
        self.txt_lat_b = QLineEdit("-33.100000")
        self.txt_lon_b = QLineEdit("-71.650000")
        row_b.addWidget(QLabel("Lat:"))
        row_b.addWidget(self.txt_lat_b)
        row_b.addWidget(QLabel("Lon:"))
        row_b.addWidget(self.txt_lon_b)
        coords.add_layout(row_b)

        self.btn_from_gps = QPushButton("📡 Tomar la del GPS conectado")
        self.btn_from_gps.setToolTip(
            "Copia en la estación 1 la última coordenada recibida por el "
            "módulo IMU/GPS de la pestaña 7.")
        self.btn_from_gps.clicked.connect(self._from_gps)
        coords.add(self.btn_from_gps)

        self.btn_process = QPushButton("GENERAR PERFIL Y MAPA DE SOMBRAS")
        self.btn_process.setStyleSheet(theme.button_style("ok"))
        self.btn_process.clicked.connect(self._process)
        coords.add(self.btn_process)
        layout.addWidget(coords)

        # --- 3. resultados ----------------------------------------------
        results = Section("3 · Resultados")
        self.row_distance = results.add_row("Distancia geodésica", "—")
        self.row_elev_a = results.add_row("Altitud estación 1", "—")
        self.row_elev_b = results.add_row("Altitud estación 2", "—")
        self.row_clearance = results.add_row("Línea de vista", "—")
        layout.addWidget(results)

        self.console = LogConsole(max_lines=300)
        layout.addWidget(QLabel("Registro topográfico:"))
        layout.addWidget(self.console, 1)
        root.addWidget(left, 0)

        # --- panel de dibujo --------------------------------------------
        self.plot_container = QWidget()
        self.plot_layout = QVBoxLayout(self.plot_container)
        self.plot_layout.setContentsMargins(0, 0, 0, 0)
        message = "Carga un GeoTIFF y pulsa GENERAR."
        if not HAS_GEO:
            message = ("Faltan las librerías geoespaciales.\n\n"
                       "pip install rasterio pyproj shapely")
        elif not HAS_MATPLOTLIB:
            message = "matplotlib no está instalado.\n\npip install matplotlib"
        self.placeholder = QLabel(message)
        self.placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.placeholder.setWordWrap(True)
        self.placeholder.setStyleSheet(
            f"color: {theme.TEXT_FAINT}; font-size: 13px;")
        self.plot_layout.addWidget(self.placeholder)
        root.addWidget(self.plot_container, 1)

        if not HAS_GEO:
            self.btn_process.setEnabled(False)
            self.console.log("Sin rasterio/pyproj/shapely: la pestaña queda "
                             "en modo informativo.")

    # ------------------------------------------------------------------
    def _load_dem(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Abrir modelo de elevación digital", "",
            "GeoTIFF (*.tif *.tiff);;Todos los archivos (*)")
        if not path:
            return
        self.dem_path = path
        self.lbl_dem.setText(Path(path).name)
        self.lbl_dem.setStyleSheet(
            f"color: {theme.OK_BRIGHT}; font-weight: bold; font-size: 10px;")
        self.console.log(f"DEM cargado: {Path(path).name}")

        if HAS_GEO:
            try:
                with rasterio.open(path) as source:
                    self.row_crs.set_value(str(source.crs))
                    self.row_size.set_value(f"{source.width} x {source.height}")
                    self.row_res.set_value(
                        f"{abs(source.transform.a):.2f} x "
                        f"{abs(source.transform.e):.2f} m/px")
                    self.console.log(
                        f"CRS {source.crs} · {source.width}x{source.height} · "
                        f"nodata={source.nodata}")
            except Exception as exc:
                self.console.log(f"✖ No se pudo leer el DEM: {exc}")

    def _from_gps(self) -> None:
        for worker in self.manager.by_family("imu"):
            if worker.last_lat is not None and worker.last_lon is not None:
                self.txt_lat_a.setText(f"{worker.last_lat:.6f}")
                self.txt_lon_a.setText(f"{worker.last_lon:.6f}")
                self.console.log(f"Coordenada tomada de {worker.label}.")
                return
        self.console.log("Ningún módulo IMU/GPS tiene coordenada todavía.")

    # ------------------------------------------------------------------
    def _process(self) -> None:
        if not HAS_GEO:
            self.console.log("✖ Faltan las librerías geoespaciales.")
            return
        if not self.dem_path:
            self.console.log("⚠ Primero hay que cargar un archivo .tif.")
            return

        try:
            lat1 = float(self.txt_lat_a.text())
            lon1 = float(self.txt_lon_a.text())
            lat2 = float(self.txt_lat_b.text())
            lon2 = float(self.txt_lon_b.text())
        except ValueError:
            self.console.log("✖ Coordenadas inválidas.")
            return

        name1 = self.txt_name_a.text() or "Estación 1"
        name2 = self.txt_name_b.text() or "Estación 2"

        self.console.log("Calculando…")
        QApplication.processEvents()

        try:
            # --- distancia geodésica real sobre el elipsoide ------------
            geod = Geod(ellps="WGS84")
            _, _, distance_m = geod.inv(lon1, lat1, lon2, lat2)
            distance_km = distance_m / 1000.0

            with rasterio.open(self.dem_path) as source:
                transformer = Transformer.from_crs("EPSG:4326", source.crs,
                                                   always_xy=True)
                x1, y1 = transformer.transform(lon1, lat1)
                x2, y2 = transformer.transform(lon2, lat2)

                # --- perfil 1D a lo largo de la línea ------------------
                line = LineString([(x1, y1), (x2, y2)])
                distances = np.linspace(0, line.length, 500)
                coordinates = [(point.x, point.y) for point in
                               (line.interpolate(value) for value in distances)]
                samples = list(source.sample(coordinates))
                elevations = np.array(
                    [value[0] if value[0] != source.nodata else np.nan
                     for value in samples], dtype=float)

                # Huecos del DEM: se interpolan, así un nodata no abre un
                # agujero en el perfil.
                gaps = np.isnan(elevations)
                if np.all(gaps):
                    raise ValueError("El perfil cae completamente fuera del DEM.")
                if np.any(gaps):
                    index = lambda mask: mask.nonzero()[0]  # noqa: E731
                    elevations[gaps] = np.interp(
                        index(gaps), index(~gaps), elevations[~gaps])
                    self.console.log(f"Se interpolaron {int(gaps.sum())} "
                                     f"muestras sin dato.")

                profile_x = np.linspace(0, distance_km, elevations.size)

                # --- recorte 2D para el mapa de sombras ---------------
                res_x = abs(source.transform.a)
                res_y = abs(source.transform.e)
                margin_x = max(abs(x1 - x2) * 0.4, 150 * res_x)
                margin_y = max(abs(y1 - y2) * 0.4, 150 * res_y)
                window = from_bounds(min(x1, x2) - margin_x,
                                     min(y1, y2) - margin_y,
                                     max(x1, x2) + margin_x,
                                     max(y1, y2) + margin_y,
                                     transform=source.transform)
                dem_window = source.read(1, window=window)
                window_transform = source.window_transform(window)
                nodata = source.nodata

            extent = [
                window_transform.c,
                window_transform.c + dem_window.shape[1] * window_transform.a,
                window_transform.f + dem_window.shape[0] * window_transform.e,
                window_transform.f,
            ]
        except Exception as exc:
            self.console.log(f"✖ Error procesando el DEM: {exc}")
            return

        # --- métricas del enlace ---------------------------------------
        sight_line = np.linspace(elevations[0], elevations[-1], elevations.size)
        clearance = sight_line - elevations
        worst = float(np.min(clearance[1:-1])) if clearance.size > 2 else 0.0

        self.row_distance.set_value(f"{distance_km:,.3f} km")
        self.row_elev_a.set_value(f"{elevations[0]:,.1f} m")
        self.row_elev_b.set_value(f"{elevations[-1]:,.1f} m")
        if worst >= 0:
            self.row_clearance.set_value(f"libre · margen {worst:,.1f} m",
                                         theme.OK_BRIGHT)
        else:
            self.row_clearance.set_value(f"OBSTRUIDA · {abs(worst):,.1f} m",
                                         theme.DANGER)
        self.console.log(f"Distancia {distance_km:.3f} km · margen mínimo "
                         f"{worst:,.1f} m")

        if HAS_MATPLOTLIB:
            self._draw(profile_x, elevations, distance_km, name1, name2,
                       dem_window, window_transform, extent,
                       (x1, y1), (x2, y2), nodata)
        self.console.log("✔ Listo.")

    # ------------------------------------------------------------------
    def _draw(self, profile_x, elevations, distance_km, name1, name2,
              dem_window, window_transform, extent, point_a, point_b,
              nodata) -> None:
        if self._canvas is not None:
            self.plot_layout.removeWidget(self._canvas)
            self._canvas.deleteLater()
            self._canvas = None
        if self._toolbar is not None:
            self.plot_layout.removeWidget(self._toolbar)
            self._toolbar.deleteLater()
            self._toolbar = None
        if self._figure is not None:
            plt.close(self._figure)
        self.placeholder.setVisible(False)

        plt.style.use("dark_background")
        self._figure, (ax_profile, ax_map) = plt.subplots(
            2, 1, figsize=(9, 9), gridspec_kw={"height_ratios": [1, 1.8]})
        self._figure.patch.set_facecolor(theme.BG)

        # --- perfil -----------------------------------------------------
        ax_profile.set_facecolor(theme.BG_PANEL)
        ax_profile.fill_between(profile_x, 0, elevations, color="#4E342E",
                                alpha=0.9)
        ax_profile.plot(profile_x, elevations, color="#A1887F", lw=1)
        ax_profile.plot([0, distance_km], [elevations[0], elevations[-1]],
                        color="#00ffaa", lw=1.5, linestyle="--",
                        label="Línea de vista")
        ax_profile.set_title(
            f"Perfil de elevación · distancia {distance_km:.2f} km",
            color="white")
        ax_profile.set_ylabel("altitud (m)", color="white")
        ax_profile.tick_params(colors="gray")
        box = dict(boxstyle="round,pad=0.3", fc="#222222", ec="#555555",
                   alpha=0.9)
        ax_profile.annotate(f"{name1}\n{elevations[0]:.0f} m",
                            xy=(0, elevations[0]), xytext=(0, 20),
                            textcoords="offset points", ha="center",
                            color="#00ff55", bbox=box, fontweight="bold")
        ax_profile.annotate(f"{name2}\n{elevations[-1]:.0f} m",
                            xy=(distance_km, elevations[-1]), xytext=(0, 20),
                            textcoords="offset points", ha="center",
                            color="#00aaaa", bbox=box, fontweight="bold")
        span = float(np.max(elevations) - np.min(elevations))
        ax_profile.set_ylim(max(0.0, float(np.min(elevations)) - 100),
                            float(np.max(elevations)) + span * 0.5 + 10)
        ax_profile.grid(True, alpha=0.2, color="gray")
        ax_profile.legend(facecolor=theme.BG_PANEL, edgecolor="none",
                          labelcolor="white")

        # --- mapa de sombras --------------------------------------------
        surface = dem_window.astype(float)
        if nodata is not None:
            surface[surface == nodata] = np.nan
        light = LightSource(azdeg=315, altdeg=45)
        shade = light.hillshade(surface, vert_exag=1.5,
                                dx=abs(window_transform.a),
                                dy=abs(window_transform.e))

        ax_map.set_facecolor(theme.BG_PANEL)
        ax_map.imshow(shade, cmap="gray", extent=extent, origin="upper")
        ax_map.plot([point_a[0], point_b[0]], [point_a[1], point_b[1]],
                    color="#ff3333", lw=2.5)
        ax_map.plot(point_a[0], point_a[1], marker="o", color="#00ff55",
                    markersize=8, markeredgecolor="black")
        ax_map.plot(point_b[0], point_b[1], marker="o", color="#00aaaa",
                    markersize=8, markeredgecolor="black")
        stroke = [path_effects.withStroke(linewidth=3, foreground="black")]
        ax_map.text(point_a[0], point_a[1], f"  {name1}", color="#00ff55",
                    fontweight="bold", fontsize=11, ha="left", va="center",
                    path_effects=stroke)
        ax_map.text(point_b[0], point_b[1], f"  {name2}", color="#00aaaa",
                    fontweight="bold", fontsize=11, ha="right", va="center",
                    path_effects=stroke)
        ax_map.set_title("Proyección topográfica A–B", color="white")
        ax_map.set_xlabel("coordenada X (CRS del DEM)", color="gray")
        ax_map.set_ylabel("coordenada Y (CRS del DEM)", color="gray")
        ax_map.tick_params(colors="gray")

        self._figure.tight_layout()
        self._canvas = Canvas(self._figure)
        self._toolbar = Toolbar(self._canvas, self.plot_container)
        self.plot_layout.addWidget(self._toolbar)
        self.plot_layout.addWidget(self._canvas)
