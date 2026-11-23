"""
Глобальная конфигурация, пути, константы и настройки RenPyTranslator.

Модуль не зависит от остальных модулей проекта — его можно импортировать
откуда угодно.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

APP_NAME = "RenPyTranslator"
VERSION = "3.7.6"

# ---------------------------------------------------------------------------
# Расширения файлов Ren'Py
# ---------------------------------------------------------------------------
RPY_EXT = ".rpy"
RPYC_EXT = ".rpyc"
RPYM_EXT = ".rpym"
RPYMC_EXT = ".rpymc"
RPA_EXT = ".rpa"
RPI_EXT = ".rpi"   # RPA-1.0: чистый zlib-pickle индекс без заголовка

SCRIPT_SUFFIXES = (RPY_EXT, RPYC_EXT, RPYM_EXT, RPYMC_EXT)

#: Каталоги внутри игры, которые мы НИКОГДА не разбираем и не трогаем.
EXCLUDED_DIRS = {
    "renpy",            # файлы самого движка
    "tl",               # каталог переводов (обрабатывается отдельно)
    "lib",              # библиотеки движка (win/linux/mac)
    "cache",
    "saves",
    "__pycache__",
    ".git",
    ".svn",
    "node_modules",
    "_renpy_translator_backup",  # наши бэкапы
    "_backup",
    "python-packages",
    "game cache",
}

#: Служебные файлы Ren'Py, которые не содержат переводимого текста.
IGNORED_FILES = {
    "script_version.txt",
    "script_version.rpy",
    "script_version.rpyc",
    "traceback.txt",
    "errors.txt",
    "log.txt",
}

#: Язык, на который переводим по умолчанию.
DEFAULT_TARGET_LANG = "russian"
DEFAULT_SOURCE_LANG = "auto"

#: Имя каталога переводов внутри game/ (renpy.config.tl_directory).
TL_DIRECTORY = "tl"

#: Имя файла кэша переводов.
CACHE_FILENAME = "translation_cache.json"

#: Имя файла лога.
LOG_FILENAME = "translator.log"

#: Маркер, который мы ставим в шапку сгенерированных файлов.
GENERATED_MARKER = "[renpy-translator]"

#: Префикс файлов, которые создаёт программа внутри game/tl/<lang>/.
GENERATED_PREFIX = "zz_renpy_translator"


def app_dir() -> Path:
    """
    Каталог, рядом с которым лежат кэш и лог.

    * Для исходников — каталог проекта (где лежит main.py).
    * Для собранного PyInstaller-ом .exe — каталог самого .exe.
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def user_data_dir() -> Path:
    """Резервный каталог для кэша/лога, если рядом с программой нет прав записи."""
    base = os.environ.get("APPDATA") or os.environ.get("XDG_DATA_HOME")
    if base:
        return Path(base) / APP_NAME
    return Path.home() / ("." + APP_NAME.lower())


def _writable(directory: Path) -> bool:
    try:
        directory.mkdir(parents=True, exist_ok=True)
        probe = directory / ".write_test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return True
    except Exception:
        return False


