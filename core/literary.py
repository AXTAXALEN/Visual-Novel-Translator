"""
Литературный перевод: то, что нельзя затирать машинным.

Зачем это нужно
---------------
Хороший перевод новеллы делается руками: файл `renpy_translator_export.txt`
уходит агенту или переводчику, возвращается с заполненными `RU:` — и его
импортируют в игру. Беда в том, что любой следующий запуск («Сделать всё
автоматически», «Починить меню», пересборка `tl/`) снова прогоняет строки
через машинный переводчик. Если в этот момент авторский текст не лежит в
кэше, он молча заменяется на машинный — и вся работа пропадает.

Что делает модуль
-----------------
Держит отдельный файл ``game/tl/<язык>/rt_literary.json`` со строками
«оригинал → авторский перевод». Ведёт он себя как последний рубеж:

* импорт перевода сразу пополняет этот файл;
* при любом запуске такие строки берутся **из него**, а не из кэша и не от
  машинного переводчика;
* машинный перевод в него не пишется никогда — только импорт вручную.

Файл лежит внутри игры, поэтому переживает переезд программы, смену exe,
очистку кэша и повторные «Починить меню». Формат простой JSON, его можно
править руками или держать в git.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional

from config import TL_DIRECTORY
from core.scanner import RenpyProject
from core.utils import atomic_write_json, get_logger

logger = get_logger(__name__)

#: Имя файла внутри ``game/tl/<язык>/``.
LITERARY_FILENAME = "rt_literary.json"


@dataclass
class LiteraryStore:
    """Строки авторского перевода для одной игры и одного языка."""

    path: Optional[Path] = None
    entries: dict[str, str] = field(default_factory=dict)
    loaded: bool = False
    #: откуда взялись последние добавленные строки (для журнала)
    source: str = "импорт"

    # ------------------------------------------------------------------
    @property
    def count(self) -> int:
        return len(self.entries)

    def __len__(self) -> int:
        return len(self.entries)

    def get(self, original: str) -> Optional[str]:
        value = self.entries.get(original)
        return value if value and value.strip() else None

    def __contains__(self, original: object) -> bool:
        return bool(self.get(str(original)))

    # ------------------------------------------------------------------
    @classmethod
    def default_path(cls, project: RenpyProject, language: str) -> Path:
        return project.game_dir / TL_DIRECTORY / language / LITERARY_FILENAME

    @classmethod
    def load(cls, project: RenpyProject, language: str) -> "LiteraryStore":
        path = cls.default_path(project, language)
        return cls.load_file(path)

    @classmethod
    def load_file(cls, path: Path) -> "LiteraryStore":
        store = cls(path=path, loaded=True)
        try:
            if not path.is_file():
                return store
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            logger.warning("Литературный перевод не прочитан (%s): %s", path, exc)
            return store

        if isinstance(data, dict):
            raw = data.get("strings", data)
            if isinstance(raw, dict):
                for original, translation in raw.items():
                    if isinstance(original, str) and isinstance(translation, str) \
                            and translation.strip():
                        store.entries[original] = translation
        logger.info("Литературный перевод: %d строк (%s)", store.count, path.name)
        return store

    # ------------------------------------------------------------------
    def add(self, original: str, translation: str) -> bool:
        """Добавить строку. Возвращает True, если что-то изменилось."""
        if not original or not translation or not translation.strip():
            return False
        if self.entries.get(original) == translation:
            return False
        self.entries[original] = translation
        return True

    def update_many(self, pairs: dict[str, str]) -> int:
        added = 0
        for original, translation in pairs.items():
            if self.add(original, translation):
                added += 1
        return added

    def save(self) -> Optional[Path]:
        if self.path is None:
            return None
        payload = {
            "what": "Авторский (литературный) перевод. Машинный перевод сюда "
                    "не пишется, эти строки всегда главнее.",
            "count": self.count,
            "strings": dict(sorted(self.entries.items())),
        }
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_json(self.path, payload)
            return self.path
        except OSError as exc:
            logger.warning("Не удалось сохранить литературный перевод: %s", exc)
            return None

    # ------------------------------------------------------------------
    def summary(self) -> str:
        if not self.entries:
            return "литературный перевод: нет сохранённых строк"
        return (f"литературный перевод: {self.count} строк "
                f"({self.path.name if self.path else '—'})")


def store_for(project: RenpyProject, language: str) -> LiteraryStore:
    """Загрузить (или создать) хранилище авторского перевода."""
    return LiteraryStore.load(project, language)


def collect_literary_translations(items: Iterable) -> dict[str, str]:
    """Собрать из разобранных строк те, у которых есть перевод."""
    pairs: dict[str, str] = {}
    for item in items:
        translation = (getattr(item, "translation", "") or "").strip()
        original = getattr(item, "original", "") or ""
        if original and translation:
            pairs[original] = translation
    return pairs
