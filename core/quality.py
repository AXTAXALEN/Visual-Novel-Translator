"""
Контроль качества машинного перевода (v3.0).

Задача простая по смыслу и сложная по деталям: после Google нужно убедиться,
что перевод **не сломал** игру и **действительно переведён**. Проверяются:

* плейсхолдеры ``[name]``, теги ``{b}``/``{w=1.0}``, ``%s``/``%d``/``%(name)s``,
  escape-последовательности ``\\n``, ``\\"`` — их количество обязано совпадать
  с оригиналом (иначе Ren'Py покажет пустоту или упадёт на форматировании);
* пустой перевод, «перевод = оригинал», текст без единого символа целевого
  алфавита (движок вернул английский как есть);
* остатки чужой письменности (CJK в русском тексте — признак мусора);
* косметика: двойные пробелы, пробел перед запятой/точкой, слишком длинный
  текст (ломает вёрстку диалогового окна);
* глоссарий: если термин обязателен, его перевод должен присутствовать.

Результат — :class:`QualityReport` со списком :class:`Issue`. Строки с
критичными ошибками программа **перепроверяет один раз**, а если не помогло —
отдаёт оригинал вместо битого текста (``quality_fallback``): лучше английская
реплика, чем сломанная разметка.
"""

from __future__ import annotations

import re
import unicodedata
from collections import Counter
from dataclasses import dataclass, field
from typing import Iterable, Optional

from core.placeholder import cleanup_translation

SEVERITY_ERROR = "error"
SEVERITY_WARNING = "warning"
SEVERITY_INFO = "info"

SEVERITY_ORDER = {SEVERITY_ERROR: 0, SEVERITY_WARNING: 1, SEVERITY_INFO: 2}

#: Проблемы, из-за которых строку стоит перевести заново.
RETRY_CODES = frozenset({
    "empty",
    "placeholder_missing",
    "percent_mismatch",
    "tag_mismatch",
    "unbalanced_brackets",
    "not_translated",
    "foreign_script",
    "placeholder_repaired",
})

#: Проблемы, из-за которых лучше показать оригинал, чем битый перевод.
FATAL_CODES = frozenset({
    "empty",
    "placeholder_missing",
    "percent_mismatch",
    "tag_mismatch",
    "unbalanced_brackets",
})
# Примечание: «не переведено» и «чужая письменность» в FATAL не входят —
# английская реплика игре не вредит, а вот потерянный [name] или {b} ломает
# и вёрстку, и запуск. Такие строки безопаснее вернуть в оригинал.

_TAG_RE = re.compile(r"\{([^{}]*)\}")
_VAR_RE = re.compile(r"\[([^\[\]]+)\]")
_PERCENT_RE = re.compile(r"%(?:\(([^)]+)\))?[-+ #0]*\d*(?:\.\d+)?([sdifrxXeEgGc%])")
_ESCAPE_RE = re.compile(r"\\[ntrabvf\"'\\]")
_LATIN_WORD = re.compile(r"[A-Za-z][A-Za-z'’-]{2,}")
_WS = re.compile(r"\s+")

#: Какая письменность ожидается в переводе для языка.
LANGUAGE_SCRIPT = {
    "russian": "CYRILLIC",
    "ukrainian": "CYRILLIC",
    "belarusian": "CYRILLIC",
    "bulgarian": "CYRILLIC",
    "serbian": "CYRILLIC",
    "kazakh": "CYRILLIC",
    "greek": "GREEK",
    "arabic": "ARABIC",
    "hebrew": "HEBREW",
    "japanese": "CJK",
    "chinese": "CJK",
    "simplified_chinese": "CJK",
    "traditional_chinese": "CJK",
    "korean": "CJK",
    "thai": "THAI",
    "hindi": "DEVANAGARI",
}

