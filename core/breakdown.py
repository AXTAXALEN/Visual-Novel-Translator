"""
«Что именно переводим»: из чего складывается число строк (3.7.5).

Зачем
-----
Полоса прогресса показывает «3300 из 74 281». Если число кажется непомерным,
нужно не верить и не гадать, а увидеть состав: сколько строк в каком файле,
сколько из них уже переведено, сколько дублей, сколько нашлось в кэше.

Модуль ничего не меняет — только считает и объясняет.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional

from config import TL_DIRECTORY
from core.placeholder import looks_translatable
from core.utils import get_logger

logger = get_logger(__name__)

#: Длинные строки текста — так ищем файлы, которые уже переведены.
_CYRILLIC = re.compile(r"[А-Яа-яЁё]")
_ASCII_LETTER = re.compile(r"[A-Za-z]")


def _normalised(text: str) -> str:
    """Текст без разметки и лишних пробелов — для поиска почти-дублей."""
    body = re.sub(r"\{[^}]*\}|\[[^\]]*\]", "", text or "")
    return " ".join(body.split()).lower()


@dataclass
class Breakdown:
    """Из чего складывается число переводимых строк."""

    files: int = 0
    items: int = 0
    unique: int = 0
    already: int = 0
    """Уже есть перевод в игре (tl/, rt_literary.json)."""
    cached: int = 0
    untranslatable: int = 0
    per_kind: dict[str, int] = field(default_factory=dict)
    top_files: list[tuple[str, int]] = field(default_factory=list)
    duplicate_files: list[tuple[str, str]] = field(default_factory=list)
    """Файлы с одинаковым содержимым: (файл, копия чего)."""
    similar: int = 0
    """Почти одинаковые строки (отличаются только разметкой/пробелами)."""
    chars: int = 0

    @property
    def to_translate(self) -> int:
        return max(0, self.unique - self.already - self.cached
                   - self.untranslatable)

    def summary(self) -> str:
        return (f"файлов: {self.files}, строк: {self.items}, "
                f"уникальных: {self.unique}, к переводу: {self.to_translate}")

    def lines(self, limit: int = 12) -> list[str]:
        out = ["Что переводим", ""]
        out.append(f"• файлов разобрано: {self.files}")
        out.append(f"• строк всего: {self.items}")
        out.append(f"• уникальных строк: {self.unique} "
                   f"(остальные — повторы той же фразы)")
        out.append(f"• уже переведено (взять из игры): {self.already}")
        out.append(f"• есть в кэше переводов: {self.cached}")
        out.append(f"• переводить нечего (только теги/цифры): "
                   f"{self.untranslatable}")
        out.append(f"• символов текста: {self.chars:,}".replace(",", " "))
        out.append(f"• к переводу сейчас: {self.to_translate}")

        if self.per_kind:
            names = {
                "dialogue": "реплики", "narration": "нарратив",
                "extend": "продолжения", "menu": "выборы в меню",
                "ui": "интерфейс", "name": "имена персонажей",
            }
            parts = [f"{names.get(kind, kind)}: {count}"
                     for kind, count in sorted(self.per_kind.items(),
                                               key=lambda kv: -kv[1])]
            out.append("• по типам — " + ", ".join(parts))

        if self.top_files:
            out.append("")
            out.append("Самые «тяжёлые» файлы (уникальных строк):")
            for name, count in self.top_files[:limit]:
                out.append(f"   {count:>6}  {name}")
            if len(self.top_files) > limit:
                out.append(f"   … и ещё {len(self.top_files) - limit} файлов")

        if self.duplicate_files:
            out.append("")
            out.append("! файлы-двойники (одинаковое содержимое):")
            for name, twin in self.duplicate_files[:6]:
                out.append(f"   {name} = {twin}")

        if self.similar:
            out.append("")
            out.append(f"  почти одинаковых строк (различаются только "
                       f"разметкой или пробелами): {self.similar} — "
                       f"это нормально, каждая переводится отдельно")

        out.append("")
        out.append("Если число всё равно кажется завышенным — пришлите эти "
                   "строки: по списку файлов сразу видно, что лишнее "
                   "(например, папка со старой распаковкой).")
        return out


def build_breakdown(parsed, pipeline=None,
                    game_translations: Optional[dict[str, str]] = None
                    ) -> Breakdown:
    """Посчитать состав переводимого текста."""
    report = Breakdown()
    if parsed is None:
        return report

    items = list(parsed.items)
    report.items = len(items)
    report.files = len(parsed.source_files or [])
    report.chars = sum(len(item.original or "") for item in items)

    texts: dict[str, int] = {}
    per_file: dict[str, set[str]] = defaultdict(set)
    per_kind: Counter = Counter()
    for item in items:
        text = item.original or ""
        texts[text] = texts.get(text, 0) + 1
        per_file[item.file].add(text)
        per_kind[item.kind] += 1

    report.unique = len(texts)
    report.per_kind = dict(per_kind)
    report.top_files = sorted(
        ((name, len(uniq)) for name, uniq in per_file.items()),
        key=lambda pair: -pair[1])

    # почти одинаковые строки
    normalised: dict[str, int] = {}
    for text in texts:
        key = _normalised(text)
        if key:
            normalised[key] = normalised.get(key, 0) + 1
    report.similar = sum(count - 1 for count in normalised.values()
                         if count > 1)

    # что уже готово
    ready = dict(game_translations or {})
    if pipeline is not None and not ready:
        try:
            ready = pipeline.game_translations()
        except Exception as exc:  # noqa: BLE001
            logger.debug("перевод из игры не прочитан: %s", exc)
            ready = {}
    cache = getattr(pipeline, "cache", None) if pipeline is not None else None

    for text in texts:
        if not looks_translatable(text):
            report.untranslatable += 1
            continue
        if text in ready:
            report.already += 1
            continue
        if cache is not None and cache.get(text):
            report.cached += 1

    # файлы-двойники: одинаковое содержимое у разных файлов
    seen: dict[str, str] = {}
    for item_file, uniq in per_file.items():
        key = "|".join(sorted(uniq))[:200] + f":{len(uniq)}"
        if key in seen:
            report.duplicate_files.append((item_file, seen[key]))
        else:
            seen[key] = item_file

    return report


def iter_translation_files(project) -> Iterable[Path]:
    """
    Файлы перевода игры.

    Нужны только для отчёта: всё, что лежит в ``tl/``, в перевод не попадает.
    """
    root = Path(project.game_dir) / TL_DIRECTORY
    if root.is_dir():
        yield from root.rglob("*.rpy")
