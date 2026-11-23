"""
Диагностика меню и интерфейса: почему надписи в игре остаются английскими.

Ren'Py переводит надпись экрана, только если она помечена как переводимая
(``_("Start")``) **и** для неё есть строка в ``translate <язык> strings``.
Если игра рисует кнопки картинками (``imagebutton``/``imagemap``) или
задаёт язык жёстко (``config.language = "english"``), наш файл строк ничего
не изменит — и это надо показать пользователю честно, с числами.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from core.engine_strings import (ENGINE_RPY_NAME, ENGINE_STRINGS,
                                LAYOUT_PROMPTS, existing_originals,
                                has_compiled_tl)
from core.menu_patch import IMAGEBUTTON_RE, ACTION_RE, guess_label
from core.screen_patcher import needs_wrapping
from core.scanner import RenpyProject
from core.utils import get_logger

logger = get_logger(__name__)

#: Надпись, обёрнутая в _("...") — переводимая.
WRAPPED_RE = re.compile(r"""_\(\s*(?P<q>["'])(?P<text>.+?)(?P=q)\s*\)""")
#: Голая строка в текстовом элементе экрана.
PLAIN_RE = re.compile(
    r"""^\s*(?:textbutton|text|label|text_?(?:button)?)\s+(?P<q>["'])(?P<text>.+?)(?P=q)""",
    re.M)
#: Кнопки-картинки: переводить нечего, текст нарисован в изображении.
IMAGE_RE = re.compile(r"^\s*(?:imagebutton|imagemap|hotspot)\b", re.M)
#: Экран объявлен.
SCREEN_RE = re.compile(r"^screen\s+([A-Za-z_]\w*)", re.M)
#: Жёсткая установка языка игры.
FORCE_LANG_RE = re.compile(
    r"""config\.(?:language|default_language)\s*=\s*["']([\w\-]+)["']""")


@dataclass
class UIScreen:
    """Один экран игры и что в нём можно перевести."""

    name: str
    file: str
    wrapped: list[str] = field(default_factory=list)
    plain: list[str] = field(default_factory=list)
    images: int = 0


@dataclass
class UICheckReport:
    """Итог проверки интерфейса."""

    screens: list[UIScreen] = field(default_factory=list)
    images_total: int = 0
    #: сколько надписей без _() можно обернуть автоматически
    wrappable: int = 0
    #: сколько «надписей без _()» переводить нечего (пустые, "[x]", "{u}")
    plain_dead: int = 0
    #: сколько кнопок-картинок программа умеет заменить текстом
    images_translatable: int = 0
    forced_language: list[tuple[str, str]] = field(default_factory=list)
    own_translations: list[str] = field(default_factory=list)
    strings_file: Optional[str] = None
    strings_pairs: int = 0
    #: файл перевода строк движка (окна подтверждения и т.п.)
    engine_file: Optional[str] = None
    engine_prompts: int = 0
    #: почему строки движка могут не работать
    engine_note: str = ""
    #: синтаксис файлов игры (строки-блоки без двоеточия)
    syntax_note: str = ""
    syntax_issues: list[str] = None  # type: ignore[assignment]
    #: последние записи журнала изменений
    journal: list[str] = None  # type: ignore[assignment]
    warnings: list[str] = field(default_factory=list)

    # ------------------------------------------------------------------
    @property
    def wrapped(self) -> int:
        return sum(len(screen.wrapped) for screen in self.screens)

    @property
    def plain(self) -> int:
        return sum(len(screen.plain) for screen in self.screens)

    def summary(self) -> str:
        return (f"интерфейс: переводимых надписей {self.wrapped}, "
                f"без пометки _() {self.plain}, кнопок-картинок "
                f"{self.images_total}, строк в нашем файле {self.strings_pairs}")

    def lines(self) -> list[str]:
        """Человекочитаемый отчёт для журнала и консоли."""
        out = ["Проверка меню и интерфейса:"]
        out.append(f"  • надписей в экранах, помеченных _() — {self.wrapped}")
        if self.plain:
            out.append(f"  • надписей без пометки _() — {self.plain}: "
                       "Ren'Py их не переводит вообще (ни один tl-файл)")
            if self.wrappable:
                out.append(f"      — из них {self.wrappable} содержат текст "
                           f"и оборачиваются автоматически "
                           f"(кнопка «Починить меню»)")
            if self.plain_dead:
                out.append(f"      — остальные {self.plain_dead} переводить "
                           f"нечего: пустые или собраны из подстановок "
                           f"([page], {{u}}…{{/u}}, [config.version])")
        if self.images_total:
            out.append(f"  • кнопок-картинок (imagebutton/imagemap) — "
                       f"{self.images_total}: текст на них нарисован в "
                       "изображении, перевод строк его не меняет")
            if self.images_translatable:
                out.append(f"      — из них {self.images_translatable} "
                           f"программа умеет заменить текстовыми кнопками "
                           f"с русской подписью (кнопка «Починить меню»)")
        out.append(f"  • синтаксис файлов игры: {self.syntax_note}")
        for problem in (self.syntax_issues or []):
            out.append(f"  ! {problem} — добавьте ':' в конце строки "
                       f"(или нажмите «Починить меню»)")
        for entry in (self.journal or []):
            out.append(f"  • журнал изменений: {entry}")
        if self.engine_file:
            out.append(f"  • строки движка (окна «выйти?», «в главное "
                       f"меню?», «удалить сохранение?»): {self.engine_file} "
                       f"— {self.engine_prompts} строк")
            if self.engine_note:
                out.append(f"      {self.engine_note}")
        else:
            out.append("  • строки движка (окна подтверждения) — "
                       f"{self.engine_note or 'файла нет'}")
        if self.strings_file:
            out.append(f"  • наш файл строк: {self.strings_file} "
                       f"(строк old/new: {self.strings_pairs})")
        else:
            out.append("  • файла tl/<язык>/zz_renpy_translator_strings.rpy нет — "
                       "сначала запустите перевод")
        for name, value in self.forced_language:
            out.append(f"  • игра жёстко задаёт язык: {name} = \"{value}\" "
                       f"({', '.join(self.forced_language_names)})")
        if self.own_translations:
            out.append("  • в игре уже есть свои переводы: "
                       + ", ".join(self.own_translations[:6]))
        for warning in self.warnings:
            out.append(f"  ! {warning}")

        if self.wrappable or self.images_translatable:
            out.append("  Что делать: нажмите «Починить меню» — программа "
                       "заменит кнопки-картинки текстовыми с русской "
                       "подписью и обернёт строки экранов в _(). Оригинал "
                       "screens.rpy сохраняется в резервной копии.")
        if self.plain and self.wrapped:
            out.append("  Вывод: часть меню переводится (есть _()), часть — нет.")
        elif self.plain and not self.wrapped:
            out.append("  Вывод: меню собрано из строк без _() — наш файл строк "
                       "его не изменит.")
        elif self.images_total and not self.wrapped:
            out.append("  Вывод: меню нарисовано картинками — переводить "
                       "строки нечего.")
        elif self.wrapped:
            out.append("  Вывод: надписи переводимые. Если в игре они всё равно "
                       "английские — проверьте, что язык включён именно "
                       "«russian» (кнопка языка в меню или config.language).")
        return out

    @property
    def forced_language_names(self) -> list[str]:
        return [path for path, _ in self.forced_language]


# ----------------------------------------------------------------------
def _split_screens(text: str) -> list[tuple[str, str]]:
    """Разбить файл на куски «экран → его текст»."""
    matches = list(SCREEN_RE.finditer(text))
    chunks: list[tuple[str, str]] = []
    for index, match in enumerate(matches):
        start = match.start()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        chunks.append((match.group(1), text[start:end]))
    return chunks


def check_ui(project: RenpyProject, language: str = "russian") -> UICheckReport:
    """Собрать диагностику по экранам игры и файлу строк."""
    report = UICheckReport()
    screen_files = [p for p in project.rpy_files
                    if p.name in ("screens.rpy", "gui.rpy")
                    or "screen" in p.name.lower()]

    for path in screen_files:
        relative = project.relative(path)
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue

        for name, chunk in _split_screens(text):
            screen = UIScreen(name=name, file=relative)
            for match in WRAPPED_RE.finditer(chunk):
                screen.wrapped.append(match.group("text"))
            for match in PLAIN_RE.finditer(chunk):
                line = match.group(0)
                if "_(" in line:
                    continue
                text = match.group("text")
                screen.plain.append(text)
                if needs_wrapping(text):
                    report.wrappable += 1
                else:
                    report.plain_dead += 1
            screen.images = len(IMAGE_RE.findall(chunk))
            report.images_total += screen.images

            # Кнопки-картинки, которым программа знает русскую подпись.
            for line in chunk.splitlines():
                if not line.lstrip().startswith("imagebutton"):
                    continue
                match = IMAGEBUTTON_RE.match(line)
                if not match:
                    continue
                image = (match.group("auto") or match.group("idle")
                         or match.group("plain") or "")
                action_match = ACTION_RE.search(match.group("rest") or "")
                action = action_match.group("action") if action_match else ""
                if guess_label(image, action):
                    report.images_translatable += 1
            report.screens.append(screen)

    # жёсткая установка языка и свои переводы
    for path in project.rpy_files:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        relative = project.relative(path)
        for match in FORCE_LANG_RE.finditer(text):
            report.forced_language.append((relative, match.group(1)))
        for match in re.finditer(r"^translate\s+([\w\-]+)\s+strings", text, re.M):
            lang = match.group(1)
            if lang not in report.own_translations:
                report.own_translations.append(f"{lang} ({relative})")

    # наш файл строк
    strings_path = project.game_dir / "tl" / language / \
        "zz_renpy_translator_strings.rpy"
    if strings_path.is_file():
        report.strings_file = str(strings_path.relative_to(project.game_dir))
        try:
            content = strings_path.read_text(encoding="utf-8", errors="replace")
            report.strings_pairs = len(re.findall(r"^\s*old\s+", content, re.M))
        except OSError:
            report.strings_pairs = 0
    else:
        report.warnings.append(
            f"нет файла {strings_path.relative_to(project.game_dir)} — "
            "запустите перевод, он создаётся автоматически")

    # синтаксис: не осталось ли строк без двоеточия после наших правок
    try:
        from core.syntax_guard import check_project as _check_syntax

        syntax = _check_syntax(project, repair=False)
        report.syntax_note = syntax.summary()
        report.syntax_issues = [f"{issue.where(project)}: {issue.text.strip()}"
                                for issue in syntax.issues[:10]]
    except Exception as exc:  # noqa: BLE001
        report.syntax_note = f"проверить не удалось: {exc}"

    # журнал изменений файлов игры — видно, кто и когда правил
    try:
        from core.changelog import read as _read_journal

        journal = _read_journal(project, language, limit=5)
        report.journal = [f"{entry.get('time')} {entry.get('action')} "
                          f"{entry.get('path') or ''}".strip()
                          for entry in journal]
    except Exception:  # noqa: BLE001
        report.journal = []

    # строки движка: окна «выйти?», «в главное меню?» и т.п.
    engine_path = project.game_dir / "tl" / language / ENGINE_RPY_NAME
    if engine_path.is_file():
        report.engine_file = engine_path.relative_to(project.game_dir).as_posix()
        report.engine_prompts = len(LAYOUT_PROMPTS) + len(ENGINE_STRINGS)
        if has_compiled_tl(project, language):
            report.engine_note = (
                "в tl/" + language + " есть только .rpyc — блок строк движка "
                "не создаётся (иначе дубликат уронил бы игру)")
        else:
            already = existing_originals(project, language)
            if already:
                report.engine_note = (f"у игры уже есть {len(already)} "
                                      f"переведённых строк движка — они "
                                      f"главнее наших")
    else:
        report.engine_note = ("файла нет — нажмите «Починить меню» или "
                              "«Сделать всё автоматически»")

    return report