#: Слова, которые нормально оставлять латиницей в русском тексте.
LATIN_ALLOWLIST = frozenset({
    "ok", "okay", "sms", "gps", "tv", "dvd", "cd", "usb", "pc", "hq", "fbi",
    "cia", "nasa", "usa", "uk", "mr", "mrs", "ms", "dr", "st", "jr", "sr",
    "iphone", "ipad", "wi", "fi", "email", "e", "mail", "internet", "web",
    "online", "offline", "id", "vip", "pdf", "dj", "mc", "ai", "it", "pro",
    "renpy", "ren'py",
})


@dataclass
class Issue:
    """Одна найденная проблема перевода."""

    code: str
    severity: str
    message: str
    original: str = ""
    translation: str = ""
    uid: str = ""
    where: str = ""
    file: str = ""
    line: int = 0

    @property
    def retry(self) -> bool:
        return self.code in RETRY_CODES

    @property
    def fatal(self) -> bool:
        return self.code in FATAL_CODES

    def label(self) -> str:
        """Короткое описание для таблицы отчёта."""
        names = {
            "empty": "пустой перевод",
            "not_translated": "не переведено",
            "placeholder_missing": "потерян плейсхолдер",
            "placeholder_extra": "лишний плейсхолдер",
            "percent_mismatch": "сломан %-формат",
            "percent_letter": "тип %-подстановки",
            "placeholder_repaired": "плейсхолдер дописан в конец",
            "tag_mismatch": "не совпали теги",
            "unbalanced_brackets": "незакрытые скобки",
            "newline_lost": "потерян перенос строки",
            "foreign_script": "чужая письменность",
            "latin_leftovers": "латиница внутри перевода",
            "double_space": "двойной пробел",
            "space_before_punct": "пробел перед знаком",
            "too_long": "слишком длинный текст",
            "glossary_missing": "не применён термин",
        }
        return names.get(self.code, self.code)

    def key(self) -> tuple:
        return (self.code, self.original, self.translation)


@dataclass
class QualityReport:
    """Итог проверки качества."""

    language: str = "russian"
    checked: int = 0
    issues: list[Issue] = field(default_factory=list)
    retried: int = 0
    repaired: int = 0

    # ------------------------------------------------------------------
    @property
    def errors(self) -> list[Issue]:
        return [i for i in self.issues if i.severity == SEVERITY_ERROR]

    @property
    def warnings(self) -> list[Issue]:
        return [i for i in self.issues if i.severity == SEVERITY_WARNING]

    @property
    def infos(self) -> list[Issue]:
        return [i for i in self.issues if i.severity == SEVERITY_INFO]

    @property
    def ok(self) -> bool:
        return not self.errors

    def counts(self) -> dict[str, int]:
        """Сколько по типам проблем."""
        result = Counter(issue.code for issue in self.issues)
        return dict(result.most_common())

    def retry_texts(self, limit: int = 400) -> list[str]:
        """Оригиналы, которые стоит перевести заново (без дублей)."""
        out: list[str] = []
        seen: set[str] = set()
        for issue in self.issues:
            if not issue.retry or issue.original in seen:
                continue
            seen.add(issue.original)
            out.append(issue.original)
            if len(out) >= limit:
                break
        return out

    def fatal_texts(self) -> set[str]:
        """Оригиналы, для которых безопаснее оставить оригинал."""
        return {i.original for i in self.issues if i.fatal and i.original}

    def summary(self) -> str:
        if not self.issues:
            return f"Качество: проверено {self.checked}, проблем нет"
        return (f"Качество: проверено {self.checked}, ошибок {len(self.errors)}, "
                f"предупреждений {len(self.warnings)}, "
                f"замечаний {len(self.infos)}")

    def to_dict(self) -> dict:
        return {
            "language": self.language,
            "checked": self.checked,
            "retried": self.retried,
            "repaired": self.repaired,
            "counts": self.counts(),
            "issues": [
                {"code": i.code, "severity": i.severity, "message": i.message,
                 "original": i.original, "translation": i.translation,
                 "file": i.file, "line": i.line, "uid": i.uid}
                for i in self.issues
            ],
        }

    def sorted_issues(self) -> list[Issue]:
        return sorted(self.issues,
                      key=lambda i: (SEVERITY_ORDER.get(i.severity, 9), i.code))


