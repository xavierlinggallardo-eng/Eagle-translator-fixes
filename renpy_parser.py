"""
Ren'Py .rpy parser & writer — versión 2.1 (detección completa de mensajes phone/SMS/chat).

Modos:
  A) MODO TL EXISTENTE  (parse_directory): lee bloques `translate <lang> <id>:`
     y `translate <lang> strings:` con old/new (compat. con la versión 1).

  B) MODO SOURCE (extract_source_directory): escanea TODOS los .rpy fuente
     (game/) y extrae todo lo traducible, esté o no en formato translate:
       - Diálogos `say`:           character "texto"   /   "texto"
       - Bloques narrator:         "texto sin speaker"
       - Opciones de menú:         "Opción":   (dentro de  menu:)
       - UI de pantallas:          text "..."  /  textbutton "..."  /  label "..."
                                   tooltip "..."  /  caption "..."  /  hint "..."
       - gui.* strings:            gui.foo = "..."   /   define gui.x = "..."
       - Definiciones:             define char = Character("Nombre", ...)
       - Variables string sueltas: define x = "..."   $ x = "..."
       - Translate strings sueltas:  old "..." (cuando ya existen)
       - MENSAJES DE TELÉFONO:     $ send_message("char", "texto")
                                   python: listas/tuplas de conversaciones
                                   texto en screens phone/mobile/chat

Heurísticas AVN (phone/menu/dialogue) intactas.
"""
from __future__ import annotations
import os, re, json, hashlib
from dataclasses import dataclass, field, asdict
from typing import List, Dict, Optional, Tuple, Iterable, Callable

# ---------- Heurística teléfono / menús AVN ----------
PHONE_PATTERNS = [
    r'\bphone[_\.]', r'\bsms[_\.]', r'\bchat[_\.]', r'\bmsg[_\.]',
    r'\bmessage[_\.]', r'\bcontacts?[_\.]', r'\bnotif', r'\bcall[_\.]',
    r'\bwhatsapp', r'\binsta', r'\bsocial', r'\bapp_', r'\binbox',
    r'\bdialer', r'\btext_message', r'\bmobile[_\.]', r'\bconversation[_\.]',
    r'\bdm[_\.]', r'\btexting[_\.]', r'\bmessenger[_\.]', r'\bnotification',
    r'\bsend_message', r'\badd_sms', r'\badd_message', r'\bphone_message',
    r'\bsend_sms', r'\bphone\.send', r'\bmessage\.send', r'\bchat\.send',
    r'\bsms\.send', r'\bnotification\.show', r'\bnotify_message',
    r'\badd_chat', r'\badd_text', r'\bsend_text', r'\bphone_text',
    r'\bmessage_log', r'\bsms_log', r'\bchat_log', r'\bphone_log',
    r'\bphone\.add_message', r'\bphone\.send_message', r'\bphone\.add_text',
    r'\bmessenger\.send', r'\bdm\.send', r'\btext\.send', r'\binbox\.add',
    r'\badd_phone_message', r'\badd_phone_text', r'\bshow_message',
    r'\bshow_sms', r'\bnew_message', r'\bnew_sms', r'\bnew_chat',
    r'\breceive_message', r'\breceive_sms', r'\bphone_notification',
    r'\bpush_notification', r'\bmsg_notification',
    r'\bsend_self_message', r'\bset_choices',
]
MENU_PATTERNS = [
    r'\bmenu[_\.]', r'\bbutton[_\.]', r'\blabel[_\.]', r'\bui[_\.]',
    r'\bquest[_\.]', r'\bhint[_\.]', r'\btooltip', r'\bgui[_\.]',
    r'\bscreen[_\.\s]', r'\boption',
]
PHONE_RE = re.compile('|'.join(PHONE_PATTERNS), re.IGNORECASE)
MENU_RE  = re.compile('|'.join(MENU_PATTERNS), re.IGNORECASE)

PHONE_WORDS = {'messages','contacts','inbox','call','sms','chat','notification',
               'notifications','dial','whatsapp','instagram','phone','mensajes',
               'contactos','llamada','notificación', 'mobile', 'conversation',
               'dm', 'texting', 'messenger', 'text_message', 'message_log',
               'sms_log', 'chat_log', 'phone_log'}
MENU_WORDS  = {'new game','load game','save','load','options','preferences',
               'quit','main menu','continue','settings','about','help',
               'nuevo juego','cargar','guardar','opciones','salir','start'}

def classify(context: str, source_text: str = '') -> str:
    ctx = context or ''
    if PHONE_RE.search(ctx): return 'phone'
    if MENU_RE.search(ctx):  return 'menu'
    if source_text:
        low = source_text.strip().lower()
        if low in PHONE_WORDS: return 'phone'
        if low in MENU_WORDS:  return 'menu'
        if len(low) <= 20 and any(w in low for w in PHONE_WORDS): return 'phone'
        if len(low) <= 20 and any(w in low for w in MENU_WORDS):  return 'menu'
    return 'dialogue'

# ---------- Modelo ----------
@dataclass
class Entry:
    file: str                 # ruta relativa
    kind: str                 # 'dialogue' | 'string' | 'source_say' | 'source_menu'
                              # | 'source_text' | 'source_define' | 'source_character'
    block_id: str = ''
    speaker: str = ''
    source: str = ''
    translation: str = ''
    line_idx: int = 0
    category: str = 'dialogue'
    raw_old_line: str = ''
    indent: str = ''
    # contexto extra para modo source
    context_label: str = ''   # screen/menu/label en el que apareció
    is_source: bool = False   # True si viene de extract_source
    active_label: str = ''    # label de Ren'Py activo cuando se emitió esta línea

# ---------- Regex de parsing ----------
RE_TRANSLATE_BLOCK = re.compile(r'^(\s*)translate\s+(\S+)\s+(\S+):\s*$')
RE_TRANSLATE_STRINGS = re.compile(r'^(\s*)translate\s+(\S+)\s+strings:\s*$')
RE_OLD = re.compile(r'^(\s*)old\s+"((?:[^"\\]|\\.)*)"\s*$')
RE_NEW = re.compile(r'^(\s*)new\s+"((?:[^"\\]|\\.)*)"\s*$')
RE_DIALOGUE = re.compile(
    r'^(\s*)(?:(\w+|"(?:[^"\\]|\\.)*")\s+)?"((?:[^"\\]|\\.)*)"(.*)$'
)
RE_COMMENT_DIALOGUE = re.compile(r'^\s*#\s*(.*)$')

# extracción de TODAS las cadenas quoted en una línea (respeta \", \\')
# Mantiene RE_ANY_STRING por compatibilidad con código existente, pero el
# extractor raw usa iter_string_literals() para cubrir comillas simples,
# dobles, prefijos r/u/f/b y casos típicos de phone/chat Python data.
RE_ANY_STRING = re.compile(r'"((?:[^"\\]|\\.)*)"')
RE_QUOTED_STRING = re.compile(
    r'''(?ix)
    (?P<prefix>\b(?:r|u|ur|ru|f|fr|rf|b|br|rb)?\b)?
    (?P<quote>["'])
    (?P<body>(?:\\.|(?! (?P=quote) ).)*?)
    (?P=quote)
    '''
)

def iter_string_literals(line: str):
    """Yield decoded one-line single/double quoted literals from Ren'Py/Python-ish code.

    Ren'Py dialogue normally uses double quotes, but AVN phone systems often store
    conversations in Python lists/dicts with single quotes. The old regex missed
    those completely, which is why only a few phone bubbles were translated.
    """
    for m in RE_QUOTED_STRING.finditer(line):
        prefix = (m.group('prefix') or '').lower()
        body = m.group('body')
        # bytes literals are never player-facing text in Ren'Py scripts.
        if 'b' in prefix and 'f' not in prefix:
            continue
        yield _unescape(body)

# patrones específicos del modo source
RE_LABEL      = re.compile(r'^(\s*)label\s+([A-Za-z_]\w*)\s*(?:\(.*\))?\s*:')
RE_SCREEN     = re.compile(r'^(\s*)screen\s+([A-Za-z_]\w*)\s*(?:\(.*\))?\s*:')
RE_MENU       = re.compile(r'^(\s*)menu\s*(\w*)\s*:')
RE_CHARACTER  = re.compile(r'^(\s*)define\s+(\w+)\s*=\s*Character\s*\(\s*(.+)\)\s*$')
RE_GUI_DEF    = re.compile(r'^(\s*)(?:default\s+|define\s+)?(gui\.[A-Za-z_][\w\.]*)\s*=\s*"((?:[^"\\]|\\.)*)"\s*$')
RE_DEFINE_STR = re.compile(r'^(\s*)(?:default\s+|define\s+)([A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*)\s*=\s*"((?:[^"\\]|\\.)*)"\s*$')

# UI keywords que llevan texto traducible al inicio
UI_TEXT_KEYWORDS = (
    'text', 'textbutton', 'label', 'tooltip', 'caption', 'hint',
    'titlebutton', 'imagebutton',
)
RE_UI_TEXT = re.compile(
    r'^(\s*)(' + '|'.join(UI_TEXT_KEYWORDS) + r')\s+_?\(?\s*"((?:[^"\\]|\\.)*)"'
)

# líneas a ignorar (no son texto de UI sino paths/imágenes/identificadores)
SKIP_KEYWORDS = ('image', 'scene', 'show', 'hide', 'play', 'stop', 'queue',
                 'window', 'transform', 'init', 'python', 'init python',
                 'voice', 'sound', 'music', '$')

# Regex para detectar llamadas a funciones de mensajería en código Python
RE_PHONE_FUNC = re.compile(
    r'(?:send_message|send_self_message|set_choices|add_sms|add_message|phone_message|send_sms|'
    r'phone\.send|message\.send|chat\.send|sms\.send|'
    r'notification\.show|notify_message|add_chat|'
    r'add_text|send_text|phone_text|message_log|sms_log|chat_log|'
    r'phone\.add_message|phone\.send_message|phone\.add_text|'
    r'messenger\.send|dm\.send|text\.send|inbox\.add|'
    r'add_phone_message|add_phone_text|show_message|show_sms|'
    r'new_message|new_sms|new_chat|receive_message|receive_sms|'
    r'phone_notification|push_notification|msg_notification|'
    # Ren'Py API directa
    r'renpy\.notify|renpy\.call_screen|renpy\.input|renpy\.choice_screen)\s*\(',
    re.IGNORECASE
)

# Detecta listas/dicts de mensajes: variables cuyo nombre contiene palabras
# phone/message/sms/choice/option/text seguidas de .append( o .extend( o .insert(
# Cubre: text_message_screen_list.append([...]), sms_items.append(...),
#        choice_list.append(...), dialog_options.extend([...])
# NOTA: \b no funciona después de _ en Python, así que usamos lookbehind más amplio.
RE_PHONE_LIST_MUTATION = re.compile(
    r'(?:^|[\s(,])(\w*(?:message|phone|sms|chat|text_msg|dialog|choice|option|notify|inbox|'
    r'bubble|balloon|convo|conversation)\w*)\s*\.\s*(?:append|extend|insert|add)\s*\(',
    re.IGNORECASE
)

# Detecta: call screen screen_name("arg1", arg2, "arg3", ...)
# Captura el nombre de la screen y los argumentos completos para extraer strings
RE_CALL_SCREEN = re.compile(
    r'^\s*call\s+screen\s+(\w+)\s*\((.+)\)\s*$'
)

# Detecta asignación de lista de choices/opciones a variable:
#   $ choices = [("Texto A", val), ("Texto B", val)]
#   $ menu_items = [("Opción 1", 1), ("Opción 2", 2)]
RE_CHOICE_LIST_ASSIGN = re.compile(
    r'^\s*(?:\$\s*)?(\w*(?:choice|option|menu|item|pick|select|answer)\w*)\s*=\s*\[',
    re.IGNORECASE
)

# Extrae strings dentro de _("...") — marcador de traducción nativo de Ren'Py
# Cubre: _("texto"), _('texto'), N_("texto")
RE_RENPY_I18N = re.compile(r'\b_\(\s*(["\'])((?:\\.|(?!\1).)*?)\1\s*\)')

# Extrae el argumento text= de send_message / send_self_message
# Cubre: .send_message(text="..."), .send_self_message(text='...')
RE_SEND_MSG_TEXT = re.compile(
    r'\.(?:send_message|send_self_message)\s*\(\s*text\s*=\s*(["\'])((?:\\.|(?!\1).)*?)\1'
)

def iter_phone_message_texts(line: str):
    """Extrae textos de mensajes de teléfono de patrones AVN comunes:
    - .send_message(text="...") / .send_self_message(text="...")
    Devuelve (texto, es_propio) donde es_propio=True para send_self_message.
    """
    for m in RE_SEND_MSG_TEXT.finditer(line):
        body = _unescape(m.group(2))
        is_self = 'send_self_message' in m.group(0)
        yield body, is_self

def _unescape(s: str) -> str:
    # Orden importa: primero escapar \\ para no confundirlo con \n, \t, etc.
    # Usamos un placeholder que no puede aparecer en .rpy
    return (s.replace('\\\\', '\x00BSLASH\x00')
             .replace('\\n', '\n').replace('\\t', '\t')
             .replace('\\"', '"').replace("\\'", "'")
             .replace('\x00BSLASH\x00', '\\'))

def _escape(s: str) -> str:
    # Orden importa: primero escapar \ para no doblar los que ya escapamos
    return (s.replace('\\', '\\\\')
             .replace('"', '\\"')
             .replace('\n', '\\n').replace('\t', '\\t'))

# -------- helpers de filtrado --------
_NON_TEXT_RE = re.compile(r'^[\s\W_]*$')
_FILE_LIKE_RE = re.compile(r'^[\w\-/\\.]+\.(?:png|jpg|jpeg|webp|ogg|mp3|wav|mp4|webm|rpy|rpa|ttf|otf)$', re.IGNORECASE)

