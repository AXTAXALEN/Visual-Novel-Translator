#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
RenPyTranslator — автоматическая русификация визуальных новелл на Ren'Py.

Примеры запуска
---------------

Полная русификация (штатные файлы перевода, язык появится в настройках игры):

    python main.py --game "C:/Games/MyVN/game" --mode tl --engine google

Прямая замена текста в .rpy (с бэкапом):

    python main.py --game "C:/Games/MyVN/game" --mode replace --engine deepl --api-key XXX

Только экспорт текста (для ручного/AI-перевода):

    python main.py --game "C:/Games/MyVN/game" --export --format json --output translations.json

Импорт готового перевода:

    python main.py --game "C:/Games/MyVN/game" --import translations_done.json --mode tl

Подготовка (.rpa -> .rpyc -> .rpy):

    python main.py --game "C:/Games/MyVN/game" --prepare

Очистка кэша:

    python main.py --clear-cache
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path
from typing import Optional

# --- корректная кириллица в консоли Windows --------------------------------
def _fix_console_encoding() -> None:
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        if stream is None:
            continue
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except Exception:
            pass


_fix_console_encoding()


# --- вывод при запуске без консоли (.exe, собранный с --windowed) ----------
class _NullStream:
    """Заглушка для sys.stdout/stderr, когда окна консоли нет."""

    encoding = "utf-8"

    def write(self, _text: str) -> int:
        return 0

    def flush(self) -> None:
        pass

    def isatty(self) -> bool:
        return False

    def reconfigure(self, **_kwargs) -> None:  # noqa: D401
        pass


def _ensure_streams() -> None:
    """В windowed-сборке sys.stdout/stderr = None: подменяем их на файл/заглушку."""
    if sys.stdout is not None and sys.stderr is not None:
        return
    stream = None
    try:
        base = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) \
            else Path(__file__).resolve().parent
        stream = open(base / "renpy_translator_console.log", "a", encoding="utf-8",
                      buffering=1)
    except Exception:  # noqa: BLE001
        stream = None
    if sys.stdout is None:
        sys.stdout = stream or _NullStream()  # type: ignore[assignment]
    if sys.stderr is None:
        sys.stderr = stream or _NullStream()  # type: ignore[assignment]


_ensure_streams()

