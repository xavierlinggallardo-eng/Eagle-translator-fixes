"""
Ren'Py .rpy parser, extractor and writer for AVN translation pipelines.

Version 3.1  ─  parallel fill-SDK, translation memory, glossary, analyzer.

This module is the workhorse of the Eagle / Zenpy-style Ren'Py translator.
It is split into clearly delimited sections so each capability can be reasoned
about (and patched) in isolation:

    1. Constants & compiled regex     — single source of truth, frozen at load.
    2. Heuristics                      — phone / menu / dialogue classification.
    3. Data model                      — the `Entry` dataclass.
    4. Low-level helpers               — escaping, ID generation, IO.
    5. Mode A  ─ parse existing TL     — `parse_file`, `parse_directory`.
    6. Mode B  ─ extract source code   — `extract_source_directory`.
    7. Mode B' ─ raw safety net        — `extract_raw_strings_directory`.
    8. Game locator                    — `locate_game_dir`.
    9. Mode C  ─ writer (tl/ files)    — `write_tl_files`.
   10. Zenpy compat                    — `strings.json` + `replaceText.rpy`.
   11. Generators                      — language selector, screens.rpy.
   12. Fill / In-place writers         — SDK fill mode v2 (parallel, atomic,
                                          progress callback, dry-run, strict
                                          placeholder validation).
   13. SDK helpers                     — `run_sdk_generate_tl`, stats.
   14. Scan SDK tl dir (parallel)      — `scan_sdk_tl_directory`.
   15. (legacy)
   16. (legacy)
   17. TranslationMemory               — JSON-backed cross-session cache.
   18. Glossary                        — protect proper nouns / forced terms.
   19. analyze_entries                 — dashboard-friendly snapshot.
   20. CLI                             — `python -m renpy_parser <game>`.

Public API (consumed by `main.py`) is kept stable.  Internals can change.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field
from functools import lru_cache
from typing import (
    Callable, Dict, Iterable, Iterator, List, Optional, Sequence,
    Set, Tuple, Union,
)

__all__ = [
    # data model
    'Entry',
    # classification
    'classify',
    # mode A
    'parse_file', 'parse_directory',
    # mode B
    'extract_source_file', 'extract_source_directory',
    # mode B' (raw)
    'extract_raw_strings_directory',
    # locator
    'locate_game_dir',
    # mode C / writers
    'write_translations', 'write_tl_files', 'write_inplace_tl',
    # zenpy
    'write_zenpy_files', 'generate_replace_text',
    # generators
    'generate_language_selector', 'generate_screens_rpy',
    # fill / scan
    'parse_and_fill_file', 'scan_sdk_tl_directory', 'fill_sdk_tl_directory',
    'scan_inplace_directory',
    # JSON helpers
    'entries_to_json', 'entries_from_json',
    'apply_strings_map', 'load_strings_json',
    # SDK
    'find_renpy_exe', 'run_sdk_generate_tl', 'ensure_tl_ready', 'get_tl_stats',
    # quality / validators
    'validate_placeholders', 'mismatched_placeholders',
    # advanced helpers (v3.1)
    'TranslationMemory', 'Glossary', 'analyze_entries',
    'fill_sdk_tl_directory_v2',
]

log = logging.getLogger('renpy_parser')
if not log.handlers:
    # The host app (main.py) installs its own handler; we just provide a
    # silent default so the library never blows up if imported standalone.
    log.addHandler(logging.NullHandler())


# =============================================================
#  1. Constants & compiled regex
# =============================================================

# --- Phone / messaging heuristics -----------------------------------------
_PHONE_PATTERNS: Tuple[str, ...] = (
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
    r'\bpush_notification', r'\bmsg_notification', r'\bsend_self_message',
    r'\bset_choices', r'\btelegram', r'\bdiscord', r'\btwitter',
    r'\bfacebook', r'\btinder', r'\bvk[_\.]', r'\bpost_message',
)

_MENU_PATTERNS: Tuple[str, ...] = (
    r'\bmenu[_\.]', r'\bbutton[_\.]', r'\blabel[_\.]', r'\bui[_\.]',
    r'\bquest[_\.]', r'\bhint[_\.]', r'\btooltip', r'\bgui[_\.]',
    r'\bscreen[_\.\s]', r'\boption', r'\bchoice', r'\bdialog\b',
)

PHONE_RE = re.compile('|'.join(_PHONE_PATTERNS), re.IGNORECASE)
MENU_RE = re.compile('|'.join(_MENU_PATTERNS), re.IGNORECASE)

# Backwards-compat aliases (legacy code paths used to reference these directly).
PHONE_PATTERNS = list(_PHONE_PATTERNS)
MENU_PATTERNS = list(_MENU_PATTERNS)

PHONE_WORDS: frozenset = frozenset({
    'messages', 'contacts', 'inbox', 'call', 'sms', 'chat', 'notification',
    'notifications', 'dial', 'whatsapp', 'instagram', 'phone', 'mensajes',
    'contactos', 'llamada', 'notificación', 'mobile', 'conversation',
    'dm', 'texting', 'messenger', 'text_message', 'message_log',
    'sms_log', 'chat_log', 'phone_log', 'telegram', 'discord', 'twitter',
    'facebook', 'tinder', 'send', 'reply',
})

MENU_WORDS: frozenset = frozenset({
    'new game', 'load game', 'save', 'load', 'options', 'preferences',
    'quit', 'main menu', 'continue', 'settings', 'about', 'help',
    'nuevo juego', 'cargar', 'guardar', 'opciones', 'salir', 'start',
    'skip', 'auto', 'history', 'gallery', 'music', 'extras', 'credits',
})


# --- Translate block / dialogue regex -------------------------------------
RE_TRANSLATE_BLOCK   = re.compile(r'^(\s*)translate\s+(\S+)\s+(\S+):\s*$')
RE_TRANSLATE_STRINGS = re.compile(r'^(\s*)translate\s+(\S+)\s+strings:\s*$')
RE_OLD               = re.compile(r'^(\s*)old\s+"((?:[^"\\]|\\.)*)"\s*$')
RE_NEW               = re.compile(r'^(\s*)new\s+"((?:[^"\\]|\\.)*)"\s*$')
RE_DIALOGUE          = re.compile(
    r'^(\s*)(?:(\w+|"(?:[^"\\]|\\.)*")\s+)?"((?:[^"\\]|\\.)*)"(.*)$'
)
RE_COMMENT_DIALOGUE  = re.compile(r'^\s*#\s*(.*)$')

# Generic any-quoted-string extractor (legacy alias).
RE_ANY_STRING = re.compile(r'"((?:[^"\\]|\\.)*)"')

# Stronger quoted-literal extractor that understands single quotes,
# r/u/f/b string prefixes and avoids byte-literals.
RE_QUOTED_STRING = re.compile(
    r'''(?ix)
    (?P<prefix>\b(?:r|u|ur|ru|f|fr|rf|b|br|rb)?\b)?
    (?P<quote>["'])
    (?P<body>(?:\\.|(?! (?P=quote) ).)*?)
    (?P=quote)
    '''
)

# Source-mode helpers
RE_LABEL     = re.compile(r'^(\s*)label\s+([A-Za-z_]\w*)\s*(?:\(.*\))?\s*:')
RE_SCREEN    = re.compile(r'^(\s*)screen\s+([A-Za-z_]\w*)\s*(?:\(.*\))?\s*:')
RE_MENU      = re.compile(r'^(\s*)menu\s*(\w*)\s*:')
RE_CHARACTER = re.compile(r'^(\s*)define\s+(\w+)\s*=\s*Character\s*\(\s*(.+)\)\s*$')
RE_GUI_DEF   = re.compile(
    r'^(\s*)(?:default\s+|define\s+)?(gui\.[A-Za-z_][\w\.]*)\s*=\s*"((?:[^"\\]|\\.)*)"\s*$'
)
RE_DEFINE_STR = re.compile(
    r'^(\s*)(?:default\s+|define\s+)([A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*)\s*=\s*"((?:[^"\\]|\\.)*)"\s*$'
)

UI_TEXT_KEYWORDS: Tuple[str, ...] = (
    'text', 'textbutton', 'label', 'tooltip', 'caption', 'hint',
    'titlebutton', 'imagebutton',
)
RE_UI_TEXT = re.compile(
    r'^(\s*)(' + '|'.join(UI_TEXT_KEYWORDS) + r')\s+_?\(?\s*"((?:[^"\\]|\\.)*)"'
)

SKIP_KEYWORDS: frozenset = frozenset({
    'image', 'scene', 'show', 'hide', 'play', 'stop', 'queue',
    'window', 'transform', 'init', 'python', 'init python',
    'voice', 'sound', 'music', '$',
})

# Phone-function detector.  Comprehensive list of AVN/Ren'Py messaging APIs.
RE_PHONE_FUNC = re.compile(
    r'(?:send_message|send_self_message|set_choices|add_sms|add_message|'
    r'phone_message|send_sms|phone\.send|message\.send|chat\.send|sms\.send|'
    r'notification\.show|notify_message|add_chat|add_text|send_text|'
    r'phone_text|message_log|sms_log|chat_log|phone\.add_message|'
    r'phone\.send_message|phone\.add_text|messenger\.send|dm\.send|'
    r'text\.send|inbox\.add|add_phone_message|add_phone_text|show_message|'
    r'show_sms|new_message|new_sms|new_chat|receive_message|receive_sms|'
    r'phone_notification|push_notification|msg_notification|post_message|'
    # Ren'Py runtime hooks that surface text to the user.
    r'renpy\.notify|renpy\.call_screen|renpy\.input|renpy\.choice_screen)\s*\(',
    re.IGNORECASE,
)

# .append/.extend/.insert/.add on a phone-ish list/dict (text_message_list.append([...]) …)
RE_PHONE_LIST_MUTATION = re.compile(
    r'(?:^|[\s(,])(\w*(?:message|phone|sms|chat|text_msg|dialog|choice|option|'
    r'notify|inbox|bubble|balloon|convo|conversation)\w*)\s*\.\s*'
    r'(?:append|extend|insert|add)\s*\(',
    re.IGNORECASE,
)

# `call screen name("a", b, "c", ...)`
RE_CALL_SCREEN = re.compile(r'^\s*call\s+screen\s+(\w+)\s*\((.+)\)\s*$')

# `$ choices = [("opt", val), ...]`
RE_CHOICE_LIST_ASSIGN = re.compile(
    r'^\s*(?:\$\s*)?(\w*(?:choice|option|menu|item|pick|select|answer)\w*)\s*=\s*\[',
    re.IGNORECASE,
)

# Ren'Py i18n marker  _("...")  /  _('...')  /  N_("...")
RE_RENPY_I18N = re.compile(r'\b_\(\s*(["\'])((?:\\.|(?!\1).)*?)\1\s*\)')

# Kwarg `text=` of send_message / send_self_message
RE_SEND_MSG_TEXT = re.compile(
    r'\.(?:send_message|send_self_message)\s*\(\s*text\s*=\s*(["\'])'
    r'((?:\\.|(?!\1).)*?)\1'
)


# --- Placeholder validators (Ren'Py [var] / {tag} / Python {name}) --------
RE_RENPY_VAR_TAG = re.compile(
    r'\[[^\]\n]+\]'         # [var]
    r'|\{[^}\n]+\}'         # {tag} or {color=#xxx} … {/tag}
    r'|%(?:\([^)]+\))?[diouxXeEfFgGcrs%]'  # %(name)s / %d / %s …
)


# --- Asset / non-text filters ---------------------------------------------
_ASSET_EXTS = re.compile(
    r'^[\w\-/\\.]+\.(?:png|jpg|jpeg|webp|ogg|mp3|wav|mp4|webm|rpy|rpa|ttf|otf|'
    r'gif|bmp|svg|opus|mov|avi|mkv|rpyc|json|txt|xml|csv|yaml|yml)$',
    re.IGNORECASE,
)
_FILE_LIKE_RE = re.compile(
    r'^[\w\-/\\.]+\.(?:png|jpg|jpeg|webp|ogg|mp3|wav|mp4|webm|rpy|rpa|ttf|otf)$',
    re.IGNORECASE,
)
_NON_TEXT_RE = re.compile(r'^[\s\W_]*$')
_INTERNAL_RE = re.compile(r'^[a-z][a-z0-9]*(?:_[a-z0-9]+){1,}$')


# --- Raw-string safety-net filters ----------------------------------------
_RAW_SKIP_LINE_PREFIX: Tuple[str, ...] = (
    'image ', 'scene ', 'show ', 'hide ', 'play ', 'stop ', 'queue ',
    'voice ', 'sound ', 'music ', 'transform ', 'style ',
    'init ', 'init python', 'init -', 'init +',
    'screen ', 'label ', 'menu ', 'python:', 'python ',
    'translate ',
    'default persistent.', 'default _', 'define _',
    'pass', 'return', 'jump', 'call ',
    'with ', 'window ',
    'add ', 'frame ', 'vbox', 'hbox', 'fixed', 'side ',
    'use ',
)

_RAW_IDENT_RE = re.compile(r'^[A-Za-z_][\w\.\-/]*$')
_RAW_FILE_RE  = _ASSET_EXTS                     # reuse
_RAW_TAG_RE   = re.compile(r'^\{[^}]*\}$')      # {b} {color=...}
_RAW_VAR_RE   = re.compile(r'^\[[^\]]*\]$')     # [mc]
_RAW_NUM_RE   = re.compile(r'^[\d\.,:%\-+ ]+$') # 100% / 1.2.3

# Ren'Py / Python calls where a string argument is almost certainly NOT player text.
_RAW_CTX_IDENT_CALLS = re.compile(
    r'\b(?:renpy\.(?:show|hide|scene|play|stop|sound|music|movie|image|'
    r'has_image|has_label|jump|call|notify_sound|file|loadable|exists|'
    r'load_image|cache_pin|cache_unpin|free_memory|input)|'
    r'config\.[a-z_]+|persistent\.|store\.|im\.|Image\(|Movie\(|Sound\(|'
    r'Solid\(|Frame\(|Composite\(|Crop\(|At\(|Transform\(|ATL\()',
    re.IGNORECASE,
)


# --- Character-name protection regex --------------------------------------
_RE_CYRILLIC   = re.compile(r'[а-яА-ЯёЁ]')
_RE_LATIN_ONLY = re.compile(r"^[A-Za-zÀ-ÖØ-öø-ÿ0-9 '\-\.]+$")
_RE_CJK        = re.compile(r'[぀-ヿ㐀-䶿一-鿿가-힯]')


# --- Word filters consolidated --------------------------------------------
#
# The previous version of this file shipped two enormous nested sets called
# `SHORT_DIALOGUE_WORDS` and `COMMON_WORDS` with thousands of duplicate
# entries inlined into `_is_translatable`.  They are unified here into two
# frozensets that are built ONCE at import time.

_BASE_SHORT_WORDS: Set[str] = {
    # Greetings / interjections
    'hi', 'hey', 'yo', 'ok', 'ah', 'oh', 'eh', 'mm', 'mmm', 'hm', 'hmm',
    'bye', 'yeah', 'nah', 'wow', 'ooh', 'aww', 'huh', 'what', 'why', 'who',
    'yes', 'no', 'yep', 'nope', 'shh',
    # Verbs / short commands
    'please', 'thanks', 'sorry', 'wait', 'stop', 'go', 'come', 'look', 'see',
    'listen', 'help', 'run', 'hide', 'stay', 'leave', 'follow', 'trust',
    'believe', 'remember', 'forget', 'understand',
    # Boolean / decisional
    'sure', 'right', 'wrong', 'true', 'false', 'maybe', 'perhaps',
    'definitely', 'exactly', 'seriously', 'really', 'honestly', 'literally',
    'actually', 'basically',
    # Quantifiers & misc
    'all', 'none', 'some', 'many', 'few', 'most', 'least', 'enough',
}

_COMMON_MENU_WORDS: Set[str] = {
    'yes', 'no', 'ok', 'start', 'back', 'next', 'end', 'new', 'old',
    'buy', 'use', 'get', 'run', 'go', 'stop', 'play', 'win', 'lose',
    'help', 'exit', 'load', 'save', 'quit', 'menu', 'about', 'settings',
    'continue', 'return', 'cancel', 'confirm', 'close', 'skip', 'auto',
    'history', 'gallery', 'music',
    # Social-action choices typical of AVNs
    'flirt', 'stay', 'apologise', 'apologize', 'compliment', 'kiss', 'hug',
    'touch', 'hold', 'grab', 'pull', 'push', 'smile', 'laugh', 'cry',
    'shout', 'whisper', 'yell', 'agree', 'disagree', 'accept', 'refuse',
    'deny', 'ask', 'tell', 'say', 'speak', 'talk', 'chat', 'follow', 'lead',
    'trust', 'doubt', 'believe', 'defend', 'attack', 'protect', 'hide',
    'reveal', 'leave', 'enter', 'join', 'wait', 'watch', 'look', 'listen',
    'ignore', 'remember', 'forget', 'comfort', 'tease', 'joke', 'insult',
    'praise', 'beg', 'demand', 'offer', 'invite', 'lie', 'truth', 'confess',
    'admit', 'blame', 'forgive', 'thank', 'greet', 'farewell',
    'dance', 'sing', 'sleep', 'wake', 'rest', 'eat', 'drink', 'cook',
    'clean', 'work', 'study', 'read', 'write', 'draw', 'paint', 'call',
    'text', 'message', 'email', 'visit', 'date', 'propose', 'marry',
    'divorce', 'break', 'fight', 'argue', 'seduce', 'charm', 'impress',
    'escape', 'rescue', 'kill', 'die', 'live', 'survive', 'thrive', 'grow',
    'learn', 'change', 'transform', 'evolve', 'become', 'pursue', 'chase',
    'hunt', 'search', 'find', 'fail', 'succeed', 'achieve', 'dream', 'hope',
    'wish', 'desire', 'want', 'need', 'love', 'hate', 'like', 'enjoy',
    'fear', 'worry', 'care', 'mind', 'notice', 'choose', 'pick', 'select',
    'decide', 'vote', 'investigate', 'explore', 'discover', 'solve',
    'create', 'build', 'make', 'craft', 'forge', 'destroy', 'fix', 'repair',
    'heal', 'hurt', 'harm', 'damage', 'wound', 'injure', 'cure', 'treat',
    'aid', 'assist', 'support', 'encourage', 'motivate', 'inspire',
    'please', 'satisfy',
}

# Build canonical case-insensitive lookup sets.
SHORT_DIALOGUE_WORDS: frozenset = frozenset(
    w
    for base in _BASE_SHORT_WORDS
    for variant in (base, f'{base}.', f'{base}!', f'{base}?')
    for w in (variant, variant.capitalize(), variant.upper())
)

COMMON_WORDS: frozenset = frozenset(
    w
    for base in _COMMON_MENU_WORDS
    for w in (base, base.capitalize(), base.upper())
)

# Known UI text fragments that have no letters but are still meaningful
KNOWN_UI_TEXTS: frozenset = frozenset({
    '...', '..', '.', '!', '?', '!!', '?!', '...?', '...!',
    '+1', '-1', '+', '-',
    '♥', '♡', '★', '☆', '•', '·',
    'o', 'O', '■', '□', '▶', '◀', '►', '◄', '▲', '▼',
    '→', '←', '↑', '↓', '⇒', '⇐', '⇑', '⇓',
    '✓', '✔', '✗', '✘', '✕', '✖',
    '●', '○', '◐', '◑', '◒', '◓',
    '♂', '♀', '♠', '♣', '♦',
    '☰', '☱', '☲', '☳', '☴', '☵', '☶', '☷',
    '≡', '=', '≠', '≈', '∞', '∑', '∆', '√',
    '↔', '↕',
})


# =============================================================
#  2. Heuristic classification
# =============================================================
def classify(context: str, source_text: str = '') -> str:
    """Return one of {'phone', 'menu', 'dialogue'} for a hint pair.

    `context` is the surrounding code (label/screen/file path/identifier),
    `source_text` is the actual translatable string when available.
    """
    ctx = context or ''
    if PHONE_RE.search(ctx):
        return 'phone'
    if MENU_RE.search(ctx):
        return 'menu'
    if source_text:
        low = source_text.strip().lower()
        if low in PHONE_WORDS:
            return 'phone'
        if low in MENU_WORDS:
            return 'menu'
        if len(low) <= 20:
            if any(w in low for w in PHONE_WORDS):
                return 'phone'
            if any(w in low for w in MENU_WORDS):
                return 'menu'
    return 'dialogue'


# =============================================================
#  3. Data model
# =============================================================
@dataclass
class Entry:
    """One translatable unit extracted from the game.

    Stable, JSON-serialisable.  Kept backwards-compatible with v2.x.
    """

    file: str
    kind: str  # 'dialogue' | 'string' | 'source_say' | 'source_menu'
               # | 'source_text' | 'source_define' | 'source_character'
               # | 'raw_string'
    block_id: str = ''
    speaker: str = ''
    source: str = ''
    translation: str = ''
    line_idx: int = 0
    category: str = 'dialogue'
    raw_old_line: str = ''
    indent: str = ''
    # extra context (source mode only)
    context_label: str = ''
    is_source: bool = False
    active_label: str = ''

    # Convenience helpers ---------------------------------------------------
    @property
    def is_translated(self) -> bool:
        return bool(self.translation and self.translation.strip())

    @property
    def is_dialogue_like(self) -> bool:
        return self.kind in ('dialogue', 'source_say', 'source_character')

    @property
    def is_string_like(self) -> bool:
        return self.kind in ('string', 'source_text', 'source_menu',
                             'source_define', 'raw_string')


# =============================================================
#  4. Low-level helpers
# =============================================================
_BSLASH_SENTINEL = '\x00BSLASH\x00'


def _unescape(s: str) -> str:
    """Decode Ren'Py double-quoted string escapes (\\, \\n, \\t, \\", \\')."""
    return (
        s.replace('\\\\', _BSLASH_SENTINEL)
         .replace('\\n', '\n').replace('\\t', '\t')
         .replace('\\"', '"').replace("\\'", "'")
         .replace(_BSLASH_SENTINEL, '\\')
    )


def _escape(s: str) -> str:
    """Encode a Python string so it can be safely placed inside ``"..."`` in .rpy."""
    return (
        s.replace('\\', '\\\\')
         .replace('"', '\\"')
         .replace('\n', '\\n').replace('\t', '\\t')
    )


def iter_string_literals(line: str) -> Iterator[str]:
    """Yield decoded one-line single/double-quoted literals from a code line.

    Ren'Py dialogue normally uses double quotes, but AVN phone systems often
    store conversations in Python lists/dicts with single quotes.  The naive
    regex used by older code missed those, so only a fraction of phone bubbles
    were translated.  This walker covers both.
    """
    for m in RE_QUOTED_STRING.finditer(line):
        prefix = (m.group('prefix') or '').lower()
        body = m.group('body')
        if 'b' in prefix and 'f' not in prefix:
            continue                       # bytes literal — never player text
        yield _unescape(body)


def iter_phone_message_texts(line: str) -> Iterator[Tuple[str, bool]]:
    """Yield ``(text, is_self)`` from ``.send_message(text=...)`` / ``send_self_message(text=...)``."""
    for m in RE_SEND_MSG_TEXT.finditer(line):
        body = _unescape(m.group(2))
        is_self = 'send_self_message' in m.group(0)
        yield body, is_self


def _strip_inline_comment(line: str) -> str:
    """Remove a trailing ``# ...`` comment while respecting quoted strings."""
    quote = ''
    esc = False
    for i, c in enumerate(line):
        if esc:
            esc = False
            continue
        if c == '\\':
            esc = True
            continue
        if quote:
            if c == quote:
                quote = ''
            continue
        if c in ('"', "'"):
            quote = c
            continue
        if c == '#':
            return line[:i]
    return line