# ---------------------------------------------------------------------------
# Вспомогательное
# ---------------------------------------------------------------------------
def _group_counts(text: str) -> Counter:
    """Мультимножество «защищённых» фрагментов текста."""
    counter: Counter = Counter()
    for match in _VAR_RE.finditer(text):
        counter["[" + match.group(1).strip() + "]"] += 1
    for match in _TAG_RE.finditer(text):
        body = match.group(1).strip()
        # у тегов важна только «шапка» (имя), значения переводчик менять вправе
        name = body.split("=")[0].strip().lower()
        counter["{" + name + "}"] += 1
    # %-форматы проверяются отдельно (см. _percent_signature): буква
    # спецификатора может меняться без вреда, количество — нет.
    return counter


def _percent_signature(text: str) -> tuple[int, Counter]:
    """Сколько %-подстановок всего и какие именно."""
    letters: Counter = Counter()
    total = 0
    for match in _PERCENT_RE.finditer(text):
        if match.group(2) == "%":       # это просто знак процента
            continue
        total += 1
        letters[(match.group(1) or "") + match.group(2)] += 1
    return total, letters


def script_of(text: str) -> str:
    """Основная письменность текста (по первому «сильному» символу)."""
    for char in text:
        if not char.isalpha():
            continue
        try:
            name = unicodedata.name(char)
        except ValueError:
            continue
        for script in ("CYRILLIC", "GREEK", "ARABIC", "HEBREW", "HIRAGANA",
                       "KATAKANA", "CJK", "HANGUL", "THAI", "DEVANAGARI",
                       "LATIN"):
            if script in name:
                return script
    return ""


def script_ratio(text: str, script: str) -> float:
    """Доля букв заданной письменности среди всех букв."""
    letters = [c for c in text if c.isalpha()]
    if not letters:
        return 0.0
    if script == "CJK":
        wanted = sum(1 for c in letters if _is_cjk(c))
    else:
        wanted = sum(1 for c in letters if _script_char(c) == script)
    return wanted / len(letters)


def _script_char(char: str) -> str:
    try:
        name = unicodedata.name(char)
    except ValueError:
        return ""
    for script in ("CYRILLIC", "GREEK", "ARABIC", "HEBREW", "HIRAGANA",
                   "KATAKANA", "HANGUL", "THAI", "DEVANAGARI", "LATIN"):
        if script in name:
            return script
    return "CJK" if _is_cjk(char) else ""


def _is_cjk(char: str) -> bool:
    code = ord(char)
    return (0x3040 <= code <= 0x30FF or 0x3400 <= code <= 0x4DBF
            or 0x4E00 <= code <= 0x9FFF or 0xAC00 <= code <= 0xD7AF
            or 0xF900 <= code <= 0xFAFF)


def _strip_markup(text: str) -> str:
    text = _VAR_RE.sub(" ", text)
    text = _TAG_RE.sub(" ", text)
    text = _PERCENT_RE.sub(" ", text)
    text = _ESCAPE_RE.sub(" ", text)
    return text


# ---------------------------------------------------------------------------
# Проверка одной пары
# ---------------------------------------------------------------------------
def _already_in_target_language(text: str, language: str) -> bool:
    """
    Оригинал уже на языке перевода.

    «Начать», «Сохранить» и прочие подписи, которые вписали мы сами, — не
    непереведённые строки, ругать их в отчёте не за что.
    """
    expected = LANGUAGE_SCRIPT.get((language or "").lower(), "")
    if not expected:
        return False
    plain = _strip_markup(text or "")
    if len(plain.strip()) < 2:
        return False
    return script_ratio(plain, expected) >= 0.25


