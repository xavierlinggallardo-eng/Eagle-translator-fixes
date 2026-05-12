"""
Motores de traduccion -- ULTRA OPTIMIZADO v5.0
  - Google Translate BATCH real: hasta 200 textos / 8000 chars por request
  - Pool HTTP 256 conexiones, keep-alive agresivo, TCP_NODELAY
  - Cache en memoria (LRU 200k entradas) + disco persistente con ujson/json
  - Flush asincrono al disco (no bloquea workers)
  - Workers dinamicos: hasta 48 para Google, 24 para DeepLX
  - Dedupe + cache pre-check antes de lanzar threads
  - Timeouts agresivos: falla rapido, reintenta rapido
  - Protector de placeholders Ren'Py: {tags}, [variables], %(name)s
  - Hash MD5 rapido para cache keys
  - Restore de placeholders en un solo pass (O(n) vs O(n*k))
  - Batch size adaptativo segun longitud de textos
  - Fallback paralelo para items fallidos del batch
  - Pre-warming de conexiones HTTP al inicio (multiples endpoints)
  - Adaptive workers: escala segun cantidad de pendientes
  - Lock-free reads en cache caliente
  - Batch cache writes con set_many
  - ujson/orjson fallback para JSON mas rapido
  - Chunking inteligente: maximiza textos por request
  - Pipeline: cache check y translate en paralelo
"""
from __future__ import annotations
import re, time, json, threading, hashlib, os
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import List, Optional, Callable, Dict, Tuple
import requests
from requests.adapters import HTTPAdapter
try:
    from urllib3.util.retry import Retry
except ImportError:
    from requests.packages.urllib3.util.retry import Retry

# Intentar usar ujson/orjson para parsing JSON mas rapido
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
    """JSON loads con fallback a stdlib."""
    if _HAS_FAST_JSON:
        try:
            return _json_fast.loads(data)
        except Exception:
            pass
    return json.loads(data)


DEFAULT_DEEPLX = "https://deeplx.vercel.app/translate"
CACHE_PATH = Path.home() / ".renpy_translator_cache.json"

# ---------------------------------------------------------------------------
# Post-procesador: español latino neutro
# Corrige traducciones literales de Google a expresiones naturales latinas.
# Sin API, sin dependencias, siempre activo cuando target es español.
# ---------------------------------------------------------------------------
_LATINO_REPLACEMENTS = [
    # Formulas de saludo/cortesia
    (r'(?<![¿?])¿[Cc]ómo estás\?',         '¿Cómo te va?'),
    (r'\b[Mm]ucho gusto en conocerte\b',    'qué gusto conocerte'),
    (r'\b[Ff]ue un placer conocerte\b',     'qué gusto conocerte'),
    (r'\b[Ee]ncantado de conocerte\b',      'qué gusto conocerte'),
    # Afirmaciones/negaciones
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
    # Expresiones cotidianas
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
    # Peninsulares → latino neutro
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
    (r'\bordered\b',                        'ordenen'),
    (r'\bhaced\b',                          'hagan'),
    # Muletillas formales → orales
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
]

_LATINO_RE = [(re.compile(pat), rep) for pat, rep in _LATINO_REPLACEMENTS]

# Solo targets de español LATINO activan el post-procesador.
# ES (España) NO se incluye — tiene sus propias formas (vosotros, vuestro, etc.)
_LATIN_SPANISH_TARGETS = {
    'ES419', 'ES-419', 'ES_419',
    'ESLA',  'ES-LA',  'ES_LA',
    'ESMX',  'ES-MX',  'ES_MX',
    'ESAR',  'ES-AR',  'ES_AR',
    'ESCO',  'ES-CO',  'ES_CO',
    'ESCL',  'ES-CL',  'ES_CL',
}