@lru_cache(maxsize=8192)
def _is_translatable(s: str) -> bool:
    """True if ``s`` looks like player-facing text we should attempt to translate.

    Tuned for AVN content (short interjections like ``Hi.`` or ``Yo!`` are kept,
    but bare identifiers and asset paths are rejected).
    """
    if not s:
        return False
    s2 = s.strip()
    if not s2:
        return False
    if _NON_TEXT_RE.match(s2):
        return False
    if _FILE_LIKE_RE.match(s2):
        return False

    has_letter = any(c.isalpha() for c in s2)
    if not has_letter:
        if s2 in KNOWN_UI_TEXTS:
            return True
        # Pure-symbol short strings (≤10 chars, no digits) — keep, SDK generated them.
        if len(s2) <= 10 and not any(c.isdigit() for c in s2):
            return True
        return False

    # snake_case all-lowercase identifier
    if ' ' not in s2 and re.match(r'^[a-z_][a-z0-9_]*$', s2):
        return False

    # Short capitalised tokens (≤3 chars) — keep only if they look like dialogue.
    if ' ' not in s2 and len(s2) <= 3 and s2[0].isupper():
        base = s2.rstrip('.!?')
        if not (base in SHORT_DIALOGUE_WORDS or s2 in SHORT_DIALOGUE_WORDS):
            return False

    # PascalCase / ALLCAPS single token (≤12 chars) — keep only if it's a common menu word
    if ' ' not in s2 and re.match(r'^[A-Z][A-Za-z0-9]*$', s2) and len(s2) <= 12:
        if s2 not in COMMON_WORDS:
            return False

    return True


def _is_translatable_ui(s: str) -> bool:
    """Permissive variant for SDK-generated ``translate strings:`` blocks.

    The Ren'Py SDK has already decided this string deserves translation; we
    only veto obvious non-text (asset paths, strftime format strings,
    long internal identifiers, pure digits).  Erasing a string the SDK
    generated leaves it invisible in-game, so the bias is "keep it".
    """
    if not s:
        return False
    s2 = s.strip()
    if not s2:
        return False
    if _ASSET_EXTS.match(s2):
        return False
    if '%' in s2 and re.match(r'^[%\w:/ .,\-]+$', s2):
        return False
    if s2.isdigit():
        return False
    if len(s2) >= 9 and _INTERNAL_RE.match(s2):
        return False
    return True


def _stable_id(file_rel: str, line_idx: int, text: str, sub: int = 0) -> str:
    """SDK-style stable ID for translate blocks (16-hex chars)."""
    h = hashlib.md5(f'{file_rel}|{line_idx}|{sub}|{text}'.encode('utf-8')).hexdigest()
    return h[:16]


def _renpy_block_id(label: str, text: str, sub: int = 0) -> str:
    """Compute the real ID Ren'Py uses internally for translate blocks.

    Format: ``<label>_<hash8hex>`` where the hash is
    ``MD5( label + \\x00 + text + \\x00 + str(sub) )[:8]``.
    Matches the IDs you see in *Show Translation Info* (e.g. ``comenzar_d0e72641``).
    """
    payload = f'{label}\x00{text}\x00{sub}'.encode('utf-8')
    h = hashlib.md5(payload).hexdigest()[:8]
    if not label:
        return h
    label_clean = re.sub(r'[^A-Za-z0-9_]', '_', label)
    return f'{label_clean}_{h}'


def _read_text(path: str) -> List[str]:
    """Read a .rpy file as a list of lines, tolerating bad encoding."""
    with open(path, 'r', encoding='utf-8', errors='replace') as f:
        return f.readlines()


# =============================================================
#  5. Placeholder validators (Ren'Py [var] / {tag})
# =============================================================
def _extract_placeholders(s: str) -> List[str]:
    return RE_RENPY_VAR_TAG.findall(s or '')


def mismatched_placeholders(source: str, translation: str) -> Tuple[List[str], List[str]]:
    """Return ``(missing, added)`` placeholders comparing translation vs source.

    Useful as a post-translation QA step: a translator engine that drops a
    ``[mc]`` token will break the game silently.
    """
    src = _extract_placeholders(source)
    tgt = _extract_placeholders(translation)
    src_count: Dict[str, int] = defaultdict(int)
    tgt_count: Dict[str, int] = defaultdict(int)
    for t in src:
        src_count[t] += 1
    for t in tgt:
        tgt_count[t] += 1
    missing = [t for t, c in src_count.items() if tgt_count.get(t, 0) < c]
    added = [t for t, c in tgt_count.items() if src_count.get(t, 0) < c]
    return missing, added


def validate_placeholders(entries: Sequence[Entry]) -> List[Tuple[Entry, List[str], List[str]]]:
    """Return entries whose translation drops or invents placeholders.

    Each tuple is ``(entry, missing_tokens, added_tokens)``.
    """
    bad: List[Tuple[Entry, List[str], List[str]]] = []
    for e in entries:
        if not e.is_translated:
            continue
        miss, extra = mismatched_placeholders(e.source, e.translation)
        if miss or extra:
            bad.append((e, miss, extra))
    return bad


# =============================================================
#  6. Character-name protection
# =============================================================
def get_character_names_from_entries(entries: Sequence[Entry]) -> Dict[str, str]:
    """Collect ``{name: ''}`` from entries of kind ``source_character``."""
    names: Dict[str, str] = {}
    for e in entries:
        if e.kind == 'source_character' and e.source and e.source.strip():
            names[e.source.strip()] = ''
    return names


def _is_cyrillic_name(name: str) -> bool:
    return bool(_RE_CYRILLIC.search(name))


# Updated each time _auto_register_character_names runs.  Consumed by
# write_zenpy_files() to inject Cyrillic→Latin name maps into strings.json.
_last_cyrillic_name_map: Dict[str, str] = {}


def _auto_register_character_names(entries: Sequence[Entry],
                                   target_lang: str = 'EN') -> Dict[str, str]:
    """Detect, protect (Latin) and translate (Cyrillic) character names.

    Returns ``{cyrillic_original: translated}`` so the writer can inject it
    into ``strings.json``.  Silently no-ops if `translator_engines` is missing.
    """
    try:
        from translator_engines import register_character_names, translate_batch
    except ImportError:
        return {}

    raw_names = get_character_names_from_entries(entries)
    if not raw_names:
        return {}

    latin_names: List[str] = []
    cyrillic_names: List[str] = []
    for name in raw_names:
        (cyrillic_names if _is_cyrillic_name(name) else latin_names).append(name)

    if latin_names:
        register_character_names(latin_names)
        log.info('char_names: %d Latin names protected (%s%s)',
                 len(latin_names),
                 ', '.join(latin_names[:8]),
                 '…' if len(latin_names) > 8 else '')

    cyrillic_map: Dict[str, str] = {}
    if cyrillic_names:
        try:
            translated = translate_batch(
                cyrillic_names, source='auto', target=target_lang,
                engine='google', workers=4,
            )
            for orig, trans in zip(cyrillic_names, translated):
                t = (trans or '').strip()
                cyrillic_map[orig] = t if t and t != orig.strip() else orig
            register_character_names(list(cyrillic_map.values()))
            sample = ', '.join(f'{k}→{v}' for k, v in list(cyrillic_map.items())[:6])
            log.info('char_names: %d Cyrillic names translated (%s%s)',
                     len(cyrillic_map), sample,
                     '…' if len(cyrillic_map) > 6 else '')
        except Exception as ex:
            log.warning('char_names: error translating Cyrillic names: %s', ex)
            register_character_names(cyrillic_names)

    global _last_cyrillic_name_map
    _last_cyrillic_name_map = cyrillic_map
    return cyrillic_map


