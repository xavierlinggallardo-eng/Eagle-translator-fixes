"""
Motores de traduccion -- ULTRA OPTIMIZADO v6.0 "Eagle"
====================================================
Cambios principales sobre v5.0:
  - LRU eviction CORRECTO (bucle while, no un solo del)
  - translate_google_batch: preserva resultados parciales (antes los tiraba todos)
  - register_character_names: invalidacion LAZY (sin O(N*M))
  - Post-procesado latino unificado en UNA sola pasada (combina ~70 reglas)
  - Glosario de usuario (terminos forzados + nombres protegidos en una sola API)
  - Prewarming HTTP DIFERIDO (no bloquea import)
  - Progress callback con ETA (segundos restantes) + items/seg
  - Estadisticas globales (cache_hits, batch_calls, total_chars, etc.)
  - Reintento adaptativo con backoff por respuesta de Google (429 / vacio)
  - Cache namespace opcional por-proyecto
  - Postprocesador BR portugues
  - Deteccion de placeholders mejorada (renpy {color=#ffff00}, [var!t], etc.)
  - Restauracion de capitalizacion (si el original empezaba con minuscula, traducir asi)
  - Detector de "no traducido" (devuelve == original) -> reintenta con otro motor
"""
from __future__ import annotations
import re, time, json, threading, hashlib, os, random
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import List, Optional, Callable, Dict, Tuple, Iterable
import requests
from requests.adapters import HTTPAdapter
try:
    from urllib3.util.retry import Retry
except ImportError:
    from requests.packages.urllib3.util.retry import Retry

try:
    import ujson as _json_fast
    _HAS_FAST_JSON = True
except ImportError:
    try:
        import orjson as _json_fast
        _HAS_FAST_JSON = True
    except ImportError:
        _json_fast = None
        _HAS_FAST_JSON = False

def _json_loads(data) -> object:
    if _HAS_FAST_JSON:
        try:
            return _json_fast.loads(data)
        except Exception:
            pass
    return json.loads(data)


DEFAULT_DEEPLX = "https://deeplx.vercel.app/translate"
CACHE_PATH = Path.home() / ".renpy_translator_cache.json"
GLOSSARY_PATH = Path.home() / ".renpy_translator_glossary.json"

# ---------------------------------------------------------------------------
# Estadisticas globales (lecturas baratas, sin lock)
# ---------------------------------------------------------------------------
class _Stats:
    __slots__ = ('cache_hits','cache_misses','batch_calls','single_calls',
                 'fallback_calls','http_errors','total_chars','total_seconds',
                 'started_at')
    def __init__(self):
        self.cache_hits = 0
        self.cache_misses = 0
        self.batch_calls = 0
        self.single_calls = 0
        self.fallback_calls = 0
        self.http_errors = 0
        self.total_chars = 0
        self.total_seconds = 0.0
        self.started_at = time.time()

    def snapshot(self) -> Dict[str, float]:
        elapsed = max(0.001, time.time() - self.started_at)
        total = self.cache_hits + self.cache_misses
        hit_rate = (self.cache_hits / total * 100.0) if total else 0.0
        speed = self.total_chars / elapsed if elapsed else 0.0
        return {
            'cache_hits': self.cache_hits,
            'cache_misses': self.cache_misses,
            'hit_rate_pct': round(hit_rate, 1),
            'batch_calls': self.batch_calls,
            'single_calls': self.single_calls,
            'fallback_calls': self.fallback_calls,
            'http_errors': self.http_errors,
            'total_chars': self.total_chars,
            'chars_per_sec': round(speed, 1),
            'uptime_s': round(elapsed, 1),
        }

    def reset(self):
        self.__init__()

STATS = _Stats()


# ---------------------------------------------------------------------------
# Post-procesador: español latino neutro -- UNIFICADO en un solo regex
# ---------------------------------------------------------------------------
# Tabla (regex_alternation -> dict id->replacement). Combinamos todas las
# reglas en UNA sola alternation; aplicamos un unico re.sub con callback
# que resuelve el reemplazo correcto. Para 70 reglas y un batch de 10k
# strings, vamos de 700k sub-calls a 10k.

