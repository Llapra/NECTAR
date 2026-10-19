"""Tema visual de NECTAR.

Se conserva la identidad del programa anterior (fondo casi negro, acentos en
cian, consolas en verde fosforescente sobre negro) porque es lo que ya conoce
quien opera el equipo de noche en la cúpula, y porque con poca luz ambiente
un fondo oscuro no arruina la adaptación del ojo.

Todo el color vive acá: las pestañas no escriben hojas de estilo propias salvo
para el estado puntual de un botón.
"""

from __future__ import annotations

# --- paleta ---------------------------------------------------------------
BG = "#121212"
BG_PANEL = "#1a1a1a"
BG_INPUT = "#222222"
BG_CONSOLE = "#0a0a0a"
BORDER = "#333333"
BORDER_LIGHT = "#444444"

TEXT = "#ffffff"
TEXT_DIM = "#cccccc"
TEXT_FAINT = "#888888"

ACCENT = "#00aaaa"          # cian: identidad del programa
ACCENT_BRIGHT = "#00ffff"
OK = "#00cc44"
OK_BRIGHT = "#00ff55"
WARN = "#ffcc00"
DANGER = "#ff3333"
DANGER_DARK = "#cc0000"
ORANGE = "#d1861a"
CONSOLE_TEXT = "#00ff00"
GREY_LED = "#4a4a4a"

MONO = "'Consolas', 'DejaVu Sans Mono', monospace"


def stylesheet() -> str:
    """Hoja de estilo global de la aplicación."""
    return f"""
        QMainWindow, QDialog {{ background-color: {BG}; }}
        QWidget {{ background-color: {BG}; color: {TEXT}; }}

        QTabWidget::pane {{ border: 1px solid {BORDER}; background-color: {BG}; }}
        QTabBar::tab {{
            background-color: #1e1e1e; color: {TEXT_FAINT};
            padding: 9px 16px; border: 1px solid {BORDER};
            border-bottom: none; margin-right: 2px;
            font-weight: bold; font-size: 12px;
        }}
        QTabBar::tab:selected {{
            background-color: #2b2b2b; color: {ACCENT};
            border-bottom: 2px solid {ACCENT};
        }}
        QTabBar::tab:hover {{ color: {ACCENT_BRIGHT}; }}

        QLabel {{ color: {TEXT_DIM}; font-size: 12px; background: transparent; }}

        QGroupBox {{
            border: 1px solid {BORDER}; margin-top: 14px;
            font-weight: bold; color: {ACCENT}; font-size: 12px;
            background-color: {BG_PANEL}; border-radius: 5px;
        }}
        QGroupBox::title {{
            subcontrol-origin: margin; subcontrol-position: top center;
            padding: 0 10px;
        }}

        QTextEdit, QPlainTextEdit {{
            background-color: {BG_CONSOLE}; color: {CONSOLE_TEXT};
            font-family: {MONO}; font-size: 11px;
            border: 1px solid {BORDER};
        }}

        QPushButton {{
            background-color: #252525; color: {TEXT};
            border: 1px solid {BORDER_LIGHT}; border-radius: 4px;
            padding: 6px; font-weight: bold;
        }}
        QPushButton:hover {{ background-color: #353535; border: 1px solid {ACCENT}; }}
        QPushButton:pressed {{ background-color: #1b1b1b; }}
        QPushButton:disabled {{
            background-color: #151515; color: #444444; border: 1px solid #222222;
        }}

        QSlider::groove:horizontal {{
            border: 1px solid {BORDER}; height: 6px;
            background: #000000; margin: 2px 0;
        }}
        QSlider::handle:horizontal {{
            background: {ACCENT}; border: 1px solid {ACCENT};
            width: 16px; margin: -5px 0; border-radius: 8px;
        }}
        QSlider::handle:horizontal:hover {{ background: {ACCENT_BRIGHT}; }}

        QSpinBox, QDoubleSpinBox {{
            background-color: {BG_INPUT}; color: {CONSOLE_TEXT};
            border: 1px solid {BORDER_LIGHT}; padding: 3px; font-weight: bold;
        }}
        QLineEdit {{
            background-color: {BG_INPUT}; color: {TEXT};
            border: 1px solid {BORDER_LIGHT}; padding: 4px;
            selection-background-color: {ACCENT};
        }}
        QComboBox {{
            background-color: {BG_INPUT}; color: {TEXT};
            border: 1px solid {BORDER_LIGHT}; padding: 4px 6px;
        }}
        QComboBox:hover {{ border: 1px solid {ACCENT}; }}
        QComboBox QAbstractItemView {{
            background-color: {BG_PANEL}; color: {TEXT};
            selection-background-color: {ACCENT}; selection-color: #000000;
            border: 1px solid {BORDER};
        }}

        QCheckBox {{ color: {TEXT_DIM}; spacing: 6px; }}
        QCheckBox::indicator {{
            width: 15px; height: 15px;
            border: 1px solid {BORDER_LIGHT}; background: {BG_INPUT};
        }}
        QCheckBox::indicator:checked {{
            background: {ACCENT}; border: 1px solid {ACCENT};
        }}
        QRadioButton {{ color: {TEXT_DIM}; spacing: 6px; }}

        QProgressBar {{
            border: 1px solid {BORDER}; background-color: #000000;
            text-align: center; color: {TEXT}; height: 18px;
        }}
        QProgressBar::chunk {{ background-color: {ACCENT}; }}

        QScrollArea {{ border: none; background-color: {BG}; }}
        QScrollBar:vertical {{
            background: {BG}; width: 12px; margin: 0;
        }}
        QScrollBar::handle:vertical {{
            background: #3a3a3a; min-height: 30px; border-radius: 6px;
        }}
        QScrollBar::handle:vertical:hover {{ background: {ACCENT}; }}
        QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
        QScrollBar:horizontal {{ background: {BG}; height: 12px; margin: 0; }}
        QScrollBar::handle:horizontal {{
            background: #3a3a3a; min-width: 30px; border-radius: 6px;
        }}
        QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{ width: 0; }}

        QTableWidget {{
            background-color: {BG_PANEL}; color: {TEXT};
            gridline-color: {BORDER}; border: 1px solid {BORDER};
        }}
        QHeaderView::section {{
            background-color: #252525; color: {ACCENT};
            padding: 4px; border: 1px solid {BORDER}; font-weight: bold;
        }}

        QSplitter::handle {{ background-color: {BORDER}; }}
        QToolBar {{ background-color: {BG}; border: none; }}
        QToolButton {{
            background-color: #cccccc; border-radius: 4px;
            padding: 3px; margin: 2px;
        }}
        QToolButton:hover {{ background-color: #ffffff; }}
    """


