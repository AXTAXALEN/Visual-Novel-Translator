"""
Оркестратор: связывает сканирование, распаковку, разбор, перевод, сборку.

Все шаги доступны и по отдельности (их дёргает CLI и GUI), и вместе
(``Pipeline.run_full``).
"""

from __future__ import annotations

import collections
import json
import random
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Optional

from config import Settings
from core.cache import TranslationCache
from core.engine_strings import write_engine_rpy
from core.direct_replacer import ReplaceResult, replace_in_files
from core.exporter import ExportResult, export as export_text
from core.fonts import (FontReport, installed_font_rel,
                        install as install_fonts)
from core.importer import ImportResult, import_translations, translations_as_dict
from core.models import ParsedProject, TextItem
from core.placeholder import (
    TOKEN_ALT,
    TOKEN_PRIMARY,
    cleanup_translation,
    looks_translatable,
    protect,
    restore,
    verify,
)
from core.rpa_extractor import RpaExtractResult, extract_all
from core.rpyc_decompiler import DecompileReport, decompile_all
from core.rpy_parser import parse_project
from core.scanner import RenpyProject, scan_project
from core.langs import label_for
from core.quality import QualityReport, check_items, safest_translation
from core.report import build_html_report, default_report_path, write_report
from core.recovery import collect_game_translations
from core.literary import (LiteraryStore, collect_literary_translations,
                           store_for)
from core.menu_patch import MenuPatchResult, patch_image_menus
from core.screen_patcher import (WrapReport, patch_language_menus,
                                 wrap_plain_strings, write_button_screen)
from core.verify import VerifyReport, verify_project
from core.tl_writer import (GENERATED_PREFIX, TLBuildResult, build_tl,
                            clean_tl_dir, language_menu_languages, tl_status)
from core.translator import BaseEngine, TranslationError, create_engine
from core.utils import (
    OperationStats,
    Progress,
    get_logger,
    human_duration,
    read_text_file,
)

logger = get_logger(__name__)

#: Ограничение длины строки для одного запроса (Google режет на ~5000 символов).
MAX_CHARS_PER_REQUEST = 4500

#: Сколько строк подряд можно не перевести, прежде чем остановиться.
#: Защита от «тихого зависания» на лимитах Google (429).
ABORT_AFTER_FAILURES = 40

#: Медленный режим: сколько подряд пустых пакетов терпим, прежде чем сдаться.
SLOW_MODE_PATIENCE = 5
#: Стартовая пауза медленного режима и её предел.
SLOW_MODE_DELAY = 2.0
SLOW_MODE_MAX_DELAY = 60.0


#: Письменности, по которым узнаём «это уже на целевом языке».
_CYRILLIC_RE = re.compile(r"[А-Яа-яЁё]")
_LATIN_RE = re.compile(r"[A-Za-z]")


def _already_in_target_language(text: str, language: str) -> bool:
    """
    Строка уже на языке перевода и переводить её не нужно.

    Русский текст в русской сборке (например, «Начать» в кнопке, которую
    вписали мы сами) отправлять в переводчик нельзя: Google вернёт то же
    самое, но иногда с «правками» — «Ссылка», «Главное меню» и т.п. могут
    превратиться в мусор.
    """
    if (language or "") not in ("russian", "ru", "russian_f"):
        return False
    if not _CYRILLIC_RE.search(text or ""):
        return False
    # Латиница вперемешку с кириллицей — это ещё не переведённая строка
    # (например, «Привет, [name]!» — можно, а «Hello, мир» — нет).
    latin = len(_LATIN_RE.findall(text))
    cyrillic = len(_CYRILLIC_RE.findall(text))
    return cyrillic >= max(1, latin)


def _short(text: str, limit: int = 60) -> str:
    """Обрезать строку для журнала."""
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


@dataclass
class TranslateReport:
    unique_texts: int = 0
    from_cache: int = 0
    """Сколько строк взято из кэша переводов."""
    from_literary: int = 0
    """Сколько строк взято из авторского (литературного) перевода."""
    already_russian: int = 0
    """Сколько строк уже на языке перевода и в переводе не нуждаются."""
    from_game: int = 0
    """Сколько строк взято из готового перевода внутри самой игры."""
    translated: int = 0
    failed: int = 0
    skipped_untranslatable: int = 0
    elapsed: float = 0.0
    engine: str = ""
    failed_texts: list[str] = field(default_factory=list)
    recovered: int = 0
    """Сколько строк удалось дожать повторными попытками."""
    warnings: list[str] = field(default_factory=list)
    aborted: bool = False
    abort_reason: str = ""

    def summary(self) -> str:
        text = (f"перевод: {self.translated} новых, {self.from_cache} из кэша, "
                f"{self.failed} ошибок за {human_duration(self.elapsed)}")
        if self.from_literary:
            text += f" (авторских строк сохранено: {self.from_literary})"
        if self.from_game:
            text += f" (взято из игры: {self.from_game})"
        if self.already_russian:
            text += f" (уже по-русски: {self.already_russian})"
        if self.recovered:
            text += f" (дожато повторно: {self.recovered})"
        if self.aborted:
            text += " (остановлено досрочно)"
        return text


@dataclass
class FullReport:
    project: Optional[RenpyProject] = None
    parsed: Optional[ParsedProject] = None
    prepare_results: list[RpaExtractResult] = field(default_factory=list)
    decompile_report: Optional[DecompileReport] = None
    translate_report: Optional[TranslateReport] = None
    tl_result: Optional[TLBuildResult] = None
    replace_result: Optional[ReplaceResult] = None
    export_results: list[ExportResult] = field(default_factory=list)
    import_result: Optional[ImportResult] = None
    font_report: Optional[FontReport] = None
    backup_note: str = ""
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        if self.translate_report and self.translate_report.failed:
            return False
        if self.project is None:
            return False
        return True


