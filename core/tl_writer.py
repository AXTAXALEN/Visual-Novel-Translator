"""
Генерация штатных файлов перевода Ren'Py: ``game/tl/<язык>/``.

Формат ровно такой, какой создаёт сама Ren'Py-команда ``translate``
(см. ``renpy/translation/generation.py::write_translates``)::

    # game/script.rpy:42
    translate russian start_1a2b3c4d:

        # e "Hello"

        e "Привет"

Плюс отдельный ``strings``-файл для UI::

    translate russian strings:

        old "Start"
        new "Начать"

Такая структура подхватывается движком автоматически: язык появляется в
настройках игры, а реплики подменяются по идентификатору блока.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional

from config import APP_NAME, GENERATED_MARKER, GENERATED_PREFIX, TL_DIRECTORY, VERSION
from core.models import (
    BLOCK_KINDS,
    KIND_MENU,
    KIND_NAME,
    KIND_UI,
    TextItem,
)
from core.placeholder import escape_for_renpy
from core.rpy_parser import encode_say_string, quote_unicode
from core.scanner import RenpyProject
from core.utils import atomic_write_text, get_logger, remove_file

logger = get_logger(__name__)

#: `old "текст"` в чужих файлах перевода.
_OLD_RE = re.compile(r'^\s+old\s+"((?:[^"\\]|\\.)*)"\s*$', re.MULTILINE)


def _unquote_renpy(value: str) -> str:
    """Снимает экранирование quote_unicode (для сравнения с нашими строками)."""
    out = []
    i = 0
    while i < len(value):
        if value[i] == "\\" and i + 1 < len(value):
            nxt = value[i + 1]
            mapping = {"n": "\n", "t": "\t", "r": "\r", "a": "\a",
                       "b": "\b", "f": "\f", "v": "\v"}
            out.append(mapping.get(nxt, nxt))
            i += 2
        else:
            out.append(value[i])
            i += 1
    return "".join(out)


@dataclass
class TLBuildResult:
    language: str = ""
    tl_dir: Optional[Path] = None
    files_written: list[Path] = field(default_factory=list)
    blocks: int = 0
    strings: int = 0
    skipped_items: int = 0
    skipped_existing: int = 0
    """Столько блоков уже переведено в чужих tl-файлах — дубликаты не пишем."""
    removed_rpyc: list[Path] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def summary(self) -> str:
        text = (f"tl/{self.language}: {len(self.files_written)} файлов, "
                f"{self.blocks} блоков диалога, {self.strings} UI-строк")
        if self.skipped_existing:
            text += (f" (уже переведено игрой: {self.skipped_existing} блоков — "
                     "не дублируем)")
        return text


_TRANSLATE_LINE_RE = re.compile(
    r"^\s*translate\s+([A-Za-z_][\w]*)\s+([A-Za-z_][\w]*)\s*:")


def existing_block_ids(tl_dir: Path, language: str) -> set[str]:
    """Идентификаторы блоков, которые уже есть в tl-файлах не от нас.

    Игра может идти с собственной русификацией (например, официальной):
    писать второй ``translate <lang> <id>`` нельзя — Ren'Py упадёт с
    «A translation for ... already exists». Такие блоки мы пропускаем.
    """
    ids: set[str] = set()
    if not tl_dir.is_dir():
        return ids
    for path in tl_dir.rglob("*.rpy"):
        if _is_ours(path):
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for match in _TRANSLATE_LINE_RE.finditer(text):
            if match.group(1) == language and match.group(2) != "strings":
                ids.add(match.group(2))
    return ids


def _is_identity_text(text: str) -> bool:
    """
    Строка без букв («...», «{cps=1}...{/cps}», «?!») переводится сама в себя.

    Ren'Py всё равно создаёт для такой реплики translate-блок, поэтому мы тоже
    его пишем — иначе файл перевода не совпадает с ожиданиями движка
    по количеству блоков.
    """
    # теги {cps=20} и подстановки [player] буквами не считаем
    stripped = re.sub(r"\{[^{}]*\}|\[[^\[\]]*\]", "", text)
    return not any(ch.isalpha() for ch in stripped)


def _header(language: str, comment: str = "") -> str:
    lines = [
        f"# {GENERATED_MARKER} {APP_NAME} {VERSION}",
        f"# Язык: {language}. Сгенерировано автоматически "
        f"{time.strftime('%Y-%m-%d %H:%M:%S')}.",
        "# Не редактируйте этот файл вручную: при повторной генерации он будет перезаписан.",
    ]
    if comment:
        lines.append(f"# {comment}")
    lines.append("")
    return "\n".join(lines) + "\n"


def build_say_code(item: TextItem, text: str) -> str:
    """
    Собирает say-инструкцию для tl-файла: тот же персонаж, те же атрибуты,
    то же ``with``, но новый текст.
    """
    meta = item.say
    parts: list[str] = []
    if meta is None:
        return encode_say_string(text)

    if meta.who:
        parts.append(meta.who)
    if meta.attributes:
        parts.extend(meta.attributes)
    if meta.temporary_attributes:
        parts.append("@")
        parts.extend(meta.temporary_attributes)

    parts.append(encode_say_string(text))

    if not meta.interact:
        parts.append("nointeract")
    if meta.arguments:
        parts.append(meta.arguments.replace(" ,", ",").replace("( ", "(").replace(" )", ")"))
    if meta.with_:
        parts.extend(["with", meta.with_])

    return " ".join(parts)


def _is_ours(path: Path) -> bool:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return GENERATED_MARKER in f.read(400)
    except OSError:
        return False


# ---------------------------------------------------------------------------
def build_tl(project: RenpyProject, items: Iterable[TextItem], language: str,
             set_default_language: bool = False, force_language: bool = False,
             hook_fallback: bool = False) -> TLBuildResult:
    """
    Создаёт (или обновляет) ``game/tl/<language>/``.

    Ничего не пишет в оригинальные .rpy файлы игры.
    """
    result = TLBuildResult(language=language)
    items = list(items)

    tl_dir = project.game_dir / TL_DIRECTORY / language
    result.tl_dir = tl_dir
    tl_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # 1. Диалоги -> translate-блоки, по файлам
    # ------------------------------------------------------------------
    by_file: dict[str, list[TextItem]] = {}
    for item in items:
        if item.kind not in BLOCK_KINDS:
            continue
        if not item.say or not item.say.block_id:
            result.skipped_items += 1
            continue
        if not item.is_translated() and not _is_identity_text(item.original):
            result.skipped_items += 1
            continue
        by_file.setdefault(item.file, []).append(item)

    used_names: set[str] = set()

    # Блоки, которые уже переведены не нами (официальная русификация игры).
    taken = existing_block_ids(tl_dir, language)
    if taken:
        result.warnings.append(
            f"в игре уже есть перевод для {len(taken)} блоков — наши дубликаты "
            "не пишем (иначе Ren'Py упадёт на повторном идентификаторе)")

    for rel_file, file_items in sorted(by_file.items()):
        target = tl_dir / rel_file
        if target.suffix.lower() == ".rpym":
            target = target.with_suffix(".rpy")

        if target.exists() and not _is_ours(target):
            # Чужой перевод — не затираем, пишем рядом
            alt_name = f"{GENERATED_PREFIX}_{Path(rel_file).name}"
            target = tl_dir / alt_name
            result.warnings.append(
                f"Файл tl/{language}/{rel_file} уже существует и создан не нами — "
                f"перевод записан в {alt_name}"
            )

        chunks = [_header(language)]
        for item in file_items:
            block_id = item.say.block_id  # type: ignore[union-attr]
            if block_id in taken:
                result.skipped_existing += 1
                continue
            original_code = item.say.code if item.say else ""
            translated = escape_for_renpy(
                item.result_text() if item.is_translated() else item.original,
                item.original)
            if translated != item.result_text():
                result.warnings.append(
                    f"лишние скобки экранированы: {item.file}:{item.line}")

            chunks.append(f"# {item.file}:{item.line}")
            chunks.append(f"translate {language} {block_id}:")
            chunks.append("")
            if original_code:
                chunks.append(f"    # {original_code}")
                chunks.append("")
            chunks.append(f"    {build_say_code(item, translated)}")
            chunks.append("")
            result.blocks += 1

        atomic_write_text(target, "\n".join(chunks))
        result.files_written.append(target)
        used_names.add(target.name)

    # ------------------------------------------------------------------
    # 2. Строки интерфейса -> translate <lang> strings
    # ------------------------------------------------------------------
    string_items = [
        i for i in items
        if i.kind in (KIND_MENU, KIND_UI, KIND_NAME) and i.is_translated()
    ]

    if string_items:
        # уже существующие переводы (например, чужая русификация) — не дублируем,
        # иначе Ren'Py упадёт с "A translation for ... already exists"
        seen: set[str] = set()
        for other in tl_dir.glob("*.rpy"):
            if other.name.startswith(GENERATED_PREFIX):
                continue
            try:
                existing = other.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for match in _OLD_RE.finditer(existing):
                seen.add(_unquote_renpy(match.group(1)))
        if seen:
            result.warnings.append(
                f"учтено {len(seen)} существующих строк перевода из чужих tl-файлов"
            )
        lines = [_header(language, "UI-строки, варианты меню, имена персонажей")]
        lines.append(f"translate {language} strings:")
        lines.append("")
        count = 0
        for item in string_items:
            old = item.original
            if old in seen:
                continue
            seen.add(old)
            new = escape_for_renpy(item.result_text(), item.original)
            if new != item.result_text():
                result.warnings.append(
                    f"лишние скобки экранированы: {item.file}:{item.line}")
            lines.append(f"    # {item.file}:{item.line}")
            lines.append(f'    old "{quote_unicode(old)}"')
            lines.append(f'    new "{quote_unicode(new)}"')
            lines.append("")
            count += 1

        if count:
            target = tl_dir / f"{GENERATED_PREFIX}_strings.rpy"
            atomic_write_text(target, "\n".join(lines))
            result.files_written.append(target)
            result.strings = count

    # ------------------------------------------------------------------
    # 3. Служебные файлы: язык по умолчанию, runtime-хук
    # ------------------------------------------------------------------
    if set_default_language or force_language:
        target = tl_dir / f"{GENERATED_PREFIX}_language.rpy"
        code = [_header(language, "Автоматическое включение языка")]
        code.append("# Включаем язык так, чтобы это работало даже когда в меню игры")
        code.append("# «Язык» нашего пункта нет (языки часто прописаны в screens.rpy")
        code.append("# жёстко: English / Simplified Chinese и всё).")
        code.append("")
        if set_default_language:
            # init 999, а не -999: `define config.default_language = None`
            # в options.rpy игры выполняется на init 0 и иначе перекрывает нас.
            code.append("init 999 python:")
            code.append("    try:")
            code.append(f'        config.default_language = "{language}"')
            code.append("    except Exception:")
            code.append("        pass")
            code.append("")
            code.append("init 1600 python:")
            code.append("    # Если игрок ещё не выбирал язык — включаем русский.")
            code.append("    # Достаточно удалить этот файл, чтобы вернуть английский.")
            code.append("    try:")
            code.append("        if not _preferences.language:")
            code.append(f'            _preferences.language = "{language}"')
            code.append("    except Exception:")
            code.append("        pass")
            code.append("")
        if force_language:
            code.append("init 1601 python:")
            code.append("    # Принудительно включаем язык при каждом запуске.")
            code.append("    # Чтобы вернуть выбор игрока — удалите этот файл.")
            code.append("    try:")
            code.append("        _preferences.language = "
                        f'"{language}"')
            code.append("    except Exception:")
            code.append("        pass")
            code.append("")
        atomic_write_text(target, "\n".join(code))
        result.files_written.append(target)

    if hook_fallback:
        target = tl_dir / f"{GENERATED_PREFIX}_hook.rpy"
        atomic_write_text(target, _build_hook_file(items, language))
        result.files_written.append(target)
        result.warnings.append(
            "Добавлен runtime-хук (config.say_menu_text_filter): перевод будет "
            "работать даже если идентификаторы блоков не совпали с движком."
        )

    # ------------------------------------------------------------------
    # 4. Чистим .rpyc рядом со сгенерированными .rpy
    # ------------------------------------------------------------------
    for path in list(result.files_written):
        compiled = path.with_suffix(".rpyc")
        if remove_file(compiled, logger):
            result.removed_rpyc.append(compiled)

    return result


def _build_hook_file(items: Iterable[TextItem], language: str) -> str:
    """
    Файл с runtime-фильтром: страховка на случай, если идентификаторы блоков
    не совпали (нестандартная сборка движка).

    Словарь встраивается как python-литерал (repr), поэтому кавычки,
    переводы строк и юникод экранируются корректно.
    """
    mapping: dict[str, str] = {}
    for item in items:
        if not item.is_translated():
            continue
        if item.kind not in (KIND_MENU, KIND_UI, KIND_NAME) + BLOCK_KINDS:
            continue
        mapping.setdefault(item.original, item.result_text())

    if mapping:
        body = "{\n" + "".join(
            f"        {key!r}: {value!r},\n" for key, value in mapping.items()
        ) + "    }"
    else:
        body = "{}"

    return f"""{_header(language, "Runtime-хук: подмена текста без изменения оригинальных .rpy")}