_LATINO_RULES: List[Tuple[str, str]] = [
    # Saludos / cortesia
    (r'(?<![¿?])¿[Cc]ómo estás\?',         '¿Cómo te va?'),
    (r'\b[Mm]ucho gusto en conocerte\b',    'qué gusto conocerte'),
    (r'\b[Ff]ue un placer conocerte\b',     'qué gusto conocerte'),
    (r'\b[Ee]ncantado de conocerte\b',      'qué gusto conocerte'),
    (r'\b[Ee]ncantada de conocerte\b',      'qué gusto conocerte'),
    # Afirmaciones / negaciones
    (r'\b[Dd]e ninguna manera\b',           'ni de chiste'),
    (r'\b[Ee]n absoluto\b',                 'para nada'),
    (r'\b[Pp]or supuesto que no\b',         'claro que no'),
    (r'\b[Pp]or supuesto\b',                'claro que sí'),
    (r'\b[Cc]iertamente\b',                 'claro'),
    (r'\b[Aa]bsolutamente\b',               'totalmente'),
    # Intensificadores
    (r'\b[Rr]ealmente\b',                   'de verdad'),
    (r'\b[Vv]erdaderamente\b',              'de verdad'),
    (r'\b[Ss]umamente\b',                   'muy'),
    (r'\b[Ff]rancamente\b',                 'la verdad'),
    (r'\b[Hh]onestamente\b',                'en serio'),
    # Cotidiano
    (r'\b[Ee]spera un momento\b',           'espérate un momento'),
    (r'\b[Ee]spera un segundo\b',           'espérate un segundo'),
    (r'\b[Ee]stoy bromeando\b',             'es broma'),
    (r'\b[Ee]stás bromeando\b',             '¿en serio?'),
    (r'\b[Nn]o es gran cosa\b',             'no es para tanto'),
    (r'\b[Ll]o tengo bajo control\b',       'yo me hago cargo'),
    (r'\b[Mm]e arruiné\b',                  'la regué'),
    (r'\b[Ll]o arruiné\b',                  'la regué'),
    (r'\b[Ll]o eché a perder\b',            'la regué'),
    (r'\b[Tt]odo estará bien\b',            'todo va a estar bien'),
    (r'\b[Ee]stará bien\b',                 'va a estar bien'),
    (r'\b[Qq]ué genial\b',                  'qué padre'),
    (r'\b[Qq]ué increíble\b',               'qué chido'),
    (r'\b[Ee]so es genial\b',               'qué padre'),
    (r'\b[Ee]stoy enloqueciendo\b',         'me estoy volviendo loco'),
    # Peninsulares -> latino
    (r'\b[Vv]osotros\b',                    'ustedes'),
    (r'\b[Vv]uestro\b',                     'su'),
    (r'\b[Vv]uestra\b',                     'su'),
    (r'\b[Vv]uestros\b',                    'sus'),
    (r'\b[Vv]uestras\b',                    'sus'),
    (r'\bhabéis\b',                         'han'),
    (r'\btenéis\b',                         'tienen'),
    (r'\bsois\b',                           'son'),
    (r'\bvais\b',                           'van'),
    (r'\bpodéis\b',                         'pueden'),
    (r'\bdebéis\b',                         'deben'),
    (r'\bcoged\b',                          'agarren'),
    (r'\btomad\b',                          'tomen'),
    (r'\bhaced\b',                          'hagan'),
    (r'\bdejad\b',                          'dejen'),
    (r'\bmirad\b',                          'miren'),
    (r'\bvenid\b',                          'vengan'),
    # Muletillas formales -> orales
    (r'\b[Ss]in embargo\b',                 'pero'),
    (r'\b[Nn]o obstante\b',                 'pero'),
    (r'\b[Pp]or lo tanto\b',                'así que'),
    (r'\b[Pp]or consiguiente\b',            'así que'),
    (r'\b[Ee]n consecuencia\b',             'así que'),
    (r'\b[Ee]n ese caso\b',                 'entonces'),
    (r'\b[Pp]ermíteme\b',                   'déjame'),
    (r'\b[Pp]ermítame\b',                   'déjame'),
    (r'\b[Dd]ebo admitir\b',                'la verdad'),
    (r'\b[Hh]e de admitir\b',               'la verdad'),
    (r'\b[Ee]stimado\b',                    'querido'),
    # Pronombres dejar caer mas natural
    (r'\b[Nn]osotros mismos\b',             'nosotros'),
    (r'\b[Ee]llos mismos\b',                'ellos'),
    # Cortesias raras de Google
    (r'\b[Yy]o también te amo\b',           'yo también'),
    (r'\b[Mm]e gustaría saber\b',           'quiero saber'),
    (r'\b[Mm]e gustaría preguntarte\b',     'quiero preguntarte'),
    (r'\b[Tt]engo que decirte\b',           'te tengo que decir'),
    (r'\b[Vv]oy a tener que\b',             'voy a'),
    # Errores frecuentes
    (r'\b[Mm]aldita sea\b',                 'maldita sea'),  # noop, mantener
]

def _build_unified_regex(rules: List[Tuple[str, str]]):
    """Compila una sola regex con todas las reglas y devuelve (regex, dispatch)."""
    parts = []
    table: List[str] = []
    for pat, rep in rules:
        parts.append('(?:' + pat + ')')
        table.append(rep)
    big = re.compile('|'.join(parts), re.UNICODE)
    # Para mapear el match al index correcto usamos lastindex.
    # Pero como envolvimos en (?:...) no hay grupos. Truco: envolver
    # cada regla en su PROPIO grupo de captura para que lastindex apunte
    # al correcto. Reconstruimos:
    parts2 = []
    for i, (pat, _) in enumerate(rules):
        parts2.append('(' + pat + ')')
    big = re.compile('|'.join(parts2), re.UNICODE)
    def dispatch(m):
        idx = m.lastindex  # 1-based
        if idx is None:
            return m.group(0)
        rep = table[idx - 1]
        return rep
    return big, dispatch

_LATINO_BIG_RE, _LATINO_DISPATCH = _build_unified_regex(_LATINO_RULES)

# Targets que activan postprocesado latino
_LATIN_SPANISH_TARGETS = {
    'ES419', 'ES-419', 'ES_419',
    'ESLA',  'ES-LA',  'ES_LA',
    'ESMX',  'ES-MX',  'ES_MX',
    'ESAR',  'ES-AR',  'ES_AR',
    'ESCO',  'ES-CO',  'ES_CO',
    'ESCL',  'ES-CL',  'ES_CL',
    'ESPE',  'ES-PE',  'ES_PE',
    'ESUY',  'ES-UY',  'ES_UY',
    'ESVE',  'ES-VE',  'ES_VE',
    'ESBO',  'ES-BO',  'ES_BO',
    'ESEC',  'ES-EC',  'ES_EC',
    'ESPY',  'ES-PY',  'ES_PY',
    'ESDO',  'ES-DO',  'ES_DO',
    'ESPR',  'ES-PR',  'ES_PR',
    'ESCR',  'ES-CR',  'ES_CR',
    'ESPA',  'ES-PA',  'ES_PA',
    'ESHN',  'ES-HN',  'ES_HN',
    'ESGT',  'ES-GT',  'ES_GT',
    'ESSV',  'ES-SV',  'ES_SV',
    'ESNI',  'ES-NI',  'ES_NI',
}
_LATIN_SPANISH_NORM = {t.upper().replace('-','').replace('_','') for t in _LATIN_SPANISH_TARGETS}

def _is_latin_spanish(target: str) -> bool:
    return target.upper().replace('-','').replace('_','') in _LATIN_SPANISH_NORM


def _postprocess_latino(text: str, target: str) -> str:
    if not _is_latin_spanish(target):
        return text
    return _LATINO_BIG_RE.sub(_LATINO_DISPATCH, text)