class Pipeline:
    """Один запуск программы."""

    def __init__(self, settings: Settings,
                 progress_factory: Optional[Callable[..., Progress]] = None,
                 should_cancel: Optional[Callable[[], bool]] = None) -> None:
        self.settings = settings
        self.should_cancel = should_cancel
        self.project: Optional[RenpyProject] = None
        self.parsed: Optional[ParsedProject] = None
        self.cache = TranslationCache(settings.cache_path, settings.use_cache)
        self.stats = OperationStats()
        self._progress_factory = progress_factory or Progress
        self._active_engine: Optional[BaseEngine] = None
        #: движок подсунули снаружи (тест, отладка) — цепочку не трогаем
        self._engine_pinned: bool = False
        self._engine_name: str = (settings.engine or "google").lower()
        self._cancelled_flag = threading.Event()
        self.glossary: dict[str, str] = {}
        self.quality: Optional[QualityReport] = None
        self.verification: Optional[VerifyReport] = None
        self.last_translate_report: Optional[TranslateReport] = None
        #: строки, где потерянные плейсхолдеры пришлось дописать в конец
        self.repaired_texts: set[str] = set()
        self.last_build_result = None
        self.font_report: Optional[FontReport] = None
        #: что сделали с экранами игры (меню-картинки, обёртка строк в _())
        self.screen_notes: list[str] = []
        #: авторский (литературный) перевод: не затирается машинным
        self._literary: Optional[LiteraryStore] = None
        self._game_pairs: Optional[dict[str, str]] = None
        self.dry_run = bool(getattr(settings, "dry_run", False))
        self._cache_lock = threading.Lock()

        if settings.glossary_path:
            self._load_glossary(settings.glossary_path)

    # ------------------------------------------------------------------
    # Служебное
    # ------------------------------------------------------------------
    def _load_glossary(self, path: Path) -> None:
        try:
            data = json.loads(read_text_file(path))
            if isinstance(data, dict):
                self.glossary = {str(k): str(v) for k, v in data.items()}
                logger.info("Загружен глоссарий: %d записей", len(self.glossary))
        except Exception as exc:  # noqa: BLE001
            logger.warning("Не удалось загрузить глоссарий %s: %s", path, exc)

    # ------------------------------------------------------------------
    # Авторский (литературный) перевод
    # ------------------------------------------------------------------
    def literary(self) -> LiteraryStore:
        """Хранилище авторского перевода (загружается один раз)."""
        if self._literary is None:
            project = self.project
            if project is None:
                try:
                    project = self.scan()
                except Exception:  # noqa: BLE001
                    return LiteraryStore()
            try:
                self._literary = store_for(project, self.settings.normalized_lang())
            except Exception as exc:  # noqa: BLE001
                logger.warning("Литературный перевод недоступен: %s", exc)
                self._literary = LiteraryStore()
        return self._literary

    def game_translations(self) -> dict[str, str]:
        """
        Перевод, который уже лежит в игре (tl/<язык>/*.rpy, rt_literary.json).

        Читается один раз за прогон: он нужен, чтобы не переводить заново то,
        что пользователь уже получил в прошлый раз, даже если кэш потерян
        (перенос программы, удаление старой версии, чистка каталога).
        """
        if self._game_pairs is None:
            language = self.settings.normalized_lang()
            project = self.project if self.project is not None else None
            if project is None:
                try:
                    project = self.scan()
                except Exception as exc:  # noqa: BLE001
                    logger.warning("Игра не просканирована: %s", exc)
                    project = None
            self._game_pairs = collect_game_translations(project, language)
        return self._game_pairs

    def check_syntax(self, repair: bool = True):
        """Проверить (и починить) двоеточия у блочных строк в файлах игры."""
        from core.syntax_guard import check_project

        project = self.project if self.project is not None else self.scan()
        report = check_project(project, repair=repair)
        if report.issues:
            for issue in report.issues[:10]:
                logger.warning("синтаксис: %s", issue.describe())
        return report

    def save_literary(self, pairs: dict[str, str]) -> int:
        """Запомнить строки как авторские. Возвращает число новых."""
        store = self.literary()
        added = store.update_many(pairs)
        if added:
            store.save()
            logger.info("Литературный перевод: сохранено %d строк (%s) — "
                        "машинный перевод их больше не заменит",
                        store.count, store.path.name if store.path else "?")
        return added

    def glossary_lookup(self, text: str) -> Optional[str]:
        if not self.glossary:
            return None
        if text in self.glossary:
            return self.glossary[text]
        lowered = text.strip().lower()
        for key, value in self.glossary.items():
            if key.strip().lower() == lowered:
                return value
        return None

    def cancel(self) -> None:
        """Просьба остановиться (кнопка «Отмена» в окне)."""
        self._cancelled_flag.set()

    def cancelled(self) -> bool:
        """Пора ли останавливаться."""
        if self._cancelled_flag.is_set():
            return True
        check = self.should_cancel
        if check is None:
            return False
        try:
            return bool(check())
        except Exception:  # noqa: BLE001
            return False

    # --- активный движок -------------------------------------------------
    @property
    def _engine(self) -> Optional[BaseEngine]:
        return self._active_engine

    @_engine.setter
    def _engine(self, engine: Optional[BaseEngine]) -> None:
        """Движок, назначенный снаружи, считается единственным (без запасных)."""
        self._active_engine = engine
        self._engine_pinned = engine is not None

    def engine_chain(self) -> list[str]:
        """
        Порядок движков: основной и запасные.

        Бесплатный Google время от времени отвечает 403/429 — тогда незачем
        бросать 70 000 строк без перевода: пробуем другой движок и продолжаем
        с того места, где остановились.
        """
        if self._engine_pinned and self._active_engine is not None:
            own = getattr(self._active_engine, "name", "") or self._engine_name
            return [str(own)]
        name = (self.settings.engine or "google").lower()
        chain = [name]
        if name in ("mock", "none"):
            return chain
        for candidate in ("google", "google-web", "mymemory"):
            if candidate not in chain:
                chain.append(candidate)
        if getattr(self.settings, "libretranslate_url", "") and "libre" not in chain:
            chain.append("libre")
        return chain

    def switch_engine(self, name: str) -> BaseEngine:
        """Переключиться на другой движок на ходу."""
        engine = create_engine(name, self.settings)
        self._active_engine = engine
        self._engine_pinned = False
        self._engine_name = name
        logger.info("движок перевода переключён на «%s»", name)
        return engine

    def get_engine(self) -> BaseEngine:
        if self._active_engine is None:
            self._active_engine = create_engine(self.settings.engine, self.settings)
            self._engine_name = (self.settings.engine or "google").lower()
            logger.info("Движок перевода: %s", self._active_engine.name)
        self._active_engine.abort_check = self.cancelled
        return self._active_engine

    # ------------------------------------------------------------------
    # Шаг 1. Сканирование
    # ------------------------------------------------------------------
    def scan(self) -> RenpyProject:
        if self.settings.game_path is None:
            raise ValueError("не указан путь к игре (--game)")
        self.project = scan_project(self.settings.game_path)
        logger.info("\n%s", self.project.summary())
        for warning in self.project.warnings:
            logger.warning(warning)
        return self.project

    # ------------------------------------------------------------------
    # Шаг 2. Подготовка: .rpa -> .rpyc -> .rpy
    # ------------------------------------------------------------------
    def prepare(self, extract_all_files: Optional[bool] = None) -> tuple[list[RpaExtractResult], DecompileReport]:
        project = self.project or self.scan()
        rpa_results: list[RpaExtractResult] = []

        if project.rpa_files:
            logger.info("Найдено %d архив(ов) .rpa — распаковываю скрипты",
                        len(project.rpa_files))
            scripts_only = not (self.settings.extract_rpa_all
                                if extract_all_files is None else extract_all_files)
            rpa_results = extract_all(project.rpa_files, project.game_dir,
                                      scripts_only=scripts_only)
            for res in rpa_results:
                logger.info("  %s", res.summary())
                if res.errors:
                    for err in res.errors[:5]:
                        logger.warning("    %s", err)

            if rpa_results and any(r.extracted for r in rpa_results):
                # пересканируем, чтобы увидеть свежие .rpy/.rpyc
                self.project = project = scan_project(project.game_dir)
        else:
            logger.info("Архивов .rpa нет — пропускаю распаковку")

        report = decompile_all(
            project,
            overwrite=False,
            delete_compiled=self.settings.delete_rpyc_after_extract,
        )
        if report.unavailable:
            logger.warning("%s", report.unavailable)
        elif report.results:
            logger.info("%s", report.summary())
            for res in report.results:
                if not res.ok:
                    logger.warning("  %s", res.summary())

        if report.ok_count:
            self.project = project = scan_project(project.game_dir)

        # Декомпилятор и распаковщик тоже могут оставить блочную строку
        # без двоеточия — проверяем сразу, до перевода: иначе игра упадёт
        # на «expected ':' not found» уже после всех правок.
        if self.settings.normalized_lang():
            try:
                from core.changelog import record as _record
                from core.syntax_guard import check_project as _check_syntax

                syntax = _check_syntax(self.project, repair=True)
                if syntax.repaired:
                    logger.warning("После подготовки восстановлено двоеточий: "
                                   "%d (файлы игры снова разбираются)",
                                   syntax.repaired)
                    _record(self.project, self.settings.normalized_lang(),
                            "syntax_repair_after_prepare",
                            detail=f"двоеточий: {syntax.repaired}")
                elif not syntax.ok:
                    logger.warning("В файлах игры остались строки без "
                                   "двоеточия — игра не запустится")
            except Exception as exc:  # noqa: BLE001
                logger.warning("проверка синтаксиса после подготовки: %s", exc)

        return rpa_results, report

    # ------------------------------------------------------------------
    # Шаг 3. Разбор
    # ------------------------------------------------------------------
    def ensure_engine_strings(self) -> tuple[Optional[Path], int, str]:
        """
        Записать перевод строк самого движка (окна подтверждения и прочее).

        Строки вроде «Are you sure you want to return to the main menu?»
        живут внутри Ren'Py, а не в ``game/``, поэтому без этого файла они
        остаются английскими. Вызывается и при обычном запуске перевода, и
        из «Починить меню»: пользователь не должен помнить, какую кнопку
        нажимать.
        """
        if not getattr(self.settings, "engine_strings", True):
            return None, 0, ""
        project = self.project
        if project is None:
            return None, 0, ""
        try:
            return write_engine_rpy(project, self.settings.normalized_lang())
        except Exception as exc:  # noqa: BLE001
            logger.warning("Строки движка не добавлены: %s", exc)
            return None, 0, ""

    def patch_screens(self) -> list[str]:
        """
        Починить экраны игры до разбора строк.

        Две правки, обе — с резервной копией и обе обратимы:

        * меню, нарисованные картинками (``imagebutton auto "start_%s.png"``),
          заменяются текстовыми кнопками с русской подписью;
        * «голые» строки экранов (``text "Story by ..."``) оборачиваются
          в ``_()``, иначе Ren'Py их не переводит никогда.

        Делается до разбора, чтобы новые строки сразу попали в перевод.
        """
        notes: list[str] = []
        project = self.project or self.scan()

        if getattr(self.settings, "patch_image_menus", False):
            try:
                result = patch_image_menus(project, dry_run=self.dry_run)
                result.journal(project, self.settings.normalized_lang())
            except Exception as exc:  # noqa: BLE001
                notes.append(f"меню-картинки: не удалось ({exc})")
                logger.warning(notes[-1])
            else:
                notes.append(result.summary())
                logger.info(result.summary())
                for line in result.details[:12]:
                    logger.info("  %s", line)
                for skipped in result.skipped[:10]:
                    logger.warning("  меню-картинка без подписи: %s", skipped)

        if getattr(self.settings, "wrap_screen_text", False):
            try:
                report = wrap_plain_strings(project, dry_run=self.dry_run)
            except Exception as exc:  # noqa: BLE001
                notes.append(f"строки экранов: не удалось ({exc})")
                logger.warning(notes[-1])
            else:
                notes.append(report.summary())
                logger.info(report.summary())
                if report.backups:
                    logger.info("  оригинал сохранён: %s", report.backups[0])

        # страховка: после любой правки файлов игры не должно остаться
        # блочных строк без двоеточия (иначе игра не запустится)
        try:
            from core.syntax_guard import check_project as _check_syntax

            syntax = _check_syntax(self.project, repair=True)
            if syntax.repaired:
                notes.append(f"синтаксис: восстановлено двоеточий: "
                             f"{syntax.repaired}")
                try:
                    from core.changelog import record as _record

                    _record(self.project, self.settings.normalized_lang(),
                            "syntax_repair", detail=f"двоеточий: "
                                                   f"{syntax.repaired}")
                except Exception:  # noqa: BLE001
                    pass
            elif not syntax.ok:
                notes.append("синтаксис: есть строки без двоеточия — "
                             "игра не запустится (см. отчёт)")
        except Exception as exc:  # noqa: BLE001
            logger.warning("проверка синтаксиса не удалась: %s", exc)

        engine_path, engine_count, engine_note = self.ensure_engine_strings()
        if engine_path is not None:
            notes.append(f"строки движка Ren'Py: {engine_count} "
                         f"({engine_path.name})")
            logger.info("Строки движка Ren'Py: %d (%s)", engine_count,
                        engine_path.name)
            if engine_note:
                logger.info("  %s", engine_note)

        self.screen_notes = notes
        return notes

    def analyze(self, files: Optional[list[Path]] = None) -> ParsedProject:
        project = self.project or self.scan()
        if not project.rpy_files:
            raise RuntimeError(
                "В игре не найдено ни одного .rpy файла.\n"
                "Запустите подготовку: --prepare (распаковка .rpa + декомпиляция .rpyc)"
            )

        if getattr(self.settings, "patch_image_menus", False) or \
                getattr(self.settings, "wrap_screen_text", False):
            self.patch_screens()

        logger.info("Разбираю %d файлов...", len(project.rpy_files))
        self.parsed = parse_project(project, self.settings, files)
        stats = self.parsed.stats()
        logger.info("Извлечено строк: %s", stats)

        for warning in self.parsed.warnings[:20]:
            logger.warning(warning)

        total = stats.get("total", 0)
        if total == 0:
            logger.warning(
                "Не найдено ни одной переводимой строки. Возможно, скрипты "
                "скомпилированы (.rpyc) — запустите --prepare."
            )
        return self.parsed

    # ------------------------------------------------------------------
    # Шаг 4. Перевод
    # ------------------------------------------------------------------
    def translate(self, parsed: Optional[ParsedProject] = None,
                  show_progress: bool = True) -> TranslateReport:
        parsed = parsed or self.parsed or self.analyze()
        report = TranslateReport(engine=self.settings.engine)
        started = time.monotonic()

        items = parsed.items
        report.unique_texts = len({i.original for i in items})

        # --- что вообще переводим -------------------------------------
        pending: list[str] = []
        seen: set[str] = set()
        for item in items:
            text = item.original
            if text in seen:
                continue
            seen.add(text)
            if not looks_translatable(text):
                report.skipped_untranslatable += 1
                continue
            pending.append(text)

        # --- авторский перевод, глоссарий, кэш -------------------------
        to_translate: list[str] = []
        resolved: dict[str, str] = {}
        literary = self.literary()
        target_language = self.settings.normalized_lang()
        game_pairs = self.game_translations()
        if game_pairs:
            logger.info("Уже переведено в игре: %d строк — заново переводить "
                        "не будем", len(game_pairs))

        for text in pending:
            # 1. Авторский перевод — всегда главнее всего остального.
            literary_value = literary.get(text)
            if literary_value:
                resolved[text] = literary_value
                report.from_literary += 1
                continue
            # 2. Строка уже на языке перевода (например, «Начать» из меню,
            #    которое мы сами русифицировали): переводить её нельзя,
            #    иначе машинный переводчик испортит готовый текст.
            if _already_in_target_language(text, target_language):
                resolved[text] = text
                report.already_russian += 1
                continue
            # 3. Глоссарий пользователя.
            glossary_value = self.glossary_lookup(text)
            if glossary_value:
                resolved[text] = glossary_value
                with self._cache_lock:
                    self.cache.set(text, glossary_value)
                continue
            # 4. Готовый перевод, который уже стоит в самой игре.
            game_value = game_pairs.get(text)
            if game_value:
                resolved[text] = game_value
                report.from_game += 1
                with self._cache_lock:
                    self.cache.set(text, game_value)
                continue
            # 5. Кэш переводов.
            cached = self.cache.get(text)
            if cached:
                resolved[text] = cached
                report.from_cache += 1
                continue
            to_translate.append(text)

        logger.info("К переводу: %d строк (уже готово: %d, из них из кэша: %d, "
                    "авторских: %d)",
                    len(to_translate), len(resolved), report.from_cache,
                    report.from_literary)
        if report.from_literary:
            logger.info("Литературный перевод сохранён без изменений: %d строк",
                        report.from_literary)

        # --- сам перевод ----------------------------------------------
        if to_translate and self.settings.engine in ("none",):
            report.failed = len(to_translate)
            report.failed_texts = list(to_translate)
            report.warnings.append(
                "движок перевода отключён (--engine none): используются только "
                "кэш и глоссарий"
            )
            logger.info("Движок отключён: %d строк останутся без перевода", len(to_translate))
            to_translate = []

        if to_translate:
            workers = max(1, min(self.settings.workers, 16))
            batch = max(1, self.settings.batch_size)
            progress = self._progress_factory(
                total=len(to_translate),
                desc="Перевод",
                enabled=show_progress,
            )

            # Сколько подряд неудач считаем признаком «сервис нас блокирует».
            abort_after = max(6, min(len(to_translate), ABORT_AFTER_FAILURES))
            chain = self.engine_chain()
            failed_order: list[str] = []
            failed_set: set[str] = set()
            stop_reason = ""

            def apply_results(results: dict, chunk: list[str]) -> int:
                """Записать удачные переводы. Возвращает число удачных строк."""
                good = 0
                for text, value in results.items():
                    if value:
                        resolved[text] = value
                        report.translated += 1
                        good += 1
                        failed_set.discard(text)
                        with self._cache_lock:
                            self.cache.set(text, value)
                    elif text not in failed_set:
                        failed_set.add(text)
                        failed_order.append(text)
                progress.update(len(chunk))
                with self._cache_lock:
                    self.cache.maybe_save(200)
                return good

            try:
                for position, engine_name in enumerate(chain):
                    rest = [text for text in to_translate if text not in resolved]
                    if self.cancelled():
                        stop_reason = "остановлено пользователем"
                        break
                    if not rest:
                        break
                    if position == 0 or self._engine_pinned:
                        engine = self.get_engine()
                    else:
                        engine = self.switch_engine(engine_name)
                    engine.abort_check = self.cancelled
                    logger.info("Перевод: движок «%s», к переводу %d строк",
                                engine_name, len(rest))

                    chunks = [rest[i:i + batch]
                              for i in range(0, len(rest), batch)]
                    streak = 0
                    wave_reason = ""

                    with ThreadPoolExecutor(max_workers=workers) as pool:
                        futures = {pool.submit(self._translate_chunk, chunk): chunk
                                   for chunk in chunks}
                        for future in as_completed(futures):
                            chunk = futures[future]
                            try:
                                results = future.result()
                            except Exception as exc:  # noqa: BLE001
                                logger.warning("Сбой пакета перевода: %s", exc)
                                results = {t: None for t in chunk}

                            # Пакет не прошёл целиком (лимит, обрыв сети)? Пробуем
                            # по одной строке — так переживаем троттлинг без потерь.
                            if not any(results.values()):
                                retried = self._retry_chunk_individually(chunk)
                                if retried:
                                    results = retried

                            good = apply_results(results, chunk)
                            streak = 0 if good else streak + len(chunk)

                            if self.cancelled():
                                wave_reason = "остановлено пользователем"
                                break
                            if streak >= abort_after:
                                wave_reason = (
                                    f"движок «{engine_name}» ограничивает "
                                    f"запросы ({streak} строк подряд не "
                                    f"переведены)")
                                break
                        if wave_reason:
                            for pending_future in futures:
                                pending_future.cancel()

                    if not wave_reason:
                        # движок отработал всё, что мог: остатки — в дожим
                        break
                    if wave_reason == "остановлено пользователем":
                        stop_reason = wave_reason
                        break

                    left = [text for text in to_translate if text not in resolved]
                    next_name = (chain[position + 1]
                                 if position + 1 < len(chain) else None)
                    if next_name and left:
                        message = (f"{wave_reason} — переключаюсь на "
                                   f"«{next_name}» и продолжаю")
                        logger.warning(message)
                        report.warnings.append(message)
                        time.sleep(1.0)
                        continue
                    stop_reason = wave_reason
                    break
            finally:
                progress.close()

            report.failed_texts = [t for t in failed_order if t in failed_set]
            report.failed = len(report.failed_texts)

            if stop_reason:
                report.aborted = True
                report.abort_reason = stop_reason
                done = sum(1 for text in to_translate if text in resolved)
                logger.warning(
                    "Перевод остановлен: %s. Готово %d из %d строк "
                    "(кэш сохранён — при следующем запуске продолжим с этого места).",
                    stop_reason, done, len(to_translate))

            # --- дожим: строки, упавшие из-за сети и лимитов ----------
            if report.failed_texts and not self.cancelled():
                if report.aborted:
                    pause = min(3.0, 1.0 + report.failed / 300.0)
                    logger.warning("Сервис ограничивал запросы — пауза %.1f с, "
                                   "потом медленный дожим %d строк",
                                   pause, report.failed)
                    time.sleep(pause)
                self._drain_failed(report, resolved)
                report.failed_texts = [t for t in report.failed_texts
                                       if t not in resolved]
                report.failed = len(report.failed_texts)

                # Сервис отбивал всё подряд — не сдаёмся: замедляемся и идём
                # дальше по одной пачке. На 80 000 строк это часы работы, зато
                # без ручной возни и без потери уже переведённого.
                if report.failed_texts and report.aborted and not self.cancelled() \
                        and not self._engine_pinned \
                        and bool(getattr(self.settings, "slow_mode", True)):
                    self._slow_mode(report, resolved, chain[0])
                    report.failed_texts = [t for t in report.failed_texts
                                           if t not in resolved]
                    report.failed = len(report.failed_texts)

                if report.aborted and not report.failed:
                    report.aborted = False
                    report.abort_reason = ""

            if report.failed:
                if report.aborted:
                    report.warnings.append(
                        f"перевод остановлен досрочно ({report.abort_reason}); "
                        f"переведено {report.translated} строк, остальные "
                        "останутся на языке оригинала"
                    )
                else:
                    report.warnings.append(
                        f"{report.failed} строк не удалось перевести — они останутся "
                        "на языке оригинала"
                    )
                logger.warning(report.warnings[-1])

        # --- раздача по элементам -------------------------------------
        for item in items:
            if item.original in resolved:
                item.translation = resolved[item.original]
            elif item.is_translated():
                pass  # уже был перевод (например, после импорта)

        with self._cache_lock:
            self.cache.save()

        # --- контроль качества (v3.0) ---------------------------------
        if self.settings.quality_check:
            self.quality = self._check_quality(items, resolved, report)

        report.elapsed = time.monotonic() - started
        self.last_translate_report = report

        # Непереведённые строки сохраняем отдельным файлом — удобно для
        # повторной попытки или ручного перевода.
        if report.failed_texts:
            failed_path = Path(str(self.settings.cache_path) + ".failed.txt")
            try:
                failed_path.write_text(
                    "\n\n".join(report.failed_texts), encoding="utf-8"
                )
                logger.info("Непереведённые строки сохранены: %s", failed_path)
            except OSError:
                pass

        report.elapsed = time.monotonic() - started
        logger.info(report.summary())
        return report

    # ------------------------------------------------------------------
    def _drain_failed(self, report: "TranslateReport",
                      resolved: dict[str, str]) -> None:
        """Ещё раз перевести строки, которые не прошли с первого раза.

        Причина почти всегда одна: бесплатный сервис ограничивает частоту
        запросов (429) или моргнула сеть. Поэтому дожимаем терпеливо:
        сначала маленькими пакетами, потом по одной строке, с паузами.
        Всё, что удалось, дописывается в кэш — повторный запуск уже не нужен.
        """
        rounds = max(0, int(getattr(self.settings, "retry_rounds", 3) or 0))
        if not rounds:
            return
        pending = [t for t in report.failed_texts if t not in resolved]
        if not pending:
            return

        if report.aborted:
            # Сервис только что отбивал всё подряд. Прежде чем тратить время на
            # сотни одинарных запросов, проверяем на трёх строках: ожил ли он.
            probe = pending[:3]
            alive = 0
            for text in probe:
                if self.cancelled():
                    return
                try:
                    results = self._translate_chunk([text])
                except Exception:  # noqa: BLE001
                    results = {}
                for value in results.values():
                    if value:
                        alive += 1
                time.sleep(0.4)
            if alive == 0:
                logger.warning("Сервис всё ещё не отвечает — оставляю %d строк "
                               "на следующий запуск", len(pending))
                return
            logger.info("Сервис ожил (%d из %d пробных строк) — продолжаю дожим",
                        alive, len(probe))

        total_recovered = 0
        for index in range(rounds):
            if not pending or self.cancelled():
                break
            size = 10 if index == 0 else (3 if index == 1 else 1)
            size = max(1, min(size, int(self.settings.batch_size or 25)))
            if report.aborted:
                size = 1  # после лимита — только по одной строке
            pause = 0.4 * (index + 1) * (1.2 if report.aborted else 1.0)
            logger.info("Дожим, попытка %d из %d: %d строк, пакеты по %d, "
                        "пауза %.1f с", index + 1, rounds, len(pending),
                        size, pause)
            recovered: list[str] = []
            for start in range(0, len(pending), size):
                if self.cancelled():
                    break
                chunk = pending[start:start + size]
                try:
                    results = self._translate_chunk(chunk)
                except Exception as exc:  # noqa: BLE001
                    logger.debug("Дожим: пакет не прошёл (%s)", exc)
                    results = {}
                for text, value in results.items():
                    if not value:
                        continue
                    resolved[text] = value
                    recovered.append(text)
                    with self._cache_lock:
                        self.cache.set(text, value)
                if pause:
                    time.sleep(pause)
                with self._cache_lock:
                    self.cache.maybe_save(200)

            if recovered:
                total_recovered += len(recovered)
                pending = [t for t in pending if t not in resolved]
                logger.info("Дожим: удалось перевести ещё %d строк (осталось %d)",
                            len(recovered), len(pending))
            else:
                # не помогло — дальше паузы только удлиняются, смысла нет
                break

        if total_recovered:
            done = set(resolved)
            report.failed_texts = [t for t in report.failed_texts if t not in done]
            report.failed = len(report.failed_texts)
            report.translated += total_recovered
            report.recovered = total_recovered
            logger.info("Дожим завершён: +%d строк (всего переведено %d)",
                        total_recovered, report.translated)

    def _slow_mode(self, report: "TranslateReport",
                   resolved: dict[str, str], engine_name: str) -> None:
        """Последний рубеж: один поток, длинные паузы, продолжение с места.

        Бесплатные сервисы не «ломаются» навсегда — они просят сбавить темп.
        Поэтому вместо остановки переходим в медленный режим: по одной пачке,
        с паузой между запросами и удвоением паузы на каждой неудаче. Сдаёмся
        только если сервис не пускает подряд :data:`SLOW_MODE_PATIENCE` раз.
        """
        pending = [t for t in report.failed_texts if t not in resolved]
        if not pending:
            return

        if not self._engine_pinned:
            try:
                self.switch_engine(engine_name)
            except Exception as exc:  # noqa: BLE001
                logger.debug("Медленный режим: не удалось вернуть движок «%s» (%s)",
                             engine_name, exc)

        base_delay = max(SLOW_MODE_DELAY,
                         float(getattr(self.settings, "request_delay", 0.0) or 0.0))
        old_delay = self.settings.request_delay
        size = max(1, min(int(self.settings.batch_size or 20), 25))
        message = (f"включаю медленный режим: {len(pending)} строк, один поток, "
                   f"пауза {base_delay:.1f} с — это долго, но без потерь")
        logger.warning(message)
        report.warnings.append(message)

        delay = base_delay
        empty_in_row = 0
        recovered = 0
        # Неудачные пачки не выбрасываем: после паузы возвращаем в очередь,
        # чтобы на медленном темпе их всё-таки перевести.
        queue = collections.deque(range(0, len(pending), size))
        tries: dict[int, int] = {}
        try:
            while queue:
                if self.cancelled():
                    break
                start = queue.popleft()
                chunk = pending[start:start + size]
                self.settings.request_delay = delay
                try:
                    results = self._translate_chunk(chunk)
                except Exception as exc:  # noqa: BLE001
                    logger.debug("Медленный режим: пакет не прошёл (%s)", exc)
                    results = {}
                good = 0
                for text, value in results.items():
                    if not value:
                        continue
                    resolved[text] = value
                    report.translated += 1
                    recovered += 1
                    good += 1
                    with self._cache_lock:
                        self.cache.set(text, value)
                with self._cache_lock:
                    self.cache.maybe_save(200)

                if good:
                    empty_in_row = 0
                    delay = max(base_delay, delay * 0.8)
                    done = sum(1 for text in pending if text in resolved)
                    if done % 100 < good:
                        logger.info("Медленный режим: %d из %d строк готово",
                                    done, len(pending))
                    continue

                empty_in_row += 1
                delay = min(SLOW_MODE_MAX_DELAY, delay * 2)
                if empty_in_row >= SLOW_MODE_PATIENCE:
                    logger.warning(
                        "Сервис не пускает даже в медленном режиме — "
                        "оставляю %d строк на следующий запуск (кэш сохранён)",
                        len(pending) - recovered)
                    break
                tries[start] = tries.get(start, 0) + 1
                if tries[start] < 2:
                    queue.append(start)      # ещё один шанс на новом темпе
                pause = min(SLOW_MODE_MAX_DELAY, 2.0 ** empty_in_row)
                logger.info("Медленный режим: пусто подряд %d — пауза %.0f с, "
                            "дальше пауза запросов %.1f с", empty_in_row, pause, delay)
                time.sleep(pause)
        finally:
            self.settings.request_delay = old_delay

        if recovered:
            done = set(resolved)
            report.failed_texts = [t for t in report.failed_texts if t not in done]
            report.failed = len(report.failed_texts)
            if recovered > getattr(report, "recovered", 0):
                report.recovered = recovered
            message = (f"медленный режим дал ещё {recovered} строк "
                       f"(осталось {report.failed})")
            logger.info(message)

    def _retry_chunk_individually(self, chunk: list[str],
                                  pause: float = 0.0) -> dict[str, Optional[str]]:
        """Повторить пакет по одной строке — спасает при троттлинге."""
        limit = min(len(chunk), 40)
        logger.info("Пакет не переведён целиком — пробую по одной строке (%d)",
                    limit)
        out: dict[str, Optional[str]] = {}
        try:
            engine = self.get_engine().clone()
            engine.abort_check = self.cancelled
        except Exception as exc:  # noqa: BLE001
            logger.debug("Повтор по одной строке недоступен: %s", exc)
            return out
        for index, text in enumerate(chunk[:limit]):
            if self.cancelled():
                break
            try:
                value = engine.translate(text, self.settings.source_lang,
                                         self.settings.normalized_lang())
            except Exception as exc:  # noqa: BLE001
                logger.debug("Строка не переведена (%s): %s", text[:40], exc)
                value = None
            if value:
                out[text] = value
            if pause:
                time.sleep(pause)
            elif index and index % 10 == 0:
                time.sleep(0.3)
        logger.info("По одной строке удалось перевести %d", len(out))
        return out

    def _translate_chunk(self, chunk: list[str]) -> dict[str, Optional[str]]:
        """Переводит пакет строк с защитой служебных конструкций."""
        engine = self.get_engine().clone()
        engine.abort_check = self.cancelled
        results: dict[str, Optional[str]] = {}

        masked_map: dict[str, object] = {}
        masked_texts: list[str] = []

        for text in chunk:
            pieces = self._split_long_text(text)
            # защищаем служебные вставки в каждой части
            protected_parts = [protect(piece, TOKEN_PRIMARY) for piece in pieces]
            masked = [p.masked for p in protected_parts]
            masked_map[text] = (pieces, protected_parts)
            masked_texts.append("\x00".join(masked))

        try:
            translations = engine.translate_batch(masked_texts)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Движок не смог перевести пакет из %d строк: %s",
                           len(chunk), exc)
            translations = [None] * len(masked_texts)

        for text, translated in zip(chunk, translations):
            pieces, protected_parts = masked_map[text]  # type: ignore[misc]
            results[text] = self._restore_translation(
                engine, text, pieces, protected_parts, translated
            )

        return results

    # ------------------------------------------------------------------
    def _restore_translation(self, engine: BaseEngine, original: str,
                             pieces: list[str], protected_parts: list,
                             translated: Optional[str]) -> Optional[str]:
        """Собирает перевод обратно: снимает маски, при потере — повторяет."""
        if translated is None:
            return None

        translated_parts = translated.split("\x00")
        if len(translated_parts) != len(pieces):
            # движок «потерял» разделитель — переводим поштучно
            translated_parts = []
            for piece, prot in zip(pieces, protected_parts):
                try:
                    single = engine.translate_one(prot.masked)
                except TranslationError:
                    return None
                translated_parts.append(single)

        restored: list[str] = []
        for part, prot in zip(translated_parts, protected_parts):
            if not verify(part, prot):
                # пробуем альтернативный шаблон токенов
                alt = protect(pieces[len(restored)], TOKEN_ALT)
                try:
                    retry = engine.translate_one(alt.masked)
                except TranslationError:
                    retry = None
                if retry is not None and verify(retry, alt):
                    part = retry
                    prot = alt
                else:
                    logger.debug("Плейсхолдеры повреждены в строке: %.60r", original)
            res = restore(part, prot, repair=True)
            if res.repaired:
                self.repaired_texts.add(original)
            restored.append(res.text)

        result = "".join(restored)
        result = cleanup_translation(result)

        if not result.strip():
            return None
        return result

    # ------------------------------------------------------------------
    # Границы предложений: европейская пунктуация вместе с пробелом после неё
    # либо китайские/японские знаки «。！？；…」 — там пробела нет вообще.
    _SENTENCE_SPLIT = re.compile(r"(?:[.!?…;]+\s+|[。！？；…」』〕】]+\s*)")
    _SOFT_BREAKS = "，、,;；:：)） ]】 "
    _CJK_END = "。！？；…」』〕】"

    @classmethod
    def _split_long_text(cls, text: str) -> list[str]:
        """Очень длинные строки режем по предложениям (лимит API).

        Важно: разделители (пробелы после точки) остаются в кусках, поэтому
        склейка переводов обратно даёт ровно исходный текст. Работает и для
        языков без пробелов: китайский и японский режутся по «。！？；», а не
        по произвольному символу — иначе движок получает обрывок фразы.
        """
        if len(text) <= MAX_CHARS_PER_REQUEST:
            return [text]

        sentences: list[str] = []
        start = 0
        for match in cls._SENTENCE_SPLIT.finditer(text):
            sentences.append(text[start:match.end()])
            start = match.end()
        if start < len(text):
            sentences.append(text[start:])
        if not sentences:
            return cls._hard_split(text)

        chunks: list[str] = []
        current = ""
        for sentence in sentences:
            if len(current) + len(sentence) <= MAX_CHARS_PER_REQUEST:
                current += sentence
            else:
                if current:
                    chunks.append(current)
                current = sentence
        if current:
            chunks.append(current)

        # совсем «монолитные» куски (текст без единого знака препинания)
        final: list[str] = []
        for chunk in chunks:
            final.extend(cls._hard_split(chunk))
        return [part for part in final if part.strip()]

    @classmethod
    def _hard_split(cls, chunk: str) -> list[str]:
        """Режет кусок без знаков препинания, не разрывая [..], {..} и %%s."""
        out: list[str] = []
        limit = MAX_CHARS_PER_REQUEST
        while len(chunk) > limit:
            cut = limit
            head = chunk[:limit]
            # не разрываем подстановки [имя] и теги {b}: отступим к их началу
            for opener, closer in (("[", "]"), ("{", "}")):
                if head.count(opener) > head.count(closer):
                    pos = head.rfind(opener)
                    if pos > 0:
                        cut = min(cut, pos)
            # ещё лучше — разрезать по запятой/пробелу рядом
            window = chunk[max(0, cut - 60):cut]
            for index in range(len(window) - 1, -1, -1):
                if window[index] in cls._SOFT_BREAKS:
                    cut = max(0, cut - len(window) + index + 1)
                    break
            if cut <= 0:
                cut = limit
            out.append(chunk[:cut])
            chunk = chunk[cut:]
        if chunk:
            out.append(chunk)
        return out

    # ------------------------------------------------------------------
    # Шаг 5. Сборка
    # ------------------------------------------------------------------
    # ------------------------------------------------------------------
    @staticmethod
    def _log_quality_details(quality, limit: int = 10) -> None:
        """Показать в журнале конкретные проблемные строки (файл:строка)."""
        errors = quality.errors
        warnings = quality.warnings
        if not errors and not warnings:
            return
        for issue in (errors + warnings)[:limit]:
            where = issue.where or "—"
            logger.warning("  %s: %s — %s — «%s»", issue.label(), where,
                           _short(issue.original, 60),
                           _short(issue.translation, 60))
        rest = len(errors) + len(warnings) - limit
        if rest > 0:
            logger.warning("  …и ещё %d замечаний — полный список в HTML-отчёте",
                           rest)

    def _check_quality(self, items, resolved: dict[str, str],
                       report: TranslateReport) -> QualityReport:
        """Проверить перевод и, если нужно, починить или откатить строки."""
        language = self.settings.normalized_lang()
        quality = check_items(items, language, self.glossary or None,
                              repaired=self.repaired_texts)
        logger.info(quality.summary())

        retry = quality.retry_texts() if self.settings.quality_retry else []
        if retry and self.settings.engine not in ("none", ""):
            logger.info("Перепроверяю проблемные строки: %d", len(retry))
            fixed = self._retry_texts(retry)
            if fixed:
                for text, value in fixed.items():
                    resolved[text] = value
                    with self._cache_lock:
                        self.cache.set(text, value)
                quality.retried = len(fixed)
                quality = check_items(items, language, self.glossary or None,
                                      repaired=self.repaired_texts)

        self._log_quality_details(quality)

        if self.settings.quality_fallback:
            fatal = quality.fatal_texts()
            if fatal:
                dropped = 0
                for item in items:
                    if item.original in fatal:
                        item.translation = ""
                        resolved.pop(item.original, None)
                        dropped += 1
                quality.repaired = dropped
                note = (f"{dropped} строк с потерянными плейсхолдерами оставлены "
                        "на языке оригинала — иначе сломалась бы разметка игры")
                report.warnings.append(note)
                logger.warning(note)

        if quality.errors:
            report.warnings.append(
                f"качество: {len(quality.errors)} строк с ошибками, "
                f"{len(quality.warnings)} предупреждений (подробности — в отчёте)")
        return quality

    def _retry_texts(self, texts: list[str]) -> dict[str, str]:
        """Повторно перевести строки (по одной, без пакетов)."""
        result: dict[str, str] = {}
        if not texts:
            return result
        try:
            engine = self.get_engine()
        except Exception as exc:  # noqa: BLE001
            logger.warning("Повторная проверка невозможна: %s", exc)
            return result
        for text in texts[:200]:
            if self.cancelled():
                break
            try:
                value = engine.translate(text, self.settings.source_lang,
                                         self.settings.normalized_lang())
            except Exception as exc:  # noqa: BLE001
                logger.debug("Повтор не удался (%s): %s", text[:40], exc)
                continue
            if value and value.strip():
                result[text] = value
        logger.info("Повторная попытка: исправлено %d из %d", len(result), len(texts))
        return result

    # ------------------------------------------------------------------
    def write_report(self, path: Optional[Path] = None,
                     translate_report: Optional[TranslateReport] = None) -> Path:
        """Сохранить HTML-отчёт о последнем прогоне."""
        project = self.project
        target = Path(path) if path else default_report_path(
            project.project_root if project else None)
        return write_report(
            target,
            game_name=project.name if project else "—",
            game_path=str(project.game_dir) if project else "—",
            language=self.settings.normalized_lang(),
            translate_report=translate_report or self.last_translate_report,
            quality=self.quality,
            verify=self.verification,
            build_result=self.last_build_result,
            font_report=self.font_report,
            items=self.parsed.items if self.parsed else None,
        )

    def report_html(self) -> str:
        """HTML-отчёт строкой (для предпросмотра в интерфейсе)."""
        project = self.project
        return build_html_report(
            game_name=project.name if project else "—",
            game_path=str(project.game_dir) if project else "—",
            language=self.settings.normalized_lang(),
            translate_report=self.last_translate_report,
            quality=self.quality,
            verify=self.verification,
            build_result=self.last_build_result,
            font_report=self.font_report,
            items=self.parsed.items if self.parsed else None,
        )

    def build(self, parsed: Optional[ParsedProject] = None,
              mode: Optional[str] = None) -> tuple[Optional[TLBuildResult], Optional[ReplaceResult]]:
        parsed = parsed or self.parsed
        project = self.project
        if parsed is None or project is None:
            raise RuntimeError("сначала выполните разбор проекта (analyze)")

        mode = (mode or self.settings.mode or "tl").lower()
        language = self.settings.normalized_lang()

        if mode == "replace":
            logger.info("Режим прямой замены текста в .rpy...")
            result = replace_in_files(
                project,
                parsed.items,
                delete_rpyc=self.settings.delete_stale_rpyc,
                dry_run=self.settings.dry_run,
            )
            logger.info(result.summary())
            for warning in result.warnings[:10]:
                logger.warning(warning)
            self.font_report = install_fonts(project, parsed, language,
                                             mode=self.settings.font_mode,
                                             build_mode="replace")
            return None, result

        logger.info("Генерация game/tl/%s/ ...", language)
        if not self.settings.keep_orphan_translations:
            removed = clean_tl_dir(project, language)
            if removed:
                logger.info("Удалено старых файлов нашей генерации: %d", len(removed))

        tl_result = build_tl(
            project,
            parsed.items,
            language=language,
            set_default_language=self.settings.set_default_language,
            force_language=self.settings.force_language,
            hook_fallback=self.settings.hook_fallback,
        )
        logger.info(tl_result.summary())
        for warning in tl_result.warnings[:10]:
            logger.warning(warning)

        # Меню «Язык» в чужой игре почти всегда прописано вручную
        # (English / Simplified Chinese), нашего пункта там нет.
        has_lang_menu, offered = language_menu_languages(project)
        if has_lang_menu and language not in offered:
            if self.settings.set_default_language or self.settings.force_language:
                logger.info(
                    "В меню игры «Язык» (%s) пункта «%s» нет — язык включён "
                    "автоматически файлом %s_language.rpy.",
                    ", ".join(sorted(offered)) or "только English",
                    language, GENERATED_PREFIX)
            else:
                tl_result.warnings.append(
                    f"в меню игры «Язык» нет пункта «{language}» "
                    f"(там: {', '.join(sorted(offered)) or 'только English'}). "
                    "Поставьте галочку "
                    "«Включать язык автоматически» и соберите файлы ещё раз — "
                    "иначе переключить язык в игре будет нечем.")
                logger.warning(tl_result.warnings[-1])

        # Своя кнопка-переключатель: не трогает файлы игры и работает даже
        # когда screens.rpy есть только в виде .rpyc.
        if self.settings.language_button:
            path = write_button_screen(project, language, force=True,
                                       label_native=label_for(language))
            logger.info("Кнопка переключения языка: tl/%s/%s", language,
                        path.name if path else "?")

        # Строки самого движка (окна подтверждения, быстрые сохранения):
        # их нет ни в game/, ни в наших tl-файлах, поэтому без этого шага
        # «вернуться в главное меню?» остаётся английским.
        if getattr(self.settings, "engine_strings", True):
            engine_path, engine_count, engine_note = self.ensure_engine_strings()
            if engine_path is not None:
                logger.info("Строки движка Ren'Py: %d (%s)", engine_count,
                            engine_path.name)
            if engine_note:
                logger.info("  %s", engine_note)

        # Шрифты ставим до правки меню: тогда пункт «Русский» можно сразу
        # привязать к шрифту, в котором гарантированно есть кириллица.
        font_report = install_fonts(project, parsed, language,
                                    mode=self.settings.font_mode, build_mode="tl")
        self.font_report = font_report
        for warning in font_report.warnings:
            logger.warning(warning)

        # Опционально: вписать пункт прямо в меню игры (screens.rpy).
        if self.settings.patch_language_menu:
            try:
                font_override = installed_font_rel(project, language)
            except Exception:  # noqa: BLE001
                font_override = None
            done, patch_warnings = patch_language_menus(
                project, language, label_for(language),
                font_override=font_override)
            for message in done:
                logger.info(message)
            for message in patch_warnings:
                logger.warning(message)
                tl_result.warnings.append(message)

        self.last_build_result = tl_result

        if self.settings.verify_after_build:
            try:
                self.verification = verify_project(project, language, parsed)
                logger.info(self.verification.summary())
                for issue in self.verification.errors[:10]:
                    message = f"проверка tl: {issue.message} ({issue.where()})"
                    tl_result.warnings.append(message)
                    logger.warning(message)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Проверка tl не удалась: %s", exc)

        logger.info("Состояние каталога: %s", tl_status(project, language))
        return tl_result, None

    # ------------------------------------------------------------------
    # Экспорт / импорт
    # ------------------------------------------------------------------
    def _draft_translations(self) -> dict[str, str]:
        """Черновик для экспорта: авторский перевод, переведённое, кэш."""
        drafts: dict[str, str] = {}
        literary = self.literary() if self.project is not None else LiteraryStore()
        for item in (self.parsed.items if self.parsed else []):
            saved = literary.get(item.original)
            if saved:
                drafts[item.original] = saved
                continue
            if item.translation and item.translation.strip():
                drafts[item.original] = item.translation
        game_pairs = self.game_translations()
        for item in (self.parsed.items if self.parsed else []):
            if item.original in drafts:
                continue
            from_game = game_pairs.get(item.original)
            if from_game:
                drafts[item.original] = from_game
                continue
            cached = self.cache.get(item.original)
            if cached:
                drafts[item.original] = cached
        return drafts

    def export(self, fmt: str = "txt", output: Optional[Path] = None,
               include_context: bool = True,
               split: int = 0) -> ExportResult:
        parsed = self.parsed or self.analyze()
        project = self.project or self.scan()
        result = export_text(project, parsed, fmt, output, include_context,
                             translations=self._draft_translations(),
                             split=split)
        logger.info(result.summary())
        return result

    def export_all(self, base: Optional[Path] = None,
                   with_json: bool = False,
                   with_docx: bool = False,
                   split: int = 0) -> list[ExportResult]:
        """
        Экспорт текста для агента.

        По умолчанию — **только TXT** (при ``split`` — частями). JSON и DOCX
        включаются явно: на новеллах в 100 000+ строк именно они приводили
        к зависанию и вылету.
        """
        from core.exporter import export_both

        parsed = self.parsed or self.analyze()
        project = self.project or self.scan()
        base = base or project.project_root / "renpy_translator_export"
        drafts = self._draft_translations()
        logger.info("Черновой машинный перевод в файле: %d строк", len(drafts))

        def progress(done: int, total: int) -> None:
            logger.info("  записано %d из %d строк…", done, total)

        results = export_both(project, parsed, base,
                              translations=drafts, split=split,
                              with_json=with_json, with_docx=with_docx,
                              progress=progress)
        for res in results:
            logger.info(res.summary())
            for warning in res.warnings:
                logger.warning(warning)
        return results

    def import_file(self, path: Path, fill_from_cache: bool = True,
                    merge: bool = True) -> ImportResult:
        """
        Импорт перевода + «добор» недостающих строк из кэша.

        Благодаря этому импорт аддитивен: если в файле переведена только часть
        строк, остальные берутся из кэша (например, после автоперевода).
        """
        parsed = self.parsed or self.analyze()
        result = import_translations(path, parsed)

        if fill_from_cache and self.cache.enabled:
            filled = 0
            for item in parsed.items:
                if item.is_translated():
                    continue
                cached = self.cache.get(item.original)
                if cached:
                    item.translation = cached
                    filled += 1
            if filled:
                result.warnings.append(
                    f"{filled} строк добраны из кэша переводов"
                )
                logger.info("Из кэша добрано строк: %d", filled)

        # Импортированные строки — авторские: сохраняем их отдельным файлом
        # (game/tl/<язык>/rt_literary.json), чтобы НИ ОДИН следующий запуск
        # не заменил их машинным переводом.
        imported = translations_as_dict(parsed)
        # В авторское хранилище — ТОЛЬКО то, что реально было в файле:
        # иначе туда уедет и машинный перевод, подставленный из кэша.
        from_file = getattr(result, "pairs", None) or {}
        added = self.save_literary(from_file)
        if added and hasattr(result, "warnings"):
            result.warnings.append(
                f"авторский перевод сохранён: {added} строк в "
                f"{self.literary().path.name if self.literary().path else '—'} — "
                "машинный перевод их больше не тронет")
        # и в общий кэш (для отчётов и экспорта)
        with self._cache_lock:
            self.cache.update(imported)
            self.cache.save()
        return result

    # ------------------------------------------------------------------
    # Полный цикл
    # ------------------------------------------------------------------
    def run_full(self, do_export: bool = False,
                 export_format: str = "json",
                 export_output: Optional[Path] = None) -> FullReport:
        report = FullReport()

        # 1. сканирование
        report.project = self.scan()

        # 2. подготовка (если нужна)
        if report.project.needs_preparation:
            logger.info("Требуется подготовка (.rpa/.rpyc) — выполняю")
            rpa_results, decompile_report = self.prepare()
            report.prepare_results = rpa_results
            report.decompile_report = decompile_report
            report.project = self.project

        # 3. бэкап
        if self.settings.backup:
            from core.backup import create_backup

            backup = create_backup(
                report.project, extra_paths=[self.settings.cache_path]
            )
            report.backup_note = backup.summary()
            logger.info(backup.summary())

        # 3.5 страховка: битое `menu` из прошлых правок чиним ДО разбора,
        # иначе игра не запустится даже при удачном переводе
        syntax = self.check_syntax(repair=True)
        if syntax.repaired:
            report.warnings.append(
                f"в файлах игры восстановлено двоеточий: {syntax.repaired}")
            logger.info("синтаксис файлов игры: восстановлено двоеточий: %d",
                        syntax.repaired)

        # 4. разбор
        report.parsed = self.analyze()

        # 5. экспорт (опционально)
        if do_export:
            report.export_results = [
                self.export(export_format, export_output)
            ]

        # 6. перевод (глоссарий и кэш работают и при --engine none)
        report.translate_report = self.translate(report.parsed)

        # 7. сборка
        tl_result, replace_result = self.build(report.parsed)
        report.tl_result = tl_result
        report.replace_result = replace_result
        report.font_report = self.font_report

        report.warnings.extend(self.parsed.warnings if self.parsed else [])
        logger.info("Готово. %s", self.stats.summary())
        return report