def _is_translatable(s: str) -> bool:
    if not s: return False
    s2 = s.strip()
    if len(s2) < 1: return False
    if _NON_TEXT_RE.match(s2): return False
    if _FILE_LIKE_RE.match(s2): return False
    # solo dígitos / símbolos — PERO permitir textos cortos legítimos como "...", "Hi.", "!", "?"
    has_letter = any(c.isalpha() for c in s2)
    # Si no tiene letras, verificar si es un texto de UI/diálogo conocido que debe preservarse
    if not has_letter:
        # Preservar símbolos de UI, puntuación emocional, y emojis de menú
        known_ui_texts = {'...', '..', '.', '!', '?', '!!', '?!', '...?', '...!', 
                          '+1', '-1', '+', '-', '♥', '♡', '★', '☆', '•', '·',
                          'o', 'O', '■', '□', '▶', '◀', '►', '◄', '▲', '▼',
                          '→', '←', '↑', '↓', '⇒', '⇐', '⇑', '⇓',
                          '✓', '✔', '✗', '✘', '✕', '✖',
                          '●', '○', '◐', '◑', '◒', '◓',
                          '♂', '♀', '♠', '♣', '♦', '♥',
                          '☰', '☱', '☲', '☳', '☴', '☵', '☶', '☷',
                          '≡', '=', '≠', '≈', '∞', '∑', '∆', '√',
                          '←', '→', '↑', '↓', '↔', '↕'}
        if s2 in known_ui_texts:
            return True
        # Si es puramente símbolos/puntuación sin letras ni números, 
        # y tiene al menos 1 char, dejar pasar (el SDK lo generó por algo)
        if len(s2) <= 10 and not any(c.isdigit() for c in s2):
            return True
        return False
    # nombres tipo identificador snake_case todo minúsculas (main_menu, new_game...)
    if ' ' not in s2 and re.match(r'^[a-z_][a-z0-9_]*$', s2):
        return False
    # palabras únicas muy cortas (≤3 chars) que no sean palabras comunes del juego
    # evita traducir nombres propios como "Ash", "Kim", "Ren" que rompería el juego
    # PERO: permitir saludos/interjecciones cortas que SÍ deben traducirse
    SHORT_DIALOGUE_WORDS = {
        'Hi', 'Hi.', 'Hey', 'Hey!', 'Yo', 'Yo!', 'Ok', 'Ok.', 'OK', 'Ok!',
        'Ah', 'Ah.', 'Ah!', 'Oh', 'Oh.', 'Oh!', 'Eh', 'Eh.', 'Eh!',
        'Mm', 'Mm.', 'Mmm', 'Mmm.', 'Hm', 'Hm.', 'Hmm', 'Hmm.',
        'Bye', 'Bye.', 'Bye!', 'Yeah', 'Yeah.', 'Yeah!', 'Nah', 'Nah.',
        'Wow', 'Wow.', 'Wow!', 'Ooh', 'Ooh.', 'Ooh!', 'Aww', 'Aww.',
        'Huh', 'Huh.', 'Huh!', 'What', 'What?', 'Why', 'Why?', 'Who', 'Who?',
        'Yes', 'Yes.', 'Yes!', 'No', 'No.', 'No!', 'Yep', 'Yep.', 'Nope',
        'Please', 'Please.', 'Thanks', 'Thanks.', 'Sorry', 'Sorry.',
        'Wait', 'Wait.', 'Wait!', 'Stop', 'Stop.', 'Stop!', 'Go', 'Go.', 'Go!',
        'Come', 'Come.', 'Come!', 'Look', 'Look.', 'Look!', 'See', 'See.',
        'Listen', 'Listen.', 'Shh', 'Shh.', 'Help', 'Help.', 'Help!',
        'Run', 'Run.', 'Run!', 'Hide', 'Hide.', 'Hide!', 'Stay', 'Stay.',
        'Leave', 'Leave.', 'Leave!', 'Follow', 'Follow.', 'Trust', 'Trust.',
        'Believe', 'Believe.', 'Remember', 'Remember.', 'Forget', 'Forget.',
        'Understand', 'Understand.', 'Sure', 'Sure.', 'Right', 'Right.', 'Wrong', 'Wrong.',
        'True', 'True.', 'False', 'False.', 'Maybe', 'Maybe.', 'Perhaps', 'Perhaps.',
        'Definitely', 'Definitely.', 'Exactly', 'Exactly.', 'Seriously', 'Seriously.',
        'Really', 'Really.', 'Honestly', 'Honestly.', 'Literally', 'Literally.',
        'Actually', 'Actually.', 'Basically', 'Basically.', 'Essentially', 'Essentially.',
        'Ultimately', 'Ultimately.', 'Finally', 'Finally.', 'Eventually', 'Eventually.',
        'Usually', 'Usually.', 'Normally', 'Normally.', 'Generally', 'Generally.',
        'Typically', 'Typically.', 'Commonly', 'Commonly.', 'Often', 'Often.',
        'Sometimes', 'Sometimes.', 'Rarely', 'Rarely.', 'Seldom', 'Seldom.',
        'Hardly', 'Hardly.', 'Barely', 'Barely.', 'Nearly', 'Nearly.', 'Almost', 'Almost.',
        'Practically', 'Practically.', 'Virtually', 'Virtually.', 'Fairly', 'Fairly.',
        'Quite', 'Quite.', 'Rather', 'Rather.', 'Pretty', 'Pretty.', 'Very', 'Very.',
        'Too', 'Too.', 'So', 'So.', 'Much', 'Much.', 'Many', 'Many.', 'More', 'More.',
        'Most', 'Most.', 'Less', 'Less.', 'Least', 'Least.', 'Enough', 'Enough.',
        'Plenty', 'Plenty.', 'Several', 'Several.', 'Various', 'Various.',
        'Here', 'Here.', 'There', 'There.', 'Everywhere', 'Everywhere.',
        'Somewhere', 'Somewhere.', 'Anywhere', 'Anywhere.', 'Nowhere', 'Nowhere.',
        'Elsewhere', 'Elsewhere.', 'Now', 'Now.', 'Then', 'Then.', 'Today', 'Today.',
        'Tonight', 'Tonight.', 'Tomorrow', 'Tomorrow.', 'Yesterday', 'Yesterday.',
        'Soon', 'Soon.', 'Later', 'Later.', 'Earlier', 'Earlier.', 'Before', 'Before.',
        'After', 'After.', 'During', 'During.', 'While', 'While.', 'Until', 'Until.',
        'Since', 'Since.', 'Ago', 'Ago.', 'Ahead', 'Ahead.', 'Behind', 'Behind.',
        'Above', 'Above.', 'Below', 'Below.', 'Over', 'Over.', 'Under', 'Under.',
        'Between', 'Between.', 'Among', 'Among.', 'Within', 'Within.', 'Outside', 'Outside.',
        'Inside', 'Inside.', 'Near', 'Near.', 'Far', 'Far.', 'Close', 'Close.',
        'Distant', 'Distant.', 'Remote', 'Remote.', 'Local', 'Local.', 'Global', 'Global.',
        'Worldwide', 'Worldwide.', 'Universal', 'Universal.', 'National', 'National.',
        'International', 'International.', 'Regional', 'Regional.', 'Urban', 'Urban.',
        'Rural', 'Rural.', 'Suburban', 'Suburban.', 'Domestic', 'Domestic.',
        'Foreign', 'Foreign.', 'Native', 'Native.', 'Alien', 'Alien.', 'Strange', 'Strange.',
        'Familiar', 'Familiar.', 'Known', 'Known.', 'Unknown', 'Unknown.',
        'Famous', 'Famous.', 'Infamous', 'Infamous.', 'Popular', 'Popular.', 'Unpopular', 'Unpopular.',
        'Normal', 'Normal.', 'Regular', 'Regular.', 'Common', 'Common.', 'Uncommon', 'Uncommon.',
        'Rare', 'Rare.', 'Unique', 'Unique.', 'Original', 'Original.', 'Copy', 'Copy.',
        'Fake', 'Fake.', 'Real', 'Real.', 'True', 'True.', 'False', 'False.',
        'Right', 'Right.', 'Wrong', 'Wrong.', 'Correct', 'Correct.', 'Incorrect', 'Incorrect.',
        'Proper', 'Proper.', 'Improper', 'Improper.', 'Appropriate', 'Appropriate.',
        'Inappropriate', 'Inappropriate.', 'Suitable', 'Suitable.', 'Unsuitable', 'Unsuitable.',
        'Fit', 'Fit.', 'Unfit', 'Unfit.', 'Ready', 'Ready.', 'Prepared', 'Prepared.',
        'Unready', 'Unready.', 'Unprepared', 'Unprepared.', 'Done', 'Done.', 'Finished', 'Finished.',
        'Complete', 'Complete.', 'Incomplete', 'Incomplete.', 'Partial', 'Partial.',
        'Total', 'Total.', 'Full', 'Full.', 'Empty', 'Empty.', 'Half', 'Half.',
        'Whole', 'Whole.', 'Part', 'Part.', 'Piece', 'Piece.', 'Bit', 'Bit.',
        'Lot', 'Lot.', 'Ton', 'Ton.', 'Dozen', 'Dozen.', 'Hundred', 'Hundred.',
        'Thousand', 'Thousand.', 'First', 'First.', 'Second', 'Second.', 'Third', 'Third.',
        'Fourth', 'Fourth.', 'Fifth', 'Fifth.', 'Last', 'Last.', 'Final', 'Final.',
        'Initial', 'Initial.', 'Original', 'Original.', 'Previous', 'Previous.',
        'Prior', 'Prior.', 'Subsequent', 'Subsequent.', 'Former', 'Former.', 'Latter', 'Latter.',
        'Current', 'Current.', 'Present', 'Present.', 'Past', 'Past.', 'Future', 'Future.',
        'Soon', 'Soon.', 'Later', 'Later.', 'Now', 'Now.', 'Then', 'Then.',
        'Before', 'Before.', 'After', 'After.', 'During', 'During.', 'While', 'While.',
        'Until', 'Until.', 'Since', 'Since.', 'Ago', 'Ago.', 'Ahead', 'Ahead.',
        'Behind', 'Behind.', 'Above', 'Above.', 'Below', 'Below.', 'Over', 'Over.',
        'Under', 'Under.', 'Between', 'Between.', 'Among', 'Among.', 'Within', 'Within.',
        'Outside', 'Outside.', 'Inside', 'Inside.', 'Near', 'Near.', 'Far', 'Far.',
        'Close', 'Close.', 'Distant', 'Distant.', 'Remote', 'Remote.', 'Local', 'Local.',
        'Global', 'Global.', 'Worldwide', 'Worldwide.', 'Universal', 'Universal.',
        'National', 'National.', 'International', 'International.', 'Regional', 'Regional.',
        'Urban', 'Urban.', 'Rural', 'Rural.', 'Suburban', 'Suburban.', 'Domestic', 'Domestic.',
        'Foreign', 'Foreign.', 'Native', 'Native.', 'Alien', 'Alien.', 'Strange', 'Strange.',
        'Familiar', 'Familiar.', 'Known', 'Known.', 'Unknown', 'Unknown.',
        'Famous', 'Famous.', 'Infamous', 'Infamous.', 'Popular', 'Popular.', 'Unpopular', 'Unpopular.'
    }
    if ' ' not in s2 and len(s2) <= 3 and s2[0].isupper():
        # Verificar si es una palabra de diálogo conocida que SÍ debe traducirse
        base_word = s2.rstrip('.!?')
        if base_word in SHORT_DIALOGUE_WORDS or s2 in SHORT_DIALOGUE_WORDS:
            pass  # continuar, es traducible
        else:
            return False
    # string de una sola palabra en PascalCase o ALLCAPS que parece identificador/variable
    # PERO: las opciones de menú de VN suelen ser palabras comunes de acción
    if ' ' not in s2 and re.match(r'^[A-Z][A-Za-z0-9]*$', s2) and len(s2) <= 12:
        # Lista EXPANDIDA de palabras comunes que aparecen en menús de VN
        # Incluye: acciones, emociones, respuestas sociales, direcciones, etc.
        COMMON_WORDS = {'Yes', 'No', 'Ok', 'OK', 'Start', 'Back', 'Next',
                        'End', 'New', 'Old', 'Buy', 'Use', 'Get', 'Run',
                        'Go', 'Stop', 'Play', 'Win', 'Lose', 'Help', 'Exit',
                        'Load', 'Save', 'Quit', 'Menu', 'About', 'Settings',
                        'Continue', 'Return', 'Cancel', 'Confirm', 'Close',
                        'Skip', 'Auto', 'History', 'Gallery', 'Music',
                        # Acciones sociales/emocionales (VN choices)
                        'Flirt', 'Stay', 'Apologise', 'Apologize', 'Compliment',
                        'Kiss', 'Hug', 'Touch', 'Hold', 'Grab', 'Pull', 'Push',
                        'Smile', 'Laugh', 'Cry', 'Shout', 'Whisper', 'Yell',
                        'Agree', 'Disagree', 'Accept', 'Refuse', 'Deny',
                        'Ask', 'Tell', 'Say', 'Speak', 'Talk', 'Chat',
                        'Follow', 'Lead', 'Trust', 'Doubt', 'Believe',
                        'Defend', 'Attack', 'Protect', 'Hide', 'Reveal',
                        'Leave', 'Enter', 'Join', 'Wait', 'Watch', 'Look',
                        'Listen', 'Ignore', 'Remember', 'Forget',
                        'Comfort', 'Tease', 'Joke', 'Insult', 'Praise',
                        'Beg', 'Demand', 'Offer', 'Invite', 'Refuse',
                        'Lie', 'Truth', 'Confess', 'Admit', 'Deny',
                        'Blame', 'Forgive', 'Thank', 'Greet', 'Farewell',
                        'Dance', 'Sing', 'Sleep', 'Wake', 'Rest',
                        'Eat', 'Drink', 'Cook', 'Clean', 'Work',
                        'Study', 'Read', 'Write', 'Draw', 'Paint',
                        'Call', 'Text', 'Message', 'Email', 'Visit',
                        'Date', 'Propose', 'Marry', 'Divorce', 'Break',
                        'Fight', 'Argue', 'Seduce', 'Charm', 'Impress',
                        'Escape', 'Rescue', 'Save', 'Kill', 'Die',
                        'Live', 'Survive', 'Thrive', 'Grow', 'Learn',
                        'Change', 'Transform', 'Evolve', 'Become',
                        'Pursue', 'Chase', 'Hunt', 'Search', 'Find',
                        'Lose', 'Win', 'Fail', 'Succeed', 'Achieve',
                        'Dream', 'Hope', 'Wish', 'Desire', 'Want',
                        'Need', 'Love', 'Hate', 'Like', 'Enjoy',
                        'Fear', 'Worry', 'Care', 'Mind', 'Notice',
                        'Choose', 'Pick', 'Select', 'Decide', 'Vote',
                        'Investigate', 'Explore', 'Discover', 'Solve',
                        'Create', 'Build', 'Make', 'Craft', 'Forge',
                        'Destroy', 'Fix', 'Repair', 'Heal',
                        'Hurt', 'Harm', 'Damage', 'Wound', 'Injure',
                        'Cure', 'Treat', 'Aid', 'Assist',
                        'Support', 'Encourage', 'Motivate', 'Inspire',
                        'Discourage', 'Disappoint', 'Upset', 'Anger',
                        'Please', 'Satisfy', 'Content', 'Happy',
                        'Sad', 'Mad', 'Glad', 'Bad', 'Good',
                        'Better', 'Best', 'Worse', 'Worst',
                        'More', 'Less', 'Most', 'Least', 'All',
                        'None', 'Some', 'Many', 'Few', 'Several',
                        'Always', 'Never', 'Sometimes', 'Often',
                        'Rarely', 'Usually', 'Normally', 'Generally',
                        'Specifically', 'Especially', 'Particularly',
                        'Exactly', 'Precisely', 'Approximately',
                        'Certainly', 'Definitely', 'Absolutely',
                        'Maybe', 'Perhaps', 'Probably', 'Possibly',
                        'Likely', 'Unlikely', 'Sure', 'Uncertain',
                        'Clear', 'Unclear', 'Obvious', 'Hidden',
                        'Visible', 'Invisible', 'Apparent', 'Secret',
                        'Public', 'Private', 'Personal', 'General',
                        'Special', 'Normal', 'Regular', 'Common',
                        'Uncommon', 'Rare', 'Unique', 'Original',
                        'Copy', 'Fake', 'Real', 'True', 'False',
                        'Right', 'Wrong', 'Correct', 'Incorrect',
                        'Proper', 'Improper', 'Appropriate', 'Inappropriate',
                        'Suitable', 'Unsuitable', 'Fit', 'Unfit',
                        'Ready', 'Prepared', 'Unready', 'Unprepared',
                        'Done', 'Finished', 'Complete', 'Incomplete',
                        'Partial', 'Total', 'Full', 'Empty',
                        'Half', 'Whole', 'Part', 'Piece', 'Bit',
                        'Lot', 'Ton', 'Dozen', 'Hundred', 'Thousand',
                        'First', 'Second', 'Third', 'Fourth', 'Fifth',
                        'Last', 'Final', 'Initial', 'Original',
                        'Previous', 'Prior', 'Subsequent',
                        'Former', 'Latter', 'Current', 'Present',
                        'Past', 'Future', 'Soon', 'Later', 'Now',
                        'Then', 'Before', 'After', 'During', 'While',
                        'Until', 'Since', 'Ago', 'Ahead', 'Behind',
                        'Above', 'Below', 'Over', 'Under', 'Between',
                        'Among', 'Within', 'Outside', 'Inside',
                        'Near', 'Far', 'Close', 'Distant', 'Remote',
                        'Local', 'Global', 'Worldwide', 'Universal',
                        'National', 'International', 'Regional',
                        'Urban', 'Rural', 'Suburban', 'Domestic',
                        'Foreign', 'Native', 'Alien', 'Strange',
                        'Familiar', 'Known', 'Unknown', 'Famous',
                        'Infamous', 'Popular', 'Unpopular'}
        if s2 not in COMMON_WORDS:
            return False
    return True

# ─── Filtro PERMISIVO para bloques translate strings: del SDK ────────────────
# El SDK de Ren'Py ya decidió qué es traducible. Solo excluimos lo que
# definitivamente NO es texto: paths de assets, strings completamente vacíos,
# y strings sin ninguna letra (símbolos puros como ">>>" se dejan pasar
# porque el SDK los generó por algo, aunque el motor los devuelva igual).
# NO excluimos: palabras cortas, snake_case, PascalCase, ALLCAPS — todos
# pueden ser labels de UI legítimos ("bar", "prev", "Enable", "QUIT", etc.)
_ASSET_EXTS = re.compile(
    r'^[\w\-/\\.]+\.(?:png|jpg|jpeg|webp|ogg|mp3|wav|mp4|webm|rpy|rpa|ttf|otf)$',
    re.IGNORECASE
)
# Identificadores internos de Ren'Py: snake_case con guión bajo Y más de 8 chars
# (game_menu, save_page_prev, tl_language_overlay…)
# Excluir solo si tiene underscore Y todo minúsculas Y ≥ 9 chars
_INTERNAL_RE = re.compile(r'^[a-z][a-z0-9]*(?:_[a-z0-9]+){1,}$')

def _is_translatable_ui(s: str) -> bool:
    """
    Versión permisiva de _is_translatable para bloques translate strings: del SDK.
    Confía en el SDK para la mayoría del filtrado; solo excluye casos obvios.
    CRÍTICO: Nunca rechazar símbolos, emojis, o textos cortos del SDK — 
    dejarlos vacíos (new "") los hace invisibles en el juego.
    """
    if not s:
        return False
    s2 = s.strip()
    if not s2:
        return False
    # paths de assets
    if _ASSET_EXTS.match(s2):
        return False
    # strings de formato de fecha/hora estilo strftime: %A, %B:%M, etc.
    if re.match(r'^[%\w:/ .,\-]+$', s2) and '%' in s2:
        return False
    # strings puramente numéricos (sin letras ni símbolos significativos)
    if s2.isdigit():
        return False
    # identificadores internos largos con underscore (game_menu, save_page_prev…)
    if _INTERNAL_RE.match(s2) and len(s2) >= 9:
        return False
    # TODO lo demás lo generó el SDK por algo → traducible
    return True


def _strip_inline_comment(line: str) -> str:
    # quita comentarios pero respeta # dentro de strings con comilla simple/doble
    quote = ''
    esc = False
    for i, c in enumerate(line):
        if esc:
            esc = False; continue
        if c == '\\':
            esc = True; continue
        if quote:
            if c == quote:
                quote = ''
            continue
        if c in ('"', "'"):
            quote = c; continue
        if c == '#':
            return line[:i]
    return line

def _stable_id(file_rel: str, line_idx: int, text: str, sub: int = 0) -> str:
    """ID estable estilo Ren'Py SDK para bloques translate.
    sub: índice adicional para cuando múltiples entries comparten la misma línea."""
    h = hashlib.md5(f"{file_rel}|{line_idx}|{sub}|{text}".encode('utf-8')).hexdigest()[:16]
    return h


def _renpy_block_id(label: str, text: str, sub: int = 0) -> str:
    """
    Genera el ID real que Ren'Py usa internamente para bloques translate.
    Formato: <label>_<hash8hex>
    Algoritmo: MD5( <label> + \\x00 + <text> + \\x00 + str(sub) ), primeros 8 hex chars.
    Coincide con el formato 'comenzar_d0e72641' que muestra Show Translation Info.
    
    Si label está vacío (texto fuera de cualquier label), usa solo el hash sin prefijo.
    """
    payload = f"{label}\x00{text}\x00{sub}".encode('utf-8')
    h = hashlib.md5(payload).hexdigest()[:8]
    if label:
        # limpiar el label: solo alfanuméricos y guiones bajos
        label_clean = re.sub(r'[^A-Za-z0-9_]', '_', label)
        return f"{label_clean}_{h}"
    return h

# =============================================================
#  PROTECCION DE NOMBRES DE PERSONAJES
# =============================================================

_RE_CYRILLIC = re.compile(r'[а-яА-ЯёЁ]')
_RE_LATIN_ONLY = re.compile(r'^[A-Za-zÀ-ÖØ-öø-ÿ0-9 \'\-\.]+$')

def get_character_names_from_entries(entries) -> dict:
    """Extrae todos los nombres de personajes de las entradas source_character.

    Retorna un dict {nombre_original: ''} donde los valores se rellenarán
    con la forma traducida (para nombres cirílicos) o se dejan vacíos (nombres
    latinos que se protegen tal cual).
    """
    names = {}
    for e in entries:
        if e.kind == 'source_character' and e.source and e.source.strip():
            names[e.source.strip()] = ''
    return names


def _is_cyrillic_name(name: str) -> bool:
    """True si el nombre contiene caracteres cirílicos."""
    return bool(_RE_CYRILLIC.search(name))


def _auto_register_character_names(entries, target_lang: str = 'EN') -> dict:
    """Detecta nombres de personajes, registra latinos como protegidos,
    traduce cirílicos al idioma destino y retorna un mapa cirílico→traducido.

    - Nombres latinos (Toronjil, Melissa, etc.) → se registran en translator_engines
      para que el Protector los omita durante la traducción.
    - Nombres cirílicos (Элин, etc.) → se traducen una sola vez al target_lang
      y la forma traducida también se registra para no re-traducirla.

    Retorna: {nombre_cirílico: nombre_traducido} para inyectarlo en strings.json.
    """
    try:
        from translator_engines import register_character_names, translate_batch
    except ImportError:
        return {}

    raw_names = get_character_names_from_entries(entries)
    if not raw_names:
        return {}

    latin_names = []
    cyrillic_names = []
    for name in raw_names:
        if _is_cyrillic_name(name):
            cyrillic_names.append(name)
        else:
            latin_names.append(name)

    # 1) Registrar nombres latinos directamente (no se traducen)
    if latin_names:
        register_character_names(latin_names)
        print(f'[char_names] {len(latin_names)} nombres latinos protegidos: '
              f'{", ".join(latin_names[:8])}{"..." if len(latin_names) > 8 else ""}')

    # 2) Traducir nombres cirílicos al idioma destino
    cyrillic_map: dict = {}
    if cyrillic_names:
        try:
            translated = translate_batch(
                cyrillic_names,
                source='auto',
                target=target_lang,
                engine='google',
                workers=4,
            )
            for orig, trans in zip(cyrillic_names, translated):
                if trans and trans.strip() and trans.strip() != orig.strip():
                    cyrillic_map[orig] = trans.strip()
                else:
                    # Si la traducción falló o es igual, usar el original
                    cyrillic_map[orig] = orig

            # Registrar las formas TRADUCIDAS para protegerlas en el resto del texto
            register_character_names(list(cyrillic_map.values()))
            print(f'[char_names] {len(cyrillic_map)} nombres cirílicos traducidos: '
                  + ', '.join(f'{k}→{v}' for k, v in list(cyrillic_map.items())[:6])
                  + ('...' if len(cyrillic_map) > 6 else ''))
        except Exception as ex:
            print(f'[char_names] error traduciendo nombres cirílicos: {ex}')
            # fallback: proteger como están
            register_character_names(cyrillic_names)

    # Guardar en global para que write_zenpy_files lo inyecte en strings.json
    global _last_cyrillic_name_map
    _last_cyrillic_name_map = cyrillic_map
    return cyrillic_map


# mapa global actualizado por _auto_register_character_names para que
# write_zenpy_files pueda inyectarlo en strings.json sin parámetros extra
_last_cyrillic_name_map: dict = {}