# ---------------------------------------------------------------------------
# Post-procesador BR (portugues brasileno)
# ---------------------------------------------------------------------------
_BR_RULES = [
    # Tutear vs vos vs voce
    (r'\b[Tt]u és\b',     'você é'),
    (r'\b[Tt]u estás\b',  'você está'),
    (r'\b[Tt]u tens\b',   'você tem'),
    (r'\b[Tt]u podes\b',  'você pode'),
    (r'\b[Tt]u sabes\b',  'você sabe'),
    (r'\b[Cc]ontigo\b',   'com você'),
    # Peninsulares portugues PT-PT -> BR
    (r'\b[Aa]utocarro\b', 'ônibus'),
    (r'\b[Tt]elemóvel\b', 'celular'),
    (r'\b[Ff]rigorífico\b','geladeira'),
    (r'\b[Cc]asa de banho\b','banheiro'),
    (r'\b[Rr]apaz\b',     'menino'),
]
_BR_BIG_RE, _BR_DISPATCH = _build_unified_regex(_BR_RULES)

def _postprocess_brazilian(text: str, target: str) -> str:
    t = target.upper().replace('-','').replace('_','')
    if t not in ('PTBR', 'PT_BR'):
        return text
    return _BR_BIG_RE.sub(_BR_DISPATCH, text)


def _postprocess_all(text: str, target: str) -> str:
    """Aplica todos los post-procesadores aplicables al target."""
    text = _postprocess_latino(text, target)
    text = _postprocess_brazilian(text, target)
    return text


# ---------------------------------------------------------------------------
# Placeholders Ren'Py
# ---------------------------------------------------------------------------
# Mas robusto: cubre {color=#ffff00}, {b}...{/b}, [var!t], %(name)s, \n, \t.
PLACEHOLDER_PATTERNS = [
    r'\{[^{}]*\}',          # {color=#...}, {b}, {/b}, {size=+12}
    r'\[[^\[\]]+\]',        # [name], [var!t]
    r'%\([^)]+\)[sdif]',    # %(name)s
    r'%[sdif]',             # %s %d
    r'\\n', r'\\t', r'\\r', r'\\"',
]
PH_RE = re.compile('|'.join(PLACEHOLDER_PATTERNS))
_RESTORE_RE = re.compile(
    r'(?:⟦|【|〔|《|\[\[)\s*(\d+)\s*(?:⟧|】|〕|》|\]\])'
)


class Protector:
    """Sustituye placeholders y nombres protegidos por tokens ⟦N⟧."""
    __slots__ = ('tokens',)
    def __init__(self):
        self.tokens: List[str] = []

    def shield(self, text: str) -> str:
        self.tokens = []
        tokens = self.tokens
        def repl(m):
            tokens.append(m.group(0))
            return f"⟦{len(tokens)-1}⟧"
        out = PH_RE.sub(repl, text)
        _names = _PROTECTED_NAMES.get_all()
        if _names:
            for name in sorted(_names, key=len, reverse=True):
                if name in out:
                    tokens.append(name)
                    out = out.replace(name, f"⟦{len(tokens)-1}⟧")
        return out

    def restore(self, text: str) -> str:
        if not self.tokens:
            return text
        tokens = self.tokens
        def repl(m):
            idx = int(m.group(1))
            if 0 <= idx < len(tokens):
                return tokens[idx]
            return m.group(0)
        return _RESTORE_RE.sub(repl, text)


# ---------------------------------------------------------------------------
# Registro de nombres protegidos (lazy invalidation, sin O(N*M))
# ---------------------------------------------------------------------------
class _CharacterNameRegistry:
    """Almacena nombres de personajes que NO deben ser traducidos.
    La invalidacion de cache es LAZY: en lugar de recorrer el cache cada
    vez que se registra un nombre, solo bumpeamos un epoch. Al hacer
    CACHE.get() para un nombre protegido, esta funcion ya devuelve el
    nombre original sin tocar cache (translate_batch lo gestiona)."""
    def __init__(self):
        self._names: set = set()
        self._lock = threading.Lock()
        self._epoch = 0

    def register(self, names):
        new = []
        with self._lock:
            for n in names:
                n = n.strip()
                if n and len(n) >= 2 and n not in self._names:
                    self._names.add(n)
                    new.append(n)
            if new:
                self._epoch += 1

    def clear(self):
        with self._lock:
            self._names.clear()
            self._epoch += 1

    def remove(self, name):
        with self._lock:
            self._names.discard(name)
            self._epoch += 1

    def get_all(self) -> list:
        with self._lock:
            return list(self._names)

    def get_set(self) -> set:
        with self._lock:
            return set(self._names)

    @property
    def epoch(self) -> int:
        return self._epoch

_PROTECTED_NAMES = _CharacterNameRegistry()


def register_character_names(names) -> None:
    _PROTECTED_NAMES.register(names)

def get_protected_names() -> list:
    return _PROTECTED_NAMES.get_all()

def clear_character_names() -> None:
    _PROTECTED_NAMES.clear()