# --- путь к проекту (чтобы работало `python main.py` из любого каталога) ---
PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config import (  # noqa: E402
    APP_NAME,
    DEFAULT_SOURCE_LANG,
    DEFAULT_TARGET_LANG,
    MODES,
    SUPPORTED_ENGINES,
    VERSION,
    Settings,
    default_cache_path,
)
from core.cache import TranslationCache  # noqa: E402
from core.pipeline import FullReport, Pipeline  # noqa: E402
from core.rpa_extractor import archive_summary  # noqa: E402
from core.rpyc_decompiler import tool_status  # noqa: E402
from core.scanner import ProjectError, scan_project  # noqa: E402
from core.translator import TranslationError, create_engine  # noqa: E402
from core.utils import (  # noqa: E402
    get_logger,
    human_duration,
    setup_logging,
)

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Аргументы командной строки
# ---------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="renpy_translator",
        description=f"{APP_NAME} {VERSION} — автоматический перевод визуальных "
                    "новелл на движке Ren'Py.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )

    parser.add_argument("--game", "-g", metavar="ПУТЬ",
                        help="папка game/ или корень игры (обязательно для рабочих режимов)")
    parser.add_argument("--mode", "-m", choices=MODES, default="tl",
                        help="tl — штатные файлы game/tl/русский (рекомендуется); "
                             "replace — замена текста прямо в .rpy")
    parser.add_argument("--engine", "-e", choices=SUPPORTED_ENGINES, default="google",
                        help="движок перевода (по умолчанию google)")
    parser.add_argument("--api-key", metavar="КЛЮЧ",
                        help="API-ключ DeepL / LibreTranslate")
    parser.add_argument("--libre-url", metavar="URL",
                        help="адрес LibreTranslate, например http://localhost:5000")
    parser.add_argument("--lang", default=DEFAULT_TARGET_LANG,
                        help="язык перевода (по умолчанию russian)")
    parser.add_argument("--source-lang", default=DEFAULT_SOURCE_LANG,
                        help="язык оригинала (по умолчанию auto)")

    # --- действия -------------------------------------------------------
    parser.add_argument("--prepare", action="store_true",
                        help="только подготовка: распаковать .rpa и декомпилировать .rpyc")
    parser.add_argument("--export", action="store_true",
                        help="экспортировать текст (нужен --format)")
    parser.add_argument("--format", dest="export_format",
                        choices=("json", "txt", "docx", "both"), default="txt",
                        help="формат экспорта (both = JSON + TXT)")
    parser.add_argument("--output", "-o", metavar="ФАЙЛ",
                        help="куда сохранить экспорт")
    parser.add_argument("--import", dest="import_file", metavar="ФАЙЛ",
                        help="импортировать готовый перевод (JSON/TXT) и собрать tl/")
    parser.add_argument("--clear-cache", action="store_true",
                        help="удалить кэш переводов и выйти")
    parser.add_argument("--doctor", action="store_true",
                        help="показать состояние окружения (зависимости, инструменты)")
    parser.add_argument("--gui", action="store_true", help="запустить графический интерфейс")

    # --- тонкая настройка ----------------------------------------------
    parser.add_argument("--cache", metavar="ФАЙЛ", help="путь к translation_cache.json")
    parser.add_argument("--log", metavar="ФАЙЛ", help="путь к translator.log")
    parser.add_argument("--glossary", metavar="ФАЙЛ",
                        help="JSON-словарь постоянных переводов (имена, термины)")
    parser.add_argument("--batch-size", type=int, default=25,
                        help="сколько строк отправлять одним пакетом (по умолчанию 25)")
    parser.add_argument("--workers", "-j", type=int, default=4,
                        help="параллельных потоков перевода (по умолчанию 4)")
    parser.add_argument("--delay", type=float, default=0.0,
                        help="задержка между запросами, сек (для лимитов API)")
    parser.add_argument("--retries", type=int, default=4,
                        help="повторов при ошибке сети/API (по умолчанию 4)")
    parser.add_argument("--timeout", type=float, default=30.0,
                        help="таймаут запроса, сек")
    parser.add_argument("--no-cache", action="store_true", help="не использовать кэш")
    parser.add_argument("--no-backup", action="store_true", help="не создавать бэкап")
    parser.add_argument("--no-names", action="store_true",
                        help="не переводить имена персонажей")
    parser.add_argument("--no-ui", action="store_true",
                        help="не переводить UI-строки (экраны, _())")
    parser.add_argument("--set-default-language", action="store_true",
                        help="прописать config.default_language в tl/ (русский по умолчанию)")
    parser.add_argument("--force-language", action="store_true",
                        help="принудительно включать язык при каждом запуске игры")
    parser.add_argument("--font-mode", choices=("pretty", "fix", "off"), default="pretty",
                        help="шрифты с кириллицей: pretty — весь текст PT Serif "
                             "(по умолчанию), fix — только шрифты без русских букв, "
                             "off — не трогать")
    parser.add_argument("--language-button", dest="language_button",
                        action="store_true", default=True,
                        help="своя кнопка «Русский/English» в меню игры (по умолчанию)")
    parser.add_argument("--no-language-button", dest="language_button",
                        action="store_false",
                        help="не добавлять свою кнопку переключения языка")
    parser.add_argument("--patch-menu", dest="patch_language_menu",
                        action="store_true",
                        help="вписать пункт «Русский» прямо в screens.rpy игры "
                             "(с резервной копией)")
    parser.add_argument("--patch-image-menus", dest="patch_image_menus",
                        action="store_true", default=True,
                        help="заменить кнопки меню, нарисованные картинками "
                             "(imagebutton auto ...), текстовыми с русской "
                             "подписью (правка screens.rpy с бэкапом; "
                             "по умолчанию включено)")
    parser.add_argument("--no-patch-image-menus", dest="patch_image_menus",
                        action="store_false",
                        help="не трогать кнопки-картинки в меню")
    parser.add_argument("--wrap-screen-text", dest="wrap_screen_text",
                        action="store_true", default=True,
                        help="обернуть строки экранов без _() в _(), иначе они "
                             "не переводятся никогда (правка .rpy игры с "
                             "бэкапом; по умолчанию включено)")
    parser.add_argument("--no-wrap-screen-text", dest="wrap_screen_text",
                        action="store_false",
                        help="не оборачивать строки экранов в _()")
    parser.add_argument("--engine-strings", dest="engine_strings",
                        action="store_true", default=True,
                        help="переводить строки движка Ren'Py: окна "
                             "подтверждения, быстрые сохранения (по умолчанию)")
    parser.add_argument("--no-engine-strings", dest="engine_strings",
                        action="store_false",
                        help="не добавлять перевод строк движка")
    parser.add_argument("--hook-fallback", action="store_true",
                        help="добавить runtime-хук: страховка, если id-блоки не совпадут")
    parser.add_argument("--keep-orphan", action="store_true",
                        help="не удалять ранее сгенерированные tl-файлы перед сборкой")
    parser.add_argument("--keep-rpyc", action="store_true",
                        help="не удалять .rpyc рядом с изменёнными .rpy")
    parser.add_argument("--extract-all-rpa", action="store_true",
                        help="извлекать из .rpa ВСЕ файлы (по умолчанию — только "
                             "скрипты .rpy/.rpyc)")
    parser.add_argument("--dry-run", action="store_true",
                        help="ничего не писать на диск (только показать, что будет сделано)")
    parser.add_argument("--verify", action="store_true",
                        help="проверить готовый tl-каталог (линтер) и выйти")
    parser.add_argument("--report", metavar="ФАЙЛ", nargs="?", const="",
                        help="сохранить HTML-отчёт (по умолчанию рядом с игрой)")
    parser.add_argument("--merge-parts", dest="merge_parts", nargs="?",
                        const="", metavar="ПУТЬ",
                        help="слить части экспорта (папка или любой файл "
                             "из набора) в один txt")
    parser.add_argument("--verify-parts", dest="verify_parts", nargs="?",
                        const="", metavar="ПУТЬ",
                        help="сверить части с текстом игры: ничего не "
                             "потеряно и не подменено")
    parser.add_argument("--docx-limit", dest="docx_limit", type=int,
                        metavar="СТРОК",
                        help="предел строк для DOCX (по умолчанию 15000)")
    parser.add_argument("--split", dest="split", type=int, default=0,
                        metavar="СТРОК",
                        help="резать txt на части по N строк (например 5000)")
    parser.add_argument("--with-json", dest="with_json", action="store_true",
                        help="к txt добавить JSON")
    parser.add_argument("--with-docx", dest="with_docx", action="store_true",
                        help="к txt добавить DOCX (медленно, предел 15 000 строк)")
    parser.add_argument("--check-syntax", dest="check_syntax",
                        action="store_true",
                        help="проверить файлы игры: не пропало ли двоеточие "
                             "у menu/label/screen/init")
    parser.add_argument("--fix-syntax", dest="fix_syntax",
                        action="store_true",
                        help="вернуть двоеточия на место (с резервной копией)")
    parser.add_argument("--breakdown", action="store_true",
                        help="показать, из чего складывается число "
                             "переводимых строк (по файлам и типам)")
    parser.add_argument("--find-image-text", dest="find_image_text",
                        action="store_true",
                        help="показать картинки, на которых нарисован текст")
    parser.add_argument("--image-replace", dest="image_replace",
                        metavar="ФАЙЛ",
                        help="нарисовать русскую версию картинки, "
                             "например gui/main_menu_title.png")
    parser.add_argument("--image-text", dest="image_text", default="",
                        help="русский текст для --image-replace")
    parser.add_argument("--image-font", dest="image_font", metavar="TTF",
                        help="шрифт для --image-replace")
    parser.add_argument("--image-size", dest="image_size", type=int,
                        help="размер шрифта (по умолчанию — подбор)")
    parser.add_argument("--image-color", dest="image_color",
                        default="#ffffff", help="цвет текста, #RRGGBB")
    parser.add_argument("--image-box", dest="image_box", metavar="x,y,w,h",
                        help="область для заливки и надписи")
    parser.add_argument("--restore-images", dest="restore_images",
                        action="store_true",
                        help="вернуть оригинальные картинки и экраны")
    parser.add_argument("--restore", metavar="АРХИВ", nargs="?", const="",
                        help="откатить игру из резервной копии (по умолчанию "
                             "самая свежая)")
    parser.add_argument("--list-backups", action="store_true",
                        help="показать резервные копии игры и выйти")
    parser.add_argument("--no-quality", action="store_true",
                        help="не проверять качество перевода")
    parser.add_argument("--no-quality-fallback", action="store_true",
                        help="не откатывать битые строки в оригинал")
    parser.add_argument("--no-verify", action="store_true",
                        help="не проверять tl-каталог после сборки")
    parser.add_argument("--translate-dev-tools", action="store_true",
                        help="переводить и файлы инструментов разработчика "
                             "(ActionEditor, редакторы экранов и т.п.)")
    parser.add_argument("--retry-rounds", type=int, default=None, metavar="N",
                        help="сколько раз дожимать строки, которые не перевелись "
                             "с первого раза (по умолчанию 3)")
    parser.add_argument("--check-ui", action="store_true",
                        help="проверить меню и интерфейс: какие надписи "
                             "переводимые, а какие нет (и почему)")
    parser.add_argument("--info", action="store_true",
                        help="показать информацию о найденной игре и выйти")
    parser.add_argument("-v", "--verbose", action="store_true", help="подробный лог")
    parser.add_argument("-q", "--quiet", action="store_true", help="минимум вывода")
    parser.add_argument("--version", action="version",
                        version=f"{APP_NAME} {VERSION}")

    return parser