def migrate_legacy_cache(target: Path) -> Optional[Path]:
    """
    Перенести кэш, лежавший рядом со старой версией программы.

    Раньше ``translation_cache.json`` жил в папке с ``.exe``: удалили старую
    версию — потеряли весь перевод. Теперь кэш лежит в профиле пользователя,
    а старый файл (если он ещё есть) аккуратно вливается в него.
    """
    import json

    legacy = app_dir() / CACHE_FILENAME
    if not legacy.is_file():
        return None
    try:
        if legacy.resolve() == Path(target).resolve():
            return None
    except OSError:
        return None
    try:
        data = json.loads(legacy.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not data:
        return None

    merged: dict[str, str] = {}
    try:
        if Path(target).is_file():
            current = json.loads(Path(target).read_text(encoding="utf-8"))
            if isinstance(current, dict):
                merged.update({k: v for k, v in current.items()
                               if isinstance(k, str) and isinstance(v, str)})
    except (OSError, ValueError):
        pass
    added = 0
    for key, value in data.items():
        if isinstance(key, str) and isinstance(value, str) and key not in merged:
            merged[key] = value
            added += 1
    if added:
        try:
            Path(target).parent.mkdir(parents=True, exist_ok=True)
            tmp = Path(str(target) + ".tmp")
            tmp.write_text(json.dumps(merged, ensure_ascii=False, indent=1),
                           encoding="utf-8")
            tmp.replace(target)
        except OSError:
            return None
    return legacy


def default_cache_path() -> Path:
    """
    Путь к файлу кэша переводов (translation_cache.json).

    Кэш намеренно живёт **в профиле пользователя** (``%APPDATA%/RenPyTranslator``
    на Windows), а не рядом с ``.exe``: иначе перенос программы в другую папку
    или удаление прошлой версии стирает весь готовый перевод, и прогон
    начинается с нуля.
    """
    stable = user_data_dir()
    if _writable(stable):
        target = stable / CACHE_FILENAME
        migrate_legacy_cache(target)
        return target
    return app_dir() / CACHE_FILENAME


def default_log_path() -> Path:
    """Путь к файлу лога (translator.log)."""
    if _writable(app_dir()):
        return app_dir() / LOG_FILENAME
    return user_data_dir() / LOG_FILENAME


@dataclass
class Settings:
    """Все настройки одного запуска программы."""

    # --- вход/выход --------------------------------------------------------
    game_path: Optional[Path] = None
    """Путь, который ввёл пользователь (корень игры или game/)."""

    mode: str = "tl"
    """Режим сборки: 'tl' (штатные файлы перевода) или 'replace' (прямая замена)."""

    target_lang: str = DEFAULT_TARGET_LANG
    source_lang: str = DEFAULT_SOURCE_LANG

    # --- перевод -----------------------------------------------------------
    engine: str = "google"
    api_key: Optional[str] = None
    libretranslate_url: Optional[str] = None
    batch_size: int = 25
    workers: int = 4
    request_delay: float = 0.0
    max_retries: int = 4
    timeout: float = 30.0
    glossary_path: Optional[Path] = None

    # --- что извлекать -----------------------------------------------------
    extract_names: bool = True
    """Переводить отображаемые имена персонажей (Character("..."))."""

    extract_ui: bool = True
    """Переводить UI-строки: меню, экраны, config.name и т.п."""

    # --- служебное ---------------------------------------------------------
    cache_path: Path = field(default_factory=default_cache_path)
    log_path: Path = field(default_factory=default_log_path)
    use_cache: bool = True
    backup: bool = True
    delete_stale_rpyc: bool = True
    keep_orphan_translations: bool = False
    set_default_language: bool = False
    """Прописать в tl-файл config.default_language = <target_lang>."""

    force_language: bool = False
    """Жёстко включать язык при каждом запуске игры."""

    hook_fallback: bool = False
    """Дополнительно ставить runtime-хук (config.say_menu_text_filter)."""

    quality_check: bool = True
    """Проверять целостность перевода (плейсхолдеры, теги, %-форматы)."""

    skip_dev_tools: bool = True
    """Не переводить файлы инструментов разработчика (ActionEditor3,
    редактор экранов, тестовые сцены): игрок их не видит, а строки там
    технические — они только портят отчёт."""

    retry_rounds: int = 3
    slow_mode: bool = True
    """Если бесплатный сервис упёрся в лимит — не останавливаться, а перейти
    в медленный режим: один поток, паузы между запросами. Долго, зато перевод
    доходит до конца и продолжается с того же места при следующем запуске."""
    """Сколько раз «дожимать» строки, которые не перевелись с первого раза
    (сеть, лимиты Google). 0 — не дожимать."""

    quality_retry: bool = True
    """Перепроверять «сломанные» строки ещё раз перед сборкой."""

    quality_fallback: bool = True
    """Битые строки отдавать оригиналом, а не ломать разметку игры."""

    verify_after_build: bool = True
    """Статическая проверка готового tl-каталога после сборки."""

    language_button: bool = True
    """Своя кнопка-переключатель «Русский / English» в главном меню и
    настройках игры (файл tl/<язык>/zz_renpy_translator_language_button.rpy,
    файлы игры не трогаются)."""

    patch_language_menu: bool = False
    """Вписать пункт языка прямо в screens.rpy игры (правка файла игры;
    перед изменением делается резервная копия)."""

    patch_image_menus: bool = True
    """Заменить кнопки меню, нарисованные картинками
    (``imagebutton auto "gui/menubuttons/start_%s.png"``), текстовыми
    кнопками с русской подписью. Правка screens.rpy с резервной копией."""

    engine_strings: bool = True
    """Переводить строки самого движка: окна подтверждения («вернуться в
    главное меню?»), быстрые сохранения, служебные сообщения."""

    wrap_screen_text: bool = True
    """Обернуть в ``_()`` строки экранов, написанные без него
    (``text "Story by ..."``): иначе Ren'Py их не переводит никогда.
    Правка .rpy игры с резервной копией."""

    font_mode: str = "pretty"
    """Шрифты с кириллицей: 'pretty' (весь текст PT Serif), 'fix' (только
    шрифты без русских букв), 'off' (не трогать)."""

    dry_run: bool = False
    verbose: bool = False
    extract_rpa_all: bool = False
    delete_rpyc_after_extract: bool = True

    def normalized_lang(self) -> str:
        """Имя языка в том виде, в котором его ждёт Ren'Py (tl/<lang>/)."""
        lang = (self.target_lang or DEFAULT_TARGET_LANG).strip().lower()
        return lang or DEFAULT_TARGET_LANG


#: Поддерживаемые движки перевода (для CLI/GUI/валидации).
SUPPORTED_ENGINES = (
    "google",       # бесплатный Google пачками (client=dict-chrome-ex), без ключа
    "google-free",  # то же, что google (синоним)
    "google-web",   # старый endpoint translate_a/single (часто отвечает 429)
    "google-dt",    # Google через пакет deep-translator
    "mymemory",     # MyMemory — бесплатный резерв без ключа
    "deepl",        # DeepL API (нужен ключ)
    "libre",        # LibreTranslate (свой сервер или публичный)
    "mock",         # оффлайн-заглушка для тестов
    "none",         # не переводить (использовать кэш/импорт)
)

MODES = ("tl", "replace")