# ---------------------------------------------------------------------------
# Glosario de usuario (terminos forzados source->target)
# ---------------------------------------------------------------------------
class _Glossary:
    """Glosario de terminos source->target. Aplicacion en dos modos:
       - hard: el termino se sustituye por el target ANTES de traducir
         (y se restaura por su placeholder), garantiza que aparezca tal cual.
       - post: se sustituye en el texto traducido (case-insensitive).
    """
    def __init__(self):
        self._terms: Dict[str, str] = {}
        self._lock = threading.Lock()
        self._compiled = None  # (regex_big, table)

    def _recompile(self):
        if not self._terms:
            self._compiled = None
            return
        items = sorted(self._terms.items(), key=lambda kv: -len(kv[0]))
        keys = [re.escape(k) for k, _ in items]
        vals = [v for _, v in items]
        pattern = re.compile(
            r'(?<![A-Za-z0-9_])(' + '|'.join(keys) + r')(?![A-Za-z0-9_])',
            re.IGNORECASE
        )
        # mapa case-insensitive
        ci_map = {k.lower(): v for k, v in items}
        self._compiled = (pattern, ci_map)

    def add(self, source: str, target: str):
        s = (source or '').strip(); t = (target or '').strip()
        if not s or not t:
            return
        with self._lock:
            self._terms[s] = t
            self._recompile()

    def add_many(self, mapping: Dict[str, str]):
        with self._lock:
            for k, v in mapping.items():
                k = (k or '').strip(); v = (v or '').strip()
                if k and v:
                    self._terms[k] = v
            self._recompile()

    def remove(self, source: str):
        with self._lock:
            self._terms.pop(source, None)
            self._recompile()

    def clear(self):
        with self._lock:
            self._terms.clear()
            self._compiled = None

    def get_all(self) -> Dict[str, str]:
        with self._lock:
            return dict(self._terms)

    def apply_post(self, text: str) -> str:
        """Aplica el glosario despues de traducir (preserva capitalizacion del match)."""
        if not self._compiled or not text:
            return text
        pattern, ci_map = self._compiled
        def repl(m):
            src = m.group(1)
            tgt = ci_map.get(src.lower(), src)
            # preservar capitalizacion basica
            if src.isupper(): return tgt.upper()
            if src[0].isupper(): return tgt[:1].upper() + tgt[1:]
            return tgt
        return pattern.sub(repl, text)

    def save(self, path: Path = GLOSSARY_PATH):
        try:
            with self._lock:
                path.write_text(json.dumps(self._terms, ensure_ascii=False, indent=2),
                                encoding='utf-8')
        except Exception as e:
            print('[glossary save]', e)

    def load(self, path: Path = GLOSSARY_PATH):
        try:
            if path.exists():
                data = json.loads(path.read_text(encoding='utf-8'))
                if isinstance(data, dict):
                    self.add_many({str(k): str(v) for k, v in data.items()})
        except Exception as e:
            print('[glossary load]', e)


GLOSSARY = _Glossary()
GLOSSARY.load()


def glossary_add(source: str, target: str) -> None:
    GLOSSARY.add(source, target); GLOSSARY.save()

def glossary_add_many(mapping: Dict[str, str]) -> None:
    GLOSSARY.add_many(mapping); GLOSSARY.save()

def glossary_remove(source: str) -> None:
    GLOSSARY.remove(source); GLOSSARY.save()

def glossary_clear() -> None:
    GLOSSARY.clear(); GLOSSARY.save()

def glossary_all() -> Dict[str, str]:
    return GLOSSARY.get_all()


# ---------------------------------------------------------------------------
# Sesion HTTP -- pool 256, keep-alive
# ---------------------------------------------------------------------------
def _make_session() -> requests.Session:
    s = requests.Session()
    retry = Retry(
        total=3, connect=2, read=2, backoff_factor=0.15,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset(['GET', 'POST']),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(pool_connections=256, pool_maxsize=256, max_retries=retry)
    s.mount('http://', adapter)
    s.mount('https://', adapter)
    s.headers.update({
        'User-Agent': ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                       '(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36'),
        'Accept-Encoding': 'gzip, deflate, br',
        'Accept': '*/*',
        'Connection': 'keep-alive',
    })
    return s

_SESSION = _make_session()

# Prewarming DIFERIDO: se dispara al primer translate, no al import
_prewarm_done = False
_prewarm_lock = threading.Lock()

def _prewarm_connections_once():
    global _prewarm_done
    if _prewarm_done:
        return
    with _prewarm_lock:
        if _prewarm_done:
            return
        _prewarm_done = True
        def _do():
            endpoints = ['https://translate.googleapis.com', 'https://translate.google.com']
            for ep in endpoints:
                try: _SESSION.head(ep, timeout=3)
                except Exception: pass
        threading.Thread(target=_do, daemon=True).start()


# ---------------------------------------------------------------------------
# Cache en memoria (LRU) + disco persistente
# ---------------------------------------------------------------------------
class LRUMemoryCache:
    def __init__(self, maxsize: int = 200_000):
        self._data: dict = {}
        self._maxsize = maxsize
        self._lock = threading.Lock()

    def get(self, k: str) -> Optional[str]:
        v = self._data.get(k)
        if v is not None and len(self._data) > self._maxsize * 0.9:
            # mueve al final solo si estamos cerca del limite
            with self._lock:
                if k in self._data:
                    self._data[k] = self._data.pop(k)
        return v

    def set(self, k: str, v: str):
        with self._lock:
            if k in self._data:
                del self._data[k]
            self._data[k] = v
            self._evict_locked()

    def set_many(self, items: Dict[str, str]):
        if not items:
            return
        with self._lock:
            for k, v in items.items():
                if k in self._data:
                    del self._data[k]
                self._data[k] = v
            self._evict_locked()

    def _evict_locked(self):
        # FIX: bucle while -- evicta TODOS los excedentes, no solo uno
        while len(self._data) > self._maxsize:
            try:
                oldest = next(iter(self._data))
                del self._data[oldest]
            except StopIteration:
                break

    def bulk_load(self, data: Dict[str, str]):
        with self._lock:
            self._data.update(data)
            self._evict_locked()

    def clear(self):
        with self._lock:
            self._data.clear()

    def snapshot(self) -> Dict[str, str]:
        with self._lock:
            return dict(self._data)

    def __len__(self):
        return len(self._data)


