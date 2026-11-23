"""
Строки самого Ren'Py: диалоги подтверждения, быстрые сохранения и прочее.

Почему это отдельная история
----------------------------
Часть интерфейса рисует не игра, а движок: «Are you sure you want to return to
the main menu? This will lose unsaved progress.», «Are you sure you want to
quit?», «Quick save complete.» и т.п. Эти строки лежат в
``renpy/common/00layout.rpy`` и ``00action_file.rpy`` — то есть **внутри
движка**, а не в ``game/``. Наш перевод их не видит: в ``tl/`` игры их нет,
и в отчёте «Проверить меню» они не считаются.

Из-за этого получается обидно: меню по-русски, реплики по-русски, а окно
«вернуться в главное меню?» — по-английски.

Что делает модуль
-----------------
Пишет ``game/tl/<язык>/zz_renpy_translator_engine.rpy``:

* ``translate <язык> strings`` — для тех строк движка, которые обёрнуты
  в ``_()`` (они переводятся штатным механизмом Ren'Py);
* подмену ``layout.*`` для окон подтверждения. Эти строки движок берёт
  из Python-переменных, ``_()`` над ними не вызывается, поэтому штатный
  перевод до них не доходит. Подмена включается **только** когда выбран
  наш язык: в английском режиме остаётся английский текст.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Iterable, Optional

from config import GENERATED_MARKER, GENERATED_PREFIX, TL_DIRECTORY, VERSION
from core.utils import get_logger

logger = get_logger(__name__)

#: ``old "текст"`` в файле перевода.
_OLD_RE = re.compile(r'^\s*old\s+"(?P<text>(?:\\.|[^"\\])*)"', re.M)

#: Имя генерируемого файла.
ENGINE_RPY_NAME = "zz_renpy_translator_engine.rpy"

#: Окна подтверждения: переменная движка → русский текст.
#: Оригиналы — из ``renpy/common/00layout.rpy`` (Ren'Py 8.x).
LAYOUT_PROMPTS: dict[str, tuple[str, str]] = {
    "ARE_YOU_SURE": ("Are you sure?", "Вы уверены?"),
    "DELETE_SAVE": ("Are you sure you want to delete this save?",
                    "Удалить это сохранение?"),
    "OVERWRITE_SAVE": ("Are you sure you want to overwrite your save?",
                       "Перезаписать это сохранение?"),
    "LOADING": ("Loading will lose unsaved progress.\n"
                "Are you sure you want to do this?",
                "Загрузка сбросит несохранённый прогресс.\n"
                "Продолжить?"),
    "QUIT": ("Are you sure you want to quit?", "Выйти из игры?"),
    "MAIN_MENU": ("Are you sure you want to return to the main menu?\n"
                  "This will lose unsaved progress.",
                  "Вернуться в главное меню?\n"
                  "Несохранённый прогресс будет потерян."),
    "CONTINUE": ("Are you sure you want to continue where you left off?",
                 "Продолжить с того места, где вы остановились?"),
    "END_REPLAY": ("Are you sure you want to end the replay?",
                   "Закончить просмотр?"),
    "SLOW_SKIP": ("Are you sure you want to begin skipping?",
                  "Начать пропуск текста?"),
    "FAST_SKIP_SEEN": ("Are you sure you want to skip to the next choice?",
                       "Пропустить до следующего выбора?"),
    "FAST_SKIP_UNSEEN": ("Are you sure you want to skip unseen dialogue to "
                         "the next choice?",
                         "Пропустить невиданные реплики до следующего выбора?"),
}

#: Строки движка, которые он сам оборачивает в ``_()``.
ENGINE_STRINGS: dict[str, str] = {
    "Are you sure?": "Вы уверены?",
    "Are you sure you want to delete this save?": "Удалить это сохранение?",
    "Are you sure you want to overwrite your save?": "Перезаписать это сохранение?",
    "Loading will lose unsaved progress.\nAre you sure you want to do this?":
        "Загрузка сбросит несохранённый прогресс.\nПродолжить?",
    "Are you sure you want to quit?": "Выйти из игры?",
    "Are you sure you want to return to the main menu?\nThis will lose "
    "unsaved progress.":
        "Вернуться в главное меню?\nНесохранённый прогресс будет потерян.",
    "Are you sure you want to continue where you left off?":
        "Продолжить с того места, где вы остановились?",
    "Are you sure you want to end the replay?": "Закончить просмотр?",
    "Are you sure you want to begin skipping?": "Начать пропуск текста?",
    "Are you sure you want to skip to the next choice?":
        "Пропустить до следующего выбора?",
    "Are you sure you want to skip unseen dialogue to the next choice?":
        "Пропустить невиданные реплики до следующего выбора?",
    "Quick save complete.": "Быстрое сохранение готово.",
    "Quick save.": "Быстрое сохранение.",
    "Quick load.": "Быстрая загрузка.",
    "Quick saves": "Быстрые сохранения",
    "Automatic saves": "Автосохранения",
    "Auto forward": "Автопрокрутка",
    "Cancel": "Отмена",
    "Delete slot [text]": "Удалить сохранение [text]",
    "File page [text]": "Страница [text]",
    "File page auto": "Страница автосохранений",
    "File page quick": "Страница быстрых сохранений",
    "Language [text]": "Язык: [text]",
    "Load slot %s: [text]": "Загрузить сохранение %s: [text]",
    "Save slot %s: [text]": "Сохранить в слот %s: [text]",
    "Next file page.": "Следующая страница.",
    "Previous file page.": "Предыдущая страница.",
    "Failed to save screenshot as %s.": "Не удалось сохранить снимок экрана как %s.",
    "Saved screenshot as %s.": "Снимок экрана сохранён как %s.",
    "Image [index] of [count] locked.": "Изображение [index] из [count] закрыто.",
}

#: Языки, для которых у нас есть готовые строки движка.
SUPPORTED_LANGUAGES = ("russian",)


def _quote(text: str) -> str:
    """Строковый литерал для .rpy (кавычки и переводы строк экранируются)."""
    escaped = (text.replace("\\", "\\\\")
                   .replace('"', '\\"')
                   .replace("\n", "\\n"))
    return '"' + escaped + '"'


def _quote_strings(text: str) -> str:
    """
    Литерал для Ren'Py ``strings``: переводы строк — как ``\\n``.

    В ``translate <язык> strings`` строка задаётся через ``old``/``new``,
    переводы строк в них записываются escape-последовательностью.
    """
    escaped = (text.replace("\\", "\\\\")
                   .replace('"', '\\"')
                   .replace("\n", "\\n"))
    return f'"{escaped}"'


def _unescape(text: str) -> str:
    return (text.replace("\\n", "\n").replace("\\t", "\t")
                .replace('\\"', '"').replace("\\\\", "\\"))


def existing_originals(project, language: str) -> set[str]:
    """
    Какие строки движка в этой игре уже переведены.

    Ren'Py **падает**, если одна и та же строка `old "…"` встречается в двух
    разных блоках ``translate <язык> strings`` («A translation for "…" already
    exists»). Поэтому свои строки добавляем только те, которых ещё нет.
    """
    found: set[str] = set()
    directory = project.game_dir / TL_DIRECTORY / language
    if not directory.is_dir():
        return found
    for path in directory.rglob("*.rpy"):
        # Свои же сгенерированные файлы не считаем «чужим переводом»:
        # иначе при пересборке мы бы выкинули собственные строки.
        if path.name.startswith(GENERATED_PREFIX):
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for match in _OLD_RE.finditer(text):
            found.add(_unescape(match.group("text")))
    return found


def has_compiled_tl(project, language: str) -> bool:
    """
    Есть ли в ``tl/<язык>/`` файлы ``.rpyc`` без исходника.

    Их строк мы не видим, а значит не можем гарантировать, что не создадим
    дубликат (который валит игру). В этом случае блок ``strings`` не пишем
    вообще — остаётся безопасная подмена окон подтверждения.
    """
    directory = project.game_dir / TL_DIRECTORY / language
    if not directory.is_dir():
        return False
    for path in directory.rglob("*.rpyc"):
        if not path.with_suffix(".rpy").is_file():
            return True
    return False


def build_engine_rpy(language: str = "russian",
                     skip: Iterable[str] = (),
                     include_strings: bool = True) -> str:
    """Текст ``zz_renpy_translator_engine.rpy`` для одного языка."""
    if language not in SUPPORTED_LANGUAGES:
        language = "russian"

    skipped = set(skip)
    lines: list[str] = [
        f"# {GENERATED_MARKER} {VERSION} — строки движка Ren'Py",
        "# Файл создан автоматически: здесь перевод окон подтверждения",
        "# («вернуться в главное меню?»), быстрых сохранений и служебных",
        "# сообщений. Удалите файл, чтобы вернуть английские подписи.",
        "",
    ]
    pairs = [(original, translation)
             for original, translation in ENGINE_STRINGS.items()
             if original not in skipped]

    if include_strings and pairs:
        lines += [f"translate {language} strings:", ""]
        for original, translation in pairs:
            lines.append(f"    old {_quote_strings(original)}")
            lines.append(f"    new {_quote_strings(translation)}")
            lines.append("")
    else:
        lines += [
            "# Блок strings не создан: у игры уже есть перевод строк движка",
            "# (или он есть только в виде .rpyc) — дубликат уронил бы игру.",
            "",
        ]

    # Окна подтверждения живут в переменных движка (layout.*), а не в _():
    # без этой подмены они так и останутся английскими.
    lines += [
        "",
        "init 999 python:",
        "    # Подменяем только когда включён наш язык: в английском режиме",
        "    # остаётся английский текст.",
        "    _rt_layout_prompts = {",
    ]
    for name, (original, translation) in LAYOUT_PROMPTS.items():
        lines.append(f"        {_quote(name)}: ({_quote(original)}, "
                     f"{_quote(translation)}),")
    lines += [
        "    }",
        "",
        "    def _rt_apply_layout_prompts():",
        "        try:",
        f"            _rt_ru = (_preferences.language == {_quote(language)})",
        "        except Exception:",
        "            return",
        "        for _rt_name, (_rt_en, _rt_ru_text) in "
        "_rt_layout_prompts.items():",
        "            try:",
        "                if not hasattr(layout, _rt_name):",
        "                    continue",
        "                setattr(layout, _rt_name,",
        "                        _rt_ru_text if _rt_ru else _rt_en)",
        "            except Exception:",
        "                pass",
        "",
        "    try:",
        "        _rt_apply_layout_prompts()",
        "        config.interact_callbacks.append(_rt_apply_layout_prompts)",
        "    except Exception:",
        "        pass",
        "",
    ]
    return "\n".join(lines) + "\n"


def write_engine_rpy(project, language: str = "russian",
                     force: bool = False):
    """
    Записать файл строк движка в ``game/tl/<язык>/``.

    Возвращает ``(путь, сколько строк добавлено, что пропущено)``.
    """
    if language not in SUPPORTED_LANGUAGES:
        return None, 0, ""

    directory = project.game_dir / TL_DIRECTORY / language
    existing = existing_originals(project, language)
    include_strings = not has_compiled_tl(project, language)
    if not include_strings:
        logger.info("в tl/%s есть только .rpyc — строки движка не добавляю, "
                    "чтобы не создать дубликат", language)

    pairs = [original for original in ENGINE_STRINGS
             if original not in existing]
    note = ""
    if len(pairs) < len(ENGINE_STRINGS):
        note = (f"уже переведено в игре: {len(ENGINE_STRINGS) - len(pairs)} "
                f"строк движка")

    path = directory / ENGINE_RPY_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    text = build_engine_rpy(language, skip=existing,
                            include_strings=include_strings)
    if path.exists() and not force:
        try:
            if path.read_text(encoding="utf-8") == text:
                return path, len(pairs), note
        except OSError:
            pass
    path.write_text(text, encoding="utf-8")
    return path, len(pairs), note


def engine_strings_count() -> int:
    return len(ENGINE_STRINGS) + len(LAYOUT_PROMPTS)


def iter_engine_pairs() -> Iterable[tuple[str, str]]:
    """Пары «оригинал → русский» — пригодится в отчётах и тестах."""
    for name, (original, translation) in LAYOUT_PROMPTS.items():
        yield name, original
        yield name, translation
    for original, translation in ENGINE_STRINGS.items():
        yield original, translation
