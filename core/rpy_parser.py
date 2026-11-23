"""
Парсер скриптов Ren'Py (.rpy / .rpym).

Модуль делает две важные вещи:

1. Извлекает весь отображаемый игроку текст: реплики, нарратив, `extend`,
   варианты меню, UI-строки экранов, `_()`-обёрнутые строки, имена персонажей.

2. Вычисляет для каждой реплики **точный идентификатор translate-блока**
   (`<label>_<md5[:8]>`), который использует сам движок Ren'Py. Это позволяет
   генерировать `game/tl/russian/*.rpy`, которые Ren'Py подхватывает штатным
   образом — без правки оригинальных скриптов.

Алгоритм идентификаторов воспроизводит `renpy/translation/__init__.py`
(класс ``Restructurer``) и ``Say.get_code`` из `renpy/ast.py`:

    digest = md5(code + "\\r\\n")[:8]
    identifier = label.replace(".", "_") + "_" + digest
    (при коллизии добавляется суффикс "_1", "_2", ...)

где ``code`` — каноническая запись инструкции say (тот же вид, что возвращает
``Say.get_code()``: ``who attributes "text" with dissolve``).
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional

from config import Settings
from core.models import (
    Edit,
    KIND_DIALOGUE,
    KIND_EXTEND,
    KIND_MENU,
    KIND_NAME,
    KIND_NARRATION,
    KIND_UI,
    ParsedFile,
    ParsedProject,
    SayMeta,
    TextItem,
)
from core.scanner import RenpyProject
from core.utils import get_logger, read_text_file, safe_relpath

logger = get_logger(__name__)


# ===========================================================================
# 1. Работа со строками Ren'Py (точная копия правил lexer.py / ast.py)
# ===========================================================================
def decode_string_body(body: str, raw: bool = False) -> str:
    """
    Декодирует внутренность строкового литерала так, как это делает Ren'Py
    (`renpy/lexer.py`, методы ``Lexer.string`` / ``triple_string``):

    * пробельные серии ``[ \\n]+`` схлопываются в один пробел;
    * ``\\{`` -> ``{{``, ``\\[`` -> ``[[``, ``\\%`` -> ``%%``;
    * ``\\n`` -> перевод строки, ``\\uXXXX`` -> символ;
    * любой другой ``\\X`` -> ``X``.

    Для raw-строк (``r"..."``) возвращается текст как есть.
    """
    if raw:
        return body

    body = re.sub(r"[ \n]+", " ", body)

    def dequote(m: "re.Match[str]") -> str:
        c = m.group(1)
        if c == "{":
            return "{{"
        if c == "[":
            return "[["
        if c == "%":
            return "%%"
        if c == "n":
            return "\n"
        if c and c[0] == "u":
            group2 = m.group(2)
            if group2:
                try:
                    return chr(int(group2, 16))
                except ValueError:
                    return c
        return c

    return re.sub(r"\\(u([0-9a-fA-F]{1,4})|.)", dequote, body, flags=re.S)


def decode_triple_body(body: str, monologue_delimiter: Optional[str] = None,
                       raw: bool = False) -> list[tuple[int, str]]:
    """
    Декодирует тройные кавычки (monologue mode) по правилам
    ``Lexer.triple_string``. Возвращает список (смещение_строки, текст).
    """
    if raw:
        return [(0, body)]

    s = re.sub(r" *\n *", "\n", body)

    if monologue_delimiter:
        parts = s.split(monologue_delimiter)
    else:
        parts = [s]

    rv: list[tuple[int, str]] = []
    pos = 0
    for part in parts:
        leading_ws = len(part) - len(part.lstrip())
        line_offset = s.count("\n", 0, pos) + part.count("\n", 0, leading_ws)
        pos += len(part) + (len(monologue_delimiter) if monologue_delimiter else 0)

        part = part.strip()
        if not part:
            continue
        if monologue_delimiter:
            part = re.sub(r"[ \n]+", " ", part)
        else:
            part = re.sub(r" +", " ", part)

        def dequote(m: "re.Match[str]") -> str:
            c = m.group(1)
            if c == "{":
                return "{{"
            if c == "[":
                return "[["
            if c == "%":
                return "%%"
            if c == "n":
                return "\n"
            if c and c[0] == "u":
                group2 = m.group(2)
                if group2:
                    try:
                        return chr(int(group2, 16))
                    except ValueError:
                        return c
            return c

        part = re.sub(r"\\(u([0-9a-fA-F]{1,4})|.)", dequote, part)
        rv.append((line_offset, part))

    return rv


def encode_say_string(s: str) -> str:
    """Копия ``renpy.translation.encode_say_string`` (используется для digest)."""
    s = s.replace("\\", "\\\\")
    s = s.replace("\n", "\\n")
    s = s.replace('"', '\\"')
    s = re.sub(r"(?<= ) ", "\\\\ ", s)
    return '"' + s + '"'


def quote_unicode(s: str) -> str:
    """Копия ``renpy.translation.quote_unicode`` (экранирование для tl-файлов)."""
    s = s.replace("\\", "\\\\")
    s = s.replace('"', '\\"')
    s = s.replace("\a", "\\a")
    s = s.replace("\b", "\\b")
    s = s.replace("\f", "\\f")
    s = s.replace("\n", "\\n")
    s = s.replace("\r", "\\r")
    s = s.replace("\t", "\\t")
    s = s.replace("\v", "\\v")
    return s


def make_literal(text: str, quote: str = '"', raw: bool = False,
                 triple: bool = False) -> str:
    """
    Собирает строковый литерал Ren'Py из «человеческого» текста.

    Для режима прямой замены: подбирает кавычку, которая не встречается
    в тексте, корректно экранирует переводы строк.
    """
    if triple:
        run = quote * 3
        body = text
        if not raw:
            body = body.replace("\\", "\\\\") if False else body  # raw-режим не трогаем
        # В тройных кавычках достаточно убрать закрывающую последовательность.
        body = body.replace(run, quote * 2)
        return run + body + run

    # Выбираем кавычку, которой нет в тексте (по умолчанию — исходная).
    for cand in (quote, '"', "'", "`"):
        if cand not in text:
            quote = cand
            break
    else:
        quote = '"'

    if raw:
        body = text
        if text.endswith("\\"):
            body = text[:-1]
    else:
        body = text.replace("\\", "\\\\")
        body = body.replace("\n", "\\n")
        body = body.replace("\r", "")
        body = body.replace(quote, "\\" + quote)
    return quote + body + quote


# ===========================================================================
# 2. Логические строки
# ===========================================================================
@dataclass
class LogicalLine:
    """Одна «логическая» строка скрипта Ren'Py."""

    indent: int
    text: str
    lineno: int
    offsets: list[int]  # абсолютное смещение каждого символа text в файле

    def abs_offset(self, index: int) -> int:
        if not self.offsets:
            return 0
        index = max(0, min(index, len(self.offsets) - 1))
        return self.offsets[index]


QUOTES = "\"'`"


def build_logical_lines(content: str) -> list[LogicalLine]:
    """
    Разбивает текст .rpy на логические строки:

    * убирает комментарии ``# ...`` (вне строковых литералов);
    * склеивает продолжения внутри скобок и после ``\\``;
    * сохраняет переводы строк внутри тройных кавычек;
    * запоминает отступ и абсолютные смещения.
    """
    # Страховка: CRLF/CR не должны попадать в текст инструкций.
    if "\r" in content:
        content = content.replace("\r\n", "\n").replace("\r", "\n")

    lines: list[LogicalLine] = []
    n = len(content)
    i = 0

    while i < n:
        # --- начало физической строки -----------------------------------
        line_start = content.rfind("\n", 0, i) + 1
        j = i
        while j < n and content[j] in " \t":
            j += 1

        if j >= n:
            break
        if content[j] == "\n":
            i = j + 1
            continue
        if content[j] == "#":
            k = content.find("\n", j)
            i = n if k < 0 else k + 1
            continue

        indent = j - line_start
        lineno = content.count("\n", 0, j) + 1

        text_chars: list[str] = []
        offsets: list[int] = []
        depth = 0
        k = j
        last_was_backslash = False

        while k < n:
            ch = content[k]

            if ch == "\n":
                if depth > 0 or last_was_backslash:
                    text_chars.append(" ")
                    offsets.append(k)
                    k += 1
                    while k < n and content[k] in " \t":
                        k += 1
                    last_was_backslash = False
                    continue
                break

            if ch == "#":
                # комментарий до конца строки
                eol = content.find("\n", k)
                k = n if eol < 0 else eol
                continue

            if ch in QUOTES:
                quote = ch
                triple = content.startswith(quote * 3, k)
                qlen = 3 if triple else 1
                text_chars.extend(content[k:k + qlen])
                offsets.extend(range(k, k + qlen))
                k += qlen
                while k < n:
                    if content[k] == "\\":
                        text_chars.append(content[k])
                        offsets.append(k)
                        if k + 1 < n:
                            text_chars.append(content[k + 1])
                            offsets.append(k + 1)
                        k += 2
                        continue
                    if triple and content.startswith(quote * 3, k):
                        text_chars.extend(content[k:k + 3])
                        offsets.extend(range(k, k + 3))
                        k += 3
                        break
                    if not triple and content[k] == quote:
                        text_chars.append(quote)
                        offsets.append(k)
                        k += 1
                        break
                    text_chars.append(content[k])
                    offsets.append(k)
                    k += 1
                last_was_backslash = False
                continue

            if ch in "([{":
                depth += 1
            elif ch in ")]}":
                depth = max(0, depth - 1)

            text_chars.append(ch)
            offsets.append(k)
            last_was_backslash = (ch == "\\")
            k += 1

        # обрезаем хвостовые пробелы
        while text_chars and text_chars[-1] in " \t":
            text_chars.pop()
            offsets.pop()

        if text_chars:
            lines.append(LogicalLine(indent, "".join(text_chars), lineno, offsets))

        # переходим к следующей физической строке
        i = content.find("\n", k)
        i = n if i < 0 else i + 1

    return lines


# ===========================================================================
# 3. Мини-лексер инструкций
# ===========================================================================
@dataclass
class Tok:
    kind: str      # 'word' | 'string' | 'group' | 'op'
    raw: str       # исходный текст
    value: str     # для string — внутренность без кавычек, иначе raw
    start: int     # смещение в логической строке
    end: int
    quote: str = ""
    triple: bool = False
    raw_prefix: bool = False


WORD_STOP = set(" \t\"'`([{:")


def _scan_string(text: str, i: int, raw_prefix: bool = False) -> tuple[int, str, str, bool, bool]:
    """Возвращает (конец, внутренность, кавычка, triple, raw)."""
    quote = text[i]
    triple = text.startswith(quote * 3, i)
    qlen = 3 if triple else 1
    j = i + qlen
    n = len(text)
    while j < n:
        if text[j] == "\\":
            j += 2
            continue
        if triple and text.startswith(quote * 3, j):
            j += 3
            break
        if not triple and text[j] == quote:
            j += 1
            break
        j += 1
    body = text[i + qlen:j - qlen] if j - i >= 2 * qlen else ""
    return j, body, quote, triple, raw_prefix


def tokenize(text: str) -> list[Tok]:
    """Токенизирует одну логическую строку Ren'Py."""
    toks: list[Tok] = []
    i = 0
    n = len(text)

    while i < n:
        ch = text[i]
        if ch in " \t":
            i += 1
            continue

        # raw-строка: r"..." / r'...' / r`...`
        if ch == "r" and i + 1 < n and text[i + 1] in QUOTES:
            j, body, quote, triple, _ = _scan_string(text, i + 1, raw_prefix=True)
            toks.append(Tok("string", text[i:j], body, i, j, quote, triple, True))
            i = j
            continue

        if ch in QUOTES:
            j, body, quote, triple, _ = _scan_string(text, i)
            toks.append(Tok("string", text[i:j], body, i, j, quote, triple, False))
            i = j
            continue

        if ch in "([{":
            close = {"(": ")", "[": "]", "{": "}"}[ch]
            j = i
            depth = 0
            while j < n:
                c = text[j]
                if c in QUOTES:
                    j, _, _, _, _ = _scan_string(text, j)
                    continue
                if c == ch:
                    depth += 1
                elif c == close:
                    depth -= 1
                    if depth == 0:
                        j += 1
                        break
                j += 1
            toks.append(Tok("group", text[i:j], text[i:j], i, j))
            i = j
            continue

        if ch in ":@":
            toks.append(Tok("op", ch, ch, i, i + 1))
            i += 1
            continue

        j = i
        while j < n and text[j] not in WORD_STOP:
            j += 1
        if j == i:  # страховка от зацикливания
            j = i + 1
        word = text[i:j]
        toks.append(Tok("word", word, word, i, j))
        i = j

    return toks


# ===========================================================================
# 4. Классификация инструкций
# ===========================================================================
#: Слова, с которых начинаются инструкции движка (не say-инструкции).
RESERVED_STATEMENTS = {
    "$", "show", "scene", "hide", "with", "play", "stop", "queue", "pause",
    "jump", "call", "return", "python", "init", "label", "menu", "define",
    "default", "image", "transform", "style", "screen", "translate", "pass",
    "if", "elif", "else", "while", "for", "include", "voice", "nvl", "window",
    "camera", "at", "animation", "onlayer", "layeredimage", "key", "input",
    "bar", "add", "grid", "vbox", "hbox", "fixed", "frame", "text", "textbutton",
    "tooltip", "imagemap", "hotspot", "imagebutton", "use", "modal", "timer",
    "drag", "draggroup", "viewport", "null", "side", "has", "contains",
    "layers", "parallax", "build", "old", "new", "repeat", "block", "parallel",
    "choice", "function", "event", "expression", "contains",
}

#: Слова-инструкции screen language, у которых первый аргумент — текст для игрока.
SCREEN_TEXT_WORDS = {"text", "textbutton", "label", "tooltip", "textentry"}

LABEL_RE = re.compile(
    r"^label\s+(?P<name>[A-Za-z_.][\w.]*)\s*(?:\([^)]*\)\s*)?(?P<hide>hide\b)?\s*:$"
)
MENU_RE = re.compile(r"^menu\b")
SCREEN_RE = re.compile(r"^screen\s+[A-Za-z_][\w.]*\s*(?:\([^)]*\))?\s*:$")
#: `python:`, `python early:`, `init python:`, `init -5 python in ns:`
PYTHON_BLOCK_RE = re.compile(
    r"^(?:init\b(?:\s+[-\d.]+)?\s+)?python(?:\s+early)?\b[^:]*:$"
)
TRANSLATE_RE = re.compile(r"^translate\s+\S+\s+\S+\s*:$")
IMAGE_BLOCK_RE = re.compile(r"^(?:layered)?image\s+[^=]+:$")
#: Блок ATL: `show blood07:` / `scene bg at x:` / `camera …:` — внутри
#: лежат свойства (`alpha 0.5`, `blend "multiply"`), а не реплики.
SHOW_ATL_RE = re.compile(r"^(?:show|scene|hide|camera|transform)\s+[^=]+:$")
ATL_BLOCK_RE = re.compile(r"^transform\s+[A-Za-z_][\w.]*\s*(?:\([^)]*\))?\s*:$")
STYLE_BLOCK_RE = re.compile(r"^style\s+\S+\s*:$")
CHARACTER_RE = re.compile(
    r"^(?:define|default)\s+(?P<var>[A-Za-z_][\w.]*)\s*=\s*"
    r"(?:Dynamic|NVL|ADV|Bubble|Side|Centered)?Character\s*\("
)
CONFIG_NAME_RE = re.compile(
    r"^(?:define|default)\s+config\.(?:name|window_title)\s*=\s*(?P<rest>.+)$"
)
UNDERSCORE_RE = re.compile(r"(?<![.\w])(?:___|__|_p|_)\s*\(")


def parse_say_statement(text: str) -> Optional[dict]:
    """
    Разбирает инструкцию say.

    Возвращает словарь с полями who/attributes/... либо None, если строка
    не является say-инструкцией.
    """
    if text.startswith("$") or text.startswith(">>"):
        return None

    toks = tokenize(text)
    if not toks:
        return None

    who: Optional[str] = None
    attributes: list[str] = []
    temporary: list[str] = []
    idx = 0

    # Отсекаем присваивания и прочий python-код: `NAME = "строка"`,
    # `obj.attr = """…"""`, `d["k"] = "…"`. Иначе строки из сторонних
    # библиотек (шейдеры, константы) попадают в файл перевода и ломают игру.
    if any(t.kind == "word" and ("=" in t.value or t.value in ("+", "-", "*", "/", "%"))
           for t in toks):
        return None

    if toks[0].kind == "string":
        # Вариант 1: нарратив — "Текст".
        # Вариант 2: who как строковое выражение — "Eileen" "Текст".
        has_string_after = any(t.kind == "string" for t in toks[1:])
        if has_string_after and toks[1].kind in ("string", "word"):
            who = toks[0].raw  # сохраняем как в исходнике, вместе с кавычками
            idx = 1
            while idx < len(toks) and toks[idx].kind == "word":
                nxt = toks[idx].value
                if nxt in ("with", "nointeract", "id"):
                    break
                attributes.append(nxt)
                idx += 1
            if idx >= len(toks) or toks[idx].kind != "string":
                return None
            what_tok = toks[idx]
            idx += 1
        else:
            what_tok = toks[0]
            idx = 1
    elif toks[0].kind == "word":
        word = toks[0].value
        if word in RESERVED_STATEMENTS:
            return None
        # «who» — простое имя персонажа/переменной
        if not re.match(r"^[A-Za-z_][\w.]*$", word):
            return None
        who = word
        idx = 1
        # атрибуты образа (e happy "..." / e -happy "...")
        while idx < len(toks) and toks[idx].kind == "word":
            nxt = toks[idx].value
            if nxt in ("with", "nointeract", "id"):
                break
            if not re.match(r"^-?[A-Za-z_][\w.]*$", nxt):
                return None
            attributes.append(nxt)
            idx += 1
        if idx < len(toks) and toks[idx].kind == "op" and toks[idx].value == "@":
            idx += 1
            while idx < len(toks) and toks[idx].kind == "word":
                nxt = toks[idx].value
                if nxt in ("with", "nointeract", "id"):
                    break
                temporary.append(nxt)
                idx += 1
        if idx >= len(toks) or toks[idx].kind != "string":
            return None
        what_tok = toks[idx]
        idx += 1
    else:
        return None

    with_: Optional[str] = None
    arguments: Optional[str] = None
    interact = True
    explicit_id: Optional[str] = None

    while idx < len(toks):
        tok = toks[idx]
        if tok.kind == "word" and tok.value == "nointeract":
            interact = False
            idx += 1
        elif tok.kind == "word" and tok.value == "with":
            if idx + 1 >= len(toks):
                return None
            with_ = toks[idx + 1].raw
            idx += 2
        elif tok.kind == "word" and tok.value == "id":
            if idx + 1 >= len(toks):
                return None
            explicit_id = toks[idx + 1].value
            idx += 2
        elif tok.kind == "group" and tok.raw.startswith("("):
            if arguments is not None:
                return None
            arguments = tok.raw
            idx += 1
        else:
            return None

    return {
        "who": who,
        "attributes": attributes,
        "temporary_attributes": temporary,
        "what_tok": what_tok,
        "with": with_,
        "arguments": arguments,
        "interact": interact,
        "explicit_id": explicit_id,
    }


def say_get_code(parsed: dict, what_text: str) -> str:
    """Каноническая запись инструкции — как ``Say.get_code()`` в Ren'Py."""
    parts: list[str] = []
    if parsed["who"]:
        parts.append(parsed["who"])
    if parsed["attributes"]:
        parts.extend(parsed["attributes"])
    if parsed["temporary_attributes"]:
        parts.append("@")
        parts.extend(parsed["temporary_attributes"])
    parts.append(encode_say_string(what_text))
    if not parsed["interact"]:
        parts.append("nointeract")
    if parsed["explicit_id"]:
        parts.extend(["id", parsed["explicit_id"]])
    if parsed["arguments"]:
        parts.append(parsed["arguments"].replace(" ,", ",").replace("( ", "(").replace(" )", ")"))
    if parsed["with"]:
        parts.extend(["with", parsed["with"]])
    return " ".join(parts)


# ===========================================================================
# 5. Реестр идентификаторов translate-блоков
# ===========================================================================
class IdRegistry:
    """
    Воспроизводит логику ``Restructurer.unique_identifier``:

    ``id_exists`` учитывает уже выданные идентификаторы и явные ``id``-клаузулы.
    """

    def __init__(self) -> None:
        self.used: set[str] = set()
        self.preexisting: set[str] = set()

    def register_preexisting(self, identifier: Optional[str]) -> None:
        if identifier:
            self.preexisting.add(identifier)

    def exists(self, identifier: str) -> bool:
        return identifier in self.used or identifier in self.preexisting

    def unique_identifier(self, label: Optional[str], digest: str) -> str:
        if not label:
            base = digest
        else:
            base = label.replace(".", "_") + "_" + digest

        i = 0
        suffix = ""
        while True:
            identifier = base + suffix
            if not self.exists(identifier):
                break
            i += 1
            suffix = f"_{i}"
        self.used.add(identifier)
        return identifier

    @staticmethod
    def digest(codes: Iterable[str]) -> str:
        md5 = hashlib.md5()
        for code in codes:
            md5.update((code + "\r\n").encode("utf-8"))
        return md5.hexdigest()[:8]


# ===========================================================================
# 6. Парсер файла
# ===========================================================================
@dataclass
class _PendingSay:
    """Отложенная реплика: id вычисляем вторым проходом (порядок важен)."""

    item: TextItem
    label: Optional[str]
    code: str
    explicit_id: Optional[str]
    alternate: Optional[str]


class FileParser:
    """Разбирает один .rpy файл."""

    def __init__(self, content: str, rel_path: str, registry: IdRegistry,
                 settings: Settings) -> None:
        self.content = content
        self.rel_path = rel_path
        self.registry = registry
        self.settings = settings

        self.items: list[TextItem] = []
        self.labels: list[str] = []
        self.warnings: list[str] = []

        self._used_spans: list[tuple[int, int]] = []
        self._stmt_texts: list[str] = []
        self._pending_says: list[_PendingSay] = []
        self._uid_counter: dict[str, int] = {}

        # мнемоника: какие состояния игры/персонажи уже известны
        self._character_vars: set[str] = set()

    # ------------------------------------------------------------------
    def parse(self) -> ParsedFile:
        lines = build_logical_lines(self.content)
        self._walk(lines)
        self._assign_block_ids()
        self._fill_context()
        return ParsedFile(
            path=self.rel_path,
            items=self.items,
            labels=self.labels,
            warnings=self.warnings,
        )

    # ------------------------------------------------------------------
    # утилиты
    # ------------------------------------------------------------------
    def _uid(self, line: int) -> str:
        base = f"{self.rel_path}:{line}"
        count = self._uid_counter.get(base, 0)
        self._uid_counter[base] = count + 1
        return base if count == 0 else f"{base}#{count + 1}"

    def _span_used(self, start: int, end: int) -> bool:
        for s, e in self._used_spans:
            if start < e and s < end:
                return True
        return False

    def _add_item(self, item: TextItem, stmt_index: int) -> None:
        self.items.append(item)
        item.extra["stmt_index"] = stmt_index

    # ------------------------------------------------------------------
    def _walk(self, lines: list[LogicalLine]) -> None:
        stack: list[tuple[int, str]] = []  # (indent, kind)
        label: Optional[str] = None
        alternate: Optional[str] = None

        # первый проход: собираем явные id (preexisting), как это делает Ren'Py
        for ll in lines:
            parsed = parse_say_statement(ll.text)
            if parsed and parsed["explicit_id"]:
                self.registry.register_preexisting(parsed["explicit_id"])

        stmt_index = -1
        for ll in lines:
            while stack and stack[-1][0] >= ll.indent:
                stack.pop()

            kinds = [k for _, k in stack]
            ctx = stack[-1][1] if stack else "top"

            stmt_index += 1
            self._stmt_texts.append(ll.text)

            if "python" in kinds or "translate" in kinds:
                if ll.text.endswith(":"):
                    stack.append((ll.indent, "block"))
                continue

            # ---------------------------------------------------------
            # инструкции, открывающие блок
            # ---------------------------------------------------------
            text = ll.text

            m = LABEL_RE.match(text)
            if m:
                name = m.group("name")
                if not m.group("hide"):
                    if name.startswith(".") and label:
                        name = label + name
                    if name.startswith("_"):
                        alternate = name
                    else:
                        label = name
                        alternate = None
                        self.labels.append(name)
                if text.endswith(":"):
                    stack.append((ll.indent, "label"))
                continue

            if MENU_RE.match(text) and text.endswith(":"):
                stack.append((ll.indent, "menu"))
                continue

            if SCREEN_RE.match(text):
                stack.append((ll.indent, "screen"))
                continue

            if PYTHON_BLOCK_RE.match(text):
                stack.append((ll.indent, "python"))
                continue

            if TRANSLATE_RE.match(text):
                stack.append((ll.indent, "translate"))
                continue

            if (IMAGE_BLOCK_RE.match(text) or ATL_BLOCK_RE.match(text)
                    or STYLE_BLOCK_RE.match(text) or SHOW_ATL_RE.match(text)):
                stack.append((ll.indent, "atl"))
                continue

            in_skippable = any(k == "atl" for k in kinds)

            # ---------------------------------------------------------
            # извлечение текста
            # ---------------------------------------------------------
            if not in_skippable:
                self._extract_underscore_strings(ll, stmt_index)

                if ctx == "menu":
                    self._handle_menu_line(ll, stmt_index, label, alternate)
                elif "screen" in kinds:
                    if self.settings.extract_ui:
                        self._handle_screen_line(ll, stmt_index, label)
                else:
                    self._handle_statement_line(ll, stmt_index, label, alternate)

            # ---------------------------------------------------------
            # открытие generic-блока
            # ---------------------------------------------------------
            if text.endswith(":"):
                stack.append((ll.indent, "block"))

            if text.startswith("label "):
                continue

    # ------------------------------------------------------------------
    def _handle_statement_line(self, ll: LogicalLine, stmt_index: int,
                               label: Optional[str], alternate: Optional[str],
                               force_nointeract: bool = False) -> None:
        text = ll.text

        # --- имена персонажей -------------------------------------------
        if self.settings.extract_names:
            m = CHARACTER_RE.match(text)
            if m:
                self._extract_character_name(ll, m.group("var"), stmt_index, label)

        # --- config.name ------------------------------------------------
        if self.settings.extract_ui:
            m = CONFIG_NAME_RE.match(text)
            if m:
                toks = tokenize(m.group("rest"))
                if toks and toks[0].kind == "string":
                    self._register_string_literal(
                        ll, toks[0], KIND_UI, stmt_index, label, character=None,
                        shift=m.start("rest"),
                    )

        # --- диалог / нарратив ------------------------------------------
        parsed = parse_say_statement(text)
        if not parsed:
            return

        if force_nointeract:
            # Ren'Py: say-менюреплика внутри menu: всегда interact=False
            parsed["interact"] = False

        tok: Tok = parsed["what_tok"]
        abs_start = ll.abs_offset(tok.start)
        abs_end = ll.abs_offset(tok.end - 1) + 1

        if tok.triple:
            paragraphs = decode_triple_body(tok.value, None, tok.raw_prefix)
        else:
            paragraphs = [(0, decode_string_body(tok.value, tok.raw_prefix))]

        paragraphs = [(off, p) for off, p in paragraphs if p.strip()]
        if not paragraphs:
            return

        who = parsed["who"]
        display_who = who
        if who and who[:1] in QUOTES:
            # who задан строковым литералом — для экспорта берём само имя
            display_who = decode_string_body(who[1:-1])
        if not who:
            kind = KIND_NARRATION
        elif who == "extend":
            kind = KIND_EXTEND
        else:
            kind = KIND_DIALOGUE

        group_id = self._uid(ll.lineno) if len(paragraphs) > 1 else None
        if group_id:
            self._uid_counter[f"{self.rel_path}:{ll.lineno}"] -= 1  # не считаем группу дважды

        for p_index, (line_offset, para_text) in enumerate(paragraphs):
            if len(paragraphs) == 1 and line_offset == 0:
                uid = self._uid(ll.lineno)
            else:
                uid = f"{self.rel_path}:{ll.lineno + line_offset}"
                count = self._uid_counter.get(uid, 0)
                self._uid_counter[uid] = count + 1
                if count or len(paragraphs) > 1:
                    uid = f"{uid}#{count + 1}"

            meta = SayMeta(
                who=who,
                attributes=list(parsed["attributes"]),
                temporary_attributes=list(parsed["temporary_attributes"]),
                with_=parsed["with"],
                arguments=parsed["arguments"],
                interact=parsed["interact"],
                explicit_id=parsed["explicit_id"],
            )

            code_parts = dict(parsed)
            code_parts["what_tok"] = tok
            meta.code = say_get_code(code_parts, para_text)

            item = TextItem(
                uid=uid,
                file=self.rel_path,
                line=ll.lineno + line_offset,
                kind=kind,
                original=para_text,
                label=label,
                character=display_who,
                say=meta,
                edit=Edit(
                    start=abs_start,
                    end=abs_end,
                    quote=tok.quote,
                    raw=tok.raw_prefix,
                    triple=tok.triple,
                ),
                extra={
                    "stmt_index": stmt_index,
                    "indent": ll.indent,
                    "paragraph_index": p_index,
                    "paragraph_count": len(paragraphs),
                },
            )
            if len(paragraphs) > 1:
                item.extra["group"] = group_id
            self.items.append(item)

            self._pending_says.append(
                _PendingSay(
                    item=item,
                    label=label,
                    code=meta.code,
                    explicit_id=parsed["explicit_id"],
                    alternate=alternate,
                )
            )

    # ------------------------------------------------------------------
    def _register_string_literal(self, ll: LogicalLine, tok: Tok, kind: str,
                                 stmt_index: int, label: Optional[str],
                                 character: Optional[str],
                                 shift: int = 0) -> Optional[TextItem]:
        """
        Регистрирует строковый литерал.

        `shift` — смещение токена относительно начала логической строки
        (нужно, если токены получены из подстроки, например `rest`).
        """
        abs_start = ll.abs_offset(tok.start + shift)
        abs_end = ll.abs_offset(tok.end + shift - 1) + 1
        if self._span_used(abs_start, abs_end):
            return None

        if tok.triple:
            parts = decode_triple_body(tok.value, None, tok.raw_prefix)
            value = "\n".join(p for _, p in parts).strip()
        else:
            value = decode_string_body(tok.value, tok.raw_prefix)

        if not value.strip():
            return None

        item = TextItem(
            uid=self._uid(ll.lineno),
            file=self.rel_path,
            line=ll.lineno,
            kind=kind,
            original=value,
            label=label,
            character=character,
            edit=Edit(start=abs_start, end=abs_end, quote=tok.quote,
                      raw=tok.raw_prefix, triple=tok.triple),
            extra={"stmt_index": stmt_index, "indent": ll.indent},
        )
        self._used_spans.append((abs_start, abs_end))
        self._add_item(item, stmt_index)
        return item

    # ------------------------------------------------------------------
    def _extract_character_name(self, ll: LogicalLine, var: str,
                                stmt_index: int, label: Optional[str]) -> None:
        """`define e = Character("Eileen")` -> имя «Eileen» переводится."""
        text = ll.text
        start = text.index("(") + 1
        toks = tokenize(text[start:])

        name_tok: Optional[Tok] = None
        if toks and toks[0].kind == "string":
            name_tok = toks[0]
        else:
            for i, t in enumerate(toks[:-1]):
                if t.kind == "word" and t.value == "name" and toks[i + 1].kind == "op" \
                        and toks[i + 1].value == ":" and i + 2 < len(toks) \
                        and toks[i + 2].kind == "string":
                    name_tok = toks[i + 2]
                    break
            if name_tok is None:
                # name=... внутри kwargs: ищем 'name' '=' ... упрощённо
                for i, t in enumerate(toks):
                    if t.kind == "word" and t.value == "name":
                        for j in range(i + 1, min(i + 3, len(toks))):
                            if toks[j].kind == "string":
                                name_tok = toks[j]
                                break
                    if name_tok:
                        break

        if name_tok is None:
            return

        # пересчитываем смещения: tokenize шёл по подстроке
        shift = start
        abs_start = ll.abs_offset(name_tok.start + shift)
        abs_end = ll.abs_offset(name_tok.end + shift - 1) + 1
        if self._span_used(abs_start, abs_end):
            return

        value = decode_string_body(name_tok.value, name_tok.raw_prefix)
        if not value.strip():
            return

        # имя может быть обёрнуто в _("...") — тогда это уже UI-строка
        prefix = ll.text[max(0, name_tok.start + shift - 3):name_tok.start + shift]
        if prefix.rstrip().endswith("_(") or prefix.rstrip().endswith("_ ("):
            return

        item = TextItem(
            uid=self._uid(ll.lineno),
            file=self.rel_path,
            line=ll.lineno,
            kind=KIND_NAME,
            original=value,
            label=label,
            character=var,
            edit=Edit(start=abs_start, end=abs_end, quote=name_tok.quote,
                      raw=name_tok.raw_prefix, triple=name_tok.triple),
            extra={"stmt_index": stmt_index, "variable": var, "indent": ll.indent},
        )
        self._used_spans.append((abs_start, abs_end))
        self._add_item(item, stmt_index)

    # ------------------------------------------------------------------
    def _handle_menu_line(self, ll: LogicalLine, stmt_index: int,
                          label: Optional[str], alternate: Optional[str] = None) -> None:
        toks = tokenize(ll.text)
        if not toks:
            return

        # `menu:` может содержать «say-менюреплику»:  s "Текст"  (без двоеточия).
        # Ren'Py превращает её в Say с interact=False.
        if toks[0].kind == "word" and not ll.text.endswith(":"):
            if parse_say_statement(ll.text):
                self._handle_statement_line(ll, stmt_index, label, alternate,
                                            force_nointeract=True)
                return

        if toks[0].kind != "string":
            return
        tok = toks[0]
        value = decode_string_body(tok.value, tok.raw_prefix)
        if not value.strip():
            return

        abs_start = ll.abs_offset(tok.start)
        abs_end = ll.abs_offset(tok.end - 1) + 1
        if self._span_used(abs_start, abs_end):
            return

        item = TextItem(
            uid=self._uid(ll.lineno),
            file=self.rel_path,
            line=ll.lineno,
            kind=KIND_MENU,
            original=value,
            label=label,
            edit=Edit(start=abs_start, end=abs_end, quote=tok.quote,
                      raw=tok.raw_prefix, triple=tok.triple),
            extra={"stmt_index": stmt_index, "indent": ll.indent},
        )
        self._used_spans.append((abs_start, abs_end))
        self._add_item(item, stmt_index)

        if ll.text.endswith(":"):
            pass  # стек обновит основной цикл

    # ------------------------------------------------------------------
    def _handle_screen_line(self, ll: LogicalLine, stmt_index: int,
                            label: Optional[str]) -> None:
        """
        UI-текст в screen language: ``text "Start"``, ``textbutton "Load"``.

        Берём только ПЕРВЫЙ аргумент: свойства вроде ``style "x"``,
        ``id "what"``, ``font "a.ttf"`` переводить нельзя.
        """
        toks = tokenize(ll.text)
        if len(toks) < 2 or toks[0].kind != "word":
            return
        if toks[0].value not in SCREEN_TEXT_WORDS:
            return
        if toks[1].kind != "string":
            return
        self._register_string_literal(ll, toks[1], KIND_UI, stmt_index, label, None)

    # ------------------------------------------------------------------
    def _extract_underscore_strings(self, ll: LogicalLine, stmt_index: int) -> None:
        """Извлекает строки, обёрнутые в ``_("...")`` (стандартный маркер Ren'Py)."""
        if not self.settings.extract_ui:
            return
        text = ll.text
        for m in UNDERSCORE_RE.finditer(text):
            i = m.end()
            n = len(text)
            literals: list[Tok] = []
            while i < n:
                while i < n and text[i] in " \t\n\\":
                    i += 1
                if i < n and text[i] in QUOTES:
                    j, body, quote, triple, _ = _scan_string(text, i)
                    # обрезаем лишние кавычки в body
                    value_body = text[i + (3 if triple else 1):j - (3 if triple else 1)]
                    literals.append(Tok("string", text[i:j], value_body, i, j, quote, triple, False))
                    i = j
                    continue
                break

            if not literals:
                continue

            if len(literals) == 1:
                self._register_string_literal(ll, literals[0], KIND_UI, stmt_index, None, None)
            else:
                # конкатенация строк внутри _("a" "b") — берём как одну единицу
                value = "".join(
                    decode_string_body(t.value, t.raw_prefix) for t in literals
                )
                first, last = literals[0], literals[-1]
                abs_start = ll.abs_offset(first.start)
                abs_end = ll.abs_offset(last.end - 1) + 1
                if not value.strip() or self._span_used(abs_start, abs_end):
                    continue
                item = TextItem(
                    uid=self._uid(ll.lineno),
                    file=self.rel_path,
                    line=ll.lineno,
                    kind=KIND_UI,
                    original=value,
                    edit=None,  # прямая замена таких строк не поддерживается
                    extra={"stmt_index": stmt_index, "indent": ll.indent},
                )
                self._used_spans.append((abs_start, abs_end))
                self._add_item(item, stmt_index)

    # ------------------------------------------------------------------
    def _assign_block_ids(self) -> None:
        """Второй проход: вычисляем идентификаторы translate-блоков по порядку."""
        for pending in self._pending_says:
            digest = IdRegistry.digest([pending.code])
            if pending.explicit_id:
                # У явного id приоритет; alternate-идентификатор не критичен.
                identifier = pending.explicit_id
                self.registry.used.add(identifier)
            else:
                identifier = self.registry.unique_identifier(pending.label, digest)
            if pending.item.say is not None:
                pending.item.say.block_id = identifier

    # ------------------------------------------------------------------
    def _fill_context(self) -> None:
        """Заполняет context_before / context_after для каждого элемента."""
        total = len(self._stmt_texts)
        for item in self.items:
            idx = item.extra.get("stmt_index")
            if idx is None:
                continue
            before = self._stmt_texts[idx - 1] if 0 <= idx - 1 < total else ""
            after = self._stmt_texts[idx + 1] if 0 <= idx + 1 < total else ""
            item.context_before = before[:300]
            item.context_after = after[:300]


# ===========================================================================
# 7. Публичный API
# ===========================================================================
#: Строка, «похожая на реплику»: необязательное имя говорящего + строка в кавычках.
LOOSE_SAY_RE = re.compile(
    r'^\s*(?:[A-Za-z_][\w.]*(?:\s+[A-Za-z_][\w.\-]*)*\s+)?'
    r'(?:r?)"[^"]{2,}"\s*(?:with\s+\S+)?\s*:?\s*$'
)


def count_candidate_says(content: str) -> int:
    """Сколько строк в файле «выглядят как реплики» (грубая оценка)."""
    n = 0
    for line in content.splitlines():
        if line.lstrip().startswith("#"):
            continue
        if LOOSE_SAY_RE.match(line):
            n += 1
    return n


def check_coverage(content: str, parsed: ParsedFile) -> None:
    """
    Страховка от «тихого» недобора текста.

    Если в файле сотни строк, похожих на реплики, а распознали единицы —
    это признак того, что парсер не понял файл (иная кодировка, переводы
    строк, необычный синтаксис). Лучше честно предупредить пользователя,
    чем молча перевести один интерфейс.
    """
    candidates = count_candidate_says(content)
    # Считаем любые извлечённые строки: в screens.rpy «похожие на реплики»
    # строки — это элементы интерфейса (`text "…"`), их не должно быть
    # в знаменателе как пропущенный текст.
    found = len(parsed.items)
    if candidates >= 20 and found < candidates * 0.5:
        parsed.warnings.append(
            f"{parsed.path}: похоже, текст распознан не полностью — "
            f"строк, похожих на реплики: {candidates}, распознано: {found}. "
            f"Пришлите этот файл разработчику, чтобы добавить поддержку."
        )
        logger.warning(
            "%s: возможен недобор текста (похожих на реплики строк %d, "
            "распознано %d)", parsed.path, candidates, found)


def parse_file(path: Path, rel_path: str, registry: Optional[IdRegistry] = None,
               settings: Optional[Settings] = None) -> ParsedFile:
    """Разбирает один файл."""
    settings = settings or Settings()
    registry = registry or IdRegistry()
    content = read_text_file(path)
    parser = FileParser(content, rel_path, registry, settings)
    try:
        parsed = parser.parse()
        check_coverage(content, parsed)
        return parsed
    except Exception as exc:  # pragma: no cover
        logger.exception("Ошибка разбора %s", rel_path)
        return ParsedFile(
            path=rel_path,
            items=[],
            labels=[],
            warnings=[f"Не удалось разобрать {rel_path}: {exc}"],
        )


#: Признаки файлов инструментов разработчика. Игрок их не видит: это редакторы
#: экранов/спрайтов, тестовые сцены и отладочные меню, которые разработчик
#: положил в game/. Строки там технические («spline_editor», «[v[0]]»,
#: «Select a warper function») — переводить их бессмысленно, а в отчёте они
#: выглядят как «204 строки не перевелись».
DEV_TOOL_MARKERS = (
    "actioneditor", "action_editor", "screen_editor", "screeneditor",
    "sprite_editor", "spriteeditor", "developer_tools", "dev_tools",
    "devtools", "testcases", "_testcase", "renpy_editor", "editor_screens",
    "shader_editor", "shadereditor", "warper_editor", "warpereditor",
    "debug_menu", "debugmenu",
    "zz_test_", "test_scene", "tests/",
)

#: Обычные слова, которые не должны попадать под фильтр (мало ли что в пути).
DEV_TOOL_SAFE = ("screens.rpy", "options.rpy", "gui.rpy", "script.rpy")


def is_dev_tool_file(relative: str,
                     directory_hints: tuple[str, ...] = ()) -> bool:
    """Похож ли путь на файл инструмента разработчика."""
    low = str(relative).replace("\\", "/").lower()
    name = low.rsplit("/", 1)[-1]
    if name in DEV_TOOL_SAFE:
        return False
    if any(hint and hint in low for hint in directory_hints):
        return True
    return any(marker in low for marker in DEV_TOOL_MARKERS)


def parse_project(project: RenpyProject, settings: Optional[Settings] = None,
                  files: Optional[list[Path]] = None) -> ParsedProject:
    """
    Разбирает все .rpy файлы проекта.

    Файлы обрабатываются в алфавитном порядке — так же, как их загружает
    сам Ren'Py, что важно для уникальности идентификаторов translate-блоков.
    """
    settings = settings or Settings()
    result = ParsedProject()
    registry = IdRegistry()

    rpy_files = files if files is not None else project.rpy_files
    rpy_files = [p for p in rpy_files if p.suffix.lower() in (".rpy", ".rpym")]
    rpy_files.sort(key=lambda p: project.relative(p).lower())

    skipped_dev: list[str] = []
    for path in rpy_files:
        rel = safe_relpath(path, project.game_dir)
        if getattr(settings, "skip_dev_tools", True) and is_dev_tool_file(rel):
            skipped_dev.append(rel)
            continue
        parsed = parse_file(path, rel, registry, settings)
        result.files.append(parsed)
        result.warnings.extend(parsed.warnings)

    if skipped_dev:
        preview = ", ".join(skipped_dev[:4])
        if len(skipped_dev) > 4:
            preview += f" и ещё {len(skipped_dev) - 4}"
        result.warnings.append(
            f"пропущены файлы инструментов разработчика ({len(skipped_dev)}): "
            f"{preview} — игрок их не видит (переводить их можно ключом "
            f"--translate-dev-tools)")

    return result