# =============================================================
#                 MODO A: TL existente (compat)
# =============================================================
def parse_file(path: str, base: str = '') -> List[Entry]:
    rel = os.path.relpath(path, base) if base else os.path.basename(path)
    with open(path, 'r', encoding='utf-8', errors='replace') as f:
        lines = f.readlines()

    entries: List[Entry] = []
    i = 0; n = len(lines)
    while i < n:
        line = lines[i]
        m = RE_TRANSLATE_STRINGS.match(line)
        if m:
            indent = m.group(1)
            i += 1
            current_old = None; current_old_idx = -1; current_old_raw = ''
            while i < n:
                sub = lines[i]
                if sub.strip() == '' or sub.startswith(indent + ' ') or sub.startswith(indent + '\t'):
                    mo = RE_OLD.match(sub); mn = RE_NEW.match(sub)
                    if mo:
                        current_old = _unescape(mo.group(2))
                        current_old_idx = i; current_old_raw = sub
                    elif mn and current_old is not None:
                        new_val = _unescape(mn.group(2))
                        cat = classify(current_old, source_text=current_old)
                        entries.append(Entry(
                            file=rel, kind='string',
                            source=current_old, translation=new_val,
                            line_idx=current_old_idx, category=cat,
                            raw_old_line=current_old_raw, indent=indent,
                        ))
                        current_old = None
                    i += 1
                else:
                    break
            continue

        m = RE_TRANSLATE_BLOCK.match(line)
        if m:
            indent = m.group(1); block_id = m.group(3); i += 1
            original_text = ''; speaker = ''
            while i < n:
                sub = lines[i]
                if sub.strip() == '': i += 1; continue
                cm = RE_COMMENT_DIALOGUE.match(sub)
                if cm and not original_text:
                    inner = cm.group(1)
                    md = RE_DIALOGUE.match('    ' + inner)
                    if md:
                        speaker = (md.group(2) or '').strip().strip('"')
                        original_text = _unescape(md.group(3))
                    i += 1; continue
                md = RE_DIALOGUE.match(sub)
                if md:
                    new_text = _unescape(md.group(3))
                    sp = (md.group(2) or '').strip().strip('"')
                    cat = classify(block_id + ' ' + (speaker or sp), source_text=original_text or new_text)
                    entries.append(Entry(
                        file=rel, kind='dialogue', block_id=block_id,
                        speaker=speaker or sp,
                        source=original_text or new_text,
                        translation=new_text if original_text else '',
                        line_idx=i, category=cat,
                        raw_old_line=sub, indent=indent,
                    ))
                    i += 1; break
                stripped = sub.lstrip()
                if stripped and not stripped.startswith('#'): break
                i += 1
            continue
        i += 1
    return entries


def parse_directory(root: str) -> List[Entry]:
    out: List[Entry] = []
    for dirpath, _, files in os.walk(root):
        for fn in files:
            if fn.endswith('.rpy'):
                full = os.path.join(dirpath, fn)
                try:
                    out.extend(parse_file(full, base=root))
                except Exception as e:
                    print(f'[parse error] {full}: {e}')
    # Registrar nombres de personajes para protegerlos en la traducción
    try:
        _auto_register_character_names(out, target_lang='EN')
    except Exception:
        pass
    return out


# =============================================================
#       MODO B: extracción "source" — TODO el .rpy fuente
# =============================================================

def _is_phone_file(rel_path: str) -> bool:
    """True si la ruta del archivo sugiere que es de teléfono/mensajería.

    Cubre los patrones descubiertos en Cybernetic Seduction y similares:
      - ep*_messages.rpy, ep*_msg.rpy  (eps 2–5)
      - ep*_fr.rpy / carpeta freeroam/  (eps 6–8: conversaciones en free roam)
      - phone, sms, chat, mobile…       (patrón clásico)
    """
    low = rel_path.lower()
    indicators = ('phone', 'sms', 'chat', 'msg', 'message', 'mobile',
                  'conversation', 'dm', 'texting', 'messenger', 'notification',
                  'contact', 'call', 'inbox', 'whatsapp', 'text_message')
    if any(ind in low for ind in indicators):
        return True
    # Archivos freeroam (_fr.rpy) — eps 6-8 mueven mensajes al free roam
    fn = os.path.basename(low)
    if fn.endswith('_fr.rpy') or fn.endswith('_fr.rpym'):
        return True
    # Carpeta freeroam/
    parts = low.replace('\\', '/').split('/')
    if 'freeroam' in parts:
        return True
    return False


def extract_source_file(path: str, base: str) -> List[Entry]:
    """Extrae todo lo traducible de un .rpy fuente (no necesita formato translate)."""
    rel = os.path.relpath(path, base)
    is_phone_file_flag = _is_phone_file(rel)
    with open(path, 'r', encoding='utf-8', errors='replace') as f:
        raw_lines = f.readlines()

    entries: List[Entry] = []
    n = len(raw_lines)
    i = 0
    in_python = False
    python_indent = ''
    context_stack: List[Tuple[int, str, str]] = []  # (indent_len, kind, name)
    active_renpy_label: str = ''  # label de primer nivel activo (para IDs reales de Ren'Py)

    # Seguimiento de strings ya extraídos en la misma línea (evita duplicados en archivos phone)
    seen_phone_keys: set = set()

    def cur_context_label() -> str:
        if not context_stack: return ''
        return ' > '.join(f"{k}:{nm}" for _, k, nm in context_stack)

    def update_context(line: str, idx: int):
        nonlocal context_stack, active_renpy_label
        stripped = line.lstrip()
        indent_len = len(line) - len(stripped)
        # pop contexts de mayor indent
        context_stack = [c for c in context_stack if c[0] < indent_len]
        m = RE_LABEL.match(line)
        if m:
            context_stack.append((indent_len, 'label', m.group(2)))
            # solo actualizamos active_renpy_label con labels de primer nivel (indent 0)
            if indent_len == 0:
                active_renpy_label = m.group(2)
            return
        m = RE_SCREEN.match(line)
        if m:
            context_stack.append((indent_len, 'screen', m.group(2))); return
        m = RE_MENU.match(line)
        if m:
            context_stack.append((indent_len, 'menu', m.group(2) or '_')); return

    while i < n:
        raw = raw_lines[i]
        line = _strip_inline_comment(raw).rstrip('\n')
        stripped = line.lstrip()

        # bloques python: saltar (pero en archivos phone O líneas con llamadas phone, extraer strings)
        if in_python:
            if stripped and not raw.startswith(python_indent + ' ') and not raw.startswith(python_indent + '\t') and stripped != '':
                in_python = False
            else:
                # Extraer si: es archivo phone O la línea contiene una llamada a función de mensajería
                # O es una mutación de lista phone (.append/.extend en variable phone/message/sms...)
                is_phone_line_py   = bool(RE_PHONE_FUNC.search(line))
                is_phone_list_py   = bool(RE_PHONE_LIST_MUTATION.search(line))
                if is_phone_file_flag or is_phone_line_py or is_phone_list_py:
                    phone_ctx = True
                    for txt in iter_string_literals(line):
                        key = (i, txt)
                        if key not in seen_phone_keys and (_is_translatable(txt) or _raw_is_textlike(txt, phone_context=phone_ctx)):
                            seen_phone_keys.add(key)
                            entries.append(Entry(
                                file=rel, kind='source_text',
                                speaker='phone', source=txt,
                                line_idx=i, category='phone',
                                raw_old_line=raw,
                                indent=python_indent + '    ',
                                context_label=cur_context_label() or 'phone:python',
                                is_source=True,
                                active_label=active_renpy_label,
                            ))
                i += 1; continue
        if stripped.startswith('init python') or stripped == 'python:' or stripped.startswith('python '):
            in_python = True
            python_indent = raw[:len(raw)-len(stripped)]
            i += 1; continue

        # saltar líneas vacías/comentarios puros
        if not stripped or stripped.startswith('#'):
            i += 1; continue

        # actualizar contexto (label/screen/menu)
        update_context(line, i)
        ctx_label = cur_context_label()
        cur_active_label = active_renpy_label  # snapshot del label activo para este entry

        # IGNORAR si ya estamos dentro de un bloque translate (modo A se encarga)
        if RE_TRANSLATE_BLOCK.match(line) or RE_TRANSLATE_STRINGS.match(line):
            # saltar el bloque entero
            base_indent = len(line) - len(stripped)
            i += 1
            while i < n:
                s2 = raw_lines[i]
                stripped2 = s2.lstrip()
                if stripped2 and (len(s2) - len(stripped2)) <= base_indent and not s2.strip().startswith('#'):
                    break
                i += 1
            continue

        # 1) Character("Nombre", ...)
        m = RE_CHARACTER.match(line)
        if m:
            args = m.group(3)
            # extraer la primera cadena "..."
            sm = RE_ANY_STRING.search(args)
            if sm:
                txt = _unescape(sm.group(1))
                if _is_translatable(txt):
                    entries.append(Entry(
                        file=rel, kind='source_character',
                        speaker=m.group(2), source=txt,
                        line_idx=i, category='dialogue',
                        raw_old_line=raw, indent=m.group(1),
                        context_label=f'character:{m.group(2)}', is_source=True,
                        active_label=cur_active_label,
                    ))
            i += 1; continue

        # 2) gui.*  /  define foo = "..."
        m = RE_GUI_DEF.match(line) or RE_DEFINE_STR.match(line)
        if m:
            txt = _unescape(m.group(3))
            if _is_translatable(txt):
                cat = classify(m.group(2), source_text=txt)
                entries.append(Entry(
                    file=rel, kind='source_define',
                    speaker=m.group(2), source=txt,
                    line_idx=i, category=cat,
                    raw_old_line=raw, indent=m.group(1),
                    context_label=f'define:{m.group(2)}', is_source=True,
                    active_label=cur_active_label,
                ))
            i += 1; continue

        # 3) UI: text/textbutton/label/tooltip "..."
        m = RE_UI_TEXT.match(line)
        if m:
            kw = m.group(2); txt = _unescape(m.group(3))
            if _is_translatable(txt):
                cat = classify(ctx_label + ' ' + kw, source_text=txt)
                if kw == 'text' and cat == 'dialogue': cat = 'menu'
                entries.append(Entry(
                    file=rel, kind='source_text',
                    speaker=kw, source=txt,
                    line_idx=i, category=cat,
                    raw_old_line=raw, indent=m.group(1),
                    context_label=ctx_label, is_source=True,
                    active_label=cur_active_label,
                ))
            i += 1; continue

        # 4) Opciones de menú:  "Texto":
        if context_stack and context_stack[-1][1] == 'menu':
            mm = re.match(r'^(\s*)"((?:[^"\\]|\\.)*)"\s*(?:if\s.+)?\s*:\s*$', line)
            if mm:
                txt = _unescape(mm.group(2))
                if _is_translatable(txt):
                    entries.append(Entry(
                        file=rel, kind='source_menu',
                        speaker='menu', source=txt,
                        line_idx=i, category='menu',
                        raw_old_line=raw, indent=mm.group(1),
                        context_label=ctx_label, is_source=True,
                        active_label=cur_active_label,
                    ))
                i += 1; continue

        # 4b) call screen screen_name("arg1", "arg2", ...)
        #     Cubre opciones de menú custom: call screen zoey_choice_screen("Encourage him", ...)
        #     También cubre: call screen input_screen("prompt", ...)
        if stripped.startswith('call screen ') or stripped.startswith('call screen\t'):
            m_cs = RE_CALL_SCREEN.match(line)
            if m_cs:
                screen_name = m_cs.group(1)
                args_str    = m_cs.group(2)
                cat = classify(screen_name + ' ' + ctx_label)
                if cat == 'dialogue': cat = 'menu'  # call screen → siempre UI/menú
                for txt in iter_string_literals(args_str):
                    if _is_translatable(txt):
                        key = (i, txt)
                        if key not in seen_phone_keys:
                            seen_phone_keys.add(key)
                            entries.append(Entry(
                                file=rel, kind='source_menu',
                                speaker=f'screen:{screen_name}', source=txt,
                                line_idx=i, category=cat,
                                raw_old_line=raw,
                                indent=line[:len(line)-len(stripped)],
                                context_label=ctx_label or f'call_screen:{screen_name}',
                                is_source=True,
                                active_label=cur_active_label,
                            ))
            i += 1; continue

        # 4c) Asignación de lista de choices: $ choices = [("Texto A", val), ...]
        #     Detecta patrones comunes de menús dinámicos en AVNs
        if stripped.startswith('$'):
            _inner_test = stripped[1:].strip()
            if RE_CHOICE_LIST_ASSIGN.match(_inner_test) or RE_CHOICE_LIST_ASSIGN.match(stripped):
                for txt in iter_string_literals(_inner_test):
                    if _is_translatable(txt):
                        key = (i, txt)
                        if key not in seen_phone_keys:
                            seen_phone_keys.add(key)
                            entries.append(Entry(
                                file=rel, kind='source_menu',
                                speaker='menu', source=txt,
                                line_idx=i, category='menu',
                                raw_old_line=raw,
                                indent=line[:len(line)-len(stripped)],
                                context_label=ctx_label or 'choice_list',
                                is_source=True,
                                active_label=cur_active_label,
                            ))
                i += 1; continue

        # 5) Líneas Python de una línea ($) con funciones de mensajería
        #    o cualquier string en archivos phone/mensajería
        if stripped.startswith('$'):
            inner = stripped[1:].strip()
            is_phone_line = bool(RE_PHONE_FUNC.search(inner))
            # Fix \b: RE_PHONE_LIST_MUTATION detecta .append/.extend en variables
            # con palabras phone/message aunque estén precedidas por _ (text_message_...)
            is_phone_list  = bool(RE_PHONE_LIST_MUTATION.search(inner))
            if is_phone_line or is_phone_list or is_phone_file_flag:
                # PRIORIDAD: extraer text= kwargs de send_message/send_self_message
                # Esto captura: $ obj.send_message(text="Hola") directamente
                phone_msg_texts = list(iter_phone_message_texts(inner))
                if phone_msg_texts:
                    for txt, is_self in phone_msg_texts:
                        key = (i, txt)
                        if key not in seen_phone_keys and txt.strip():
                            seen_phone_keys.add(key)
                            entries.append(Entry(
                                file=rel, kind='source_text',
                                speaker='phone_self' if is_self else 'phone',
                                source=txt,
                                line_idx=i, category='phone',
                                raw_old_line=raw,
                                indent=line[:len(line)-len(stripped)],
                                context_label=ctx_label or 'phone:send_message',
                                is_source=True,
                                active_label=cur_active_label,
                            ))
                else:
                    # Fallback: extraer strings genéricos de la línea
                    phone_ctx = is_phone_line or is_phone_list or is_phone_file_flag
                    _cat = 'phone' if (is_phone_line or is_phone_list or is_phone_file_flag) else 'dialogue'
                    for txt in iter_string_literals(inner):
                        key = (i, txt)
                        if key not in seen_phone_keys and (_is_translatable(txt) or _raw_is_textlike(txt, phone_context=phone_ctx)):
                            seen_phone_keys.add(key)
                            entries.append(Entry(
                                file=rel, kind='source_text',
                                speaker='phone', source=txt,
                                line_idx=i, category=_cat,
                                raw_old_line=raw,
                                indent=line[:len(line)-len(stripped)],
                                context_label=ctx_label or 'phone:python',
                                is_source=True,
                                active_label=cur_active_label,
                            ))
            i += 1; continue

        # 6) say:  character "texto"   o   "texto"   o   "speaker" "texto"
        # solo si la línea NO empieza con keyword conocido
        first_word = stripped.split(None, 1)[0] if stripped else ''
        if first_word not in SKIP_KEYWORDS:
            md = RE_DIALOGUE.match(line)
            if md:
                speaker = (md.group(2) or '').strip()
                txt = _unescape(md.group(3))
                rest = (md.group(4) or '').strip()
                # filtrar: el "rest" no debería ser código raro tipo "= ..."; si lo es, skip
                if rest.startswith('=') or rest.startswith('('):
                    i += 1; continue
                if _is_translatable(txt):
                    speaker_clean = speaker.strip('"')
                    cat = classify(ctx_label + ' ' + speaker_clean, source_text=txt)
                    entries.append(Entry(
                        file=rel, kind='source_say',
                        speaker=speaker_clean, source=txt,
                        line_idx=i, category=cat,
                        raw_old_line=raw, indent=line[:len(line)-len(stripped)],
                        context_label=ctx_label, is_source=True,
                        active_label=cur_active_label,
                    ))

        # 7) EXTRACCIÓN DE RESPALDO en archivos phone/mensajería:
        #    Solo extrae strings genéricos si:
        #      a) Estamos dentro de un label (context_stack tiene 'label') — mensajes reales
        #      b) La línea tiene _("texto") explícito — strings marcados para traducción
        #    Fuera de labels (líneas default/define con Room/Activity/etc.), solo
        #    capturar _() para evitar ruido de IDs, nombres de imagen, etc.
        if is_phone_file_flag:
            in_label = any(kind == 'label' for _, kind, _ in context_stack)
            has_i18n = bool(RE_RENPY_I18N.search(line))

            if has_i18n:
                # Extraer solo los strings dentro de _("...") — evita capturar
                # paths y IDs que están en la misma línea
                for m_i18n in RE_RENPY_I18N.finditer(line):
                    txt = _unescape(m_i18n.group(2))
                    key = (i, txt)
                    if key not in seen_phone_keys and txt.strip():
                        seen_phone_keys.add(key)
                        entries.append(Entry(
                            file=rel, kind='source_text',
                            speaker='phone', source=txt,
                            line_idx=i, category='phone',
                            raw_old_line=raw,
                            indent=line[:len(line)-len(stripped)],
                            context_label=ctx_label or 'phone:i18n',
                            is_source=True,
                            active_label=cur_active_label,
                        ))
            elif in_label:
                # Dentro de un label: extraer todos los strings (mensajes de chat)
                for txt in iter_string_literals(line):
                    key = (i, txt)
                    if key not in seen_phone_keys and (_is_translatable(txt) or _raw_is_textlike(txt, phone_context=True)):
                        seen_phone_keys.add(key)
                        entries.append(Entry(
                            file=rel, kind='source_text',
                            speaker='phone', source=txt,
                            line_idx=i, category='phone',
                            raw_old_line=raw,
                            indent=line[:len(line)-len(stripped)],
                            context_label=ctx_label or 'phone:generic',
                            is_source=True,
                            active_label=cur_active_label,
                        ))

        i += 1
    return entries


def extract_source_directory(root: str) -> List[Entry]:
    """Escanea TODA la carpeta game/ buscando .rpy y extrae todo lo traducible."""
    out: List[Entry] = []
    skipped_dirs = {'tl', 'cache'}  # no escanear traducciones existentes ni cache
    for dirpath, dirs, files in os.walk(root):
        # podar
        rel = os.path.relpath(dirpath, root)
        parts = rel.split(os.sep)
        if any(p in skipped_dirs for p in parts):
            dirs[:] = []
            continue
        for fn in files:
            if (fn.endswith('.rpy') or fn.endswith('.rpym')) and not fn.endswith('.rpyc'):
                full = os.path.join(dirpath, fn)
                try:
                    out.extend(extract_source_file(full, base=root))
                except Exception as e:
                    print(f'[source extract error] {full}: {e}')
    # asignar block_id estable a entradas tipo dialogue/menu
    # Para entries de tipo say (source_say, dialogue), usar el algoritmo REAL de Ren'Py:
    #   <label>_<hash8hex>  — coincide con lo que muestra Show Translation Info
    # Para strings/UI/defines, el block_id no importa (van en translate strings:)
    from collections import defaultdict
    line_counts: dict = defaultdict(int)
    for e in out:
        if not e.block_id:
            key = (e.file, e.line_idx)
            sub = line_counts[key]
            if e.kind in ('source_say', 'dialogue', 'source_character'):
                # usar algoritmo real de Ren'Py con el label activo
                e.block_id = _renpy_block_id(e.active_label, e.source, sub)
            else:
                e.block_id = _stable_id(e.file, e.line_idx, e.source, sub)
            line_counts[key] += 1
    # Registrar nombres de personajes automáticamente para protegerlos
    # durante la traducción. Los cirílicos se traducen a EN como fallback
    # (el caller puede llamar _auto_register_character_names con el target real
    # si lo conoce; este registro inicial protege al menos los latinos).
    try:
        _auto_register_character_names(out, target_lang='EN')
    except Exception:
        pass
    return out


# =============================================================
#  EXTRACTOR UNIVERSAL DE STRINGS RAW (red de seguridad estilo HZ Ille)
# =============================================================
#
# Este extractor hace un barrido completo de TODOS los string literals
# "..." en cada .rpy del juego, sin importar el contexto. Captura cosas que
# los parsers semánticos suelen perder:
#   - $ todo.append("Investigar el armario")
#   - $ renpy.notify("Has subido de nivel")
#   - $ gl_bar.add_item(0, "Glass Jane", "Encontrada en el bar", ...)
#   - phone.add_message("Nina", "What does this face mean?")
#   - listas / dicts dentro de bloques python:
#   - cualquier llamada a función custom del juego con strings de UI
#
# Las entradas se marcan kind='raw_string' y se vuelcan SOLO a strings.json
# (no a translate strings: blocks) para que el runtime replaceText.rpy
# (KeywordProcessor) las reemplace en cualquier texto que muestre Ren'Py.
# Es exactamente la estrategia de HZ Ille / Zenpy.

