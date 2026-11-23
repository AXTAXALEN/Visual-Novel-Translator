"""
Русификация меню, нарисованных картинками.

Проблема
--------
Некоторые игры рисуют кнопки главного меню изображениями::

    imagebutton auto "gui/menubuttons/start_%s.png" action Start()

Текст «start» в этом случае часть картинки. Ни ``translate russian strings``,
ни правка шрифтов его не изменят — игрок так и видит английские слова, хотя
весь остальной интерфейс уже переведён.

Что делает модуль
-----------------
Находит такие кнопки в ``screens.rpy`` и заменяет их на обычные текстовые::

    textbutton _("Начать") action Start()

Подпись берётся по действию кнопки (``Start()`` → «Начать»), а если действие
незнакомо — по имени файла картинки (``start_%s.png`` → «Начать»). Кнопка
остаётся на своём месте (тот же ``vbox``, тот же ``style_prefix``), поэтому
оформление берётся из стилей игры.

Правка выполняется только по явной команде (``--patch-image-menus`` или
кнопка в интерфейсе), перед записью создаётся резервная копия, а откатить
всё можно кнопкой «Откатить бэкап» (``--restore``).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional

from core.scanner import RenpyProject
from core.utils import get_logger

logger = get_logger(__name__)

#: Строка с кнопкой-картинкой.
IMAGEBUTTON_RE = re.compile(
    r"""(?P<indent>[ \t]*)imagebutton\s+
        (?:
            auto\s+(?P<auto>"[^"]+"|'[^']+')
          | idle\s+(?P<idle>"[^"]+"|'[^']+')
          | (?P<plain>"[^"]+"|'[^']+')
        )
        (?P<rest>[^\n]*)
    """,
    re.VERBOSE)

ACTION_RE = re.compile(r"\baction\s+(?P<action>.*?)\s*(?:##.*)?$")

#: Действие Ren'Py → подпись кнопки.
ACTION_LABELS: tuple[tuple[str, str], ...] = (
    (r"Start\s*\(", "Начать"),
    (r"ShowMenu\s*\(\s*['\"]save", "Сохранить"),
    (r"ShowMenu\s*\(\s*['\"]load", "Загрузить"),
    (r"ShowMenu\s*\(\s*['\"]preferences", "Настройки"),
    (r"ShowMenu\s*\(\s*['\"]about", "Об игре"),
    (r"ShowMenu\s*\(\s*['\"]help", "Справка"),
    (r"ShowMenu\s*\(\s*['\"]history", "История"),
    (r"ShowMenu\s*\(\s*['\"]main_menu", "Главное меню"),
    (r"MainMenu\s*\(", "Главное меню"),
    (r"Quit\s*\(", "Выход"),
    (r"Return\s*\(", "Назад"),
    (r"Language\s*\(", "Язык"),
    (r"ShowMenu\s*\(\s*['\"]language", "Язык"),
    (r"Preference\s*\(", "Настройки"),
    (r"FilePage|QuickSave|FileSave", "Сохранить"),
    (r"QuickLoad|FileLoad", "Загрузить"),
    (r"EndReplay\s*\(", "Закончить повтор"),
)

#: Имя файла картинки → подпись (последний резерв).
FILE_LABELS: tuple[tuple[str, str], ...] = (
    ("mainmenu", "Главное меню"),
    ("main_menu", "Главное меню"),
    ("start", "Начать"),
    ("load", "Загрузить"),
    ("save", "Сохранить"),
    ("preferences", "Настройки"),
    ("options", "Настройки"),
    ("settings", "Настройки"),
    ("about", "Об игре"),
    ("help", "Справка"),
    ("history", "История"),
    ("quit", "Выход"),
    ("exit", "Выход"),
    ("replay", "Повтор"),
)


@dataclass
class MenuPatchResult:
    """Что заменено и что осталось как было."""

    file: Optional[Path] = None
    replaced: int = 0
    skipped: list[str] = field(default_factory=list)
    details: list[str] = field(default_factory=list)
    backup: Optional[Path] = None
    changed: bool = False
    dry_run: bool = False
    #: сколько строк, испорченных 3.4.0, починено
    repaired: int = 0

    def journal(self, project: RenpyProject, language: str) -> None:
        """Отметить правку в журнале изменений игры."""
        try:
            from core.changelog import record

            record(project, language,
                   "image_menu_patch" if self.changed else "image_menu_check",
                   self.file, self.summary())
        except Exception:  # noqa: BLE001
            pass

    def summary(self) -> str:
        if not self.changed:
            if self.repaired:
                return (f"починены кнопки меню без слова action: "
                        f"{self.repaired} (игра снова запустится)")
            return "меню-картинок не найдено — менять нечего"
        prefix = "(проверка без записи) " if self.dry_run else ""
        text = f"{prefix}меню-картинки заменены на текст: {self.replaced}"
        if self.repaired:
            text = (f"починены кнопки без action: {self.repaired}; " + text)
        if self.backup:
            text += f", резервная копия: {self.backup.name}"
        return text


def _label_from_action(action: str) -> Optional[str]:
    for pattern, label in ACTION_LABELS:
        if re.search(pattern, action):
            return label
    return None


def _label_from_file(image: str) -> Optional[str]:
    name = image.strip("\"'").rsplit("/", 1)[-1].lower()
    name = name.replace("_%s", "").replace("%s", "")
    for key, label in FILE_LABELS:
        if key in name:
            return label
    return None


def guess_label(image: str, action: str) -> Optional[str]:
    """Русская подпись для кнопки-картинки (или None, если не угадали)."""
    return _label_from_action(action or "") or _label_from_file(image or "")


def _screen_files(project: RenpyProject) -> list[Path]:
    return [path for path in project.rpy_files
            if path.name == "screens.rpy" or "screen" in path.name.lower()]


MARKER = "## было: imagebutton"

#: Наша же замена, но без ключевого слова ``action`` (ошибка версии 3.4.0):
#: ``textbutton _("Начать") Start()  ## было: imagebutton start_%s.png``
BROKEN_RE = re.compile(
    r"^(?P<indent>[ \t]*)textbutton\s+_\(\s*(?P<q>[\"'])(?P<label>.*?)(?P=q)\s*\)"
    r"\s+(?!action\b)(?P<rest>\S.*?)(?P<marker>\s+##\s*было:\s*imagebutton.*)$")


def repair_broken_textbuttons(project: RenpyProject,
                              files: Optional[Iterable[Path]] = None
                              ) -> tuple[int, list[str]]:
    """
    Починить строки, испорченные версией 3.4.0.

    В 3.4.0 кнопка меню заменялась на ``textbutton _("Начать") Start()`` —
    без слова ``action``, из-за чего игра не запускалась. Здесь такие строки
    (их видно по хвосту ``## было: imagebutton``) получают ``action`` обратно.
    """
    fixed = 0
    details: list[str] = []
    candidates = list(files) if files is not None else list(_screen_files(project))

    for path in candidates:
        try:
            raw = path.read_bytes()
        except OSError:
            continue
        text = raw.decode("utf-8", errors="surrogateescape")
        lines = text.splitlines(keepends=True)
        changed = False

        for index, line in enumerate(lines):
            match = BROKEN_RE.match(line.rstrip("\r\n"))
            if not match:
                continue
            ending = "\r\n" if line.endswith("\r\n") else (
                "\n" if line.endswith("\n") else "")
            lines[index] = (f"{match.group('indent')}textbutton _("
                            f"{match.group('q')}{match.group('label')}"
                            f"{match.group('q')}) action {match.group('rest')}"
                            f"{match.group('marker')}{ending}")
            fixed += 1
            changed = True
            details.append(f"{project.relative(path)}: «{match.group('label')}»")

        if changed:
            path.write_bytes("".join(lines).encode("utf-8",
                                                  errors="surrogateescape"))

    if fixed:
        logger.info("починены кнопки меню без слова action: %d", fixed)
    return fixed, details


def patch_image_menus(project: RenpyProject,
                      files: Optional[Iterable[Path]] = None,
                      dry_run: bool = False,
                      make_backup: bool = True) -> MenuPatchResult:
    """Заменить кнопки-картинки меню на текстовые кнопки с русской подписью."""
    result = MenuPatchResult(dry_run=dry_run)
    candidates = list(files) if files is not None else _screen_files(project)

    # Сначала лечим то, что могла испортить версия 3.4.0 (нет слова action),
    # иначе игра не запустится и правка меню уже не поможет.
    if not dry_run:
        fixed, fixed_details = repair_broken_textbuttons(project, candidates)
        result.repaired = fixed
        result.details.extend(fixed_details)

    changed_files: list[Path] = []
    for path in candidates:
        try:
            raw = path.read_bytes()
        except OSError as exc:
            logger.debug("Не удалось прочитать %s: %s", path, exc)
            continue
        # BOM и переводы строк (CRLF) сохраняем байт в байт.
        text = raw.decode("utf-8", errors="surrogateescape")

        new_lines: list[str] = []
        file_changes = 0
        for line in text.splitlines(keepends=True):
            stripped = line.lstrip()
            if not stripped.startswith("imagebutton"):
                new_lines.append(line)
                continue

            match = IMAGEBUTTON_RE.match(line)
            if not match:
                new_lines.append(line)
                continue

            image = (match.group("auto") or match.group("idle")
                     or match.group("plain") or "").strip("\"'")
            rest = match.group("rest") or ""
            action_match = ACTION_RE.search(rest)
            action = action_match.group("action").strip() if action_match else ""
            action = action.rstrip()

            label = _label_from_action(action) or _label_from_file(image)
            if not label:
                result.skipped.append(f"{image} ({action or 'без action'})")
                new_lines.append(line)
                continue

            comment = f"  ## было: imagebutton {image}"
            newline = "\r\n" if line.endswith("\r\n") else "\n"
            # Ключевое слово `action` обязательно: без него Ren'Py падает с
            # «'Start' is not a keyword argument ... of the textbutton statement».
            replacement = (f"{match.group('indent')}textbutton _(\"{label}\")"
                           f" action {action}{comment}{newline}")
            new_lines.append(replacement)
            file_changes += 1
            result.details.append(
                f"{project.relative(path)}: {image} → «{label}»")

        if file_changes:
            changed_files.append(path)
            result.replaced += file_changes
            if not dry_run:
                if make_backup and result.backup is None:
                    result.backup = _make_backup(project)
                path.write_bytes("".join(new_lines).encode(
                    "utf-8", errors="surrogateescape"))

    if candidates and result.replaced:
        result.file = changed_files[0] if changed_files else None
        result.changed = True
        if not dry_run:
            logger.info("%s", result.summary())

    return result


def _make_backup(project: RenpyProject) -> Optional[Path]:
    try:
        from core.backup import create_backup

        backup = create_backup(project, include_tl=False)
        return backup.path
    except Exception as exc:  # noqa: BLE001
        logger.warning("Резервную копию создать не удалось: %s", exc)
        return None
