"""
Eagle Translator -- GUI PyQt6  v3.0 "Apex"
==========================================
Rediseno completo sobre v2.1. Mantiene la integracion con renpy_parser y
translator_engines, pero anade:
  - Scan en hilo (no congela la UI)
  - Atajos de teclado en todas las acciones
  - Drag & drop de carpetas/exes sobre la ventana
  - Recientes (ultimos 8 proyectos) con menu en la pestana Traduccion
  - Autoguardado de sesion y reanudacion automatica
  - Pestana Glosario con editor visual (source -> target)
  - Pestana Estadisticas con metricas en vivo del motor
  - Gestion de nombres protegidos desde Ajustes
  - Namespace de cache por-proyecto (aisla traducciones entre juegos)
  - Tooltips en TODOS los controles
  - Log con limite de lineas (truncado automatico)
  - Barra de progreso con ETA + velocidad
  - Busqueda/filtro en Funciones y Clases
  - Mejor contraste, sin sombras innecesarias
  - About dialog (F1)
  - 25+ idiomas en el selector
  - Boton "Ver detalles" en lugar de QMessageBox amontonados
  - Limpieza de cache TOTAL (LRU + disco + protegidos + namespace)
"""
from __future__ import annotations
import os, sys, json, traceback, time, re
from pathlib import Path
from typing import List, Optional, Tuple

from PyQt6.QtCore import (
    Qt, QThread, pyqtSignal, QPoint, QSize, QTimer, QUrl, QEvent,
    QSortFilterProxyModel, QStringListModel,
)
from PyQt6.QtGui import (
    QIcon, QColor, QFont, QPixmap, QPainter, QBrush, QPen, QMouseEvent,
    QKeySequence, QShortcut, QAction, QDragEnterEvent, QDropEvent,
    QDesktopServices,
)
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QGridLayout,
    QLabel, QLineEdit, QPushButton, QComboBox, QCheckBox, QProgressBar,
    QTabWidget, QFileDialog, QPlainTextEdit, QListWidget, QListWidgetItem,
    QMessageBox, QSpinBox, QSlider, QFrame, QSizePolicy, QGraphicsDropShadowEffect,
    QStackedWidget, QTableWidget, QTableWidgetItem, QHeaderView, QMenu,
    QToolButton, QInputDialog, QAbstractItemView, QDialog, QDialogButtonBox,
    QFormLayout, QTextEdit,
)

from renpy_parser import (
    parse_directory, parse_file, extract_source_directory,
    write_translations, write_tl_files, locate_game_dir,
    generate_language_selector, generate_screens_rpy, generate_replace_text,
    parse_and_fill_file,
    scan_inplace_directory, write_inplace_tl,
    scan_sdk_tl_directory, fill_sdk_tl_directory,
    Entry, classify, entries_to_json,
    extract_raw_strings_directory,
)
from translator_engines import (
    translate_batch, DEFAULT_DEEPLX, CACHE,
    GLOSSARY, glossary_add, glossary_add_many, glossary_remove,
    glossary_clear, glossary_all,
    register_character_names, get_protected_names, clear_character_names,
    set_cache_namespace, stats_snapshot, stats_reset,
)

APP_NAME      = "Eagle Translator"
APP_VERSION   = "3.0.0"
CONFIG_PATH   = Path.home() / ".renpy_translator_config.json"
SESSION_PATH  = Path.home() / ".renpy_translator_session.json"

# ---------------------------------------------------------------------------
# Paleta
# ---------------------------------------------------------------------------
COL_BG       = "#0c1117"
COL_BG2      = "#10161f"
COL_PANEL    = "#1a2230"
COL_PANEL2   = "#222d3d"
COL_BORDER   = "#2a3445"
COL_BORDER2  = "#36425a"
COL_TEXT     = "#e9f0f7"
COL_DIM      = "#9aa6b8"
COL_MUTED    = "#5e6a7e"
COL_ACCENT   = "#29e0d4"
COL_ACCENT2  = "#4cf0e6"
COL_OK       = "#5ee49a"
COL_WARN     = "#f0c64a"
COL_ERR      = "#e96c6c"

QSS = f"""
* {{ outline: none; }}
QWidget {{
    background: {COL_BG};
    color: {COL_TEXT};
    font-family: 'Segoe UI', 'Inter', 'Helvetica Neue', sans-serif;
    font-size: 10pt;
}}
QToolTip {{
    background: #0b1018;
    color: {COL_TEXT};
    border: 1px solid {COL_ACCENT};
    padding: 5px 8px;
    border-radius: 6px;
}}
#Root {{
    background: {COL_BG2};
    border: 1px solid {COL_BORDER2};
    border-radius: 14px;
}}
#TitleBar  {{ background: transparent; border: none; }}
#TitleLabel {{ color: {COL_TEXT}; font-size: 11pt; font-weight: 600; letter-spacing: 0.3px; }}
#WinBtn    {{ background: transparent; color: {COL_DIM}; border: none; border-radius: 6px; padding: 4px 10px; font-size: 12pt; }}
#WinBtn:hover  {{ background: {COL_PANEL2}; color: {COL_TEXT}; }}
#WinClose:hover {{ background: #b3261e; color: white; }}

QTabWidget::pane {{ border: none; background: transparent; top: 0; }}
QTabBar {{ background: transparent; qproperty-drawBase: 0; }}
QTabBar::tab {{
    background: transparent; color: {COL_DIM};
    padding: 11px 18px; margin: 0 3px;
    border: none; font-size: 10.5pt; font-weight: 500;
}}
QTabBar::tab:hover {{ color: {COL_TEXT}; }}
QTabBar::tab:selected {{
    color: {COL_ACCENT};
    border-bottom: 2px solid {COL_ACCENT};
    font-weight: 600;
}}

QLineEdit, QPlainTextEdit, QComboBox, QSpinBox, QTextEdit {{
    background: {COL_PANEL};
    color: {COL_TEXT};
    border: 1px solid {COL_BORDER2};
    border-radius: 8px;
    padding: 9px 12px;
    selection-background-color: {COL_ACCENT};
    selection-color: #000;
}}
QLineEdit:focus, QComboBox:focus, QSpinBox:focus, QPlainTextEdit:focus, QTextEdit:focus {{
    border: 1px solid {COL_ACCENT};
}}
QLineEdit:disabled, QComboBox:disabled, QSpinBox:disabled {{ color: {COL_MUTED}; }}
QComboBox::drop-down {{ border: none; width: 28px; }}
QComboBox::down-arrow {{
    image: none;
    border-left: 5px solid transparent;
    border-right: 5px solid transparent;
    border-top: 6px solid {COL_DIM};
    margin-right: 10px;
}}
QComboBox QAbstractItemView {{
    background: {COL_PANEL}; color: {COL_TEXT};
    border: 1px solid {COL_BORDER2};
    selection-background-color: {COL_ACCENT}; selection-color: #000;
    padding: 4px; outline: none;
}}

QPushButton {{
    background: transparent; color: {COL_TEXT};
    border: 1px solid {COL_BORDER2};
    border-radius: 8px; padding: 10px 18px; font-weight: 500;
}}
QPushButton:hover {{ border-color: {COL_ACCENT}; color: {COL_ACCENT}; }}
QPushButton:pressed {{ background: {COL_PANEL2}; }}
QPushButton:disabled {{ color: {COL_MUTED}; border-color: {COL_BORDER}; }}

QPushButton#primary {{
    background: {COL_ACCENT}; color: #001417;
    border: 1px solid {COL_ACCENT}; font-weight: 700; letter-spacing: 1.2px;
}}
QPushButton#primary:hover {{ background: {COL_ACCENT2}; border-color: {COL_ACCENT2}; }}
QPushButton#primary:disabled {{ background: {COL_PANEL2}; color: {COL_MUTED}; border-color: {COL_BORDER2}; }}

QPushButton#danger {{
    border: 1px solid {COL_ERR}; color: {COL_ERR};
}}
QPushButton#danger:hover {{ background: {COL_ERR}; color: #1a0000; }}

QPushButton#ghost {{ background: transparent; border: 1px solid {COL_BORDER2}; padding: 9px 16px; }}

QLabel#H1 {{ font-size: 24pt; font-weight: 700; color: {COL_TEXT}; }}
QLabel#Section {{ color: {COL_DIM}; font-size: 9.5pt; font-weight: 500; }}
QLabel#Big {{ font-size: 38pt; font-weight: 700; color: {COL_TEXT}; }}
QLabel#Small {{ color: {COL_DIM}; font-size: 9pt; }}
QLabel#Detected {{ color: {COL_ACCENT}; font-size: 9pt; }}
QLabel#OK {{ color: {COL_OK}; font-size: 9pt; }}
QLabel#Warn {{ color: {COL_WARN}; font-size: 9pt; }}
QLabel#Err {{ color: {COL_ERR}; font-size: 9pt; }}

QCheckBox {{ color: {COL_TEXT}; spacing: 8px; }}
QCheckBox::indicator {{
    width: 18px; height: 18px;
    border: 1.5px solid {COL_BORDER2}; border-radius: 4px; background: {COL_PANEL};
}}
QCheckBox::indicator:hover {{ border-color: {COL_ACCENT}; }}
QCheckBox::indicator:checked {{ background: {COL_ACCENT}; border-color: {COL_ACCENT}; }}

QSlider::groove:horizontal {{ height: 6px; background: {COL_PANEL2}; border-radius: 3px; }}
QSlider::sub-page:horizontal {{ background: {COL_ACCENT}; border-radius: 3px; }}
QSlider::handle:horizontal {{
    background: {COL_ACCENT}; width: 18px; height: 18px;
    margin: -7px 0; border-radius: 9px; border: 2px solid {COL_BG2};
}}
QSlider::handle:horizontal:hover {{ background: {COL_ACCENT2}; }}

QProgressBar {{
    background: {COL_PANEL}; border: 1px solid {COL_BORDER2};
    border-radius: 6px; text-align: center; color: {COL_TEXT};
    height: 16px; font-size: 8.5pt;
}}
QProgressBar::chunk {{ background: {COL_ACCENT}; border-radius: 5px; }}

QListWidget, QPlainTextEdit, QTextEdit, QTableWidget {{
    background: {COL_PANEL};
    border: 1px solid {COL_BORDER2};
    border-radius: 8px; padding: 6px;
    alternate-background-color: {COL_PANEL2};
}}
QListWidget::item, QTableWidget::item {{ padding: 6px 8px; border-radius: 4px; }}
QListWidget::item:hover {{ background: {COL_PANEL2}; }}
QListWidget::item:selected, QTableWidget::item:selected {{ background: {COL_ACCENT}; color: #001417; }}

QHeaderView::section {{
    background: {COL_PANEL2}; color: {COL_TEXT};
    border: 0px; padding: 8px 10px;
    border-bottom: 1px solid {COL_BORDER2};
    font-weight: 600;
}}

QPushButton#CatBtn {{
    background: transparent; color: {COL_DIM};
    border: none; border-radius: 8px;
    padding: 8px 14px; text-align: center;
    font-size: 11pt; font-weight: 500;
}}
QPushButton#CatBtn:hover {{ color: {COL_TEXT}; }}
QPushButton#CatBtn:checked {{ background: {COL_PANEL2}; color: {COL_TEXT}; font-weight: 600; }}

QFrame#Divider {{ background: {COL_BORDER}; max-height: 1px; min-height: 1px; border: none; }}

QMenu {{
    background: {COL_PANEL}; color: {COL_TEXT};
    border: 1px solid {COL_BORDER2}; padding: 6px; border-radius: 8px;
}}
QMenu::item {{ padding: 7px 14px; border-radius: 4px; }}
QMenu::item:selected {{ background: {COL_ACCENT}; color: #001417; }}

QScrollBar:vertical {{ background: transparent; width: 10px; }}
QScrollBar::handle:vertical {{ background: {COL_PANEL2}; border-radius: 5px; min-height: 30px; }}
QScrollBar::handle:vertical:hover {{ background: {COL_BORDER2}; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; }}
"""