# Líneas que nunca tienen texto humano traducible
_RAW_SKIP_LINE_PREFIX = (
    'image ', 'scene ', 'show ', 'hide ', 'play ', 'stop ', 'queue ',
    'voice ', 'sound ', 'music ', 'transform ', 'style ',
    'init ', 'init python', 'init -', 'init +',
    'screen ', 'label ', 'menu ', 'python:', 'python ',
    'translate ',  # ya manejado por extract_source_*
    'default persistent.', 'default _', 'define _',
    'pass', 'return', 'jump', 'call ',
    'with ', 'window ',
    'add ', 'frame ', 'vbox', 'hbox', 'fixed', 'side ',
    'use ',
)

# Identificadores Ren'Py de un solo "token" sin espacios — falsos positivos típicos
_RAW_IDENT_RE = re.compile(r'^[A-Za-z_][\w\.\-/]*$')
_RAW_FILE_RE  = re.compile(
    r'^[\w\-/\\.]+\.(?:png|jpg|jpeg|webp|gif|bmp|svg|ogg|mp3|wav|opus|mp4|webm|'
    r'mov|avi|mkv|rpy|rpyc|rpa|ttf|otf|json|txt|xml|csv|yaml|yml)$',
    re.IGNORECASE
)
_RAW_TAG_RE = re.compile(r'^\{[^}]*\}$')           # {b} {color=...}
_RAW_VAR_RE = re.compile(r'^\[[^\]]*\]$')          # [mc]
_RAW_NUM_RE = re.compile(r'^[\d\.,:%\-+ ]+$')      # 100% / 1.2.3

# Llamadas Ren'Py donde "X" suele ser un identificador, NO texto:
#   show character "name" ... → skip
#   renpy.show("name", ...)   → skip
_RAW_CTX_IDENT_CALLS = re.compile(
    r'\b(?:renpy\.(?:show|hide|scene|play|stop|sound|music|movie|image|'
    r'has_image|has_label|jump|call|notify_sound|file|loadable|exists|'
    r'load_image|cache_pin|cache_unpin|free_memory|input)|'
    r'config\.[a-z_]+|persistent\.|store\.|im\.|Image\(|Movie\(|Sound\(|'
    r'Solid\(|Frame\(|Composite\(|Crop\(|At\(|Transform\(|ATL\()',
    re.IGNORECASE
)


def _raw_is_textlike(s: str, phone_context: bool = False) -> bool:
    """Filtro: solo aceptar strings que parecen texto humano.

    phone_context=True: más permisivo para archivos phone/mensajería donde
    mensajes cortos como "Fine", "Tonight", "Sure" son bubbles válidos.
    """
    if not s:
        return False
    s2 = s.strip()
    if len(s2) < 2:
        return False
    if _RAW_FILE_RE.match(s2):
        return False
    if _RAW_TAG_RE.match(s2) or _RAW_VAR_RE.match(s2):
        return False
    if _RAW_NUM_RE.match(s2):
        return False
    # debe tener al menos una letra
    if not any(c.isalpha() for c in s2):
        return False
    # si tiene espacios, casi seguro es texto humano (frases, mensajes, opciones)
    if ' ' in s2:
        return True
    # sin espacios: aceptar si termina en puntuación humana
    if s2[-1] in '.!?…':
        # rechazar tokens tipo "module.method."
        if _RAW_IDENT_RE.match(s2.rstrip('.!?…')) and '.' in s2 and s2[-1] != '.':
            return False
        # rechazar "abc.def" puro identificador con punto
        if s2.count('.') >= 1 and s2[-1] not in '!?…':
            tail = s2.rstrip('.')
            if _RAW_IDENT_RE.match(tail) and tail[0].islower():
                return False
        return True
    # En contexto phone/mensajeria: aceptar PascalCase y palabras con mayuscula
    # que son bubbles de chat cortos ("Fine", "Tonight", "Sure", "Excelente", etc.)
    if phone_context and _RAW_IDENT_RE.match(s2):
        if '_' in s2:
            return False  # snake_case -> identificador
        if s2 == s2.lower() and len(s2) > 8:
            return False  # todo minusculas largo -> variable
        if s2[0].isupper() or (any(c.isupper() for c in s2) and any(c.islower() for c in s2)):
            return True
        return False
    # fuera de contexto phone: PascalCase parece identificador
    if _RAW_IDENT_RE.match(s2):
        return False
    return False


def extract_raw_strings_directory(root: str,
                                  known_sources: Optional[set] = None) -> List[Entry]:
    """
    Barrido universal de TODOS los string literals "..." en cada .rpy de game/.
    Devuelve Entry(kind='raw_string', category='raw') con dedup por contenido.
    `known_sources` = strings ya capturados por extract_source_directory (se
    omiten para no duplicar trabajo).
    """
    known = set(known_sources or [])
    out: List[Entry] = []
    seen: set = set()
    skipped_dirs = {'tl', 'cache'}

    for dirpath, dirs, files in os.walk(root):
        rel_dir = os.path.relpath(dirpath, root)
        parts = rel_dir.split(os.sep)
        if any(p in skipped_dirs for p in parts):
            dirs[:] = []
            continue
        for fn in files:
            if not (fn.endswith('.rpy') or fn.endswith('.rpym')) or fn.endswith('.rpyc'):
                continue
            full = os.path.join(dirpath, fn)
            rel = os.path.relpath(full, root).replace('\\', '/')
            try:
                with open(full, 'r', encoding='utf-8', errors='replace') as fh:
                    lines = fh.readlines()
            except Exception as e:
                print(f'[raw extract error] {full}: {e}')
                continue

            in_translate_block = False
            translate_indent = 0
            for i, raw_line in enumerate(lines):
                line = raw_line.rstrip('\n')
                stripped = line.lstrip()
                if not stripped or stripped.startswith('#'):
                    continue
                indent = len(line) - len(stripped)

                # Saltar bloques translate (ya capturados por extract_source_*)
                if stripped.startswith('translate '):
                    in_translate_block = True
                    translate_indent = indent
                    continue
                if in_translate_block:
                    if indent <= translate_indent and stripped:
                        in_translate_block = False
                    else:
                        continue

                # Skip prefijos no-texto
                low = stripped.lower()
                if any(low.startswith(p) for p in _RAW_SKIP_LINE_PREFIX):
                    # EXCEPCIONES: $ y python: contienen llamadas a funciones
                    # custom (notify, append, send_message...) con texto humano.
                    # NO skipear $.
                    if not (stripped.startswith('$') or stripped == '$'):
                        continue

                # quitar comentario inline
                code_part = _strip_inline_comment(line)

                # contexto: ¿la línea es una llamada a una función Ren'Py
                # interna donde los strings son identificadores?
                ctx_ident = bool(_RAW_CTX_IDENT_CALLS.search(code_part))
                # ¿Es una llamada a función de mensajería phone/chat?
                is_phone_line = bool(RE_PHONE_FUNC.search(code_part))
                # archivo phone o línea con función de mensajería → contexto permisivo
                phone_ctx = _is_phone_file(rel) or is_phone_line

                for raw_s in iter_string_literals(code_part):
                    if not _raw_is_textlike(raw_s, phone_context=phone_ctx):
                        continue
                    # En contextos identificador-like, exigir espacios (frase real)
                    if ctx_ident and ' ' not in raw_s.strip():
                        continue
                    if raw_s in known or raw_s in seen:
                        continue
                    seen.add(raw_s)
                    out.append(Entry(
                        file=rel,
                        kind='raw_string',
                        speaker='',
                        source=raw_s,
                        line_idx=i,
                        category='raw',
                        raw_old_line=line,
                        indent=line[:len(line) - len(stripped)],
                        context_label='raw',
                        is_source=True,
                    ))
    return out


# =============================================================
#         Auto-localización del game/ desde el .exe
# =============================================================
def locate_game_dir(path_in: str) -> Optional[str]:
    """
    Dado un .exe / carpeta del juego / carpeta game/ / archivo .rpy,
    devuelve la ruta absoluta de la carpeta game/ del proyecto Ren'Py.
    """
    if not path_in: return None
    p = os.path.abspath(path_in)
    if os.path.isfile(p):
        d = os.path.dirname(p)
    else:
        d = p
    # subir hasta encontrar 'game' como subdir (ampliado a 8 niveles para estructuras profundas)
    for _ in range(8):
        cand = os.path.join(d, 'game')
        if os.path.isdir(cand):
            contents = os.listdir(cand)
            has_rpy  = any(f.endswith('.rpy')  for f in contents)
            has_rpyc = any(f.endswith('.rpyc') for f in contents)
            if has_rpy or has_rpyc:
                if not has_rpy and has_rpyc:
                    print(f'[warn] La carpeta game/ solo contiene .rpyc compilados (sin fuentes .rpy). '
                          f'El modo Source no podrá extraer texto. Necesitas los archivos .rpy originales.')
                return cand
        if os.path.basename(d).lower() == 'game' and os.path.isdir(d):
            return d
        parent = os.path.dirname(d)
        if parent == d: break
        d = parent
    # último intento: si la ruta inicial es game/
    if os.path.isdir(path_in) and os.path.basename(os.path.abspath(path_in)).lower() == 'game':
        return os.path.abspath(path_in)
    return None


# =============================================================
#  MODO C: writer — produce game/tl/<lang>/<archivo>.rpy válido
# =============================================================
def write_tl_files(game_dir: str, tl_lang: str, entries: List[Entry],
                   out_root: Optional[str] = None) -> Tuple[int, int]:
    """
    Genera archivos en game/tl/<tl_lang>/<mismo_nombre>.rpy con bloques:
        # game/path/file.rpy:LINE
        translate <tl_lang> <id>:
            # speaker "original"
            speaker "traducción"

    Para entries 'source_menu', 'source_text', 'source_define', 'source_character',
    'source_say' que NO son diálogo `say` regular, se generan como
    `translate <lang> strings:` con bloques old/new (compatible con Ren'Py).

    Devuelve (n_archivos, n_entradas_escritas).
    """
    if not entries:
        return (0, 0)

    out_root = out_root or os.path.join(game_dir, 'tl', tl_lang)
    os.makedirs(out_root, exist_ok=True)

    # agrupar por archivo
    by_file: Dict[str, List[Entry]] = {}
    for e in entries:
        if not e.translation or not e.translation.strip():
            continue
        by_file.setdefault(e.file, []).append(e)

    written_files = 0
    written_entries = 0

    for rel, items in by_file.items():
        items.sort(key=lambda x: x.line_idx)

        # separar say-like (translate <id>:) vs strings-like (translate strings:)
        say_like = [e for e in items if e.kind in ('dialogue', 'source_say', 'source_character')]
        str_like = [e for e in items if e.kind in ('string', 'source_text', 'source_menu',
                                                    'source_define')]
        # 'raw_string' NO se escribe a .rpy (va solo a strings.json vía replaceText runtime)

        out_path = os.path.join(out_root, rel)
        os.makedirs(os.path.dirname(out_path), exist_ok=True)

        with open(out_path, 'w', encoding='utf-8') as f:
            f.write('# TODO: Translation updated by RenpyTranslator\n')
            f.write(f'# Source: {rel}\n\n')

            # 1) bloques translate <lang> <id>:
            for e in say_like:
                # saltar bloques sin texto real (generados por SDK para efectos/sonidos)
                if not e.source or not e.source.strip():
                    continue
                bid = e.block_id or _stable_id(e.file, e.line_idx, e.source)
                speaker = e.speaker.strip()
                # Character() definitions se traducen como string suelta
                if e.kind == 'source_character':
                    continue  # se irá al bloque strings abajo
                f.write(f'# {rel}:{e.line_idx + 1}\n')
                f.write(f'translate {tl_lang} {bid}:\n')
                # comentario con original
                if speaker and not speaker.startswith('"'):
                    f.write(f'    # {speaker} "{_escape(e.source)}"\n')
                    f.write(f'    {speaker} "{_escape(e.translation)}"\n\n')
                else:
                    sp = speaker if speaker else ''
                    sp_part = (sp + ' ') if sp else ''
                    f.write(f'    # {sp_part}"{_escape(e.source)}"\n')
                    f.write(f'    {sp_part}"{_escape(e.translation)}"\n\n')
                written_entries += 1

            # 2) translate <lang> strings: para UI, menús, defines, characters
            char_items = [e for e in items if e.kind == 'source_character']
            all_strings = str_like + char_items
            if all_strings:
                seen: Dict[str, str] = {}
                # filtrar: solo strings con source real (no vacío)
                valid_strings = [e for e in all_strings if e.source and e.source.strip()]
                if valid_strings:
                    f.write(f'translate {tl_lang} strings:\n\n')
                    for e in valid_strings:
                        if e.source in seen:
                            if seen[e.source] != e.translation and e.translation:
                                print(
                                    f'[warn] String duplicada con traducción distinta en {rel}:{e.line_idx+1}\n'
                                    f'  source: {e.source[:80]!r}\n'
                                    f'  primera trad: {seen[e.source][:60]!r}\n'
                                    f'  ignorada:     {e.translation[:60]!r}'
                                )
                            continue
                        seen[e.source] = e.translation or ''
                        f.write(f'    # {rel}:{e.line_idx + 1}\n')
                        f.write(f'    old "{_escape(e.source)}"\n')
                        # Si no hay traduccion: escribir new "" (vacio) en vez del original
                        # para que _scan_strings_block lo detecte como pendiente de traduccion
                        # EXCEPCION: entradas que no son menus/UI (source_character, etc.)
                        # donde tiene mas sentido dejar el original que un hueco vacio
                        if e.translation and e.translation.strip():
                            tl_val = e.translation
                        elif e.kind in ('source_menu', 'source_text', 'string', 'source_define'):
                            tl_val = ''   # dejar vacio para que el scanner lo detecte
                        else:
                            tl_val = e.source  # otros: copiar original
                        if tl_val:
                            f.write(f'    new "{_escape(tl_val)}"\n\n')
                        else:
                            f.write(f'    new ""\n\n')
                        written_entries += 1

        written_files += 1

    # Generar strings.json + replaceText.rpy (compatibilidad Zenpy)
    try:
        write_zenpy_files(out_root, tl_lang, entries)
    except Exception as _ze:
        print(f'[zenpy] Error generando archivos zenpy: {_ze}')

    return (written_files, written_entries)




# =============================================================
#  ZENPY-COMPAT: genera strings.json + replaceText.rpy
# =============================================================

