"""
Работа с экранами игры: где строится меню «Язык» и как добавить туда пункт.

Ren'Py **не** наполняет меню «Язык» из каталога ``tl/``: кнопки языков
прописаны руками в ``game/screens.rpy`` (чаще всего в ``screen preferences``)
примерно так::

    vbox:
        textbutton "English" text_font "DejaVuSans.ttf" action Language(None)
        textbutton "简体中文" text_font "DejaVuSans.ttf" action Language("simplified_chinese")

Поэтому у большинства чужих новелл русского пункта нет вообще.

Модуль умеет две вещи:

1. **Найти** такие блоки: файл, имя экрана, номер строки-образца, отступ,
   использованный шрифт (``find_language_buttons``). Это нужно, чтобы
   понимать, есть ли у игры своё меню языков и похоже ли оно на наше.
2. **Аккуратно дописать** в него кнопку нашего языка, сохранив отступ,
   шрифт и переводы строк файла (``patch_language_menus``). Правка делается
   только по явному требованию пользователя: перед изменением оригинал
   сохраняется в ``game/tl/<язык>/rt_backup/``.

Если править чужой файл нельзя или страшно (``grid`` с фиксированным числом
ячеек, только ``.rpyc`` без исходника) — используется второй, полностью
безопасный путь: собственная кнопка-переключатель на экране
(``button_screen_code``), которая не трогает файлы игры вообще.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional

from config import GENERATED_MARKER, GENERATED_PREFIX, TL_DIRECTORY, VERSION
from core.scanner import RenpyProject
from core.utils import get_logger

logger = get_logger(__name__)

#: Строка с действием переключения языка.
_LANGUAGE_ACTION = re.compile(r"Language\s*\(\s*(None|\"[^\"]*\"|'[^']*')\s*\)")
#: Начало объявления экрана: ``screen preferences():``
_SCREEN_DECL = re.compile(r"^(\s*)screen\s+([A-Za-z_]\w*)")
#: Шрифт, которым нарисованы соседние кнопки.
_TEXT_FONT = re.compile(r"text_font\s+\"([^\"]+)\"")
#: ``grid 3 3`` — фиксированная сетка, лишний ребёнок ломает запуск.
_GRID = re.compile(r"^(\s*)grid\s+(\d+)\s+(\d+)")

#: Экраны, в которых обычно живёт переключатель языка (в порядке приоритета).
_PREFERRED_SCREENS = ("preferences", "settings", "language", "options")

#: Имя собственного экрана-переключателя.
BUTTON_SCREEN_NAME = "rt_language_toggle"
#: Файл, в котором он объявлен.
BUTTON_SCREEN_FILE = f"{GENERATED_PREFIX}_language_button.rpy"
#: Каталог для резервных копий изменённых файлов игры.
BACKUP_DIRNAME = "rt_backup"


@dataclass
class LanguageButton:
    """Одна найденная кнопка языка в экране игры."""

    path: Path
    screen: str
    line_no: int = 0
    """Номер строки (1-based) с ``action Language(...)``."""
    indent: str = "    "
    font: Optional[str] = None
    text: str = ""
    """Исходная строка целиком (без перевода строки)."""

    def where(self) -> str:
        try:
            rel = self.path.name
        except Exception:  # pragma: no cover - защита от экзотики
            rel = str(self.path)
        return f"{rel}:{self.line_no} ({self.screen})"


def _iter_script_files(project: RenpyProject) -> Iterable[Path]:
    """Все .rpy проекта, кроме самого каталога ``tl/`` и движка."""
    skip = {TL_DIRECTORY.lower(), "renpy", "cache", "saves", "libs",
            "python-packages", BACKUP_DIRNAME}
    for path in project.rpy_files:
        parts = [p.lower() for p in path.parts]
        if any(part in skip for part in parts):
            continue
        yield path


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def find_language_buttons(project: RenpyProject) -> list[LanguageButton]:
    """Найти все строки ``action Language(...)`` внутри объявлений экранов."""
    found: list[LanguageButton] = []

    for path in _iter_script_files(project):
        try:
            text = _read(path)
        except OSError:
            continue
        if "Language(" not in text:
            continue

        screen = ""
        for number, line in enumerate(text.splitlines(), start=1):
            decl = _SCREEN_DECL.match(line)
            if decl:
                screen = decl.group(2)
                continue
            if "Language(" not in line:
                continue
            if not _LANGUAGE_ACTION.search(line):
                continue
            stripped = line.lstrip()
            # Нас интересуют только элементы интерфейса, а не python-код.
            if stripped.startswith(("#", "python", "init", "define", "if ")):
                continue
            found.append(LanguageButton(
                path=path,
                screen=screen,
                line_no=number,
                indent=line[:len(line) - len(stripped)],
                font=(_TEXT_FONT.search(line).group(1)
                      if _TEXT_FONT.search(line) else None),
                text=line.rstrip("\r\n"),
            ))

    return found


def pick_button(found: list[LanguageButton]) -> Optional[LanguageButton]:
    """Выбрать блок, куда логичнее вписать нашу кнопку.

    Приоритет: экран ``preferences`` → другой «настроечный» экран → первый
    найденный. Внутри блока предпочитаем строку с ``Language(None)``
    (пункт «English»): наш пункт встанет сразу за ним.
    """
    if not found:
        return None

    def screen_rank(button: LanguageButton) -> int:
        name = button.screen.lower()
        for index, preferred in enumerate(_PREFERRED_SCREENS):
            if name == preferred:
                return index
        return len(_PREFERRED_SCREENS)

    best_screen = min(screen_rank(b) for b in found)
    candidates = [b for b in found if screen_rank(b) == best_screen]

    for button in candidates:
        if "Language(None)" in button.text.replace(" ", ""):
            return button
    return candidates[0]


def _grid_conflict(lines: list[str], anchor_index: int) -> Optional[str]:
    """Есть ли над кнопкой ``grid N M`` с фиксированным числом ячеек."""
    indent = len(lines[anchor_index]) - len(lines[anchor_index].lstrip())
    for index in range(anchor_index, -1, -1):
        line = lines[index]
        if not line.strip() or line.strip().startswith("#"):
            continue
        current = len(line) - len(line.lstrip())
        if current < indent:
            match = _GRID.match(line)
            if match:
                return line.strip()
            indent = current
            if current == 0:
                break
    return None


def _has_language(path: Path, language: str) -> bool:
    text = _read(path)
    return bool(re.search(
        r"Language\s*\(\s*[\"']" + re.escape(language) + r"[\"']", text))


def _backup(path: Path, project: RenpyProject, language: str) -> Path:
    """Сохранить оригинал файла рядом с переводом (не .rpy — Ren'Py его не загрузит)."""
    target = (project.game_dir / TL_DIRECTORY / language / BACKUP_DIRNAME /
              (path.name + ".bak"))
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(path.read_bytes())
    return target


def _insert(path: Path, anchor: LanguageButton, new_line: str) -> None:
    """Вставить строку после ``anchor.line_no``, сохранив переводы строк файла."""
    raw = path.read_bytes()
    crlf = b"\r\n" in raw
    text = raw.decode("utf-8", errors="replace")
    lines = text.split("\n")
    index = anchor.line_no  # строка 1 → индекс 1 (вставляем сразу после)
    if index > len(lines):
        index = len(lines)
    lines.insert(index, new_line.rstrip("\r\n") + ("\r" if crlf else ""))
    path.write_bytes("\n".join(lines).encode("utf-8"))


def _ensure_font(path: Path, language: str, font: str) -> tuple[bool, str]:
    """
    Уже вписанному пункту языка добавить ``text_font`` с кириллицей.

    Нужно, если пункт был добавлен раньше (версия 3.3), а надпись рисовалась
    квадратиками: у кнопок языка в игре может стоять шрифт вообще без русских
    букв. ``text_font`` перебивает стиль и решает проблему наверняка.
    """
    raw = path.read_bytes()
    crlf = b"\r\n" in raw
    text = raw.decode("utf-8", errors="surrogateescape")
    lines = text.split("\n")
    pattern = re.compile(r"Language\s*\(\s*[\"']" + re.escape(language) + r"[\"']\s*\)")

    for index, line in enumerate(lines):
        if "text_font" in line or not pattern.search(line):
            continue
        if "action" not in line:
            continue
        lines[index] = line.replace(
            "action", f'text_font "{font}" action', 1)
        path.write_bytes("\n".join(lines).encode("utf-8", errors="surrogateescape"))
        return True, (f"в {path.name} пункту «{language}» задан шрифт с "
                      f"кириллицей ({font}) — квадратиков больше не будет")
    return False, ""


def add_button_to_screen(project: RenpyProject, button: LanguageButton,
                         language: str, label: str,
                         font_override: Optional[str] = None) -> tuple[bool, str]:
    """Дописать кнопку языка в найденный блок. Возвращает (изменено, сообщение)."""
    if _has_language(button.path, language):
        if font_override:
            fixed, message = _ensure_font(button.path, language, font_override)
            if fixed:
                return True, message
        return False, (f"пункт «{language}» уже есть в {button.path.name} "
                       f"({button.screen}) — файл не трогаю")

    lines = _read(button.path).splitlines()
    conflict = _grid_conflict(lines, button.line_no - 1)
    if conflict:
        return False, (f"в {button.path.name}:{button.line_no} меню языков "
                       f"собрано через «{conflict}» — лишняя кнопка сломает "
                       "запуск, вписывать не буду")

    parts = [f'textbutton "{label}"']
    # Свой шрифт важнее соседского: надпись обязана иметь кириллицу, даже если
    # у кнопок языка в этой игре стоит декоративный латинский (или чисто
    # иероглифический) шрифт — иначе «Русский» рисуется квадратиками.
    font = font_override or button.font
    if font:
        parts.append(f'text_font "{font}"')
    parts.append(f'action Language("{language}")')
    new_line = button.indent + " ".join(parts)

    try:
        backup = _backup(button.path, project, language)
        _insert(button.path, button, new_line)
    except OSError as error:
        return False, f"не удалось изменить {button.path.name}: {error}"

    return True, (f"в {button.path.name} ({button.screen}, строка "
                  f"{button.line_no}) добавлена кнопка «{label}»; "
                  f"оригинал сохранён в tl/{language}/{BACKUP_DIRNAME}/"
                  f"{backup.name}")


def patch_language_menus(project: RenpyProject, language: str,
                         label: str = "Русский",
                         font_override: Optional[str] = None
                         ) -> tuple[list[str], list[str]]:
    """Вписать пункт языка во все найденные меню игры.

    Возвращает (что сделано, предупреждения).
    """
    done: list[str] = []
    warnings: list[str] = []

    found = find_language_buttons(project)
    if not found:
        warnings.append(
            "меню «Язык» в игре не найдено (нет ни одной кнопки "
            "action Language(...) в .rpy) — вписывать некуда, "
            "оставляю только накладную кнопку-переключатель")
        return done, warnings

    # Один файл может содержать несколько блоков — правим каждый по разу.
    seen: set[tuple[Path, str]] = set()
    for button in found:
        key = (button.path, button.screen)
        if key in seen:
            continue
        seen.add(key)
        changed, message = add_button_to_screen(project, button, language,
                                                label,
                                                font_override=font_override)
        if changed:
            done.append(message)
            logger.info(message)
        else:
            warnings.append(message)
            logger.warning(message)

    return done, warnings


def button_screen_code(language: str, label_native: str = "Русский",
                       label_default: str = "English") -> str:
    """Код собственного экрана-переключателя (не трогает файлы игры).

    Экран добавляется в ``config.overlay_screens`` — он виден только в
    главном меню и в меню игры (настройки/сохранения), поверх остальных
    экранов, и не мешает диалогам.
    """
    return f'''# {GENERATED_MARKER} {VERSION}
# Кнопка переключения языка. Файл сгенерирован автоматически.
# Удалите его, чтобы вернуть исходное поведение игры.
#
# Зачем: Ren'Py не берёт список языков из tl/ — пункты меню прописаны в
# screens.rpy игры вручную, и русского там чаще всего нет. Этот файл
# добавляет свою маленькую кнопку в главное меню и в настройки.

init 1602 python:
    # always_shown_screens показывает экран всегда; сам экран ниже рисует
    # кнопку только в главном меню и меню игры, в диалогах он пустой.
    try:
        if "{BUTTON_SCREEN_NAME}" not in config.always_shown_screens:
            config.always_shown_screens.append("{BUTTON_SCREEN_NAME}")
    except Exception:
        pass


screen {BUTTON_SCREEN_NAME}():
    zorder 200

    if (renpy.get_screen("preferences") or renpy.get_screen("main_menu")
            or renpy.get_screen("game_menu")):

        frame:
            align (0.985, 0.02)
            background "#00000099"
            padding (10, 6)

            hbox:
                spacing 12

                textbutton "{label_native}":
                    action Language("{language}")
                    selected (_preferences.language == "{language}")
                    text_size 18
                    text_idle_color "#ffffffb0"
                    text_hover_color "#ffd479"
                    text_selected_color "#ffd479"

                textbutton "{label_default}":
                    action Language(None)
                    selected (not _preferences.language)
                    text_size 18
                    text_idle_color "#ffffffb0"
                    text_hover_color "#ffd479"
                    text_selected_color "#ffd479"
'''


def button_screen_path(project: RenpyProject, language: str) -> Path:
    return project.game_dir / TL_DIRECTORY / language / BUTTON_SCREEN_FILE


def write_button_screen(project: RenpyProject, language: str,
                        force: bool = False,
                        label_native: str = "Русский") -> Optional[Path]:
    """Записать файл с накладной кнопкой-переключателем."""
    path = button_screen_path(project, language)
    if path.exists() and not force:
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(button_screen_code(language, label_native),
                    encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Обёртка «голых» строк экранов в _()
# ---------------------------------------------------------------------------
#: Строка экрана с текстом, который Ren'Py переводит только через _():
#: ``text "Story by"``, ``textbutton "Continue"``, ``label "Credits"``.
_WRAP_CALL = re.compile(
    r"""^(?P<indent>[ \t]*)(?P<call>text|textbutton|label|imagetext)\s+
        (?P<q>["'])(?P<body>(?:\\.|(?!\3).)*?)(?P=q)""",
    re.VERBOSE)


@dataclass
class WrapReport:
    """Итог обёртки экранных строк в ``_()``."""

    wrapped: int = 0
    files: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    backups: list[str] = field(default_factory=list)
    dry_run: bool = False
    samples: list[str] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return self.wrapped > 0

    def summary(self) -> str:
        if not self.wrapped:
            return "строк без _() не нашлось — экраны переводить нечего"
        prefix = "(проверка без записи) " if self.dry_run else ""
        return (f"{prefix}обёрнуто в _(): {self.wrapped} "
                f"в {len(self.files)} файл(ах)")


#: Экранированные символы, которые не делают строку текстом: ``\n``, ``\t``.
_ESCAPES = re.compile(r"\\(?:n|t|r|\"|'|\\|\[|\]|\{|\})")


def _looks_dynamic(body: str) -> bool:
    """
    Строка без собственного текста: ``"[page]"``, ``"{#auto_page}A"``, ``""``.

    Такие надписи переводить нечего — они целиком собираются из подстановок
    Ren'Py, и обёртка в ``_()`` только запутала бы переводчика.
    """
    stripped = _ESCAPES.sub("", re.sub(r"\[[^\]]*\]|\{[^}]*\}", "", body))
    return not any(char.isalnum() for char in stripped)


def _is_cyrillic_only(body: str) -> bool:
    """Надпись уже по-русски (например, «Русский», вписанный нами в меню)."""
    letters = [char for char in body if char.isalpha()]
    return bool(letters) and all("\u0400" <= char <= "\u04FF" for char in letters)


def needs_wrapping(text: str) -> bool:
    """Есть ли в этой надписи что переводить (не пустая и не из подстановок)."""
    return bool(text.strip()) and not _looks_dynamic(text) \
        and not _is_cyrillic_only(text)


def wrap_plain_strings(project: RenpyProject,
                       files: Optional[Iterable[Path]] = None,
                       dry_run: bool = False,
                       make_backup: bool = True) -> WrapReport:
    """
    Обернуть текстовые литералы экранов в ``_()``.

    Ren'Py переводит только те строки, которые помечены ``_()``. Авторы
    новелл часто пишут в ``screens.rpy`` просто ``text "Story by ..."`` —
    такие строки не попадают ни в один ``tl/`` и остаются английскими, хотя
    весь остальной интерфейс уже переведён.

    Функция не трогает: комментарии, строки с уже стоящим ``_()``/``_p()``,
    строки без букв (``"[page]"``), а также уже кириллические надписи
    (например, «Русский», который мы сами вписали в меню языков).
    """
    report = WrapReport(dry_run=dry_run)
    candidates = list(files) if files is not None else list(_iter_script_files(project))
    backup_done = False

    for path in candidates:
        try:
            raw = path.read_bytes()
        except OSError as exc:
            logger.debug("Не удалось прочитать %s: %s", path, exc)
            continue
        crlf = b"\r\n" in raw
        text = raw.decode("utf-8", errors="surrogateescape")
        lines = text.splitlines(keepends=True)

        changed = False
        for index, line in enumerate(lines):
            stripped = line.lstrip()
            if not stripped or stripped.startswith("#"):
                continue
            match = _WRAP_CALL.match(line)
            if not match:
                continue
            body = match.group("body")
            if not body.strip() or _looks_dynamic(body) or _is_cyrillic_only(body):
                if body.strip():
                    report.skipped.append(f"{path.name}: {body[:40]}")
                continue

            quote = match.group("q")
            head = line[:match.start("q")]
            tail = line[match.end():]
            new_line = f"{head}_({quote}{body}{quote}){tail}"
            lines[index] = new_line
            changed = True
            report.wrapped += 1
            if len(report.samples) < 5:
                report.samples.append(body[:60])

        if not changed:
            continue

        report.files.append(project.relative(path))
        if dry_run:
            continue
        try:
            if make_backup and not backup_done:
                backup = _backup(path, project, "russian")
                report.backups.append(str(backup))
                backup_done = True
            path.write_bytes("".join(lines).replace("\r\n", "\n")
                             .replace("\n", "\r\n" if crlf else "\n")
                             .encode("utf-8", errors="surrogateescape"))
        except OSError as exc:
            logger.warning("Не удалось записать %s: %s", path, exc)

    if report.wrapped:
        logger.info(report.summary())
    return report