# ---------------------------------------------------------------------------
# Idiomas (ampliado a 25+)
# ---------------------------------------------------------------------------
LANGS = [
    ("Detectar automáticamente", "auto"),
    ("English",                  "EN"),
    ("Spanish (Latam)",          "ES-419"),
    ("Spanish (España)",         "ES"),
    ("Spanish México",           "ES-MX"),
    ("Spanish Argentina",        "ES-AR"),
    ("Spanish Colombia",         "ES-CO"),
    ("Portuguese (BR)",          "PT-BR"),
    ("Portuguese (Portugal)",    "PT-PT"),
    ("French",                   "FR"),
    ("German",                   "DE"),
    ("Italian",                  "IT"),
    ("Dutch",                    "NL"),
    ("Catalan",                  "CA"),
    ("Japanese",                 "JA"),
    ("Chinese (Simplificado)",   "ZH"),
    ("Chinese (Tradicional)",    "ZH-TW"),
    ("Korean",                   "KO"),
    ("Russian",                  "RU"),
    ("Ukrainian",                "UK"),
    ("Polish",                   "PL"),
    ("Czech",                    "CS"),
    ("Hungarian",                "HU"),
    ("Romanian",                 "RO"),
    ("Greek",                    "EL"),
    ("Turkish",                  "TR"),
    ("Arabic",                   "AR"),
    ("Hebrew",                   "HE"),
    ("Hindi",                    "HI"),
    ("Vietnamese",               "VI"),
    ("Thai",                     "TH"),
    ("Indonesian",               "ID"),
    ("Malay",                    "MS"),
    ("Swedish",                  "SV"),
    ("Danish",                   "DA"),
    ("Norwegian",                "NO"),
    ("Finnish",                  "FI"),
]

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
DEFAULT_CONFIG = {
    "deeplx_endpoint":     DEFAULT_DEEPLX,
    "engine":              "google",
    "fallback_google":     True,
    "tl_name":             "spanish_latino",
    "phone_priority":      True,
    "workers":             16,
    "scan_mode":           "source",
    "selector_position":   "bottom_right",
    "source_lang":         "auto",
    "target_lang":         "ES-419",
    "recent_paths":        [],
    "cache_namespace":     "",
    "log_max_lines":       5000,
    "auto_resume":         True,
}

def load_config() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    if CONFIG_PATH.exists():
        try:
            user = json.loads(CONFIG_PATH.read_text(encoding='utf-8'))
            if isinstance(user, dict):
                cfg.update(user)
        except Exception:
            pass
    return cfg

def save_config(cfg: dict):
    try:
        CONFIG_PATH.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding='utf-8')
    except Exception as e:
        print("config save err:", e)

def push_recent(cfg: dict, path: str, limit: int = 8):
    if not path:
        return
    r = [p for p in cfg.get("recent_paths", []) if p != path]
    r.insert(0, path)
    cfg["recent_paths"] = r[:limit]
    save_config(cfg)


# ---------------------------------------------------------------------------
# Workers
# ---------------------------------------------------------------------------
class ScanWorker(QThread):
    """Escaneo en hilo separado para no congelar la UI en proyectos grandes."""
    log = pyqtSignal(str)
    progress = pyqtSignal(str)
    finished_ok = pyqtSignal(list, str)   # entries, game_dir
    failed = pyqtSignal(str)

    def __init__(self, path: str, mode: str, tl_lang: str, sdk_dir_hint: str = ''):
        super().__init__()
        self.path = path
        self.mode = mode
        self.tl_lang = tl_lang
        self.sdk_dir_hint = sdk_dir_hint

    def run(self):
        try:
            path = self.path
            mode = self.mode
            entries: List[Entry] = []
            game_dir = ''

            if os.path.isfile(path) and path.endswith('.rpy'):
                base = os.path.dirname(path)
                entries = parse_file(path, base=base)
                game_dir = base
            else:
                gd = locate_game_dir(path)
                if not gd:
                    self.failed.emit("No se encontró la carpeta game/ del proyecto.")
                    return
                game_dir = gd

                if mode == "source":
                    self.progress.emit("Extrayendo strings fuente…")
                    entries = extract_source_directory(gd)
                    self.progress.emit("Barrido raw universal…")
                    try:
                        known = {e.source for e in entries if e.source}
                        raw = extract_raw_strings_directory(gd, known_sources=known)
                        if raw:
                            entries.extend(raw)
                            self.log.emit(f"🔤 Raw scan: +{len(raw)} strings universales")
                    except Exception as _re:
                        self.log.emit(f"[raw extract] error: {_re}")

                elif mode == "fill_existing":
                    self.progress.emit("Buscando archivos translate existentes…")
                    for dirpath, _, files in os.walk(gd):
                        for fn in files:
                            if fn.endswith('.rpy') and not fn.endswith('.rpyc'):
                                full = os.path.join(dirpath, fn)
                                try: entries.extend(parse_and_fill_file(full, base=gd))
                                except Exception as e:
                                    print(f'[fill parse error] {full}: {e}')
                    self.log.emit(f"Modo Fill: {len(entries)} entradas listas para rellenar.")

                elif mode == "fill_sdk":
                    sdk_dir = self.sdk_dir_hint or os.path.join(gd, 'tl', self.tl_lang)
                    if not os.path.isdir(sdk_dir):
                        self.failed.emit(f"No se encontró: {sdk_dir}")
                        return
                    self.progress.emit("Escaneando SDK tl/…")
                    sdk_entries = scan_sdk_tl_directory(sdk_dir, lang='')
                    entries = list(sdk_entries)
                    try:
                        sdk_sources = {e.source for e in sdk_entries if e.source}
                        source_entries = extract_source_directory(gd)
                        extra_defines = [e for e in source_entries
                                         if e.kind == "source_define"
                                         and e.source and e.source not in sdk_sources]
                        extra_phone = [e for e in source_entries
                                       if e.category == "phone"
                                       and e.source and e.source not in sdk_sources]
                        known = sdk_sources | {e.source for e in source_entries if e.source}
                        raw_entries = extract_raw_strings_directory(gd, known_sources=known)
                        if extra_defines or extra_phone or raw_entries:
                            entries.extend(extra_defines); entries.extend(extra_phone); entries.extend(raw_entries)
                            self.log.emit(
                                f"🧩 Fill SDK extra: +{len(extra_defines)} define + "
                                f"{len(extra_phone)} phone + {len(raw_entries)} raw")
                    except Exception as _sdk_ex:
                        self.log.emit(f"[fill_sdk extras] error: {_sdk_ex}")

                elif mode == "inplace":
                    self.progress.emit("Escaneando bloques translate inplace…")
                    entries = scan_inplace_directory(gd, self.tl_lang)
                    if not entries:
                        self.failed.emit(
                            f"No se encontraron bloques 'translate {self.tl_lang}:' con líneas vacías.")
                        return

                else:
                    self.progress.emit("Parse tl/ existente…")
                    entries = parse_directory(gd)

            self.finished_ok.emit(entries, game_dir)
        except Exception as ex:
            self.failed.emit(f"{ex}\n{traceback.format_exc()}")


class TranslateWorker(QThread):
    progress = pyqtSignal(int, int, dict)  # done, total, info(elapsed/speed/eta)
    log = pyqtSignal(str)
    finished_ok = pyqtSignal(list)
    failed = pyqtSignal(str)

    def __init__(self, entries: List[Entry], source: str, target: str,
                 engine: str, deeplx_endpoint: str, fallback: bool,
                 workers: int = 16, only_empty: bool = True):
        super().__init__()
        self.entries = entries
        self.source = source
        self.target = target
        self.engine = engine
        self.deeplx_endpoint = deeplx_endpoint
        self.fallback = fallback
        self.workers = workers
        self.only_empty = only_empty
        self._stop = False

    def stop(self): self._stop = True

    def run(self):
        try:
            todo = [e for e in self.entries
                    if (not self.only_empty or not (e.translation and e.translation.strip()))]
            total = len(todo)
            self.log.emit(f"→ Traduciendo {total} entradas con {self.engine.upper()} "
                          f"({self.source}→{self.target}) | workers={self.workers} | "
                          f"fallback={'on' if self.fallback else 'off'}")
            if total == 0:
                self.finished_ok.emit(self.entries); return

            t0 = time.time()
            texts = [e.source for e in todo]

            def _progress(d, t, info=None):
                self.progress.emit(d, t, info or {})

            results = translate_batch(
                texts, source=self.source, target=self.target,
                engine=self.engine, deeplx_endpoint=self.deeplx_endpoint,
                fallback=self.fallback, workers=self.workers,
                progress_cb=_progress, stop_flag=lambda: self._stop,
            )
            for e, out in zip(todo, results):
                if out and out.strip():
                    e.translation = out

            failed_count = sum(1 for r in results if not r or not r.strip())
            dt = time.time() - t0
            speed = total / dt if dt > 0 else 0
            self.log.emit(f"⏱ Completado en {dt:.1f}s  ({speed:.1f} entradas/s)")
            if failed_count:
                self.log.emit(f"⚠ {failed_count} entradas no pudieron traducirse.")
            self.finished_ok.emit(self.entries)
        except Exception as ex:
            self.failed.emit(f"{ex}\n{traceback.format_exc()}")