REPLACE_TEXT_TEMPLATE = """init python:
    import os as zenpy_os
    import json as zenpy_json
    import string as zenpy_string
    import io as zenpy_io

    if not hasattr(renpy, "session"):
        setattr(renpy, "session", {})

    renpy.session["zenpy_dir"] = __builtins__["dir"]
    renpy.session["zenpy_set"] = __builtins__["set"]
    renpy.session["zenpy_len"] = __builtins__["len"]

    if not "zenpy_variables" in renpy.session:
        renpy.session["zenpy_variables"] = {}

    if not "KeywordProcessorr" in renpy.session["zenpy_dir"]():
        class KeywordProcessor(object):
            def __init__(self, case_sensitive=False):
                self._keyword = '_keyword_'
                self._white_space_chars = renpy.session["zenpy_set"](['.', '\\t', '\\n', '\\a', ' ', ','])
                try:
                    self.non_word_boundaries = renpy.session["zenpy_set"](zenpy_string.digits + zenpy_string.letters + '_')
                except AttributeError:
                    self.non_word_boundaries = renpy.session["zenpy_set"](zenpy_string.digits + zenpy_string.ascii_letters + '_')
                self.keyword_trie_dict = __builtins__["dict"]()
                self.case_sensitive = case_sensitive
                self._terms_in_trie = 0

            def __len__(self): return self._terms_in_trie

            def __contains__(self, word):
                if not self.case_sensitive: word = word.lower()
                current_dict = self.keyword_trie_dict
                len_covered = 0
                for char in word:
                    if char in current_dict: current_dict = current_dict[char]; len_covered += 1
                    else: break
                return self._keyword in current_dict and len_covered == renpy.session["zenpy_len"](word)

            def __setitem__(self, keyword, clean_name=None):
                status = False
                if not clean_name and keyword: clean_name = keyword
                if keyword and clean_name:
                    if not self.case_sensitive: keyword = keyword.lower()
                    current_dict = self.keyword_trie_dict
                    for letter in keyword: current_dict = current_dict.setdefault(letter, {})
                    if self._keyword not in current_dict: status = True; self._terms_in_trie += 1
                    current_dict[self._keyword] = clean_name
                return status

            def set_non_word_boundaries(self, non_word_boundaries):
                self.non_word_boundaries = non_word_boundaries

            def add_keyword(self, keyword, clean_name=None):
                return self.__setitem__(keyword, clean_name)

            def try_replace(self, sentence):
                try: return self.replace_keywords(sentence)
                except: return sentence

            def replace_keywords(self, sentence):
                if not sentence: return sentence
                new_sentence = []
                orig_sentence = sentence
                if not self.case_sensitive: sentence = sentence.lower()
                current_word = ''
                current_dict = self.keyword_trie_dict
                current_white_space = ''
                sequence_end_pos = 0
                idx = 0
                sentence_len = renpy.session["zenpy_len"](sentence)
                while idx < sentence_len:
                    char = sentence[idx]
                    if char not in self.non_word_boundaries:
                        current_word += orig_sentence[idx]
                        current_white_space = char
                        if self._keyword in current_dict or char in current_dict:
                            sequence_found = None
                            longest_sequence_found = None
                            is_longer_seq_found = False
                            if self._keyword in current_dict:
                                sequence_found = current_dict[self._keyword]
                                longest_sequence_found = current_dict[self._keyword]
                                sequence_end_pos = idx
                            if char in current_dict:
                                current_dict_continued = current_dict[char]
                                current_word_continued = current_word
                                idy = idx + 1
                                while idy < sentence_len:
                                    inner_char = sentence[idy]
                                    if inner_char not in self.non_word_boundaries and self._keyword in current_dict_continued and ((idx-1 < 0 or not sentence[idx-1].isalpha()) or not sentence[idx].isalpha()) and (not sentence[idy].isalpha() or not sentence[idy-1].isalpha()):
                                        current_white_space = inner_char
                                        longest_sequence_found = current_dict_continued[self._keyword]
                                        sequence_end_pos = idy
                                        is_longer_seq_found = True
                                    if inner_char in current_dict_continued:
                                        current_word_continued += orig_sentence[idy]
                                        current_dict_continued = current_dict_continued[inner_char]
                                    else: break
                                    idy += 1
                                else:
                                    if self._keyword in current_dict_continued and (renpy.session["zenpy_len"](current_word_continued) == renpy.session["zenpy_len"](sentence) or (idy - renpy.session["zenpy_len"](current_word_continued) - 1 > -1 and (not sentence[idy - renpy.session["zenpy_len"](current_word_continued)-1].isalpha() or not sentence[idx].isalpha()))):
                                        current_white_space = ''
                                        longest_sequence_found = current_dict_continued[self._keyword]
                                        sequence_end_pos = idy
                                        is_longer_seq_found = True
                                if is_longer_seq_found:
                                    idx = sequence_end_pos
                                    current_word = current_word_continued
                            current_dict = self.keyword_trie_dict
                            if longest_sequence_found:
                                if current_word.islower(): longest_sequence_found = longest_sequence_found.lower()
                                elif current_word.isupper(): longest_sequence_found = longest_sequence_found.upper()
                                elif renpy.session["zenpy_len"](current_word) > 1 and renpy.session["zenpy_len"](longest_sequence_found) > 1:
                                    if current_word[0].islower() and not longest_sequence_found[0].islower():
                                        lst = [c for c in longest_sequence_found]; lst[0] = lst[0].lower(); longest_sequence_found = "".join(lst)
                                    elif current_word[0].isupper() and not longest_sequence_found[0].isupper():
                                        lst = [c for c in longest_sequence_found]; lst[0] = lst[0].upper(); longest_sequence_found = "".join(lst)
                                new_sentence.append(longest_sequence_found + current_white_space)
                                current_word = ''; current_white_space = ''
                            else:
                                new_sentence.append(current_word); current_word = ''; current_white_space = ''
                        else:
                            current_dict = self.keyword_trie_dict
                            new_sentence.append(current_word); current_word = ''; current_white_space = ''
                    elif char in current_dict:
                        current_word += orig_sentence[idx]; current_dict = current_dict[char]
                    else:
                        current_word += orig_sentence[idx]
                        current_dict = self.keyword_trie_dict
                        idy = idx + 1
                        while idy < sentence_len:
                            char = sentence[idy]
                            current_word += orig_sentence[idy]
                            if char not in self.non_word_boundaries: break
                            idy += 1
                        idx = idy
                        new_sentence.append(current_word); current_word = ''; current_white_space = ''
                    if idx + 1 >= sentence_len:
                        if self._keyword in current_dict: new_sentence.append(current_dict[self._keyword])
                        else: new_sentence.append(current_word)
                    idx += 1
                return "".join(new_sentence)

    def zenpy_get_strings_path():
        zenpy_file = "/tl/{lang}/strings.json"
        if zenpy_os.path.isfile(zenpy_file): return zenpy_file
        zenpy_file = (renpy.config.gamedir + "/tl/{lang}/strings.json").replace("\\\\", "/")
        if zenpy_os.path.isfile(zenpy_file): return zenpy_file
        zenpy_file = zenpy_file.replace("/", "\\\\")
        if zenpy_os.path.isfile(zenpy_file): return zenpy_file
        if hasattr(renpy.config, "searchpath") and renpy.config.searchpath != None:
            for d in renpy.config.searchpath:
                zenpy_file = (d + "/tl/{lang}/strings.json").replace("\\\\", "/")
                if zenpy_os.path.isfile(zenpy_file): return zenpy_file
                zenpy_file = zenpy_file.replace("/", "\\\\")
                if zenpy_os.path.isfile(zenpy_file): return zenpy_file
        return ""

    renpy.session["zenpy_variables"]["{lang}"] = {"next_replace": None, "keyword_processor": KeywordProcessor(case_sensitive=False)}
    renpy.session["zenpy_variables"]["{lang}"]["keyword_processor"].set_non_word_boundaries(
        renpy.session["zenpy_set"]("\u00f1\u00d1\u00e1\u00e9\u00ed\u00f3\u00fa\u00c9\u00d3\u00da\u00e0\u00e2\u00e4\u00e3\u00e8\u00ea\u00eb\u00ef\u00ee\u00ec\u00f4\u00f6\u00f2\u00f5\u00f9\u00fb\u00fc\u00ff\u00e7\u00c2\u00c0\u00c3\u00c8\u00ca\u00cb\u00ce\u00cc\u00d4\u00db\u00d9\u00dc\u0178\u00c7\u00d2\u00d5"))

    zenpy_strings = {}
    zenpy_file = zenpy_get_strings_path()

    if renpy.android and not zenpy_os.path.isfile(zenpy_file):
        pass
    elif zenpy_os.path.isfile(zenpy_file):
        zenpy_opened_file = zenpy_io.open(zenpy_file, 'r', encoding="UTF8")
        zenpy_strings = zenpy_json.loads(zenpy_opened_file.read())
        zenpy_opened_file.close()
    else:
        print("strings.json NO ENCONTRADO: " + zenpy_file)

    zenpy_exact = {}
    zenpy_placeholder_rules = []

    def zenpy__escape_re(s):
        out = ""
        for ch in s:
            if ch in ".^$*+?{}[]\\|()":
                out += "\\" + ch
            else:
                out += ch
        return out

    def zenpy__compile_placeholder_rule(original, replace):
        # f-strings / .format(): "Hi {name}" can reach the UI as "Hi Mike".
        # Ren'Py substitutions: "Hi [mc]" may also be already expanded in custom UIs.
        pattern = "^"
        repl = ""
        captures = []
        i = 0
        group_idx = 1
        while i < renpy.session["zenpy_len"](original):
            ch = original[i]
            if ch in "[{":
                close = "]" if ch == "[" else "}"
                j = original.find(close, i + 1)
                if j > i + 1:
                    token = original[i:j+1]
                    captures.append((token, group_idx))
                    pattern += "(.+?)"
                    group_idx += 1
                    i = j + 1
                    continue
            pattern += zenpy__escape_re(ch)
            i += 1
        pattern += "$"
        repl = replace
        return (pattern, captures, repl) if captures else None

    if zenpy_strings != None and renpy.session["zenpy_len"](zenpy_strings) > 0:
        for original, replace in zenpy_strings.items():
            original = original.strip()
            replace = replace.strip()
            if not original or not replace or original == replace:
                continue
            zenpy_exact[original] = replace
            renpy.session["zenpy_variables"]["{lang}"]["keyword_processor"].add_keyword(original, replace)
            if ("{" in original and "}" in original) or ("[" in original and "]" in original):
                rule = zenpy__compile_placeholder_rule(original, replace)
                if rule:
                    zenpy_placeholder_rules.append(rule)

    renpy.session["zenpy_variables"]["{lang}"]["exact"] = zenpy_exact
    renpy.session["zenpy_variables"]["{lang}"]["placeholder_rules"] = zenpy_placeholder_rules

    # Registrar aliases comunes para evitar que el runtime no haga nada si el
    # juego usa "Spanish" pero la carpeta/config dice spanish_latino (o al revés).
    zenpy_aliases = renpy.session["zenpy_set"](["{lang}", "Spanish", "spanish", "spanish_latino", "es", "Español", "Espanol"])
    for zenpy_alias in zenpy_aliases:
        if zenpy_alias not in renpy.session["zenpy_variables"]:
            renpy.session["zenpy_variables"][zenpy_alias] = renpy.session["zenpy_variables"]["{lang}"]

    if not "__next_replace__" in renpy.session["zenpy_variables"]:
        renpy.session["zenpy_variables"]["__next_replace__"] = config.replace_text

    if not renpy.config.custom_text_tags:
        renpy.config.custom_text_tags["z" + "enpy_enable_tags"] = None

    def zenpy__regex_match(pattern, text):
        try:
            import re as zenpy_re
            return zenpy_re.match(pattern, text)
        except Exception:
            return None

    def zenpy_text(text):
        lang = _preferences.language
        if lang in renpy.session["zenpy_variables"]:
            zvars = renpy.session["zenpy_variables"][lang]
            try:
                if text in zvars.get("exact", {}):
                    return zvars["exact"][text]
                stripped = text.strip()
                if stripped in zvars.get("exact", {}):
                    return text.replace(stripped, zvars["exact"][stripped], 1)
                for pattern, captures, repl in zvars.get("placeholder_rules", []):
                    m = zenpy__regex_match(pattern, text)
                    if m:
                        out = repl
                        for token, idx in captures:
                            try:
                                out = out.replace(token, m.group(idx))
                            except Exception:
                                pass
                        return out
            except Exception:
                pass
            return zvars["keyword_processor"].try_replace(text)
        elif renpy.session["zenpy_variables"]["__next_replace__"] != None:
            return renpy.session["zenpy_variables"]["__next_replace__"](text)
        else:
            return text

    config.replace_text = zenpy_text
"""


def _build_replace_text_rpy(lang: str) -> str:
    """
    Construye el contenido de replaceText.rpy sin usar templates con escapes anidados.
    Cada línea se define explícitamente para evitar el bug 'out += \\" + ch'.
    """
    # Caracteres especiales que necesitan estar literalmente en el .rpy generado
    BSLASH = '\\'          # un backslash literal
    QUOTE  = '"'           # una comilla doble literal

    # La línea problemática en el .rpy debe quedar exactamente:
    #   out += "\" + ch
    # Lo que en Python escribimos como: BSLASH + QUOTE + ' + ch'
    escape_re_body = (
        '    def zenpy__escape_re(s):\n'
        '        out = ""\n'
        '        for ch in s:\n'
        '            if ch in ".^$*+?{}[]\\\\ |()":\n'
        '                out += "\\\\" + ch\n'
        '            else:\n'
        '                out += ch\n'
        '        return out\n'
    )
    # Verificar que la línea problemática quede correcta
    # Debe tener:  out += "\" + ch   (un solo backslash dentro de las comillas)
    assert escape_re_body.count('out += "\\\\" + ch') == 1, "escape_re line broken"

    # Rutas del strings.json — deben tener doble backslash en el .rpy
    sj_path = f'/tl/{lang}/strings.json'
    sj_path_game = f'renpy.config.gamedir + "/tl/{lang}/strings.json"'

    lines = []
    lines.append('init python:\n')
    lines.append('    import os as zenpy_os\n')
    lines.append('    import json as zenpy_json\n')
    lines.append('    import string as zenpy_string\n')
    lines.append('    import io as zenpy_io\n')
    lines.append('\n')
    lines.append('    if not hasattr(renpy, "session"):\n')
    lines.append('        setattr(renpy, "session", {})\n')
    lines.append('\n')
    lines.append('    renpy.session["zenpy_dir"] = __builtins__["dir"]\n')
    lines.append('    renpy.session["zenpy_set"] = __builtins__["set"]\n')
    lines.append('    renpy.session["zenpy_len"] = __builtins__["len"]\n')
    lines.append('\n')
    lines.append('    if not "zenpy_variables" in renpy.session:\n')
    lines.append('        renpy.session["zenpy_variables"] = {}\n')
    lines.append('\n')
    lines.append('    if not "KeywordProcessorr" in renpy.session["zenpy_dir"]():\n')
    lines.append('        class KeywordProcessor(object):\n')
    lines.append('            def __init__(self, case_sensitive=False):\n')
    lines.append('                self._keyword = \'_keyword_\'\n')
    lines.append('                self._white_space_chars = renpy.session["zenpy_set"]([\'.\', \'\\t\', \'\\n\', \'\\a\', \' \', \',\'])\n')
    lines.append('                try:\n')
    lines.append('                    self.non_word_boundaries = renpy.session["zenpy_set"](zenpy_string.digits + zenpy_string.letters + \'_\')\n')
    lines.append('                except AttributeError:\n')
    lines.append('                    self.non_word_boundaries = renpy.session["zenpy_set"](zenpy_string.digits + zenpy_string.ascii_letters + \'_\')\n')
    lines.append('                self.keyword_trie_dict = __builtins__["dict"]()\n')
    lines.append('                self.case_sensitive = case_sensitive\n')
    lines.append('                self._terms_in_trie = 0\n')
    lines.append('\n')
    lines.append('            def __len__(self): return self._terms_in_trie\n')
    lines.append('\n')
    lines.append('            def __contains__(self, word):\n')
    lines.append('                if not self.case_sensitive: word = word.lower()\n')
    lines.append('                current_dict = self.keyword_trie_dict\n')
    lines.append('                len_covered = 0\n')
    lines.append('                for char in word:\n')
    lines.append('                    if char in current_dict: current_dict = current_dict[char]; len_covered += 1\n')
    lines.append('                    else: break\n')
    lines.append('                return self._keyword in current_dict and len_covered == renpy.session["zenpy_len"](word)\n')
    lines.append('\n')
    lines.append('            def __setitem__(self, keyword, clean_name=None):\n')
    lines.append('                status = False\n')
    lines.append('                if not clean_name and keyword: clean_name = keyword\n')
    lines.append('                if keyword and clean_name:\n')
    lines.append('                    if not self.case_sensitive: keyword = keyword.lower()\n')
    lines.append('                    current_dict = self.keyword_trie_dict\n')
    lines.append('                    for letter in keyword: current_dict = current_dict.setdefault(letter, {})\n')
    lines.append('                    if self._keyword not in current_dict: status = True; self._terms_in_trie += 1\n')
    lines.append('                    current_dict[self._keyword] = clean_name\n')
    lines.append('                return status\n')
    lines.append('\n')
    lines.append('            def set_non_word_boundaries(self, non_word_boundaries):\n')
    lines.append('                self.non_word_boundaries = non_word_boundaries\n')
    lines.append('\n')
    lines.append('            def add_keyword(self, keyword, clean_name=None):\n')
    lines.append('                return self.__setitem__(keyword, clean_name)\n')
    lines.append('\n')
    lines.append('            def try_replace(self, sentence):\n')
    lines.append('                try: return self.replace_keywords(sentence)\n')
    lines.append('                except: return sentence\n')
    lines.append('\n')
    lines.append('            def replace_keywords(self, sentence):\n')
    lines.append('                if not sentence: return sentence\n')
    lines.append('                new_sentence = []\n')
    lines.append('                orig_sentence = sentence\n')
    lines.append('                if not self.case_sensitive: sentence = sentence.lower()\n')
    lines.append('                current_word = \'\'\n')
    lines.append('                current_dict = self.keyword_trie_dict\n')
    lines.append('                current_white_space = \'\'\n')
    lines.append('                sequence_end_pos = 0\n')
    lines.append('                idx = 0\n')
    lines.append('                sentence_len = renpy.session["zenpy_len"](sentence)\n')
    lines.append('                while idx < sentence_len:\n')
    lines.append('                    char = sentence[idx]\n')
    lines.append('                    if char not in self.non_word_boundaries:\n')
    lines.append('                        current_word += orig_sentence[idx]\n')
    lines.append('                        current_white_space = char\n')
    lines.append('                        if self._keyword in current_dict or char in current_dict:\n')
    lines.append('                            sequence_found = None\n')
    lines.append('                            longest_sequence_found = None\n')
    lines.append('                            is_longer_seq_found = False\n')
    lines.append('                            if self._keyword in current_dict:\n')
    lines.append('                                sequence_found = current_dict[self._keyword]\n')
    lines.append('                                longest_sequence_found = current_dict[self._keyword]\n')
    lines.append('                                sequence_end_pos = idx\n')
    lines.append('                            if char in current_dict:\n')
    lines.append('                                current_dict_continued = current_dict[char]\n')
    lines.append('                                current_word_continued = current_word\n')
    lines.append('                                idy = idx + 1\n')
    lines.append('                                while idy < sentence_len:\n')
    lines.append('                                    inner_char = sentence[idy]\n')
    lines.append('                                    if inner_char not in self.non_word_boundaries and self._keyword in current_dict_continued and ((idx-1 < 0 or not sentence[idx-1].isalpha()) or not sentence[idx].isalpha()) and (not sentence[idy].isalpha() or not sentence[idy-1].isalpha()):\n')
    lines.append('                                        current_white_space = inner_char\n')
    lines.append('                                        longest_sequence_found = current_dict_continued[self._keyword]\n')
    lines.append('                                        sequence_end_pos = idy\n')
    lines.append('                                        is_longer_seq_found = True\n')
    lines.append('                                    if inner_char in current_dict_continued:\n')
    lines.append('                                        current_word_continued += orig_sentence[idy]\n')
    lines.append('                                        current_dict_continued = current_dict_continued[inner_char]\n')
    lines.append('                                    else: break\n')
    lines.append('                                    idy += 1\n')
    lines.append('                                else:\n')
    lines.append('                                    if self._keyword in current_dict_continued and (renpy.session["zenpy_len"](current_word_continued) == renpy.session["zenpy_len"](sentence) or (idy - renpy.session["zenpy_len"](current_word_continued) - 1 > -1 and (not sentence[idy - renpy.session["zenpy_len"](current_word_continued)-1].isalpha() or not sentence[idx].isalpha()))):\n')
    lines.append('                                        current_white_space = \'\'\n')
    lines.append('                                        longest_sequence_found = current_dict_continued[self._keyword]\n')
    lines.append('                                        sequence_end_pos = idy\n')
    lines.append('                                        is_longer_seq_found = True\n')
    lines.append('                                if is_longer_seq_found:\n')
    lines.append('                                    idx = sequence_end_pos\n')
    lines.append('                                    current_word = current_word_continued\n')
    lines.append('                            current_dict = self.keyword_trie_dict\n')
    lines.append('                            if longest_sequence_found:\n')
    lines.append('                                if current_word.islower(): longest_sequence_found = longest_sequence_found.lower()\n')
    lines.append('                                elif current_word.isupper(): longest_sequence_found = longest_sequence_found.upper()\n')
    lines.append('                                elif renpy.session["zenpy_len"](current_word) > 1 and renpy.session["zenpy_len"](longest_sequence_found) > 1:\n')
    lines.append('                                    if current_word[0].islower() and not longest_sequence_found[0].islower():\n')
    lines.append('                                        lst = [c for c in longest_sequence_found]; lst[0] = lst[0].lower(); longest_sequence_found = "".join(lst)\n')
    lines.append('                                    elif current_word[0].isupper() and not longest_sequence_found[0].isupper():\n')
    lines.append('                                        lst = [c for c in longest_sequence_found]; lst[0] = lst[0].upper(); longest_sequence_found = "".join(lst)\n')
    lines.append('                                new_sentence.append(longest_sequence_found + current_white_space)\n')
    lines.append('                                current_word = \'\'; current_white_space = \'\'\n')
    lines.append('                            else:\n')
    lines.append('                                new_sentence.append(current_word); current_word = \'\'; current_white_space = \'\'\n')
    lines.append('                        else:\n')
    lines.append('                            current_dict = self.keyword_trie_dict\n')
    lines.append('                            new_sentence.append(current_word); current_word = \'\'; current_white_space = \'\'\n')
    lines.append('                    elif char in current_dict:\n')
    lines.append('                        current_word += orig_sentence[idx]; current_dict = current_dict[char]\n')
    lines.append('                    else:\n')
    lines.append('                        current_word += orig_sentence[idx]\n')
    lines.append('                        current_dict = self.keyword_trie_dict\n')
    lines.append('                        idy = idx + 1\n')
    lines.append('                        while idy < sentence_len:\n')
    lines.append('                            char = sentence[idy]\n')
    lines.append('                            current_word += orig_sentence[idy]\n')
    lines.append('                            if char not in self.non_word_boundaries: break\n')
    lines.append('                            idy += 1\n')
    lines.append('                        idx = idy\n')
    lines.append('                        new_sentence.append(current_word); current_word = \'\'; current_white_space = \'\'\n')
    lines.append('                    if idx + 1 >= sentence_len:\n')
    lines.append('                        if self._keyword in current_dict: new_sentence.append(current_dict[self._keyword])\n')
    lines.append('                        else: new_sentence.append(current_word)\n')
    lines.append('                    idx += 1\n')
    lines.append('                return "".join(new_sentence)\n')
    lines.append('\n')
    lines.append(f'    def zenpy_get_strings_path():\n')
    lines.append(f'        zenpy_file = "{sj_path}"\n')
    lines.append(f'        if zenpy_os.path.isfile(zenpy_file): return zenpy_file\n')
    lines.append(f'        zenpy_file = ({sj_path_game}).replace("\\\\", "/")\n')
    lines.append(f'        if zenpy_os.path.isfile(zenpy_file): return zenpy_file\n')
    lines.append(f'        zenpy_file = zenpy_file.replace("/", "\\\\")\n')
    lines.append(f'        if zenpy_os.path.isfile(zenpy_file): return zenpy_file\n')
    lines.append( '        if hasattr(renpy.config, "searchpath") and renpy.config.searchpath != None:\n')
    lines.append( '            for d in renpy.config.searchpath:\n')
    lines.append(f'                zenpy_file = (d + "{sj_path}").replace("\\\\", "/")\n')
    lines.append( '                if zenpy_os.path.isfile(zenpy_file): return zenpy_file\n')
    lines.append(f'                zenpy_file = zenpy_file.replace("/", "\\\\")\n')
    lines.append( '                if zenpy_os.path.isfile(zenpy_file): return zenpy_file\n')
    lines.append( '        return ""\n')
    lines.append('\n')
    lines.append(f'    renpy.session["zenpy_variables"]["{lang}"] = {{"next_replace": None, "keyword_processor": KeywordProcessor(case_sensitive=False)}}\n')
    lines.append(f'    renpy.session["zenpy_variables"]["{lang}"]["keyword_processor"].set_non_word_boundaries(\n')
    lines.append( '        renpy.session["zenpy_set"]("' + 'ñÑáéíóúÉÓÚàâäãèêëïîìôöòõùûüÿçÂÀÃÈÊËÎÌÔÛÙÜŸÇÒÕ' + '"))\n')
    lines.append('\n')
    lines.append('    zenpy_strings = {}\n')
    lines.append('    zenpy_file = zenpy_get_strings_path()\n')
    lines.append('\n')
    lines.append('    if renpy.android and not zenpy_os.path.isfile(zenpy_file):\n')
    lines.append('        pass\n')
    lines.append('    elif zenpy_os.path.isfile(zenpy_file):\n')
    lines.append('        zenpy_opened_file = zenpy_io.open(zenpy_file, \'r\', encoding="UTF8")\n')
    lines.append('        zenpy_strings = zenpy_json.loads(zenpy_opened_file.read())\n')
    lines.append('        zenpy_opened_file.close()\n')
    lines.append('    else:\n')
    lines.append('        print("strings.json NO ENCONTRADO: " + zenpy_file)\n')
    lines.append('\n')
    lines.append('    zenpy_exact = {}\n')
    lines.append('    zenpy_placeholder_rules = []\n')
    lines.append('\n')
    # La función escape_re — la parte más crítica, escrita carácter a carácter
    lines.append('    def zenpy__escape_re(s):\n')
    lines.append('        out = ""\n')
    lines.append('        for ch in s:\n')
    lines.append('            if ch in ".^$*+?{}[]\\\\|()":\n')
    # En disco debe quedar:  out += "\\" + ch
    # "\\" en Ren'Py/Python = un backslash literal. "\" sola es string sin cerrar.
    lines.append('                out += "\\\\" + ch\n')
    lines.append('            else:\n')
    lines.append('                out += ch\n')
    lines.append('        return out\n')
    lines.append('\n')
    lines.append('    def zenpy__compile_placeholder_rule(original, replace):\n')
    lines.append('        pattern = "^"\n')
    lines.append('        captures = []\n')
    lines.append('        i = 0\n')
    lines.append('        group_idx = 1\n')
    lines.append('        while i < renpy.session["zenpy_len"](original):\n')
    lines.append('            ch = original[i]\n')
    lines.append('            if ch in "[{":\n')
    lines.append('                close = "]" if ch == "[" else "}"\n')
    lines.append('                j = original.find(close, i + 1)\n')
    lines.append('                if j > i + 1:\n')
    lines.append('                    token = original[i:j+1]\n')
    lines.append('                    captures.append((token, group_idx))\n')
    lines.append('                    pattern += "(.+?)"\n')
    lines.append('                    group_idx += 1\n')
    lines.append('                    i = j + 1\n')
    lines.append('                    continue\n')
    lines.append('            pattern += zenpy__escape_re(ch)\n')
    lines.append('            i += 1\n')
    lines.append('        pattern += "$"\n')
    lines.append('        repl = replace\n')
    lines.append('        return (pattern, captures, repl) if captures else None\n')
    lines.append('\n')
    lines.append('    if zenpy_strings != None and renpy.session["zenpy_len"](zenpy_strings) > 0:\n')
    lines.append('        for original, replace in zenpy_strings.items():\n')
    lines.append('            original = original.strip()\n')
    lines.append('            replace = replace.strip()\n')
    lines.append('            if not original or not replace or original == replace:\n')
    lines.append('                continue\n')
    lines.append('            zenpy_exact[original] = replace\n')
    lines.append(f'            renpy.session["zenpy_variables"]["{lang}"]["keyword_processor"].add_keyword(original, replace)\n')
    lines.append('            if ("{" in original and "}" in original) or ("[" in original and "]" in original):\n')
    lines.append('                rule = zenpy__compile_placeholder_rule(original, replace)\n')
    lines.append('                if rule:\n')
    lines.append('                    zenpy_placeholder_rules.append(rule)\n')
    lines.append('\n')
    lines.append(f'    renpy.session["zenpy_variables"]["{lang}"]["exact"] = zenpy_exact\n')
    lines.append(f'    renpy.session["zenpy_variables"]["{lang}"]["placeholder_rules"] = zenpy_placeholder_rules\n')
    lines.append('\n')
    lines.append(f'    zenpy_aliases = renpy.session["zenpy_set"](["{lang}", "Spanish", "spanish", "spanish_latino", "es", "Español", "Espanol"])\n')
    lines.append('    for zenpy_alias in zenpy_aliases:\n')
    lines.append('        if zenpy_alias not in renpy.session["zenpy_variables"]:\n')
    lines.append(f'            renpy.session["zenpy_variables"][zenpy_alias] = renpy.session["zenpy_variables"]["{lang}"]\n')
    lines.append('\n')
    lines.append('    if not "__next_replace__" in renpy.session["zenpy_variables"]:\n')
    lines.append('        renpy.session["zenpy_variables"]["__next_replace__"] = config.replace_text\n')
    lines.append('\n')
    lines.append('    if not renpy.config.custom_text_tags:\n')
    lines.append('        renpy.config.custom_text_tags["z" + "enpy_enable_tags"] = None\n')
    lines.append('\n')
    lines.append('    def zenpy__regex_match(pattern, text):\n')
    lines.append('        try:\n')
    lines.append('            import re as zenpy_re\n')
    lines.append('            return zenpy_re.match(pattern, text)\n')
    lines.append('        except Exception:\n')
    lines.append('            return None\n')
    lines.append('\n')
    lines.append('    def zenpy_text(text):\n')
    lines.append('        lang = _preferences.language\n')
    lines.append('        if lang in renpy.session["zenpy_variables"]:\n')
    lines.append('            zvars = renpy.session["zenpy_variables"][lang]\n')
    lines.append('            try:\n')
    lines.append('                if text in zvars.get("exact", {}):\n')
    lines.append('                    return zvars["exact"][text]\n')
    lines.append('                stripped = text.strip()\n')
    lines.append('                if stripped in zvars.get("exact", {}):\n')
    lines.append('                    return text.replace(stripped, zvars["exact"][stripped], 1)\n')
    lines.append('                for pattern, captures, repl in zvars.get("placeholder_rules", []):\n')
    lines.append('                    m = zenpy__regex_match(pattern, text)\n')
    lines.append('                    if m:\n')
    lines.append('                        out = repl\n')
    lines.append('                        for token, idx in captures:\n')
    lines.append('                            try:\n')
    lines.append('                                out = out.replace(token, m.group(idx))\n')
    lines.append('                            except Exception:\n')
    lines.append('                                pass\n')
    lines.append('                        return out\n')
    lines.append('            except Exception:\n')
    lines.append('                pass\n')
    lines.append('            return zvars["keyword_processor"].try_replace(text)\n')
    lines.append('        elif renpy.session["zenpy_variables"]["__next_replace__"] != None:\n')
    lines.append('            return renpy.session["zenpy_variables"]["__next_replace__"](text)\n')
    lines.append('        else:\n')
    lines.append('            return text\n')
    lines.append('\n')
    lines.append('    config.replace_text = zenpy_text\n')

    return ''.join(lines)