# --- estilos puntuales reutilizables --------------------------------------
def button_style(kind: str = "normal") -> str:
    """Estilo de un botón según su papel.

    ``fire`` es el botón central de la pestaña 1 y tiene que ser inconfundible:
    grande y rojo. ``stop`` es el mismo botón una vez que ya está grabando.
    """
    styles = {
        "normal": "",
        "accent": f"background-color: {ACCENT}; color: #000000; font-weight: bold;",
        "ok": f"background-color: {OK}; color: #000000; font-weight: bold;",
        "warn": f"background-color: {ORANGE}; color: #ffffff; font-weight: bold;",
        "danger": f"background-color: {DANGER_DARK}; color: #ffffff; font-weight: bold;",
        "fire": (f"background-color: #c62828; color: #ffffff; font-weight: bold; "
                 f"font-size: 22px; letter-spacing: 3px; border: 2px solid #ff5555; "
                 f"border-radius: 10px; padding: 18px;"),
        "stop": (f"background-color: #1b1b1b; color: {DANGER}; font-weight: bold; "
                 f"font-size: 22px; letter-spacing: 3px; border: 2px solid {DANGER}; "
                 f"border-radius: 10px; padding: 18px;"),
    }
    return styles.get(kind, "")


def console_style(color: str = CONSOLE_TEXT) -> str:
    return (f"background-color: {BG_CONSOLE}; color: {color}; "
            f"font-family: {MONO}; font-size: 11px; border: 1px solid {BORDER};")


def value_style(color: str = CONSOLE_TEXT, size: int = 14) -> str:
    return (f"font-family: {MONO}; font-size: {size}px; "
            f"color: {color}; font-weight: bold; background: transparent;")


def chip_style(color: str) -> str:
    return (f"background-color: {BG_INPUT}; color: {color}; "
            f"border: 1px solid {color}; border-radius: 9px; "
            f"padding: 2px 8px; font-weight: bold; font-size: 11px;")
