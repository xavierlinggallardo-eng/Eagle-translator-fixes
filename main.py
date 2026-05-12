"""
Ren'Py Translator — GUI PyQt6  v2.1
Rediseño visual estilo "Zenpy dark cyan" (frameless, acento turquesa).
Toda la lógica de escaneo / traducción / exportación se conserva intacta
y usa renpy_parser.py + translator_engines.py (sin modificarlos).
"""
from __future__ import annotations
import os, sys, json, traceback, time
from pathlib import Path
from typing import List, Optional

from PyQt6.QtCore import Qt, QThread, pyqtSignal, QPoint, QSize
from PyQt6.QtGui import (
    QIcon, QColor, QFont, QPixmap, QPainter, QBrush, QPen, QMouseEvent,
)
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QGridLayout,
    QLabel, QLineEdit, QPushButton, QComboBox, QCheckBox, QProgressBar,
    QTabWidget, QFileDialog, QPlainTextEdit, QListWidget, QListWidgetItem,
    QMessageBox, QSpinBox, QSlider, QFrame, QSizePolicy, QGraphicsDropShadowEffect,
    QStackedWidget,
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
from translator_engines import translate_batch, DEFAULT_DEEPLX, CACHE

APP_NAME = "Ren'Py Translator"
APP_VERSION = "2.1.0"
CONFIG_PATH = Path.home() / ".renpy_translator_config.json"

# ---------------------------------------------------------------------------
# Paleta y QSS
# ---------------------------------------------------------------------------
COL_BG       = "#0f1419"
COL_BG2      = "#121821"
COL_PANEL    = "#161d27"
COL_PANEL2   = "#1b2330"
COL_BORDER   = "#222b38"
COL_BORDER2  = "#2a3445"
COL_TEXT     = "#e6edf3"
COL_DIM      = "#8b97a8"
COL_MUTED    = "#5b6678"
COL_ACCENT   = "#29e0d4"
COL_ACCENT2  = "#4cf0e6"

