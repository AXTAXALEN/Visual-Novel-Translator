"""
Слияние и сверка частей экспорта.

Зачем
-----
Большие новеллы выгружаются частями (``export-part01.txt`` …), потому что
файл на 100 000 строк неудобно и опасно гонять через агента целиком. После
перевода части нужно собрать обратно — и при этом:

* **ничего не потерять**: каждая строка ``EN:`` из частей обязана попасть
  в общий файл;
* **ничего не заменить**: ``EN:`` в собранном файле должно совпадать с тем,
  что реально в игре;
* **не задублировать**: повторяющиеся шапки и справочные блоки из каждой
  части отбрасываются, тело копируется дословно (байт в байт).

Модуль умеет три вещи:

``collect_parts`` — найти части рядом с указанным файлом/каталогом;
``merge``         — собрать их в один файл, отбросив повторные шапки;
``verify``        — сверить: идентификаторы, количество строк, заполненные
                    ``RU:`` и совпадение ``EN:`` с текстом игры.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional

from core.utils import get_logger

logger = get_logger(__name__)

#: ``renpy_translator_export-part03.txt``
PART_RE = re.compile(r"^(?P<stem>.+?)[-_]part[ _-]?(?P<index>\d+)$", re.I)

#: Строка, с которой начинается тело части.
BODY_MARK = "=== "

#: Идентификатор строки в экспорте: ``[#c-x_edg01.rpy:24]``
UID_RE = re.compile(r"^\[#(?P<uid>[^\]]+)\]\s")


@dataclass
class PartSet:
    """Найденный набор частей одного экспорта."""

    stem: str
    files: list[Path] = field(default_factory=list)

    @property
    def first(self) -> Optional[Path]:
        return self.files[0] if self.files else None


@dataclass
class MergeResult:
    """Итог слияния."""

    path: Path
    parts: list[Path] = field(default_factory=list)
    lines: int = 0
    ids: int = 0
    duplicates: int = 0
    warnings: list[str] = field(default_factory=list)

    def summary(self) -> str:
        size = ""
        try:
            size = f", {self.path.stat().st_size / 1048576:.1f} МБ"
        except OSError:
            pass
        text = (f"частей: {len(self.parts)}, строк: {self.lines}, "
                f"идентификаторов: {self.ids} -> {self.path.name} "
                f"({self.path.name.split('.')[-1]}{size})")
        if self.duplicates:
            text += f", повторов: {self.duplicates}"
        return text


@dataclass
class VerifyResult:
    """Итог сверки частей."""

    parts: list[Path] = field(default_factory=list)
    ids: int = 0
    duplicate_ids: list[str] = field(default_factory=list)
    empty_ru: int = 0
    filled_ru: int = 0
    missing_in_game: list[str] = field(default_factory=list)
    extra_in_game: list[str] = field(default_factory=list)
    changed_en: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not (self.duplicate_ids or self.missing_in_game
                    or self.extra_in_game or self.changed_en
                    or self.problems)

    def summary(self) -> str:
        text = (f"частей: {len(self.parts)}, идентификаторов: {self.ids}, "
                f"переведено RU: {self.filled_ru}, пусто: {self.empty_ru}")
        if self.duplicate_ids:
            text += f", повторов id: {len(self.duplicate_ids)}"
        if self.missing_in_game:
            text += f", потеряно строк: {len(self.missing_in_game)}"
        if self.extra_in_game:
            text += f", лишних строк: {len(self.extra_in_game)}"
        if self.changed_en:
            text += f", изменён EN: {len(self.changed_en)}"
        if self.problems:
            text += f", замечаний: {len(self.problems)}"
        return text

    def lines(self, limit: int = 12) -> list[str]:
        out = [self.summary()]
        for title, items in (("повторяются", self.duplicate_ids),
                             ("нет в файле (потеряно)", self.missing_in_game),
                             ("лишние (нет в игре)", self.extra_in_game),
                             ("EN отличался от игры", self.changed_en),
                             ("замечание", self.problems)):
            for item in items[:limit]:
                out.append(f"  ! {title}: {item}")
            if len(items) > limit:
                out.append(f"  … и ещё {len(items) - limit} «{title}»")
        if self.ok:
            out.append("  Всё сходится: ни одна строка не потеряна и не "
                       "подменена.")
        return out


# ---------------------------------------------------------------------------
# Поиск частей
# ---------------------------------------------------------------------------
def _part_index(path: Path) -> Optional[tuple[str, int]]:
    match = PART_RE.match(path.stem)
    if not match:
        return None
    return match.group("stem"), int(match.group("index"))


def collect_parts(target: Path) -> PartSet:
    """
    Найти части экспорта.

    ``target`` — либо каталог, где лежат части, либо любой файл из набора
    (в том числе сам общий файл — имя подскажет маску).
    """
    target = Path(target)
    if target.is_file():
        found = _part_index(target)
        if found is not None:
            stem, _index = found
            candidates = sorted(target.parent.glob(f"{stem}*"))
        else:
            stem = target.stem
            candidates = sorted(target.parent.glob(f"{stem}-part*"))
            candidates += sorted(target.parent.glob(f"{stem}_part*"))
    else:
        candidates = sorted(path for path in target.iterdir()
                            if path.is_file() and path.suffix.lower() == ".txt")
        stem = None

    by_stem: dict[str, list[tuple[int, Path]]] = {}
    for path in candidates:
        found = _part_index(path)
        if found is None:
            continue
        part_stem, index = found
        by_stem.setdefault(part_stem, []).append((index, path))

    if not by_stem:
        return PartSet(stem=stem or (target.stem if target.is_file()
                                     else ""), files=[])

    # берём набор с наибольшим числом частей (если в каталоге их несколько)
    part_stem, pairs = max(by_stem.items(), key=lambda kv: len(kv[1]))
    pairs.sort(key=lambda item: item[0])
    files = [path for _index, path in pairs]

    # предупреждаем о дырках в нумерации
    indexes = [index for index, _path in pairs]
    if indexes and indexes != list(range(indexes[0], indexes[0] + len(indexes))):
        logger.warning("нумерация частей с пропусками: %s", indexes)

    return PartSet(stem=part_stem, files=files)


# ---------------------------------------------------------------------------
# Слияние
# ---------------------------------------------------------------------------
def _split_header(path: Path) -> tuple[list[str], list[str]]:
    """
    Разделить часть на «шапку» и «тело».

    Шапка — это ведущие строки-комментарии (``#``) и пустые строки: правила
    для агента и справочник персонажей/сцен. Тело начинается с первой
    содержательной строки — ``=== файл | label ===`` или ``[#файл:строка]``.
    Так части, начатые с середины главы, тоже разбираются правильно.
    """
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
    for index, line in enumerate(lines):
        if line.startswith("#") or not line.strip():
            continue
        # пустые строки прямо перед телом принадлежат телу: в экспорте одним
        # файлом перед заголовком «=== файл ===» стоит пустая строка, и она
        # обязана остаться на своём месте
        while index > 0 and not lines[index - 1].strip():
            index -= 1
        return lines[:index], lines[index:]
    return lines, []


def merge(parts: Iterable[Path], output: Optional[Path] = None,
          progress=None) -> MergeResult:
    """
    Собрать части в один файл.

    Шапка берётся из первой части, тело всех частей копируется дословно —
    мы не пересобираем и не переформатируем строки, поэтому перевод не
    может «поехать». Строки-заголовки ``=== файл ===`` из следующих частей
    сохраняются: по ним видно, откуда кусок.
    """
    files = [Path(path) for path in parts]
    if not files:
        raise ValueError("нечего сливать: части не найдены")

    if output is None:
        found = _part_index(files[0])
        stem = found[0] if found else files[0].stem
        output = files[0].with_name(f"{stem}.txt")

    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    tmp = output.with_suffix(output.suffix + ".part")

    ids: set[str] = set()
    duplicates = 0
    line_count = 0

    with tmp.open("w", encoding="utf-8", newline="\n") as out:
        for number, path in enumerate(files, 1):
            header, body = _split_header(path)
            if number == 1:
                for line in header:
                    out.write(line)
                    line_count += 1
            for line in body:
                match = UID_RE.match(line)
                if match:
                    uid = match.group("uid")
                    if uid in ids:
                        duplicates += 1
                    else:
                        ids.add(uid)
                out.write(line)
                line_count += 1
            if progress is not None:
                progress(number, len(files))

    tmp.replace(output)
    logger.info("Слито частей: %d -> %s (id: %d, повторов: %d)",
                len(files), output.name, len(ids), duplicates)
    return MergeResult(path=output, parts=files, lines=line_count,
                       ids=len(ids), duplicates=duplicates)


# ---------------------------------------------------------------------------
# Сверка
# ---------------------------------------------------------------------------
def _read_entries(files: Iterable[Path]):
    """Идём по частям и отдаём (uid, en, ru) для каждой строки."""
    for path in files:
        uid: Optional[str] = None
        en_lines: list[str] = []
        ru_filled = False
        for raw in path.open("r", encoding="utf-8", errors="replace"):
            line = raw.rstrip("\n")
            match = UID_RE.match(line)
            if match:
                if uid is not None:
                    yield uid, "\n".join(en_lines), ru_filled
                uid = match.group("uid")
                en_lines = []
                ru_filled = False
                continue
            if uid is None:
                continue
            if line.startswith("EN: "):
                en_lines = [line[4:]]
            elif en_lines and line.startswith("    ") and not line.startswith("RU:"):
                en_lines.append(line.strip())
            elif line.startswith("RU: "):
                if line[4:].strip():
                    ru_filled = True
                if uid is not None:
                    yield uid, "\n".join(en_lines), ru_filled
                uid = None
                en_lines = []
                ru_filled = False
        if uid is not None:
            yield uid, "\n".join(en_lines), ru_filled


def verify(parts: Iterable[Path], parsed=None,
           limit: int = 5000) -> VerifyResult:
    """
    Сверить части между собой и (если передан ``parsed``) с текстом игры.

    Проверяет: повторяющиеся идентификаторы, заполненность ``RU:``,
    потерянные строки (есть в игре — нет в частях), лишние строки,
    изменённые ``EN:``.
    """
    files = [Path(path) for path in parts]
    result = VerifyResult(parts=files)
    seen: dict[str, str] = {}
    duplicate_ids: list[str] = []

    for uid, en, ru_filled in _read_entries(files):
        if uid in seen:
            duplicate_ids.append(uid)
            continue
        seen[uid] = en
        if ru_filled:
            result.filled_ru += 1
        else:
            result.empty_ru += 1

    result.ids = len(seen)
    result.duplicate_ids = duplicate_ids

    if parsed is not None:
        expected: dict[str, str] = {}
        for item in parsed.items:
            expected[str(item.uid)] = item.original
        result.missing_in_game = [uid for uid in expected if uid not in seen]
        result.extra_in_game = [uid for uid in seen if uid not in expected]
        changed = []
        for uid, en in seen.items():
            original = expected.get(uid)
            if original is None:
                continue
            if en.strip() != str(original).strip():
                changed.append(uid)
        result.changed_en = changed[:limit]

    return result