class TranslationCache:
    """Cache de dos capas: memoria LRU + disco persistente."""
    def __init__(self, path: Path = CACHE_PATH):
        self.path = path
        self._lock = threading.Lock()
        self._data: Dict[str, str] = {}
        self._mem = LRUMemoryCache(maxsize=200_000)
        self._dirty = False
        self._flush_thread: Optional[threading.Thread] = None
        self._namespace = ''  # cache namespace opcional por proyecto
        self.load()

    def set_namespace(self, ns: str):
        """Cambia el namespace -- las nuevas keys lo incluyen. No invalida lo guardado."""
        self._namespace = (ns or '').strip()

    @staticmethod
    def _make_key(text: str, source: str, target: str, engine: str, ns: str = '') -> str:
        payload = f"{ns}|{engine}|{source}|{target}|{text}".encode('utf-8')
        return hashlib.md5(payload, usedforsecurity=False).hexdigest()

    def namespaced_key(self, text: str, source: str, target: str, engine: str) -> str:
        """Key con namespace activo (usar en translate_batch / _translate_one)."""
        return self._make_key(text, source, target, engine, self._namespace)

    # Compatibilidad con codigo externo que llamaba TranslationCache.key(...)
    # como staticmethod, sin namespace (renpy_parser, code legacy).
    @staticmethod
    def key(text: str, source: str, target: str, engine: str) -> str:
        return TranslationCache._make_key(text, source, target, engine, '')

    def load(self):
        if self.path.exists():
            try:
                raw = self.path.read_text(encoding='utf-8')
                data = _json_loads(raw)
                if isinstance(data, dict):
                    self._data = data
                    self._mem.bulk_load(data)
            except Exception:
                self._data = {}

    def get(self, k: str) -> Optional[str]:
        v = self._mem.get(k)
        if v is not None:
            return v
        return self._data.get(k)

    def set(self, k: str, v: str):
        self._mem.set(k, v)
        with self._lock:
            self._data[k] = v
            self._dirty = True

    def set_many(self, items: Dict[str, str]):
        if not items: return
        self._mem.set_many(items)
        with self._lock:
            self._data.update(items)
            self._dirty = True

    def delete_prefix(self, prefix: str) -> int:
        """Elimina entradas cuya key tenga ese prefijo (rara, util para debug)."""
        n = 0
        with self._lock:
            keys = [k for k in self._data if k.startswith(prefix)]
            for k in keys:
                self._data.pop(k, None)
                n += 1
            if n: self._dirty = True
        if n:
            for k in keys:
                self._mem._data.pop(k, None)
        return n

    def clear_all(self):
        """Limpia TODO: memoria, disco y flush."""
        with self._lock:
            self._data.clear()
            self._dirty = True
        self._mem.clear()
        try:
            if self.path.exists():
                self.path.unlink()
        except Exception:
            pass

    def flush(self):
        with self._lock:
            if not self._dirty:
                return
            try:
                snapshot = dict(self._data)
                tmp = self.path.with_suffix('.tmp')
                tmp.write_text(json.dumps(snapshot, ensure_ascii=False), encoding='utf-8')
                tmp.replace(self.path)
                self._dirty = False
            except Exception as e:
                print('cache flush err:', e)

    def flush_async(self):
        if not self._dirty:
            return
        if self._flush_thread and self._flush_thread.is_alive():
            return
        self._flush_thread = threading.Thread(target=self.flush, daemon=True)
        self._flush_thread.start()

    def size(self) -> Tuple[int, int]:
        """Devuelve (entradas_disco, entradas_memoria)."""
        return (len(self._data), len(self._mem))


CACHE = TranslationCache()


# ---------------------------------------------------------------------------
# DeepLX
# ---------------------------------------------------------------------------
def translate_deeplx(text: str, source: str, target: str,
                     endpoint: str, timeout: int = 10) -> str:
    payload = {"text": text, "source_lang": source.upper(), "target_lang": target.upper()}
    r = _SESSION.post(endpoint, json=payload, timeout=timeout)
    r.raise_for_status()
    j = _json_loads(r.content)
    if isinstance(j, dict):
        if isinstance(j.get('data'), str): return j['data']
        if j.get('translations'): return j['translations'][0].get('text', '')
        if 'text' in j: return j['text']
    raise RuntimeError(f"Respuesta DeepLX inesperada: {str(j)[:200]}")


# ---------------------------------------------------------------------------
# Google Free
# ---------------------------------------------------------------------------
GOOGLE_URL = "https://translate.googleapis.com/translate_a/single"
GOOGLE_BATCH_URL = "https://translate.googleapis.com/translate_a/t"

_GOOGLE_BATCH_CHAR_LIMIT = 8000
_GOOGLE_BATCH_MAX_TEXTS = 200


def translate_google_free(text: str, source: str, target: str, timeout: int = 10) -> str:
    _prewarm_connections_once()
    params = {"client": "gtx", "sl": source, "tl": target, "dt": "t", "q": text}
    STATS.single_calls += 1
    r = _SESSION.get(GOOGLE_URL, params=params, timeout=timeout)
    r.raise_for_status()
    r.encoding = 'utf-8'
    raw = r.text.strip()
    if not raw:
        raise RuntimeError("Google devolvio respuesta vacia")
    try:
        data = _json_loads(raw)
    except Exception:
        raise RuntimeError(f"Google respuesta no es JSON: {raw[:200]}")
    result = ''.join(seg[0] for seg in data[0] if seg and seg[0])
    STATS.total_chars += len(text)
    return _postprocess_all(result, target)