# =============================================================
#  7. Mode A — parse existing TL files (compat)
# =============================================================
def parse_file(path: str, base: str = '') -> List[Entry]:
    """Parse an existing translated .rpy file.

    Captures both `translate <lang> <id>:` blocks (dialogue) and
    `translate <lang> strings:` blocks (old/new pairs).
    """
    rel = os.path.relpath(path, base) if base else os.path.basename(path)
    lines = _read_text(path)

    entries: List[Entry] = []
    n = len(lines)
    i = 0
    while i < n:
        line = lines[i]

        ms = RE_TRANSLATE_STRINGS.match(line)
        if ms:
            indent = ms.group(1)
            i += 1
            current_old: Optional[str] = None
            current_old_idx = -1
            current_old_raw = ''
            while i < n:
                sub = lines[i]
                if sub.strip() == '' or sub.startswith(indent + ' ') or sub.startswith(indent + '\t'):
                    mo = RE_OLD.match(sub)
                    mn = RE_NEW.match(sub)
                    if mo:
                        current_old = _unescape(mo.group(2))
                        current_old_idx = i
                        current_old_raw = sub
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

        mb = RE_TRANSLATE_BLOCK.match(line)
        if mb:
            indent = mb.group(1)
            block_id = mb.group(3)
            i += 1
            original_text = ''
            speaker = ''
            while i < n:
                sub = lines[i]
                if sub.strip() == '':
                    i += 1
                    continue
                cm = RE_COMMENT_DIALOGUE.match(sub)
                if cm and not original_text:
                    inner = cm.group(1)
                    md = RE_DIALOGUE.match('    ' + inner)
                    if md:
                        speaker = (md.group(2) or '').strip().strip('"')
                        original_text = _unescape(md.group(3))
                    i += 1
                    continue
                md = RE_DIALOGUE.match(sub)
                if md:
                    new_text = _unescape(md.group(3))
                    sp = (md.group(2) or '').strip().strip('"')
                    cat = classify(
                        f'{block_id} {speaker or sp}',
                        source_text=original_text or new_text,
                    )
                    entries.append(Entry(
                        file=rel, kind='dialogue', block_id=block_id,
                        speaker=speaker or sp,
                        source=original_text or new_text,
                        translation=new_text if original_text else '',
                        line_idx=i, category=cat,
                        raw_old_line=sub, indent=indent,
                    ))
                    i += 1
                    break
                stripped = sub.lstrip()
                if stripped and not stripped.startswith('#'):
                    break
                i += 1
            continue

        i += 1
    return entries


def parse_directory(root: str) -> List[Entry]:
    """Parse every .rpy under ``root`` (Mode A — TL files)."""
    out: List[Entry] = []
    for dirpath, _, files in os.walk(root):
        for fn in files:
            if not fn.endswith('.rpy'):
                continue
            full = os.path.join(dirpath, fn)
            try:
                out.extend(parse_file(full, base=root))
            except Exception as e:
                log.error('parse error %s: %s', full, e)
    try:
        _auto_register_character_names(out, target_lang='EN')
    except Exception:
        pass
    return out


# =============================================================
#  8. Mode B — extract source .rpy (no `translate` block required)
# =============================================================
_PHONE_PATH_HINTS: Tuple[str, ...] = (
    'phone', 'sms', 'chat', 'msg', 'message', 'mobile', 'conversation',
    'dm', 'texting', 'messenger', 'notification', 'contact', 'call',
    'inbox', 'whatsapp', 'text_message', 'telegram', 'discord',
)


def _is_phone_file(rel_path: str) -> bool:
    """True if the relative path screams "phone/messaging script"."""
    low = rel_path.lower().replace('\\', '/')
    if any(h in low for h in _PHONE_PATH_HINTS):
        return True
    fn = os.path.basename(low)
    # Common AVN convention: `_fr.rpy` / freeroam/ — messages live in free-roam.
    if fn.endswith('_fr.rpy') or fn.endswith('_fr.rpym'):
        return True
    if 'freeroam' in low.split('/'):
        return True
    return False


def extract_source_file(path: str, base: str) -> List[Entry]:
    """Extract every translatable string from a single source .rpy file."""
    rel = os.path.relpath(path, base)
    is_phone_file_flag = _is_phone_file(rel)
    raw_lines = _read_text(path)

    entries: List[Entry] = []
    n = len(raw_lines)
    i = 0
    in_python = False
    python_indent = ''
    context_stack: List[Tuple[int, str, str]] = []
    active_renpy_label = ''
    seen_phone_keys: Set[Tuple[int, str]] = set()

    def cur_context_label() -> str:
        if not context_stack:
            return ''
        return ' > '.join(f'{k}:{nm}' for _, k, nm in context_stack)

    def update_context(line: str) -> None:
        nonlocal context_stack, active_renpy_label
        stripped = line.lstrip()
        indent_len = len(line) - len(stripped)
        context_stack = [c for c in context_stack if c[0] < indent_len]
        m = RE_LABEL.match(line)
        if m:
            context_stack.append((indent_len, 'label', m.group(2)))
            if indent_len == 0:
                active_renpy_label = m.group(2)
            return
        m = RE_SCREEN.match(line)
        if m:
            context_stack.append((indent_len, 'screen', m.group(2)))
            return
        m = RE_MENU.match(line)
        if m:
            context_stack.append((indent_len, 'menu', m.group(2) or '_'))
            return

    def push_phone_text(line_idx: int, txt: str, raw: str, indent: str,
                        ctx: str, speaker: str = 'phone',
                        cat: str = 'phone',
                        kind: str = 'source_text',
                        active_label: str = '') -> None:
        key = (line_idx, txt)
        if key in seen_phone_keys:
            return
        seen_phone_keys.add(key)
        entries.append(Entry(
            file=rel, kind=kind,
            speaker=speaker, source=txt,
            line_idx=line_idx, category=cat,
            raw_old_line=raw, indent=indent,
            context_label=ctx or 'phone:python',
            is_source=True, active_label=active_label,
        ))

    while i < n:
        raw = raw_lines[i]
        line = _strip_inline_comment(raw).rstrip('\n')
        stripped = line.lstrip()

        # python blocks: emit phone-context strings, otherwise skip
        if in_python:
            if stripped and not (raw.startswith(python_indent + ' ')
                                 or raw.startswith(python_indent + '\t')):
                in_python = False
            else:
                is_phone_line_py = bool(RE_PHONE_FUNC.search(line))
                is_phone_list_py = bool(RE_PHONE_LIST_MUTATION.search(line))
                if is_phone_file_flag or is_phone_line_py or is_phone_list_py:
                    for txt in iter_string_literals(line):
                        if _is_translatable(txt) or _raw_is_textlike(txt, phone_context=True):
                            push_phone_text(
                                i, txt, raw,
                                python_indent + '    ',
                                cur_context_label(),
                                active_label=active_renpy_label,
                            )
                i += 1
                continue
        if stripped.startswith('init python') or stripped == 'python:' or stripped.startswith('python '):
            in_python = True
            python_indent = raw[:len(raw) - len(stripped)]
            i += 1
            continue

        if not stripped or stripped.startswith('#'):
            i += 1
            continue

        update_context(line)
        ctx_label = cur_context_label()
        cur_active_label = active_renpy_label

        # Skip existing translate blocks (Mode A owns them).
        if RE_TRANSLATE_BLOCK.match(line) or RE_TRANSLATE_STRINGS.match(line):
            base_indent = len(line) - len(stripped)
            i += 1
            while i < n:
                s2 = raw_lines[i]
                s2_strip = s2.lstrip()
                if s2_strip and (len(s2) - len(s2_strip)) <= base_indent and not s2.strip().startswith('#'):
                    break
                i += 1
            continue

        # 1) Character("Name", ...)
        m = RE_CHARACTER.match(line)
        if m:
            args = m.group(3)
            sm = RE_ANY_STRING.search(args)
            if sm:
                txt = _unescape(sm.group(1))
                if _is_translatable(txt):
                    entries.append(Entry(
                        file=rel, kind='source_character',
                        speaker=m.group(2), source=txt,
                        line_idx=i, category='dialogue',
                        raw_old_line=raw, indent=m.group(1),
                        context_label=f'character:{m.group(2)}',
                        is_source=True, active_label=cur_active_label,
                    ))
            i += 1
            continue

        # 2) gui.* / define foo = "..."
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
                    context_label=f'define:{m.group(2)}',
                    is_source=True, active_label=cur_active_label,
                ))
            i += 1
            continue

        # 3) UI: text/textbutton/label/tooltip "..."
        m = RE_UI_TEXT.match(line)
        if m:
            kw = m.group(2)
            txt = _unescape(m.group(3))
            if _is_translatable(txt):
                cat = classify(f'{ctx_label} {kw}', source_text=txt)
                if kw == 'text' and cat == 'dialogue':
                    cat = 'menu'
                entries.append(Entry(
                    file=rel, kind='source_text',
                    speaker=kw, source=txt,
                    line_idx=i, category=cat,
                    raw_old_line=raw, indent=m.group(1),
                    context_label=ctx_label, is_source=True,
                    active_label=cur_active_label,
                ))
            i += 1
            continue

        # 4) Menu options:  "Text":
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
                i += 1
                continue

        # 4b) call screen name("arg1", "arg2", ...)
        if stripped.startswith('call screen ') or stripped.startswith('call screen\t'):
            m_cs = RE_CALL_SCREEN.match(line)
            if m_cs:
                screen_name = m_cs.group(1)
                args_str = m_cs.group(2)
                cat = classify(f'{screen_name} {ctx_label}')
                if cat == 'dialogue':
                    cat = 'menu'
                for txt in iter_string_literals(args_str):
                    if _is_translatable(txt):
                        push_phone_text(
                            i, txt, raw,
                            line[:len(line) - len(stripped)],
                            ctx_label or f'call_screen:{screen_name}',
                            speaker=f'screen:{screen_name}',
                            cat=cat, kind='source_menu',
                            active_label=cur_active_label,
                        )
            i += 1
            continue

        # 4c) $ choices = [("Text A", val), ...]
        if stripped.startswith('$'):
            inner_test = stripped[1:].strip()
            if RE_CHOICE_LIST_ASSIGN.match(inner_test) or RE_CHOICE_LIST_ASSIGN.match(stripped):
                for txt in iter_string_literals(inner_test):
                    if _is_translatable(txt):
                        push_phone_text(
                            i, txt, raw,
                            line[:len(line) - len(stripped)],
                            ctx_label or 'choice_list',
                            speaker='menu', cat='menu',
                            kind='source_menu', active_label=cur_active_label,
                        )
                i += 1
                continue

        # 5) `$ ...` python lines with messaging functions or in phone files
        if stripped.startswith('$'):
            inner = stripped[1:].strip()
            is_phone_line = bool(RE_PHONE_FUNC.search(inner))
            is_phone_list = bool(RE_PHONE_LIST_MUTATION.search(inner))
            if is_phone_line or is_phone_list or is_phone_file_flag:
                phone_msg_texts = list(iter_phone_message_texts(inner))
                if phone_msg_texts:
                    for txt, is_self in phone_msg_texts:
                        if not txt.strip():
                            continue
                        push_phone_text(
                            i, txt, raw,
                            line[:len(line) - len(stripped)],
                            ctx_label or 'phone:send_message',
                            speaker='phone_self' if is_self else 'phone',
                            cat='phone', active_label=cur_active_label,
                        )
                else:
                    cat = 'phone' if (is_phone_line or is_phone_list or is_phone_file_flag) else 'dialogue'
                    for txt in iter_string_literals(inner):
                        if _is_translatable(txt) or _raw_is_textlike(txt, phone_context=True):
                            push_phone_text(
                                i, txt, raw,
                                line[:len(line) - len(stripped)],
                                ctx_label or 'phone:python',
                                cat=cat, active_label=cur_active_label,
                            )
            i += 1
            continue

        # 6) say:  character "text"  /  "text"  /  "speaker" "text"
        first_word = stripped.split(None, 1)[0] if stripped else ''
        if first_word not in SKIP_KEYWORDS:
            md = RE_DIALOGUE.match(line)
            if md:
                speaker = (md.group(2) or '').strip()
                txt = _unescape(md.group(3))
                rest = (md.group(4) or '').strip()
                if rest.startswith('=') or rest.startswith('('):
                    i += 1
                    continue
                if _is_translatable(txt):
                    speaker_clean = speaker.strip('"')
                    cat = classify(f'{ctx_label} {speaker_clean}', source_text=txt)
                    entries.append(Entry(
                        file=rel, kind='source_say',
                        speaker=speaker_clean, source=txt,
                        line_idx=i, category=cat,
                        raw_old_line=raw, indent=line[:len(line) - len(stripped)],
                        context_label=ctx_label, is_source=True,
                        active_label=cur_active_label,
                    ))

        # 7) Phone-file safety net: capture generic strings when inside a label
        if is_phone_file_flag:
            in_label = any(kind == 'label' for _, kind, _ in context_stack)
            if RE_RENPY_I18N.search(line):
                for m_i18n in RE_RENPY_I18N.finditer(line):
                    txt = _unescape(m_i18n.group(2))
                    if txt.strip():
                        push_phone_text(
                            i, txt, raw,
                            line[:len(line) - len(stripped)],
                            ctx_label or 'phone:i18n',
                            active_label=cur_active_label,
                        )
            elif in_label:
                for txt in iter_string_literals(line):
                    if _is_translatable(txt) or _raw_is_textlike(txt, phone_context=True):
                        push_phone_text(
                            i, txt, raw,
                            line[:len(line) - len(stripped)],
                            ctx_label or 'phone:generic',
                            active_label=cur_active_label,
                        )

        i += 1
    return entries


def _assign_stable_ids(entries: Sequence[Entry]) -> None:
    """Assign block_ids consistent with Ren'Py's algorithm for dialogue-like entries."""
    line_counts: Dict[Tuple[str, int], int] = defaultdict(int)
    for e in entries:
        if e.block_id:
            continue
        key = (e.file, e.line_idx)
        sub = line_counts[key]
        if e.kind in ('source_say', 'dialogue', 'source_character'):
            e.block_id = _renpy_block_id(e.active_label, e.source, sub)
        else:
            e.block_id = _stable_id(e.file, e.line_idx, e.source, sub)
        line_counts[key] += 1


def _walk_rpy_files(root: str,
                    skipped_dirs: Iterable[str] = ('tl', 'cache')) -> Iterator[str]:
    """Yield absolute paths of every .rpy / .rpym under ``root`` (skipping tl/cache)."""
    skipped = set(skipped_dirs)
    for dirpath, dirs, files in os.walk(root):
        rel = os.path.relpath(dirpath, root)
        parts = rel.split(os.sep)
        if any(p in skipped for p in parts):
            dirs[:] = []
            continue
        for fn in files:
            if (fn.endswith('.rpy') or fn.endswith('.rpym')) and not fn.endswith('.rpyc'):
                yield os.path.join(dirpath, fn)


def extract_source_directory(root: str) -> List[Entry]:
    """Scan every source .rpy in ``root`` and extract translatable strings."""
    out: List[Entry] = []
    for full in _walk_rpy_files(root):
        try:
            out.extend(extract_source_file(full, base=root))
        except Exception as e:
            log.error('source extract error %s: %s', full, e)
    _assign_stable_ids(out)
    try:
        _auto_register_character_names(out, target_lang='EN')
    except Exception:
        pass
    return out