def write_zenpy_files(out_root: str, tl_lang: str, entries,
                      base_strings_json: str = '') -> None:
    """
    Genera strings.json LIGERO adaptado al juego + replaceText.rpy.

    El strings.json generado es SELECTIVO (optimizado para rendimiento):
    1. Toda la UI del juego (kind: string/source_text/source_define/source_character)
    2. Opciones de menú (kind: source_menu) — elecciones del jugador
    3. Mensajes/chats de conversaciones (category: 'phone' o contexto chat/sms)
    4. Del strings.json base solo se retienen entradas cortas/semi-cortas (≤50 chars)
       que sean de UI, menú o palabras sueltas que podrían faltar en la traducción
    5. NO se incluyen diálogos largos como fallback (esos ya van en los bloques tl/)

    Esto mantiene el archivo pequeño y sin lag en runtime.
    """
    import json as _json

    # ── 1. Cargar strings.json base (bundled con el traductor) ───────────────
    # Solo retener entradas cortas/semi-cortas: UI, menús y palabras clave
    base_map: Dict[str, str] = {}
    here = os.path.dirname(os.path.abspath(__file__))
    sj_candidates = []
    if base_strings_json:
        sj_candidates.append(base_strings_json)
    sj_candidates += [
        os.path.join(here, 'strings.json'),
        os.path.join(os.getcwd(), 'strings.json'),
    ]
    for cand in sj_candidates:
        if os.path.isfile(cand):
            try:
                with open(cand, 'r', encoding='utf-8') as _f:
                    raw_base = {str(k): str(v) for k, v in _json.load(_f).items() if v}
                # Del base solo guardamos entradas cortas/semi-cortas (≤50 chars)
                # que sean palabras de UI, menú o palabras sueltas
                # Excluimos los diálogos largos que ya estarán en tl/
                base_map = {
                    k: v for k, v in raw_base.items()
                    if len(k) <= 50 and v.strip() and v.strip() != k.strip()
                }
                break
            except Exception:
                pass

    # ── 2. Recolectar traducciones de esta sesión ────────────────────────────
    # Categorías que SÍ se incluyen siempre (UI + menús + chats)
    ui_kinds = {'string', 'source_text', 'source_define', 'source_character', 'raw_string'}
    menu_kinds = {'source_menu'}
    # Palabras de contexto que indican mensaje de chat/teléfono
    _phone_ctx = {'phone', 'sms', 'chat', 'msg', 'message', 'messages', 'inbox',
                  'call', 'whatsapp', 'insta', 'social', 'dialer', 'notification',
                  'conversation', 'dm', 'texting', 'messenger', 'send_message',
                  'add_sms', 'add_message', 'add_chat', 'send_sms', 'receive_message'}

    def _is_phone_entry(e: 'Entry') -> bool:
        """Detecta si una entrada pertenece a conversaciones/mensajes de teléfono."""
        if getattr(e, 'category', '') == 'phone':
            return True
        ctx = (getattr(e, 'context_label', '') or '').lower()
        if any(w in ctx for w in _phone_ctx):
            return True
        src_low = (e.source or '').strip().lower()
        if any(w in src_low for w in _phone_ctx):
            return True
        return False

    ui_map: Dict[str, str] = {}        # UI + GUI strings
    menu_map: Dict[str, str] = {}      # Opciones de menú (elecciones)
    phone_map: Dict[str, str] = {}     # Mensajes/chats de conversaciones
    # Diálogos muy cortos (≤30 chars) que podrían no tener bloque tl/ propio
    short_dialogue: Dict[str, str] = {}
    # raw_string: extraídos de código Python (notify, listas, dicts) — NUNCA
    # tienen bloque translate propio, así que SIEMPRE deben ir en strings.json
    # independientemente de si son cirílicos con puntuación final.
    raw_string_keys: set = set()

    for e in entries:
        if not e.source or not e.translation or not e.translation.strip():
            continue
        # Para menus: incluir SIEMPRE aunque traduccion == source (puede ser
        # que el MT no cambio el texto por ser nombre propio, numero, etc.)
        # Para el resto: excluir si no cambio nada (ahorra espacio)
        kind = getattr(e, 'kind', '')
        src = e.source
        is_menu = kind in menu_kinds
        if not is_menu and e.translation.strip() == src.strip():
            continue  # sin cambio real, no aporta nada al strings.json

        if kind in ui_kinds:
            if src not in ui_map:
                ui_map[src] = e.translation
            # marcar raw_string para eximir de _should_keep
            if kind == 'raw_string':
                raw_string_keys.add(src)
        elif is_menu:
            if src not in menu_map:
                menu_map[src] = e.translation
        elif _is_phone_entry(e):
            if src not in phone_map:
                phone_map[src] = e.translation
        elif kind in ('dialogue', 'source_say') and len(src) <= 30:
            # Solo diálogos MUY cortos (palabras/frases sueltas ≤30 chars)
            # que probablemente no tengan bloque translate propio
            if src not in short_dialogue:
                short_dialogue[src] = e.translation

    # ── 3. Merge: base_ligero → short_dialogue → menu → phone → ui (mayor prioridad gana)
    final_map: Dict[str, str] = {}
    final_map.update(base_map)
    final_map.update(short_dialogue)
    final_map.update(menu_map)
    final_map.update(phone_map)
    final_map.update(ui_map)   # UI siempre tiene prioridad máxima

    # ── 4. Limpieza: eliminar entradas sin traducción real + diálogos puros
    # Reglas (misma lógica que el filtro manual del strings.json bundled):
    #   CONSERVAR si:
    #     a) <=20 chars  → palabras de UI, botones, labels
    #     b) tiene [var] o {tag} de Ren'Py → UI dinámica, el replaceText las necesita
    #     c) solo latin/inglés → UI estándar de Ren'Py (Save, Load, etc.)
    #     d) cirílico/otro script SIN puntuación final → elección de menú o label
    #   EXCLUIR si:
    #     - cirílico/otro script CON puntuación final (. ! ? …) sin variables
    #       → diálogo narrativo que ya está cubierto por tl/ y no aporta en replaceText
    import re as _re
    _has_renpy_var = _re.compile(r'\[[^\]]+\]|\{[^}]+\}')
    _has_cyrillic  = _re.compile(r'[а-яА-ЯёЁ]')
    _ends_sentence = _re.compile(r'[.!?…]$')

    def _should_keep(k: str) -> bool:
        k = k.strip()
        # raw_string: extraídos de código Python (notify, listas, dicts, etc.)
        # NUNCA tienen bloque translate propio → siempre deben estar en strings.json
        if k in raw_string_keys:
            return True
        if len(k) <= 20:
            return True
        if _has_renpy_var.search(k):
            return True
        if not _has_cyrillic.search(k):   # solo latin/inglés = UI Ren'Py
            return True
        if not _ends_sentence.search(k):  # cirílico sin puntuación = menú/label
            return True
        return False                       # cirílico con puntuación = diálogo → fuera

    before = len(final_map)
    final_map = {k: v for k, v in final_map.items()
                 if v and v.strip() and v.strip() != k.strip() and _should_keep(k)}
    removed = before - len(final_map)

    # ── 5a. Inyectar mapa de nombres cirílicos (Элин → Elin, etc.) ──────────
    # Entran siempre, sin pasar por _should_keep, para que replaceText.rpy
    # pueda reemplazarlos en runtime sin importar longitud ni puntuación.
    try:
        for cyrillic_orig, translated_name in _last_cyrillic_name_map.items():
            if cyrillic_orig and translated_name and cyrillic_orig != translated_name:
                final_map[cyrillic_orig] = translated_name
    except Exception:
        pass

    # ── 5. Escribir strings.json del juego ──────────────────────────────────
    json_path = os.path.join(out_root, 'strings.json')
    with open(json_path, 'w', encoding='utf-8') as f:
        _json.dump(final_map, f, ensure_ascii=False, indent=2)
    print(f'[zenpy] strings.json → {json_path} '
          f'({len(ui_map)} UI + {len(menu_map)} menús + {len(phone_map)} chats '
          f'+ {len(short_dialogue)} frases cortas + {len(base_map)} base'
          f' = {len(final_map)} total, {removed} diálogos descartados)')

    # ── 5. Escribir replaceText.rpy ─────────────────────────────────────────
    rpy_content = _build_replace_text_rpy(tl_lang)
    rpy_path = os.path.join(out_root, 'replaceText.rpy')
    with open(rpy_path, 'w', encoding='utf-8') as f:
        f.write(rpy_content)
    print(f'[zenpy] replaceText.rpy → {rpy_path}')


# =============================================================
#  MODO D: generador de selector de idioma en-juego
# =============================================================

# Nombres bonitos para mostrar en el botón del selector
_LANG_DISPLAY: Dict[str, str] = {
    'spanish_latino': 'Español (Latam)',
    'spanish':        'Español (España)',
    'es':             'Español',
    'es-419':         'Español (Latam)',
    'english':        'English',
    'en':             'English',
    'portuguese':     'Português',
    'pt':             'Português',
    'pt-br':          'Português (BR)',
    'french':         'Français',
    'fr':             'Français',
    'german':         'Deutsch',
    'de':             'Deutsch',
    'italian':        'Italiano',
    'it':             'Italiano',
    'japanese':       '日本語',
    'ja':             '日本語',
    'chinese':        '中文',
    'zh':             '中文',
    'korean':         '한국어',
    'ko':             '한국어',
    'russian':        'Русский',
    'ru':             'Русский',
}

def _lang_display_name(folder_name: str) -> str:
    """Devuelve el nombre legible para mostrar en el botón del juego."""
    return _LANG_DISPLAY.get(folder_name.lower(), folder_name.replace('_', ' ').title())


def generate_language_selector(game_dir: str, tl_lang: str,
                               position: str = 'bottom_right') -> str:
    """
    Genera game/tl_language_selector.rpy — un archivo que añade un botón
    flotante en la esquina de la pantalla para cambiar de idioma en cualquier
    momento sin modificar nada del juego original.

    Detecta automáticamente todas las carpetas en game/tl/ para listar
    los idiomas disponibles.

    position: 'bottom_right' | 'bottom_left' | 'top_right' | 'top_left'
    Devuelve la ruta del archivo generado.
    """
    tl_root = os.path.join(game_dir, 'tl')
    out_path = os.path.join(game_dir, 'tl_language_selector.rpy')

    # Detectar carpetas de idioma disponibles en game/tl/
    available_langs: List[Tuple[str, str]] = []  # (folder_name, display_name)
    if os.path.isdir(tl_root):
        for entry in sorted(os.listdir(tl_root)):
            full = os.path.join(tl_root, entry)
            if os.path.isdir(full) and not entry.startswith('.'):
                available_langs.append((entry, _lang_display_name(entry)))

    # Siempre incluir inglés como opción base (idioma original del juego)
    # aunque no tenga carpeta en tl/ — Ren'Py lo soporta con None
    has_english = any(f in ('english', 'en') for f, _ in available_langs)
    if not has_english:
        available_langs.insert(0, ('None', 'English'))

    # Posición del botón
    anchor_map = {
        'bottom_right': ('xalign 1.0', 'yalign 1.0', 'xoffset -10', 'yoffset -10'),
        'bottom_left':  ('xalign 0.0', 'yalign 1.0', 'xoffset 10',  'yoffset -10'),
        'top_right':    ('xalign 1.0', 'yalign 0.0', 'xoffset -10', 'yoffset 10'),
        'top_left':     ('xalign 0.0', 'yalign 0.0', 'xoffset 10',  'yoffset 10'),
    }
    xalign, yalign, xoffset, yoffset = anchor_map.get(position, anchor_map['bottom_right'])

    # Construir bloque de opciones del menú de idioma
    lang_items = []
    for folder, display in available_langs:
        lang_val = 'None' if folder == 'None' else f'"{folder}"'
        lang_items.append(
            f'            textbutton _("{display}"):\n'
            f'                action [SetField(persistent, "_language_choice", {lang_val}),\n'
            f'                        Language({lang_val}),\n'
            f'                        Hide("language_selector_popup")]\n'
            f'                style "tl_sel_item"\n'
            f'                text_style "tl_sel_item_text"\n'
            f'                selected (persistent._language_choice == {lang_val})\n'
        )

    lang_block = '\n'.join(lang_items)

    # Nombre del idioma activo para el botón principal
    lang_label_cases = []
    for folder, display in available_langs:
        lang_val = 'None' if folder == 'None' else f'"{folder}"'
        lang_label_cases.append(
            f'    $ _tl_label = _("{display}") if persistent._language_choice == {lang_val} else _tl_label'
        )
    lang_label_block = '\n'.join(lang_label_cases)

    rpy_content = f'''\
## ============================================================
## tl_language_selector.rpy — generado por RenpyTranslator
## Selector de idioma flotante (esquina {position.replace("_", " ")})
## No modifica ningún archivo original del juego.
## Para desinstalar: borra este archivo.
## ============================================================

init -1 python:
    if not hasattr(persistent, "_language_choice"):
        persistent._language_choice = None  # None = inglés original


## Overlay: se muestra en TODAS las pantallas automáticamente
screen tl_language_overlay():
    zorder 50
    frame:
        {xalign}
        {yalign}
        {xoffset}
        {yoffset}
        background "#00000088"
        padding (6, 4)
        style_prefix "tl_sel"
        textbutton "🌐":
            action Show("language_selector_popup", transition=dissolve)
            tooltip _("Cambiar idioma / Change language")
            style "tl_sel_globe_btn"


## Popup con la lista de idiomas disponibles
screen language_selector_popup():
    modal True
    zorder 201

    ## click fuera cierra el popup
    button:
        xfill True
        yfill True
        action Hide("language_selector_popup")
        background "#00000000"

    frame:
        {xalign}
        {yalign}
        {xoffset}
        {yoffset}
        background "#1a1a1aee"
        padding (12, 10)
        vbox:
            spacing 4
            text _("Seleccionar idioma") style "tl_sel_title_text"
            null height 6

{lang_block}


## Registrar el overlay en todas las pantallas que usen show_overlays
## Ren'Py llama a los overlays definidos en config.overlay_screens automáticamente
init python:
    if hasattr(config, "overlay_screens"):
        if "tl_language_overlay" not in config.overlay_screens:
            config.overlay_screens.append("tl_language_overlay")
    else:
        config.overlay_screens = ["tl_language_overlay"]


## Estilos del selector — minimalistas, compatibles con cualquier juego
style tl_sel_globe_btn:
    padding (8, 6)
    hover_background "#ffffff22"
    background "#00000000"

style tl_sel_globe_btn_text:
    size 20
    color "#ffffffcc"
    hover_color "#ffffff"

style tl_sel_item:
    padding (10, 6)
    xfill True
    hover_background "#ffffff18"
    background "#00000000"

style tl_sel_item_text:
    size 16
    color "#e0e0e0"
    hover_color "#ffffff"
    selected_color "#29b6f6"
    selected_bold True

style tl_sel_title_text:
    size 13
    color "#aaaaaa"
    bold False
'''

    with open(out_path, 'w', encoding='utf-8') as f:
        f.write(rpy_content)

    return out_path