QSS = f"""
* {{ outline: none; }}
QWidget {{
    background: {COL_BG};
    color: {COL_TEXT};
    font-family: 'Segoe UI', 'Inter', 'Helvetica Neue', sans-serif;
    font-size: 10pt;
}}
#Root {{
    background: {COL_BG2};
    border: 1px solid {COL_BORDER2};
    border-radius: 14px;
}}
#TitleBar {{
    background: transparent;
    border: none;
}}
#TitleLabel {{
    color: {COL_TEXT};
    font-size: 11pt;
    font-weight: 600;
    letter-spacing: 0.3px;
}}
#WinBtn {{
    background: transparent;
    color: {COL_DIM};
    border: none;
    border-radius: 6px;
    padding: 4px 10px;
    font-size: 12pt;
}}
#WinBtn:hover {{ background: {COL_PANEL2}; color: {COL_TEXT}; }}
#WinClose:hover {{ background: #b3261e; color: white; }}

/* ----- Tabs (estilo underline) ----- */
QTabWidget::pane {{ border: none; background: transparent; top: 0; }}
QTabBar {{ background: transparent; qproperty-drawBase: 0; }}
QTabBar::tab {{
    background: transparent;
    color: {COL_DIM};
    padding: 12px 22px;
    margin: 0 4px;
    border: none;
    font-size: 11pt;
    font-weight: 500;
}}
QTabBar::tab:hover {{ color: {COL_TEXT}; }}
QTabBar::tab:selected {{
    color: {COL_ACCENT};
    border-bottom: 2px solid {COL_ACCENT};
    font-weight: 600;
}}

/* ----- Inputs ----- */
QLineEdit, QPlainTextEdit, QComboBox, QSpinBox {{
    background: {COL_PANEL};
    color: {COL_TEXT};
    border: 1px solid {COL_BORDER2};
    border-radius: 8px;
    padding: 9px 12px;
    selection-background-color: {COL_ACCENT};
    selection-color: #000;
}}
QLineEdit:focus, QComboBox:focus, QSpinBox:focus, QPlainTextEdit:focus {{
    border: 1px solid {COL_ACCENT};
}}
QComboBox::drop-down {{ border: none; width: 28px; }}
QComboBox::down-arrow {{
    image: none;
    border-left: 5px solid transparent;
    border-right: 5px solid transparent;
    border-top: 6px solid {COL_DIM};
    margin-right: 10px;
}}
QComboBox QAbstractItemView {{
    background: {COL_PANEL};
    color: {COL_TEXT};
    border: 1px solid {COL_BORDER2};
    selection-background-color: {COL_ACCENT};
    selection-color: #000;
    padding: 4px;
    outline: none;
}}

/* ----- Buttons ----- */
QPushButton {{
    background: transparent;
    color: {COL_TEXT};
    border: 1px solid {COL_BORDER2};
    border-radius: 8px;
    padding: 11px 22px;
    font-weight: 500;
}}
QPushButton:hover {{ border-color: {COL_ACCENT}; color: {COL_ACCENT}; }}
QPushButton:pressed {{ background: {COL_PANEL2}; }}
QPushButton:disabled {{ color: {COL_MUTED}; border-color: {COL_BORDER}; }}

QPushButton#primary {{
    background: {COL_ACCENT};
    color: #001417;
    border: 1px solid {COL_ACCENT};
    font-weight: 700;
    letter-spacing: 1.5px;
}}
QPushButton#primary:hover {{ background: {COL_ACCENT2}; border-color: {COL_ACCENT2}; }}
QPushButton#primary:disabled {{ background: {COL_PANEL2}; color: {COL_MUTED}; border-color: {COL_BORDER2}; }}

QPushButton#ghost {{
    background: transparent; border: 1px solid {COL_BORDER2}; padding: 9px 18px;
}}

/* ----- Labels semánticos ----- */
QLabel#H1 {{ font-size: 32pt; font-weight: 700; color: {COL_TEXT}; }}
QLabel#Section {{ color: {COL_DIM}; font-size: 9.5pt; font-weight: 500; }}
QLabel#Big   {{ font-size: 44pt; font-weight: 700; color: {COL_TEXT}; }}
QLabel#Small {{ color: {COL_DIM}; font-size: 9pt; }}
QLabel#Detected {{ color: {COL_ACCENT}; font-size: 9pt; }}

/* ----- Checkbox ----- */
QCheckBox {{ color: {COL_TEXT}; spacing: 8px; }}
QCheckBox::indicator {{
    width: 18px; height: 18px;
    border: 1.5px solid {COL_BORDER2};
    border-radius: 4px;
    background: {COL_PANEL};
}}
QCheckBox::indicator:hover {{ border-color: {COL_ACCENT}; }}
QCheckBox::indicator:checked {{
    background: {COL_ACCENT}; border-color: {COL_ACCENT};
    image: none;
}}

/* ----- Slider ----- */
QSlider::groove:horizontal {{
    height: 6px; background: {COL_PANEL2}; border-radius: 3px;
}}
QSlider::sub-page:horizontal {{ background: {COL_ACCENT}; border-radius: 3px; }}
QSlider::handle:horizontal {{
    background: {COL_ACCENT};
    width: 18px; height: 18px;
    margin: -7px 0;
    border-radius: 9px;
    border: 2px solid {COL_BG2};
}}
QSlider::handle:horizontal:hover {{ background: {COL_ACCENT2}; }}

/* ----- ProgressBar ----- */
QProgressBar {{
    background: {COL_PANEL};
    border: 1px solid {COL_BORDER2};
    border-radius: 6px;
    text-align: center;
    color: {COL_TEXT};
    height: 14px;
    font-size: 8.5pt;
}}
QProgressBar::chunk {{
    background: {COL_ACCENT};
    border-radius: 5px;
}}

/* ----- Lists / log ----- */
QListWidget, QPlainTextEdit {{
    background: {COL_PANEL};
    border: 1px solid {COL_BORDER2};
    border-radius: 8px;
    padding: 6px;
}}
QListWidget::item {{ padding: 6px 8px; border-radius: 4px; }}
QListWidget::item:hover {{ background: {COL_PANEL2}; }}
QListWidget::item:selected {{ background: {COL_ACCENT}; color: #001417; }}

/* ----- Category panel ----- */
QPushButton#CatBtn {{
    background: transparent;
    color: {COL_DIM};
    border: none;
    border-radius: 8px;
    padding: 8px 14px;
    text-align: center;
    font-size: 11pt;
    font-weight: 500;
}}
QPushButton#CatBtn:hover {{ color: {COL_TEXT}; }}
QPushButton#CatBtn:checked {{
    background: {COL_PANEL2};
    color: {COL_TEXT};
    font-weight: 600;
}}

QFrame#Divider {{ background: {COL_BORDER}; max-height: 1px; min-height: 1px; border: none; }}
"""

# ---------------------------------------------------------------------------
# Idiomas
# ---------------------------------------------------------------------------
LANGS = [
    ("Detectar automáticamente", "auto"),
    ("English", "EN"),
    ("Spanish (Latam)", "ES-419"),
    ("Spanish (España)", "ES"),
    ("Spanish México", "ES-MX"),
    ("Portuguese (BR)", "PT-BR"),
    ("Portuguese (Portugal)", "PT-PT"),
    ("French", "FR"),
    ("German", "DE"),
    ("Italian", "IT"),
    ("Japanese", "JA"),
    ("Chinese", "ZH"),
    ("Korean", "KO"),
    ("Russian", "RU"),
]

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
def load_config() -> dict:
    if CONFIG_PATH.exists():
        try:
            return json.loads(CONFIG_PATH.read_text(encoding='utf-8'))
        except Exception:
            pass
    return {
        "deeplx_endpoint": DEFAULT_DEEPLX,
        "engine": "google",
        "fallback_google": True,
        "tl_name": "spanish_latino",
        "phone_priority": True,
        "workers": 16,
        "scan_mode": "source",
        "selector_position": "bottom_right",
        "source_lang": "auto",
        "target_lang": "ES-419",
    }

def save_config(cfg: dict):
    try:
        CONFIG_PATH.write_text(json.dumps(cfg, indent=2), encoding='utf-8')
    except Exception as e:
        print("config save err:", e)

