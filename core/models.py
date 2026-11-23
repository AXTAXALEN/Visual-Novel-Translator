"""
Модели данных: элементы текста, метаданные диалоговых строк, результаты разбора.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional


#: Типы извлечённых строк.
KIND_DIALOGUE = "dialogue"   # e "Hello"           — реплика персонажа
KIND_NARRATION = "narration"  # "It was dark."      — нарратив
KIND_EXTEND = "extend"       # extend "..."         — продолжение реплики
KIND_MENU = "menu"           # "Choice":            — вариант выбора
KIND_UI = "ui"               # text "Start" / _("Start") — интерфейс
KIND_NAME = "name"           # Character("Eileen")  — отображаемое имя

ALL_KINDS = (KIND_DIALOGUE, KIND_NARRATION, KIND_EXTEND, KIND_MENU, KIND_UI, KIND_NAME)

#: Типы, которые попадают в `translate <lang> <id>:` блоки (диалоги).
BLOCK_KINDS = (KIND_DIALOGUE, KIND_NARRATION, KIND_EXTEND)


@dataclass
class Edit:
    """Как заменить строку в исходном файле (для режима прямой замены)."""

    start: int          # смещение начала литерала в тексте файла (включая кавычки)
    end: int            # смещение конца литерала (за последней кавычкой)
    quote: str = '"'    # исходная кавычка: " ' или `
    raw: bool = False   # был ли литерал raw-строкой (r"...")
    triple: bool = False  # тройные кавычки


@dataclass
class SayMeta:
    """Разобранная «скелетная» часть say-инструкции Ren'Py."""

    who: Optional[str] = None
    attributes: list[str] = field(default_factory=list)
    temporary_attributes: list[str] = field(default_factory=list)
    with_: Optional[str] = None
    arguments: Optional[str] = None   # текст в скобках "(...)" как есть
    interact: bool = True
    explicit_id: Optional[str] = None
    block_id: str = ""                # вычисленный идентификатор translate-блока
    code: str = ""                    # канонический вид строки (как у Ren'Py get_code)


@dataclass
class TextItem:
    """Одна переводимая строка новеллы."""

    uid: str                      # уникальный идентификатор: "script.rpy:42" / "script.rpy:42#2"
    file: str                     # путь относительно game/ (posix)
    line: int                     # номер строки в файле (1-based)
    kind: str                     # см. KIND_*
    original: str                 # декодированный исходный текст
    label: Optional[str] = None   # активный label
    character: Optional[str] = None  # тег персонажа / имя переменной Character
    translation: str = ""         # перевод
    context_before: str = ""      # предыдущая значимая строка скрипта
    context_after: str = ""       # следующая значимая строка скрипта
    end_line: Optional[int] = None
    say: Optional[SayMeta] = None
    edit: Optional[Edit] = None
    extra: dict[str, Any] = field(default_factory=dict)

    # ------------------------------------------------------------------
    @property
    def has_block_id(self) -> bool:
        return bool(self.say and self.say.block_id)

    def is_translated(self) -> bool:
        return bool(self.translation.strip())

    def result_text(self) -> str:
        """Перевод, либо оригинал, если перевода нет."""
        value = self.translation.strip("\n")
        return value if value.strip() else self.original

    def to_export_dict(self) -> dict[str, Any]:
        return {
            "id": self.uid,
            "file": self.file,
            "line": self.line,
            "kind": self.kind,
            "label": self.label,
            "character": self.character,
            "original": self.original,
            "translation": self.translation,
            "context_before": self.context_before,
            "context_after": self.context_after,
            "block_id": self.say.block_id if self.say else None,
        }


@dataclass
class ParsedFile:
    """Результат разбора одного .rpy файла."""

    path: str                     # путь относительно game/
    items: list[TextItem] = field(default_factory=list)
    labels: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


@dataclass
class ParsedProject:
    """Результат разбора всего проекта."""

    files: list[ParsedFile] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    # ------------------------------------------------------------------
    @property
    def items(self) -> list[TextItem]:
        out: list[TextItem] = []
        for f in self.files:
            out.extend(f.items)
        return out

    @property
    def source_files(self) -> list[str]:
        return [f.path for f in self.files]

    def items_by_kind(self, kind: str) -> list[TextItem]:
        return [i for i in self.items if i.kind == kind]

    def stats(self) -> dict[str, int]:
        stats: dict[str, int] = {}
        for item in self.items:
            stats[item.kind] = stats.get(item.kind, 0) + 1
        stats["total"] = len(self.items)
        stats["unique_texts"] = len({i.original for i in self.items})
        return stats