def check_pair(original: str, translation: str, language: str = "russian",
               glossary: Optional[dict[str, str]] = None) -> list[Issue]:
    """Проверить один перевод. Возвращает список проблем (возможно пустой)."""
    issues: list[Issue] = []
    if not translation or not translation.strip():
        issues.append(Issue("empty", SEVERITY_ERROR,
                            "перевод пустой — Ren'Py покажет пустую строку",
                            original, translation))
        return issues

    source, target = _group_counts(original), _group_counts(translation)

    missing = source - target
    extra = target - source
    if missing:
        lost = ", ".join(sorted(missing)[:4])
        issues.append(Issue("placeholder_missing", SEVERITY_ERROR,
                            f"потеряно: {lost}", original, translation))
    if extra:
        gained = ", ".join(sorted(extra)[:4])
        issues.append(Issue("placeholder_extra", SEVERITY_WARNING,
                            f"появилось лишнее: {gained}", original, translation))

    for opening, closing, code in (("{", "}", "tag_mismatch"),
                                   ("[", "]", "unbalanced_brackets")):
        if translation.count(opening) != translation.count(closing):
            issues.append(Issue(code, SEVERITY_ERROR,
                                f"несбалансированные {opening}{closing}",
                                original, translation))
            break

    source_percent, source_letters = _percent_signature(original)
    target_percent, target_letters = _percent_signature(translation)
    if source_percent != target_percent:
        issues.append(Issue("percent_mismatch", SEVERITY_ERROR,
                            f"%-подстановок {target_percent} вместо "
                            f"{source_percent}", original, translation))
    elif source_letters != target_letters:
        issues.append(Issue("percent_letter", SEVERITY_INFO,
                            "изменён тип %-подстановки (обычно безопасно)",
                            original, translation))

    # Признак «автопочинки»: движок вернул текст без плейсхолдеров, и мы
    # дописали их в конец. В игре это работает, но выглядит криво —
    # строку стоит перевести заново.
    trailing = re.search(
        r"(?:\s*(?:\[[^\[\]]+\]|\{[^{}]*\}|"
        r"%(?:\([^)]+\))?[-+ #0]*\d*(?:\.\d+)?[sdifrxXeEgGc%]))+$",
        translation)
    if trailing and len(_VAR_RE.findall(trailing.group(0))) \
            + len(_TAG_RE.findall(trailing.group(0))) \
            + len(_PERCENT_RE.findall(trailing.group(0))) >= 2:
        issues.append(Issue("placeholder_repaired", SEVERITY_WARNING,
                            "плейсхолдеры дописаны в конец строки — "
                            "перевод стоит переделать", original, translation))

    if "\\n" in original and "\\n" not in translation:
        issues.append(Issue("newline_lost", SEVERITY_WARNING,
                            "в оригинале есть перенос строки \\n", original,
                            translation))

    expected_script = LANGUAGE_SCRIPT.get((language or "").lower(), "")
    plain_target = _strip_markup(translation)
    plain_source = _strip_markup(original)

    script_issue = False
    if (expected_script and len(plain_target.strip()) >= 3
            and script_ratio(plain_target, expected_script) < 0.25):
        # Перевод остался на языке оригинала (или движок вернул ерунду).
        code = "not_translated" if translation.strip() == original.strip() \
            else "foreign_script"
        issues.append(Issue(code, SEVERITY_ERROR,
                            "текст остался без перевода", original, translation))
        script_issue = True
    elif (translation.strip() == original.strip() and plain_source.strip()
          and not _already_in_target_language(original, language)):
        issues.append(Issue("not_translated", SEVERITY_ERROR,
                            "перевод совпадает с оригиналом", original,
                            translation))
        script_issue = True

    # CJK/арабица в русском тексте — почти всегда мусор от движка.
    if not script_issue and expected_script in ("CYRILLIC", "LATIN"):
        foreign = [c for c in plain_target
                   if _is_cjk(c) and not _is_cjk_safe(c)]
        if foreign and not any(_is_cjk(c) for c in plain_source):
            sample = "".join(foreign[:6])
            issues.append(Issue("foreign_script", SEVERITY_WARNING,
                                f"чужие символы в тексте: {sample}",
                                original, translation))

    latin_words = [w for w in _LATIN_WORD.findall(plain_target)
                   if w.lower() not in LATIN_ALLOWLIST]
    if expected_script == "CYRILLIC" and latin_words:
        issues.append(Issue("latin_leftovers", SEVERITY_INFO,
                            "латиница: " + ", ".join(latin_words[:5]),
                            original, translation))

    if "  " in translation.strip():
        issues.append(Issue("double_space", SEVERITY_INFO,
                            "двойной пробел", original, translation))
    if re.search(r"\s+[,.:;!?](?!\.)", translation):
        issues.append(Issue("space_before_punct", SEVERITY_WARNING,
                            "пробел перед знаком препинания", original,
                            translation))

    if len(original) > 20 and len(translation) > max(60, len(original) * 2.6):
        issues.append(Issue("too_long", SEVERITY_WARNING,
                            f"длина {len(translation)} против {len(original)} "
                            "в оригинале", original, translation))

    if glossary:
        lowered = translation.lower()
        for term, required in glossary.items():
            if not term or not required:
                continue
            if term.lower() in original.lower() and required.lower() not in lowered:
                issues.append(Issue("glossary_missing", SEVERITY_WARNING,
                                    f"термин «{term}» должен быть «{required}»",
                                    original, translation))
                break

    return issues