# ---------------------------------------------------------------------------
# Mascota
# ---------------------------------------------------------------------------
def make_mascot(size: int = 36) -> QPixmap:
    pm = QPixmap(size, size); pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm); p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setBrush(QBrush(QColor(COL_ACCENT))); p.setPen(Qt.PenStyle.NoPen)
    p.drawRoundedRect(2, 2, size-4, size-4, 10, 10)
    p.setBrush(QBrush(QColor("#001417")))
    eye = size * 0.13
    p.drawEllipse(int(size*0.30), int(size*0.38), int(eye), int(eye))
    p.drawEllipse(int(size*0.58), int(size*0.38), int(eye), int(eye))
    pen = QPen(QColor("#001417")); pen.setWidth(max(2, size//18))
    p.setPen(pen)
    p.drawArc(int(size*0.32), int(size*0.50), int(size*0.36), int(size*0.30), 0, -180*16)
    p.end()
    return pm


# ---------------------------------------------------------------------------
# StatsPanel (lateral derecho)
# ---------------------------------------------------------------------------
class StatsPanel(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedWidth(240)
        v = QVBoxLayout(self); v.setContentsMargins(0, 6, 0, 6); v.setSpacing(2)

        v.addStretch(1)
        self.count = QLabel("0"); self.count.setObjectName("Big")
        self.count.setAlignment(Qt.AlignmentFlag.AlignCenter)
        v.addWidget(self.count)
        sub = QLabel("cadenas totales"); sub.setObjectName("Small")
        sub.setAlignment(Qt.AlignmentFlag.AlignCenter)
        v.addWidget(sub)

        v.addSpacing(20)

        self.btn_dialogue = self._mk("Diálogos")
        self.btn_phone    = self._mk("Teléfono")
        self.btn_menu     = self._mk("Menús")
        self.btn_raw      = self._mk("Raw")
        for b in (self.btn_dialogue, self.btn_phone, self.btn_menu, self.btn_raw):
            v.addWidget(b)
        self.btn_dialogue.setChecked(True)

        v.addSpacing(16)
        self.translated = QLabel("Sin traducir"); self.translated.setObjectName("Small")
        self.translated.setAlignment(Qt.AlignmentFlag.AlignCenter)
        v.addWidget(self.translated)

        v.addStretch(2)
        self._counts = {"dialogue": 0, "phone": 0, "menu": 0, "raw": 0, "total": 0}

    def _mk(self, name: str) -> QPushButton:
        b = QPushButton(name); b.setObjectName("CatBtn"); b.setCheckable(True)
        b.setAutoExclusive(True); b.setCursor(Qt.CursorShape.PointingHandCursor)
        return b

    def set_counts(self, dialogue, phone, menu, raw, total):
        self._counts = {"dialogue": dialogue, "phone": phone, "menu": menu, "raw": raw, "total": total}
        self.count.setText(f"{total:,}".replace(',', '.'))
        self.btn_dialogue.setText(f"Diálogos  ·  {dialogue:,}".replace(',', '.'))
        self.btn_phone.setText(f"Teléfono  ·  {phone:,}".replace(',', '.'))
        self.btn_menu.setText(f"Menús  ·  {menu:,}".replace(',', '.'))
        self.btn_raw.setText(f"Raw  ·  {raw:,}".replace(',', '.'))

    def set_translated(self, n_ok: int, total: int):
        if total <= 0:
            self.translated.setText(""); return
        pct = (n_ok / total * 100.0) if total else 0
        self.translated.setText(f"{n_ok:,}/{total:,}  ·  {pct:.0f}%")


# ---------------------------------------------------------------------------
# TAB 1 -- Traduccion
# ---------------------------------------------------------------------------
class TraduccionTab(QWidget):
    def __init__(self, main):
        super().__init__()
        self.main = main
        cfg = main.config
        self.setAcceptDrops(True)

        root = QHBoxLayout(self); root.setContentsMargins(30, 14, 30, 22); root.setSpacing(24)
        left = QVBoxLayout(); left.setSpacing(12)

        # Ruta
        left.addWidget(self._section("Ruta del juego"))
        path_row = QHBoxLayout(); path_row.setSpacing(8)
        self.path_input = QLineEdit()
        self.path_input.setPlaceholderText(r"C:\Games\MiJuego  ·  arrastra carpeta o .exe")
        self.path_input.setToolTip("Ruta al juego: puede ser carpeta, .exe o archivo .rpy")
        self.path_input.textChanged.connect(self._on_path_changed)
        path_row.addWidget(self.path_input, 1)

        self.btn_browse_dir = QPushButton("Carpeta")
        self.btn_browse_dir.setToolTip("Selecciona la carpeta raíz del juego (Ctrl+O)")
        self.btn_browse_dir.clicked.connect(self.browse_dir)
        path_row.addWidget(self.btn_browse_dir)

        self.btn_browse_exe = QPushButton(".exe")
        self.btn_browse_exe.setToolTip("Selecciona el ejecutable del juego")
        self.btn_browse_exe.clicked.connect(self.browse_exe)
        path_row.addWidget(self.btn_browse_exe)

        self.btn_recent = QToolButton(); self.btn_recent.setText("Recientes ▾")
        self.btn_recent.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.btn_recent.setToolTip("Proyectos abiertos recientemente")
        self.btn_recent.setStyleSheet(
            f"QToolButton {{background: transparent; color: {COL_TEXT};"
            f" border: 1px solid {COL_BORDER2}; border-radius: 8px; padding: 9px 12px;}} "
            f"QToolButton:hover {{ border-color: {COL_ACCENT}; color: {COL_ACCENT}; }}"
        )
        self.menu_recent = QMenu(self); self.btn_recent.setMenu(self.menu_recent)
        self._rebuild_recent_menu()
        path_row.addWidget(self.btn_recent)

        left.addLayout(path_row)
        self.detected = QLabel(""); self.detected.setObjectName("Detected")
        left.addWidget(self.detected)

        left.addSpacing(4)

        # Idiomas
        lang_row = QHBoxLayout(); lang_row.setSpacing(14)
        lcol_a = QVBoxLayout(); lcol_a.setSpacing(6)
        lcol_a.addWidget(self._section("Idioma original"))
        self.src = QComboBox()
        self.src.setToolTip("Idioma del juego original")
        for n, c in LANGS: self.src.addItem(n, c)
        self._select_by_data(self.src, cfg.get("source_lang", "auto"))
        lcol_a.addWidget(self.src)
        lang_row.addLayout(lcol_a, 1)

        lcol_b = QVBoxLayout(); lcol_b.setSpacing(6)
        lcol_b.addWidget(self._section("Nuevo idioma"))
        self.dst = QComboBox()
        self.dst.setToolTip("Idioma al que traducir")
        for n, c in LANGS[1:]: self.dst.addItem(n, c)
        self._select_by_data(self.dst, cfg.get("target_lang", "ES-419"))
        lcol_b.addWidget(self.dst)
        lang_row.addLayout(lcol_b, 1)
        left.addLayout(lang_row)

        # Modo
        left.addWidget(self._section("Modo de escaneo"))
        self.scan_mode = QComboBox()
        self.scan_mode.setToolTip(
            "source       : extrae todo de los .rpy fuente del juego\n"
            "tl_existing  : usa solo bloques translate ya existentes\n"
            "fill_existing: rellena traducciones vacías en .rpy del juego\n"
            "inplace      : escribe en los .rpy originales (con backup)\n"
            "fill_sdk     : rellena los archivos generados por el SDK Ren'Py"
        )
        self.scan_mode.addItem("Auto-detectar TODO (recomendado)", "source")
        self.scan_mode.addItem("Solo bloques translate existentes (tl/)", "tl_existing")
        self.scan_mode.addItem("Rellenar traducciones existentes (Fill Mode)", "fill_existing")
        self.scan_mode.addItem("Escribir directo en .rpy originales (InPlace)", "inplace")
        self.scan_mode.addItem("Rellenar archivos del SDK de Ren'Py (Fill SDK)", "fill_sdk")
        self._select_by_data(self.scan_mode, cfg.get("scan_mode", "source"))
        left.addWidget(self.scan_mode)

        # Hilos
        hil_lbl = QLabel("Hilos"); hil_lbl.setObjectName("Section"); left.addWidget(hil_lbl)
        hil_row = QHBoxLayout(); hil_row.setSpacing(10)
        self.sl_workers = QSlider(Qt.Orientation.Horizontal)
        self.sl_workers.setToolTip("Hilos de traducción en paralelo. 16-32 recomendado.")
        self.sl_workers.setRange(4, 48); self.sl_workers.setValue(int(cfg.get("workers", 16)))
        hil_row.addWidget(self.sl_workers, 1)
        self.sl_workers_lbl = QLabel(str(self.sl_workers.value())); self.sl_workers_lbl.setFixedWidth(28)
        self.sl_workers.valueChanged.connect(lambda v: self.sl_workers_lbl.setText(str(v)))
        hil_row.addWidget(self.sl_workers_lbl)
        left.addLayout(hil_row)

        self.cb_phone = QCheckBox("Priorizar Teléfono / Menús")
        self.cb_phone.setToolTip("Reordena entradas para traducir primero teléfono y menús")
        self.cb_phone.setChecked(cfg.get("phone_priority", True))
        left.addWidget(self.cb_phone)

        # Progress
        self.progress = QProgressBar(); self.progress.setValue(0); self.progress.setVisible(False)
        left.addWidget(self.progress)
        self.eta_label = QLabel(""); self.eta_label.setObjectName("Small")
        left.addWidget(self.eta_label)

        # Botones
        btn_row = QHBoxLayout(); btn_row.setSpacing(12)
        self.btn_import = QPushButton("Importar / Escanear")
        self.btn_import.setObjectName("ghost"); self.btn_import.setMinimumHeight(44)
        self.btn_import.setToolTip("Escanear el proyecto (Ctrl+R)")
        self.btn_import.clicked.connect(self.scan)
        btn_row.addWidget(self.btn_import, 1)

        self.btn_translate = QPushButton("TRADUCIR")
        self.btn_translate.setObjectName("primary"); self.btn_translate.setMinimumHeight(44)
        self.btn_translate.setToolTip("Iniciar traducción (Ctrl+T)")
        self.btn_translate.setEnabled(False)
        self.btn_translate.clicked.connect(self.translate_all)
        self._add_glow(self.btn_translate)
        btn_row.addWidget(self.btn_translate, 1)
        left.addLayout(btn_row)

        sec_row = QHBoxLayout(); sec_row.setSpacing(8)
        self.btn_stop = QPushButton("Detener"); self.btn_stop.setEnabled(False)
        self.btn_stop.setToolTip("Cancela la traducción en curso (Esc)")
        self.btn_stop.clicked.connect(self.stop)
        sec_row.addWidget(self.btn_stop)

        self.btn_export = QPushButton("Exportar a game/tl/")
        self.btn_export.setEnabled(False)
        self.btn_export.setToolTip("Genera los archivos .rpy de traducción (Ctrl+E)")
        self.btn_export.clicked.connect(self.export)
        sec_row.addWidget(self.btn_export)

        self.btn_resume = QPushButton("Reanudar")
        self.btn_resume.setToolTip("Cargar la última sesión guardada")
        self.btn_resume.clicked.connect(self.resume_session)
        sec_row.addWidget(self.btn_resume)

        sec_row.addStretch()
        left.addLayout(sec_row)

        self.status = QLabel("Listo."); self.status.setObjectName("Small")
        left.addWidget(self.status)

        left.addStretch(1)
        root.addLayout(left, 1)

        self.stats = StatsPanel()
        right_wrap = QVBoxLayout(); right_wrap.setContentsMargins(0, 4, 0, 0)
        right_wrap.addWidget(self.stats); right_wrap.addStretch()
        root.addLayout(right_wrap, 0)

        # Refresh recent menu cuando la pestana es mostrada
        QTimer.singleShot(0, self._rebuild_recent_menu)

    # ---- helpers ----
    def _section(self, txt: str) -> QLabel:
        lb = QLabel(txt); lb.setObjectName("Section"); return lb

    def _select_by_data(self, combo: QComboBox, data):
        for i in range(combo.count()):
            if combo.itemData(i) == data:
                combo.setCurrentIndex(i); return

    def _add_glow(self, w: QWidget):
        eff = QGraphicsDropShadowEffect(self)
        eff.setBlurRadius(36); eff.setColor(QColor(41, 224, 212, 160))
        eff.setOffset(0, 0); w.setGraphicsEffect(eff)

    def _rebuild_recent_menu(self):
        self.menu_recent.clear()
        recent = self.main.config.get("recent_paths", []) or []
        if not recent:
            a = self.menu_recent.addAction("(vacío)"); a.setEnabled(False)
            return
        for p in recent:
            short = p if len(p) <= 70 else "…" + p[-67:]
            act = self.menu_recent.addAction(short)
            act.triggered.connect(lambda _checked=False, pp=p: self._use_recent(pp))
        self.menu_recent.addSeparator()
        clear = self.menu_recent.addAction("Limpiar lista")
        clear.triggered.connect(self._clear_recent)

    def _use_recent(self, p: str):
        self.path_input.setText(p); self._auto_detect()

    def _clear_recent(self):
        self.main.config["recent_paths"] = []
        save_config(self.main.config); self._rebuild_recent_menu()

    def _on_path_changed(self, txt: str):
        if txt and (os.path.isfile(txt) or os.path.isdir(txt)):
            self._auto_detect()

    # ---- acciones ----
    def browse_dir(self):
        d = QFileDialog.getExistingDirectory(self, "Selecciona la carpeta del juego")
        if d: self.path_input.setText(d); self._auto_detect()

    def browse_exe(self):
        f, _ = QFileDialog.getOpenFileName(self, "Selecciona el .exe del juego", "",
                                           "Ejecutables (*.exe);;Todos (*)")
        if f: self.path_input.setText(f); self._auto_detect()

    def _auto_detect(self):
        path = self.path_input.text().strip()
        gd = locate_game_dir(path) if path else None
        if gd:
            self.detected.setText(f"✓ game/ detectado: {gd}")
            self.main.game_dir = gd
            # Activar namespace de cache automatico
            try:
                ns = self.main.config.get("cache_namespace", "")
                if not ns:
                    set_cache_namespace(os.path.basename(os.path.dirname(gd)) or gd)
                else:
                    set_cache_namespace(ns)
            except Exception:
                pass
        else:
            self.detected.setText("⚠ No se detectó carpeta game/. Usa una ruta válida.")
            self.main.game_dir = None

    def scan(self):
        path = self.path_input.text().strip()
        if not path or not os.path.exists(path):
            QMessageBox.warning(self, APP_NAME, "Ruta inválida."); return
        self._auto_detect()
        self.status.setText("Escaneando…")
        self.btn_import.setEnabled(False)
        self.btn_translate.setEnabled(False)

        mode = self.scan_mode.currentData()
        tl_lang = self.main.config.get("tl_name", "spanish_latino")
        push_recent(self.main.config, path)
        self._rebuild_recent_menu()

        # Worker
        self.scan_worker = ScanWorker(path, mode, tl_lang)
        self.scan_worker.log.connect(self.main.log)
        self.scan_worker.progress.connect(lambda m: self.status.setText(m))
        self.scan_worker.finished_ok.connect(self._on_scan_ok)
        self.scan_worker.failed.connect(self._on_scan_fail)
        self.scan_worker.start()

    def _on_scan_ok(self, entries, game_dir):
        self.btn_import.setEnabled(True)
        self.main.entries = entries
        if game_dir:
            self.main.game_dir = game_dir
            self.main.project_root = game_dir
        ph = sum(1 for e in entries if e.category == 'phone')
        mn = sum(1 for e in entries if e.category == 'menu')
        dl = sum(1 for e in entries if e.category == 'dialogue')
        rw = sum(1 for e in entries if e.category == 'raw')
        self.stats.set_counts(dl, ph, mn, rw, len(entries))
        n_ok = sum(1 for e in entries if e.translation and e.translation.strip())
        self.stats.set_translated(n_ok, len(entries))
        self.status.setText(f"Importado: {len(entries)} cadenas.")
        self.btn_translate.setEnabled(len(entries) > 0)
        self.btn_export.setEnabled(False)
        self.main.refresh_all()
        self.main.log(f"Escaneado: {len(entries)} (phone={ph}, menu={mn}, dialogue={dl}, raw={rw})")
        self.main.save_session_async()

    def _on_scan_fail(self, msg):
        self.btn_import.setEnabled(True)
        self.status.setText("Error.")
        QMessageBox.critical(self, APP_NAME, msg)

    def translate_all(self):
        if not getattr(self.main, 'entries', None):
            QMessageBox.information(self, APP_NAME, "Primero importa un proyecto."); return
        cfg = self.main.config
        src = self.src.currentData() or 'auto'
        tgt = self.dst.currentData() or 'ES'

        entries = list(self.main.entries)
        if self.cb_phone.isChecked():
            entries.sort(key=lambda e: 0 if e.category == 'phone' else (1 if e.category == 'menu' else 2))

        cfg["workers"] = int(self.sl_workers.value())
        cfg["phone_priority"] = self.cb_phone.isChecked()
        cfg["scan_mode"] = self.scan_mode.currentData()
        cfg["source_lang"] = src; cfg["target_lang"] = tgt
        save_config(cfg)

        self.progress.setVisible(True); self.progress.setValue(0)
        self.eta_label.setText("")
        stats_reset()
        self.worker = TranslateWorker(
            entries=entries, source=src, target=tgt,
            engine=cfg.get("engine", "google"),
            deeplx_endpoint=cfg.get("deeplx_endpoint", DEFAULT_DEEPLX),
            fallback=cfg.get("fallback_google", True),
            workers=int(self.sl_workers.value()),
            only_empty=True,
        )
        self.worker.progress.connect(self._on_progress)
        self.worker.log.connect(self.main.log)
        self.worker.finished_ok.connect(self.on_done)
        self.worker.failed.connect(lambda m: (QMessageBox.critical(self, APP_NAME, m),
                                              self.status.setText("Error.")))
        self.btn_translate.setEnabled(False); self.btn_stop.setEnabled(True)
        self.btn_import.setEnabled(False)
        self.status.setText("Traduciendo…")
        self.worker.start()

    def _on_progress(self, d, t, info):
        self.progress.setMaximum(t); self.progress.setValue(d)
        speed = info.get('speed', 0) if info else 0
        eta   = info.get('eta', 0)   if info else 0
        if t > 0:
            pct = d / t * 100
            self.status.setText(f"Traduciendo  {d:,}/{t:,}  ·  {pct:.1f}%")
            if speed > 0:
                eta_txt = self._fmt_eta(eta)
                self.eta_label.setText(f"{speed:.1f} entradas/s  ·  ETA {eta_txt}")

    def _fmt_eta(self, secs: float) -> str:
        if secs <= 0: return "—"
        if secs < 60: return f"{secs:.0f}s"
        if secs < 3600: return f"{secs/60:.1f}m"
        return f"{secs/3600:.1f}h"

    def stop(self):
        if hasattr(self, 'worker'): self.worker.stop()
        self.status.setText("Deteniendo…")

    def on_done(self, entries):
        self.main.entries = entries
        self.btn_translate.setEnabled(True); self.btn_stop.setEnabled(False)
        self.btn_import.setEnabled(True)
        self.btn_export.setEnabled(True)
        n_ok = sum(1 for e in entries if e.translation and e.translation.strip())
        self.status.setText(f"Traducción completa ✔  ({n_ok}/{len(entries)})")
        self.stats.set_translated(n_ok, len(entries))
        self.main.log(f"✔ Traducción completa. {n_ok}/{len(entries)} con texto.")
        self.main.save_session_async()
        QMessageBox.information(self, APP_NAME,
            f"Traducción terminada.\n{n_ok}/{len(entries)} entradas traducidas.\n\n"
            f"Pulsa 'Exportar a game/tl/' para generar los .rpy.")

    def resume_session(self):
        if not SESSION_PATH.exists():
            QMessageBox.information(self, APP_NAME, "No hay sesión guardada."); return
        try:
            data = json.loads(SESSION_PATH.read_text(encoding='utf-8'))
            entries = [Entry(**d) for d in data.get('entries', [])]
            gd = data.get('game_dir', '')
            path = data.get('path', '')
            if path:
                self.path_input.setText(path)
            if gd:
                self.main.game_dir = gd
                self.main.project_root = gd
            self.main.entries = entries
            ph = sum(1 for e in entries if e.category == 'phone')
            mn = sum(1 for e in entries if e.category == 'menu')
            dl = sum(1 for e in entries if e.category == 'dialogue')
            rw = sum(1 for e in entries if e.category == 'raw')
            self.stats.set_counts(dl, ph, mn, rw, len(entries))
            n_ok = sum(1 for e in entries if e.translation and e.translation.strip())
            self.stats.set_translated(n_ok, len(entries))
            self.btn_translate.setEnabled(len(entries) > 0)
            self.btn_export.setEnabled(n_ok > 0)
            self.main.refresh_all()
            self.status.setText(f"Sesión restaurada: {len(entries)} cadenas ({n_ok} traducidas).")
            self.main.log(f"⤴ Sesión restaurada de {SESSION_PATH}")
        except Exception as e:
            QMessageBox.critical(self, APP_NAME, f"Error cargando sesión: {e}")

    def export(self):
        if not getattr(self.main, 'entries', None):
            QMessageBox.information(self, APP_NAME, "Nada que exportar."); return
        if not self.main.game_dir:
            QMessageBox.warning(self, APP_NAME, "No hay carpeta game/ detectada."); return

        mode = self.scan_mode.currentData()

        if mode == "inplace":
            n_with_tl = sum(1 for e in self.main.entries if e.translation and e.translation.strip())
            if n_with_tl == 0:
                QMessageBox.warning(self, APP_NAME, "Traduce primero antes de exportar."); return
            reply = QMessageBox.question(self, APP_NAME,
                f"MODO ESCRITURA DIRECTA\nSe van a modificar los .rpy del juego.\n"
                f"Se creará un .rpy.bak por archivo.\n\nEntradas: {n_with_tl}\n¿Continuar?")
            if reply != QMessageBox.StandardButton.Yes: return
            try:
                n_files, n_lines = write_inplace_tl(self.main.game_dir, self.main.entries, backup=True)
                self.main.log(f"✏️  InPlace: {n_files} archivos / {n_lines} líneas")
                QMessageBox.information(self, APP_NAME, f"✅ {n_files} archivos / {n_lines} líneas escritas.")
            except Exception as e:
                QMessageBox.critical(self, APP_NAME, f"Error:\n{e}\n{traceback.format_exc()}")
            return

        if mode == "fill_sdk":
            n_with_tl = sum(1 for e in self.main.entries if e.translation and e.translation.strip())
            if n_with_tl == 0:
                QMessageBox.warning(self, APP_NAME, "Traduce primero antes de exportar."); return
            sdk_dir = getattr(self.main, '_sdk_tl_dir', None)
            if not sdk_dir or not os.path.isdir(sdk_dir):
                tl_lang = self.main.config.get("tl_name", "spanish_latino")
                sdk_dir = QFileDialog.getExistingDirectory(self,
                    f"Selecciona la carpeta tl/{tl_lang}/", os.path.join(self.main.game_dir, 'tl'))
                if not sdk_dir: return
            tl_lang = os.path.basename(os.path.normpath(sdk_dir))
            try:
                n_files, n_lines = fill_sdk_tl_directory(sdk_dir, self.main.entries, lang='', backup=True)
                try:
                    rt_path = generate_replace_text(self.main.game_dir, tl_lang, self.main.entries)
                    self.main.log(f"🔁 Fill SDK runtime → {rt_path}")
                except Exception as _e:
                    self.main.log(f"⚠ replaceText: {_e}")
                self.main.log(f"🔧 Fill SDK: {n_files} archivos / {n_lines} líneas")
                QMessageBox.information(self, APP_NAME,
                    f"✅ Fill SDK completado.\n{n_files} archivos / {n_lines} líneas.")
            except Exception as e:
                QMessageBox.critical(self, APP_NAME, f"Error:\n{e}\n{traceback.format_exc()}")
            return

        tl_lang = self.main.config.get("tl_name", "spanish_latino")
        default_out = os.path.join(self.main.game_dir, 'tl', tl_lang)
        out_dir = QFileDialog.getExistingDirectory(self,
            f"Carpeta de salida (sugerido: {default_out})", default_out)
        if not out_dir: return
        try:
            lang_from_path = os.path.basename(os.path.normpath(out_dir)) or tl_lang
            n_files, n_entries = write_tl_files(self.main.game_dir, lang_from_path,
                                                self.main.entries, out_root=out_dir)
            self.main.log(f"💾 Exportado: {n_files} archivos / {n_entries} entradas → {out_dir}")

            try:
                sc_path = generate_screens_rpy(self.main.game_dir, lang_from_path)
                self.main.log(f"🖥  screens.rpy → {sc_path}")
            except Exception as e:
                self.main.log(f"⚠ screens.rpy: {e}")
            try:
                rt_path = generate_replace_text(self.main.game_dir, lang_from_path, self.main.entries)
                self.main.log(f"🔁 replaceText.rpy → {rt_path}")
            except Exception as e:
                self.main.log(f"⚠ replaceText.rpy: {e}")

            reply = QMessageBox.question(self, APP_NAME,
                f"Exportado correctamente.\n{n_files} archivos / {n_entries} entradas.\n\n"
                f"¿Generar también el selector de idioma en-juego?")
            if reply == QMessageBox.StandardButton.Yes:
                pos = self.main.config.get("selector_position", "bottom_right")
                sel_path = generate_language_selector(
                    game_dir=self.main.game_dir, tl_lang=lang_from_path, position=pos)
                self.main.log(f"🌐 Selector → {sel_path}")
                QMessageBox.information(self, APP_NAME, f"✅ Selector generado:\n{sel_path}")
        except Exception as e:
            QMessageBox.critical(self, APP_NAME, f"{e}\n{traceback.format_exc()}")

    # Drag & drop
    def dragEnterEvent(self, e: QDragEnterEvent):
        if e.mimeData().hasUrls():
            for u in e.mimeData().urls():
                if u.isLocalFile():
                    e.acceptProposedAction(); return
        e.ignore()

    def dropEvent(self, e: QDropEvent):
        for u in e.mimeData().urls():
            if u.isLocalFile():
                p = u.toLocalFile()
                self.path_input.setText(p)
                self._auto_detect()
                break


# ---------------------------------------------------------------------------
# TAB -- Glosario
# ---------------------------------------------------------------------------
class GlosarioTab(QWidget):
    def __init__(self, main):
        super().__init__(); self.main = main
        lay = QVBoxLayout(self); lay.setContentsMargins(30, 14, 30, 22); lay.setSpacing(10)
        head = QHBoxLayout()
        head.addWidget(QLabel("Glosario  ·  términos forzados (no se traducen, se sustituyen exactamente)", objectName="Section"))
        head.addStretch()

        self.search = QLineEdit(); self.search.setPlaceholderText("Buscar…")
        self.search.setMaximumWidth(220)
        self.search.textChanged.connect(self._apply_filter)
        head.addWidget(self.search)
        lay.addLayout(head)

        self.table = QTableWidget(0, 2)
        self.table.setHorizontalHeaderLabels(["Origen", "Traducción forzada"])
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.table.verticalHeader().setVisible(False)
        self.table.setAlternatingRowColors(True)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.DoubleClicked
                                   | QAbstractItemView.EditTrigger.SelectedClicked
                                   | QAbstractItemView.EditTrigger.EditKeyPressed)
        self.table.itemChanged.connect(self._on_item_changed)
        lay.addWidget(self.table, 1)

        btns = QHBoxLayout()
        self.btn_add = QPushButton("Añadir"); self.btn_add.setToolTip("Crear nueva entrada")
        self.btn_add.clicked.connect(self.add_row)
        btns.addWidget(self.btn_add)

        self.btn_remove = QPushButton("Quitar"); self.btn_remove.setToolTip("Eliminar selección")
        self.btn_remove.clicked.connect(self.remove_selected)
        btns.addWidget(self.btn_remove)

        self.btn_import = QPushButton("Importar JSON"); self.btn_import.clicked.connect(self.import_json)
        btns.addWidget(self.btn_import)
        self.btn_export = QPushButton("Exportar JSON"); self.btn_export.clicked.connect(self.export_json)
        btns.addWidget(self.btn_export)
        self.btn_clear = QPushButton("Vaciar todo"); self.btn_clear.setObjectName("danger")
        self.btn_clear.clicked.connect(self.clear_all)
        btns.addWidget(self.btn_clear)
        btns.addStretch()

        self.count_label = QLabel(""); self.count_label.setObjectName("Small")
        btns.addWidget(self.count_label)
        lay.addLayout(btns)

        self._loading = False
        self.reload()

    def reload(self):
        self._loading = True
        items = glossary_all()
        self.table.setRowCount(0)
        for src, tgt in sorted(items.items()):
            r = self.table.rowCount(); self.table.insertRow(r)
            self.table.setItem(r, 0, QTableWidgetItem(src))
            self.table.setItem(r, 1, QTableWidgetItem(tgt))
        self.count_label.setText(f"{len(items)} entradas")
        self._loading = False
        self._apply_filter(self.search.text())

    def _apply_filter(self, txt: str):
        q = (txt or '').strip().lower()
        for r in range(self.table.rowCount()):
            if not q:
                self.table.setRowHidden(r, False); continue
            a = self.table.item(r, 0); b = self.table.item(r, 1)
            t1 = a.text().lower() if a else ''
            t2 = b.text().lower() if b else ''
            self.table.setRowHidden(r, q not in t1 and q not in t2)

    def add_row(self):
        src, ok = QInputDialog.getText(self, APP_NAME, "Texto original:")
        if not ok or not src.strip(): return
        tgt, ok = QInputDialog.getText(self, APP_NAME, f"Traducción forzada para '{src}':")
        if not ok or not tgt.strip(): return
        glossary_add(src.strip(), tgt.strip())
        self.reload()
        self.main.log(f"📓 Glosario +1: {src!r} → {tgt!r}")

    def remove_selected(self):
        rows = sorted({i.row() for i in self.table.selectedItems()}, reverse=True)
        if not rows:
            return
        for r in rows:
            it = self.table.item(r, 0)
            if it:
                glossary_remove(it.text())
            self.table.removeRow(r)
        self.reload()

    def clear_all(self):
        if QMessageBox.question(self, APP_NAME, "¿Vaciar TODO el glosario?") != QMessageBox.StandardButton.Yes:
            return
        glossary_clear(); self.reload()

    def import_json(self):
        p, _ = QFileDialog.getOpenFileName(self, "Importar glosario JSON", "", "JSON (*.json)")
        if not p: return
        try:
            data = json.loads(Path(p).read_text(encoding='utf-8'))
            if isinstance(data, dict):
                glossary_add_many({str(k): str(v) for k, v in data.items()})
                self.reload()
                self.main.log(f"📓 Glosario importado: +{len(data)} entradas")
        except Exception as e:
            QMessageBox.critical(self, APP_NAME, f"Error: {e}")

    def export_json(self):
        p, _ = QFileDialog.getSaveFileName(self, "Exportar glosario", "glossary.json", "JSON (*.json)")
        if not p: return
        Path(p).write_text(json.dumps(glossary_all(), ensure_ascii=False, indent=2), encoding='utf-8')
        QMessageBox.information(self, APP_NAME, "Glosario exportado.")

    def _on_item_changed(self, item: QTableWidgetItem):
        if self._loading: return
        r = item.row()
        a = self.table.item(r, 0); b = self.table.item(r, 1)
        if not a or not b: return
        src = a.text().strip(); tgt = b.text().strip()
        if src and tgt:
            glossary_add(src, tgt)


# ---------------------------------------------------------------------------
# TAB -- Estadisticas
# ---------------------------------------------------------------------------
class EstadisticasTab(QWidget):
    def __init__(self, main):
        super().__init__(); self.main = main
        lay = QGridLayout(self); lay.setContentsMargins(34, 18, 34, 28)
        lay.setHorizontalSpacing(14); lay.setVerticalSpacing(10)

        lay.addWidget(QLabel("Estadísticas del motor de traducción", objectName="Section"), 0, 0, 1, 4)

        self.metrics = {}
        rows = [
            ("Cache hits",        "cache_hits"),
            ("Cache misses",      "cache_misses"),
            ("Hit rate",          "hit_rate_pct"),
            ("Batch calls",       "batch_calls"),
            ("Single calls",      "single_calls"),
            ("Fallbacks",         "fallback_calls"),
            ("Errores HTTP",      "http_errors"),
            ("Caracteres totales","total_chars"),
            ("Chars/seg",         "chars_per_sec"),
            ("Uptime",            "uptime_s"),
        ]
        for i, (label, k) in enumerate(rows):
            row = 1 + (i // 2)
            col = (i % 2) * 2
            lay.addWidget(QLabel(label, objectName="Section"), row, col)
            v = QLabel("0"); v.setStyleSheet(f"color: {COL_TEXT}; font-size: 16pt; font-weight: 600;")
            self.metrics[k] = v
            lay.addWidget(v, row, col + 1)

        sep = QFrame(); sep.setObjectName("Divider")
        lay.addWidget(sep, 7, 0, 1, 4)

        lay.addWidget(QLabel("Caché en disco", objectName="Section"), 8, 0)
        self.cache_lbl = QLabel("—"); lay.addWidget(self.cache_lbl, 8, 1, 1, 3)

        lay.addWidget(QLabel("Nombres protegidos", objectName="Section"), 9, 0)
        self.prot_lbl = QLabel("—"); lay.addWidget(self.prot_lbl, 9, 1, 1, 3)

        btns = QHBoxLayout()
        self.btn_refresh = QPushButton("Actualizar"); self.btn_refresh.clicked.connect(self.refresh)
        btns.addWidget(self.btn_refresh)
        self.btn_reset = QPushButton("Reset contadores"); self.btn_reset.clicked.connect(self._reset)
        btns.addWidget(self.btn_reset)
        btns.addStretch()
        w = QWidget(); w.setLayout(btns); lay.addWidget(w, 10, 0, 1, 4)
        lay.setRowStretch(11, 1)

        self._timer = QTimer(self); self._timer.timeout.connect(self.refresh); self._timer.start(1500)
        self.refresh()

    def _reset(self):
        stats_reset(); self.refresh()

    def refresh(self):
        try:
            snap = stats_snapshot()
            for k, lbl in self.metrics.items():
                v = snap.get(k, 0)
                if k == 'hit_rate_pct':
                    lbl.setText(f"{v}%")
                elif k == 'total_chars':
                    lbl.setText(f"{int(v):,}".replace(',', '.'))
                elif k == 'uptime_s':
                    lbl.setText(f"{v}s")
                else:
                    lbl.setText(f"{v}")
            disk, mem = CACHE.size()
            self.cache_lbl.setText(f"{disk:,} disco · {mem:,} memoria".replace(',', '.'))
            prot = get_protected_names()
            self.prot_lbl.setText(f"{len(prot)} nombres registrados")
        except Exception:
            pass


# ---------------------------------------------------------------------------
# TAB -- Funciones (con filtro)
# ---------------------------------------------------------------------------
class FuncionesTab(QWidget):
    def __init__(self, main):
        super().__init__(); self.main = main
        lay = QVBoxLayout(self); lay.setContentsMargins(30, 14, 30, 22); lay.setSpacing(10)

        head = QHBoxLayout()
        head.addWidget(QLabel("Funciones / Speakers", objectName="Section"))
        head.addStretch()
        self.search = QLineEdit(); self.search.setPlaceholderText("Filtrar…")
        self.search.setMaximumWidth(240); self.search.textChanged.connect(self._filter)
        head.addWidget(self.search)
        lay.addLayout(head)

        self.list = QListWidget(); lay.addWidget(self.list, 1)
        self._entries: List[Entry] = []

    def refresh(self, entries: List[Entry]):
        self._entries = entries
        self._render()

    def _render(self):
        self.list.clear()
        counts = {}
        for e in self._entries:
            key = e.speaker or e.block_id or ('strings' if e.kind == 'string' else e.kind)
            counts[key] = counts.get(key, 0) + 1
        q = (self.search.text() or '').strip().lower()
        for k, v in sorted(counts.items(), key=lambda x: -x[1])[:5000]:
            if q and q not in str(k).lower(): continue
            self.list.addItem(f"{k}   ×{v}")

    def _filter(self, _): self._render()


# ---------------------------------------------------------------------------
# TAB -- Clases (con filtro)
# ---------------------------------------------------------------------------
class ClasesTab(QWidget):
    def __init__(self, main):
        super().__init__(); self.main = main
        lay = QVBoxLayout(self); lay.setContentsMargins(30, 14, 30, 22); lay.setSpacing(10)

        head = QHBoxLayout()
        head.addWidget(QLabel("Clasificación por categoría", objectName="Section"))
        head.addStretch()
        self.search = QLineEdit(); self.search.setPlaceholderText("Filtrar…")
        self.search.setMaximumWidth(240); self.search.textChanged.connect(self._filter)
        head.addWidget(self.search)
        lay.addLayout(head)

        row = QHBoxLayout(); row.setSpacing(20)
        self.cat_list = QListWidget(); self.cat_list.setMaximumWidth(280)
        row.addWidget(self.cat_list)
        self.kind_list = QListWidget()
        row.addWidget(self.kind_list, 1)
        lay.addLayout(row, 1)
        self._entries: List[Entry] = []

    def refresh(self, entries: List[Entry]):
        self._entries = entries; self._render()

    def _render(self):
        self.cat_list.clear(); self.kind_list.clear()
        cats = {}; kinds = {}
        for e in self._entries:
            cats[e.category] = cats.get(e.category, 0) + 1
            kinds[e.kind] = kinds.get(e.kind, 0) + 1
        q = (self.search.text() or '').strip().lower()
        for k, v in sorted(cats.items(), key=lambda x: -x[1]):
            if q and q not in str(k).lower(): continue
            icon = {'phone': '📱', 'menu': '🧭', 'dialogue': '💬', 'raw': '🔤'}.get(k, '·')
            self.cat_list.addItem(f"{icon}  {k}   ×{v}")
        for k, v in sorted(kinds.items(), key=lambda x: -x[1]):
            if q and q not in str(k).lower(): continue
            self.kind_list.addItem(f"{k}   ×{v}")

    def _filter(self, _): self._render()


# ---------------------------------------------------------------------------
# TAB -- Herramientas
# ---------------------------------------------------------------------------
class HerramientasTab(QWidget):
    def __init__(self, main):
        super().__init__(); self.main = main
        lay = QGridLayout(self); lay.setContentsMargins(30, 14, 30, 22)
        lay.setHorizontalSpacing(12); lay.setVerticalSpacing(12)

        lay.addWidget(QLabel("Utilidades", objectName="Section"), 0, 0, 1, 2)

        btns = [
            ("Exportar JSON de entradas",       "Guardar entries como JSON para inspección.", self.tool_extract),
            ("Limpiar caché (memoria + disco)", "Borra el caché LRU, el archivo en disco y nombres protegidos.", self.tool_clear_cache),
            ("Mostrar tamaño de caché",         "Reporta entradas en memoria y disco.", self.tool_cache_size),
            ("Re-escanear carpeta del juego",   "Ejecuta el escaneo de nuevo (Ctrl+R).", self.tool_rescan),
            ("Generar screens.rpy (fix GUI)",   "Crea el archivo de UI traducido.", self.tool_gen_screens),
            ("Generar replaceText.rpy",         "Crea el runtime de reemplazo de texto.", self.tool_gen_replace),
            ("Abrir carpeta del juego",         "Abre game/ en el explorador.", self.tool_open_game),
            ("Reset estadísticas",              "Pone los contadores del motor a 0.", self.tool_reset_stats),
        ]
        for i, (txt, tip, fn) in enumerate(btns):
            b = QPushButton(txt); b.setToolTip(tip); b.setMinimumHeight(40)
            b.clicked.connect(fn)
            lay.addWidget(b, 1 + i // 2, i % 2)

        sep = QFrame(); sep.setObjectName("Divider")
        lay.addWidget(sep, 1 + (len(btns)+1)//2 + 1, 0, 1, 2)

        row = 1 + (len(btns)+1)//2 + 2
        lay.addWidget(QLabel("Selector de idioma en-juego", objectName="Section"), row, 0, 1, 2); row += 1

        info = QLabel(
            "Genera tl_language_selector.rpy en game/ — añade un botón 🌐 en la esquina\n"
            "para cambiar idioma sin salir del juego. Para desinstalar: borra el .rpy.")
        info.setWordWrap(True)
        lay.addWidget(info, row, 0, 1, 2); row += 1

        pos_row = QHBoxLayout()
        pos_row.addWidget(QLabel("Posición:"))
        self.pos_combo = QComboBox()
        for label, key in (("Esquina inferior derecha", "bottom_right"),
                           ("Esquina inferior izquierda", "bottom_left"),
                           ("Esquina superior derecha",  "top_right"),
                           ("Esquina superior izquierda","top_left")):
            self.pos_combo.addItem(label, key)
        pos_row.addWidget(self.pos_combo, 1)
        pw = QWidget(); pw.setLayout(pos_row)
        lay.addWidget(pw, row, 0, 1, 2); row += 1

        b_sel = QPushButton("Generar selector de idioma"); b_sel.setObjectName("primary")
        b_sel.setMinimumHeight(44); b_sel.clicked.connect(self.tool_generate_selector)
        lay.addWidget(b_sel, row, 0, 1, 2); row += 1

        lay.setRowStretch(row, 1)

    def _info(self, t): QMessageBox.information(self, APP_NAME, t)

    def tool_extract(self):
        if not self.main.entries: self._info("Importa un proyecto primero."); return
        path, _ = QFileDialog.getSaveFileName(self, "Guardar JSON", "extract.json", "JSON (*.json)")
        if not path: return
        Path(path).write_text(entries_to_json(self.main.entries), encoding='utf-8')
        self._info(f"Exportadas {len(self.main.entries)} entradas.")

    def tool_clear_cache(self):
        if QMessageBox.question(self, APP_NAME,
            "¿Limpiar TODO?\n- LRU en memoria\n- Archivo en disco\n- Nombres protegidos") != QMessageBox.StandardButton.Yes:
            return
        try:
            CACHE.clear_all()
            clear_character_names()
            stats_reset()
            self._info("Caché y nombres protegidos limpiados.")
            self.main.log("🧹 Caché LRU + disco + nombres protegidos limpiados.")
        except Exception as e:
            self._info(f"Error: {e}")

    def tool_cache_size(self):
        from translator_engines import CACHE_PATH
        disk, mem = CACHE.size()
        size = CACHE_PATH.stat().st_size if CACHE_PATH.exists() else 0
        self._info(f"Caché:\n· Disco : {disk:,} entradas ({size/1024:.1f} KB)\n"
                   f"· Memoria: {mem:,} entradas\n\nRuta: {CACHE_PATH}".replace(',', '.'))

    def tool_rescan(self):
        self.main.tabs.setCurrentIndex(0); self.main.tab_trad.scan()

    def tool_gen_screens(self):
        if not self.main.game_dir: self._info("Importa un proyecto primero."); return
        try:
            path = generate_screens_rpy(self.main.game_dir,
                                        self.main.config.get("tl_name", "spanish_latino"))
            self.main.log(f"🖥  screens.rpy → {path}")
            self._info(f"✅ screens.rpy:\n{path}")
        except Exception as e:
            QMessageBox.critical(self, APP_NAME, f"Error:\n{e}")

    def tool_gen_replace(self):
        if not self.main.game_dir: self._info("Importa un proyecto primero."); return
        try:
            path = generate_replace_text(self.main.game_dir,
                                         self.main.config.get("tl_name", "spanish_latino"),
                                         self.main.entries or [])
            self.main.log(f"🔁 replaceText.rpy → {path}")
            self._info(f"✅ replaceText.rpy:\n{path}")
        except Exception as e:
            QMessageBox.critical(self, APP_NAME, f"Error:\n{e}")

    def tool_open_game(self):
        if not self.main.game_dir:
            self._info("No hay carpeta game/ detectada."); return
        QDesktopServices.openUrl(QUrl.fromLocalFile(self.main.game_dir))

    def tool_reset_stats(self):
        stats_reset(); self._info("Contadores reseteados.")

    def tool_generate_selector(self):
        if not self.main.game_dir:
            QMessageBox.warning(self, APP_NAME, "Primero importa un proyecto."); return
        try:
            position = self.pos_combo.currentData()
            self.main.config["selector_position"] = position; save_config(self.main.config)
            out_path = generate_language_selector(
                game_dir=self.main.game_dir,
                tl_lang=self.main.config.get("tl_name", "spanish_latino"),
                position=position)
            self.main.log(f"🌐 Selector → {out_path}")
            self._info(f"✅ Selector generado:\n{out_path}")
        except Exception as e:
            QMessageBox.critical(self, APP_NAME, f"Error:\n{e}")


# ---------------------------------------------------------------------------
# TAB -- Registro (con filtro y truncado)
# ---------------------------------------------------------------------------
class RegistroTab(QWidget):
    def __init__(self, main):
        super().__init__(); self.main = main
        lay = QVBoxLayout(self); lay.setContentsMargins(30, 14, 30, 22); lay.setSpacing(8)
        head = QHBoxLayout()
        head.addWidget(QLabel("Registro en vivo", objectName="Section"))
        head.addStretch()

        self.search = QLineEdit(); self.search.setPlaceholderText("Filtrar texto…")
        self.search.setMaximumWidth(220); self.search.textChanged.connect(self._apply_filter)
        head.addWidget(self.search)

        b_clear = QPushButton("Limpiar"); b_clear.clicked.connect(lambda: self.log.clear())
        head.addWidget(b_clear)
        b_export = QPushButton("Exportar"); b_export.clicked.connect(self.export_log)
        head.addWidget(b_export)
        lay.addLayout(head)

        self.log = QPlainTextEdit(); self.log.setReadOnly(True)
        max_lines = int(main.config.get("log_max_lines", 5000) or 5000)
        self.log.setMaximumBlockCount(max_lines)
        lay.addWidget(self.log, 1)

    def append(self, msg: str):
        # Aplicar filtro al insertar
        f = self.search.text().strip().lower() if self.search else ''
        if f and f not in msg.lower(): return
        self.log.appendPlainText(msg)

    def _apply_filter(self, _):
        pass  # solo filtra a futuro, no reescribe historico (mas barato)

    def export_log(self):
        path, _ = QFileDialog.getSaveFileName(self, "Guardar log", "renpy_translator.log",
                                              "Log (*.log);;Texto (*.txt)")
        if not path: return
        Path(path).write_text(self.log.toPlainText(), encoding='utf-8')
        QMessageBox.information(self, APP_NAME, "Log exportado.")


# ---------------------------------------------------------------------------
# TAB -- Ajustes
# ---------------------------------------------------------------------------
class AjustesTab(QWidget):
    def __init__(self, main):
        super().__init__(); self.main = main
        cfg = main.config
        lay = QGridLayout(self); lay.setContentsMargins(30, 14, 30, 22)
        lay.setHorizontalSpacing(14); lay.setVerticalSpacing(10)
        row = 0

        lay.addWidget(QLabel("Idioma destino (carpeta tl/)", objectName="Section"), row, 0)
        self.tl_name = QLineEdit(cfg.get("tl_name", "spanish_latino"))
        self.tl_name.setToolTip("Nombre de la carpeta tl/<idioma>/ a generar")
        lay.addWidget(self.tl_name, row, 1); row += 1

        lay.addWidget(QLabel("Motor preferido", objectName="Section"), row, 0)
        self.engine = QComboBox(); self.engine.setToolTip("Motor principal de traducción")
        self.engine.addItem("Google Translate (gratis)", "google")
        self.engine.addItem("DeepLX", "deeplx")
        idx = 1 if cfg.get("engine") == "deeplx" else 0
        self.engine.setCurrentIndex(idx)
        lay.addWidget(self.engine, row, 1); row += 1

        lay.addWidget(QLabel("Endpoint DeepLX", objectName="Section"), row, 0)
        self.deeplx = QLineEdit(cfg.get("deeplx_endpoint", DEFAULT_DEEPLX))
        self.deeplx.setToolTip("URL del servicio DeepLX (público o propio)")
        lay.addWidget(self.deeplx, row, 1); row += 1

        lay.addWidget(QLabel("Hilos por defecto", objectName="Section"), row, 0)
        self.workers = QSpinBox(); self.workers.setRange(1, 64)
        self.workers.setValue(int(cfg.get("workers", 16)))
        lay.addWidget(self.workers, row, 1); row += 1

        lay.addWidget(QLabel("Namespace de caché", objectName="Section"), row, 0)
        self.ns = QLineEdit(cfg.get("cache_namespace", ""))
        self.ns.setPlaceholderText("(automático: nombre del juego)")
        self.ns.setToolTip("Aísla el caché entre juegos. Vacío = automático por carpeta.")
        lay.addWidget(self.ns, row, 1); row += 1

        lay.addWidget(QLabel("Líneas máx. del log", objectName="Section"), row, 0)
        self.log_max = QSpinBox(); self.log_max.setRange(500, 100000)
        self.log_max.setValue(int(cfg.get("log_max_lines", 5000)))
        lay.addWidget(self.log_max, row, 1); row += 1

        self.cb_fallback = QCheckBox("Usar el otro motor como fallback automático")
        self.cb_fallback.setChecked(cfg.get("fallback_google", True))
        lay.addWidget(self.cb_fallback, row, 0, 1, 2); row += 1

        self.cb_phone_prio = QCheckBox("Priorizar mensajes de teléfono y menús (AVN)")
        self.cb_phone_prio.setChecked(cfg.get("phone_priority", True))
        lay.addWidget(self.cb_phone_prio, row, 0, 1, 2); row += 1

        self.cb_resume = QCheckBox("Autoguardar sesión (reanudable)")
        self.cb_resume.setChecked(cfg.get("auto_resume", True))
        lay.addWidget(self.cb_resume, row, 0, 1, 2); row += 1

        # Nombres protegidos
        sep = QFrame(); sep.setObjectName("Divider")
        lay.addWidget(sep, row, 0, 1, 2); row += 1
        lay.addWidget(QLabel("Nombres protegidos (no se traducen)", objectName="Section"), row, 0, 1, 2); row += 1
        self.protected_edit = QTextEdit()
        self.protected_edit.setToolTip("Un nombre por línea. Se aplican como tokens durante la traducción.")
        self.protected_edit.setPlaceholderText("Mike\nMelisa\nKim\n…")
        self.protected_edit.setPlainText("\n".join(get_protected_names()))
        self.protected_edit.setMaximumHeight(120)
        lay.addWidget(self.protected_edit, row, 0, 1, 2); row += 1

        prot_btns = QHBoxLayout()
        b_apply_prot = QPushButton("Aplicar nombres"); b_apply_prot.clicked.connect(self._apply_protected)
        prot_btns.addWidget(b_apply_prot)
        b_clear_prot = QPushButton("Vaciar"); b_clear_prot.setObjectName("danger")
        b_clear_prot.clicked.connect(self._clear_protected)
        prot_btns.addWidget(b_clear_prot)
        prot_btns.addStretch()
        pw = QWidget(); pw.setLayout(prot_btns); lay.addWidget(pw, row, 0, 1, 2); row += 1

        tips = QLabel(
            "Tips de velocidad y calidad\n"
            "• Google Translate gratis = más rápido y estable.\n"
            "• DeepLX público puede limitar; uno propio (Vercel/Railway) es ideal.\n"
            "• 16 hilos buen balance. Sube a 24-32 con DeepLX propio.\n"
            "• Usa el Glosario para forzar términos clave (nombres de objetos, lugares).\n"
            "• Caché por-proyecto evita contaminación entre juegos."
        )
        tips.setObjectName("Small"); tips.setWordWrap(True)
        lay.addWidget(tips, row, 0, 1, 2); row += 1

        btns = QHBoxLayout()
        b_save = QPushButton("Guardar"); b_save.setObjectName("primary"); b_save.setMinimumHeight(40)
        b_save.clicked.connect(self.save)
        b_reset = QPushButton("Restaurar"); b_reset.clicked.connect(self.reset)
        btns.addWidget(b_save); btns.addWidget(b_reset); btns.addStretch()
        w = QWidget(); w.setLayout(btns); lay.addWidget(w, row, 0, 1, 2); row += 1
        lay.setRowStretch(row, 1)

    def _apply_protected(self):
        names = [l.strip() for l in self.protected_edit.toPlainText().splitlines() if l.strip()]
        clear_character_names()
        if names:
            register_character_names(names)
        self.main.log(f"🛡 Nombres protegidos: {len(names)}")

    def _clear_protected(self):
        self.protected_edit.clear()
        clear_character_names()
        self.main.log("🛡 Nombres protegidos: 0")

    def save(self):
        cfg = self.main.config
        cfg["tl_name"]         = self.tl_name.text().strip() or "spanish_latino"
        cfg["engine"]          = self.engine.currentData()
        cfg["deeplx_endpoint"] = self.deeplx.text().strip() or DEFAULT_DEEPLX
        cfg["fallback_google"] = self.cb_fallback.isChecked()
        cfg["phone_priority"]  = self.cb_phone_prio.isChecked()
        cfg["auto_resume"]     = self.cb_resume.isChecked()
        cfg["workers"]         = int(self.workers.value())
        cfg["cache_namespace"] = self.ns.text().strip()
        cfg["log_max_lines"]   = int(self.log_max.value())
        save_config(cfg)
        set_cache_namespace(cfg["cache_namespace"])
        self.main.tab_log.log.setMaximumBlockCount(cfg["log_max_lines"])
        self._apply_protected()
        QMessageBox.information(self, APP_NAME, "Configuración guardada.")

    def reset(self):
        if CONFIG_PATH.exists(): CONFIG_PATH.unlink()
        self.main.config = load_config()
        c = self.main.config
        self.tl_name.setText(c["tl_name"])
        self.engine.setCurrentIndex(0 if c["engine"] == "google" else 1)
        self.deeplx.setText(c["deeplx_endpoint"])
        self.workers.setValue(int(c["workers"]))
        self.cb_fallback.setChecked(c["fallback_google"])
        self.cb_phone_prio.setChecked(c["phone_priority"])
        self.cb_resume.setChecked(c["auto_resume"])
        self.ns.setText(c["cache_namespace"])
        self.log_max.setValue(int(c["log_max_lines"]))
        QMessageBox.information(self, APP_NAME, "Configuración restaurada.")


# ---------------------------------------------------------------------------
# Title bar (frameless)
# ---------------------------------------------------------------------------
class TitleBar(QWidget):
    def __init__(self, parent: 'MainWindow'):
        super().__init__(parent)
        self.parent_win = parent
        self.setObjectName("TitleBar")
        self.setFixedHeight(50)
        self._drag_offset: Optional[QPoint] = None

        lay = QHBoxLayout(self); lay.setContentsMargins(16, 8, 12, 8); lay.setSpacing(10)

        avatar = QLabel(); avatar.setPixmap(make_mascot(34))
        lay.addWidget(avatar)

        title = QLabel(f"{APP_NAME} v{APP_VERSION}"); title.setObjectName("TitleLabel")
        lay.addWidget(title)
        lay.addStretch(1)

        # Boton "?" (About)
        b_help = QPushButton("?"); b_help.setObjectName("WinBtn")
        b_help.setFixedSize(30, 26); b_help.setToolTip("Acerca de (F1)")
        b_help.setCursor(Qt.CursorShape.PointingHandCursor)
        b_help.clicked.connect(parent.show_about)
        lay.addWidget(b_help)

        for txt, slot, obj in (
            ("—", parent.showMinimized, "WinBtn"),
            ("☐", self._toggle_max, "WinBtn"),
            ("✕", parent.close, "WinClose"),
        ):
            b = QPushButton(txt); b.setObjectName(obj); b.setFixedSize(34, 26)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.clicked.connect(slot)
            lay.addWidget(b)

    def _toggle_max(self):
        if self.parent_win.isMaximized(): self.parent_win.showNormal()
        else: self.parent_win.showMaximized()

    def mousePressEvent(self, e: QMouseEvent):
        if e.button() == Qt.MouseButton.LeftButton:
            self._drag_offset = e.globalPosition().toPoint() - self.parent_win.frameGeometry().topLeft()

    def mouseMoveEvent(self, e: QMouseEvent):
        if self._drag_offset is None: return
        if not (e.buttons() & Qt.MouseButton.LeftButton): return
        if self.parent_win.isMaximized():
            # restaurar y recalibrar offset para que la ventana siga al cursor
            self.parent_win.showNormal()
            w = self.parent_win.width()
            self._drag_offset = QPoint(w // 2, 18)
        self.parent_win.move(e.globalPosition().toPoint() - self._drag_offset)

    def mouseReleaseEvent(self, e: QMouseEvent):
        self._drag_offset = None

    def mouseDoubleClickEvent(self, e: QMouseEvent):
        self._toggle_max()


# ---------------------------------------------------------------------------
# MainWindow
# ---------------------------------------------------------------------------
class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(APP_NAME)
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.Window)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.resize(1180, 740); self.setMinimumSize(960, 600)
        self.setAcceptDrops(True)

        self.config = load_config()
        self.entries: List[Entry] = []
        self.project_root: str = ""
        self.game_dir: Optional[str] = None

        root = QWidget(); root.setObjectName("Root")
        self.setCentralWidget(root)

        v = QVBoxLayout(root); v.setContentsMargins(2, 2, 2, 2); v.setSpacing(0)

        self.title_bar = TitleBar(self)
        v.addWidget(self.title_bar)

        self.tabs = QTabWidget(); self.tabs.setDocumentMode(True)
        self.tabs.tabBar().setExpanding(False)
        self.tab_trad   = TraduccionTab(self);    self.tabs.addTab(self.tab_trad,    "Traducción")
        self.tab_gloss  = GlosarioTab(self);      self.tabs.addTab(self.tab_gloss,   "Glosario")
        self.tab_stats  = EstadisticasTab(self);  self.tabs.addTab(self.tab_stats,   "Estadísticas")
        self.tab_funcs  = FuncionesTab(self);     self.tabs.addTab(self.tab_funcs,   "Funciones")
        self.tab_clases = ClasesTab(self);        self.tabs.addTab(self.tab_clases,  "Clases")
        self.tab_tools  = HerramientasTab(self);  self.tabs.addTab(self.tab_tools,   "Herramientas")
        self.tab_log    = RegistroTab(self);      self.tabs.addTab(self.tab_log,     "Registro")
        self.tab_settings = AjustesTab(self);     self.tabs.addTab(self.tab_settings,"Ajustes")
        v.addWidget(self.tabs, 1)

        # Sombra solo en root (en vez de en 3 widgets)
        eff = QGraphicsDropShadowEffect(self)
        eff.setBlurRadius(40); eff.setColor(QColor(0, 0, 0, 200)); eff.setOffset(0, 6)
        root.setGraphicsEffect(eff)

        # Atajos
        self._make_shortcuts()

        # Aplicar namespace de cache de la config
        try:
            ns = self.config.get("cache_namespace", "") or ""
            if ns: set_cache_namespace(ns)
        except Exception:
            pass

        # Autoresume al iniciar (si esta habilitado y hay sesion reciente)
        if self.config.get("auto_resume", True):
            QTimer.singleShot(200, self._maybe_offer_resume)

    def _make_shortcuts(self):
        def make(seq, fn):
            sc = QShortcut(QKeySequence(seq), self); sc.activated.connect(fn)
            return sc
        make("Ctrl+O", self.tab_trad.browse_dir)
        make("Ctrl+R", self.tab_trad.scan)
        make("Ctrl+T", self.tab_trad.translate_all)
        make("Ctrl+E", self.tab_trad.export)
        make("Esc",    self.tab_trad.stop)
        make("F1",     self.show_about)
        make("F5",     lambda: self.tabs.setCurrentIndex(0))
        make("Ctrl+G", lambda: self.tabs.setCurrentIndex(1))
        make("Ctrl+L", lambda: self.tabs.setCurrentIndex(6))
        make("Ctrl+,", lambda: self.tabs.setCurrentIndex(7))
        make("Ctrl+W", self.close)

    def log(self, msg: str):
        self.tab_log.append(f"[{time.strftime('%H:%M:%S')}] {msg}")

    def refresh_all(self):
        self.tab_funcs.refresh(self.entries)
        self.tab_clases.refresh(self.entries)

    # ---- Drag & drop a nivel ventana ----
    def dragEnterEvent(self, e: QDragEnterEvent):
        if e.mimeData().hasUrls(): e.acceptProposedAction()
        else: e.ignore()

    def dropEvent(self, e: QDropEvent):
        for u in e.mimeData().urls():
            if u.isLocalFile():
                p = u.toLocalFile()
                self.tab_trad.path_input.setText(p)
                self.tab_trad._auto_detect()
                self.tabs.setCurrentIndex(0)
                break

    # ---- Sesion ----
    def save_session_async(self):
        if not self.config.get("auto_resume", True):
            return
        try:
            data = {
                "saved_at": time.time(),
                "path":     self.tab_trad.path_input.text(),
                "game_dir": self.game_dir or '',
                "entries":  [e.__dict__ for e in (self.entries or [])],
            }
            payload = json.dumps(data, ensure_ascii=False)
            # escribir en hilo para no bloquear
            def _w():
                try: SESSION_PATH.write_text(payload, encoding='utf-8')
                except Exception as e: print('session save err:', e)
            import threading
            threading.Thread(target=_w, daemon=True).start()
        except Exception:
            pass

    def _maybe_offer_resume(self):
        if not SESSION_PATH.exists(): return
        try:
            data = json.loads(SESSION_PATH.read_text(encoding='utf-8'))
            saved = data.get('saved_at', 0)
            age_h = (time.time() - saved) / 3600
            n = len(data.get('entries', []))
            if n == 0 or age_h > 168:  # > 1 semana, ignorar
                return
            reply = QMessageBox.question(
                self, APP_NAME,
                f"¿Reanudar la última sesión?\n"
                f"{n:,} entradas guardadas hace {age_h:.1f}h\n"
                f"Ruta: {data.get('path','')}".replace(',', '.'))
            if reply == QMessageBox.StandardButton.Yes:
                self.tab_trad.resume_session()
        except Exception:
            pass

    # ---- About ----
    def show_about(self):
        QMessageBox.about(self, APP_NAME,
            f"<b>{APP_NAME} {APP_VERSION}</b><br>"
            f"Traductor de proyectos Ren'Py con motor Google/DeepLX, glosario,<br>"
            f"protección de nombres y caché en disco.<br><br>"
            f"<b>Atajos</b><br>"
            f"Ctrl+O — Abrir carpeta · Ctrl+R — Escanear · Ctrl+T — Traducir<br>"
            f"Ctrl+E — Exportar · Esc — Detener · F1 — Acerca de<br>"
            f"Ctrl+G — Glosario · Ctrl+L — Registro · Ctrl+, — Ajustes<br><br>"
            f"<i>dev by xav</i>")


# ---------------------------------------------------------------------------
def main():
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setStyleSheet(QSS)
    app.setFont(QFont("Segoe UI", 10))
    # Icono de aplicacion si esta presente
    here = Path(__file__).resolve().parent
    for ico in ("Eagler.ico", "eagler.ico", "eagle.ico"):
        p = here / ico
        if p.exists():
            app.setWindowIcon(QIcon(str(p))); break
    w = MainWindow(); w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
