"""
Шрифты с полной кириллицей для переведённой игры.

Зачем это нужно
---------------
Перевод — это текст, а текст рисуется шрифтом. Если игра использует
собственный шрифт без русских букв (частая история: декоративный латинский
шрифт в ``gui.rpy``), игрок увидит вместо букв пустые квадратики. Это не
ошибка перевода, но выглядит как поломка.

Что делает модуль
-----------------
1. Читает скрипты игры и собирает все ссылки на шрифты
   (``gui.text_font = "fonts/x.ttf"``, ``style say_dialogue: font "..."`` и т.п.).
2. Проверяет у каждого найденного файла наличие кириллицы — разбирая таблицу
   ``cmap`` TrueType/OpenType напрямую (без внешних библиотек).
3. Копирует в игру шрифт **PT Serif** (ParaType, лицензия OFL 1.1 — можно
   свободно распространять) во всех четырёх начертаниях.
4. Генерирует ``zz_renpy_translator_fonts.rpy``, который:

   * подменяет через ``config.font_replacement_map`` ровно те шрифты игры,
     в которых нет кириллицы (режим «починить»), либо
   * ставит PT Serif для всех текстовых стилей (режим «красиво»).

Режимы
------
``pretty`` — «красиво»: весь текст (диалоги, имена, меню, интерфейс) набрается
             PT Serif. Значение по умолчанию.
``fix``    — «починить»: трогаем только шрифты без кириллицы.
``off``    — ничего не делать.

Ничего не пишется в оригинальные ``.rpy`` игры: файл шрифтов лежит рядом с
переводом (``game/tl/<язык>/fonts`` в режиме ``tl``).
"""

from __future__ import annotations

import re
import shutil
import struct
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional

from config import GENERATED_MARKER, TL_DIRECTORY
from core.models import ParsedProject
from core.rpa_extractor import RpaError, read_index
from core.scanner import RenpyProject
from core.utils import get_logger

logger = get_logger(__name__)

#: Каталог с исходниками шрифтов (внутри пакета программы).
FONTS_SOURCE_DIR = Path(__file__).resolve().parent.parent / "assets" / "fonts"

#: Имя файла-описания шрифтов (сортируется последним и грузится после игры).
FONT_RPY_NAME = "zz_renpy_translator_fonts.rpy"

#: Имя каталога со шрифтами внутри игры.
FONT_GAME_DIR = "fonts"

#: Начертания: (жирный, курсив) -> файл.
FONT_FILES: dict[tuple[bool, bool], str] = {
    (False, False): "PT_Serif-Regular.ttf",
    (True, False): "PT_Serif-Bold.ttf",
    (False, True): "PT_Serif-Italic.ttf",
    (True, True): "PT_Serif-BoldItalic.ttf",
}

#: Шрифты, которые Ren'Py тащит с собой: кириллица в них есть всегда.
BUILTIN_SAFE_FONTS = {
    "dejavusans.ttf", "dejavusans-bold.ttf", "dejavusansoblique.ttf",
    "dejavusans-boldoblique.ttf", "dejavuserif.ttf", "roboto.ttf", "roboto-bold.ttf",
}

#: Символы, которые обязаны быть в «хорошем» шрифте.
REQUIRED_CHARS = tuple(range(0x0410, 0x0450)) + (0x0401, 0x0451) + (
    0x00AB, 0x00BB, 0x2014, 0x2013, 0x2026,
)

MODES = ("pretty", "fix", "off")

#: Имена стилей, которым точно нужен шрифт (страховка для режима pretty).
KNOWN_TEXT_STYLES = (
    "default", "text", "say_dialogue", "say_label", "say_thought", "namebox",
    "namebox_label", "choice_button_text", "choice_label", "button_text",
    "label_text", "input", "input_text", "menu_button_text", "main_menu_title",
    "quick_button_text", "notify_text", "help", "gui_text", "confirm_prompt",
    "skip_text", "nvl_dialogue", "nvl_label", "bubble_text", "history_text",
)

