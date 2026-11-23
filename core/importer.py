"""
Импорт готового перевода (JSON / TXT) и «вклеивание» его в элементы проекта.

Поддерживаются:

* JSON, созданный нашей программой (``{"strings": [{"id": ..., "translation": ...}]}``);
* «плоский» JSON-словарь ``{"Оригинал": "Перевод"}`` (как в кэше);
* TXT формата ``[#id] (Персонаж) / EN: / RU:`` — в том числе с
  многострочными переводами.

Сопоставление идёт сначала по ``id``, затем по оригинальному тексту —
так файл можно править и перемешивать без потери данных.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional

from core.models import ParsedProject, TextItem
from core.placeholder import count_protectable
from core.utils import get_logger, read_text_file

logger = get_logger(__name__)

UID_RE = re.compile(r"^\[#(?P<uid>.+?)\]\s*(?:\((?P<who>.*?)\))?\s*$")
HEADER_RE = re.compile(r"^===\s*(?P<file>.+?)\s*\|\s*label:\s*(?P<label>.*?)\s*===\s*$")


class ImportError_(Exception):
    """Ошибка импорта."""


@dataclass
class ImportResult:
    source: Path
    total_in_file: int = 0
    applied: int = 0
    by_id: int = 0
    by_text: int = 0
    missing: int = 0
    suspicious: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    #: оригинал → перевод, ровно то, что было в файле (для авторского
    #: хранилища: туда должен попасть только текст из файла, а не машинный)
    pairs: dict[str, str] = field(default_factory=dict)

    def summary(self) -> str:
        return (f"импорт {self.source.name}: применено {self.applied} "
                f"(по id: {self.by_id}, по тексту: {self.by_text}), "
                f"не найдено: {self.missing}")


# ---------------------------------------------------------------------------
# Парсеры файлов
# ---------------------------------------------------------------------------
def parse_txt(path: Path) -> dict[str, str]:
    """
    Разбирает TXT-экспорт. Возвращает {uid: перевод}.

    Дополнительно кладёт записи с ключом ``"orig:" + оригинал`` — на случай,
    если пользователь менял нумерацию.
    """
    text = read_text_file(path)
    results: dict[str, str] = {}

    current_uid: Optional[str] = None
    current_original: Optional[str] = None
    ru_lines: list[str] = []
    mt_lines: list[str] = []

    def flush() -> None:
        nonlocal current_uid, current_original, ru_lines, mt_lines
        if current_uid is not None:
            translation = "\n".join(ru_lines).strip("\n")
            if not translation.strip():
                # Агент оставил RU: пустым — берём машинный черновик MT:
                translation = "\n".join(mt_lines).strip("\n")
            if translation.strip():
                results[current_uid] = translation
                if current_original:
                    results["orig:" + current_original] = translation
        current_uid = None
        current_original = None
        ru_lines = []
        mt_lines = []

    for raw_line in text.split("\n"):
        line = raw_line.rstrip("\r")

        if line.startswith("[#"):
            flush()
            m = UID_RE.match(line)
            if m:
                current_uid = m.group("uid").strip()
            continue

        if current_uid is None:
            continue

        if line.startswith("EN:"):
            current_original = line[3:].strip()
            continue

        if line.startswith("RU:"):
            ru_lines.append(line[3:].lstrip(" "))
            continue

        if line.startswith("MT:"):
            mt_lines.append(line[3:].lstrip(" "))
            continue

        if line.startswith("===") or line.startswith("#"):
            continue

        if not line.strip():
            # пустая строка завершает многострочный перевод (если он уже начат)
            if ru_lines and any(l.strip() for l in ru_lines):
                flush()
            continue

        if ru_lines:
            ru_lines.append(line.strip())

    flush()
    return results


def parse_json(path: Path) -> tuple[dict[str, str], dict[str, str]]:
    """Возвращает (по_id, по_оригиналу)."""
    data = json.loads(read_text_file(path))
    by_id: dict[str, str] = {}
    by_text: dict[str, str] = {}

    if isinstance(data, dict) and "strings" in data and isinstance(data["strings"], list):
        for entry in data["strings"]:
            if not isinstance(entry, dict):
                continue
            translation = (entry.get("translation") or "").strip()
            if not translation:
                continue
            uid = entry.get("id")
            original = entry.get("original")
            if uid:
                by_id[str(uid)] = translation
            if original:
                by_text[str(original)] = translation
        return by_id, by_text

    if isinstance(data, dict):
        # плоский словарь оригинал -> перевод (совместимо с кэшем)
        for key, value in data.items():
            if isinstance(value, str) and value.strip():
                by_text[str(key)] = value.strip()
            elif isinstance(value, dict) and isinstance(value.get("translation"), str):
                by_text[str(key)] = value["translation"].strip()
        return by_id, by_text

    raise ImportError_("неизвестная структура JSON-файла")


def parse_docx(path: Path) -> tuple[dict[str, str], dict[str, str]]:
    """
    Читает перевод из .docx (наш экспорт, отредактированный в Word).

    Поддерживаются два вида документа:

    * тот, что создаёт программа (абзацы ``[#id]`` / ``EN:`` / ``RU:``);
    * простая таблица из двух колонок: оригинал | перевод.
    """
    try:
        from docx import Document  # type: ignore
    except ImportError as exc:  # pragma: no cover
        raise ImportError_(
            "для импорта DOCX нужен python-docx (pip install python-docx)"
        ) from exc

    by_id: dict[str, str] = {}
    by_text: dict[str, str] = {}

    try:
        doc = Document(str(path))
    except Exception as exc:  # noqa: BLE001
        raise ImportError_(f"не удалось открыть DOCX: {exc}") from exc

    current_uid: Optional[str] = None
    current_original: Optional[str] = None
    ru_lines: list[str] = []

    def flush() -> None:
        nonlocal current_uid, current_original, ru_lines
        if current_uid is not None:
            translation = "\n".join(ru_lines).strip("\n")
            if translation.strip():
                by_id[current_uid] = translation
                if current_original:
                    by_text[current_original] = translation
        current_uid = None
        current_original = None
        ru_lines = []

    for paragraph in doc.paragraphs:
        text = (paragraph.text or "").replace("\xa0", " ").strip()

        if not text:
            continue

        m = UID_RE.match(text)
        if m:
            flush()
            current_uid = m.group("uid").strip()
            continue

        if current_uid is None:
            continue

        if text.startswith("EN:"):
            current_original = text[3:].strip()
            continue

        if text.startswith("RU:"):
            ru_lines.append(text[3:].lstrip(" "))
            continue

        # заголовок раздела — завершаем текущую запись
        style = (getattr(paragraph.style, "name", "") or "").lower()
        if style.startswith("heading") or style.startswith("заголовок"):
            flush()
            continue

        if ru_lines:
            ru_lines.append(text)

    flush()

    # --- запасной вариант: таблица «оригинал | перевод» ---
    if not by_text:
        for table in doc.tables:
            for row in table.rows:
                cells = [c.text.strip() for c in row.cells]
                if len(cells) >= 2 and cells[0] and cells[1]:
                    if cells[0].lower() in ("en", "english", "оригинал", "original"):
                        continue
                    by_text[cells[0]] = cells[1]

    return by_id, by_text


def parse_file(path: Path) -> tuple[dict[str, str], dict[str, str]]:
    suffix = path.suffix.lower()
    if suffix == ".json":
        return parse_json(path)
    if suffix in (".txt", ".md"):
        by_text_in_txt = parse_txt(path)
        by_id = {k: v for k, v in by_text_in_txt.items() if not k.startswith("orig:")}
        by_text = {k[5:]: v for k, v in by_text_in_txt.items() if k.startswith("orig:")}
        return by_id, by_text
    if suffix == ".docx":
        return parse_docx(path)
    if suffix == ".doc":
        raise ImportError_(
            "Старый формат .doc не поддерживается — сохраните файл как .docx "
            "или .txt (Файл → Сохранить как)."
        )
    raise ImportError_(f"неподдерживаемый формат файла: {suffix}")


# ---------------------------------------------------------------------------
# Применение
# ---------------------------------------------------------------------------
def apply_translations(items: Iterable[TextItem], by_id: dict[str, str],
                       by_text: dict[str, str]) -> ImportResult:
    result = ImportResult(source=Path("<memory>"))
    result.total_in_file = len(by_id) or len(by_text)

    for item in items:
        value = by_id.get(item.uid)
        if value is not None:
            item.translation = value
            result.applied += 1
            result.by_id += 1
        else:
            value = by_text.get(item.original)
            if value is not None:
                item.translation = value
                result.applied += 1
                result.by_text += 1
            else:
                result.missing += 1
                continue

        # проверка сохранности служебных вставок
        if count_protectable(item.original) and not item.translation:
            result.suspicious.append(item.uid)

    return result


def import_translations(path: Path, parsed: ParsedProject) -> ImportResult:
    """Импортирует файл перевода и заполняет ``translation`` у элементов."""
    path = Path(path)
    if not path.exists():
        raise ImportError_(f"файл не найден: {path}")

    by_id, by_text = parse_file(path)
    if not by_id and not by_text:
        raise ImportError_(
            f"в файле {path.name} не найдено заполненных переводов "
            "(поле translation / строки RU:)"
        )

    result = apply_translations(parsed.items, by_id, by_text)
    result.source = path

    # Что именно пришло из файла: по тексту — как есть, по id — через
    # оригинал соответствующей строки проекта.
    pairs: dict[str, str] = {}
    for item in parsed.items:
        if item.uid in by_id:
            pairs[item.original] = by_id[item.uid]
    for original, translation in by_text.items():
        pairs[original] = translation
    result.pairs = pairs

    if result.missing:
        result.warnings.append(
            f"{result.missing} строк из проекта остались без перевода"
        )

    logger.info(result.summary())
    return result


def translations_as_dict(parsed: ParsedProject) -> dict[str, str]:
    """Все переводы проекта в виде словаря оригинал -> перевод (для кэша)."""
    out: dict[str, str] = {}
    for item in parsed.items:
        if item.is_translated():
            out.setdefault(item.original, item.result_text())
    return out