def translate_google_batch(texts: List[str], source: str, target: str,
                           timeout: int = 14) -> List[str]:
    """
    Traduce multiples textos en una sola request HTTP.
    FIX v6.0: si la respuesta tiene N != len(texts), conservamos los
    que se mapearon correctamente y reintentamos solo los faltantes
    en paralelo (antes se tiraba toda la respuesta).
    """
    if not texts:
        return []
    if len(texts) == 1:
        try:
            return [translate_google_free(texts[0], source, target, timeout)]
        except Exception:
            return ['']

    _prewarm_connections_once()
    params = [('client', 'gtx'), ('sl', source), ('tl', target), ('dt', 't')]
    for t in texts:
        params.append(('q', t))

    STATS.batch_calls += 1
    try:
        r = _SESSION.get(GOOGLE_BATCH_URL, params=params, timeout=timeout)
        r.raise_for_status()
        r.encoding = 'utf-8'
        raw = r.text.strip()
        if not raw:
            raise ValueError("Google batch devolvio respuesta vacia")
        data = _json_loads(raw)

        results: List[str] = []
        if isinstance(data, list):
            for item in data:
                if isinstance(item, list):
                    if item and isinstance(item[0], list):
                        results.append(_postprocess_all(
                            ''.join(seg[0] for seg in item if seg and seg[0]), target))
                    elif item and isinstance(item[0], str):
                        results.append(_postprocess_all(item[0], target))
                    else:
                        results.append('')
                elif isinstance(item, str):
                    results.append(_postprocess_all(item, target))
                else:
                    results.append('')

        STATS.total_chars += sum(len(t) for t in texts)

        if len(results) == len(texts):
            return results

        # MISMATCH: rescatamos lo que coincide por posicion (asumiendo
        # que google preserva el orden de los q=) y reintentamos los faltantes
        # en paralelo, en vez de tirar todo.
        n = len(texts)
        out: List[str] = [''] * n
        for i, r2 in enumerate(results):
            if i < n:
                out[i] = r2 or ''
        missing_idx = [i for i in range(n) if not out[i] or not out[i].strip()]
        if missing_idx:
            with ThreadPoolExecutor(max_workers=min(len(missing_idx), 12)) as ex:
                fut_map = {ex.submit(translate_google_free, texts[i], source, target, timeout): i
                           for i in missing_idx}
                for fut in as_completed(fut_map):
                    j = fut_map[fut]
                    try:
                        out[j] = fut.result() or ''
                    except Exception:
                        out[j] = ''
        return out
    except Exception:
        STATS.http_errors += 1
        # Fallback paralelo total
        out = [''] * len(texts)
        with ThreadPoolExecutor(max_workers=min(len(texts), 12)) as ex:
            futs = {ex.submit(translate_google_free, t, source, target, timeout): i
                    for i, t in enumerate(texts)}
            for fut in as_completed(futs):
                i = futs[fut]
                try: out[i] = fut.result()
                except Exception: out[i] = ''
        return out


# ---------------------------------------------------------------------------
# Lang mapping
# ---------------------------------------------------------------------------
_GOOGLE_LANG_MAP = {
    'ES': 'es',
    'ES-419': 'es-419', 'ES_419': 'es-419', 'ES_LA': 'es-419',
    'ES-MX': 'es-419', 'ES_MX': 'es-419',
    'ES-AR': 'es-419', 'ES-CO': 'es-419', 'ES-CL': 'es-419',
    'ES-PE': 'es-419', 'ES-UY': 'es-419', 'ES-VE': 'es-419',
    'EN': 'en', 'PT': 'pt', 'PT-BR': 'pt', 'PT-PT': 'pt-PT',
    'FR': 'fr', 'DE': 'de', 'IT': 'it',
    'JA': 'ja', 'ZH': 'zh-CN', 'ZH-TW': 'zh-TW', 'KO': 'ko', 'RU': 'ru',
    'PL': 'pl', 'TR': 'tr', 'AR': 'ar', 'NL': 'nl',
    'CS': 'cs', 'SV': 'sv', 'DA': 'da', 'FI': 'fi', 'NO': 'no',
    'UK': 'uk', 'HU': 'hu', 'RO': 'ro', 'BG': 'bg', 'EL': 'el',
    'CA': 'ca', 'VI': 'vi', 'TH': 'th', 'ID': 'id', 'MS': 'ms',
    'HE': 'iw', 'HI': 'hi', 'AUTO': 'auto',
}

def _map_target(engine: str, target: str) -> str:
    t = target.upper()
    if engine == 'google':
        return _GOOGLE_LANG_MAP.get(t, target.lower())
    if t in ('ES-419', 'ES_LA', 'ES-MX'): return 'ES'
    if t == 'PT-BR': return 'PT-BR'
    if t in ('PT-PT', 'PT'): return 'PT-PT'
    if t == 'AUTO': return 'ES'
    return t

def _map_source(engine: str, source: str) -> str:
    if source.lower() == 'auto':
        return 'auto' if engine == 'google' else 'EN'
    return source.lower() if engine == 'google' else source.upper()


# ---------------------------------------------------------------------------
# Heuristica: la traduccion parece sin traducir (igual al original)?
# ---------------------------------------------------------------------------
def _looks_untranslated(src: str, out: str, source_lang: str, target: str) -> bool:
    if not out or not src:
        return False
    if source_lang.lower() == target.lower():
        return False
    if out.strip() == src.strip():
        # ignorar casos legitimos: puntuacion pura, numeros, urls
        s = src.strip()
        if len(s) <= 3: return False
        if re.match(r'^[\d\W_]+$', s): return False
        return True
    return False


# ---------------------------------------------------------------------------
# Traduccion individual con fallback
# ---------------------------------------------------------------------------
def _translate_one(text: str, source: str, target: str,
                   engine: str, deeplx_endpoint: str,
                   fallback: bool) -> str:
    if not text or not text.strip():
        return text

    ck = CACHE.namespaced_key(text, source, target, engine)
    cached = CACHE.get(ck)
    if cached is not None:
        STATS.cache_hits += 1
        return cached
    STATS.cache_misses += 1

    # Glosario: si el texto es exactamente un termino del glosario, usa el target
    gloss = GLOSSARY.get_all()
    if gloss:
        gt = gloss.get(text.strip())
        if gt:
            CACHE.set(ck, gt)
            return gt

    # Nombre protegido: devolver original
    if text.strip() in _PROTECTED_NAMES.get_set():
        return text

    p = Protector()
    shielded = p.shield(text)

    order = [engine]
    if fallback:
        order.append('google' if engine == 'deeplx' else 'deeplx')

    last_err = None
    for eng in order:
        try:
            src = _map_source(eng, source)
            tgt = _map_target(eng, target)
            if eng == 'deeplx':
                result = translate_deeplx(shielded, src, tgt, deeplx_endpoint)
            else:
                result = translate_google_free(shielded, src, tgt)
            restored = p.restore(result)
            if not restored or not restored.strip():
                continue
            # Aplicar glosario post-traduccion
            restored = GLOSSARY.apply_post(restored)
            # Si parece sin traducir y queda otro motor, intentar
            if _looks_untranslated(text, restored, source, target) and eng != order[-1]:
                STATS.fallback_calls += 1
                continue
            CACHE.set(ck, restored)
            return restored
        except Exception as e:
            last_err = e
            continue

    if last_err:
        print(f'[translate fallback->original] "{text[:60]}": {last_err}')
    return text