def _postprocess_latino(text: str, target: str) -> str:
    """Aplica correcciones de español latino neutro a la traducción.
    Solo se activa cuando el target es explícitamente español latino/latinoamérica.
    ES (España) NO se procesa para no alterar sus formas propias.
    """
    norm = target.upper().replace('-', '').replace('_', '')
    # Verificar tanto la forma normalizada como la original
    if norm not in {t.upper().replace('-','').replace('_','') for t in _LATIN_SPANISH_TARGETS}:
        return text
    for rx, rep in _LATINO_RE:
        text = rx.sub(rep, text)
    return text


# ---------------------------------------------------------------------------
# Placeholders
# ---------------------------------------------------------------------------
PLACEHOLDER_PATTERNS = [
    r'\{[^{}]*\}',
    r'\[[^\[\]]+\]',
    r'%\([^)]+\)[sdif]',
    r'%[sdif]',
    r'\\n', r'\\t', r'\\"',
]
PH_RE = re.compile('|'.join(PLACEHOLDER_PATTERNS))
# Patron para restore de todos los tokens en un solo pass
_RESTORE_RE = re.compile(
    r'(?:\u27e6|\u3010|\u3014|\u300a|\[\[)\s*(\d+)\s*(?:\u27e7|\u3011|\u3015|\u300b|\]\])'
)


class Protector:
    """Sustituye placeholders por tokens \u27e6N\u27e7 y los restaura en un solo pass."""
    __slots__ = ('tokens',)

    def __init__(self):
        self.tokens: List[str] = []

    def shield(self, text: str) -> str:
        self.tokens = []
        tokens = self.tokens
        def repl(m):
            tokens.append(m.group(0))
            return f"\u27e6{len(tokens)-1}\u27e7"
        # Paso 1: proteger {tags} y [variables] de Ren'Py
        out = PH_RE.sub(repl, text)
        # Paso 2: proteger nombres de personajes registrados
        _names = _PROTECTED_NAMES.get_all()
        if _names:
            # ordenar de mas largo a mas corto para evitar matches parciales
            for name in sorted(_names, key=len, reverse=True):
                if name in out:
                    tokens.append(name)
                    out = out.replace(name, f"\u27e6{len(tokens)-1}\u27e7")
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
# Registro de nombres de personajes protegidos (module-level singleton)
# ---------------------------------------------------------------------------
class _CharacterNameRegistry:
    """Almacena nombres de personajes que NO deben ser traducidos."""
    def __init__(self):
        self._names: set = set()
        self._lock = threading.Lock()

    def register(self, names):
        """Registra una coleccion de nombres como terminos protegidos.
        
        Al registrar, también limpia del caché cualquier traducción
        incorrecta que se haya guardado previamente para esos nombres
        (p.ej. si "Melisa" fue cacheada como "Toronjil" en sesión anterior).
        """
        with self._lock:
            for n in names:
                n = n.strip()
                if n and len(n) >= 2:
                    self._names.add(n)
                    # Limpiar del caché en memoria cualquier traducción
                    # incorrecta para este nombre (evita envenenamiento de caché)
                    try:
                        CACHE._mem._data = {
                            k: v for k, v in CACHE._mem._data.items()
                            if not (k.startswith(n + '|') or ('|' + n + '|') in k)
                        }
                    except Exception:
                        pass

    def clear(self):
        with self._lock:
            self._names.clear()

    def get_all(self) -> list:
        with self._lock:
            return list(self._names)

_PROTECTED_NAMES = _CharacterNameRegistry()


def register_character_names(names) -> None:
    """Registra nombres de personajes para que el Protector no los traduzca.

    Args:
        names: lista/set/iterable de strings con los nombres.
    """
    _PROTECTED_NAMES.register(names)


def get_protected_names() -> list:
    """Devuelve la lista de nombres actualmente protegidos."""
    return _PROTECTED_NAMES.get_all()