def settings_from_args(args: argparse.Namespace) -> Settings:
    settings = Settings()
    settings.game_path = Path(args.game).expanduser() if args.game else None
    settings.mode = args.mode
    settings.engine = args.engine
    settings.api_key = args.api_key
    settings.libretranslate_url = args.libre_url
    settings.target_lang = args.lang
    settings.source_lang = args.source_lang
    settings.batch_size = max(1, args.batch_size)
    settings.workers = max(1, args.workers)
    settings.request_delay = max(0.0, args.delay)
    settings.max_retries = max(0, args.retries)
    settings.timeout = max(5.0, args.timeout)
    settings.extract_names = not args.no_names
    settings.extract_ui = not args.no_ui
    settings.use_cache = not args.no_cache
    settings.backup = not args.no_backup
    settings.delete_stale_rpyc = not args.keep_rpyc
    settings.keep_orphan_translations = args.keep_orphan
    settings.set_default_language = args.set_default_language
    settings.force_language = args.force_language
    settings.hook_fallback = args.hook_fallback
    settings.language_button = args.language_button
    settings.patch_language_menu = args.patch_language_menu
    settings.patch_image_menus = args.patch_image_menus
    settings.wrap_screen_text = args.wrap_screen_text
    settings.engine_strings = args.engine_strings
    settings.quality_check = not getattr(args, "no_quality", False)
    settings.quality_fallback = not getattr(args, "no_quality_fallback", False)
    settings.verify_after_build = not getattr(args, "no_verify", False)
    settings.skip_dev_tools = not getattr(args, "translate_dev_tools", False)
    if getattr(args, "retry_rounds", None) is not None:
        settings.retry_rounds = max(0, args.retry_rounds)
    settings.font_mode = getattr(args, "font_mode", "pretty") or "pretty"
    settings.dry_run = args.dry_run
    settings.verbose = args.verbose
    settings.extract_rpa_all = bool(args.extract_all_rpa)
    if args.cache:
        settings.cache_path = Path(args.cache).expanduser()
    if args.log:
        settings.log_path = Path(args.log).expanduser()
    if args.glossary:
        settings.glossary_path = Path(args.glossary).expanduser()
    return settings