# =============================================================
#  9. Mode B' — raw string safety net (HZ Ille / Zenpy style)
# =============================================================
def _raw_is_textlike(s: str, phone_context: bool = False) -> bool:
    """Heuristic: accept only strings that look like human text.

    `phone_context=True` is permissive — short chat bubbles like *Fine*,
    *Sure*, *Excelente* are accepted because messaging UIs are nearly all
    one-word bubbles.
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
    if not any(c.isalpha() for c in s2):
        return False
    if ' ' in s2:
        return True
    if s2[-1] in '.!?…':
        # reject "module.method." style
        if _RAW_IDENT_RE.match(s2.rstrip('.!?…')) and '.' in s2 and s2[-1] != '.':
            return False
        if s2.count('.') >= 1 and s2[-1] not in '!?…':
            tail = s2.rstrip('.')
            if _RAW_IDENT_RE.match(tail) and tail[0].islower():
                return False
        return True
    # phone bubbles: PascalCase / first-cap single word
    if phone_context and _RAW_IDENT_RE.match(s2):
        if '_' in s2:
            return False
        if s2 == s2.lower() and len(s2) > 8:
            return False
        if s2[0].isupper() or (any(c.isupper() for c in s2) and any(c.islower() for c in s2)):
            return True
        return False
    if _RAW_IDENT_RE.match(s2):
        return False
    return False


def extract_raw_strings_directory(root: str,
                                  known_sources: Optional[Iterable[str]] = None
                                  ) -> List[Entry]:
    """Universal sweep of every quoted literal in every .rpy under ``root``.

    Catches strings the semantic extractor misses (raw notify calls, list
    appends, arbitrary custom helpers).  Output goes to ``strings.json``
    only (never `translate <id>:` blocks).
    """
    known: Set[str] = set(known_sources or ())
    seen: Set[str] = set()
    out: List[Entry] = []

    for full in _walk_rpy_files(root):
        rel = os.path.relpath(full, root).replace('\\', '/')
        try:
            lines = _read_text(full)
        except Exception as e:
            log.error('raw extract error %s: %s', full, e)
            continue

        in_translate_block = False
        translate_indent = 0
        for i, raw_line in enumerate(lines):
            line = raw_line.rstrip('\n')
            stripped = line.lstrip()
            if not stripped or stripped.startswith('#'):
                continue
            indent = len(line) - len(stripped)

            if stripped.startswith('translate '):
                in_translate_block = True
                translate_indent = indent
                continue
            if in_translate_block:
                if indent <= translate_indent and stripped:
                    in_translate_block = False
                else:
                    continue

            low = stripped.lower()
            if any(low.startswith(p) for p in _RAW_SKIP_LINE_PREFIX):
                if not (stripped.startswith('$') or stripped == '$'):
                    continue

            code_part = _strip_inline_comment(line)
            ctx_ident = bool(_RAW_CTX_IDENT_CALLS.search(code_part))
            is_phone_line = bool(RE_PHONE_FUNC.search(code_part))
            phone_ctx = _is_phone_file(rel) or is_phone_line

            for raw_s in iter_string_literals(code_part):
                if not _raw_is_textlike(raw_s, phone_context=phone_ctx):
                    continue
                if ctx_ident and ' ' not in raw_s.strip():
                    continue
                if raw_s in known or raw_s in seen:
                    continue
                seen.add(raw_s)
                out.append(Entry(
                    file=rel, kind='raw_string', speaker='',
                    source=raw_s, line_idx=i, category='raw',
                    raw_old_line=line,
                    indent=line[:len(line) - len(stripped)],
                    context_label='raw', is_source=True,
                ))
    return out


# =============================================================
# 10. Game-dir locator
# =============================================================
def locate_game_dir(path_in: str) -> Optional[str]:
    """Given a path inside (or near) a Ren'Py project, return the ``game/`` dir.

    Accepts a path to:
      • the project root (``MyGame/``)
      • the ``game/`` folder itself
      • the .exe / .sh launcher
      • any .rpy file inside the project

    Walks up to 8 parent levels.  Returns ``None`` if the project couldn't be
    identified.
    """
    if not path_in:
        return None
    p = os.path.abspath(path_in)
    d = os.path.dirname(p) if os.path.isfile(p) else p
    for _ in range(8):
        cand = os.path.join(d, 'game')
        if os.path.isdir(cand):
            try:
                contents = os.listdir(cand)
            except OSError:
                contents = []
            has_rpy = any(f.endswith('.rpy') for f in contents)
            has_rpyc = any(f.endswith('.rpyc') for f in contents)
            if has_rpy or has_rpyc:
                if not has_rpy and has_rpyc:
                    log.warning(
                        'game/ contains only compiled .rpyc files. Source-mode '
                        'extraction cannot run — original .rpy files are required.'
                    )
                return cand
        if os.path.basename(d).lower() == 'game' and os.path.isdir(d):
            return d
        parent = os.path.dirname(d)
        if parent == d:
            break
        d = parent
    if os.path.isdir(path_in) and os.path.basename(os.path.abspath(path_in)).lower() == 'game':
        return os.path.abspath(path_in)
    return None


# =============================================================
# 11. Mode C — writer that emits valid tl/<lang>/<file>.rpy
# =============================================================
def write_tl_files(game_dir: str, tl_lang: str, entries: List[Entry],
                   out_root: Optional[str] = None) -> Tuple[int, int]:
    """Emit translated .rpy files into ``out_root`` (default ``<game>/tl/<lang>/``).

    Dialogue-like entries become ``translate <lang> <id>:`` blocks.
    String-like entries (UI, menu, define, character) become entries inside
    one ``translate <lang> strings:`` block per file.

    Also emits ``strings.json`` + ``replaceText.rpy`` for Zenpy-style runtime
    string replacement (handled by :func:`write_zenpy_files`).

    Returns ``(files_written, entries_written)``.
    """
    if not entries:
        return (0, 0)

    out_root = out_root or os.path.join(game_dir, 'tl', tl_lang)
    os.makedirs(out_root, exist_ok=True)

    by_file: Dict[str, List[Entry]] = {}
    for e in entries:
        if not e.is_translated:
            continue
        by_file.setdefault(e.file, []).append(e)

    files_written = 0
    entries_written = 0

    for rel, items in by_file.items():
        items.sort(key=lambda x: x.line_idx)
        say_like = [e for e in items
                    if e.kind in ('dialogue', 'source_say', 'source_character')]
        str_like = [e for e in items
                    if e.kind in ('string', 'source_text', 'source_menu', 'source_define')]
        # raw_string never goes into .rpy; it is consumed by strings.json only.

        out_path = os.path.join(out_root, rel)
        os.makedirs(os.path.dirname(out_path), exist_ok=True)

        with open(out_path, 'w', encoding='utf-8') as f:
            f.write('# TODO: Translation updated by RenpyTranslator\n')
            f.write(f'# Source: {rel}\n\n')

            for e in say_like:
                if not e.source or not e.source.strip():
                    continue
                if e.kind == 'source_character':
                    # Render character names through the strings: block below.
                    continue
                bid = e.block_id or _stable_id(e.file, e.line_idx, e.source)
                speaker = e.speaker.strip()
                f.write(f'# {rel}:{e.line_idx + 1}\n')
                f.write(f'translate {tl_lang} {bid}:\n')
                if speaker and not speaker.startswith('"'):
                    f.write(f'    # {speaker} "{_escape(e.source)}"\n')
                    f.write(f'    {speaker} "{_escape(e.translation)}"\n\n')
                else:
                    sp_part = (speaker + ' ') if speaker else ''
                    f.write(f'    # {sp_part}"{_escape(e.source)}"\n')
                    f.write(f'    {sp_part}"{_escape(e.translation)}"\n\n')
                entries_written += 1

            char_items = [e for e in items if e.kind == 'source_character']
            all_strings = [e for e in str_like + char_items
                           if e.source and e.source.strip()]
            if all_strings:
                f.write(f'translate {tl_lang} strings:\n\n')
                seen: Dict[str, str] = {}
                for e in all_strings:
                    if e.source in seen:
                        if seen[e.source] != e.translation and e.translation:
                            log.warning(
                                'duplicate string with diverging translation in %s:%d '
                                '(kept %r, dropped %r)',
                                rel, e.line_idx + 1,
                                seen[e.source][:60], e.translation[:60],
                            )
                        continue
                    seen[e.source] = e.translation or ''
                    f.write(f'    # {rel}:{e.line_idx + 1}\n')
                    f.write(f'    old "{_escape(e.source)}"\n')
                    if e.translation and e.translation.strip():
                        tl_val: str = e.translation
                    elif e.kind in ('source_menu', 'source_text', 'string', 'source_define'):
                        tl_val = ''
                    else:
                        tl_val = e.source
                    if tl_val:
                        f.write(f'    new "{_escape(tl_val)}"\n\n')
                    else:
                        f.write('    new ""\n\n')
                    entries_written += 1

        files_written += 1

    try:
        write_zenpy_files(out_root, tl_lang, entries)
    except Exception as ex:
        log.error('zenpy generation failed: %s', ex)

    return (files_written, entries_written)


# =============================================================
# 12. Zenpy-compat:  strings.json + replaceText.rpy
# =============================================================
# Categories used by `write_zenpy_files` -----------------------------------
_UI_KINDS:   frozenset = frozenset({
    'string', 'source_text', 'source_define', 'source_character', 'raw_string',
})
_MENU_KINDS: frozenset = frozenset({'source_menu'})
_PHONE_CTX_KEYWORDS: frozenset = frozenset({
    'phone', 'sms', 'chat', 'msg', 'message', 'messages', 'inbox', 'call',
    'whatsapp', 'insta', 'social', 'dialer', 'notification', 'conversation',
    'dm', 'texting', 'messenger', 'send_message', 'add_sms', 'add_message',
    'add_chat', 'send_sms', 'receive_message',
})


def _is_phone_entry(e: Entry) -> bool:
    if getattr(e, 'category', '') == 'phone':
        return True
    ctx = (getattr(e, 'context_label', '') or '').lower()
    if any(w in ctx for w in _PHONE_CTX_KEYWORDS):
        return True
    src_low = (e.source or '').strip().lower()
    if any(w in src_low for w in _PHONE_CTX_KEYWORDS):
        return True
    return False


def write_zenpy_files(out_root: str, tl_lang: str, entries: Sequence[Entry],
                      base_strings_json: str = '') -> None:
    """Write ``strings.json`` (selective) + ``replaceText.rpy`` (runtime).

    The generated ``strings.json`` is intentionally lean so the runtime
    KeywordProcessor stays fast.  We include:
      1. All UI / GUI strings.
      2. Menu choices (always — translators may keep proper names verbatim).
      3. Phone / chat messages.
      4. Very short dialogue (≤30 chars) without their own translate block.
      5. Short bundled-base entries (≤50 chars) of generic UI words.

    We never bundle long Cyrillic-script dialogue: those already live in
    ``tl/<lang>/`` and only inflate ``strings.json``.
    """
    # ── 1. Load bundled base strings.json (optional, short entries only) ──
    base_map: Dict[str, str] = {}
    here = os.path.dirname(os.path.abspath(__file__))
    sj_candidates: List[str] = []
    if base_strings_json:
        sj_candidates.append(base_strings_json)
    sj_candidates += [
        os.path.join(here, 'strings.json'),
        os.path.join(os.getcwd(), 'strings.json'),
    ]
    for cand in sj_candidates:
        if os.path.isfile(cand):
            try:
                with open(cand, 'r', encoding='utf-8') as f:
                    raw_base = {str(k): str(v) for k, v in json.load(f).items() if v}
                base_map = {
                    k: v for k, v in raw_base.items()
                    if len(k) <= 50 and v.strip() and v.strip() != k.strip()
                }
                break
            except Exception as ex:
                log.warning('failed to load base strings.json %s: %s', cand, ex)

    # ── 2. Bucket the session entries ─────────────────────────────────────
    ui_map: Dict[str, str] = {}
    menu_map: Dict[str, str] = {}
    phone_map: Dict[str, str] = {}
    short_dialogue: Dict[str, str] = {}
    raw_string_keys: Set[str] = set()

    for e in entries:
        if not e.is_translated or not e.source:
            continue
        kind = e.kind
        src = e.source
        is_menu = kind in _MENU_KINDS
        if not is_menu and e.translation.strip() == src.strip():
            continue

        if kind in _UI_KINDS:
            ui_map.setdefault(src, e.translation)
            if kind == 'raw_string':
                raw_string_keys.add(src)
        elif is_menu:
            menu_map.setdefault(src, e.translation)
        elif _is_phone_entry(e):
            phone_map.setdefault(src, e.translation)
        elif kind in ('dialogue', 'source_say') and len(src) <= 30:
            short_dialogue.setdefault(src, e.translation)

    # ── 3. Merge  ─────────────────────────────────────────────────────────
    final_map: Dict[str, str] = {}
    final_map.update(base_map)
    final_map.update(short_dialogue)
    final_map.update(menu_map)
    final_map.update(phone_map)
    final_map.update(ui_map)  # UI has highest priority

    # ── 4. Cleanup  ───────────────────────────────────────────────────────
    _has_renpy_var = re.compile(r'\[[^\]]+\]|\{[^}]+\}')
    _has_cyrillic = re.compile(r'[а-яА-ЯёЁ]')
    _ends_sentence = re.compile(r'[.!?…]$')

    def _should_keep(k: str) -> bool:
        k = k.strip()
        if k in raw_string_keys:
            return True
        if len(k) <= 20:
            return True
        if _has_renpy_var.search(k):
            return True
        if not _has_cyrillic.search(k):  # pure Latin/English → UI
            return True
        if not _ends_sentence.search(k):  # Cyrillic without punctuation → menu/label
            return True
        return False                      # Cyrillic + punctuation → dialogue → drop

    before = len(final_map)
    final_map = {
        k: v for k, v in final_map.items()
        if v and v.strip() and v.strip() != k.strip() and _should_keep(k)
    }
    removed = before - len(final_map)

    # ── 5a. Inject Cyrillic → Latin name map ──────────────────────────────
    try:
        for cyrillic_orig, translated_name in _last_cyrillic_name_map.items():
            if cyrillic_orig and translated_name and cyrillic_orig != translated_name:
                final_map[cyrillic_orig] = translated_name
    except Exception:
        pass

    # ── 5b. Write strings.json ────────────────────────────────────────────
    os.makedirs(out_root, exist_ok=True)
    json_path = os.path.join(out_root, 'strings.json')
    with open(json_path, 'w', encoding='utf-8') as f:
        json.dump(final_map, f, ensure_ascii=False, indent=2)
    log.info(
        'zenpy strings.json → %s  (%d UI + %d menus + %d chats + %d short + %d base '
        '= %d total, %d dialogues dropped)',
        json_path, len(ui_map), len(menu_map), len(phone_map),
        len(short_dialogue), len(base_map), len(final_map), removed,
    )

    # ── 6. Write replaceText.rpy ──────────────────────────────────────────
    rpy_path = os.path.join(out_root, 'replaceText.rpy')
    with open(rpy_path, 'w', encoding='utf-8') as f:
        f.write(_build_replace_text_rpy(tl_lang))
    log.info('zenpy replaceText.rpy → %s', rpy_path)


# --- replaceText.rpy template ----------------------------------------------
#
# The runtime is a Ren'Py init-python script that:
#   • loads strings.json next to it,
#   • builds a KeywordProcessor (flashtext-style trie) for fast substring
#     replacement, plus an `exact` dict for whole-string hits,
#   • compiles placeholder rules for ``[var]`` / ``{tag}`` templates,
#   • hooks `config.replace_text` so every UI string is rewritten on the fly.
#
# We assemble it as plain text and substitute the language only — no fancy
# templating to avoid backslash-escape surprises (the old code had three
# different attempts at this).

_REPLACE_TEXT_RPY = r'''init python:
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
                self._white_space_chars = renpy.session["zenpy_set"](['.', '\t', '\n', '\a', ' ', ','])
                try:
                    self.non_word_boundaries = renpy.session["zenpy_set"](zenpy_string.digits + zenpy_string.letters + '_')
                except AttributeError:
                    self.non_word_boundaries = renpy.session["zenpy_set"](zenpy_string.digits + zenpy_string.ascii_letters + '_')
                self.keyword_trie_dict = __builtins__["dict"]()
                self.case_sensitive = case_sensitive
                self._terms_in_trie = 0

            def __len__(self):
                return self._terms_in_trie

            def __contains__(self, word):
                if not self.case_sensitive:
                    word = word.lower()
                current_dict = self.keyword_trie_dict
                len_covered = 0
                for char in word:
                    if char in current_dict:
                        current_dict = current_dict[char]
                        len_covered += 1
                    else:
                        break
                return self._keyword in current_dict and len_covered == renpy.session["zenpy_len"](word)

            def __setitem__(self, keyword, clean_name=None):
                status = False
                if not clean_name and keyword:
                    clean_name = keyword
                if keyword and clean_name:
                    if not self.case_sensitive:
                        keyword = keyword.lower()
                    current_dict = self.keyword_trie_dict
                    for letter in keyword:
                        current_dict = current_dict.setdefault(letter, {})
                    if self._keyword not in current_dict:
                        status = True
                        self._terms_in_trie += 1
                    current_dict[self._keyword] = clean_name
                return status

            def set_non_word_boundaries(self, non_word_boundaries):
                self.non_word_boundaries = non_word_boundaries

            def add_keyword(self, keyword, clean_name=None):
                return self.__setitem__(keyword, clean_name)

            def try_replace(self, sentence):
                try:
                    return self.replace_keywords(sentence)
                except:
                    return sentence

            def replace_keywords(self, sentence):
                if not sentence:
                    return sentence
                new_sentence = []
                orig_sentence = sentence
                if not self.case_sensitive:
                    sentence = sentence.lower()
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
                                    else:
                                        break
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
                                if current_word.islower():
                                    longest_sequence_found = longest_sequence_found.lower()
                                elif current_word.isupper():
                                    longest_sequence_found = longest_sequence_found.upper()
                                elif renpy.session["zenpy_len"](current_word) > 1 and renpy.session["zenpy_len"](longest_sequence_found) > 1:
                                    if current_word[0].islower() and not longest_sequence_found[0].islower():
                                        lst = [c for c in longest_sequence_found]
                                        lst[0] = lst[0].lower()
                                        longest_sequence_found = "".join(lst)
                                    elif current_word[0].isupper() and not longest_sequence_found[0].isupper():
                                        lst = [c for c in longest_sequence_found]
                                        lst[0] = lst[0].upper()
                                        longest_sequence_found = "".join(lst)
                                new_sentence.append(longest_sequence_found + current_white_space)
                                current_word = ''
                                current_white_space = ''
                            else:
                                new_sentence.append(current_word)
                                current_word = ''
                                current_white_space = ''
                        else:
                            current_dict = self.keyword_trie_dict
                            new_sentence.append(current_word)
                            current_word = ''
                            current_white_space = ''
                    elif char in current_dict:
                        current_word += orig_sentence[idx]
                        current_dict = current_dict[char]
                    else:
                        current_word += orig_sentence[idx]
                        current_dict = self.keyword_trie_dict
                        idy = idx + 1
                        while idy < sentence_len:
                            char = sentence[idy]
                            current_word += orig_sentence[idy]
                            if char not in self.non_word_boundaries:
                                break
                            idy += 1
                        idx = idy
                        new_sentence.append(current_word)
                        current_word = ''
                        current_white_space = ''
                    if idx + 1 >= sentence_len:
                        if self._keyword in current_dict:
                            new_sentence.append(current_dict[self._keyword])
                        else:
                            new_sentence.append(current_word)
                    idx += 1
                return "".join(new_sentence)

    def zenpy_get_strings_path():
        zenpy_file = "/tl/__LANG__/strings.json"
        if zenpy_os.path.isfile(zenpy_file):
            return zenpy_file
        zenpy_file = (renpy.config.gamedir + "/tl/__LANG__/strings.json").replace("\\", "/")
        if zenpy_os.path.isfile(zenpy_file):
            return zenpy_file
        zenpy_file = zenpy_file.replace("/", "\\")
        if zenpy_os.path.isfile(zenpy_file):
            return zenpy_file
        if hasattr(renpy.config, "searchpath") and renpy.config.searchpath != None:
            for d in renpy.config.searchpath:
                zenpy_file = (d + "/tl/__LANG__/strings.json").replace("\\", "/")
                if zenpy_os.path.isfile(zenpy_file):
                    return zenpy_file
                zenpy_file = zenpy_file.replace("/", "\\")
                if zenpy_os.path.isfile(zenpy_file):
                    return zenpy_file
        return ""

    renpy.session["zenpy_variables"]["__LANG__"] = {"next_replace": None, "keyword_processor": KeywordProcessor(case_sensitive=False)}
    renpy.session["zenpy_variables"]["__LANG__"]["keyword_processor"].set_non_word_boundaries(
        renpy.session["zenpy_set"]("ñÑáéíóúÉÓÚàâäãèêëïîìôöòõùûüÿçÂÀÃÈÊËÎÌÔÛÙÜŸÇÒÕ"))

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
        pattern = "^"
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
            renpy.session["zenpy_variables"]["__LANG__"]["keyword_processor"].add_keyword(original, replace)
            if ("{" in original and "}" in original) or ("[" in original and "]" in original):
                rule = zenpy__compile_placeholder_rule(original, replace)
                if rule:
                    zenpy_placeholder_rules.append(rule)

    renpy.session["zenpy_variables"]["__LANG__"]["exact"] = zenpy_exact
    renpy.session["zenpy_variables"]["__LANG__"]["placeholder_rules"] = zenpy_placeholder_rules

    zenpy_aliases = renpy.session["zenpy_set"](["__LANG__", "Spanish", "spanish", "spanish_latino", "es", "Español", "Espanol"])
    for zenpy_alias in zenpy_aliases:
        if zenpy_alias not in renpy.session["zenpy_variables"]:
            renpy.session["zenpy_variables"][zenpy_alias] = renpy.session["zenpy_variables"]["__LANG__"]

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
'''


def _build_replace_text_rpy(lang: str) -> str:
    """Build the runtime ``replaceText.rpy`` for a specific language.

    The template is a raw Python string with ``__LANG__`` placeholders so we
    don't have to dance around backslashes (a real source of bugs in the
    previous handcrafted generator).
    """
    safe_lang = re.sub(r'[^A-Za-z0-9_\-]', '_', lang) or 'spanish_latino'
    rpy = _REPLACE_TEXT_RPY.replace('__LANG__', safe_lang)
    # Sanity check the escape_re function survived intact: the rendered file
    # must contain `out += "\\" + ch` (two literal backslashes between quotes
    # — Ren'Py reads `"\\"` as a single-backslash string).
    assert r'out += "\\" + ch' in rpy, 'replaceText template corrupted'
    return rpy


# Backwards-compat constant — some downstream tools dereference it directly.
REPLACE_TEXT_TEMPLATE = _REPLACE_TEXT_RPY.replace('__LANG__', '{lang}')


def generate_replace_text(game_dir: str, tl_lang: str,
                          entries: Optional[List[Entry]] = None) -> str:
    """Emit ``strings.json`` + ``replaceText.rpy`` under ``game/tl/<lang>/``.

    Returns the absolute path of the generated ``replaceText.rpy``.
    """
    out_dir = os.path.join(game_dir, 'tl', tl_lang)
    os.makedirs(out_dir, exist_ok=True)
    write_zenpy_files(out_dir, tl_lang, entries or [])
    return os.path.join(out_dir, 'replaceText.rpy')


# =============================================================
# 13. Generators: language selector and screens.rpy
# =============================================================
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
    'zh-cn':          '简体中文',
    'zh-tw':          '繁體中文',
    'korean':         '한국어',
    'ko':             '한국어',
    'russian':        'Русский',
    'ru':             'Русский',
    'arabic':         'العربية',
    'ar':             'العربية',
    'turkish':        'Türkçe',
    'tr':             'Türkçe',
    'polish':         'Polski',
    'pl':             'Polski',
}


def _lang_display_name(folder_name: str) -> str:
    return _LANG_DISPLAY.get(folder_name.lower(), folder_name.replace('_', ' ').title())


_BUTTON_ANCHORS: Dict[str, Tuple[str, str, str, str]] = {
    'bottom_right': ('xalign 1.0', 'yalign 1.0', 'xoffset -10', 'yoffset -10'),
    'bottom_left':  ('xalign 0.0', 'yalign 1.0', 'xoffset 10',  'yoffset -10'),
    'top_right':    ('xalign 1.0', 'yalign 0.0', 'xoffset -10', 'yoffset 10'),
    'top_left':     ('xalign 0.0', 'yalign 0.0', 'xoffset 10',  'yoffset 10'),
}


def generate_language_selector(game_dir: str, tl_lang: str,
                               position: str = 'bottom_right') -> str:
    """Drop a floating language-selector overlay into ``game/``.

    Detects every folder under ``game/tl/`` and offers it as a choice.  The
    generated file (``tl_language_selector.rpy``) is self-contained and does
    not modify any existing project file — delete it to uninstall.
    """
    tl_root = os.path.join(game_dir, 'tl')
    out_path = os.path.join(game_dir, 'tl_language_selector.rpy')

    available_langs: List[Tuple[str, str]] = []
    if os.path.isdir(tl_root):
        for entry in sorted(os.listdir(tl_root)):
            full = os.path.join(tl_root, entry)
            if os.path.isdir(full) and not entry.startswith('.'):
                available_langs.append((entry, _lang_display_name(entry)))

    has_english = any(f in ('english', 'en') for f, _ in available_langs)
    if not has_english:
        available_langs.insert(0, ('None', 'English'))

    xalign, yalign, xoffset, yoffset = _BUTTON_ANCHORS.get(position,
                                                           _BUTTON_ANCHORS['bottom_right'])

    lang_items: List[str] = []
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

    rpy_content = f'''\
## ============================================================
## tl_language_selector.rpy — generated by RenpyTranslator
## Floating language selector (corner: {position.replace("_", " ")})
## Does not modify any original game file. Delete to uninstall.
## ============================================================

init -1 python:
    if not hasattr(persistent, "_language_choice"):
        persistent._language_choice = None  # None = original English


## Overlay: shown on every screen automatically
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


## Popup with the available languages
screen language_selector_popup():
    modal True
    zorder 201

    ## clicking outside closes the popup
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


## Register the overlay so Ren'Py renders it on every screen
init python:
    if hasattr(config, "overlay_screens"):
        if "tl_language_overlay" not in config.overlay_screens:
            config.overlay_screens.append("tl_language_overlay")
    else:
        config.overlay_screens = ["tl_language_overlay"]


## Minimal styles — compatible with any game
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


def generate_screens_rpy(game_dir: str, tl_lang: str) -> str:
    """Emit ``game/tl/<lang>/screens.rpy`` with translated UI strings.

    Mostly useful when the in-game UI lives in ``game/screens.rpy`` and the
    SDK didn't pick it up.  Falls back to copying the original string when
    no translation exists yet.
    """
    out_dir = os.path.join(game_dir, 'tl', tl_lang)
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, 'screens.rpy')

    strings_map: Dict[str, str] = {}
    for candidate in (
        os.path.join(os.path.dirname(__file__), 'strings.json'),
        os.path.join(game_dir, 'strings.json'),
        os.path.join(os.path.dirname(__file__), '..', 'strings.json'),
    ):
        if os.path.isfile(candidate):
            try:
                with open(candidate, 'r', encoding='utf-8') as f:
                    raw = json.load(f)
                for k, v in raw.items():
                    clean_k = re.sub(r'^NOTRADUCIR', '', k)
                    strings_map[clean_k] = v
                break
            except Exception:
                pass

    ui_strings: List[Tuple[str, str]] = []
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

    string_blocks: List[str] = []
    for origin, txt in ui_strings:
        tl = strings_map.get(txt, txt)
        string_blocks.append(
            f'    # {origin}\n'
            f'    old "{_escape(txt)}"\n'
            f'    new "{_escape(tl)}"\n'
        )

    strings_section = (
        f'translate {tl_lang} strings:\n\n' + '\n'.join(string_blocks)
        if string_blocks else ''
    )
    content = (
        '# screens.rpy — UI translation — generated by RenpyTranslator\n'
        f'# Language: {tl_lang}\n\n'
        f'{strings_section}\n'
    )
    with open(out_path, 'w', encoding='utf-8') as f:
        f.write(content)
    return out_path


# =============================================================
# 14. Fill / scan / in-place writers
# =============================================================
_RE_EMPTY_LINE   = re.compile(r'^(\s*)(?:("(?:[^"\\]|\\.)*"|\w+)\s+)?""\s*(with\s+\S+)?\s*$')
_RE_EMPTY_NARR   = re.compile(r'^(\s*)""\s*$')
_RE_EMPTY_DIAL   = re.compile(r'^(\s*)(\w+|"[^"]*")\s*""\s*$')
_RE_DIAL_FILLED  = re.compile(r'^(\s*)(?:("(?:[^"\\]|\\.)*"|\w+)\s+)?"(?:[^"\\]|\\.)+"\s*(?:with\s+\S+)?\s*$')
_RE_NEW_EMPTY    = re.compile(r'^\s+new\s+""\s*$')
_RE_NEW_FILLED   = re.compile(r'^\s+new\s+"(?:[^"\\]|\\.)+"\s*$')
_SPEAKER_SKIP    = frozenset({
    'old', 'new', 'label', 'screen', 'image', 'show', 'hide',
    'play', 'stop', 'call', 'jump', 'return',
})


def parse_and_fill_file(path: str, base: str = '', lang: str = '') -> List[Entry]:
    """Parse SDK-generated ``translate <lang> <id>:`` blocks with empty bodies.

    Each entry points at the exact line that needs to be replaced (``mc ""``)
    and preserves any trailing ``with dissolve`` suffix so the writer can
    re-emit it verbatim.
    """
    rel = os.path.relpath(path, base) if base else os.path.basename(path)
    lines = _read_text(path)
    entries: List[Entry] = []
    n = len(lines)
    i = 0
    while i < n:
        line = lines[i]
        m = RE_TRANSLATE_BLOCK.match(line)
        if m and (not lang or m.group(2).lower() == lang.lower()):
            indent = m.group(1)
            block_id = m.group(3)
            i += 1
            speaker = ''
            original_text = ''
            target_line_idx = -1
            target_line_raw = ''
            target_suffix = ''

            while i < n:
                sub = lines[i]
                sub_stripped = sub.strip()
                if sub_stripped and not sub_stripped.startswith('#'):
                    cur_ind = len(sub) - len(sub.lstrip())
                    if cur_ind <= len(indent):
                        break
                if sub_stripped.startswith('#') and not original_text:
                    cm = re.search(
                        r'#\s*(?:("(?:[^"\\]|\\.)*"|\w+)\s+)?"((?:[^"\\]|\\.)*)"', sub,
                    )
                    if cm:
                        speaker = (cm.group(1) or '').strip()
                        original_text = _unescape(cm.group(2))
                elif target_line_idx == -1:
                    me = _RE_EMPTY_LINE.match(sub)
                    if me:
                        target_line_idx = i
                        target_line_raw = sub
                        target_suffix = (' ' + me.group(3)) if me.group(3) else ''
                        if not speaker and me.group(2):
                            speaker = me.group(2)
                i += 1

            if original_text and target_line_idx != -1:
                is_tr = _is_translatable(original_text)
                cat = classify(f'{block_id} {speaker}', source_text=original_text)
                entries.append(Entry(
                    file=rel, kind='dialogue', block_id=block_id,
                    speaker=speaker, source=original_text,
                    translation='' if is_tr else original_text,
                    line_idx=target_line_idx, category=cat,
                    raw_old_line=target_line_raw,
                    indent=indent + '    ',
                    is_source=False,
                    active_label=target_suffix,
                ))
            continue
        i += 1
    return entries


def _scan_strings_block(path: str, base: str = '', lang: str = '') -> List[Entry]:
    """Find ``new ""`` entries inside ``translate <lang> strings:`` blocks."""
    rel = os.path.relpath(path, base) if base else os.path.basename(path)
    try:
        lines = _read_text(path)
    except Exception:
        return []

    entries: List[Entry] = []
    n = len(lines)
    i = 0
    in_strings_block = False

    while i < n:
        line = lines[i]
        ms = RE_TRANSLATE_STRINGS.match(line)
        if ms and (not lang or ms.group(2).lower() == lang.lower()):
            in_strings_block = True
            i += 1
            continue

        if in_strings_block and line.strip() and not line.startswith((' ', '\t')):
            if not line.startswith('#'):
                in_strings_block = False

        if in_strings_block:
            mo = re.match(r'^\s+old "((?:[^"\\]|\\.)*)"\s*$', line)
            if mo:
                old_text = _unescape(mo.group(1))
                j = i + 1
                while j < n and j < i + 4:
                    mn = re.match(r'^(\s+)new ""\s*$', lines[j])
                    if mn:
                        if old_text and old_text.strip() and _is_translatable_ui(old_text):
                            cat = classify('menu', source_text=old_text)
                            entries.append(Entry(
                                file=rel, kind='string', block_id='',
                                speaker='', source=old_text, translation='',
                                line_idx=j, category=cat,
                                raw_old_line=lines[j], indent=mn.group(1),
                                is_source=False, active_label='new',
                            ))
                        elif old_text and old_text.strip():
                            cat = classify('menu', source_text=old_text)
                            entries.append(Entry(
                                file=rel, kind='string', block_id='',
                                speaker='', source=old_text, translation=old_text,
                                line_idx=j, category=cat,
                                raw_old_line=lines[j], indent=mn.group(1),
                                is_source=False, active_label='new',
                            ))
                        break
                    if lines[j].strip() and not lines[j].strip().startswith('#'):
                        break
                    j += 1
        i += 1
    return entries


_FILLABLE_KINDS: frozenset = frozenset({
    'dialogue', 'string',
    'source_say', 'source_menu', 'source_text',
    'source_define', 'source_character', 'raw_string',
})


# Pre-compiled patterns for fill mode (used per-entry; compiled once for the
# hot inner loop instead of on every line).
_RE_FILL_STRING_EMPTY = re.compile(r'^(\s*)new ""\s*$')
_RE_FILL_DIAL_EMPTY = re.compile(
    r'^(\s*)(?:("(?:[^"\\]|\\.)*"|\w+)\s+)?""\s*(with\s+\S+)?\s*$'
)


@dataclass
class _TLFileIndex:
    """Pre-built index of an SDK `tl/<lang>/` directory.

    Built once with a single `os.walk`; resolving a file thereafter is O(1)
    for the common case and O(K) where K is the number of duplicate
    basenames (usually 1).  Drastically faster than the old approach which
    re-walked the entire tree for every missing entry.
    """

    sdk_tl_dir: str
    # rel-path → absolute path  (e.g. "common.rpy" → "/abs/tl/Spanish/common.rpy")
    rel_to_abs: Dict[str, str] = field(default_factory=dict)
    # basename → list of absolute paths (for fallback resolution)
    basename_to_abs: Dict[str, List[str]] = field(default_factory=lambda: defaultdict(list))

    @classmethod
    def build(cls, sdk_tl_dir: str) -> '_TLFileIndex':
        idx = cls(sdk_tl_dir=sdk_tl_dir)
        if not sdk_tl_dir or not os.path.isdir(sdk_tl_dir):
            return idx
        for dp, _dirs, fns in os.walk(sdk_tl_dir):
            for fn in fns:
                if fn.endswith('.rpyc'):
                    continue
                if not (fn.endswith('.rpy') or fn.endswith('.rpym')):
                    continue
                full = os.path.join(dp, fn)
                rel = os.path.relpath(full, sdk_tl_dir).replace('\\', '/')
                idx.rel_to_abs[rel] = full
                idx.basename_to_abs[fn].append(full)
        return idx

    def resolve(self, rel_path: str) -> Optional[str]:
        """O(1) resolution with basename fallback.

        Tries `rel_path` directly, then falls back to basename matches that
        look like translation files (`translate ` or `old "` near the top).
        """
        rel_norm = rel_path.replace('\\', '/')
        hit = self.rel_to_abs.get(rel_norm)
        if hit:
            return hit
        basename = os.path.basename(rel_norm)
        candidates = self.basename_to_abs.get(basename, [])
        if not candidates:
            return None
        if len(candidates) == 1:
            return candidates[0]
        # Multiple basename matches — pick the one that looks like a TL file.
        for cand in candidates:
            try:
                with open(cand, 'r', encoding='utf-8', errors='replace') as cf:
                    head = cf.read(2000)
            except Exception:
                continue
            if 'translate ' in head or 'old "' in head:
                return cand
        return candidates[0]


def _resolve_tl_file(sdk_tl_dir: str, rel_path: str) -> Optional[str]:
    """Legacy single-shot resolver.

    Prefer :class:`_TLFileIndex` for any loop that touches more than one
    file — that builds a single index instead of re-walking the tree.
    """
    direct = os.path.join(sdk_tl_dir, rel_path)
    if os.path.isfile(direct):
        return direct
    basename = os.path.basename(rel_path)
    flat = os.path.join(sdk_tl_dir, basename)
    if os.path.isfile(flat):
        return flat
    for dp, _dirs, fns in os.walk(sdk_tl_dir):
        if basename in fns:
            candidate = os.path.join(dp, basename)
            try:
                with open(candidate, 'r', encoding='utf-8', errors='replace') as cf:
                    head = cf.read(2000)
            except Exception:
                continue
            if 'translate ' in head or 'old "' in head:
                return candidate
    return None


def _atomic_write_text(path: str, lines: List[str]) -> None:
    """Write ``lines`` to ``path`` atomically (temp file in same dir + replace).

    Avoids leaving a half-written .rpy on disk if the process dies mid-write,
    which would corrupt the game for the player.
    """
    dirname = os.path.dirname(path) or '.'
    fd, tmp_path = tempfile.mkstemp(prefix='.rpy_fill_', suffix='.tmp', dir=dirname)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8', newline='') as f:
            f.writelines(lines)
        os.replace(tmp_path, path)
    except Exception:
        # cleanup on error
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _apply_entry_to_lines(e: Entry, lines: List[str]) -> Tuple[bool, Optional[str]]:
    """Mutate ``lines`` in place to apply translation for one entry.

    Returns ``(written, reason_skipped)``.  ``reason_skipped`` is set when
    we deliberately didn't write (out-of-range index, malformed line, …)
    so the caller can surface QA info.
    """
    idx = e.line_idx
    if idx < 0 or idx >= len(lines):
        return (False, 'line-out-of-range')
    original_line = lines[idx]
    escaped = _escape(e.translation)

    if e.kind == 'string' or e.active_label == 'new':
        mn = _RE_FILL_STRING_EMPTY.match(original_line)
        if not mn:
            return (False, 'string-line-already-filled-or-malformed')
        lines[idx] = f'{mn.group(1)}new "{escaped}"\n'
        return (True, None)

    me = _RE_FILL_DIAL_EMPTY.match(original_line)
    if not me:
        return (False, 'dialogue-line-already-filled-or-malformed')
    line_indent = me.group(1)
    line_speaker = me.group(2) or e.speaker
    line_suffix = (' ' + me.group(3)) if me.group(3) else (e.active_label or '')
    if line_speaker and line_speaker not in ('new', 'old'):
        lines[idx] = f'{line_indent}{line_speaker} "{escaped}"{line_suffix}\n'
    else:
        lines[idx] = f'{line_indent}"{escaped}"{line_suffix}\n'
    return (True, None)


# Result type for the advanced fill-mode writer.
@dataclass
class FillResult:
    """Detailed return value of :func:`fill_sdk_tl_directory_v2`.

    Backwards-compat: iterable, so ``files, lines = fill_sdk_tl_directory_v2(...)``
    still works for older call sites.
    """

    files_modified: int = 0
    lines_written: int = 0
    files_seen: int = 0
    entries_seen: int = 0
    entries_skipped: int = 0
    entries_unresolved: int = 0
    placeholder_warnings: int = 0
    duration_seconds: float = 0.0
    # per-file detail (path → counters), only populated when verbose=True
    per_file: Dict[str, Dict[str, int]] = field(default_factory=dict)
    # entries we deliberately skipped (rel_path → list of reasons)
    skip_reasons: Dict[str, List[str]] = field(default_factory=dict)

    def __iter__(self) -> Iterator[int]:
        yield self.files_modified
        yield self.lines_written

    def summary(self) -> str:
        return (
            f'fill_sdk: {self.files_modified}/{self.files_seen} files, '
            f'{self.lines_written}/{self.entries_seen} lines, '
            f'{self.entries_skipped} skipped, '
            f'{self.entries_unresolved} unresolved, '
            f'{self.placeholder_warnings} placeholder warnings — '
            f'{self.duration_seconds:.2f}s'
        )


def fill_sdk_tl_directory_v2(
    sdk_tl_dir: str,
    entries: List[Entry],
    lang: str = '',
    backup: bool = True,
    *,
    workers: int = 4,
    progress_cb: Optional[Callable[[int, int, str], None]] = None,
    dry_run: bool = False,
    strict_placeholders: bool = False,
    skip_unchanged: bool = True,
    verbose: bool = False,
    atomic: bool = True,
) -> FillResult:
    """Advanced, parallel, validated SDK-tl filler.

    Parameters
    ----------
    sdk_tl_dir
        ``game/tl/<lang>/`` produced by Ren'Py SDK.
    entries
        Entries already translated (``entry.translation`` populated).
    lang
        Optional language hint; only used for logging.
    backup
        Create a one-time ``.bak`` per modified file.  Skipped when
        ``dry_run`` is True.
    workers
        Number of parallel file writers (I/O bound; 4 is a good default,
        set to 1 for deterministic ordering / debugging).
    progress_cb
        ``cb(done, total, current_file)`` invoked from the main thread after
        each file completes — safe to wire to a Qt signal.
    dry_run
        Compute everything but never touch disk.
    strict_placeholders
        Skip entries whose translation drops or invents ``[var]`` / ``{tag}``
        / ``%s`` placeholders.  Recommended for unattended pipelines.
    skip_unchanged
        Skip entries where ``translation.strip() == source.strip()`` — they
        wouldn't change anything.  Saves I/O on already-translated files.
    verbose
        Populate ``per_file`` and ``skip_reasons`` in the result.
    atomic
        Use atomic writes (temp file + rename).  Strongly recommended.

    Returns
    -------
    FillResult
        Tuple-compatible with the v1 ``(files_modified, lines_written)``
        return so legacy callers keep working.
    """
    t0 = time.monotonic()
    result = FillResult()

    # ── 0. Bucket entries by file & filter early ─────────────────────────
    by_file: Dict[str, List[Entry]] = defaultdict(list)
    for e in entries:
        if e.kind not in _FILLABLE_KINDS:
            continue
        if not e.is_translated:
            continue
        result.entries_seen += 1
        if skip_unchanged and e.translation.strip() == (e.source or '').strip():
            result.entries_skipped += 1
            if verbose:
                result.skip_reasons.setdefault(e.file, []).append('unchanged')
            continue
        if strict_placeholders:
            miss, extra = mismatched_placeholders(e.source, e.translation)
            if miss or extra:
                result.placeholder_warnings += 1
                result.entries_skipped += 1
                if verbose:
                    result.skip_reasons.setdefault(e.file, []).append(
                        f'placeholder-mismatch missing={miss} extra={extra}'
                    )
                continue
        by_file[e.file].append(e)

    if not by_file:
        result.duration_seconds = time.monotonic() - t0
        return result

    # ── 1. Build the file index ONCE (was O(N²) before) ──────────────────
    index = _TLFileIndex.build(sdk_tl_dir)
    result.files_seen = len(by_file)

    # ── 2. Per-file worker (pure function, safe to parallelise) ──────────
    lock = threading.Lock()
    done_counter = [0]

    def _process_one(rel_path: str, file_entries: List[Entry]) -> Dict[str, int]:
        stats = {'lines_written': 0, 'modified': 0, 'unresolved': 0, 'skipped': 0}
        full = index.resolve(rel_path)
        if not full:
            stats['unresolved'] = 1
            log.warning('fill_sdk: file not found: %s', rel_path)
            with lock:
                done_counter[0] += 1
                if progress_cb:
                    try:
                        progress_cb(done_counter[0], result.files_seen, rel_path)
                    except Exception:
                        pass
            return stats
        try:
            lines = _read_text(full)
        except Exception as ex:
            log.error('fill_sdk read %s: %s', full, ex)
            stats['unresolved'] = 1
            return stats

        # Sort by line_idx ascending so we touch the file linearly.
        # This is a no-op for correctness but yields nicer diffs.
        file_entries_sorted = sorted(file_entries, key=lambda x: x.line_idx)

        modified = False
        local_skip_reasons: List[str] = []
        for e in file_entries_sorted:
            written, reason = _apply_entry_to_lines(e, lines)
            if written:
                stats['lines_written'] += 1
                modified = True
            else:
                stats['skipped'] += 1
                if verbose and reason:
                    local_skip_reasons.append(f'{rel_path}:{e.line_idx + 1} {reason}')

        if modified and not dry_run:
            if backup:
                bak = full + '.bak'
                if not os.path.exists(bak):
                    try:
                        shutil.copy2(full, bak)
                    except Exception as ex:
                        log.warning('fill_sdk backup %s: %s', full, ex)
            try:
                if atomic:
                    _atomic_write_text(full, lines)
                else:
                    with open(full, 'w', encoding='utf-8') as f:
                        f.writelines(lines)
                stats['modified'] = 1
            except Exception as ex:
                log.error('fill_sdk write %s: %s', full, ex)
                stats['modified'] = 0
        elif modified and dry_run:
            stats['modified'] = 1  # would have been modified

        if verbose and local_skip_reasons:
            with lock:
                result.skip_reasons.setdefault(rel_path, []).extend(local_skip_reasons)

        with lock:
            done_counter[0] += 1
            if progress_cb:
                try:
                    progress_cb(done_counter[0], result.files_seen, rel_path)
                except Exception:
                    pass
        return stats

    # ── 3. Dispatch  (parallel I/O — release the GIL during read/write) ──
    workers = max(1, int(workers))
    if workers == 1 or len(by_file) == 1:
        # Serial path — easier to debug and avoids thread setup overhead.
        for rel_path, file_entries in by_file.items():
            stats = _process_one(rel_path, file_entries)
            result.files_modified += stats['modified']
            result.lines_written += stats['lines_written']
            result.entries_skipped += stats['skipped']
            result.entries_unresolved += stats['unresolved']
            if verbose:
                result.per_file[rel_path] = stats
    else:
        with ThreadPoolExecutor(max_workers=workers,
                                thread_name_prefix='renpy-fill') as ex:
            futures = {
                ex.submit(_process_one, rel, items): rel
                for rel, items in by_file.items()
            }
            for fut in as_completed(futures):
                rel = futures[fut]
                try:
                    stats = fut.result()
                except Exception as ex2:
                    log.error('fill_sdk worker %s: %s', rel, ex2)
                    result.entries_unresolved += 1
                    continue
                result.files_modified += stats['modified']
                result.lines_written += stats['lines_written']
                result.entries_skipped += stats['skipped']
                result.entries_unresolved += stats['unresolved']
                if verbose:
                    result.per_file[rel] = stats

    result.duration_seconds = time.monotonic() - t0
    log.info(result.summary())
    return result


def fill_sdk_tl_directory(sdk_tl_dir: str, entries: List[Entry],
                          lang: str = '', backup: bool = True,
                          **kwargs) -> Tuple[int, int]:
    """Backwards-compatible wrapper around :func:`fill_sdk_tl_directory_v2`.

    Returns the old ``(files_modified, lines_written)`` tuple so existing
    UI code keeps working unchanged.  Accepts every extra keyword the v2
    function supports — e.g.

        fill_sdk_tl_directory(tl_dir, entries, workers=8,
                              progress_cb=cb, strict_placeholders=True)
    """
    # Sensible defaults for the legacy entry point.
    kwargs.setdefault('workers', 4)
    kwargs.setdefault('skip_unchanged', True)
    kwargs.setdefault('atomic', True)
    result = fill_sdk_tl_directory_v2(
        sdk_tl_dir, entries, lang=lang, backup=backup, **kwargs,
    )
    return (result.files_modified, result.lines_written)


def apply_strings_map(entries: List[Entry], strings_map: Dict[str, str]) -> int:
    """Pre-fill entries whose source already exists in ``strings_map``.

    Spares the translator engine from re-translating common UI strings.
    Lookup is exact first, then case-insensitive.
    """
    if not strings_map:
        return 0
    exact_map: Dict[str, str] = {}
    ci_map: Dict[str, str] = {}
    for k, v in strings_map.items():
        if v and v.strip():
            exact_map[k] = v
            ci_map[k.lower()] = v

    filled = 0
    for e in entries:
        if e.is_translated or not e.source:
            continue
        tl = exact_map.get(e.source) or ci_map.get(e.source.lower())
        if tl and tl.strip() and tl.strip() != e.source.strip():
            e.translation = tl
            filled += 1
    return filled


def load_strings_json(path: str) -> Dict[str, str]:
    """Load a ``strings.json`` into ``{original: translation}``.

    Looks in several common locations if ``path`` is not absolute.
    """
    candidates: List[str] = [path]
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
            except Exception as ex:
                log.warning('strings.json load %s: %s', cand, ex)
    return {}


def _detect_lang_from_dir(sdk_tl_dir: str) -> str:
    """Sniff the first `translate <lang>` directive in any .rpy under the dir."""
    if not sdk_tl_dir or not os.path.isdir(sdk_tl_dir):
        return ''
    for fn in os.listdir(sdk_tl_dir):
        if not fn.endswith('.rpy'):
            continue
        try:
            with open(os.path.join(sdk_tl_dir, fn), 'r',
                      encoding='utf-8', errors='replace') as f:
                for line in f:
                    m = RE_TRANSLATE_BLOCK.match(line)
                    if m:
                        return m.group(2)
        except Exception:
            continue
    return ''


def scan_sdk_tl_directory(sdk_tl_dir: str, lang: str = '',
                          strings_json_path: str = '',
                          *,
                          workers: int = 4,
                          progress_cb: Optional[Callable[[int, int, str], None]] = None,
                          ) -> List[Entry]:
    """Scan an SDK-generated ``tl/<lang>/`` and return entries needing translation.

    Auto-detects the language if ``lang`` is empty or doesn't match the files,
    pre-fills entries from any nearby ``strings.json``, and parses files in
    parallel (``workers``) so AVN projects with hundreds of .rpy files load
    in seconds instead of minutes.
    """
    detected_lang = lang or _detect_lang_from_dir(sdk_tl_dir)
    if detected_lang != lang:
        log.info('scan_sdk: auto-detected language %r (config had %r)',
                 detected_lang, lang)

    # ── Build the list of files upfront so workers can be scheduled ──────
    rpy_files: List[str] = []
    for dirpath, _, files in os.walk(sdk_tl_dir):
        for fn in files:
            if not (fn.endswith('.rpy') or fn.endswith('.rpym')) or fn.endswith('.rpyc'):
                continue
            rpy_files.append(os.path.join(dirpath, fn))

    total = len(rpy_files)
    entries: List[Entry] = []
    lock = threading.Lock()
    done = [0]

    def _parse_one(full: str) -> List[Entry]:
        out: List[Entry] = []
        try:
            out.extend(parse_and_fill_file(full, base=sdk_tl_dir, lang=detected_lang))
            out.extend(_scan_strings_block(full, base=sdk_tl_dir, lang=detected_lang))
        except Exception as ex:
            log.error('scan_sdk %s: %s', full, ex)
        with lock:
            done[0] += 1
            if progress_cb:
                try:
                    progress_cb(done[0], total, os.path.basename(full))
                except Exception:
                    pass
        return out

    workers = max(1, int(workers))
    if workers == 1 or total <= 1:
        for full in rpy_files:
            entries.extend(_parse_one(full))
    else:
        with ThreadPoolExecutor(max_workers=workers,
                                thread_name_prefix='renpy-scan') as ex:
            for chunk in ex.map(_parse_one, rpy_files):
                entries.extend(chunk)

    # ── Pre-fill from strings.json ───────────────────────────────────────
    here = os.path.dirname(os.path.abspath(__file__))
    sj_candidates: List[str] = []
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
                with open(cand, 'r', encoding='utf-8') as f:
                    strings_map = {str(k): str(v) for k, v in json.load(f).items() if v}
                log.info('scan_sdk: strings.json loaded %s (%d entries)',
                         cand, len(strings_map))
                break
            except Exception as ex:
                log.warning('scan_sdk strings.json %s: %s', cand, ex)

    if strings_map:
        pre = apply_strings_map(entries, strings_map)
        if pre:
            log.info('scan_sdk: %d entries pre-filled from strings.json', pre)
    return entries


def _find_all_tl_empty_lines(path: str, lang: str, base: str = '') -> List[Entry]:
    """Find every ``translate <lang> <id>:`` block with an empty dialogue line."""
    rel = os.path.relpath(path, base) if base else os.path.basename(path)
    try:
        lines = _read_text(path)
    except Exception as ex:
        log.error('inplace read %s: %s', path, ex)
        return []

    entries: List[Entry] = []
    n = len(lines)
    i = 0
    while i < n:
        line = lines[i]
        m = RE_TRANSLATE_BLOCK.match(line)
        if not m:
            i += 1
            continue
        if m.group(2).lower() != lang.lower():
            i += 1
            continue

        block_id = m.group(3)
        block_indent = len(m.group(1))
        i += 1
        original_text = ''
        speaker = ''
        empty_line_idx = -1
        empty_line_raw = ''
        empty_speaker = ''
        empty_indent = ''

        while i < n:
            sub = lines[i]
            stripped = sub.strip()
            if stripped and not stripped.startswith('#'):
                cur_indent = len(sub) - len(sub.lstrip())
                if cur_indent <= block_indent:
                    break
            if stripped.startswith('#') and not original_text:
                cm = re.search(
                    r'#\s*(?:("(?:[^"\\]|\\.)*"|\w+)\s+)?"((?:[^"\\]|\\.)*)"', sub,
                )
                if cm:
                    speaker = (cm.group(1) or '').strip()
                    original_text = _unescape(cm.group(2))
            me = _RE_EMPTY_DIAL.match(sub)
            if me and empty_line_idx == -1:
                empty_indent = me.group(1)
                empty_speaker = me.group(2)
                empty_line_idx = i
                empty_line_raw = sub
                i += 1
                continue
            mn = _RE_EMPTY_NARR.match(sub)
            if mn and empty_line_idx == -1:
                empty_indent = mn.group(1)
                empty_speaker = ''
                empty_line_idx = i
                empty_line_raw = sub
                i += 1
                continue
            i += 1

        if original_text and empty_line_idx != -1 and _is_translatable(original_text):
            cat = classify(f'{block_id} {speaker or empty_speaker}',
                           source_text=original_text)
            entries.append(Entry(
                file=rel, kind='dialogue', block_id=block_id,
                speaker=speaker or empty_speaker,
                source=original_text, translation='',
                line_idx=empty_line_idx, category=cat,
                raw_old_line=empty_line_raw, indent=empty_indent,
                is_source=False,
            ))
    return entries


def scan_inplace_directory(game_dir: str, lang: str) -> List[Entry]:
    """Scan the entire ``game/`` tree (including tl/) for empty TL lines."""
    entries: List[Entry] = []
    for dirpath, _, files in os.walk(game_dir):
        for fn in files:
            if (fn.endswith('.rpy') or fn.endswith('.rpym')) and not fn.endswith('.rpyc'):
                full = os.path.join(dirpath, fn)
                try:
                    entries.extend(_find_all_tl_empty_lines(full, lang, base=game_dir))
                except Exception as ex:
                    log.error('inplace scan %s: %s', full, ex)
    return entries


def write_inplace_tl(game_dir: str, entries: List[Entry],
                     backup: bool = True) -> Tuple[int, int]:
    """Write translations *directly* into the original .rpy files.

    Returns ``(files_modified, lines_written)``.
    """
    by_file: Dict[str, List[Entry]] = {}
    for e in entries:
        if e.is_translated:
            by_file.setdefault(e.file, []).append(e)

    files_modified = 0
    lines_written = 0

    for rel, file_entries in by_file.items():
        abs_path = os.path.join(game_dir, rel)
        if not os.path.isfile(abs_path):
            fn = os.path.basename(rel)
            found: Optional[str] = None
            for dp, _, fns in os.walk(game_dir):
                if fn in fns:
                    cand = os.path.join(dp, fn)
                    if os.path.relpath(cand, game_dir) == rel:
                        found = cand
                        break
                    if found is None:
                        found = cand
            if not found:
                log.warning('inplace: not found: %s', rel)
                continue
            abs_path = found

        try:
            lines = _read_text(abs_path)
        except Exception as ex:
            log.error('inplace read %s: %s', abs_path, ex)
            continue

        if backup:
            bak = abs_path + '.bak'
            if not os.path.exists(bak):
                try:
                    shutil.copy2(abs_path, bak)
                except Exception as ex:
                    log.warning('inplace backup %s: %s', abs_path, ex)

        modified = False
        for e in file_entries:
            idx = e.line_idx
            if idx < 0 or idx >= len(lines):
                log.warning('inplace: line %d out of range in %s', idx, rel)
                continue
            raw = lines[idx]
            escaped = _escape(e.translation)

            me = re.match(r'^(\s*)(\w+|"[^"]*")\s*""\s*$', raw)
            if me:
                lines[idx] = f'{me.group(1)}{me.group(2)} "{escaped}"\n'
                lines_written += 1
                modified = True
                continue

            mn = re.match(r'^(\s*)""\s*$', raw)
            if mn:
                lines[idx] = f'{mn.group(1)}"{escaped}"\n'
                lines_written += 1
                modified = True

        if modified:
            try:
                with open(abs_path, 'w', encoding='utf-8') as f:
                    f.writelines(lines)
                files_modified += 1
            except Exception as ex:
                log.error('inplace write %s: %s', abs_path, ex)

    return (files_modified, lines_written)


def write_translations(root_in: str, root_out: str, entries: List[Entry]) -> int:
    """Legacy entry point used by older UI buttons.

    Auto-selects between source-mode writer and legacy translate-rewrite based
    on whether any entries are flagged ``is_source``.
    """
    has_source = any(getattr(e, 'is_source', False) for e in entries)
    if has_source:
        parts = os.path.normpath(root_out).split(os.sep)
        tl_lang = parts[-1] if parts else 'spanish_latino'
        _, written = write_tl_files(root_in, tl_lang, entries, out_root=root_out)
        return written

    by_file: Dict[str, List[Entry]] = {}
    for e in entries:
        by_file.setdefault(e.file, []).append(e)

    changed = 0
    for rel, file_entries in by_file.items():
        src = os.path.join(root_in, rel)
        dst = os.path.join(root_out, rel)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        lines = _read_text(src)
        file_entries.sort(key=lambda x: x.line_idx)
        for e in file_entries:
            if not e.translation:
                continue
            idx = e.line_idx
            if idx < 0 or idx >= len(lines):
                continue
            line = lines[idx]
            if e.kind == 'string':
                for j in range(idx + 1, min(idx + 6, len(lines))):
                    mn = RE_NEW.match(lines[j])
                    if mn:
                        lines[j] = f'{mn.group(1)}new "{_escape(e.translation)}"\n'
                        changed += 1
                        break
            else:
                md = RE_DIALOGUE.match(line)
                if md:
                    indent = md.group(1)
                    speaker = md.group(2) or ''
                    rest = md.group(4) or ''
                    sp_part = (speaker + ' ') if speaker else ''
                    lines[idx] = f'{indent}{sp_part}"{_escape(e.translation)}"{rest}\n'
                    changed += 1
        with open(dst, 'w', encoding='utf-8') as f:
            f.writelines(lines)
    return changed


# =============================================================
# 15. JSON ↔ entries
# =============================================================
def entries_to_json(entries: Sequence[Entry]) -> str:
    return json.dumps([asdict(e) for e in entries], ensure_ascii=False, indent=2)


def entries_from_json(s: str) -> List[Entry]:
    return [Entry(**d) for d in json.loads(s)]


# =============================================================
# 16. SDK integration — generate tl/<lang>/ from Ren'Py SDK
# =============================================================
def find_renpy_exe(sdk_path: str) -> Optional[str]:
    """Locate the Ren'Py launcher inside ``sdk_path`` (Windows .exe / *nix .sh)."""
    sdk_path = os.path.abspath(sdk_path) if sdk_path else ''
    if not sdk_path:
        return None
    candidates = (
        os.path.join(sdk_path, 'renpy.exe'),
        os.path.join(sdk_path, 'renpy.sh'),
        os.path.join(sdk_path, 'renpy'),
        os.path.join(sdk_path, 'renpy-8.5.2.exe'),
        os.path.join(sdk_path, 'renpy-8.4.0.exe'),
        os.path.join(sdk_path, 'launcher', 'renpy.exe'),
    )
    for c in candidates:
        if os.path.isfile(c):
            return c
    if os.path.isdir(sdk_path):
        for fn in os.listdir(sdk_path):
            if fn.lower().startswith('renpy') and (fn.endswith('.exe') or fn.endswith('.sh')):
                full = os.path.join(sdk_path, fn)
                if os.path.isfile(full):
                    return full
    return None