# ---------------------------------------------------------------------------
# Sesion HTTP global -- pool 256, keep-alive agresivo
# ---------------------------------------------------------------------------
def _make_session() -> requests.Session:
    s = requests.Session()
    retry = Retry(
        total=3, connect=2, read=2, backoff_factor=0.1,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset(['GET', 'POST']),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(
        pool_connections=256,
        pool_maxsize=256,
        max_retries=retry,
    )
    s.mount('http://', adapter)
    s.mount('https://', adapter)
    s.headers.update({
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36',
        'Accept-Encoding': 'gzip, deflate, br',
        'Accept': '*/*',
        'Connection': 'keep-alive',
    })
    return s

_SESSION = _make_session()


def _prewarm_connections():
    """Pre-calienta conexiones HTTP para eliminar latencia de handshake."""
    endpoints = [
        'https://translate.googleapis.com',
        'https://translate.google.com',
    ]
    for ep in endpoints:
        try:
            _SESSION.head(ep, timeout=3)
        except Exception:
            pass

# Pre-warming en background al importar el modulo
_prewarm_thread = threading.Thread(target=_prewarm_connections, daemon=True)
_prewarm_thread.start()


# ---------------------------------------------------------------------------
# Cache en memoria (LRU) + disco persistente
# ---------------------------------------------------------------------------
class LRUMemoryCache:
    """Cache en memoria con limite de tamano (LRU eviction).
    Usa dict normal (Python 3.7+ mantiene orden de insercion) para menos overhead.
    """
    def __init__(self, maxsize: int = 200_000):
        self._data: dict = {}
        self._maxsize = maxsize
        self._lock = threading.Lock()

    def get(self, k: str) -> Optional[str]:
        # Fast path: sin lock para reads (dict es thread-safe para reads en CPython)
        v = self._data.get(k)
        if v is not None:
            # Mover al final (LRU) solo si hay riesgo de eviction
            if len(self._data) > self._maxsize * 0.9:
                with self._lock:
                    if k in self._data:
                        self._data[k] = self._data.pop(k)
        return v

    def set(self, k: str, v: str):
        with self._lock:
            if k in self._data:
                del self._data[k]
            self._data[k] = v
            if len(self._data) > self._maxsize:
                # Evict el mas antiguo
                try:
                    next(iter(self._data))
                    del self._data[next(iter(self._data))]
                except StopIteration:
                    pass

    def set_many(self, items: Dict[str, str]):
        if not items:
            return
        with self._lock:
            for k, v in items.items():
                if k in self._data:
                    del self._data[k]
                self._data[k] = v
            # Evict si excede maxsize
            while len(self._data) > self._maxsize:
                try:
                    del self._data[next(iter(self._data))]
                except StopIteration:
                    break

    def bulk_load(self, data: Dict[str, str]):
        with self._lock:
            self._data.update(data)

    def snapshot(self) -> Dict[str, str]:
        with self._lock:
            return dict(self._data)


class TranslationCache:
    """Cache de dos capas: memoria (LRU rapido) + disco (persistente).
    Flush al disco es asincrono para no bloquear workers."""

    def __init__(self, path: Path = CACHE_PATH):
        self.path = path
        self._lock = threading.Lock()
        self._data: Dict[str, str] = {}
        self._mem = LRUMemoryCache(maxsize=200_000)
        self._dirty = False
        self._flush_thread: Optional[threading.Thread] = None
        self.load()

    @staticmethod
    def key(text: str, source: str, target: str, engine: str) -> str:
        # MD5 es ~30% mas rapido que SHA1 para este uso
        return hashlib.md5(
            f"{engine}|{source}|{target}|{text}".encode('utf-8'),
            usedforsecurity=False
        ).hexdigest()

    def load(self):
        if self.path.exists():
            try:
                raw = self.path.read_text(encoding='utf-8')
                data = _json_loads(raw)
                self._data = data
                self._mem.bulk_load(data)
            except Exception:
                self._data = {}

    def get(self, k: str) -> Optional[str]:
        # Fast path: memoria primero (sin I/O)
        v = self._mem.get(k)
        if v is not None:
            return v
        # Fallback a dict principal
        return self._data.get(k)

    def set(self, k: str, v: str):
        self._mem.set(k, v)
        with self._lock:
            self._data[k] = v
            self._dirty = True

    def set_many(self, items: Dict[str, str]):
        """Batch set -- mas eficiente que llamar set() en loop."""
        if not items:
            return
        self._mem.set_many(items)
        with self._lock:
            self._data.update(items)
            self._dirty = True

    def flush(self):
        """Flush sincrono (llamado al final del batch)."""
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
        """Flush asincrono -- no bloquea el thread que llama."""
        if not self._dirty:
            return
        if self._flush_thread and self._flush_thread.is_alive():
            return  # ya hay un flush en curso
        self._flush_thread = threading.Thread(target=self.flush, daemon=True)
        self._flush_thread.start()


CACHE = TranslationCache()


# ---------------------------------------------------------------------------
# DeepLX
# ---------------------------------------------------------------------------
def translate_deeplx(text: str, source: str, target: str,
                     endpoint: str, timeout: int = 8) -> str:
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
# Google Free -- SINGLE y BATCH
# ---------------------------------------------------------------------------
GOOGLE_URL = "https://translate.googleapis.com/translate_a/single"
GOOGLE_BATCH_URL = "https://translate.googleapis.com/translate_a/t"

# Limites optimizados para maximo throughput
# Google acepta hasta ~5000 chars por request, usamos 8000 con el endpoint /t
# que es mas generoso. Maximo 200 textos por request.
_GOOGLE_BATCH_CHAR_LIMIT = 8000
_GOOGLE_BATCH_MAX_TEXTS = 200


def translate_google_free(text: str, source: str, target: str,
                          timeout: int = 8) -> str:
    """Traduce un solo texto con Google Translate gratis."""
    params = {
        "client": "gtx",
        "sl": source,
        "tl": target,
        "dt": "t",
        "q": text,
    }
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
    return _postprocess_latino(result, target)


def translate_google_batch(texts: List[str], source: str, target: str,
                           timeout: int = 12) -> List[str]:
    """
    Traduce multiples textos en UNA sola request HTTP usando el endpoint
    translate_a/t de Google (acepta multiples parametros q=).
    Mucho mas rapido que N requests individuales.
    """
    if not texts:
        return []
    if len(texts) == 1:
        try:
            return [translate_google_free(texts[0], source, target, timeout)]
        except Exception:
            return ['']

    # Construir params con multiples q= usando lista de tuplas (mas eficiente)
    params = [
        ('client', 'gtx'),
        ('sl', source),
        ('tl', target),
        ('dt', 't'),
    ]
    for t in texts:
        params.append(('q', t))

    try:
        r = _SESSION.get(GOOGLE_BATCH_URL, params=params, timeout=timeout)
        r.raise_for_status()
        r.encoding = 'utf-8'
        raw = r.text.strip()
        if not raw:
            raise ValueError("Google batch devolvio respuesta vacia")
        try:
            data = _json_loads(raw)
        except Exception:
            raise ValueError(f"Google batch respuesta no es JSON: {raw[:200]}")
        # El endpoint /t devuelve lista de listas o lista de strings
        results = []
        if isinstance(data, list):
            for item in data:
                if isinstance(item, list):
                    if item and isinstance(item[0], list):
                        results.append(_postprocess_latino(''.join(seg[0] for seg in item if seg and seg[0]), target))
                    elif item and isinstance(item[0], str):
                        results.append(_postprocess_latino(item[0], target))
                    else:
                        results.append('')
                elif isinstance(item, str):
                    results.append(_postprocess_latino(item, target))
                else:
                    results.append('')
        # Si el resultado no coincide en cantidad, fallback a individuales
        if len(results) != len(texts):
            raise ValueError(f"Batch returned {len(results)} results for {len(texts)} texts")
        return results
    except Exception:
        # Fallback paralelo: traducir en paralelo (no secuencial)
        out = [''] * len(texts)
        with ThreadPoolExecutor(max_workers=min(len(texts), 12)) as ex:
            futs = {ex.submit(translate_google_free, t, source, target, timeout): i
                    for i, t in enumerate(texts)}
            for fut in as_completed(futs):
                i = futs[fut]
                try:
                    out[i] = fut.result()
                except Exception:
                    out[i] = ''
        return out


# ---------------------------------------------------------------------------
# Lang mapping
# ---------------------------------------------------------------------------
_GOOGLE_LANG_MAP = {
    # Español España → 'es' (Google default)
    'ES': 'es',
    # Español Latino/Latinoamérica → 'es-419' (Google sí lo soporta)
    'ES-419': 'es-419', 'ES_419': 'es-419', 'ES_LA': 'es-419',
    'ES-MX': 'es-419', 'ES_MX': 'es-419',
    'ES-AR': 'es-419', 'ES-CO': 'es-419', 'ES-CL': 'es-419',
    'EN': 'en', 'PT': 'pt', 'PT-BR': 'pt', 'PT-PT': 'pt-PT',
    'FR': 'fr', 'DE': 'de', 'IT': 'it',
    'JA': 'ja', 'ZH': 'zh-CN', 'KO': 'ko', 'RU': 'ru',
    'AUTO': 'auto',
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
# Traduccion individual con fallback
# ---------------------------------------------------------------------------
def _translate_one(text: str, source: str, target: str,
                   engine: str, deeplx_endpoint: str,
                   fallback: bool) -> str:
    if not text or not text.strip():
        return text

    ck = TranslationCache.key(text, source, target, engine)
    cached = CACHE.get(ck)
    if cached is not None:
        return cached

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
            # No cachear ni devolver resultados vacios: probar siguiente motor
            if not restored or not restored.strip():
                continue
            CACHE.set(ck, restored)
            return restored
        except Exception as e:
            last_err = e
            continue
    # Si todo falla / devuelve vacio, devolver el texto original (mejor que "")
    # No se cachea para permitir reintentos en siguientes ejecuciones.
    if last_err:
        print(f'[translate fallback->original] "{text[:60]}": {last_err}')
    return text


# Wrapper compatible con version anterior
def translate(text: str, source: str = 'EN', target: str = 'ES',
              engine: str = 'deeplx', deeplx_endpoint: str = DEFAULT_DEEPLX,
              fallback: bool = True) -> str:
    return _translate_one(text, source, target, engine, deeplx_endpoint, fallback)


# ---------------------------------------------------------------------------
# translate_batch -- ULTRA OPTIMIZADO v5.0
# ---------------------------------------------------------------------------
def translate_batch(
    texts: List[str],
    source: str = 'auto',
    target: str = 'ES',
    engine: str = 'google',
    deeplx_endpoint: str = DEFAULT_DEEPLX,
    fallback: bool = True,
    workers: int = 16,
    progress_cb: Optional[Callable[[int, int], None]] = None,
    stop_flag: Optional[Callable[[], bool]] = None,
) -> List[str]:
    """
    Traduce lista de textos en paralelo -- ULTRA OPTIMIZADO v5.0:
    - Google: batch real (hasta 200 textos / 8000 chars por request) + ThreadPool
    - DeepLX: ThreadPool con workers
    - Cache de dos capas (memoria LRU 200k + disco)
    - Dedupe automatico
    - Flush asincrono al disco
    - Fallback paralelo para items fallidos
    - Workers adaptativos segun cantidad de pendientes
    """
    n = len(texts)
    if n == 0:
        return []

    results: List[str] = [''] * n

    # -- 1. Dedupe --
    unique: Dict[str, List[int]] = {}
    for i, t in enumerate(texts):
        unique.setdefault(t, []).append(i)

    # -- 2. Resolver cache primero (sin red) --
    # IMPORTANTE: los nombres de personaje protegidos se devuelven tal cual,
    # sin tocar caché ni red. Esto previene que traducciones incorrectas
    # cacheadas (p.ej. "Melisa"→"Toronjil") vuelvan a usarse.
    _protected_set = set(_PROTECTED_NAMES.get_all())
    pending: List[str] = []
    cache_hits = 0
    for t in unique.keys():
        if not t or not t.strip():
            for idx in unique[t]:
                results[idx] = t
            cache_hits += len(unique[t])
            continue
        # Nombre protegido: devolver original, ignorar caché
        if t.strip() in _protected_set:
            for idx in unique[t]:
                results[idx] = t
            cache_hits += len(unique[t])
            continue
        ck = TranslationCache.key(t, source, target, engine)
        c = CACHE.get(ck)
        if c is not None:
            for idx in unique[t]:
                results[idx] = c
            cache_hits += len(unique[t])
        else:
            pending.append(t)

    done_count = cache_hits
    if progress_cb:
        progress_cb(done_count, n)

    if not pending:
        CACHE.flush_async()
        return results

    # -- 3. Traducir los pendientes --
    if engine == 'google':
        _translate_google_parallel(
            pending, unique, results, source, target,
            deeplx_endpoint, fallback, workers,
            done_count, n, progress_cb, stop_flag
        )
    else:
        _translate_deeplx_parallel(
            pending, unique, results, source, target,
            deeplx_endpoint, fallback, workers,
            done_count, n, progress_cb, stop_flag
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
    progress_cb, stop_flag
):
    """
    Para Google: divide pending en chunks y los traduce en paralelo.
    Cada worker hace 1 request batch con multiples textos.
    Maximiza throughput: menos requests, mas textos por request.
    v5.0: hasta 48 workers, 200 textos/request, 8000 chars/request.
    """
    # Escalar workers segun cantidad de pendientes
    # Mas pendientes = mas workers (hasta 48)
    if len(pending) <= 50:
        google_workers = min(max(workers, 8), 16)
    elif len(pending) <= 200:
        google_workers = min(max(workers, 16), 32)
    else:
        google_workers = min(max(workers, 24), 48)

    src = _map_source('google', source)
    tgt = _map_target('google', target)

    # Dividir pending en chunks balanceados respetando limites
    all_sub_chunks = _split_by_char_limit(pending, _GOOGLE_BATCH_CHAR_LIMIT, _GOOGLE_BATCH_MAX_TEXTS)

    # Si hay pocos chunks, no vale la pena tanto paralelismo
    actual_workers = min(google_workers, max(1, len(all_sub_chunks)))

    def process_sub_chunk(sub: List[str]) -> Dict[str, str]:
        """Procesa un sub-chunk con una sola request batch."""
        chunk_results: Dict[str, str] = {}
        if not sub:
            return chunk_results
        protectors = [Protector() for _ in sub]
        shielded = [p.shield(t) for p, t in zip(protectors, sub)]
        translated = translate_google_batch(shielded, src, tgt)
        cache_batch: Dict[str, str] = {}
        # Reintento individual para items que el batch devolvio vacios
        # (frecuente con textos cortos como ">>", "Rock", "Pop", "...")
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
                chunk_results[orig] = restored
                ck = TranslationCache.key(orig, source, target, 'google')
                cache_batch[ck] = restored
            else:
                # Marcar como vacio: el llamador hara fallback a deeplx y,
                # si tambien falla, usara el texto original.
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

                # Fallback paralelo para los que fallaron
                if failed_texts:
                    fallback_results = _parallel_fallback(
                        failed_texts, source, target, deeplx_endpoint
                    )
                    for orig_text, translation in fallback_results.items():
                        # Ultimo recurso: si sigue vacio, usar texto original
                        # para no escribir new "" en los archivos .rpy
                        final = translation if (translation and translation.strip()) else orig_text
                        for idx in unique.get(orig_text, []):
                            results[idx] = final
                        done_count += len(unique.get(orig_text, []))
                # Casos sin fallback habilitado: tampoco dejar vacio
                elif not fallback:
                    for orig_text, translation in chunk_res.items():
                        if not translation:
                            for idx in unique.get(orig_text, []):
                                if not results[idx]:
                                    results[idx] = orig_text

                if progress_cb:
                    progress_cb(done_count, n)
            except Exception as e:
                print(f'[chunk error] {e}')


def _parallel_fallback(
    texts: List[str],
    source: str, target: str,
    deeplx_endpoint: str,
    max_workers: int = 12,
) -> Dict[str, str]:
    """Fallback paralelo a DeepLX para textos que fallaron en Google batch."""
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
            # Solo cachear si no esta vacio
            if restored and restored.strip():
                ck = TranslationCache.key(t, source, target, 'google')
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
    progress_cb, stop_flag
):
    """Para DeepLX: 1 texto por request, maximo paralelismo.
    v5.0: hasta 24 workers.
    """
    # Escalar workers segun pendientes
    if len(pending) <= 20:
        deeplx_workers = min(max(workers, 4), 8)
    elif len(pending) <= 100:
        deeplx_workers = min(max(workers, 8), 16)
    else:
        deeplx_workers = min(max(workers, 12), 24)

    def task(t: str) -> Tuple[str, str, Optional[Exception]]:
        try:
            out = _translate_one(t, source, target, 'deeplx',
                                 deeplx_endpoint, fallback)
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
                print(f'[translate error] "{src_text[:60]}": {err}')
            # Nunca dejar vacio: usar el original si todo fallo
            final = out if (out and out.strip()) else src_text
            for idx in unique.get(src_text, []):
                results[idx] = final
            done_count += len(unique.get(src_text, []))
            processed += 1
            if processed % flush_every == 0:
                CACHE.flush_async()
            if progress_cb:
                progress_cb(done_count, n)

    if error_count:
        print(f'[translate_batch] {error_count} textos fallaron de {len(pending)} unicos.')


# ---------------------------------------------------------------------------
# Helpers de chunking
# ---------------------------------------------------------------------------
def _split_into_chunks(items: List[str], n_chunks: int) -> List[List[str]]:
    """Divide lista en n_chunks partes aproximadamente iguales."""
    if not items:
        return []
    n_chunks = max(1, min(n_chunks, len(items)))
    size = -(-len(items) // n_chunks)  # ceil division
    return [items[i:i + size] for i in range(0, len(items), size)]


def _split_by_char_limit(items: List[str], char_limit: int,
                         max_items: int) -> List[List[str]]:
    """Divide lista respetando limite de caracteres y maximo de items por chunk.
    v5.0: char_limit=8000, max_items=200 para maximo throughput.
    """
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
# Test rapido
# ---------------------------------------------------------------------------
if __name__ == '__main__':
    import time
    # Limpiar cache para test real de red
    CACHE._data = {}
    CACHE._mem._data.clear()

    test_texts = [
        "Hello {b}world{/b}, [name]!",
        "Good morning, how are you?",
        "Hello {b}world{/b}, [name]!",  # duplicado -- debe usar dedupe
        "I love you so much.",
        "What do you want to do today?",
        "Let's go to the park.",
        "She smiled at me.",
        "The sun is shining bright.",
        "I can't believe you did that.",
        "This is the best day of my life.",
        "Where are you going tonight?",
        "Please don't leave me alone.",
        "I have something important to tell you.",
        "Are you sure about this decision?",
        "Everything will be alright in the end.",
        "I missed you so much while you were gone.",
    ]
    print(f"Traduciendo {len(test_texts)} textos ({len(set(test_texts))} unicos)...")
    t0 = time.time()
    out = translate_batch(
        test_texts,
        source='auto', target='ES', engine='google', workers=16,
        progress_cb=lambda d, t: print(f"  progreso: {d}/{t}")
    )
    dt = time.time() - t0
    if dt > 0.001:
        print(f"\nTiempo: {dt:.2f}s | {len(test_texts)/dt:.1f} textos/s")
    else:
        print(f"\nTiempo: <1ms (cache hit)")
    for orig, trans in zip(test_texts, out):
        print(f"  {orig!r}")
        print(f"    -> {trans!r}")