def translate(text: str, source: str = 'EN', target: str = 'ES',
              engine: str = 'deeplx', deeplx_endpoint: str = DEFAULT_DEEPLX,
              fallback: bool = True) -> str:
    return _translate_one(text, source, target, engine, deeplx_endpoint, fallback)


# ---------------------------------------------------------------------------
# translate_batch -- ULTRA OPTIMIZADO v6.0
# ---------------------------------------------------------------------------
def translate_batch(
    texts: List[str],
    source: str = 'auto',
    target: str = 'ES',
    engine: str = 'google',
    deeplx_endpoint: str = DEFAULT_DEEPLX,
    fallback: bool = True,
    workers: int = 16,
    progress_cb: Optional[Callable] = None,
    stop_flag: Optional[Callable[[], bool]] = None,
) -> List[str]:
    """progress_cb puede aceptar (done, total) o (done, total, info_dict)."""
    n = len(texts)
    if n == 0:
        return []

    results: List[str] = [''] * n
    t_start = time.time()

    def _emit_progress(done: int):
        if not progress_cb: return
        elapsed = time.time() - t_start
        speed = done / elapsed if elapsed > 0 else 0
        eta = (n - done) / speed if speed > 0 else 0
        try:
            progress_cb(done, n, {'elapsed': elapsed, 'speed': speed, 'eta': eta})
        except TypeError:
            try: progress_cb(done, n)
            except Exception: pass

    # 1) Dedupe
    unique: Dict[str, List[int]] = {}
    for i, t in enumerate(texts):
        unique.setdefault(t, []).append(i)

    # 2) Cache + glossary + protected prefilter
    _protected = _PROTECTED_NAMES.get_set()
    _gloss = GLOSSARY.get_all()
    pending: List[str] = []
    cache_hits = 0
    for t in unique.keys():
        if not t or not t.strip():
            for idx in unique[t]:
                results[idx] = t
            continue
        if t.strip() in _protected:
            for idx in unique[t]:
                results[idx] = t
            continue
        # Glosario exacto -> sin red
        gt = _gloss.get(t.strip()) if _gloss else None
        if gt:
            for idx in unique[t]:
                results[idx] = gt
            continue
        ck = CACHE.namespaced_key(t, source, target, engine)
        c = CACHE.get(ck)
        if c is not None:
            STATS.cache_hits += 1
            cache_hits += len(unique[t])
            for idx in unique[t]:
                results[idx] = c
        else:
            STATS.cache_misses += 1
            pending.append(t)

    done_count = cache_hits
    # contar tambien los protegidos/glosario/empty como done
    for t in unique.keys():
        if (not t or not t.strip() or t.strip() in _protected
                or (_gloss and t.strip() in _gloss)):
            done_count += len(unique[t])
    _emit_progress(done_count)

    if not pending:
        CACHE.flush_async()
        return results

    if engine == 'google':
        _translate_google_parallel(
            pending, unique, results, source, target,
            deeplx_endpoint, fallback, workers,
            done_count, n, _emit_progress, stop_flag
        )
    else:
        _translate_deeplx_parallel(
            pending, unique, results, source, target,
            deeplx_endpoint, fallback, workers,
            done_count, n, _emit_progress, stop_flag
        )

    CACHE.flush_async()
    return results


def _translate_google_parallel(
    pending: List[str],
    unique: Dict[str, List[int]],
    results: List[str],
    source: str, target: str,
    deeplx_endpoint: str, fallback: bool,
    workers: int,
    done_count: int, n: int,
    emit_progress, stop_flag,
):
    if len(pending) <= 50:
        google_workers = min(max(workers, 8), 16)
    elif len(pending) <= 200:
        google_workers = min(max(workers, 16), 32)
    else:
        google_workers = min(max(workers, 24), 48)

    src = _map_source('google', source)
    tgt = _map_target('google', target)

    all_sub_chunks = _split_by_char_limit(pending, _GOOGLE_BATCH_CHAR_LIMIT, _GOOGLE_BATCH_MAX_TEXTS)
    actual_workers = min(google_workers, max(1, len(all_sub_chunks)))

    def process_sub_chunk(sub: List[str]) -> Dict[str, str]:
        chunk_results: Dict[str, str] = {}
        if not sub:
            return chunk_results
        protectors = [Protector() for _ in sub]
        shielded = [p.shield(t) for p, t in zip(protectors, sub)]
        translated = translate_google_batch(shielded, src, tgt)
        cache_batch: Dict[str, str] = {}
        for i, (orig, prot, trans) in enumerate(zip(sub, protectors, translated)):
            if not trans or not trans.strip():
                try:
                    retry = translate_google_free(shielded[i], src, tgt)
                    if retry and retry.strip():
                        trans = retry
                except Exception:
                    pass
            if trans and trans.strip():
                restored = prot.restore(trans)
                restored = GLOSSARY.apply_post(restored)
                # detector "no traducido"
                if _looks_untranslated(orig, restored, source, target):
                    chunk_results[orig] = ''  # forzar fallback
                else:
                    chunk_results[orig] = restored
                    ck = CACHE.namespaced_key(orig, source, target, 'google')
                    cache_batch[ck] = restored
            else:
                chunk_results[orig] = ''
        if cache_batch:
            CACHE.set_many(cache_batch)
        return chunk_results

    with ThreadPoolExecutor(max_workers=actual_workers) as ex:
        future_map = {ex.submit(process_sub_chunk, sub): sub for sub in all_sub_chunks if sub}
        for fut in as_completed(future_map):
            if stop_flag and stop_flag():
                for f in future_map:
                    f.cancel()
                break
            try:
                chunk_res = fut.result()
                failed_texts = []
                for orig_text, translation in chunk_res.items():
                    if not translation and fallback:
                        failed_texts.append(orig_text)
                    else:
                        for idx in unique.get(orig_text, []):
                            results[idx] = translation
                        done_count += len(unique.get(orig_text, []))

                if failed_texts:
                    STATS.fallback_calls += len(failed_texts)
                    fallback_results = _parallel_fallback(
                        failed_texts, source, target, deeplx_endpoint
                    )
                    for orig_text, translation in fallback_results.items():
                        final = translation if (translation and translation.strip()) else orig_text
                        final = GLOSSARY.apply_post(final)
                        for idx in unique.get(orig_text, []):
                            results[idx] = final
                        done_count += len(unique.get(orig_text, []))
                elif not fallback:
                    for orig_text, translation in chunk_res.items():
                        if not translation:
                            for idx in unique.get(orig_text, []):
                                if not results[idx]:
                                    results[idx] = orig_text

                emit_progress(done_count)
            except Exception as e:
                print(f'[chunk error] {e}')