init -10 python:

    _renpy_translator_data = {body}


init 1650 python:

    def _renpy_translator_filter(text):
        if not isinstance(text, str):
            return text
        try:
            if _preferences.language != "{language}":
                return text
        except Exception:
            return text
        return _renpy_translator_data.get(text, text)

    def _renpy_translator_install():
        # Ставим фильтр поверх уже существующего (если игра свой поставила).
        try:
            old = getattr(config, "say_menu_text_filter", None)
            if old is None:
                config.say_menu_text_filter = _renpy_translator_filter
            else:
                config.say_menu_text_filter = (
                    lambda s: _renpy_translator_filter(old(s))
                )
        except Exception:
            pass

        # В старых Ren'Py фильтр применялся только при этом флаге, в новых
        # такой переменной нет вовсе. Ставим только если она существует,
        # иначе движок падает с "not a known configuration variable".
        try:
            if hasattr(config, "use_menu_text_filter"):
                config.use_menu_text_filter = True
        except Exception:
            pass

    _renpy_translator_install()
"""


#: `Language("russian")` / `Language(None)` в screens.rpy — меню выбора языка игры.
LANGUAGE_ACTION_RE = re.compile(r"Language\s*\(\s*(None|\"[^\"]*\"|'[^']*')")


def language_menu_languages(project: RenpyProject) -> tuple[bool, set[str]]:
    """
    Возвращает (есть ли меню «Язык» вообще, какие языки оно предлагает).

    Ren'Py не показывает список каталогов `tl/` автоматически: пункты меню
    прописаны в `screens.rpy` вручную (`action Language("simplified_chinese")`).
    Поэтому в чужой игре русского пункта обычно нет — язык приходится включать
    из нашего файла `zz_renpy_translator_language.rpy`.
    """
    found: set[str] = set()
    has_menu = False
    skip_dirs = {"tl", "renpy", "cache", "saves", "libs", "python-packages"}
    for path in project.game_dir.rglob("*.rpy"):
        parts = path.relative_to(project.game_dir).parts
        if any(part.lower() in skip_dirs for part in parts[:-1]):
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if "Language(" not in text:
            continue
        has_menu = True
        for match in LANGUAGE_ACTION_RE.finditer(text):
            value = match.group(1).strip()
            if value == "None":
                continue
            found.add(value.strip("\"'"))
    return has_menu, found


def clean_tl_dir(project: RenpyProject, language: str) -> list[Path]:
    """Удаляет все наши файлы из tl/<language>/ (полезно перед регенерацией)."""
    tl_dir = project.game_dir / TL_DIRECTORY / language
    removed: list[Path] = []
    if not tl_dir.is_dir():
        return removed
    for path in tl_dir.rglob("*.rpy*"):
        if path.suffix.lower() == ".rpyc":
            continue
        if _is_ours(path):
            try:
                path.unlink()
                removed.append(path)
            except OSError:
                pass
    return removed


def tl_status(project: RenpyProject, language: str) -> dict[str, int]:
    """Сколько блоков/строк уже есть в tl-каталоге."""
    tl_dir = project.game_dir / TL_DIRECTORY / language
    stats = {"files": 0, "blocks": 0, "strings": 0}
    if not tl_dir.is_dir():
        return stats
    for path in tl_dir.rglob("*.rpy"):
        stats["files"] += 1
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        stats["blocks"] += text.count(f"translate {language} ")
        stats["strings"] += text.count("    old ")
    return stats