#: Регулярные выражения для поиска ссылок на шрифты в скриптах игры.
FONT_REF_PATTERNS = (
    # font = "path.ttf"   (python-код: gui.text_font = ..., config.font = ...)
    re.compile(r'font\s*=\s*[rbu]{0,2}"([^"\n]+)"'),
    re.compile(r"font\s*=\s*[rbu]{0,2}'([^'\n]+)'"),
    # font "path.ttf"     (style-блок)
    re.compile(r'^\s*font\s+[rbu]{0,2}"([^"\n]+)"', re.MULTILINE),
    re.compile(r"^\s*font\s+[rbu]{0,2}'([^'\n]+)'", re.MULTILINE),
)


# ---------------------------------------------------------------------------
# Разбор TrueType/OpenType
# ---------------------------------------------------------------------------
def _iter_cmap_codepoints(data: bytes) -> set[int]:
    """Возвращает набор поддерживаемых кодпойнтов из таблицы cmap."""
    if len(data) < 12:
        return set()

    num_tables = struct.unpack(">H", data[4:6])[0]
    tables: dict[str, tuple[int, int]] = {}
    for index in range(num_tables):
        offset = 12 + 16 * index
        if offset + 16 > len(data):
            break
        tag = data[offset:offset + 4].decode("latin-1")
        start, length = struct.unpack(">II", data[offset + 8:offset + 16])
        tables[tag] = (start, length)

    if "cmap" not in tables:
        return set()

    cmap_start = tables["cmap"][0]
    if cmap_start + 4 > len(data):
        return set()

    count = struct.unpack(">H", data[cmap_start + 2:cmap_start + 4])[0]
    subtables: list[int] = []
    for index in range(count):
        record = cmap_start + 4 + 8 * index
        if record + 8 > len(data):
            break
        platform, encoding, sub_offset = struct.unpack(">HHI", data[record:record + 8])
        if (platform, encoding) in ((3, 1), (3, 10), (0, 3), (0, 4), (0, 6)):
            subtables.append(cmap_start + sub_offset)

    codepoints: set[int] = set()
    for sub in subtables:
        if sub + 2 > len(data):
            continue
        fmt = struct.unpack(">H", data[sub:sub + 2])[0]

        if fmt == 4:
            if sub + 14 > len(data):
                continue
            seg_x2 = struct.unpack(">H", data[sub + 6:sub + 8])[0]
            segments = seg_x2 // 2
            ends_at = sub + 14
            starts_at = ends_at + seg_x2 + 2
            if starts_at + seg_x2 > len(data):
                continue
            ends = struct.unpack(f">{segments}H", data[ends_at:ends_at + seg_x2])
            starts = struct.unpack(f">{segments}H", data[starts_at:starts_at + seg_x2])
            for start, end in zip(starts, ends):
                if start <= end and (end - start) < 0x10000:
                    codepoints.update(range(start, end + 1))

        elif fmt == 12:
            if sub + 16 > len(data):
                continue
            groups = struct.unpack(">I", data[sub + 12:sub + 16])[0]
            for group in range(min(groups, 4096)):
                base = sub + 16 + 12 * group
                if base + 12 > len(data):
                    break
                start, end, _glyph = struct.unpack(">III", data[base:base + 12])
                if start <= end and (end - start) < 0x10000:
                    codepoints.update(range(start, end + 1))

    return codepoints


def font_has_cyrillic_bytes(data: bytes) -> bool:
    """Есть ли в шрифте русские буквы и типографские символы."""
    if data[:4] not in (b"\x00\x01\x00\x00", b"OTTO", b"true", b"ttcf"):
        return False
    codepoints = _iter_cmap_codepoints(data)
    if not codepoints:
        return False
    return all(char in codepoints for char in REQUIRED_CHARS)