def _is_cjk_safe(char: str) -> bool:
    """Символы, которые допустимы (японские кавычки/знаки в цитатах)."""
    return char in "・ー"


# ---------------------------------------------------------------------------
# Проверка набора строк
# ---------------------------------------------------------------------------
def check_translations(pairs: Iterable[tuple[str, str]],
                       language: str = "russian",
                       glossary: Optional[dict[str, str]] = None,
                       limit: int = 4000,
                       repaired: Optional[set[str]] = None) -> QualityReport:
    """Проверить набор пар ``(оригинал, перевод)``."""
    report = QualityReport(language=language)
    seen: set[str] = set()
    for original, translation in pairs:
        if original in seen:
            continue
        seen.add(original)
        report.checked += 1
        for issue in check_pair(original, translation, language, glossary):
            report.issues.append(issue)
        if repaired and original in repaired:
            report.issues.append(Issue(
                "placeholder_repaired", SEVERITY_WARNING,
                "плейсхолдеры были потеряны движком и дописаны в конец — "
                "строку лучше перевести заново", original, translation))
        if report.checked >= limit:
            break
    return report


def check_items(items: Iterable, language: str = "russian",
                glossary: Optional[dict[str, str]] = None,
                limit: int = 4000,
                repaired: Optional[set[str]] = None) -> QualityReport:
    """Проверить переведённые элементы разбора (:class:`TextItem`)."""
    report = QualityReport(language=language)
    seen: set[str] = set()
    for item in items:
        translation = getattr(item, "translation", "") or ""
        original = getattr(item, "original", "") or ""
        if not translation or original in seen:
            continue
        seen.add(original)
        report.checked += 1
        for issue in check_pair(original, translation, language, glossary):
            issue.uid = getattr(item, "uid", "")
            issue.file = getattr(item, "file", "")
            issue.line = int(getattr(item, "line", 0) or 0)
            issue.where = f"{issue.file}:{issue.line}" if issue.file else ""
            report.issues.append(issue)
        if repaired and original in repaired:
            issue = Issue("placeholder_repaired", SEVERITY_WARNING,
                          "плейсхолдеры были потеряны движком и дописаны "
                          "в конец — строку лучше перевести заново",
                          original, translation)
            issue.uid = getattr(item, "uid", "")
            issue.file = getattr(item, "file", "")
            issue.line = int(getattr(item, "line", 0) or 0)
            issue.where = f"{issue.file}:{issue.line}" if issue.file else ""
            report.issues.append(issue)
        if report.checked >= limit:
            break
    return report


def safest_translation(original: str, translation: str,
                       language: str = "russian") -> str:
    """Перевод, который точно не сломает игру (или ``""`` — оставить оригинал)."""
    if not translation:
        return ""
    issues = [i for i in check_pair(original, translation, language) if i.fatal]
    if not issues:
        cleaned = cleanup_translation(translation)
        return cleaned or translation
    # Попытка починить: вернуть потерянные плейсхолдеры в конец строки нельзя —
    # порядок важен, поэтому безопаснее оставить оригинал.
    return ""