def run_sdk_generate_tl(game_dir: str, lang: str,
                       sdk_path: str = r'C:\renpy-8.5.2-sdk',
                       timeout: int = 180,
                       progress_cb: Optional[Callable[[str], None]] = None,
                       ) -> Tuple[bool, str]:
    """Run ``renpy.exe <project> translate <lang>`` synchronously.

    Streams output via ``progress_cb`` so the UI stays responsive.  Returns
    ``(True, '')`` on success or ``(False, message)`` otherwise.
    """
    def _log(msg: str) -> None:
        log.info('sdk %s', msg)
        if progress_cb:
            progress_cb(msg)

    if not game_dir or not os.path.isdir(game_dir):
        return False, f'game_dir not found: {game_dir!r}'

    project_dir = os.path.dirname(game_dir)
    if not os.path.isdir(project_dir):
        return False, f'project_dir not found: {project_dir!r}'

    renpy_exe = find_renpy_exe(sdk_path)
    if not renpy_exe:
        return False, (
            f'renpy.exe not found in {sdk_path!r}.\n'
            f'Verify the SDK install. Download: https://www.renpy.org/latest.html'
        )

    _log(f'SDK: {renpy_exe}')
    _log(f'Project: {project_dir}')
    _log(f'Language: {lang}')
    _log('Running translate (may take 1-2 min)…')

    cmd = [renpy_exe, project_dir, 'translate', lang]
    output_lines: List[str] = []
    try:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding='utf-8', errors='replace',
        )
        while True:
            assert proc.stdout is not None
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
        return False, f'SDK timed out after {timeout}s and was killed.'
    except FileNotFoundError:
        return False, f'Could not execute {renpy_exe!r}. Check permissions.'
    except Exception as ex:
        return False, f'Error launching SDK: {ex}'

    tl_dir = os.path.join(game_dir, 'tl', lang)
    if returncode != 0:
        _log(f'SDK exited with code {returncode}')
        if os.path.isdir(tl_dir) and any(f.endswith('.rpy') for f in os.listdir(tl_dir)):
            _log('(files were generated, continuing)')
            return True, ''
        return False, (
            f'SDK exited with code {returncode}.\n'
            f'Last lines:\n' + '\n'.join(output_lines[-5:])
        )

    if not os.path.isdir(tl_dir):
        return False, (
            f'SDK exited OK but did not create {tl_dir!r}.\n'
            f'Make sure the target is a valid Ren\'Py project.'
        )

    n_rpy = sum(1 for f in os.listdir(tl_dir) if f.endswith('.rpy'))
    _log(f'OK — tl/{lang}/ generated with {n_rpy} .rpy files')
    return True, ''