# ---------------------------------------------------------------------------
# Служебные команды
# ---------------------------------------------------------------------------
def cmd_doctor() -> int:
    import platform

    print(f"{APP_NAME} {VERSION}")
    print(f"Python: {platform.python_version()} ({platform.system()} {platform.release()})")
    print(f"Кэш:    {default_cache_path()}")

    print("\nЗависимости:")
    packages = [
        ("tqdm", "прогресс-бары"),
        ("deep_translator", "Google/DeepL/Libre через deep-translator"),
        ("rpycdec", "декомпиляция .rpyc и распаковка .rpa"),
        ("unrpa", "альтернативная распаковка .rpa"),
        ("docx", "экспорт в DOCX (python-docx)"),
        ("tkinter", "графический интерфейс"),
    ]
    for module, purpose in packages:
        try:
            __import__(module)
            print(f"  [ok]   {module:16s} — {purpose}")
        except Exception:
            print(f"  [нет]  {module:16s} — {purpose}")

    print("\nИнструменты декомпиляции:")
    for name, status in tool_status().items():
        print(f"  {name}: {status}")

    return 0


def cmd_info(settings: Settings) -> int:
    try:
        project = scan_project(settings.game_path)  # type: ignore[arg-type]
    except ProjectError as exc:
        logger.error("%s", exc)
        return 1

    print(project.summary())
    if project.rpa_files:
        print("\nАрхивы:")
        for rpa in project.rpa_files:
            print("  " + archive_summary(rpa))
    return 0