def font_has_cyrillic(path: Path) -> bool:
    """То же самое, но для файла (ошибки чтения = «нет кириллицы»)."""
    try:
        return font_has_cyrillic_bytes(path.read_bytes())
    except OSError as exc:
        logger.debug("Не удалось прочитать шрифт %s: %s", path, exc)
        return False


#: Символы для проверки «есть ли в шрифте иероглифы/хангыль».
#: 中 (китайский), 漢 (традиционный), カ (катакана), 가 (хангыль).
CJK_PROBE_CHARS = (0x4E2D, 0x6F22, 0x30AB, 0xAC00)


def font_has_cjk_bytes(data: bytes) -> bool:
    """Умеет ли шрифт рисовать иероглифы, кану и хангыль."""
    if data[:4] not in (b"\x00\x01\x00\x00", b"OTTO", b"true", b"ttcf"):
        return False
    codepoints = _iter_cmap_codepoints(data)
    if not codepoints:
        return False
    return all(char in codepoints for char in CJK_PROBE_CHARS)


def font_has_cjk(path: Path) -> bool:
    """То же, но для файла: умеет ли он рисовать иероглифы/хангыль."""
    try:
        return font_has_cjk_bytes(path.read_bytes())
    except OSError as exc:
        logger.debug("Не удалось прочитать шрифт %s: %s", path, exc)
        return False


def cjk_fallback_fonts(project: RenpyProject,
                       references: Iterable[str]) -> list[str]:
    """
    Шрифты игры, в которых есть иероглифы.

    Нужны, чтобы собрать ``FontGroup``: кириллица из PT Serif, а иероглифы —
    из родного шрифта игры (иначе пункт «简体中文» в меню языков превратится
    в квадратики, как раньше превращался «Русский»).
    """
    found: list[str] = []
    for name in references:
        data = read_font_bytes(project, name)
        if data and font_has_cjk_bytes(data):
            found.append(name)
    return found


# ---------------------------------------------------------------------------
# Поиск ссылок на шрифты
# ---------------------------------------------------------------------------
def _read_archive_member(archive: Path, name: str) -> Optional[bytes]:
    """Читает файл из .rpa (нужно для шрифтов внутри архива)."""
    try:
        index, _key = read_index(archive)
    except (RpaError, OSError) as exc:
        logger.debug("Не удалось прочитать индекс %s: %s", archive, exc)
        return None

    wanted = {name, "game/" + name, name.replace("/", "\\")}
    for member, parts in index.items():
        if member not in wanted and member.replace("\\", "/") not in wanted:
            continue
        chunks: list[bytes] = []
        try:
            with open(archive, "rb") as handle:
                for offset, length, prefix in parts:
                    if prefix:
                        chunks.append(prefix)
                    handle.seek(offset)
                    remaining = length
                    while remaining > 0:
                        block = handle.read(min(remaining, 1 << 20))
                        if not block:
                            break
                        chunks.append(block)
                        remaining -= len(block)
        except OSError as exc:
            logger.debug("Ошибка чтения %s из %s: %s", member, archive, exc)
            return None
        return b"".join(chunks)
    return None


def read_font_bytes(project: RenpyProject, name: str) -> Optional[bytes]:
    """Ищет файл шрифта в game/, в корне игры и внутри .rpa."""
    normalized = name.replace("\\", "/").lstrip("./")
    candidates = [
        project.game_dir / normalized,
        project.project_root / normalized,
    ]
    for candidate in candidates:
        try:
            if candidate.is_file():
                return candidate.read_bytes()
        except OSError:
            continue

    for archive in project.rpa_files:
        data = _read_archive_member(archive, normalized)
        if data:
            return data
    return None


def collect_font_references(project: RenpyProject) -> list[str]:
    """Все имена шрифтов, на которые ссылаются скрипты игры (без tl/)."""
    found: dict[str, None] = {}
    for path in project.rpy_files:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for pattern in FONT_REF_PATTERNS:
            for match in pattern.finditer(text):
                name = match.group(1).strip()
                if not name or "\n" in name:
                    continue
                found.setdefault(name, None)
    return sorted(found)


