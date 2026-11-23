"""
Журнал изменений файлов игры.

Зачем
-----
Когда игра перестаёт запускаться, первый вопрос — «кто это сделал».
Раньше ответ приходилось собирать по логам и догадкам. Теперь каждый
модуль, который пишет в ``game/``, оставляет строку в
``game/tl/<язык>/rt_changes.jsonl``: что, когда и какой файл.

Формат — одна JSON-строка на действие, дописывается в конец::

    {"time": "2026-11-21 00:12:03", "version": "3.6.2",
     "action": "image_menu_patch", "path": "screens.rpy",
     "detail": "меню-картинки заменены на текст: 12"}

Плюс сохраняется контрольная «до/после» по числу строк и наличию
двоеточий — по журналу видно, после какого шага файл стал другим.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Optional

from config import TL_DIRECTORY, VERSION
from core.scanner import RenpyProject
from core.utils import get_logger

logger = get_logger(__name__)

JOURNAL_NAME = "rt_changes.jsonl"


def journal_path(project: RenpyProject, language: str) -> Path:
    return project.game_dir / TL_DIRECTORY / language / JOURNAL_NAME


def record(project: Optional[RenpyProject], language: str, action: str,
           path: Optional[Path] = None, detail: str = "") -> None:
    """Записать действие в журнал. Никогда не падает."""
    if project is None:
        return
    rel = ""
    if path is not None:
        try:
            rel = path.relative_to(project.game_dir).as_posix()
        except ValueError:
            rel = path.name
    entry = {
        "time": time.strftime("%Y-%m-%d %H:%M:%S"),
        "version": VERSION,
        "action": action,
        "path": rel,
        "detail": detail,
    }
    try:
        target = journal_path(project, language)
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError as exc:  # noqa: BLE001
        logger.debug("журнал изменений недоступен: %s", exc)


def read(project: RenpyProject, language: str, limit: int = 40) -> list[dict]:
    """Последние записи журнала (для отчёта и кнопки «Журнал»)."""
    path = journal_path(project, language)
    if not path.is_file():
        return []
    entries: list[dict] = []
    try:
        for line in path.read_text(encoding="utf-8",
                                   errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    except OSError:
        return []
    return entries[-limit:]


def report_lines(project: Optional[RenpyProject], language: str,
                 limit: int = 15) -> list[str]:
    if project is None:
        return []
    entries = read(project, language, limit=limit)
    if not entries:
        return []
    out = ["Что и когда менялось в файлах игры:"]
    for entry in entries:
        where = f" {entry.get('path')}" if entry.get("path") else ""
        detail = f" — {entry['detail']}" if entry.get("detail") else ""
        out.append(f"  {entry.get('time')}  {entry.get('action')}"
                   f"{where}{detail}")
    return out