def cmd_list_backups(settings: Settings) -> int:
    from core.backup import list_backups
    from core.utils import human_size

    try:
        project = scan_project(settings.game_path)  # type: ignore[arg-type]
    except ProjectError as exc:
        logger.error("%s", exc)
        return 1
    backups = list_backups(project)
    if not backups:
        print("Резервных копий нет.")
        return 0
    print(f"Резервные копии {project.project_root}:")
    for path in backups:
        try:
            size = human_size(path.stat().st_size)
            stamp = time.strftime("%d.%m.%Y %H:%M",
                                  time.localtime(path.stat().st_mtime))
        except OSError:
            size, stamp = "?", "?"
        print(f"  {path.name:32s} {size:>9s}  {stamp}")
    print('\nОткатить: --restore "путь к архиву" '
          '(или --restore без пути — возьмётся самый свежий)')
    return 0


def cmd_restore(settings: Settings, archive_arg: str) -> int:
    from core.backup import restore_backup

    try:
        project = scan_project(settings.game_path)  # type: ignore[arg-type]
    except ProjectError as exc:
        logger.error("%s", exc)
        return 1
    archive = Path(archive_arg).expanduser() if archive_arg else None
    result = restore_backup(project, archive)
    print(result.summary())
    for problem in result.problems[:10]:
        print("  ! " + problem)
    if result.archive:
        print(f"  архив: {result.archive.name}")
    return 0 if result.ok else 1


def _parts_target(value: str, settings: Settings) -> Path:
    if value:
        return Path(value).expanduser()
    if settings.game_path:
        return Path(settings.game_path).expanduser().parent
    return Path.cwd()


def cmd_merge_parts(settings: Settings, value: str,
                    output: Optional[Path] = None) -> int:
    from core.parts import collect_parts, merge

    target = _parts_target(value, settings)
    found = collect_parts(target)
    if not found.files:
        print(f"Части не найдены: {target}")
        return 1
    print(f"Найдено частей: {len(found.files)} ({found.files[0].name} … "
          f"{found.files[-1].name})")
    result = merge(found.files, output)
    print("Слияние: " + result.summary())
    if result.duplicates:
        print(f"  ! повторяющихся идентификаторов: {result.duplicates}")
    return 0


def cmd_verify_parts(settings: Settings, value: str) -> int:
    from core.parts import collect_parts, verify

    target = _parts_target(value, settings)
    found = collect_parts(target)
    if not found.files:
        print(f"Части не найдены: {target}")
        return 1
    parsed = None
    if settings.game_path:
        try:
            pipeline = Pipeline(settings)
            pipeline.scan()
            parsed = pipeline.analyze()
        except Exception as exc:  # noqa: BLE001
            logger.warning("Сверить с игрой не удалось: %s", exc)
    report = verify(found.files, parsed)
    for line in report.lines():
        print(line)
    return 0 if report.ok else 1


def cmd_breakdown(settings: Settings) -> int:
    """Показать, из чего складывается число переводимых строк."""
    import dataclasses

    from core.breakdown import build_breakdown
    from core.pipeline import Pipeline

    read_only = dataclasses.replace(
        settings, patch_image_menus=False, wrap_screen_text=False,
        patch_language_menu=False, backup=False)

    pipeline = Pipeline(read_only)
    pipeline.scan()
    if pipeline.project is not None and pipeline.project.needs_preparation:
        print("Нужна подготовка (.rpa/.rpyc) — считаю по тому, что есть.\n")
    parsed = pipeline.analyze()
    report = build_breakdown(parsed, pipeline)
    for line in report.lines():
        print(line)
    return 0


def cmd_syntax(settings: Settings, repair: bool = False) -> int:
    """Проверить (и починить) двоеточия у блочных строк в файлах игры."""
    from core.screen_refs import check_screen_refs
    from core.syntax_guard import check_project

    project = scan_project(settings.game_path)  # type: ignore[arg-type]
    report = check_project(project, repair=repair)
    for line in report.lines(project):
        print(line)

    # экран мог целиком пропасть при декомпиляции
    screens = check_screen_refs(project)
    if screens.missing:
        print()
        for line in screens.lines():
            print(line)
    if report.repaired:
        print(f"\nИсправлено строк: {report.repaired}. Рядом лежащие .rpyc "
              f"удалены — Ren'Py перекомпилирует скрипт.")
    elif report.issues:
        print("\nДобавьте «:» в конец такой строки (например «menu» -> «menu:») "
              "или запустите с --fix-syntax")
    return 0 if report.ok else 1