def classify_font(project: RenpyProject, name: str) -> str:
    """``cyrillic`` / ``no_cyrillic`` / ``missing`` / ``builtin``."""
    if name.lower().rsplit("/", 1)[-1] in BUILTIN_SAFE_FONTS:
        return "builtin"
    data = read_font_bytes(project, name)
    if data is None:
        return "missing"
    return "cyrillic" if font_has_cyrillic_bytes(data) else "no_cyrillic"


# ---------------------------------------------------------------------------
# Результат работы
# ---------------------------------------------------------------------------
@dataclass
class FontReport:
    mode: str = "pretty"
    fonts_dir: Optional[Path] = None
    rpy_path: Optional[Path] = None
    copied: list[str] = field(default_factory=list)
    replaced: list[str] = field(default_factory=list)
    kept: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    cjk_fallbacks: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def summary(self) -> str:
        if self.mode == "off":
            return "шрифты: не трогали"
        parts = [f"шрифты: {self.mode}"]
        if self.replaced:
            parts.append(f"подменено {len(self.replaced)}")
        if self.kept:
            parts.append(f"оставлено {len(self.kept)}")
        if self.copied:
            parts.append(f"скопировано файлов {len(self.copied)}")
        return ", ".join(parts)


# ---------------------------------------------------------------------------
# Генерация .rpy
# ---------------------------------------------------------------------------
def _quote(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def build_font_rpy(project: RenpyProject, fonts_rel_dir: str, mode: str,
                   references: Iterable[str],
                   cjk_fonts: Iterable[str] = ()) -> str:
    """
    Текст ``zz_renpy_translator_fonts.rpy``.

    ``fonts_rel_dir`` — путь к каталогу шрифтов относительно ``game/``.
    Все опасные операции обёрнуты в try/except: даже если в конкретной версии
    Ren'Py чего-то нет, игра запустится.
    """
    refs = list(dict.fromkeys(references))
    font_base = fonts_rel_dir.rstrip("/")

    lines: list[str] = [
        f"# {GENERATED_MARKER} — шрифты с полной кириллицей",
        "# PT Serif (ParaType), лицензия SIL Open Font License 1.1.",
        "# Файл создан автоматически и будет перезаписан при повторной сборке.",
        "",
        "init -100 python:",
        f"    _rt_font_dir = {_quote(font_base + '/')}",
        "    _rt_font_files = {",
    ]
    for (bold, italic), filename in FONT_FILES.items():
        lines.append(f"        ({bold}, {italic}): _rt_font_dir + {_quote(filename)},")
    cjk = [name for name in dict.fromkeys(cjk_fonts) if name]
    lines += [
        "    }",
    ]
    if cjk:
        lines += [
            "",
            "    # Шрифты игры с иероглифами: кириллицу берём из PT Serif,",
            "    # а китайский/японский/корейский — из родного шрифта игры.",
            "    _rt_cjk_fonts = [",
        ]
        for name in cjk:
            lines.append(f"        {_quote(name)},")
        lines += [
            "    ]",
            "",
            "    _rt_font_groups = {}",
            "",
            "    def _rt_font_for(bold, italic):",
            "        key = (bold, italic)",
            "        if key in _rt_font_groups:",
            "            return _rt_font_groups[key]",
            "        base = _rt_font_files[key]",
            "        group = base",
            "        try:",
            "            try:",
            "                _rt_group_class = FontGroup",
            "            except NameError:",
            "                from renpy.text.font import FontGroup as _rt_group_class",
            "            group = _rt_group_class()",
            "            group.add(base, 0x0000, 0x2E7F)",
            "            for _rt_cjk in _rt_cjk_fonts:",
            "                group.add(_rt_cjk, 0x2E80, 0x9FFF)",
            "                group.add(_rt_cjk, 0xA000, 0xD7FF)",
            "            group.add(base, 0xE000, 0xFFFF)",
            "        except Exception:",
            "            group = base",
            "        _rt_font_groups[key] = group",
            "        return group",
            "",
            "    _rt_font_regular = _rt_font_for(False, False)",
            "",
        ]
    else:
        lines += [
            "    _rt_font_regular = _rt_font_files[(False, False)]",
            "",
        ]

    if refs:
        lines += [
            "    # Шрифты игры без русских букв: отдаём Ren'Py правильное начертание.",
            "    _rt_broken_fonts = [",
        ]
        for ref in refs:
            lines.append(f"        {_quote(ref)},")
        lines += [
            "    ]",
            "",
            "    for _rt_name in _rt_broken_fonts:",
            "        for _rt_bold in (False, True):",
            "            for _rt_italic in (False, True):",
            "                try:",
            "                    config.font_replacement_map[",
            "                        (_rt_name, _rt_bold, _rt_italic)] = (",
            "                            _rt_font_files[(_rt_bold, _rt_italic)],",
            "                            False, False)",
            "                except Exception:",
            "                    pass",
            "",
        ]

    if mode == "pretty":
        lines += [
            "init 999 python:",
            "    # Режим «красиво»: весь текст набирается PT Serif.",
            "    try:",
            "        style.default.font = _rt_font_regular",
            "    except Exception:",
            "        pass",
            "",
            "    _rt_text_styles = (",
        ]
        for index, name in enumerate(KNOWN_TEXT_STYLES):
            suffix = "," if index < len(KNOWN_TEXT_STYLES) - 1 else ""
            lines.append(f"        {_quote(name)}{suffix}")
        lines += [
            "    )",
            "    for _rt_style_name in _rt_text_styles:",
            "        try:",
            "            getattr(style, _rt_style_name).font = _rt_font_regular",
            "        except Exception:",
            "            pass",
            "",
            "    # Все остальные стили, которые знает эта версия Ren'Py.",
            "    try:",
            "        _rt_all_styles = list(dir(style))",
            "    except Exception:",
            "        _rt_all_styles = []",
            "",
            "    for _rt_style_name in _rt_all_styles:",
            "        try:",
            "            getattr(style, _rt_style_name).font = _rt_font_regular",
            "        except Exception:",
            "            pass",
            "",
            "    # Внутренний реестр стилей — если он доступен.",
            "    try:",
            "        _rt_registry = list(renpy.style.style_map.values())",
            "    except Exception:",
            "        _rt_registry = []",
            "",
            "    for _rt_entries in _rt_registry:",
            "        for _rt_style in _rt_entries:",
            "            try:",
            "                _rt_style.font = _rt_font_regular",
            "            except Exception:",
            "                pass",
            "",
        ]

    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Установка
# ---------------------------------------------------------------------------
def source_font_files() -> list[Path]:
    """Файлы шрифтов, которые лежат рядом с программой."""
    return [FONTS_SOURCE_DIR / name for name in FONT_FILES.values()
            if (FONTS_SOURCE_DIR / name).is_file()]


def target_dir_for(project: RenpyProject, language: str, mode: str) -> Path:
    if (mode or "").lower() == "replace":
        return project.game_dir / "renpy_translator_fonts"
    return project.game_dir / TL_DIRECTORY / language / FONT_GAME_DIR


def installed_font_rel(project: RenpyProject,
                       language: str) -> Optional[str]:
    """
    Путь к установленному PT Serif относительно ``game/``.

    Такой путь можно подставлять в ``text_font "..."`` в экранах игры:
    гарантия, что у конкретной надписи (например, «Русский» в меню языков)
    будет шрифт с кириллицей, чем бы ни рисовались соседние кнопки.
    """
    for mode in ("tl", "replace"):
        candidate = target_dir_for(project, language, mode) / \
            FONT_FILES[(False, False)]
        if candidate.is_file():
            try:
                return candidate.relative_to(project.game_dir).as_posix()
            except ValueError:
                continue
    return None


def install(project: RenpyProject, parsed: Optional[ParsedProject],
            language: str, mode: str = "pretty",
            build_mode: str = "tl") -> FontReport:
    """
    Копирует шрифты в игру и создаёт ``zz_renpy_translator_fonts.rpy``.

    ``mode``       — pretty / fix / off;
    ``build_mode`` — tl / replace (куда класть файлы шрифтов).
    """
    mode = (mode or "pretty").lower()
    report = FontReport(mode=mode)
    if mode not in MODES:
        mode = "pretty"
        report.mode = mode
    if mode == "off":
        return report

    # PT Serif — шрифт с кириллицей и латиницей. Для японского, китайского,
    # арабского и т.п. он бесполезен (иероглифов в нём нет), поэтому
    # принудительно не ставим: только сообщаем об этом.
    from core.langs import needs_bundled_font

    if not needs_bundled_font(language):
        report.warnings.append(
            f"для языка «{language}» встроенный PT Serif не подходит "
            "(в нём нет нужных символов) — шрифты игры не менялись")
        logger.info(report.warnings[-1])
        return report

    sources = source_font_files()
    if len(sources) != len(FONT_FILES):
        report.warnings.append(
            "файлы шрифтов не найдены рядом с программой (assets/fonts) — "
            "шрифты не установлены")
        logger.warning(report.warnings[-1])
        return report

    references = collect_font_references(project)
    replacements: list[str] = []
    for name in references:
        status = classify_font(project, name)
        if status in ("no_cyrillic", "missing"):
            replacements.append(name)
            if status == "missing":
                report.missing.append(name)
            else:
                report.replaced.append(name)
        else:
            report.kept.append(name)

    if mode == "fix" and not replacements:
        logger.info("Шрифты игры поддерживают кириллицу — менять не нужно.")
        report.kept = list(report.kept)
        return report

    fonts_dir = target_dir_for(project, language, build_mode)
    try:
        fonts_dir.mkdir(parents=True, exist_ok=True)
        for source in sources:
            target = fonts_dir / source.name
            if not target.exists() or target.stat().st_size != source.stat().st_size:
                shutil.copyfile(source, target)
            report.copied.append(source.name)

        license_source = FONTS_SOURCE_DIR / "OFL.txt"
        if license_source.is_file():
            shutil.copyfile(license_source, fonts_dir / "OFL.txt")
    except OSError as exc:
        report.warnings.append(f"не удалось скопировать шрифты: {exc}")
        logger.warning(report.warnings[-1])
        return report

    rpy_dir = (project.game_dir / TL_DIRECTORY / language) if build_mode != "replace" \
        else project.game_dir
    rpy_dir.mkdir(parents=True, exist_ok=True)
    fonts_rel_dir = fonts_dir.relative_to(project.game_dir).as_posix()

    rpy_path = rpy_dir / FONT_RPY_NAME
    try:
        cjk_fonts = cjk_fallback_fonts(project, references)
        if cjk_fonts:
            report.cjk_fallbacks.extend(cjk_fonts)
        rpy_path.write_text(
            build_font_rpy(project, fonts_rel_dir, mode, replacements,
                           cjk_fonts),
            encoding="utf-8")
        report.rpy_path = rpy_path
    except OSError as exc:
        report.warnings.append(f"не удалось записать {rpy_path.name}: {exc}")
        logger.warning(report.warnings[-1])
        return report

    report.fonts_dir = fonts_dir
    logger.info("Шрифты: %s (подменено %d, оставлено %d)",
                report.summary(), len(report.replaced) + len(report.missing),
                len(report.kept))
    for name in replacements:
        logger.info("  шрифт без кириллицы -> PT Serif: %s", name)
    return report
