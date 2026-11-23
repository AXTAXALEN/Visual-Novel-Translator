"""
Защита служебных конструкций Ren'Py при машинном переводе.

Перед отправкой строки в переводчик всё «непереводимое» заменяется на
плейсхолдеры вида ``\\ue0000\\ue001`` (или ``[[PH0]]``):

* интерполяции переменных: ``[player_name]``, ``[points!t]``, ``[[`` ;
* текстовые теги: ``{b}``, ``{/b}``, ``{color=#ff0000}``, ``{w=0.5}``, ``{{`` ;
* подстановки: ``%s``, ``%d``, ``%(name)s``, ``%%`` ;
* escape-последовательности, если они остались в тексте: ``\\n``, ``\\"``, ``\\\\`` ;
* одиночные обратные слэши.

После перевода плейсхолдеры восстанавливаются. Если переводчик «съел»
часть плейсхолдеров, они возвращаются в конец строки — это лучше, чем
сломанный скрипт (Ren'Py сам закроет незакрытые теги, но потерянный
``{color=#ff0000}`` испортил бы вид).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

#: Основной шаблон токена: символы из Private Use Area — переводчики их
#: обычно не трогают.
TOKEN_PRIMARY = "\ue000{}\ue001"
#: Резервный шаблон: если первый вариант «потерялся».
TOKEN_ALT = "[[PH{}]]"
#: Третий вариант — для совсем капризных движков.
TOKEN_PLAIN = "{{{{{}}}}}"

TOKEN_TEMPLATES = (TOKEN_PRIMARY, TOKEN_ALT, TOKEN_PLAIN)


#: Всё, что нужно защитить. Порядок альтернатив важен!
PROTECT_RE = re.compile(
    r"""
    (?P<brace_open>\{\{)
  | (?P<brace_close>\}\})
  | (?P<bracket_open>\[\[)
  | (?P<bracket_close>\]\])
  | (?P<interp>\[[^\[\]]*\])
  | (?P<tag>\{[^{}]*\})
  | (?P<pct_named>%\([^)]*\)[#0\- +*.\d]*[sdifrxXeEgGc])
  | (?P<pct_pos>%[#0\- +]*\d*(?:\.\d+)?[sdifrxXeEgGc])
  | (?P<pct_esc>%%)
  | (?P<uni>\\u[0-9a-fA-F]{4})
  | (?P<esc>\\[\\'"nt])
  | (?P<esc_space>\\[ ])
  | (?P<slash>\\(?![A-Za-z0-9]) )
    """,
    re.VERBOSE,
)


@dataclass
class ProtectedText:
    """Результат маскировки."""

    masked: str
    slots: list[str] = field(default_factory=list)
    template: str = TOKEN_PRIMARY

    def __bool__(self) -> bool:
        return bool(self.masked)

    @property
    def has_slots(self) -> bool:
        return bool(self.slots)


def _token_regex(template: str) -> re.Pattern[str]:
    """Регулярка для поиска токенов конкретного шаблона (устойчива к пробелам)."""
    if template == TOKEN_PRIMARY:
        return re.compile("\ue000\\s*(\\d+)\\s*\ue001")
    if template == TOKEN_ALT:
        return re.compile(r"\[\[\s*PH\s*(\d+)\s*\]\]", re.IGNORECASE)
    return re.compile(r"\{\{\{\s*(\d+)\s*\}\}\}")


def protect(text: str, template: str = TOKEN_PRIMARY) -> ProtectedText:
    """
    Маскирует служебные конструкции.

    >>> p = protect('Hello, [player_name]! {b}Welcome{/b}.')
    >>> p.masked.startswith('Hello, ')
    True
    >>> len(p.slots)
    4
    """
    slots: list[str] = []
    out: list[str] = []
    last = 0

    for m in PROTECT_RE.finditer(text):
        start, end = m.span()
        # «продолжение» обратного слэша вроде '\\\\(?!)' может дать пустой матч
        if start == end:
            continue
        out.append(text[last:start])
        slots.append(m.group(0))
        out.append(template.format(len(slots) - 1))
        last = end

    out.append(text[last:])
    return ProtectedText("".join(out), slots, template)


@dataclass
class RestoreResult:
    text: str
    missing: list[int] = field(default_factory=list)
    duplicated: list[int] = field(default_factory=list)
    repaired: bool = False

    @property
    def ok(self) -> bool:
        return not self.missing and not self.duplicated


def restore(masked: str, protected: ProtectedText, repair: bool = True) -> RestoreResult:
    """
    Восстанавливает защищённые конструкции.

    Если `repair` — потерянные токены дописываются в конец строки.
    """
    if not protected.slots:
        return RestoreResult(masked)

    pattern = _token_regex(protected.template)
    counts: dict[int, int] = {}
    missing: list[int] = []

    def repl(m: "re.Match[str]") -> str:
        idx = int(m.group(1))
        counts[idx] = counts.get(idx, 0) + 1
        if idx >= len(protected.slots):
            return ""  # мусорный токен
        return protected.slots[idx]

    result = pattern.sub(repl, masked)

    for i in range(len(protected.slots)):
        if counts.get(i, 0) == 0:
            missing.append(i)

    duplicated = [i for i, c in counts.items() if c > 1 and i < len(protected.slots)]

    repaired = False
    if missing and repair:
        tail = "".join(protected.slots[i] for i in sorted(missing))
        result = result.rstrip() + tail
        repaired = True

    return RestoreResult(result, missing, duplicated, repaired)


def verify(masked_translation: str, protected: ProtectedText) -> bool:
    """Все ли токены на месте ровно по одному разу?"""
    if not protected.slots:
        return True
    pattern = _token_regex(protected.template)
    counts: dict[int, int] = {}
    for m in pattern.finditer(masked_translation):
        idx = int(m.group(1))
        counts[idx] = counts.get(idx, 0) + 1
    for i in range(len(protected.slots)):
        if counts.get(i, 0) != 1:
            return False
    return True


def count_protectable(text: str) -> int:
    return sum(1 for _ in PROTECT_RE.finditer(text))


# ---------------------------------------------------------------------------
# Утилиты очистки машинного перевода
# ---------------------------------------------------------------------------
_PUNCT_FIXES = [
    (re.compile(r"\s+([,.;:!?…»)])(?!\d)"), r"\1"),
    (re.compile(r"(?<=\S)\s{2,}"), " "),
    (re.compile(r"[“”„]"), '"'),
    (re.compile(r"[‘’]"), "'"),
]


def cleanup_translation(text: str) -> str:
    """Мягкая косметика: убирает лишние пробелы перед знаками препинания."""
    for pattern, repl in _PUNCT_FIXES:
        text = pattern.sub(repl, text)
    return text.strip()


def looks_translatable(text: str) -> bool:
    """Есть ли в строке хоть что-то, кроме плейсхолдеров/пунктуации."""
    stripped = PROTECT_RE.sub("", text)
    return bool(re.search(r"[^\W\d_]", stripped, flags=re.UNICODE))

# ---------------------------------------------------------------------------
# Экранирование для Ren'Py
# ---------------------------------------------------------------------------
#: Тэги, которые переводчик (человек или агент) имеет право добавлять сам.
SAFE_TEXT_TAGS = frozenset({
    "b", "i", "u", "s", "w", "nw", "p", "fast", "k", "cps", "plain",
    "size", "color", "alpha", "font", "vspace", "space", "rt", "rb",
    "outlinecolor", "interpolation",
})


def _bracket_groups(text: str, opening: str, closing: str) -> set[str]:
    """Все пары ``открывающая...закрывающая`` в строке — целиком."""
    if not text:
        return set()
    pattern = re.escape(opening) + "[^" + re.escape(closing) + "]*" + re.escape(closing)
    return set(re.findall(pattern, text))


def escape_for_renpy(translation: str, original: str = "") -> str:
    """
    Делает перевод безопасным для Ren'Py.

    В Ren'Py ``[переменная]`` — это интерполяция, а ``{тег}`` — тег оформления.
    Если переводчик (или AI-агент) добавит свою квадратную скобку — например,
    пометку ``[ru]`` в начале строки — игра упадёт с ``NameError``.
    Поэтому «чужие» скобки удваиваются: ``[[`` и ``{{`` — так Ren'Py покажет
    их как обычные символы.

    Скобки, которые были в оригинале (``[player_name]``, ``{b}``), и
    стандартные теги (``{i}``, ``{w}`` и т.п.) остаются рабочими.

    >>> escape_for_renpy("[ru] Привет, [player_name]!", "Hello, [player_name]!")
    '[[ru] Привет, [player_name]!'
    """
    if not translation:
        return translation

    allowed_square = _bracket_groups(original, "[", "]")
    allowed_curly = _bracket_groups(original, "{", "}")

    out: list[str] = []
    index = 0
    length = len(translation)

    while index < length:
        char = translation[index]

        if char == "[":
            end = translation.find("]", index + 1)
            if end != -1 and translation[index:end + 1] in allowed_square:
                out.append(translation[index:end + 1])
                index = end + 1
                continue
            out.append("[[")
            index += 1
            continue

        if char == "{":
            end = translation.find("}", index + 1)
            if end != -1:
                group = translation[index:end + 1]
                if group in allowed_curly:
                    out.append(group)
                    index = end + 1
                    continue
                tag = group[1:-1].split("=", 1)[0].strip().lower().lstrip("/")
                if tag in SAFE_TEXT_TAGS:
                    out.append(group)
                    index = end + 1
                    continue
            out.append("{{")
            index += 1
            continue

        out.append(char)
        index += 1

    return "".join(out)


def has_unsafe_brackets(translation: str, original: str = "") -> bool:
    """Появились ли в переводе скобки, которых не было в оригинале."""
    return escape_for_renpy(translation, original) != translation