def cmd_find_image_text(settings: Settings) -> int:
    from core.image_text import report

    project = scan_project(settings.game_path)  # type: ignore[arg-type]
    for line in report(project, settings.normalized_lang()):
        print(line)
    return 0


def cmd_restore_images(settings: Settings) -> int:
    from core.image_text import revert_references

    project = scan_project(settings.game_path)  # type: ignore[arg-type]
    restored = revert_references(project, settings.normalized_lang())
    if restored:
        print("Возвращены файлы игры: " + ", ".join(restored))
    else:
        print("Изменённых экранов не найдено.")
    return 0


def cmd_image_replace(settings: Settings, args: argparse.Namespace) -> int:
    from core.image_text import replace

    project = scan_project(settings.game_path)  # type: ignore[arg-type]
    box = None
    if args.image_box:
        try:
            parts = [int(value) for value in
                     str(args.image_box).replace(" ", "").split(",")]
        except ValueError:
            logger.error("--image-box ждёт четыре числа: x,y,ширина,высота")
            return 2
        if len(parts) != 4:
            logger.error("--image-box ждёт четыре числа: x,y,ширина,высота")
            return 2
        x, y, width, height = parts
        box = (x, y, x + width, y + height)
    try:
        result = replace(project, settings.normalized_lang(),
                         args.image_replace.replace("\\", "/"),
                         args.image_text, box=box,
                         font=Path(args.image_font) if args.image_font else None,
                         size=args.image_size, colour=args.image_color)
    except Exception as exc:  # noqa: BLE001
        logger.error("%s", exc)
        return 1
    print("Готово: " + result.summary())
    if result.helper:
        print("  служебный файл: " + result.helper.name)
    return 0


def cmd_verify(settings: Settings) -> int:
    from core.verify import verify_project

    pipeline = Pipeline(settings)
    pipeline.scan()
    language = settings.normalized_lang()
    try:
        parsed = pipeline.analyze()
    except Exception as exc:  # noqa: BLE001
        logger.warning("Разбор игры не удался (%s), проверяю только tl", exc)
        parsed = None
    report = verify_project(pipeline.project, language, parsed)
    print(report.summary())
    for issue in report.issues[:40]:
        mark = "!" if issue.severity == "error" else " "
        where = f" ({issue.where()})" if issue.where() else ""
        print(f"  {mark} {issue.label():24s} {issue.message}{where}")
    if len(report.issues) > 40:
        print(f"  … и ещё {len(report.issues) - 40} замечаний (полностью — в отчёте)")
    return 0 if report.ok else 1


def cmd_report(settings: Settings, target: str) -> int:
    from core.report import default_report_path

    pipeline = Pipeline(settings)
    report = pipeline.run_full()
    path = Path(target).expanduser() if target else default_report_path(
        report.project.project_root if report.project else None)
    pipeline.write_report(path, translate_report=report.translate_report)
    print(f"Отчёт: {path}")
    return 0 if report.ok else 1


def cmd_clear_cache(settings: Settings) -> int:
    cache = TranslationCache(settings.cache_path, enabled=False)
    if not settings.cache_path.exists():
        print(f"Кэш не найден: {settings.cache_path}")
        return 0
    cache.clear()
    print(f"Кэш удалён: {settings.cache_path}")
    return 0