def ensure_tl_ready(game_dir: str, lang: str,
                    sdk_path: str = r'C:\renpy-8.5.2-sdk',
                    progress_cb: Optional[Callable[[str], None]] = None,
                    ) -> Tuple[bool, str]:
    """If ``game/tl/<lang>/`` already has .rpy files, do nothing.  Else invoke SDK."""
    tl_dir = os.path.join(game_dir, 'tl', lang)
    if os.path.isdir(tl_dir) and any(f.endswith('.rpy') for f in os.listdir(tl_dir)):
        return True, 'existing'
    ok, err = run_sdk_generate_tl(
        game_dir=game_dir, lang=lang,
        sdk_path=sdk_path, progress_cb=progress_cb,
    )
    return (True, 'generated') if ok else (False, err)


def get_tl_stats(game_dir: str, lang: str) -> Dict[str, int]:
    """Return ``{'total', 'translated', 'files'}`` for ``game/tl/<lang>/``."""
    tl_dir = os.path.join(game_dir, 'tl', lang)
    if not os.path.isdir(tl_dir):
        return {'total': 0, 'translated': 0, 'files': 0}

    total = 0
    translated = 0
    n_files = 0
    for fn in os.listdir(tl_dir):
        if not (fn.endswith('.rpy') or fn.endswith('.rpym')) or fn.endswith('.rpyc'):
            continue
        n_files += 1
        fp = os.path.join(tl_dir, fn)
        try:
            with open(fp, 'r', encoding='utf-8', errors='replace') as f:
                in_translate = False
                in_strings = False
                for line in f:
                    stripped = line.strip()
                    m_str = RE_TRANSLATE_STRINGS.match(line)
                    if m_str and (not lang or m_str.group(2).lower() == lang.lower()):
                        in_strings = True
                        in_translate = False
                        continue
                    m_blk = RE_TRANSLATE_BLOCK.match(line)
                    if m_blk and (not lang or m_blk.group(2).lower() == lang.lower()):
                        in_translate = True
                        in_strings = False
                        continue
                    if stripped and not line[0].isspace() and not stripped.startswith('#'):
                        in_translate = in_strings = False

                    if in_translate:
                        if stripped.startswith('#'):
                            continue
                        me = _RE_EMPTY_LINE.match(line)
                        if me:
                            speaker = (me.group(2) or '').lower()
                            if speaker not in _SPEAKER_SKIP:
                                total += 1
                            continue
                        mf = _RE_DIAL_FILLED.match(line)
                        if mf:
                            speaker = (mf.group(2) or '').lower()
                            if speaker not in _SPEAKER_SKIP:
                                translated += 1
                                total += 1
                    elif in_strings:
                        if _RE_NEW_EMPTY.match(line):
                            total += 1
                        elif _RE_NEW_FILLED.match(line):
                            translated += 1
                            total += 1
        except Exception:
            pass
    return {'total': total, 'translated': translated, 'files': n_files}


