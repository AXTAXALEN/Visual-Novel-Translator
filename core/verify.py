"""
Проверка готового каталога ``game/tl/<язык>/`` — без запуска игры (v3.0).

Это «линтер» перевода: он читает сгенерированные файлы как текст и ищет то,
из-за чего Ren'Py ругается при старте или молча показывает пустоту:

* посторонний оператор верхнего уровня в tl-файле (частая причина
  ``Parsing the script failed``);
* ``translate`` на другой язык внутри ``tl/<язык>/``;
* дубликаты идентификаторов блоков (Ren'Py: «translation ... already exists»);
* непарные кавычки и незакрытые скобки;
* блоки без строк, пустые ``new ""`` (это именно то, что делает текст пустым!);
* расхождение ``old``/``new`` в strings-файлах;
* потерянные плейсхолдеры между комментарием-оригиналом и переводом;
* устаревшие ``.rpyc`` рядом со свежими ``.rpy``;
* покрытие: сколько блоков оригинала реально переведено.

Проверка полностью статическая: Ren'Py и его SDK не нужны.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional

from config import TL_DIRECTORY
from core.quality import _group_counts
from core.utils import get_logger

logger = get_logger(__name__)

SEVERITY_ERROR = "error"
SEVERITY_WARNING = "warning"
SEVERITY_INFO = "info"

_TRANSLATE_RE = re.compile(r"^translate\s+([A-Za-z_][\w]*)\s+([A-Za-z_][\w]*)\s*:\s*$")
_TOP_LEVEL_OK = re.compile(
    r"^(?:#|$|translate\s|init\b|screen\s|style\s|define\s|default\s|"
    r"transform\s|image\s|label\s|python\b)")
_SAY_RE = re.compile(
    r'^\s+(?:[A-Za-z_][\w.]*\s+)*"(?:[^"\\]|\\.)*"')
_OLD_RE = re.compile(r'^\s+old\s+"((?:[^"\\]|\\.)*)"\s*$')
_NEW_RE = re.compile(r'^\s+new\s+"((?:[^"\\]|\\.)*)"\s*$')
_SAY_STRING_RE = re.compile(r'"((?:[^"\\]|\\.)*)"')
_COMMENT_ORIGINAL_RE = re.compile(r'^\s+#\s+(?:[A-Za-z_][\w.]*\s+)?\"((?:[^\"\\]|\\.)*)\"')


@dataclass
class VerifyIssue:
    severity: str
    code: str
    message: str
    file: str = ""
    line: int = 0

    def label(self) -> str:
        names = {
            "stray_statement": "посторонний оператор",
            "wrong_language": "чужой язык в tl",
            "duplicate_id": "дубликат блока",
            "unbalanced_quotes": "непарные кавычки",
            "empty_block": "блок без строк",
            "empty_new": "пустой new",
            "old_new_mismatch": "old/new не совпадают",
            "placeholder_lost": "потерян плейсхолдер",
            "stale_rpyc": "устаревший .rpyc",
            "missing_translation": "нет перевода блока",
            "bad_header": "странный заголовок",
            "orphan_translation": "блок-сирота",
        }
        return names.get(self.code, self.code)

    def where(self) -> str:
        return f"{self.file}:{self.line}" if self.file else ""


@dataclass
class VerifyReport:
    """Результат проверки каталога перевода."""

    language: str = "russian"
    tl_dir: Optional[Path] = None
    files: int = 0
    blocks: int = 0
    strings: int = 0
    empty_blocks: int = 0
    issues: list[VerifyIssue] = field(default_factory=list)
    coverage_total: int = 0
    coverage_done: int = 0

    @property
    def errors(self) -> list[VerifyIssue]:
        return [i for i in self.issues if i.severity == SEVERITY_ERROR]

    @property
    def warnings(self) -> list[VerifyIssue]:
        return [i for i in self.issues if i.severity == SEVERITY_WARNING]

    @property
    def ok(self) -> bool:
        return not self.errors

    @property
    def coverage(self) -> float:
        if not self.coverage_total:
            return 0.0
        return self.coverage_done / self.coverage_total

    def add(self, severity: str, code: str, message: str,
            file: str = "", line: int = 0) -> None:
        self.issues.append(VerifyIssue(severity, code, message, file, line))

    def summary(self) -> str:
        parts = [f"Проверка tl/{self.language}: файлов {self.files}, "
                 f"блоков {self.blocks}, строк интерфейса {self.strings}"]
        if self.coverage_total:
            parts.append(f"покрытие {self.coverage * 100:.1f}% "
                         f"({self.coverage_done}/{self.coverage_total})")
        if self.issues:
            parts.append(f"ошибок {len(self.errors)}, "
                         f"предупреждений {len(self.warnings)}")
        else:
            parts.append("проблем нет")
        return "; ".join(parts)

    def counts(self) -> dict[str, int]:
        return dict(Counter(i.code for i in self.issues).most_common())

    def to_dict(self) -> dict:
        return {
            "language": self.language,
            "files": self.files,
            "blocks": self.blocks,
            "strings": self.strings,
            "coverage": round(self.coverage, 4),
            "coverage_total": self.coverage_total,
            "coverage_done": self.coverage_done,
            "counts": self.counts(),
            "issues": [
                {"severity": i.severity, "code": i.code, "message": i.message,
                 "file": i.file, "line": i.line}
                for i in self.issues[:500]
            ],
        }


# ---------------------------------------------------------------------------
def _unbalanced_quotes(line: str) -> bool:
    """Нечётное число неэкранированных кавычек в строке."""
    count = 0
    index = 0
    while index < len(line):
        char = line[index]
        if char == "\\":
            index += 2
            continue
        if char == '"':
            count += 1
        index += 1
    return count % 2 == 1


def verify_file(path: Path, language: str, report: VerifyReport,
                block_ids: Optional[set[str]] = None) -> set[str]:
    """Проверить один tl-файл. Возвращает найденные в нём идентификаторы блоков.

    ``block_ids`` — общий (межфайловый) набор идентификаторов: дубликат блока
    в другом файле тоже ломает игру, поэтому набор передаётся снаружи.
    """
    ids: set[str] = set()
    seen_ids = block_ids if block_ids is not None else ids
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        report.add(SEVERITY_ERROR, "unreadable", f"не читается: {exc}", path.name)
        return ids

    if "\ufffd" in text:
        report.add(SEVERITY_WARNING, "bad_encoding",
                   "файл сохранён не в UTF-8", path.name)

    lines = text.splitlines()
    current_id: Optional[str] = None
    strings_mode = False
    current_comment_original: str = ""
    block_has_text = False
    old_new: list[tuple[int, str]] = []
    pending_old: Optional[tuple[int, str]] = None
    block_start = 0

    def close_block() -> None:
        nonlocal current_id, block_has_text, strings_mode
        if current_id and not block_has_text:
            report.empty_blocks += 1
            report.add(SEVERITY_WARNING, "empty_block",
                       f"блок {current_id} не содержит строк перевода",
                       path.name, block_start)
        current_id = None
        block_has_text = False
        strings_mode = False

    for number, line in enumerate(lines, start=1):
        if not line.strip():
            continue

        match = _TRANSLATE_RE.match(line)
        if match:
            close_block()
            lang, block_id = match.group(1), match.group(2)
            block_start = number
            if lang != language:
                report.add(SEVERITY_ERROR, "wrong_language",
                           f"перевод на «{lang}» внутри tl/{language}", path.name,
                           number)
            if block_id == "strings":
                # секция old/new — не диалоговый блок
                strings_mode = True
                current_id = None
                continue
            if block_id in seen_ids:
                report.add(SEVERITY_ERROR, "duplicate_id",
                           f"идентификатор {block_id} повторяется", path.name,
                           number)
            seen_ids.add(block_id)
            ids.add(block_id)
            current_id = block_id
            block_has_text = False
            current_comment_original = ""
            continue

        if not line[:1].isspace() and not _TOP_LEVEL_OK.match(line):
            report.add(SEVERITY_ERROR, "stray_statement",
                       f"оператор верхнего уровня в tl-файле: "
                       f"{line.strip()[:60]}", path.name, number)
            continue

        if _unbalanced_quotes(line):
            report.add(SEVERITY_ERROR, "unbalanced_quotes",
                       f"непарная кавычка: {line.strip()[:60]}", path.name,
                       number)

        if current_id is None:
            # strings-секция или наши служебные файлы
            if not strings_mode:
                continue
            old_match = _OLD_RE.match(line)
            if old_match:
                pending_old = (number, old_match.group(1))
                continue
            new_match = _NEW_RE.match(line)
            if new_match:
                value = new_match.group(1)
                report.strings += 1
                if not value.strip():
                    report.add(SEVERITY_ERROR, "empty_new",
                               "new \"\" обнулит текст в игре", path.name, number)
                if pending_old and value.strip() == pending_old[1].strip() \
                        and value.strip():
                    report.add(SEVERITY_INFO, "same_as_old",
                               "перевод совпадает с оригиналом", path.name, number)
                pending_old = None
            continue

        # внутри блока перевода
        comment = _COMMENT_ORIGINAL_RE.match(line)
        if comment:
            current_comment_original = comment.group(1)
            continue
        if _SAY_RE.match(line):
            block_has_text = True
            if current_comment_original:
                say = _SAY_STRING_RE.search(line)
                if say:
                    source = _group_counts(current_comment_original)
                    target = _group_counts(say.group(1))
                    if source - target:
                        lost = ", ".join(sorted(source - target)[:3])
                        report.add(SEVERITY_ERROR, "placeholder_lost",
                                   f"в переводе нет {lost}", path.name, number)

    close_block()
    return ids


def verify_tl_dir(tl_dir: Path, language: str,
                  expected_ids: Optional[Iterable[str]] = None) -> VerifyReport:
    """Проверить весь каталог ``tl/<язык>``."""
    report = VerifyReport(language=language, tl_dir=Path(tl_dir))
    if not tl_dir.is_dir():
        report.add(SEVERITY_ERROR, "missing_dir",
                   f"каталог {tl_dir} не найден")
        return report

    all_ids: set[str] = set()
    for path in sorted(Path(tl_dir).rglob("*.rpy")):
        report.files += 1
        ids = verify_file(path, language, report, all_ids)
        all_ids |= ids
        report.blocks += len(ids)

        # устаревший .rpyc рядом (Ren'Py может взять именно его)
        compiled = path.with_suffix(".rpyc")
        if compiled.exists():
            try:
                if compiled.stat().st_mtime < path.stat().st_mtime:
                    report.add(SEVERITY_WARNING, "stale_rpyc",
                               f"{compiled.name} старше исходника — удалите его",
                               str(path.relative_to(tl_dir.parent.parent)))
            except OSError:
                pass

    if expected_ids is not None:
        expected = set(expected_ids)
        report.coverage_total = len(expected)
        report.coverage_done = len(expected & all_ids)
        missing = expected - all_ids
        for block_id in sorted(missing)[:25]:
            report.add(SEVERITY_WARNING, "missing_translation",
                       f"блок {block_id} не переведён")
        if len(missing) > 25:
            report.add(SEVERITY_WARNING, "missing_translation",
                       f"…и ещё {len(missing) - 25} блоков без перевода")

    logger.info(report.summary())
    return report


def verify_project(project, language: str,
                   parsed=None) -> VerifyReport:
    """Проверить каталог перевода проекта (удобная обёртка)."""
    tl_dir = Path(project.game_dir) / TL_DIRECTORY / language
    expected: Optional[set[str]] = None
    if parsed is not None:
        expected = {item.say.block_id for item in parsed.items
                    if getattr(item, "say", None)
                    and getattr(item.say, "block_id", "")}
    return verify_tl_dir(tl_dir, language, expected)