# ---------------------------------------------------------------------------
# Основные сценарии
# ---------------------------------------------------------------------------
def run_pipeline(settings: Settings, args: argparse.Namespace) -> int:
    pipeline = Pipeline(settings)

    # 1. Только подготовка
    if args.prepare and not args.import_file:
        pipeline.scan()
        rpa_results, decompile_report = pipeline.prepare()
        print("\nИтог подготовки:")
        for res in rpa_results:
            print("  " + res.summary())
        if decompile_report.results:
            print("  " + decompile_report.summary())
        if decompile_report.unavailable:
            print("  ! " + decompile_report.unavailable)
        if not args.info:
            return 0 if (rpa_results or decompile_report.ok_count) else 0

    # 2. Только экспорт
    if args.export and not args.import_file:
        pipeline.scan()
        if pipeline.project and pipeline.project.needs_preparation:
            logger.info("Обнаружены .rpyc/.rpa — сначала подготовка")
            pipeline.prepare()
        pipeline.analyze()
        output = Path(args.output).expanduser() if args.output else None
        if args.export_format == "both":
            results = pipeline.export_all(
                output, with_json=True, with_docx=bool(args.with_docx),
                split=max(0, args.split))
        else:
            results = [pipeline.export(args.export_format, output,
                                       split=max(0, args.split))]
        print("\nЭкспорт готов:")
        for res in results:
            print(f"  {res.path} — {res.count} строк")
        return 0

    # 3. Импорт + сборка
    if args.import_file:
        pipeline.scan()
        if pipeline.project and pipeline.project.needs_preparation:
            pipeline.prepare()
        pipeline.analyze()
        try:
            import_result = pipeline.import_file(Path(args.import_file).expanduser())
        except Exception as exc:  # noqa: BLE001
            logger.error("Импорт не удался: %s", exc)
            return 1
        print("\n" + import_result.summary())
        for warning in import_result.warnings:
            print("  ! " + warning)

        if settings.backup and not settings.dry_run:
            from core.backup import create_backup

            backup = create_backup(pipeline.project, extra_paths=[settings.cache_path])
            print("  " + backup.summary())

        tl_result, replace_result = pipeline.build(pipeline.parsed)
        print()
        if tl_result:
            print("Сборка tl:", tl_result.summary())
            for warning in tl_result.warnings:
                print("  ! " + warning)
        if replace_result:
            print("Замена:", replace_result.summary())
        return 0

    # 4. Полный цикл
    report = pipeline.run_full()
    print_final_report(report, settings)
    return 0 if report.ok else 1


def print_final_report(report: FullReport, settings: Settings) -> None:
    print("\n" + "=" * 66)
    print(f"{APP_NAME} {VERSION} — итог")
    print("=" * 66)

    if report.project:
        print(f"Игра: {report.project.name} ({report.project.game_dir})")
    if report.prepare_results:
        extracted = sum(len(r.extracted) for r in report.prepare_results)
        print(f"Распаковка .rpa: извлечено {extracted} файлов "
              f"из {len(report.prepare_results)} архивов")
    if report.decompile_report and report.decompile_report.results:
        print("Декомпиляция: " + report.decompile_report.summary())
    if report.backup_note:
        print("Бэкап: " + report.backup_note)
    if report.parsed:
        stats = report.parsed.stats()
        print(f"Извлечено строк: {stats.get('total', 0)} "
              f"(уникальных: {stats.get('unique_texts', 0)})")
        print(f"  реплики: {stats.get('dialogue', 0)}, "
              f"нарратив: {stats.get('narration', 0)}, "
              f"выбор: {stats.get('menu', 0)}, "
              f"интерфейс: {stats.get('ui', 0)}, "
              f"имена: {stats.get('name', 0)}")
    if report.import_result:
        print(report.import_result.summary())
    if report.translate_report:
        tr = report.translate_report
        print(f"Перевод ({tr.engine}): {tr.translated} новых, "
              f"{tr.from_cache} из кэша, {tr.failed} ошибок, "
              f"время {human_duration(tr.elapsed)}")
        if tr.from_game:
            print(f"  взято из игры (перевод прошлого запуска): {tr.from_game} строк")
        if tr.from_literary:
            print(f"  авторских строк сохранено: {tr.from_literary}")
        if tr.translated == 0 and tr.failed:
            print("  ! НИ ОДНА строка не переведена. Проверьте интернет/лимиты API,")
            print("    попробуйте --engine deepl (с ключом) или --engine libre,")
            print("    либо уменьшите нагрузку: --workers 1 --delay 1.5")
        if tr.failed_texts:
            print("  первые непереведённые строки:")
            for text in tr.failed_texts[:3]:
                print(f"    - {text[:70]!r}")
    for res in report.export_results:
        print("Экспорт: " + res.summary())
    if report.tl_result:
        print("Сборка: " + report.tl_result.summary())
        if report.tl_result.tl_dir:
            print(f"  каталог: {report.tl_result.tl_dir}")
    if report.replace_result:
        print("Сборка: " + report.replace_result.summary())
    if report.font_report and report.font_report.mode != "off":
        print("Шрифты: " + report.font_report.summary())
        for name in report.font_report.replaced:
            print(f"  без кириллицы → PT Serif: {name}")
        for name in report.font_report.missing:
            print(f"  файл не найден → PT Serif: {name}")

    if settings.mode == "tl":
        if settings.set_default_language or settings.force_language:
            print("\nДальше: просто запустите игру — русский язык включится сам "
                  "(файл tl/<язык>/zz_renpy_translator_language.rpy).")
        else:
            print("\nДальше: запустите игру → Настройки → Язык → Русский")
            print("        Если такого пункта в меню нет (языки часто прописаны "
                  "в screens.rpy),")
            print("        запустите с --set-default-language или поставьте "
                  "галочку в программе.")
    else:
        print("\nТекст заменён прямо в .rpy. Запустите игру — перевод применится "
              "автоматически.")
    print("=" * 66)