# =============================================================
# 17. Translation Memory  (persistent cache across sessions)
# =============================================================
class TranslationMemory:
    """Lightweight, JSON-backed translation memory keyed by source string.

    Use it to keep previous translations around so the next run of the engine
    can re-use them for free (and to keep proper nouns / repeated UI labels
    stable across episodes of long-running AVN projects).

    Example
    -------
    >>> tm = TranslationMemory.load('~/.cache/eagle_tm.json')
    >>> hits, misses = tm.fill_entries(entries)        # pre-fill what we can
    >>> # ... run engine on the misses ...
    >>> for e in entries:
    ...     if e.is_translated:
    ...         tm.put(e.source, e.translation, context=e.category)
    >>> tm.save()

    The cache is small (string→string), threadsafe, and survives renames
    because lookups normalise whitespace.
    """

    __slots__ = ('path', '_data', '_lock', '_dirty', '_ctx')

    def __init__(self, path: Optional[str] = None) -> None:
        self.path: Optional[str] = os.path.expanduser(path) if path else None
        self._data: Dict[str, str] = {}
        # context bucket: source → category hint (best-effort; not load-bearing)
        self._ctx: Dict[str, str] = {}
        self._lock = threading.RLock()
        self._dirty = False

    # ── persistence ──────────────────────────────────────────────────────
    @classmethod
    def load(cls, path: str) -> 'TranslationMemory':
        tm = cls(path)
        if not tm.path or not os.path.isfile(tm.path):
            return tm
        try:
            with open(tm.path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            if isinstance(data, dict):
                # Two on-disk formats: flat {src:tl} or {entries:{}, ctx:{}}
                if 'entries' in data and isinstance(data['entries'], dict):
                    tm._data = {str(k): str(v) for k, v in data['entries'].items()}
                    tm._ctx = {str(k): str(v) for k, v in data.get('ctx', {}).items()}
                else:
                    tm._data = {str(k): str(v) for k, v in data.items()}
        except Exception as ex:
            log.warning('TM load %s: %s', tm.path, ex)
        return tm

    def save(self, path: Optional[str] = None) -> None:
        with self._lock:
            target = os.path.expanduser(path) if path else self.path
            if not target:
                raise ValueError('TranslationMemory.save: no path provided')
            os.makedirs(os.path.dirname(target) or '.', exist_ok=True)
            payload = {'entries': self._data, 'ctx': self._ctx,
                       'version': 1, 'saved_at': time.time()}
            tmp = target + '.tmp'
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
            os.replace(tmp, target)
            self._dirty = False
            self.path = target

    # ── lookups ──────────────────────────────────────────────────────────
    @staticmethod
    def _norm(s: str) -> str:
        # Whitespace-normalised lookup key — translations stay valid even if
        # the upstream script tweaks trailing spaces or line wrapping.
        return ' '.join((s or '').split())

    def get(self, source: str) -> Optional[str]:
        with self._lock:
            tl = self._data.get(source)
            if tl is not None:
                return tl
            return self._data.get(self._norm(source))

    def put(self, source: str, translation: str, context: str = '') -> None:
        if not source or not translation:
            return
        with self._lock:
            self._data[source] = translation
            self._data[self._norm(source)] = translation
            if context:
                self._ctx[source] = context
            self._dirty = True

    def __len__(self) -> int:
        with self._lock:
            return len(self._data)

    def __contains__(self, source: str) -> bool:
        return self.get(source) is not None

    # ── bulk helpers ─────────────────────────────────────────────────────
    def fill_entries(self, entries: Sequence[Entry]) -> Tuple[int, int]:
        """Pre-fill entries whose source already exists in the TM.

        Returns ``(hits, misses)``.
        """
        hits = 0
        misses = 0
        for e in entries:
            if e.is_translated or not e.source:
                continue
            tl = self.get(e.source)
            if tl and tl.strip():
                e.translation = tl
                hits += 1
            else:
                misses += 1
        return (hits, misses)

    def absorb_entries(self, entries: Iterable[Entry]) -> int:
        """Store every translated entry into the TM.  Returns the count added."""
        n = 0
        with self._lock:
            for e in entries:
                if e.is_translated and e.source:
                    self._data[e.source] = e.translation
                    self._data[self._norm(e.source)] = e.translation
                    if e.category:
                        self._ctx[e.source] = e.category
                    n += 1
            if n:
                self._dirty = True
        return n


# =============================================================
# 18. Glossary  (protect proper nouns / fixed terms from MT engines)
# =============================================================
class Glossary:
    """Per-game glossary that pins certain source terms to specific targets.

    Typical use is "always render 'Subject 17' as 'Sujeto 17' / never translate
    'Aki'".  The translator engine wrapper calls :meth:`pre_protect` before
    sending text to the MT and :meth:`post_restore` after — placeholders keep
    the term invisible to the engine.

    Two channels:
      • ``forced[src] = dst`` — always replace src with dst (case-insensitive).
      • ``protected`` — names/terms the MT must NOT touch (rendered identically).

    JSON format (autoloaded from ``<game_root>/glossary.json``)::

        {
          "forced": {"Sister Jen": "Hermana Jen"},
          "protected": ["MC", "Aki", "Toronjil"]
        }
    """

    _PH_TEMPLATE = '⁣TMB{0}⁣'  # invisible BOM-like sentinels

    def __init__(self,
                 forced: Optional[Dict[str, str]] = None,
                 protected: Optional[Iterable[str]] = None) -> None:
        self.forced: Dict[str, str] = dict(forced or {})
        self.protected: Set[str] = set(protected or ())

    # ── persistence ──────────────────────────────────────────────────────
    @classmethod
    def load(cls, path: str) -> 'Glossary':
        gl = cls()
        if not path or not os.path.isfile(path):
            return gl
        try:
            with open(path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            if isinstance(data, dict):
                gl.forced = {str(k): str(v) for k, v in (data.get('forced') or {}).items()}
                gl.protected = {str(x) for x in (data.get('protected') or [])}
            elif isinstance(data, list):
                gl.protected = {str(x) for x in data}
        except Exception as ex:
            log.warning('Glossary load %s: %s', path, ex)
        return gl

    def save(self, path: str) -> None:
        os.makedirs(os.path.dirname(path) or '.', exist_ok=True)
        payload = {'forced': self.forced, 'protected': sorted(self.protected)}
        tmp = path + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)

    # ── core ─────────────────────────────────────────────────────────────
    def pre_protect(self, text: str) -> Tuple[str, List[Tuple[str, str]]]:
        """Replace protected/forced terms with placeholders.

        Returns ``(masked_text, mapping)`` — feed mapping back into
        :meth:`post_restore` after MT translation.
        """
        mapping: List[Tuple[str, str]] = []
        if not text:
            return ('', mapping)
        out = text
        # Process by descending length so "Subject 17" wins over "Subject".
        terms = sorted(set(self.protected) | set(self.forced.keys()),
                       key=len, reverse=True)
        for i, term in enumerate(terms):
            if not term:
                continue
            ph = self._PH_TEMPLATE.format(i)
            pattern = re.compile(re.escape(term), re.IGNORECASE)
            if not pattern.search(out):
                continue
            target = self.forced.get(term, term)
            out = pattern.sub(ph, out)
            mapping.append((ph, target))
        return (out, mapping)

    def post_restore(self, text: str, mapping: Sequence[Tuple[str, str]]) -> str:
        out = text
        for ph, target in mapping:
            out = out.replace(ph, target)
        return out

    def apply_to_entries(self, entries: Iterable[Entry]) -> int:
        """Apply every ``forced`` mapping directly to already-translated entries.

        Useful after MT in case the engine slipped past the protect step
        (e.g. matched case-insensitively).  Returns count of changes.
        """
        if not self.forced:
            return 0
        rewrites: List[Tuple[re.Pattern, str]] = [
            (re.compile(re.escape(k), re.IGNORECASE), v)
            for k, v in self.forced.items()
        ]
        n = 0
        for e in entries:
            if not e.is_translated:
                continue
            new_tl = e.translation
            for pat, dst in rewrites:
                new_tl2 = pat.sub(dst, new_tl)
                if new_tl2 != new_tl:
                    new_tl = new_tl2
                    n += 1
            e.translation = new_tl
        return n


# =============================================================
# 19. Entry analyzer (debug / progress dashboards)
# =============================================================
def analyze_entries(entries: Sequence[Entry]) -> Dict[str, object]:
    """Return a rich snapshot of an entry list for dashboards / logging.

    Keys:
      * ``total``                    – count
      * ``translated``               – count with non-empty translation
      * ``untranslated``             – complement
      * ``by_kind``                  – Counter
      * ``by_category``              – Counter (dialogue / menu / phone / raw)
      * ``unique_sources``           – distinct source strings
      * ``avg_source_len``           – mean character length of sources
      * ``placeholder_mismatches``   – translated entries with bad placeholders
      * ``files``                    – count of distinct files
    """
    by_kind: Counter = Counter()
    by_cat: Counter = Counter()
    sources: Set[str] = set()
    files: Set[str] = set()
    src_lens: List[int] = []
    placeholder_bad = 0
    translated = 0

    for e in entries:
        by_kind[e.kind] += 1
        by_cat[e.category or 'unknown'] += 1
        if e.file:
            files.add(e.file)
        if e.source:
            sources.add(e.source)
            src_lens.append(len(e.source))
        if e.is_translated:
            translated += 1
            miss, extra = mismatched_placeholders(e.source, e.translation)
            if miss or extra:
                placeholder_bad += 1

    total = len(entries)
    return {
        'total': total,
        'translated': translated,
        'untranslated': total - translated,
        'by_kind': dict(by_kind),
        'by_category': dict(by_cat),
        'unique_sources': len(sources),
        'avg_source_len': (sum(src_lens) / len(src_lens)) if src_lens else 0.0,
        'placeholder_mismatches': placeholder_bad,
        'files': len(files),
    }


# =============================================================
# 20. CLI — quick smoke-test entry point
# =============================================================
def _cli(argv: Sequence[str]) -> int:
    if len(argv) < 2:
        print('usage: python renpy_parser.py <game_path_or_exe>')
        return 2
    logging.basicConfig(level=logging.INFO,
                        format='%(asctime)s %(levelname)s %(name)s: %(message)s')
    target = argv[1]
    gd = locate_game_dir(target)
    print(f'game dir: {gd}')
    if not gd:
        return 1
    es = extract_source_directory(gd)
    print(f'{len(es)} entries extracted (source mode)')
    for e in es[:10]:
        print(f'  {e.kind:20} | {e.category:8} | {e.speaker[:15]:15} | {e.source[:60]}')
    raw = extract_raw_strings_directory(gd, known_sources={e.source for e in es})
    print(f'{len(raw)} additional raw strings')
    return 0


if __name__ == '__main__':
    sys.exit(_cli(sys.argv))