# =============================================================
#  GENERADOR: screens.rpy de traducción (fix visual choice)
# =============================================================
def generate_screens_rpy(game_dir: str, tl_lang: str) -> str:
    """
    Genera game/tl/<lang>/screens.rpy con:
    - translate <lang> strings: con todos los strings de UI del juego
    - Fix visual de choice_button para evitar barras negras
    Devuelve la ruta del archivo generado.
    """
    out_dir = os.path.join(game_dir, 'tl', tl_lang)
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, 'screens.rpy')

    # Leer strings.json si existe (junto al parser o en game/)
    strings_map: Dict[str, str] = {}
    for candidate in [
        os.path.join(os.path.dirname(__file__), 'strings.json'),
        os.path.join(game_dir, 'strings.json'),
        os.path.join(os.path.dirname(__file__), '..', 'strings.json'),
    ]:
        if os.path.isfile(candidate):
            try:
                with open(candidate, 'r', encoding='utf-8') as f:
                    raw = json.load(f)
                # limpiar prefijos NOTRADUCIR
                for k, v in raw.items():
                    clean_k = re.sub(r'^NOTRADUCIR', '', k)
                    strings_map[clean_k] = v
            except Exception:
                pass
            break

    # Extraer todos los strings de UI del screens.rpy del juego
    ui_strings: List[Tuple[str, str]] = []  # (comentario_origen, texto)
    screens_src = os.path.join(game_dir, 'screens.rpy')
    if os.path.isfile(screens_src):
        with open(screens_src, 'r', encoding='utf-8', errors='replace') as f:
            src_lines = f.readlines()
        for i, line in enumerate(src_lines):
            m = re.match(r'^\s+(?:text|textbutton|label)\s+"((?:[^"\\]|\\.)+)"', line)
            if m:
                txt = _unescape(m.group(1))
                if _is_translatable(txt):
                    ui_strings.append((f'game/screens.rpy:{i+1}', txt))

    # Construir bloque translate strings
    string_blocks = []
    for origin, txt in ui_strings:
        tl = strings_map.get(txt, txt)  # si no hay traducción, usar original
        esc_old = _escape(txt)
        esc_new = _escape(tl)
        string_blocks.append(
            f'    # {origin}\n'
            f'    old "{esc_old}"\n'
            f'    new "{esc_new}"\n'
        )

    strings_section = ''
    if string_blocks:
        strings_section = f'translate {tl_lang} strings:\n\n' + '\n'.join(string_blocks)

    content = f'''\
# screens.rpy — traducción UI — generado por RenpyTranslator
# Idioma: {tl_lang}

{strings_section}
'''

    with open(out_path, 'w', encoding='utf-8') as f:
        f.write(content)
    return out_path


# =============================================================
#  GENERADOR: replaceText.rpy adaptado al juego
# =============================================================
def generate_replace_text(game_dir: str, tl_lang: str,
                          entries: Optional[List[Entry]] = None) -> str:
    """
    Genera el pack estilo HZ/Zenpy dentro de game/tl/<lang>/:
    - strings.json con UI/raw/dialogue fallback
    - replaceText.rpy runtime con KeywordProcessor
    Devuelve la ruta del replaceText.rpy generado.
    """
    out_dir = os.path.join(game_dir, 'tl', tl_lang)
    os.makedirs(out_dir, exist_ok=True)
    write_zenpy_files(out_dir, tl_lang, entries or [])
    return os.path.join(out_dir, 'replaceText.rpy')


# Mantener nombre antiguo para compatibilidad con la pestaña Herramientas

def write_translations(root_in: str, root_out: str, entries: List[Entry]) -> int:
    """Compat: si el usuario sigue usando el botón viejo. Detecta modo:
       - si hay entradas con is_source=True → genera tl/ válido en root_out
       - si no → reescribe los .rpy originales (modo TL existente, original)."""
    has_source = any(getattr(e, 'is_source', False) for e in entries)
    if has_source:
        # root_out se interpreta como destino (ej. game/tl/spanish_latino/)
        # detectar el lang del path
        parts = os.path.normpath(root_out).split(os.sep)
        tl_lang = parts[-1] if parts else 'spanish_latino'
        n_files, n_entries = write_tl_files(root_in, tl_lang, entries, out_root=root_out)
        return n_entries

    # ---- modo legacy: reescribir bloques translate existentes ----
    by_file: Dict[str, List[Entry]] = {}
    for e in entries:
        by_file.setdefault(e.file, []).append(e)
    changed = 0
    for rel, file_entries in by_file.items():
        src = os.path.join(root_in, rel); dst = os.path.join(root_out, rel)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        with open(src, 'r', encoding='utf-8', errors='replace') as f:
            lines = f.readlines()
        file_entries.sort(key=lambda x: x.line_idx)
        for e in file_entries:
            if not e.translation: continue
            idx = e.line_idx
            if idx < 0 or idx >= len(lines): continue
            line = lines[idx]
            if e.kind == 'string':
                for j in range(idx+1, min(idx+6, len(lines))):
                    mn = RE_NEW.match(lines[j])
                    if mn:
                        lines[j] = f'{mn.group(1)}new "{_escape(e.translation)}"\n'
                        changed += 1; break
            else:
                md = RE_DIALOGUE.match(line)
                if md:
                    indent = md.group(1); speaker = md.group(2) or ''; rest = md.group(4) or ''
                    sp_part = (speaker + ' ') if speaker else ''
                    lines[idx] = f'{indent}{sp_part}"{_escape(e.translation)}"{rest}\n'
                    changed += 1
        with open(dst, 'w', encoding='utf-8') as f:
            f.writelines(lines)
    return changed


# =============================================================
#  NUEVO MODO: Rellenar traducciones existentes (Fill Mode)
#  Para archivos que ya tienen formato:
#      translate Spanish xxx:
#          # mc "Texto original..."
#          mc ""
# =============================================================

def parse_and_fill_file(path: str, base: str = '', lang: str = '') -> List[Entry]:
    """Parsea archivos .rpy que ya tienen bloques translate y prepara las entradas
    para rellenar solo la línea vacía (speaker "" / speaker "" with dissolve).
    Si lang está vacío, acepta cualquier idioma."""
    rel = os.path.relpath(path, base) if base else os.path.basename(path)
    with open(path, 'r', encoding='utf-8', errors='replace') as f:
        lines = f.readlines()

    entries: List[Entry] = []
    i = 0
    n = len(lines)

    # regex para línea vacía: mc ""  /  mc "" with dissolve  /  ""  /  "" with dissolve
    RE_EMPTY_LINE = re.compile(r'^(\s*)(?:("(?:[^"\\]|\\.)*"|\w+)\s+)?""\s*(with\s+\S+)?\s*$')

    while i < n:
        line = lines[i]

        m = RE_TRANSLATE_BLOCK.match(line)
        if m and (not lang or m.group(2).lower() == lang.lower()):
            indent = m.group(1)
            block_id = m.group(3)
            i += 1

            speaker = ""
            original_text = ""
            target_line_idx = -1
            target_line_raw = ""
            target_suffix = ""

            while i < n:
                sub = lines[i]
                sub_stripped = sub.strip()

                # fin de bloque: línea con menos o igual indentación (no vacía ni comentario)
                if sub_stripped and not sub_stripped.startswith('#'):
                    cur_ind = len(sub) - len(sub.lstrip())
                    if cur_ind <= len(indent):
                        break

                # comentario con texto original: # mc "texto..."  o  # "texto..."
                if sub_stripped.startswith('#') and not original_text:
                    cm = re.search(r'#\s*(?:("(?:[^"\\]|\\.)*"|\w+)\s+)?"((?:[^"\\]|\\.)*)"', sub)
                    if cm:
                        speaker = (cm.group(1) or '').strip()
                        original_text = _unescape(cm.group(2))

                # línea vacía: mc ""  /  ""  /  mc "" with dissolve
                elif target_line_idx == -1:
                    me = RE_EMPTY_LINE.match(sub)
                    if me:
                        target_line_idx = i
                        target_line_raw = sub
                        target_suffix = (' ' + me.group(3)) if me.group(3) else ''
                        if not speaker and me.group(2):
                            speaker = me.group(2)

                i += 1

            if original_text and target_line_idx != -1:
                # Si no es traducible (ej: "...", "Hi."), igual creamos el entry
                # pero con translation = source para que se copie el original
                is_tr = _is_translatable(original_text)
                cat = classify(block_id + " " + speaker, source_text=original_text)
                entries.append(Entry(
                    file=rel,
                    kind='dialogue',
                    block_id=block_id,
                    speaker=speaker,
                    source=original_text,
                    translation=original_text if not is_tr else "",
                    line_idx=target_line_idx,
                    category=cat,
                    raw_old_line=target_line_raw,
                    indent=indent + '    ',  # indent de la línea de diálogo
                    is_source=False,
                    active_label=target_suffix,  # reutilizamos active_label para guardar suffix
                ))
            elif original_text and target_line_idx == -1:
                # Bloque translate encontrado pero sin línea vacía — puede ser un bloque ya traducido
                # o con formato inesperado. Lo registramos para debug.
                pass
            continue

        i += 1

    return entries


def fill_sdk_tl_directory(sdk_tl_dir: str, entries: List[Entry],
                          lang: str = '', backup: bool = True) -> Tuple[int, int]:
    """
    MODO FILL SDK: Escribe las traducciones directamente en los archivos del SDK
    usando line_idx (la posición exacta de la línea vacía) de cada Entry.
    Preserva IDs reales, with dissolve, y cualquier suffix de la línea original.

    - sdk_tl_dir: carpeta tl/Spanish/ generada por el SDK
    - entries: entradas con traducción ya cargada (vienen de scan_sdk_tl_directory)
    - backup: crea .rpy.bak antes de modificar
    - Retorna (n_archivos_modificados, n_lineas_escritas)
    """
    import shutil

    # Agrupar entries con traducción por archivo
    # entry.file es relativo a sdk_tl_dir
    # IMPORTANTE: incluir source_menu, source_text, source_say, etc.
    # No solo 'dialogue'/'string' — los menus y UI tambien necesitan escribirse
    _FILLABLE_KINDS = {
        'dialogue', 'string',
        'source_say', 'source_menu', 'source_text',
        'source_define', 'source_character', 'raw_string',
    }
    from collections import defaultdict
    by_file: Dict[str, List[Entry]] = defaultdict(list)
    for e in entries:
        if e.kind not in _FILLABLE_KINDS:
            continue
        if e.translation and e.translation.strip():
            by_file[e.file].append(e)

    files_modified = 0
    lines_written = 0

    for rel_path, file_entries in by_file.items():
        # Resolver ruta absoluta — múltiples estrategias de búsqueda
        full = os.path.join(sdk_tl_dir, rel_path)
        if not os.path.isfile(full):
            # Estrategia 2: buscar solo el nombre del archivo en sdk_tl_dir
            full = os.path.join(sdk_tl_dir, os.path.basename(rel_path))
        if not os.path.isfile(full):
            # Estrategia 3: buscar recursivamente por nombre en todo sdk_tl_dir
            basename = os.path.basename(rel_path)
            found = None
            for dp, _, fns in os.walk(sdk_tl_dir):
                if basename in fns:
                    candidate = os.path.join(dp, basename)
                    # Verificar que el contenido parezca un archivo .rpy de traducción
                    try:
                        with open(candidate, 'r', encoding='utf-8', errors='replace') as cf:
                            first_lines = cf.read(2000)
                        # Debe contener bloques translate o strings
                        if 'translate ' in first_lines or 'old "' in first_lines:
                            found = candidate
                            break
                    except Exception:
                        pass
            if found:
                full = found
        if not os.path.isfile(full):
            print(f'[fill_sdk] archivo no encontrado: {rel_path} (buscado como {basename})')
            continue

        try:
            with open(full, 'r', encoding='utf-8', errors='replace') as f:
                lines = f.readlines()
        except Exception as e:
            print(f'[fill_sdk read] {full}: {e}')
            continue

        modified = False
        for e in file_entries:
            idx = e.line_idx
            if idx < 0 or idx >= len(lines):
                continue
            original_line = lines[idx]
            escaped = _escape(e.translation)

            # Entry de tipo string (bloque translate strings: / new "")
            if e.kind == 'string' or e.active_label == 'new':
                mn = re.match(r'^(\s*)new ""\s*$', original_line)
                if mn:
                    lines[idx] = f'{mn.group(1)}new "{escaped}"\n'
                    lines_written += 1
                    modified = True
                continue

            # Entry de tipo dialogue (bloque translate <id>: / mc "")
            # El speaker puede ser: una palabra (\w+) como "mc", "narrator",
            # o una cadena entre comillas como "???" — ambos casos soportados.
            me = re.match(r'^(\s*)(?:("(?:[^"\\]|\\.)*"|\w+)\s+)?""\s*(with\s+\S+)?\s*$', original_line)
            if not me:
                continue
            line_indent = me.group(1)
            # me.group(2) puede ser: 'mc', '"???"', o None
            # e.speaker viene del comentario: 'mc', '"???"', o ''
            line_speaker = me.group(2) or e.speaker
            line_suffix = (' ' + me.group(3)) if me.group(3) else (e.active_label or '')
            if line_speaker and line_speaker not in ('new', 'old'):
                lines[idx] = f'{line_indent}{line_speaker} "{escaped}"{line_suffix}\n'
            else:
                lines[idx] = f'{line_indent}"{escaped}"{line_suffix}\n'
            lines_written += 1
            modified = True

        if modified:
            if backup:
                bak = full + '.bak'
                if not os.path.exists(bak):
                    try:
                        shutil.copy2(full, bak)
                    except Exception as e:
                        print(f'[fill_sdk backup] {full}: {e}')
            try:
                with open(full, 'w', encoding='utf-8') as f:
                    f.writelines(lines)
                files_modified += 1
            except Exception as e:
                print(f'[fill_sdk write] {full}: {e}')

    return (files_modified, lines_written)


def apply_strings_map(entries: List[Entry], strings_map: Dict[str, str]) -> int:
    """
    Pre-rellena entries con traducciones del strings_map (strings.json) antes de
    mandar al motor de traducción. Evita que strings de UI comunes vayan a MT.
    Devuelve el número de entradas pre-rellenadas.
    La comparación es case-insensitive para capturar "QUIT" → "Quit" etc.
    """
    if not strings_map:
        return 0
    # Construir lookup insensible a mayúsculas: lower(key) → translation
    ci_map: Dict[str, str] = {}
    exact_map: Dict[str, str] = {}
    for k, v in strings_map.items():
        if v and v.strip():
            exact_map[k] = v
            ci_map[k.lower()] = v

    filled = 0
    for e in entries:
        if e.translation and e.translation.strip():
            continue  # ya tiene traducción
        src = e.source
        if not src:
            continue
        # 1. Coincidencia exacta
        tl = exact_map.get(src)
        # 2. Coincidencia case-insensitive
        if not tl:
            tl = ci_map.get(src.lower())
        if tl and tl.strip() and tl.strip() != src.strip():
            e.translation = tl
            filled += 1
    return filled