# ---------------------------------------------------------------------------
def main(argv: Optional[list[str]] = None) -> int:
    args_list = argv if argv is not None else sys.argv[1:]
    parser = build_parser()

    # запуск без аргументов -> GUI (если доступен)
    if not args_list:
        if _try_gui():
            return 0
        parser.print_help()
        return 0

    args = parser.parse_args(args_list)

    settings = settings_from_args(args)
    setup_logging(settings.log_path, verbose=args.verbose)
    if getattr(args, "docx_limit", None):
        from core.exporter import set_docx_limit

        set_docx_limit(args.docx_limit)
    if args.quiet:
        import logging

        logging.getLogger("renpy_translator").setLevel(logging.WARNING)

    if args.doctor:
        return cmd_doctor()

    if args.clear_cache:
        return cmd_clear_cache(settings)

    if args.gui:
        if _try_gui(settings):
            return 0
        logger.error("GUI недоступен (нет tkinter). Используйте CLI-режим.")
        return 1

    # слияние и сверка частей не требуют игры вовсе
    if args.merge_parts is not None:
        return cmd_merge_parts(settings, args.merge_parts,
                               Path(args.output).expanduser()
                               if args.output else None)

    if args.verify_parts is not None:
        return cmd_verify_parts(settings, args.verify_parts)

    if not args.game:
        logger.error("Укажите путь к игре: --game \"C:/Games/MyVN/game\"")
        parser.print_help()
        return 2

    if args.check_ui:
        from core.scanner import scan_project
        from core.uicheck import check_ui

        project = scan_project(settings.game_path
                              if settings.game_path else ".")
        report = check_ui(project, settings.normalized_lang())
        for line in report.lines():
            print(line)
        return 0

    if args.info:
        return cmd_info(settings)

    if args.check_syntax or args.fix_syntax:
        return cmd_syntax(settings, repair=bool(args.fix_syntax))

    if args.breakdown:
        return cmd_breakdown(settings)

    if args.find_image_text:
        return cmd_find_image_text(settings)

    if args.restore_images:
        return cmd_restore_images(settings)

    if args.image_replace:
        return cmd_image_replace(settings, args)

    if args.list_backups:
        return cmd_list_backups(settings)

    if args.restore is not None:
        return cmd_restore(settings, args.restore)

    if args.verify and not args.import_file:
        return cmd_verify(settings)

    if args.report is not None and not (args.prepare or args.export):
        return cmd_report(settings, args.report)

    try:
        return run_pipeline(settings, args)
    except ProjectError as exc:
        logger.error("%s", exc)
        return 1
    except TranslationError as exc:
        logger.error("Ошибка перевода: %s", exc)
        return 1
    except KeyboardInterrupt:
        logger.warning("Прервано пользователем (прогресс сохранён в кэше)")
        return 130
    except Exception as exc:  # noqa: BLE001
        logger.exception("Непредвиденная ошибка: %s", exc)
        return 1


def _try_gui(settings: Optional[Settings] = None) -> bool:
    try:
        from gui import run_gui  # импорт внутри, чтобы CLI работал без tkinter
    except Exception as exc:  # noqa: BLE001
        logger.debug("GUI недоступен: %s", exc)
        return False
    try:
        run_gui(settings)
        return True
    except Exception as exc:  # noqa: BLE001
        logger.error("Не удалось запустить GUI: %s", exc)
        return False


if __name__ == "__main__":
    sys.exit(main())
