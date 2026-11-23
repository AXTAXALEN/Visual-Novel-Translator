"""
Возврат уже сделанного перевода из самой игры (3.7.2).

Зачем
-----
Кэш переводов раньше лежал рядом с программой (``translation_cache.json``
возле ``.exe``). Стоило перенести программу в другую папку или удалить старую
версию — и кэш исчезал вместе с ней. Программа честно считала, что перевода
не было, и заново гоняла 40-минутный прогон по всем строкам.

Но перевод никуда не девался: он лежит **в игре**. В режиме ``tl`` это
``game/tl/<язык>/*.rpy``::

    translate russian start_a1b2c3:
        # e "Good morning."
        e "Доброе утро."

и ``translate <язык> strings:`` с парами ``old "…"`` / ``new "…"``.
В режиме прямой замены русский текст стоит прямо в ``.rpy`` — такие строки
переводчик и так узнаёт («уже по-русски») и не трогает.

Модуль вычитывает эти пары и отдаёт словарь «оригинал → готовый перевод».
Дальше пайплайн подставляет их вместо машинного перевода: ни одной лишней
секунды ожидания и никакой подмены готового текста черновиком.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Optional

from config import TL_DIRECTORY
from core.literary import LITERARY_FILENAME
from core.scanner import RenpyProject
from core.utils import get_logger

logger = get_logger(__name__)

#: Строка ``translate russian start_a1b2c3:``
_BLOCK_RE = re.compile(r"^translate\s+(?P<lang>[A-Za-z_]\w*)\s+(?P<id>\w+)\s*:\s*$")

#: Комментарий с оригиналом внутри блока: ``    # e "Good morning."``
_COMMENT_RE = re.compile(
    r'^\s*#\s*(?:[A-Za-z_][\w.]*\s+)*"(?P<text>(?:\\.|[^"\\])*)"')

#: Реплика внутри блока: ``    e "Доброе утро."`` / ``    "Доброе утро."``
_SAY_RE = re.compile(
    r'^\s+(?:[A-Za-z_][\w.]*\s+)*"(?P<text>(?:\\.|[^"\\])*)"')

#: Строки интерфейса: ``    old "Start"`` / ``    new "Начать"``
_OLD_RE = re.compile(r'^\s*old\s+"(?P<text>(?:\\.|[^"\\])*)"\s*$')
_NEW_RE = re.compile(r'^\s*new\s+"(?P<text>(?:\\.|[^"\\])*)"\s*$')


def _unescape(text: str) -> str:
    """Снять экранирование строкового литерала Ren'Py."""
    return (text.replace("\\\\", "\0")
                .replace('\\"', '"')
                .replace("\\n", "\n")
                .replace("\\t", "\t")
                .replace("\\'", "'")
                .replace("\0", "\\"))


def _pairs_from_tl_file(path: Path) -> dict[str, str]:
    """Вычитать пары «оригинал → перевод» из одного tl-файла."""
    pairs: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as exc:
        logger.warning("tl-файл не прочитан (%s): %s", path.name, exc)
        return pairs

    in_block = False
    original = ""
    pending_old: Optional[str] = None

    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue

        block = _BLOCK_RE.match(stripped)
        if block:
            # ``translate russian strings:`` — это не блок диалога, а пары
            # old/new, их читаем веткой ниже
            in_block = block.group("id") != "strings"
            original = ""
            continue

        if not line[:1].isspace() and not stripped.startswith("#"):
            # вышли на верхний уровень (новый оператор) — блок закончился
            in_block = False

        if not in_block:
            old_match = _OLD_RE.match(line)
            if old_match:
                pending_old = _unescape(old_match.group("text"))
                continue
            new_match = _NEW_RE.match(line)
            if new_match and pending_old is not None:
                value = _unescape(new_match.group("text"))
                if value.strip() and value.strip() != pending_old.strip():
                    pairs[pending_old] = value
                pending_old = None
            continue

        comment = _COMMENT_RE.match(line)
        if comment:
            original = _unescape(comment.group("text"))
            continue
        say = _SAY_RE.match(line)
        if say and original:
            value = _unescape(say.group("text"))
            if value.strip() and value.strip() != original.strip():
                pairs[original] = value
            original = ""
    return pairs


def _pairs_from_literary(path: Path) -> dict[str, str]:
    """Пары из ``rt_literary.json`` (тот же формат, что у LiteraryStore)."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    raw = data.get("strings", data) if isinstance(data, dict) else {}
    if not isinstance(raw, dict):
        return {}
    return {original: translation for original, translation in raw.items()
            if isinstance(original, str) and isinstance(translation, str)
            and translation.strip()}


def collect_game_translations(project: RenpyProject,
                              language: str) -> dict[str, str]:
    """
    Собрать перевод, который уже лежит в игре.

    Возвращает словарь «оригинал (EN) → готовый русский текст». Пустой
    словарь означает, что в игре перевода нет и переводить надо с нуля.
    """
    pairs: dict[str, str] = {}
    if project is None:
        return pairs

    tl_dir = Path(project.game_dir) / TL_DIRECTORY / language
    if tl_dir.is_dir():
        for path in sorted(tl_dir.rglob("*.rpy")):
            pairs.update(_pairs_from_tl_file(path))

    # литературный перевод может лежать и в другом месте под game/
    try:
        candidates = list(Path(project.game_dir).rglob(LITERARY_FILENAME))
    except OSError:
        candidates = []
    for path in candidates:
        pairs.update(_pairs_from_literary(path))

    if pairs:
        logger.info("Готовый перевод найден в игре: %d строк (%s)",
                    len(pairs), tl_dir.name or language)
    return pairs