def load_strings_json(path: str) -> Dict[str, str]:
    """
    Carga un strings.json y devuelve un dict {original: traducción}.
    Busca en múltiples ubicaciones si el path no es absoluto.
    """
    candidates = [path]
    # si no es absoluto, buscar junto al parser y en el directorio actual
    if path and not os.path.isabs(path):
        here = os.path.dirname(os.path.abspath(__file__))
        candidates = [
            path,
            os.path.join(here, path),
            os.path.join(here, 'strings.json'),
            os.path.join(os.getcwd(), 'strings.json'),
        ]

    for cand in candidates:
        if os.path.isfile(cand):
            try:
                with open(cand, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                return {str(k): str(v) for k, v in data.items() if v}
            except Exception as e:
                print(f'[strings.json] Error cargando {cand}: {e}')
    return {}


def scan_sdk_tl_directory(sdk_tl_dir: str, lang: str = '',
                          strings_json_path: str = '') -> List[Entry]:
    """
    Escanea la carpeta tl/<lang>/ generada por el SDK de Ren'Py y extrae
    todas las entradas con líneas vacías listas para traducir.
    Preserva los IDs reales del SDK (comenzar_xxx, start_xxx, etc).
    Si lang está vacío o no coincide con los archivos, lo auto-detecta.

    strings_json_path: ruta al strings.json con traducciones de UI conocidas.
    Si se pasa (o se encuentra automáticamente junto al parser), pre-rellena
    entradas de UI antes de devolverlas → esas no van al motor de traducción.
    """
    # Auto-detectar el idioma real desde los archivos si el lang no matchea
    detected_lang = lang
    if sdk_tl_dir and os.path.isdir(sdk_tl_dir):
        for fn in os.listdir(sdk_tl_dir):
            if fn.endswith('.rpy'):
                try:
                    with open(os.path.join(sdk_tl_dir, fn), 'r', encoding='utf-8', errors='replace') as f:
                        for line in f:
                            m = RE_TRANSLATE_BLOCK.match(line)
                            if m:
                                detected_lang = m.group(2)
                                break
                    if detected_lang:
                        break
                except Exception:
                    pass

    if detected_lang != lang:
        print(f'[scan_sdk] Auto-detectado idioma: {detected_lang!r} (config tenía: {lang!r})')

    entries: List[Entry] = []
    for dirpath, _, files in os.walk(sdk_tl_dir):
        for fn in files:
            if (fn.endswith('.rpy') or fn.endswith('.rpym')) and not fn.endswith('.rpyc'):
                full = os.path.join(dirpath, fn)
                rel = os.path.relpath(full, sdk_tl_dir)
                try:
                    # Escanear bloques translate <id>: con líneas vacías
                    entries.extend(parse_and_fill_file(full, base=sdk_tl_dir, lang=detected_lang))
                    # Escanear bloques translate strings: con new "" vacíos
                    entries.extend(_scan_strings_block(full, base=sdk_tl_dir, lang=detected_lang))
                except Exception as e:
                    print(f'[scan_sdk error] {full}: {e}')

    # ── Pre-rellenar desde strings.json ─────────────────────────────────────
    # Busca el strings.json: primero el path explícito, luego junto al parser,
    # luego junto al sdk_tl_dir (por si el usuario lo copió ahí).
    here = os.path.dirname(os.path.abspath(__file__))
    sj_candidates = []
    if strings_json_path:
        sj_candidates.append(strings_json_path)
    sj_candidates += [
        os.path.join(here, 'strings.json'),
        os.path.join(os.path.dirname(sdk_tl_dir), 'strings.json'),
        os.path.join(sdk_tl_dir, 'strings.json'),
        os.path.join(os.getcwd(), 'strings.json'),
    ]
    strings_map: Dict[str, str] = {}
    for cand in sj_candidates:
        if os.path.isfile(cand):
            try:
                with open(cand, 'r', encoding='utf-8') as _f:
                    strings_map = {str(k): str(v) for k, v in json.load(_f).items() if v}
                print(f'[scan_sdk] strings.json cargado: {cand} ({len(strings_map)} entradas)')
                break
            except Exception as _e:
                print(f'[scan_sdk] Error cargando strings.json {cand}: {_e}')

    if strings_map:
        pre_filled = apply_strings_map(entries, strings_map)
        if pre_filled:
            print(f'[scan_sdk] {pre_filled} entradas pre-rellenadas desde strings.json')

    return entries


def _scan_strings_block(path: str, base: str = '', lang: str = '') -> List[Entry]:
    """
    Escanea bloques `translate <lang> strings:` buscando entradas con `new ""` vacío.
    Retorna entries listas para traducir (source = old, translation = "").
    """
    rel = os.path.relpath(path, base) if base else os.path.basename(path)
    try:
        with open(path, 'r', encoding='utf-8', errors='replace') as f:
            lines = f.readlines()
    except Exception:
        return []

    entries: List[Entry] = []
    n = len(lines)
    i = 0
    in_strings_block = False

    while i < n:
        line = lines[i]
        # detectar inicio de bloque translate strings
        ms = RE_TRANSLATE_STRINGS.match(line)
        if ms and (not lang or ms.group(2).lower() == lang.lower()):
            in_strings_block = True
            i += 1
            continue

        # salir del bloque si encontramos otra directiva de primer nivel
        if in_strings_block and line.strip() and not line.startswith(' ') and not line.startswith('\t'):
            if not line.startswith('#'):
                in_strings_block = False

        if in_strings_block:
            # buscar: old "texto"  seguido de  new ""
            mo = re.match(r'^\s+old "((?:[^"\\]|\\.)*)"\s*$', line)
            if mo:
                old_text = _unescape(mo.group(1))
                # buscar el new "" en las siguientes líneas
                j = i + 1
                while j < n and j < i + 4:
                    mn = re.match(r'^(\s+)new ""\s*$', lines[j])
                    if mn:
                        if old_text and old_text.strip() and _is_translatable_ui(old_text):
                            cat = classify('menu', source_text=old_text)
                            entries.append(Entry(
                                file=rel,
                                kind='string',
                                block_id='',
                                speaker='',
                                source=old_text,
                                translation='',
                                line_idx=j,  # índice de la línea new ""
                                category=cat,
                                raw_old_line=lines[j],
                                indent=mn.group(1),
                                is_source=False,
                                active_label='new',  # marca que es bloque new
                            ))
                        elif old_text and old_text.strip():
                            # No es traducible UI pero el SDK lo generó → copiar original
                            cat = classify('menu', source_text=old_text)
                            entries.append(Entry(
                                file=rel,
                                kind='string',
                                block_id='',
                                speaker='',
                                source=old_text,
                                translation=old_text,  # ← copia original
                                line_idx=j,
                                category=cat,
                                raw_old_line=lines[j],
                                indent=mn.group(1),
                                is_source=False,
                                active_label='new',
                            ))
                        break
                    elif lines[j].strip() and not lines[j].strip().startswith('#'):
                        break  # nueva entrada, no encontramos new ""
                    j += 1
        i += 1

    return entries


#  Busca bloques translate <lang> <id>: con línea vacía mc ""
#  y los rellena directamente en el archivo fuente.
#  Es el único método que funciona en juegos que no cargan tl/.
# =============================================================

def _find_all_tl_empty_lines(path: str, lang: str, base: str = '') -> List[Entry]:
    """
    Escanea un .rpy original buscando bloques:
        translate <lang> <id>:
            # mc "texto original"
            mc ""              ← línea vacía que hay que rellenar

    Devuelve entradas con line_idx apuntando exactamente a la línea mc "".
    Solo captura entradas donde la línea de diálogo está vacía ("").
    """
    rel = os.path.relpath(path, base) if base else os.path.basename(path)
    try:
        with open(path, 'r', encoding='utf-8', errors='replace') as f:
            lines = f.readlines()
    except Exception as e:
        print(f'[inplace read error] {path}: {e}')
        return []

    entries: List[Entry] = []
    n = len(lines)
    i = 0

    # regex para línea de diálogo vacía: mc ""  /  "narrator" ""  / narrator ""
    RE_EMPTY_DIALOGUE = re.compile(r'^(\s*)(\w+|"[^"]*")\s*""\s*$')
    # también: solo ""  (narrator sin nombre)
    RE_EMPTY_NARR     = re.compile(r'^(\s*)""\s*$')

    while i < n:
        line = lines[i]
        m = RE_TRANSLATE_BLOCK.match(line)
        if not m:
            i += 1; continue

        tl_lang_found = m.group(2)
        block_id      = m.group(3)
        block_indent  = len(m.group(1))

        # solo nos interesa el idioma que el usuario eligió
        if tl_lang_found.lower() != lang.lower():
            i += 1; continue

        i += 1
        original_text = ''
        speaker       = ''
        empty_line_idx   = -1
        empty_line_raw   = ''
        empty_speaker    = ''
        empty_indent     = ''

        while i < n:
            sub     = lines[i]
            stripped = sub.strip()

            # fin de bloque: línea con menos o igual indent que el translate (y no vacía/comentario)
            if stripped and not stripped.startswith('#'):
                cur_indent = len(sub) - len(sub.lstrip())
                if cur_indent <= block_indent:
                    break

            # comentario con original:  # mc "texto"  /  # "texto"
            if stripped.startswith('#') and not original_text:
                cm = re.search(r'#\s*(?:("(?:[^"\\]|\\.)*"|\w+)\s+)?"((?:[^"\\]|\\.)*)"', sub)
                if cm:
                    speaker       = (cm.group(1) or '').strip()
                    original_text = _unescape(cm.group(2))

            # línea de diálogo vacía: mc "" / "narrator" "" / ""
            me = RE_EMPTY_DIALOGUE.match(sub)
            if me and not empty_line_idx != -1:
                empty_indent    = me.group(1)
                empty_speaker   = me.group(2)
                empty_line_idx  = i
                empty_line_raw  = sub
                i += 1; continue

            mn2 = RE_EMPTY_NARR.match(sub)
            if mn2 and not empty_line_idx != -1:
                empty_indent   = mn2.group(1)
                empty_speaker  = ''
                empty_line_idx = i
                empty_line_raw = sub
                i += 1; continue

            i += 1

        if original_text and empty_line_idx != -1 and _is_translatable(original_text):
            cat = classify(block_id + ' ' + (speaker or empty_speaker), source_text=original_text)
            entries.append(Entry(
                file=rel,
                kind='dialogue',
                block_id=block_id,
                speaker=speaker or empty_speaker,
                source=original_text,
                translation='',
                line_idx=empty_line_idx,
                category=cat,
                raw_old_line=empty_line_raw,
                indent=empty_indent,
                is_source=False,
            ))

    return entries


def scan_inplace_directory(game_dir: str, lang: str) -> List[Entry]:
    """
    Escanea TODA game/ (incluyendo subcarpetas tl/<lang>/) buscando
    bloques translate <lang>: con líneas vacías.
    Devuelve las entradas listas para traducir y luego escribir in-place.
    """
    entries: List[Entry] = []
    for dirpath, dirs, files in os.walk(game_dir):
        for fn in files:
            if (fn.endswith('.rpy') or fn.endswith('.rpym')) and not fn.endswith('.rpyc'):
                full = os.path.join(dirpath, fn)
                try:
                    entries.extend(_find_all_tl_empty_lines(full, lang, base=game_dir))
                except Exception as e:
                    print(f'[inplace scan error] {full}: {e}')
    return entries


def write_inplace_tl(game_dir: str, entries: List[Entry],
                     backup: bool = True) -> Tuple[int, int]:
    """
    Escribe las traducciones DIRECTAMENTE en los .rpy originales,
    reemplazando la línea mc "" por mc "traducción".

    - backup=True crea .rpy.bak antes de modificar (recomendado)
    - Retorna (n_archivos_modificados, n_lineas_escritas)
    """
    # agrupar por archivo
    by_file: Dict[str, List[Entry]] = {}
    for e in entries:
        if not e.translation or not e.translation.strip():
            continue
        by_file.setdefault(e.file, []).append(e)

    files_modified = 0
    lines_written  = 0

    for rel, file_entries in by_file.items():
        # buscar el archivo real: puede estar en game/ o en subdirectorios
        abs_path = os.path.join(game_dir, rel)
        if not os.path.isfile(abs_path):
            # fallback: buscar por nombre en el árbol
            fn = os.path.basename(rel)
            found = None
            for dp, _, fns in os.walk(game_dir):
                if fn in fns:
                    candidate = os.path.join(dp, fn)
                    if os.path.relpath(candidate, game_dir) == rel:
                        found = candidate; break
                    if found is None:
                        found = candidate
            if not found:
                print(f'[inplace] No encontrado: {rel}'); continue
            abs_path = found

        try:
            with open(abs_path, 'r', encoding='utf-8', errors='replace') as f:
                lines = f.readlines()
        except Exception as e:
            print(f'[inplace read] {abs_path}: {e}'); continue

        # backup antes de tocar
        if backup:
            bak = abs_path + '.bak'
            if not os.path.exists(bak):  # solo hacer backup la primera vez
                try:
                    import shutil
                    shutil.copy2(abs_path, bak)
                except Exception as e:
                    print(f'[inplace backup warn] {abs_path}: {e}')

        modified = False
        for e in file_entries:
            idx = e.line_idx
            if idx < 0 or idx >= len(lines):
                print(f'[inplace] Línea {idx} fuera de rango en {rel}'); continue

            raw = lines[idx]
            escaped_tl = _escape(e.translation)

            # Caso 1: mc ""  →  mc "traducción"
            me = re.match(r'^(\s*)(\w+|"[^"]*")\s*""\s*$', raw)
            if me:
                lines[idx] = f'{me.group(1)}{me.group(2)} "{escaped_tl}"\n'
                lines_written += 1; modified = True; continue

            # Caso 2: ""  (narrator sin nombre) →  "traducción"
            mn2 = re.match(r'^(\s*)""\s*$', raw)
            if mn2:
                lines[idx] = f'{mn2.group(1)}"{escaped_tl}"\n'
                lines_written += 1; modified = True; continue

            # Caso 3: la línea ya tiene texto (fue rellenada antes) — saltar
            # No sobreescribimos texto existente

        if modified:
            try:
                with open(abs_path, 'w', encoding='utf-8') as f:
                    f.writelines(lines)
                files_modified += 1
            except Exception as e:
                print(f'[inplace write] {abs_path}: {e}')

    return (files_modified, lines_written)
def entries_to_json(entries: List[Entry]) -> str:
    return json.dumps([asdict(e) for e in entries], ensure_ascii=False, indent=2)

def entries_from_json(s: str) -> List[Entry]:
    return [Entry(**d) for d in json.loads(s)]


# =============================================================
#  INTEGRACIÓN SDK  — auto-generate tl/<lang>/ con Ren'Py SDK
# =============================================================

def find_renpy_exe(sdk_path: str) -> Optional[str]:
    """
    Localiza el ejecutable de Ren'Py SDK dado un path base.
    Soporta Windows (.exe) y Linux/Mac (.sh).
    También busca en sub-carpetas comunes si el path no tiene el exe directo.
    """
    sdk_path = os.path.abspath(sdk_path) if sdk_path else ''
    if not sdk_path:
        return None

    # Candidatos en orden de preferencia
    candidates = [
        os.path.join(sdk_path, 'renpy.exe'),
        os.path.join(sdk_path, 'renpy.sh'),
        os.path.join(sdk_path, 'renpy'),
        # a veces el SDK viene con la versión en el exe
        os.path.join(sdk_path, 'renpy-8.5.2.exe'),
        os.path.join(sdk_path, 'renpy-8.4.0.exe'),
        # sub-carpeta launcher
        os.path.join(sdk_path, 'launcher', 'renpy.exe'),
    ]
    for c in candidates:
        if os.path.isfile(c):
            return c

    # Buscar cualquier renpy*.exe en el sdk_path (un nivel)
    if os.path.isdir(sdk_path):
        for fn in os.listdir(sdk_path):
            if fn.lower().startswith('renpy') and (fn.endswith('.exe') or fn.endswith('.sh')):
                full = os.path.join(sdk_path, fn)
                if os.path.isfile(full):
                    return full
    return None


def run_sdk_generate_tl(
    game_dir: str,
    lang: str,
    sdk_path: str = r'C:\renpy-8.5.2-sdk',
    timeout: int = 180,
    progress_cb: Optional[Callable] = None,
) -> Tuple[bool, str]:
    """
    Ejecuta el SDK de Ren'Py para generar los archivos de traducción vacíos en
    game/tl/<lang>/ usando el comando:
        renpy.exe <project_dir> translate <lang>

    Parámetros:
        game_dir   : ruta a la carpeta game/ del proyecto
        lang       : nombre del idioma (ej: 'Spanish', 'spanish_latino')
        sdk_path   : ruta al SDK de Ren'Py (ej: C:\\renpy-8.5.2-sdk)
        timeout    : segundos máx. de espera (default 180)
        progress_cb: callback(mensaje: str) para mostrar progreso en UI

    Devuelve:
        (True, '')       si OK
        (False, 'error') si falló
    """
    import subprocess

    def _log(msg: str):
        print(f'[sdk] {msg}')
        if progress_cb:
            progress_cb(msg)

    # 1. Verificar que game_dir existe
    if not game_dir or not os.path.isdir(game_dir):
        return False, f'game_dir no encontrado: {game_dir!r}'

    # 2. Obtener la carpeta PADRE del proyecto (la que contiene game/)
    project_dir = os.path.dirname(game_dir)
    if not os.path.isdir(project_dir):
        return False, f'project_dir no encontrado: {project_dir!r}'

    # 3. Localizar renpy.exe
    renpy_exe = find_renpy_exe(sdk_path)
    if not renpy_exe:
        return False, (
            f'No se encontró renpy.exe en {sdk_path!r}.\n'
            f'Verifica que el SDK esté instalado y la ruta sea correcta.\n'
            f'Descarga: https://www.renpy.org/latest.html'
        )

    _log(f'SDK: {renpy_exe}')
    _log(f'Proyecto: {project_dir}')
    _log(f'Idioma: {lang}')
    _log(f'Ejecutando: translate → esto puede tardar 1-2 min...')

    # 4. Ejecutar el SDK
    cmd = [renpy_exe, project_dir, 'translate', lang]
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding='utf-8',
            errors='replace',
        )

        # Stream output en tiempo real
        output_lines: List[str] = []
        while True:
            line = proc.stdout.readline()
            if not line and proc.poll() is not None:
                break
            if line.strip():
                output_lines.append(line.rstrip())
                _log(line.rstrip())

        proc.wait(timeout=timeout)
        returncode = proc.returncode

    except subprocess.TimeoutExpired:
        proc.kill()
        return False, f'El SDK tardó más de {timeout}s y fue terminado.'
    except FileNotFoundError:
        return False, f'No se pudo ejecutar {renpy_exe!r}. Verifica permisos.'
    except Exception as e:
        return False, f'Error al lanzar SDK: {e}'

    # 5. Verificar resultado
    tl_dir = os.path.join(game_dir, 'tl', lang)
    if returncode != 0:
        _log(f'SDK terminó con código {returncode}')
        # A veces el SDK sale con código != 0 pero SÍ generó los archivos
        if os.path.isdir(tl_dir) and any(f.endswith('.rpy') for f in os.listdir(tl_dir)):
            _log('(pero los archivos sí se generaron, continuando)')
            return True, ''
        return False, (
            f'SDK terminó con error (código {returncode}).\n'
            f'Últimas líneas:\n' + '\n'.join(output_lines[-5:])
        )

    if not os.path.isdir(tl_dir):
        return False, (
            f'El SDK terminó OK pero no creó {tl_dir!r}.\n'
            f'Verifica que el juego sea un proyecto Ren\'Py válido.'
        )

    n_rpy = sum(1 for f in os.listdir(tl_dir) if f.endswith('.rpy'))
    _log(f'✅ tl/{lang}/ generado con {n_rpy} archivos .rpy')
    return True, ''


def ensure_tl_ready(
    game_dir: str,
    lang: str,
    sdk_path: str = r'C:\renpy-8.5.2-sdk',
    progress_cb: Optional[Callable] = None,
) -> Tuple[bool, str]:
    """
    Verifica que game/tl/<lang>/ exista con archivos .rpy.
    Si no existe, invoca run_sdk_generate_tl() automáticamente.

    Devuelve:
        (True, 'existing')  si ya existía
        (True, 'generated') si se generó correctamente
        (False, 'error')    si falló
    """
    tl_dir = os.path.join(game_dir, 'tl', lang)

    # Comprobar si ya existe con contenido
    if os.path.isdir(tl_dir):
        rpy_files = [f for f in os.listdir(tl_dir) if f.endswith('.rpy')]
        if rpy_files:
            return True, 'existing'

    # No existe → generar con SDK
    ok, err = run_sdk_generate_tl(
        game_dir=game_dir,
        lang=lang,
        sdk_path=sdk_path,
        progress_cb=progress_cb,
    )
    if ok:
        return True, 'generated'
    return False, err


def get_tl_stats(game_dir: str, lang: str) -> Dict[str, int]:
    """
    Devuelve estadísticas de la carpeta tl/<lang>/:
        total      : entradas encontradas (vacías + traducidas)
        translated : entradas ya traducidas (con texto)
        files      : número de archivos .rpy
    """
    tl_dir = os.path.join(game_dir, 'tl', lang)
    if not os.path.isdir(tl_dir):
        return {'total': 0, 'translated': 0, 'files': 0}

    total      = 0
    translated = 0
    n_files    = 0

    # Línea de diálogo vacía: mc ""  /  ""  /  mc "" with dissolve
    # Excluye old/new/label/screen (son keywords de strings/UI, no diálogos)
    _SPEAKER_SKIP = {'old', 'new', 'label', 'screen', 'image', 'show',
                     'hide', 'play', 'stop', 'call', 'jump', 'return'}
    RE_DIAL_EMPTY  = re.compile(r'^(\s*)(?:("(?:[^"\\]|\\.)*"|\w+)\s+)?""\s*(?:with\s+\S+)?\s*$')
    RE_DIAL_FILLED = re.compile(r'^(\s*)(?:("(?:[^"\\]|\\.)*"|\w+)\s+)?"(?:[^"\\]|\\.)+"\s*(?:with\s+\S+)?\s*$')
    RE_NEW_EMPTY   = re.compile(r'^\s+new\s+""\s*$')
    RE_NEW_FILLED  = re.compile(r'^\s+new\s+"(?:[^"\\]|\\.)+"\s*$')

    for fn in os.listdir(tl_dir):
        if not (fn.endswith('.rpy') or fn.endswith('.rpym')) or fn.endswith('.rpyc'):
            continue
        n_files += 1
        fp = os.path.join(tl_dir, fn)
        try:
            with open(fp, 'r', encoding='utf-8', errors='replace') as f:
                in_translate = False
                in_strings   = False
                for line in f:
                    stripped = line.strip()

                    # IMPORTANTE: chequear strings: ANTES que el bloque normal,
                    # porque "translate X strings:" también matchea RE_TRANSLATE_BLOCK
                    m_str = RE_TRANSLATE_STRINGS.match(line)
                    if m_str and (not lang or m_str.group(2).lower() == lang.lower()):
                        in_strings = True; in_translate = False; continue

                    m_blk = RE_TRANSLATE_BLOCK.match(line)
                    if m_blk and (not lang or m_blk.group(2).lower() == lang.lower()):
                        in_translate = True; in_strings = False; continue

                    # Salir de bloque si encontramos una directiva de nivel 0 (sin indent)
                    if stripped and not line[0].isspace():
                        if not stripped.startswith('#'):
                            in_translate = in_strings = False

                    if in_translate:
                        # Ignorar comentarios
                        if stripped.startswith('#'):
                            continue
                        me = RE_DIAL_EMPTY.match(line)
                        if me:
                            speaker = (me.group(2) or '').lower()
                            if speaker not in _SPEAKER_SKIP:
                                total += 1
                            continue
                        mf = RE_DIAL_FILLED.match(line)
                        if mf:
                            speaker = (mf.group(2) or '').lower()
                            if speaker not in _SPEAKER_SKIP:
                                translated += 1
                                total += 1

                    elif in_strings:
                        if RE_NEW_EMPTY.match(line):
                            total += 1
                        elif RE_NEW_FILLED.match(line):
                            translated += 1
                            total += 1
        except Exception:
            pass

    return {'total': total, 'translated': translated, 'files': n_files}


if __name__ == '__main__':
    import sys
    if len(sys.argv) >= 2:
        target = sys.argv[1]
        gd = locate_game_dir(target)
        print(f'game dir: {gd}')
        if gd:
            es = extract_source_directory(gd)
            print(f'{len(es)} entradas extraídas (source mode)')
            for e in es[:10]:
                print(f' {e.kind:20} | {e.category:8} | {e.speaker[:15]:15} | {e.source[:60]}')