# ---------------------------------------------------------------------------
# Worker
# ---------------------------------------------------------------------------
class TranslateWorker(QThread):
    progress = pyqtSignal(int, int, str)
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
            results = translate_batch(
                texts,
                source=self.source, target=self.target,
                engine=self.engine, deeplx_endpoint=self.deeplx_endpoint,
                fallback=self.fallback, workers=self.workers,
                progress_cb=lambda d, t: self.progress.emit(d, t, f"{d}/{t}"),
                stop_flag=lambda: self._stop,
            )
            for e, out in zip(todo, results):
                if out and out.strip():
                    e.translation = out

            failed_count = sum(1 for r in results if not r or not r.strip())
            dt = time.time() - t0
            speed = total / dt if dt > 0 else 0
            self.log.emit(f"⏱ Completado en {dt:.1f}s  ({speed:.1f} entradas/s)")
            if failed_count:
                self.log.emit(f"⚠ {failed_count} entradas no pudieron traducirse. Reintenta solo vacías.")
            self.finished_ok.emit(self.entries)
        except Exception as ex:
            self.failed.emit(f"{ex}\n{traceback.format_exc()}")


# ---------------------------------------------------------------------------
# Avatar mascota
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
# Stats panel derecho
# ---------------------------------------------------------------------------
class StatsPanel(QWidget):
    """Contador grande + lista de categorías (Diálogos/Teléfono/Menús)."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedWidth(220)
        v = QVBoxLayout(self); v.setContentsMargins(0, 6, 0, 6); v.setSpacing(2)

        v.addStretch(1)
        self.count = QLabel("0"); self.count.setObjectName("Big")
        self.count.setAlignment(Qt.AlignmentFlag.AlignCenter)
        v.addWidget(self.count)

        sub = QLabel("cadenas totales"); sub.setObjectName("Small")
        sub.setAlignment(Qt.AlignmentFlag.AlignCenter)
        v.addWidget(sub)

        v.addSpacing(24)

        self.btn_dialogue = self._make_cat("Diálogos")
        self.btn_phone    = self._make_cat("Teléfono")
        self.btn_menu     = self._make_cat("Menús")
        self.btn_raw      = self._make_cat("Raw")
        for b in (self.btn_dialogue, self.btn_phone, self.btn_menu, self.btn_raw):
            v.addWidget(b)
        self.btn_dialogue.setChecked(True)
        v.addStretch(2)

        self._counts = {"dialogue": 0, "phone": 0, "menu": 0, "raw": 0, "total": 0}

    def _make_cat(self, name: str) -> QPushButton:
        b = QPushButton(name); b.setObjectName("CatBtn"); b.setCheckable(True)
        b.setAutoExclusive(True); b.setCursor(Qt.CursorShape.PointingHandCursor)
        return b

    def set_counts(self, dialogue: int, phone: int, menu: int, raw: int, total: int):
        self._counts = {"dialogue": dialogue, "phone": phone, "menu": menu, "raw": raw, "total": total}
        self.count.setText(f"{total}")
        self.btn_dialogue.setText(f"Diálogos  ·  {dialogue}")
        self.btn_phone.setText(f"Teléfono  ·  {phone}")
        self.btn_menu.setText(f"Menús  ·  {menu}")
        self.btn_raw.setText(f"Raw  ·  {raw}")


# ---------------------------------------------------------------------------
# TAB 1: Traducción
# ---------------------------------------------------------------------------
class TraduccionTab(QWidget):
    def __init__(self, main):
        super().__init__()
        self.main = main
        cfg = main.config

        root = QHBoxLayout(self); root.setContentsMargins(34, 18, 34, 28); root.setSpacing(28)

        # ---------- Columna izquierda (form) ----------
        left = QVBoxLayout(); left.setSpacing(14)

        # Ruta
        left.addWidget(self._section("Ruta del juego"))
        path_row = QHBoxLayout(); path_row.setSpacing(10)
        self.path_input = QLineEdit()
        self.path_input.setPlaceholderText(r"C:\Games\MiJuego\game  ó  juego.exe")
        path_row.addWidget(self.path_input, 1)
        btn_browse = QPushButton("Buscar"); btn_browse.clicked.connect(self.browse)
        btn_browse.setMinimumWidth(110)
        path_row.addWidget(btn_browse)
        left.addLayout(path_row)
        self.detected = QLabel(""); self.detected.setObjectName("Detected")
        left.addWidget(self.detected)

        left.addSpacing(6)

        # Idiomas
        lang_row = QHBoxLayout(); lang_row.setSpacing(16)
        lcol_a = QVBoxLayout(); lcol_a.setSpacing(6)
        lcol_a.addWidget(self._section("Idioma original"))
        self.src = QComboBox()
        for n, c in LANGS: self.src.addItem(n, c)
        self._select_by_data(self.src, cfg.get("source_lang", "auto"))
        lcol_a.addWidget(self.src)
        lang_row.addLayout(lcol_a, 1)

        lcol_b = QVBoxLayout(); lcol_b.setSpacing(6)
        lcol_b.addWidget(self._section("Nuevo idioma"))
        self.dst = QComboBox()
        for n, c in LANGS[1:]: self.dst.addItem(n, c)
        self._select_by_data(self.dst, cfg.get("target_lang", "ES-419"))
        lcol_b.addWidget(self.dst)
        lang_row.addLayout(lcol_b, 1)
        left.addLayout(lang_row)

        left.addSpacing(8)

        # Modo de escaneo
        left.addWidget(self._section("Modo de escaneo"))
        self.scan_mode = QComboBox()
        self.scan_mode.addItem("Auto-detectar TODO (recomendado)", "source")
        self.scan_mode.addItem("Solo bloques translate existentes (tl/)", "tl_existing")
        self.scan_mode.addItem("Rellenar traducciones existentes (Fill Mode)", "fill_existing")
        self.scan_mode.addItem("Escribir directo en .rpy originales (InPlace)", "inplace")
        self.scan_mode.addItem("Rellenar archivos del SDK de Ren'Py (Fill SDK)", "fill_sdk")
        self._select_by_data(self.scan_mode, cfg.get("scan_mode", "source"))
        left.addWidget(self.scan_mode)

        # Hilos
        hil_lbl = QLabel("Hilos"); hil_lbl.setObjectName("Section")
        left.addWidget(hil_lbl)
        hil_row = QHBoxLayout(); hil_row.setSpacing(12)
        self.sl_workers = QSlider(Qt.Orientation.Horizontal)
        self.sl_workers.setRange(4, 48)
        self.sl_workers.setValue(int(cfg.get("workers", 16)))
        self.sl_workers.setSingleStep(1)
        hil_row.addWidget(self.sl_workers, 1)
        self.sl_workers_lbl = QLabel(str(self.sl_workers.value()))
        self.sl_workers_lbl.setFixedWidth(28)
        self.sl_workers.valueChanged.connect(lambda v: self.sl_workers_lbl.setText(str(v)))
        hil_row.addWidget(self.sl_workers_lbl)
        left.addLayout(hil_row)

        left.addSpacing(4)
        self.cb_phone = QCheckBox("Priorizar Teléfono / Menús")
        self.cb_phone.setChecked(cfg.get("phone_priority", True))
        left.addWidget(self.cb_phone)

        left.addSpacing(10)
        # Progress (oculto hasta usarse)
        self.progress = QProgressBar(); self.progress.setValue(0); self.progress.setVisible(False)
        left.addWidget(self.progress)

        # Botones inferiores
        btn_row = QHBoxLayout(); btn_row.setSpacing(14)
        self.btn_import = QPushButton("Importar / Escanear"); self.btn_import.setObjectName("ghost")
        self.btn_import.setMinimumHeight(46); self.btn_import.clicked.connect(self.scan)
        btn_row.addWidget(self.btn_import, 1)

        self.btn_translate = QPushButton("TRADUCIR"); self.btn_translate.setObjectName("primary")
        self.btn_translate.setMinimumHeight(46)
        self.btn_translate.setEnabled(False)
        self.btn_translate.clicked.connect(self.translate_all)
        self._add_glow(self.btn_translate)
        btn_row.addWidget(self.btn_translate, 1)
        left.addLayout(btn_row)

        # Acciones secundarias (stop / export)
        sec_row = QHBoxLayout(); sec_row.setSpacing(10)
        self.btn_stop = QPushButton("Detener"); self.btn_stop.setEnabled(False)
        self.btn_stop.clicked.connect(self.stop)
        sec_row.addWidget(self.btn_stop)
        self.btn_export = QPushButton("Exportar a game/tl/"); self.btn_export.setEnabled(False)
        self.btn_export.clicked.connect(self.export)
        sec_row.addWidget(self.btn_export)
        sec_row.addStretch()
        left.addLayout(sec_row)

        self.status = QLabel("Listo."); self.status.setObjectName("Small")
        left.addWidget(self.status)

        left.addStretch(1)
        root.addLayout(left, 1)

        # ---------- Columna derecha (stats) ----------
        self.stats = StatsPanel()
        right_wrap = QVBoxLayout(); right_wrap.setContentsMargins(0, 4, 0, 0)
        right_wrap.addWidget(self.stats); right_wrap.addStretch()
        root.addLayout(right_wrap, 0)

    # ---- helpers ----
    def _section(self, txt: str) -> QLabel:
        lb = QLabel(txt); lb.setObjectName("Section"); return lb

    def _select_by_data(self, combo: QComboBox, data):
        for i in range(combo.count()):
            if combo.itemData(i) == data:
                combo.setCurrentIndex(i); return

    def _add_glow(self, w: QWidget):
        eff = QGraphicsDropShadowEffect(self)
        eff.setBlurRadius(40); eff.setColor(QColor(41, 224, 212, 180))
        eff.setOffset(0, 0)
        w.setGraphicsEffect(eff)

    # ---- acciones ----
    def browse(self):
        # un solo botón: archivo o carpeta
        m = QMessageBox(self)
        m.setWindowTitle(APP_NAME)
        m.setText("¿Seleccionar el .exe del juego o una carpeta?")
        b_exe = m.addButton(".exe", QMessageBox.ButtonRole.AcceptRole)
        b_dir = m.addButton("Carpeta", QMessageBox.ButtonRole.AcceptRole)
        m.addButton("Cancelar", QMessageBox.ButtonRole.RejectRole)
        m.exec()
        clicked = m.clickedButton()
        if clicked is b_exe:
            f, _ = QFileDialog.getOpenFileName(self, "Selecciona el .exe del juego", "",
                                               "Ejecutables (*.exe);;Todos (*)")
            if f: self.path_input.setText(f); self._auto_detect()
        elif clicked is b_dir:
            d = QFileDialog.getExistingDirectory(self, "Selecciona la carpeta del juego")
            if d: self.path_input.setText(d); self._auto_detect()

    def _auto_detect(self):
        path = self.path_input.text().strip()
        gd = locate_game_dir(path) if path else None
        if gd:
            self.detected.setText(f"✓ game/ detectado: {gd}")
            self.main.game_dir = gd
        else:
            self.detected.setText("⚠ No se detectó carpeta game/. Usa una ruta válida.")
            self.main.game_dir = None

    def scan(self):
        path = self.path_input.text().strip()
        if not path or not os.path.exists(path):
            QMessageBox.warning(self, APP_NAME, "Ruta inválida."); return
        self._auto_detect()
        self.status.setText("Escaneando…")
        QApplication.processEvents()
        try:
            mode = self.scan_mode.currentData()
            if os.path.isfile(path) and path.endswith('.rpy'):
                base = os.path.dirname(path)
                entries = parse_file(path, base=base)
                self.main.project_root = base
                self.main.game_dir = base
            else:
                gd = self.main.game_dir or locate_game_dir(path)
                if not gd:
                    QMessageBox.warning(self, APP_NAME,
                        "No se encontró la carpeta 'game/' del proyecto Ren'Py.")
                    self.status.setText("Error: no se encontró game/."); return
                self.main.game_dir = gd
                self.main.project_root = gd

                if mode == "source":
                    entries = extract_source_directory(gd)
                    try:
                        known = {e.source for e in entries if e.source}
                        raw = extract_raw_strings_directory(gd, known_sources=known)
                        if raw:
                            entries.extend(raw)
                            self.main.log(f"🔤 Raw scan: +{len(raw)} strings universales")
                    except Exception as _re:
                        self.main.log(f"[raw extract] error: {_re}")
                elif mode == "fill_existing":
                    entries = []
                    for dirpath, _, files in os.walk(gd):
                        for fn in files:
                            if fn.endswith('.rpy') and not fn.endswith('.rpyc'):
                                full = os.path.join(dirpath, fn)
                                try: entries.extend(parse_and_fill_file(full, base=gd))
                                except Exception as e: print(f'[fill parse error] {full}: {e}')
                    self.main.log(f"Modo Fill: {len(entries)} entradas listas para rellenar.")
                elif mode == "fill_sdk":
                    tl_lang = self.main.config.get("tl_name", "spanish_latino")
                    sdk_dir = os.path.join(gd, 'tl', tl_lang)
                    if not os.path.isdir(sdk_dir):
                        sdk_dir = QFileDialog.getExistingDirectory(
                            self, f"Selecciona la carpeta tl/{tl_lang}/ generada por el SDK",
                            os.path.join(gd, 'tl'))
                        if not sdk_dir: self.status.setText("Cancelado."); return
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
                            self.main.log(
                                f"🧩 Fill SDK extra: +{len(extra_defines)} define + "
                                f"{len(extra_phone)} phone + {len(raw_entries)} raw")
                    except Exception as _sdk_ex:
                        self.main.log(f"[fill_sdk extras] error: {_sdk_ex}")
                    self.main._sdk_tl_dir = sdk_dir
                    if not entries:
                        QMessageBox.warning(self, APP_NAME,
                            f"No se encontraron líneas vacías en:\n{sdk_dir}")
                elif mode == "inplace":
                    tl_lang = self.main.config.get("tl_name", "spanish_latino")
                    entries = scan_inplace_directory(gd, tl_lang)
                    if not entries:
                        QMessageBox.warning(self, APP_NAME,
                            f"No se encontraron bloques 'translate {tl_lang}:' con líneas vacías.")
                else:
                    entries = parse_directory(gd)

            self.main.entries = entries
            ph = sum(1 for e in entries if e.category == 'phone')
            mn = sum(1 for e in entries if e.category == 'menu')
            dl = sum(1 for e in entries if e.category == 'dialogue')
            rw = sum(1 for e in entries if e.category == 'raw')
            self.stats.set_counts(dl, ph, mn, rw, len(entries))
            self.status.setText(f"Importado: {len(entries)} cadenas.")
            self.btn_translate.setEnabled(len(entries) > 0)
            self.btn_export.setEnabled(False)
            self.main.refresh_all()
            self.main.log(f"Escaneado [{mode}]: {len(entries)} (phone={ph}, menu={mn}, dialogue={dl})")
        except Exception as e:
            QMessageBox.critical(self, APP_NAME, f"Error: {e}\n{traceback.format_exc()}")
            self.status.setText("Error.")

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
        cfg["source_lang"] = src
        cfg["target_lang"] = tgt
        save_config(cfg)

        self.progress.setVisible(True); self.progress.setValue(0)
        self.worker = TranslateWorker(
            entries=entries, source=src, target=tgt,
            engine=cfg.get("engine", "google"),
            deeplx_endpoint=cfg.get("deeplx_endpoint", DEFAULT_DEEPLX),
            fallback=cfg.get("fallback_google", True),
            workers=int(self.sl_workers.value()),
            only_empty=True,
        )
        self.worker.progress.connect(
            lambda d, t, m: (self.progress.setMaximum(t),
                             self.progress.setValue(d),
                             self.status.setText(f"Traduciendo {m}")))
        self.worker.log.connect(self.main.log)
        self.worker.finished_ok.connect(self.on_done)
        self.worker.failed.connect(lambda m: (QMessageBox.critical(self, APP_NAME, m),
                                              self.status.setText("Error.")))
        self.btn_translate.setEnabled(False); self.btn_stop.setEnabled(True)
        self.status.setText("Traduciendo…")
        self.worker.start()

    def stop(self):
        if hasattr(self, 'worker'): self.worker.stop()
        self.status.setText("Deteniendo… (los workers activos terminan su tarea actual)")

    def on_done(self, entries):
        self.main.entries = entries
        self.btn_translate.setEnabled(True); self.btn_stop.setEnabled(False)
        self.btn_export.setEnabled(True)
        n_ok = sum(1 for e in entries if e.translation and e.translation.strip())
        self.status.setText(f"Traducción completa ✔  ({n_ok}/{len(entries)})")
        self.main.log(f"✔ Traducción completa. {n_ok}/{len(entries)} con texto.")
        QMessageBox.information(self, APP_NAME,
            f"Traducción terminada.\n{n_ok}/{len(entries)} entradas traducidas.\n\n"
            f"Pulsa 'Exportar a game/tl/' para generar los .rpy.")

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


# ---------------------------------------------------------------------------
# TAB 2: Funciones (por speaker / block_id)
# ---------------------------------------------------------------------------
class FuncionesTab(QWidget):
    def __init__(self, main):
        super().__init__(); self.main = main
        lay = QVBoxLayout(self); lay.setContentsMargins(34, 18, 34, 28); lay.setSpacing(12)
        title = QLabel("Funciones / Speakers"); title.setObjectName("Section")
        lay.addWidget(title)
        lay.addWidget(QLabel("Conteo de entradas agrupadas por speaker / block / kind."))
        self.list = QListWidget()
        lay.addWidget(self.list, 1)

    def refresh(self, entries: List[Entry]):
        self.list.clear()
        counts = {}
        for e in entries:
            key = e.speaker or e.block_id or ('strings' if e.kind == 'string' else e.kind)
            counts[key] = counts.get(key, 0) + 1
        for k, v in sorted(counts.items(), key=lambda x: -x[1])[:1000]:
            self.list.addItem(f"{k}   ×{v}")


# ---------------------------------------------------------------------------
# TAB 3: Clases (categoría + kind)
# ---------------------------------------------------------------------------
class ClasesTab(QWidget):
    def __init__(self, main):
        super().__init__(); self.main = main
        lay = QVBoxLayout(self); lay.setContentsMargins(34, 18, 34, 28); lay.setSpacing(12)
        lay.addWidget(QLabel("Clasificación por categoría", objectName="Section"))

        row = QHBoxLayout(); row.setSpacing(20)
        self.cat_list = QListWidget(); self.cat_list.setMaximumWidth(260)
        row.addWidget(self.cat_list)
        self.kind_list = QListWidget()
        row.addWidget(self.kind_list, 1)
        lay.addLayout(row, 1)

    def refresh(self, entries: List[Entry]):
        self.cat_list.clear(); self.kind_list.clear()
        cats = {}; kinds = {}
        for e in entries:
            cats[e.category] = cats.get(e.category, 0) + 1
            kinds[e.kind] = kinds.get(e.kind, 0) + 1
        for k, v in sorted(cats.items(), key=lambda x: -x[1]):
            icon = {'phone': '📱', 'menu': '🧭', 'dialogue': '💬', 'raw': '🔤'}.get(k, '·')
            self.cat_list.addItem(f"{icon}  {k}   ×{v}")
        for k, v in sorted(kinds.items(), key=lambda x: -x[1]):
            self.kind_list.addItem(f"{k}   ×{v}")


# ---------------------------------------------------------------------------
# TAB 4: Herramientas
# ---------------------------------------------------------------------------
class HerramientasTab(QWidget):
    def __init__(self, main):
        super().__init__(); self.main = main
        lay = QGridLayout(self); lay.setContentsMargins(34, 18, 34, 28)
        lay.setHorizontalSpacing(12); lay.setVerticalSpacing(12)

        lay.addWidget(QLabel("Utilidades", objectName="Section"), 0, 0, 1, 2)

        btns = [
            ("Exportar JSON de entradas", self.tool_extract),
            ("Limpiar caché de traducciones", self.tool_clear_cache),
            ("Mostrar tamaño de caché", self.tool_cache_size),
            ("Re-escanear carpeta del juego", self.tool_rescan),
            ("Generar screens.rpy (fix GUI)", self.tool_gen_screens),
            ("Generar replaceText.rpy", self.tool_gen_replace),
        ]
        for i, (txt, fn) in enumerate(btns):
            b = QPushButton(txt); b.clicked.connect(fn); b.setMinimumHeight(40)
            lay.addWidget(b, 1 + i // 2, i % 2)

        # Selector de idioma in-game
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
        self.pos_combo.addItem("Esquina inferior derecha", "bottom_right")
        self.pos_combo.addItem("Esquina inferior izquierda", "bottom_left")
        self.pos_combo.addItem("Esquina superior derecha", "top_right")
        self.pos_combo.addItem("Esquina superior izquierda", "top_left")
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
        try:
            CACHE._data = {}; CACHE._dirty = True; CACHE.flush()
            self._info("Caché limpiada.")
        except Exception as e:
            self._info(f"Error: {e}")

    def tool_cache_size(self):
        from translator_engines import CACHE_PATH
        n = len(CACHE._data)
        size = CACHE_PATH.stat().st_size if CACHE_PATH.exists() else 0
        self._info(f"Caché: {n} entradas · {size/1024:.1f} KB\n{CACHE_PATH}")

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
# TAB 5: Registro
# ---------------------------------------------------------------------------
class RegistroTab(QWidget):
    def __init__(self, main):
        super().__init__(); self.main = main
        lay = QVBoxLayout(self); lay.setContentsMargins(34, 18, 34, 28); lay.setSpacing(10)
        head = QHBoxLayout()
        head.addWidget(QLabel("Registro en vivo", objectName="Section"))
        head.addStretch()
        b_clear = QPushButton("Limpiar"); b_clear.clicked.connect(lambda: self.log.clear())
        head.addWidget(b_clear)
        b_export = QPushButton("Exportar log"); b_export.clicked.connect(self.export_log)
        head.addWidget(b_export)
        lay.addLayout(head)
        self.log = QPlainTextEdit(); self.log.setReadOnly(True)
        lay.addWidget(self.log, 1)

    def append(self, msg: str): self.log.appendPlainText(msg)

    def export_log(self):
        path, _ = QFileDialog.getSaveFileName(self, "Guardar log", "renpy_translator.log",
                                              "Log (*.log);;Texto (*.txt)")
        if not path: return
        Path(path).write_text(self.log.toPlainText(), encoding='utf-8')
        QMessageBox.information(self, APP_NAME, "Log exportado.")


# ---------------------------------------------------------------------------
# TAB 6: Ajustes
# ---------------------------------------------------------------------------
class AjustesTab(QWidget):
    def __init__(self, main):
        super().__init__(); self.main = main
        cfg = main.config
        lay = QGridLayout(self); lay.setContentsMargins(34, 18, 34, 28)
        lay.setHorizontalSpacing(14); lay.setVerticalSpacing(12)

        lay.addWidget(QLabel("Idioma destino (carpeta tl/)", objectName="Section"), 0, 0)
        self.tl_name = QLineEdit(cfg.get("tl_name", "spanish_latino")); lay.addWidget(self.tl_name, 0, 1)

        lay.addWidget(QLabel("Motor preferido", objectName="Section"), 1, 0)
        self.engine = QComboBox(); self.engine.addItem("Google Translate (gratis)", "google")
        self.engine.addItem("DeepLX", "deeplx")
        idx = 1 if cfg.get("engine") == "deeplx" else 0
        self.engine.setCurrentIndex(idx)
        lay.addWidget(self.engine, 1, 1)

        lay.addWidget(QLabel("Endpoint DeepLX", objectName="Section"), 2, 0)
        self.deeplx = QLineEdit(cfg.get("deeplx_endpoint", DEFAULT_DEEPLX))
        lay.addWidget(self.deeplx, 2, 1)

        lay.addWidget(QLabel("Hilos por defecto", objectName="Section"), 3, 0)
        self.workers = QSpinBox(); self.workers.setRange(1, 64)
        self.workers.setValue(int(cfg.get("workers", 16)))
        lay.addWidget(self.workers, 3, 1)

        self.cb_fallback = QCheckBox("Usar el otro motor como fallback automático")
        self.cb_fallback.setChecked(cfg.get("fallback_google", True))
        lay.addWidget(self.cb_fallback, 4, 0, 1, 2)

        self.cb_phone_prio = QCheckBox("Priorizar mensajes de teléfono y menús (AVN)")
        self.cb_phone_prio.setChecked(cfg.get("phone_priority", True))
        lay.addWidget(self.cb_phone_prio, 5, 0, 1, 2)

        tips = QLabel(
            "Tips de velocidad\n"
            "• Google Translate gratis = más rápido y estable.\n"
            "• DeepLX público puede limitar; uno propio (Vercel/Railway) es ideal.\n"
            "• 16 hilos es buen balance. Sube a 24-32 con DeepLX propio.\n"
            "• La caché en disco evita retraducir frases repetidas.")
        tips.setObjectName("Small"); tips.setWordWrap(True)
        lay.addWidget(tips, 6, 0, 1, 2)

        btns = QHBoxLayout()
        b_save = QPushButton("Guardar"); b_save.setObjectName("primary"); b_save.setMinimumHeight(40)
        b_save.clicked.connect(self.save)
        b_reset = QPushButton("Restaurar"); b_reset.clicked.connect(self.reset)
        btns.addWidget(b_save); btns.addWidget(b_reset); btns.addStretch()
        w = QWidget(); w.setLayout(btns); lay.addWidget(w, 7, 0, 1, 2)
        lay.setRowStretch(8, 1)

    def save(self):
        cfg = self.main.config
        cfg["tl_name"] = self.tl_name.text().strip() or "spanish_latino"
        cfg["engine"] = self.engine.currentData()
        cfg["deeplx_endpoint"] = self.deeplx.text().strip() or DEFAULT_DEEPLX
        cfg["fallback_google"] = self.cb_fallback.isChecked()
        cfg["phone_priority"] = self.cb_phone_prio.isChecked()
        cfg["workers"] = int(self.workers.value())
        save_config(cfg)
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
        QMessageBox.information(self, APP_NAME, "Configuración restaurada.")


# ---------------------------------------------------------------------------
# Title bar (frameless)
# ---------------------------------------------------------------------------
class TitleBar(QWidget):
    def __init__(self, parent: 'MainWindow'):
        super().__init__(parent)
        self.parent_win = parent
        self.setObjectName("TitleBar")
        self.setFixedHeight(54)
        self._drag_pos: Optional[QPoint] = None

        lay = QHBoxLayout(self); lay.setContentsMargins(18, 10, 14, 10); lay.setSpacing(12)

        avatar = QLabel(); avatar.setPixmap(make_mascot(36))
        # glow suave en mascota
        eff = QGraphicsDropShadowEffect(self); eff.setBlurRadius(22)
        eff.setColor(QColor(41, 224, 212, 200)); eff.setOffset(0, 0)
        avatar.setGraphicsEffect(eff)
        lay.addWidget(avatar)

        lay.addStretch(1)
        title = QLabel(f"{APP_NAME} v{APP_VERSION}"); title.setObjectName("TitleLabel")
        lay.addWidget(title)
        lay.addStretch(1)

        dev_label = QLabel("dev by xav")
        dev_label.setStyleSheet("color: #00BFFF; font-size: 8.5pt; font-weight: 500; background: transparent; border: none; padding-right: 8px;")
        lay.addWidget(dev_label)

        for txt, slot, obj in (
            ("—", parent.showMinimized, "WinBtn"),
            ("☐", self._toggle_max, "WinBtn"),
            ("✕", parent.close, "WinClose"),
        ):
            b = QPushButton(txt); b.setObjectName(obj); b.setFixedSize(34, 28)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.clicked.connect(slot)
            lay.addWidget(b)

    def _toggle_max(self):
        if self.parent_win.isMaximized(): self.parent_win.showNormal()
        else: self.parent_win.showMaximized()

    def mousePressEvent(self, e: QMouseEvent):
        if e.button() == Qt.MouseButton.LeftButton:
            self._drag_pos = e.globalPosition().toPoint() - self.parent_win.frameGeometry().topLeft()

    def mouseMoveEvent(self, e: QMouseEvent):
        if self._drag_pos is not None and e.buttons() & Qt.MouseButton.LeftButton:
            if self.parent_win.isMaximized():
                self.parent_win.showNormal()
            self.parent_win.move(e.globalPosition().toPoint() - self._drag_pos)

    def mouseReleaseEvent(self, e: QMouseEvent):
        self._drag_pos = None

    def mouseDoubleClickEvent(self, e: QMouseEvent):
        self._toggle_max()


# ---------------------------------------------------------------------------
# MainWindow (frameless con root redondeado)
# ---------------------------------------------------------------------------
class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(APP_NAME)
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.Window)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.resize(1180, 740); self.setMinimumSize(1000, 640)

        self.config = load_config()
        self.entries: List[Entry] = []
        self.project_root: str = ""
        self.game_dir: Optional[str] = None

        root = QWidget(); root.setObjectName("Root")
        self.setCentralWidget(root)

        v = QVBoxLayout(root); v.setContentsMargins(2, 2, 2, 2); v.setSpacing(0)

        self.title_bar = TitleBar(self)
        v.addWidget(self.title_bar)

        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(True)
        self.tabs.tabBar().setExpanding(False)
        self.tab_trad = TraduccionTab(self);   self.tabs.addTab(self.tab_trad,   "Traducción")
        self.tab_funcs = FuncionesTab(self);   self.tabs.addTab(self.tab_funcs,  "Funciones")
        self.tab_clases = ClasesTab(self);     self.tabs.addTab(self.tab_clases, "Clases")
        self.tab_tools = HerramientasTab(self);self.tabs.addTab(self.tab_tools,  "Herramientas")
        self.tab_log = RegistroTab(self);      self.tabs.addTab(self.tab_log,    "Registro")
        self.tab_settings = AjustesTab(self);  self.tabs.addTab(self.tab_settings,"Ajustes")
        v.addWidget(self.tabs, 1)

        # sombra de la ventana
        eff = QGraphicsDropShadowEffect(self)
        eff.setBlurRadius(40); eff.setColor(QColor(0, 0, 0, 200)); eff.setOffset(0, 6)
        root.setGraphicsEffect(eff)

    def log(self, msg: str):
        self.tab_log.append(f"[{time.strftime('%H:%M:%S')}] {msg}")

    def refresh_all(self):
        self.tab_funcs.refresh(self.entries)
        self.tab_clases.refresh(self.entries)


# ---------------------------------------------------------------------------
def main():
    app = QApplication(sys.argv)
    app.setStyleSheet(QSS)
    app.setFont(QFont("Segoe UI", 10))
    w = MainWindow(); w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