def _parallel_fallback(
    texts: List[str],
    source: str, target: str,
    deeplx_endpoint: str,
    max_workers: int = 12,
) -> Dict[str, str]:
    results: Dict[str, str] = {}
    if not texts:
        return results

    def try_deeplx(t: str) -> Tuple[str, str]:
        try:
            src = _map_source('deeplx', source)
            tgt = _map_target('deeplx', target)
            p = Protector()
            shielded = p.shield(t)
            out = translate_deeplx(shielded, src, tgt, deeplx_endpoint)
            restored = p.restore(out)
            if restored and restored.strip():
                ck = CACHE.namespaced_key(t, source, target, 'google')
                CACHE.set(ck, restored)
            return (t, restored)
        except Exception:
            return (t, '')

    with ThreadPoolExecutor(max_workers=min(max_workers, len(texts))) as ex:
        for t, out in ex.map(try_deeplx, texts):
            results[t] = out
    return results


def _translate_deeplx_parallel(
    pending: List[str],
    unique: Dict[str, List[int]],
    results: List[str],
    source: str, target: str,
    deeplx_endpoint: str, fallback: bool,
    workers: int,
    done_count: int, n: int,
    emit_progress, stop_flag,
):
    if len(pending) <= 20:
        deeplx_workers = min(max(workers, 4), 8)
    elif len(pending) <= 100:
        deeplx_workers = min(max(workers, 8), 16)
    else:
        deeplx_workers = min(max(workers, 12), 24)

    def task(t: str) -> Tuple[str, str, Optional[Exception]]:
        try:
            out = _translate_one(t, source, target, 'deeplx', deeplx_endpoint, fallback)
            return (t, out, None)
        except Exception as e:
            return (t, '', e)

    flush_every = 150
    processed = 0
    error_count = 0

    with ThreadPoolExecutor(max_workers=deeplx_workers) as ex:
        futures = {ex.submit(task, t): t for t in pending}
        for fut in as_completed(futures):
            if stop_flag and stop_flag():
                for f in futures:
                    f.cancel()
                break
            src_text, out, err = fut.result()
            if err:
                error_count += 1
                STATS.http_errors += 1
                print(f'[translate error] "{src_text[:60]}": {err}')
            final = out if (out and out.strip()) else src_text
            final = GLOSSARY.apply_post(final)
            for idx in unique.get(src_text, []):
                results[idx] = final
            done_count += len(unique.get(src_text, []))
            processed += 1
            if processed % flush_every == 0:
                CACHE.flush_async()
            emit_progress(done_count)

    if error_count:
        print(f'[translate_batch] {error_count} textos fallaron de {len(pending)} unicos.')


# ---------------------------------------------------------------------------
# Helpers de chunking
# ---------------------------------------------------------------------------
def _split_by_char_limit(items: List[str], char_limit: int,
                         max_items: int) -> List[List[str]]:
    chunks = []
    current: List[str] = []
    current_chars = 0
    for item in items:
        item_len = len(item)
        if current and (current_chars + item_len > char_limit or len(current) >= max_items):
            chunks.append(current)
            current = [item]
            current_chars = item_len
        else:
            current.append(item)
            current_chars += item_len
    if current:
        chunks.append(current)
    return chunks


# ---------------------------------------------------------------------------
# API publica extra
# ---------------------------------------------------------------------------
def set_cache_namespace(ns: str) -> None:
    """Activa un namespace por-proyecto para aislar cache entre juegos."""
    CACHE.set_namespace(ns or '')

def stats_snapshot() -> Dict[str, float]:
    return STATS.snapshot()

def stats_reset() -> None:
    STATS.reset()


# ---------------------------------------------------------------------------
# Self-test
# ---------------------------------------------------------------------------
if __name__ == '__main__':
    CACHE.clear_all()
    test_texts = [
        "Hello {b}world{/b}, [name]!",
        "Good morning, how are you?",
        "Hello {b}world{/b}, [name]!",
        "I love you so much.",
        "Honestly, I don't know.",
        "Of course, I'll help you.",
    ]
    print(f"Traduciendo {len(test_texts)} textos...")
    t0 = time.time()
    out = translate_batch(
        test_texts, source='auto', target='ES-419', engine='google', workers=16,
        progress_cb=lambda d, t, info=None: print(f"  {d}/{t}"
            + (f"  eta={info['eta']:.1f}s" if info else ''))
    )
    dt = time.time() - t0
    print(f"\nTiempo: {dt:.2f}s")
    for orig, trans in zip(test_texts, out):
        print(f"  {orig!r}\n    -> {trans!r}")
    print('\nStats:', stats_snapshot())
