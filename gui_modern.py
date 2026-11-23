"""
Современный интерфейс RenPyTranslator (v3.1) на customtkinter.

Почему отдельный модуль: старый интерфейс на ``ttk`` остаётся как страховка —
если customtkinter почему-то не загрузится (старая Windows, урезанная сборка),
``gui.run_gui`` тихо откатится на него, и программа всё равно запустится.

Что здесь есть:

* два режима оформления (тёмный/светлый) с мгновенным переключением;
* крупная кнопка «Сделать всё автоматически» и понятные карточки;
* выбор языка перевода (Русский · russian и десятки других);
* прогресс с процентом, скоростью и оценкой остатка;
* сводка прогона: строки, перевод, качество, покрытие, блоки;
* вкладки: литературный текст, проверка/отчёт, журнал;
* кнопки проверки перевода, HTML-отчёта и отката резервной копии;
* журнал с уровнями, копированием и очисткой;
* горячие клавиши.
"""

from __future__ import annotations

import logging
import os
import queue
import sys
import threading
import time
import traceback
import webbrowser
from pathlib import Path
from typing import Any, Callable, Optional

import tkinter as tk
from tkinter import filedialog, messagebox

import customtkinter as ctk

from config import APP_NAME, VERSION, default_cache_path, default_log_path
from core.appconfig import UserConfig
from core.langs import LANGUAGE_LABELS, label_for
from core.pipeline import Pipeline
from core.scanner import ProjectError, scan_project
from core.utils import human_duration, get_logger

logger = get_logger(__name__)

APPEARANCE = ("dark", "light", "system")
LANG_CODES = tuple(sorted(LANGUAGE_LABELS, key=lambda code: label_for(code)))


def _lang_label(code: str) -> str:
    return f"{label_for(code)} · {code}"


PROMPT_FOR_AGENT = """Ты — профессиональный литературный переводчик визуальной новеллы
на движке Ren'Py. Переводишь с языка оригинала на русский язык.

Перед тобой файл renpy_translator_export_*.txt (или .docx), выгруженный из игры.
В нём на каждую строку есть:

  [#файл:строка] (тип · персонаж · сцена: метка)   — кто говорит и где
  # до: / # после:   — соседние реплики: что сказано раньше и что будет дальше
  EN:   оригинал (НЕ МЕНЯТЬ)
  MT:   черновой машинный перевод — это основа для адаптации, а не образец
  RU:   сюда пишешь свой литературный перевод

Если в файле поля RU: пустые — это нормально и не требует уточнений:
переводи по полю EN:, а MT: используй как черновик. Ничего не переспрашивай.

ПРАВИЛА
1. Заполняй RU: под каждой строкой EN:. Строка EN — строка RU, не склеивай
   и не разбивай реплики, порядок строк не меняй.
2. Переводи как живой литературный диалог: сохраняй характер персонажа,
   обращение (ты/вы), шутки, иронию и ритм. Не калькируй порядок слов.
3. Ориентируйся на контекст: «# до:» и «# после:» показывают, что уже сказано
   и к чему ведёт реплика; «сцена:» — метка главы; имена персонажей — в
   справке «ПЕРСОНАЖИ» в начале файла. Имена переводи единообразно по всей
   новелле (например, Atticus → Аттикус, Lucius → Люциус).
4. Служебное переноси дословно и без изменений:
   {теги} — {b}, {i}, {color=#ff0000}, {w=0.5}, {size=+4}, {/b};
   [переменные] — [player_name], [points];
   %s, %d, %1$s, \\n, \\\\ .
5. Тип «интерфейс» — надписи меню и кнопок: короткие, в стиле игровых меню
   («Start» → «Начать», «Q.Save» → «Б. сохр.»), без точки в конце.
   Тип «имя» — отображаемое имя персонажа.
   Тип «выбор» — варианты ответа игрока.
6. Комментарии «#» не удаляй: в них контекст, по нему программа сопоставляет
   строки. Номера [#файл:строка], строки EN: и заголовки === … === не меняй.
7. Не добавляй от себя реплик, не сокращай и не цензурируй текст.
8. Ответ — тот же самый файл целиком, с заполненными RU:. Формат .txt
   (или .docx, если работал в Word). JSON присылать не нужно.
"""
TEXT_MAIN = ("#1f2430", "#e6e8ee")
TEXT_MUTED = ("#5a6478", "#98a2b3")
BORDER = ("#c7cdd8", "#39404d")


def outline(parent, text: str, command=None, width: int = 120,
            height: int = 34, **kwargs) -> "ctk.CTkButton":
    """Кнопка «в рамке»: в светлой теме тоже читаемая (белый текст на белом)."""
    return ctk.CTkButton(
        parent, text=text, command=command, width=width, height=height,
        fg_color="transparent", border_width=1, border_color=BORDER,
        text_color=TEXT_MAIN, hover_color=("#e8ecf4", "#2b3140"),
        font=ctk.CTkFont(size=12), **kwargs)


def _code_from_label(text: str) -> str:
    if "·" in text:
        return text.split("·")[-1].strip() or "russian"
    return text.strip() or "russian"


# ===========================================================================
class App(ctk.CTk):
    """Главное окно (современный вид)."""

    def __init__(self, initial=None) -> None:
        super().__init__()

        self.cfg = UserConfig.load()
        if initial is not None and getattr(initial, "game_path", None):
            self.cfg.last_game_path = str(initial.game_path)

        ctk.set_appearance_mode(self.cfg.appearance or "dark")
        ctk.set_default_color_theme("blue")

        self.title(f"{APP_NAME} {VERSION} — русификатор визуальных новелл Ren'Py")
        self.geometry("1180x880")
        self.minsize(1060, 720)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        self.pipeline: Optional[Pipeline] = None
        self.project = None
        self._worker: Optional[threading.Thread] = None
        self._cancel = threading.Event()
        self._busy = False
        self._busy_started = 0.0
        self._busy_title = ""
        self._ui_queue: "queue.Queue[Callable[[], None]]" = queue.Queue()
        self._log_queue: "queue.Queue[tuple[str, str]]" = queue.Queue()
        self._last_progress = ""
        self._last_export_dir: Optional[Path] = None

        self._build()
        self._bind_shortcuts()
        self._install_log_handler()
        self._pump()

        if self.cfg.last_game_path:
            self.var_path.set(self.cfg.last_game_path)
            self.after(300, self.on_scan)

        self.log("Добро пожаловать! Выберите папку игры — дальше всё сделает "
                 "программа.", "info")

    # ------------------------------------------------------------------
    # Построение интерфейса
    # ------------------------------------------------------------------
    def _build(self) -> None:
        self.grid_columnconfigure(0, weight=0, minsize=372)
        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(1, weight=1)

        self._build_header()

        leftbox = ctk.CTkFrame(self, width=352, corner_radius=0,
                               fg_color="transparent")
        leftbox.grid(row=1, column=0, sticky="nsew", padx=(14, 6), pady=(8, 6))
        leftbox.grid_columnconfigure(0, weight=1)
        leftbox.grid_rowconfigure(0, weight=1)

        left = ctk.CTkScrollableFrame(leftbox, width=352, corner_radius=0,
                                      fg_color="transparent")
        left.grid(row=0, column=0, sticky="nsew")
        left.grid_columnconfigure(0, weight=1)
        self._build_left(left)

        pinned = ctk.CTkFrame(leftbox, corner_radius=0, fg_color="transparent")
        pinned.grid(row=1, column=0, sticky="ew", pady=(6, 0))
        pinned.grid_columnconfigure(0, weight=1)
        self._build_progress(pinned)

        right = ctk.CTkFrame(self, corner_radius=14)
        right.grid(row=1, column=1, sticky="nsew", padx=(6, 14), pady=(8, 6))
        right.grid_columnconfigure(0, weight=1)
        right.grid_rowconfigure(0, weight=1)
        self._build_tabs(right)

        self._build_footer()

    # --- шапка ---------------------------------------------------------
    def _build_header(self) -> None:
        header = ctk.CTkFrame(self, corner_radius=0, height=76)
        header.grid(row=0, column=0, columnspan=2, sticky="ew")
        header.grid_columnconfigure(1, weight=1)

        titles = ctk.CTkFrame(header, fg_color="transparent")
        titles.grid(row=0, column=0, sticky="w", padx=(18, 0), pady=12)
        ctk.CTkLabel(titles, text=APP_NAME,
                     font=ctk.CTkFont(size=21, weight="bold")).pack(anchor="w")
        ctk.CTkLabel(titles, text="Русификатор визуальных новелл на Ren'Py",
                     font=ctk.CTkFont(size=12), text_color=("#5a6478", "#98a2b3")
                     ).pack(anchor="w")

        right = ctk.CTkFrame(header, fg_color="transparent")
        right.grid(row=0, column=2, sticky="e", padx=(0, 18), pady=12)

        ctk.CTkLabel(right, text=f"v{VERSION}",
                     font=ctk.CTkFont(size=12),
                     text_color=("#5a6478", "#98a2b3")).pack(side="left",
                                                             padx=(0, 12))
        self.switch_theme = ctk.CTkSegmentedButton(
            right, values=["Тёмная", "Светлая", "Системная"],
            command=self._on_theme, width=232)
        self.switch_theme.set({"dark": "Тёмная", "light": "Светлая",
                               "system": "Системная"}.get(
                                   self.cfg.appearance or "dark", "Тёмная"))
        self.switch_theme.pack(side="left", padx=(0, 10))

        outline(right, "Настройки", self.open_settings, width=110).pack(
            side="left", padx=(0, 8))
        outline(right, "Справка", self.show_help, width=96).pack(side="left")

    # --- левая колонка --------------------------------------------------
    def _build_left(self, parent: ctk.CTkScrollableFrame) -> None:
        # 1. Папка игры
        card = self._card(parent, "Папка игры")
        self.var_path = tk.StringVar(value=self.cfg.last_game_path or "")
        row = ctk.CTkFrame(card, fg_color="transparent")
        row.pack(fill="x", padx=14, pady=(0, 8))
        self.entry_path = ctk.CTkEntry(row, textvariable=self.var_path,
                                       placeholder_text="C:\\Games\\MyNovel\\game")
        self.entry_path.pack(side="left", fill="x", expand=True)
        self.entry_path.bind("<Return>", lambda _e: self.on_scan())

        row2 = ctk.CTkFrame(card, fg_color="transparent")
        row2.pack(fill="x", padx=14, pady=(0, 6))
        ctk.CTkButton(row2, text="Выбрать папку…", width=150,
                      command=self.on_pick_folder).pack(side="left")
        outline(row2, "Проверить", self.on_scan, width=110).pack(
            side="left", padx=(8, 0))

        self.lbl_scan = ctk.CTkLabel(card, text="Папка не выбрана",
                                     font=ctk.CTkFont(size=12),
                                     text_color=("#5a6478", "#98a2b3"),
                                     anchor="w", justify="left", wraplength=300)
        self.lbl_scan.pack(fill="x", padx=14, pady=(0, 12))

        # 2. Язык перевода
        card_lang = self._card(parent, "Язык перевода",
                               "Куда переводим (tl/<язык>)")
        self.var_lang = tk.StringVar(
            value=_lang_label(self.cfg.target_lang or "russian"))
        self.menu_lang = ctk.CTkOptionMenu(
            card_lang, values=[_lang_label(code) for code in LANG_CODES],
            variable=self.var_lang, command=lambda _v: self.save_settings(),
            width=306)
        self.menu_lang.pack(fill="x", padx=14, pady=(0, 6))
        self.lbl_lang_hint = ctk.CTkLabel(
            card_lang, text="Язык оригинала определяется автоматически",
            font=ctk.CTkFont(size=11), text_color=("#5a6478", "#98a2b3"),
            anchor="w")
        self.lbl_lang_hint.pack(fill="x", padx=14, pady=(0, 12))

        # 3. Главное действие
        card_run = self._card(parent, "Запуск", "Одна кнопка — весь цикл")
        self.btn_auto = ctk.CTkButton(
            card_run, text="▶   Сделать всё автоматически", height=46,
            font=ctk.CTkFont(size=15, weight="bold"), command=self.do_auto)
        self.btn_auto.pack(fill="x", padx=14, pady=(0, 10))

        self.var_default_lang = tk.BooleanVar(
            value=bool(self.cfg.set_default_language))
        self.var_lang_button = tk.BooleanVar(value=bool(self.cfg.language_button))
        self.var_patch_menu = tk.BooleanVar(
            value=bool(self.cfg.patch_language_menu))
        self.var_patch_img = tk.BooleanVar(
            value=bool(self.cfg.patch_image_menus))
        self.var_wrap_text = tk.BooleanVar(
            value=bool(self.cfg.wrap_screen_text))
        self.var_engine = tk.BooleanVar(
            value=bool(self.cfg.engine_strings))
        self.var_quality = tk.BooleanVar(value=bool(self.cfg.quality_check))
        for text, var in (("Включать язык автоматически", self.var_default_lang),
                          ("Кнопка языка в меню игры", self.var_lang_button)):
            ctk.CTkSwitch(card_run, text=text, variable=var,
                          command=self.save_settings,
                          font=ctk.CTkFont(size=12)
                          ).pack(anchor="w", padx=14, pady=(0, 3))
        outline(card_run, "Ещё настройки…", self.open_settings,
                width=200).pack(anchor="w", padx=14, pady=(6, 10))

    def _build_progress(self, parent) -> None:
        """Карточка прогресса: всегда на виду, не уезжает при прокрутке."""
        card_prog = self._card(parent, "Прогресс")
        self.progress = ctk.CTkProgressBar(card_prog, height=10)
        self.progress.set(0)
        self.progress.pack(fill="x", padx=14, pady=(0, 8))

        row3 = ctk.CTkFrame(card_prog, fg_color="transparent")
        row3.pack(fill="x", padx=14, pady=(0, 10))
        self.var_status = tk.StringVar(value="Готов к работе")
        ctk.CTkLabel(row3, textvariable=self.var_status,
                     font=ctk.CTkFont(size=12),
                     text_color=("#5a6478", "#98a2b3"), anchor="w",
                     wraplength=210, justify="left").pack(side="left", fill="x",
                                                          expand=True)
        self.btn_cancel = ctk.CTkButton(row3, text="Стоп", width=70,
                                        fg_color="#b03a2e", hover_color="#8f2f25",
                                        command=self.on_cancel)

        chips = ctk.CTkFrame(card_prog, fg_color="transparent")
        chips.pack(fill="x", padx=14, pady=(0, 12))
        self.chips: dict[str, ctk.CTkLabel] = {}
        for index, (key, title) in enumerate((("strings", "Строк"),
                                              ("translated", "Перевод"),
                                              ("quality", "Качество"),
                                              ("coverage", "Покрытие"))):
            chips.grid_columnconfigure(index, weight=1)
            box = ctk.CTkFrame(chips, corner_radius=10,
                               fg_color=("#eef1f6", "#2b3140"))
            box.grid(row=0, column=index, sticky="ew", padx=(0, 6))
            value = ctk.CTkLabel(box, text="—",
                                 font=ctk.CTkFont(size=15, weight="bold"))
            value.pack(anchor="w", padx=8, pady=(5, 0))
            ctk.CTkLabel(box, text=title, font=ctk.CTkFont(size=9),
                         text_color=TEXT_MUTED).pack(anchor="w", padx=8,
                                                     pady=(0, 5))
            self.chips[key] = value

    # --- правая часть: вкладки ------------------------------------------
    def _build_tabs(self, parent: ctk.CTkFrame) -> None:
        self.tabs = ctk.CTkTabview(parent, corner_radius=12)
        self.tabs.grid(row=0, column=0, sticky="nsew", padx=10, pady=10)
        self.tabs.add("Литературный текст")
        self.tabs.add("Проверка и отчёт")
        self.tabs.add("Текст-картинки")
        self.tabs.add("Журнал")

        # --- литературный текст ---
        lit = self.tabs.tab("Литературный текст")
        lit.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(
            lit, justify="left", anchor="w", wraplength=640,
            font=ctk.CTkFont(size=13),
            text=("Как получить профессиональный литературный перевод:\n\n"
                  "1. Нажмите «Экспорт текста для агента» — программа выгрузит "
                  "все строки в txt с контекстом (при желании — частями по "
                  "5000 строк; JSON и DOCX включаются галочками).\n"
                  "2. Отдайте файл вашему AI-агенту: он заполнит строки «RU:», "
                  "сохранив служебные пометки, теги {b} и подстановки [имя].\n"
                  "3. Нажмите «Импорт правок в игру» и выберите файл, который "
                  "вернул агент — программа соберёт игру с литературным текстом."
                  )).grid(row=0, column=0, sticky="w", padx=16, pady=(14, 10))

        row = ctk.CTkFrame(lit, fg_color="transparent")
        row.grid(row=1, column=0, sticky="w", padx=16, pady=(0, 10))
        ctk.CTkButton(row, text="1.  Экспорт текста для агента", height=40,
                      width=250, font=ctk.CTkFont(size=13, weight="bold"),
                      command=self.do_export).pack(side="left")
        self.btn_import = outline(row, "2.  Импорт правок в игру",
                                  self.do_import, width=240, height=40)
        self.btn_import.configure(font=ctk.CTkFont(size=13))
        self.btn_import.pack(side="left", padx=(10, 0))
        outline(row, "Промпт для агента", self._copy_prompt,
                width=170, height=40).pack(side="left", padx=(10, 0))

        opts = ctk.CTkFrame(lit, fg_color="transparent")
        opts.grid(row=2, column=0, sticky="w", padx=16, pady=(0, 4))
        self.var_export_split = tk.BooleanVar(value=True)
        self.var_export_json = tk.BooleanVar(value=False)
        self.var_export_docx = tk.BooleanVar(value=False)
        ctk.CTkCheckBox(opts, text="частями по 5000 строк",
                        variable=self.var_export_split, height=22,
                        font=ctk.CTkFont(size=11)).pack(side="left")
        ctk.CTkCheckBox(opts, text="ещё JSON", variable=self.var_export_json,
                        height=22, font=ctk.CTkFont(size=11)).pack(
            side="left", padx=(14, 0))
        ctk.CTkCheckBox(opts, text="ещё DOCX (медленно, до 15 000 строк)",
                        variable=self.var_export_docx, height=22,
                        font=ctk.CTkFont(size=11)).pack(side="left",
                                                        padx=(14, 0))

        row_parts = ctk.CTkFrame(lit, fg_color="transparent")
        row_parts.grid(row=3, column=0, sticky="w", padx=16, pady=(0, 6))
        ctk.CTkLabel(row_parts, text="После перевода:",
                     font=ctk.CTkFont(size=11),
                     text_color=TEXT_MUTED).pack(side="left", padx=(0, 8))
        outline(row_parts, "Слить части в один файл", self.do_merge_parts,
                width=210, height=30).pack(side="left")
        outline(row_parts, "Сверить части", self.do_verify_parts,
                width=150, height=30).pack(side="left", padx=(8, 0))

        self.box_lit = ctk.CTkTextbox(lit, height=216, corner_radius=10,
                                      font=ctk.CTkFont(size=12))
        self.box_lit.grid(row=4, column=0, sticky="nsew", padx=16, pady=(4, 16))
        lit.grid_rowconfigure(4, weight=1)
        self._set_text(self.box_lit, PROMPT_FOR_AGENT)

        # --- проверка и отчёт ---
        chk = self.tabs.tab("Проверка и отчёт")
        chk.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(
            chk, justify="left", anchor="w", wraplength=640,
            font=ctk.CTkFont(size=13),
            text=("Проверка не запускает игру: программа читает готовый каталог "
                  "tl и сверяет его с исходниками — целы ли подстановки, нет ли "
                  "пустых строк, дубликатов блоков и непереведённых реплик."
                  )).grid(row=0, column=0, sticky="w", padx=16, pady=(14, 10))

        row_chk = ctk.CTkFrame(chk, fg_color="transparent")
        row_chk.grid(row=1, column=0, sticky="w", padx=16, pady=(0, 12))
        ctk.CTkButton(row_chk, text="Проверить перевод", height=38, width=200,
                      command=self.do_check).pack(side="left")
        outline(row_chk, "HTML-отчёт", self.do_report, width=160,
                height=38).pack(side="left", padx=(10, 0))
        outline(row_chk, "Откатить бэкап", self.do_restore, width=170,
                height=38).pack(side="left", padx=(10, 0))

        # Вспомогательные кнопки — сеткой 3×2, иначе ряд уезжает за край окна.
        row_tools = ctk.CTkFrame(chk, fg_color="transparent")
        row_tools.grid(row=2, column=0, sticky="w", padx=16, pady=(0, 12))
        for index, (text, command) in enumerate(
                (("Починить меню", self.do_fix_menus),
                 ("Починить синтаксис", self.do_check_syntax),
                 ("Что переводим", self.do_breakdown),
                 ("Проверить меню", self.do_check_ui),
                 ("Папка игры", self.do_open_game),
                 ("Перевод (папка tl)", self.do_open_tl),
                 ("Лог-файл", self.do_open_log),
                 ("Очистить кэш", self.do_clear_cache))):
            outline(row_tools, text, command, width=150, height=32).grid(
                row=index // 3, column=index % 3, padx=(0, 8), pady=(0, 8),
                sticky="w")

        self.box_check = ctk.CTkTextbox(chk, height=240, corner_radius=10,
                                        font=ctk.CTkFont(size=12))
        self.box_check.grid(row=3, column=0, sticky="nsew", padx=16, pady=(0, 16))
        chk.grid_rowconfigure(3, weight=1)
        self._set_text(self.box_check,
                       "Результат проверки появится здесь.")

        # --- текст-картинки ---
        try:
            from gui_images import ImagesTab

            self.tab_images = ImagesTab(self.tabs.tab("Текст-картинки"), self)
            self.tab_images.pack(fill="both", expand=True)
        except Exception:  # noqa: BLE001
            logger.exception("не удалось создать вкладку «Текст-картинки»")

        # --- журнал ---
        log_tab = self.tabs.tab("Журнал")
        log_tab.grid_columnconfigure(0, weight=1)
        row_log = ctk.CTkFrame(log_tab, fg_color="transparent")
        row_log.grid(row=0, column=0, sticky="ew", padx=16, pady=(14, 8))
        ctk.CTkLabel(row_log, text="Журнал работы",
                     font=ctk.CTkFont(size=13, weight="bold")
                     ).pack(side="left")
        outline(row_log, "Очистить", self._clear_log, width=100,
                height=30).pack(side="right")
        outline(row_log, "Скопировать", self._copy_log, width=120,
                height=30).pack(side="right", padx=(0, 8))

        self.box_log = ctk.CTkTextbox(log_tab, corner_radius=10,
                                      font=ctk.CTkFont(family="Consolas", size=12)
                                      if os.name == "nt" else
                                      ctk.CTkFont(size=12))
        self.box_log.grid(row=1, column=0, sticky="nsew", padx=16, pady=(0, 16))
        log_tab.grid_rowconfigure(1, weight=1)
        inner = self.box_log._textbox  # noqa: SLF001 — нужен доступ к тегам
        inner.tag_configure("ok", foreground="#2ea043")
        inner.tag_configure("warn", foreground="#d29922")
        inner.tag_configure("err", foreground="#e5534b")
        inner.tag_configure("info", foreground="#7b8794")

    # --- подвал ---------------------------------------------------------
    def _build_footer(self) -> None:
        footer = ctk.CTkFrame(self, corner_radius=0, height=30)
        footer.grid(row=2, column=0, columnspan=2, sticky="ew")
        ctk.CTkLabel(
            footer, font=ctk.CTkFont(size=10),
            text_color=("#6b7280", "#8b949e"),
            text="Перевод для личного использования · оригинальные .rpy "
                 "не меняются в режиме tl/").pack(side="left", padx=18, pady=5)
        ctk.CTkLabel(footer, font=ctk.CTkFont(size=10),
                     text_color=("#6b7280", "#8b949e"),
                     text=f"кэш: {default_cache_path()}").pack(side="right",
                                                              padx=18)

    # --- вспомогательное -------------------------------------------------
    @staticmethod
    def _card(parent, title: str, subtitle: str = "") -> ctk.CTkFrame:
        card = ctk.CTkFrame(parent, corner_radius=14)
        card.pack(fill="x", pady=(0, 8))
        ctk.CTkLabel(card, text=title, anchor="w",
                     font=ctk.CTkFont(size=14, weight="bold")
                     ).pack(fill="x", padx=14, pady=(10, 0))
        if subtitle:
            ctk.CTkLabel(card, text=subtitle, anchor="w",
                         font=ctk.CTkFont(size=11),
                         text_color=("#6b7280", "#8b949e")
                         ).pack(fill="x", padx=14, pady=(0, 8))
        return card

    @staticmethod
    def _set_text(box: ctk.CTkTextbox, text: str) -> None:
        box.configure(state="normal")
        box.delete("1.0", "end")
        box.insert("1.0", text)
        box.configure(state="disabled")

    # ------------------------------------------------------------------
    # Тема, язык, настройки
    # ------------------------------------------------------------------
    def _on_theme(self, value: str) -> None:
        mode = {"Тёмная": "dark", "Светлая": "light",
                "Системная": "system"}.get(value, "dark")
        ctk.set_appearance_mode(mode)
        self.cfg.appearance = mode
        self.save_settings()

    def current_language(self) -> str:
        try:
            return _code_from_label(self.var_lang.get())
        except (AttributeError, tk.TclError):
            return self.cfg.target_lang or "russian"

    def save_settings(self) -> None:
        try:
            self.cfg.last_game_path = self.var_path.get().strip()
            self.cfg.target_lang = self.current_language()
            self.cfg.set_default_language = bool(self.var_default_lang.get())
            self.cfg.language_button = bool(self.var_lang_button.get())
            self.cfg.patch_language_menu = bool(self.var_patch_menu.get())
            self.cfg.patch_image_menus = bool(self.var_patch_img.get())
            self.cfg.wrap_screen_text = bool(self.var_wrap_text.get())
            self.cfg.engine_strings = bool(self.var_engine.get())
            self.cfg.quality_check = bool(self.var_quality.get())
            self.cfg.save()
        except Exception as exc:  # noqa: BLE001
            logger.debug("Настройки не сохранены: %s", exc)

    def _settings(self):
        self.save_settings()
        settings = self.cfg.to_settings()
        path = (self.var_path.get() or "").strip()
        if path:
            settings.game_path = Path(path).expanduser()
        settings.target_lang = self.current_language()
        settings.set_default_language = bool(self.var_default_lang.get())
        settings.language_button = bool(self.var_lang_button.get())
        settings.patch_language_menu = bool(self.var_patch_menu.get())
        settings.patch_image_menus = bool(self.var_patch_img.get())
        settings.wrap_screen_text = bool(self.var_wrap_text.get())
        settings.engine_strings = bool(self.var_engine.get())
        settings.quality_check = bool(self.var_quality.get())
        settings.quality_fallback = bool(self.var_quality.get())
        return settings

    def _pipeline(self) -> Pipeline:
        self.pipeline = Pipeline(self._settings(),
                                 progress_factory=self._make_progress,
                                 should_cancel=lambda: self._cancel.is_set())
        return self.pipeline

    # ------------------------------------------------------------------
    # Журнал и очереди
    # ------------------------------------------------------------------
    def _install_log_handler(self) -> None:
        sink = self._log_queue

        class Handler(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                try:
                    sink.put_nowait((record.getMessage(), record.levelname.lower()))
                except Exception:  # noqa: BLE001
                    pass

        handler = Handler()
        handler.setLevel(logging.INFO)
        logging.getLogger("renpy_translator").addHandler(handler)

    def post(self, func: Callable[[], None]) -> None:
        self._ui_queue.put(func)

    def log(self, message: str, tag: str = "info") -> None:
        self._log_queue.put((message, tag))

    def _pump(self) -> None:
        """Единая точка обновления интерфейса из рабочих потоков."""
        try:
            while True:
                self._ui_queue.get_nowait()()
        except queue.Empty:
            pass
        try:
            while True:
                message, level = self._log_queue.get_nowait()
                if level in ("ok", "warn", "err", "info"):
                    tag = level
                else:
                    tag = {"error": "err", "critical": "err",
                           "warning": "warn", "debug": "info"}.get(level,
                                                                   "info")
                self._append_log(message, tag)
        except queue.Empty:
            pass
        self._tick_status()
        self.after(90, self._pump)

    _LOG_ICONS = {"ok": "✓  ", "warn": "!  ", "err": "✕  ", "info": "·  "}

    def _append_log(self, message: str, tag: str = "info") -> None:
        box = getattr(self, "box_log", None)
        if box is None:
            return
        inner = getattr(box, "_textbox", None)
        prefix = self._LOG_ICONS.get(tag, "·  ")
        try:
            inner.configure(state="normal")
            inner.insert("end", prefix + message + "\n", (tag,))
            inner.see("end")
            inner.configure(state="disabled")
        except Exception:  # noqa: BLE001
            box.configure(state="normal")
            box.insert("end", prefix + message + "\n")
            box.see("end")
            box.configure(state="disabled")

    def _clear_log(self) -> None:
        self._set_text(self.box_log, "")
        self.log("Журнал очищен.", "info")

    # --- диалоги в стиле приложения --------------------------------------
    _ICONS = {"info": "ℹ", "ok": "✓", "warn": "!", "error": "✕"}
    _COLORS = {"info": "#2f6fed", "ok": "#2ea043", "warn": "#d29922",
               "error": "#e5534b"}

    def _dialog(self, text: str, kind: str = "info", ask: bool = False,
                title: str = APP_NAME) -> bool:
        """Модальное окно на CTk: одинаково выглядит в тёмной и светлой теме."""
        window = ctk.CTkToplevel(self)
        window.title(title)
        window.resizable(False, False)
        window.transient(self)
        window.grid_columnconfigure(0, weight=1)

        header = ctk.CTkFrame(window, fg_color="transparent")
        header.grid(row=0, column=0, sticky="ew", padx=20, pady=(18, 6))
        ctk.CTkLabel(header, text=self._ICONS.get(kind, "ℹ"),
                     font=ctk.CTkFont(size=18, weight="bold"),
                     text_color=self._COLORS.get(kind, "#2f6fed"),
                     width=24).pack(side="left", padx=(0, 8))
        ctk.CTkLabel(header, text=title, anchor="w",
                     font=ctk.CTkFont(size=13, weight="bold")
                     ).pack(side="left")

        rows = sum(max(1, len(line) // 58 + 1) for line in text.split("\n"))
        height = max(84, min(300, 26 + rows * 19))
        body = ctk.CTkTextbox(window, width=460, height=height, corner_radius=10,
                              font=ctk.CTkFont(size=12), wrap="word")
        body.grid(row=1, column=0, sticky="nsew", padx=20)
        body.insert("1.0", text)
        body.configure(state="disabled")

        answer = {"ok": False}

        def close(value: bool) -> None:
            answer["ok"] = value
            window.grab_release()
            window.destroy()

        buttons = ctk.CTkFrame(window, fg_color="transparent")
        buttons.grid(row=2, column=0, sticky="ew", padx=20, pady=(12, 18))
        if ask:
            outline(buttons, "Отмена", lambda: close(False), width=120,
                    height=34).pack(side="right")
            ctk.CTkButton(buttons, text="Да", width=120, height=34,
                          command=lambda: close(True)).pack(side="right",
                                                            padx=(0, 10))
        else:
            ctk.CTkButton(buttons, text="Понятно", width=130, height=34,
                          command=lambda: close(True)).pack(side="right")

        window.bind("<Escape>", lambda _e: close(False))
        window.bind("<Return>", lambda _e: close(True))
        window.update_idletasks()
        self._center_over(window)
        window.grab_set()
        window.focus_force()
        self.wait_window(window)
        return answer["ok"]

    def _center_over(self, window) -> None:
        try:
            x = self.winfo_rootx() + (self.winfo_width() - window.winfo_width()) // 2
            y = self.winfo_rooty() + (self.winfo_height() - window.winfo_height()) // 3
            window.geometry(f"+{max(x, 0)}+{max(y, 0)}")
        except Exception:  # noqa: BLE001
            pass

    def _info(self, text: str) -> None:
        self._dialog(text, "info")

    def _ok(self, text: str) -> None:
        self._dialog(text, "ok")

    def _warn(self, text: str) -> None:
        self._dialog(text, "warn")

    def _error(self, text: str) -> None:
        self._dialog(text, "error")

    def _ask(self, text: str) -> bool:
        return self._dialog(text, "warn", ask=True)

    def _toast(self, text: str, kind: str = "ok", ms: int = 15000) -> None:
        """Всплывающая плашка внизу: не мешает работать, сама исчезает."""
        old = getattr(self, "_toast_frame", None)
        if old is not None:
            try:
                old.destroy()
            except Exception:  # noqa: BLE001
                pass
        palette = {"ok": ("#e7f6ec", "#1c3a26"), "warn": ("#fdf3d9", "#3a3218"),
                   "info": ("#e8f0fe", "#1e2a3d"), "error": ("#fdeaea", "#3a1f1e")}
        frame = ctk.CTkFrame(self, corner_radius=12, border_width=1,
                             border_color=self._COLORS.get(kind, "#2f6fed"),
                             fg_color=palette.get(kind, palette["info"])[1])
        frame.configure(fg_color=palette.get(kind, palette["info"]))
        label = ctk.CTkLabel(frame, text=text, justify="left", wraplength=520,
                             font=ctk.CTkFont(size=12),
                             text_color=("#1f2430", "#e6e8ee"))
        label.pack(padx=16, pady=10)
        frame.place(relx=0.5, rely=0.97, anchor="s")
        frame.lift()
        self._toast_frame = frame
        self.after(ms, lambda f=frame: self._drop_toast(f))

    def _drop_toast(self, frame) -> None:
        try:
            if frame is not None and frame.winfo_exists():
                frame.destroy()
        except Exception:  # noqa: BLE001
            pass
        if getattr(self, "_toast_frame", None) is frame:
            self._toast_frame = None

    def _copy_prompt(self) -> None:
        try:
            self.clipboard_clear()
            self.clipboard_append(PROMPT_FOR_AGENT)
        except Exception as exc:  # noqa: BLE001
            logger.debug("Буфер обмена недоступен: %s", exc)
        self._set_text(self.box_lit, PROMPT_FOR_AGENT)
        self.log("Промпт для агента скопирован в буфер обмена — вставьте его "
                 "в чат с ИИ вместе с выгруженным файлом.", "ok")

    def _copy_log(self) -> None:
        try:
            text = self.box_log.get("1.0", "end").strip()
            self.clipboard_clear()
            self.clipboard_append(text)
            self.log("Журнал скопирован в буфер обмена.", "ok")
        except Exception as exc:  # noqa: BLE001
            logger.debug("Не удалось скопировать журнал: %s", exc)

    # ------------------------------------------------------------------
    # Работа (потоки, прогресс, статус)
    # ------------------------------------------------------------------
    def _run(self, title: str, task: Callable[[], Any],
             on_done: Optional[Callable[[Any], None]] = None) -> None:
        if self._busy:
            return
        self._set_busy(True, title)

        def runner() -> None:
            try:
                result = task()
            except Exception as exc:  # noqa: BLE001
                logger.exception("Ошибка задачи «%s»", title)
                self.post(lambda: self._failed(exc))
                return
            self.post(lambda: self._finish(result, on_done))

        self._worker = threading.Thread(target=runner, daemon=True)
        self._worker.start()

    def _finish(self, result: Any, on_done: Optional[Callable[[Any], None]]) -> None:
        self._set_busy(False)
        self._update_summary()
        if on_done is not None:
            try:
                on_done(result)
            except Exception:  # noqa: BLE001
                logger.exception("Ошибка обработки результата")

    def _failed(self, exc: BaseException) -> None:
        self._set_busy(False)
        message = str(exc) or exc.__class__.__name__
        self.log(f"Ошибка: {message}", "err")
        self._error(f"Не получилось:\n\n{message}")

    def _set_busy(self, busy: bool, title: str = "") -> None:
        self._busy = busy
        state = "disabled" if busy else "normal"
        self.btn_auto.configure(state=state)
        self.btn_import.configure(state=state)
        if busy:
            self._cancel.clear()
            self._busy_started = time.monotonic()
            self._busy_title = title
            self._last_progress = ""
            self.var_status.set(f"{title} · прошло 0 с")
            self.btn_cancel.pack(side="right")
            self.btn_cancel.configure(state="normal", text="Стоп")
        else:
            self._busy_title = ""
            self.btn_cancel.pack_forget()
            self.progress.set(0)

    def on_cancel(self) -> None:
        self._cancel.set()
        self.btn_cancel.configure(state="disabled", text="Останавливаюсь…")
        self.log("Останавливаюсь после текущего пакета — переведённое "
                 "сохранится в кэше.", "warn")

    def _tick_status(self) -> None:
        if not self._busy:
            return
        elapsed = human_duration(time.monotonic() - self._busy_started)
        base = self._last_progress or self._busy_title or "Работаю…"
        self.var_status.set(f"{base} · прошло {elapsed}")

    def _make_progress(self, total: int, desc: str, enabled: bool):
        from core.utils import Progress

        progress = Progress(total=total, desc=desc, enabled=False)
        state = {"last": 0.0, "start": time.monotonic()}

        def callback(count: int, total_: int) -> None:
            percent = (count / total_) if total_ else 0.0
            elapsed = max(0.001, time.monotonic() - state["start"])
            speed = count / elapsed
            eta = human_duration(progress.elapsed_eta())
            text = (f"{desc}: {count} из {total_} ({percent * 100:.0f}%) · "
                    f"{speed:.0f} строк/с · осталось ≈ {eta}")

            def update() -> None:
                self.progress.set(percent)
                self._last_progress = text

            self.post(update)

        progress.callback = callback
        return progress

    def _update_summary(self) -> None:
        """Обновить чипы и строку состояния после прогона."""
        pipeline = self.pipeline
        if pipeline is None:
            return
        report = getattr(pipeline, "last_translate_report", None)
        quality = getattr(pipeline, "quality", None)
        verify = getattr(pipeline, "verification", None)

        def put(key: str, value: str, color: Optional[str] = None) -> None:
            label = self.chips.get(key)
            if label is None:
                return
            label.configure(text=value)
            if color:
                label.configure(text_color=color)

        if report is not None:
            put("strings", str(report.unique_texts))
            put("translated", str(report.translated + report.from_cache))
        if quality is not None:
            errors = len(quality.errors)
            put("quality", "чисто" if not errors else f"{errors} ош.",
                "#2ea043" if not errors else "#e5534b")
        if verify is not None:
            put("coverage", f"{verify.coverage * 100:.0f}%",
                "#2ea043" if verify.coverage >= 0.99 else "#d29922")
        if report is not None:
            failed = int(getattr(report, "failed", 0) or 0)
            recovered = int(getattr(report, "recovered", 0) or 0)
            try:
                if failed:
                    self.var_status.set(
                        f"Не переведено строк: {failed} — запустите ещё раз, "
                        f"они доберутся (остальное возьмётся из кэша)")
                elif recovered:
                    self.var_status.set(
                        f"Готово. Повторными попытками добрано строк: {recovered}")
            except Exception:  # noqa: BLE001
                pass

    # ------------------------------------------------------------------
    # Действия пользователя
    # ------------------------------------------------------------------
    def on_pick_folder(self) -> None:
        initial = (self.var_path.get() or "").strip() or None
        folder = filedialog.askdirectory(
            title="Выберите папку игры (обычно .../MyNovel или .../MyNovel/game)",
            initialdir=initial if initial and Path(initial).is_dir() else None)
        if not folder:
            return
        self.var_path.set(folder)
        self.save_settings()
        self.on_scan()

    def on_scan(self) -> None:
        path = (self.var_path.get() or "").strip()
        if not path:
            self.lbl_scan.configure(text="Папка не выбрана",
                                    text_color=("#5a6478", "#98a2b3"))
            return
        try:
            project = scan_project(path)
        except ProjectError as exc:
            self.lbl_scan.configure(text=f"✕ {exc}",
                                    text_color="#e5534b")
            return
        self.project = project
        stats = project.summary().replace("\n", " · ")
        self.lbl_scan.configure(text="✓ " + stats, text_color="#2ea043")
        self.cfg.last_game_path = str(project.game_dir)
        self.save_settings()
        self.log("Найдена игра: " + stats, "ok")

    # --- полный цикл ----------------------------------------------------
    def do_auto(self) -> None:
        def task():
            pipeline = self._pipeline()
            project = pipeline.scan()
            self.log(f"Игра: {project.name} ({project.game_dir})", "info")

            if project.needs_preparation:
                self.log("Распаковываю .rpa и декомпилирую .rpyc…", "info")
                rpa_results, decompile_report = pipeline.prepare()
                for res in rpa_results:
                    self.log("  " + res.summary(), "info")
                if decompile_report.unavailable:
                    self.log("  ! " + decompile_report.unavailable, "warn")

            # страховка: битый `menu` из прошлых правок чиним до перевода
            syntax = pipeline.check_syntax(repair=True)
            if syntax.repaired:
                self.log(f"Файлы игры: восстановлено двоеточий — "
                         f"{syntax.repaired} (иначе игра не запустилась бы)", "warn")
            elif syntax.issues:
                self.log("Файлы игры: есть строки без двоеточия — нажмите "
                         "«Починить синтаксис» на вкладке «Проверка и отчёт»",
                         "warn")

            # что уже переведено в прошлый раз — говорим сразу, до всякой работы
            ready = pipeline.game_translations()
            if ready:
                self.log(f"В игре уже есть готовый перевод: {len(ready)} строк — "
                         f"заново их переводить не будем", "ok")
            elif len(pipeline.cache):
                self.log(f"В кэше переводов лежит {len(pipeline.cache)} строк — "
                         f"они не будут переведены повторно", "ok")

            if pipeline.settings.backup:
                from core.backup import create_backup

                backup = create_backup(project,
                                       extra_paths=[pipeline.settings.cache_path])
                self.log("  " + backup.summary(), "ok")

            parsed = pipeline.analyze()
            stats = parsed.stats()
            self.log(f"Строк найдено: {stats.get('total', 0)} "
                     f"(реплик {stats.get('dialogue', 0)}, нарратив "
                     f"{stats.get('narration', 0)}, меню {stats.get('menu', 0)}, "
                     f"интерфейс {stats.get('ui', 0)})", "ok")
            if not stats.get("total"):
                raise RuntimeError(
                    "В скриптах не найдено переводимого текста. Если текст "
                    "скомпилирован (.rpyc/.rpa) — сначала нужна подготовка.")

            try:
                from core.breakdown import build_breakdown

                breakdown = build_breakdown(parsed, pipeline)
                self.log("К переводу: " + breakdown.summary(), "info")
                if breakdown.top_files:
                    top = ", ".join(f"{name} — {count}"
                                    for name, count in breakdown.top_files[:3])
                    self.log(f"  самые большие файлы: {top}", "info")
            except Exception as exc:  # noqa: BLE001
                self.log(f"  состав строк не посчитан: {exc}", "warn")

            self.log("Перевожу… кнопка «Стоп» остановит работу, уже "
                     "переведённое останется в кэше.", "info")
            report = pipeline.translate(parsed)
            self.log(report.summary(), "ok" if not report.failed else "warn")
            for warning in report.warnings:
                self.log("  ! " + warning, "warn")

            tl_result, replace_result = pipeline.build(parsed)
            if tl_result:
                self.log(tl_result.summary(), "ok")
                for warning in tl_result.warnings[:8]:
                    self.log("  ! " + warning, "warn")
            if replace_result:
                self.log(replace_result.summary(), "ok")
            if pipeline.font_report is not None:
                fonts = pipeline.font_report
                if fonts.mode != "off":
                    self.log("Шрифты: " + ("PT Serif с кириллицей" if
                                           fonts.mode == "pretty" else
                                           "заменены только там, где не было "
                                           "русских букв"), "ok")
            return report, tl_result

        self._run("Полный цикл", task, self._auto_done)

    def _auto_done(self, result) -> None:
        report, tl_result = result
        quality = getattr(self.pipeline, "quality", None)
        verify = getattr(self.pipeline, "verification", None)

        lines = ["Готово!"]
        if report is not None:
            lines.append(f"Переведено строк: {report.translated} "
                         f"(из кэша: {report.from_cache})")
            if report.failed:
                lines.append(f"Не переведено: {report.failed} — остались "
                             "на языке оригинала")
        if tl_result is not None:
            lines.append(f"Файлов перевода: {len(tl_result.files_written)} "
                         f"в {tl_result.tl_dir}")
        if quality is not None:
            lines.append("Качество: " + ("без ошибок" if not quality.errors
                                         else f"{len(quality.errors)} строк "
                                              "с ошибками"))
        if verify is not None:
            lines.append(f"Проверка файлов: "
                         f"{'чисто' if verify.ok else 'есть замечания'}, "
                         f"покрытие {verify.coverage * 100:.1f}%")
        lines.append("\nДальше:\n1) Запустите игру — язык включится сам "
                     "(если включён переключатель).\n2) Для литературного "
                     "текста: «Литературный текст» → экспорт → агент → импорт.")

        self._set_text(self.box_lit, "\n".join(lines))
        self.log("Полный цикл завершён.", "ok")

        if report is not None and getattr(report, "aborted", False):
            self._warn(
                "Перевод остановлен досрочно.\n\n"
                f"Причина: {report.abort_reason}.\n"
                f"Готово {report.translated} строк — они сохранены в кэше.\n\n"
                "Запустите ещё раз: программа продолжит с места остановки.")
            return
        self.tabs.set("Проверка и отчёт")
        self._set_text(self.box_check, "Итог полного цикла:\n\n" + "\n".join(lines))
        toast = []
        if report is not None:
            toast.append(f"переведено {report.translated} строк")
        if verify is not None:
            toast.append(f"покрытие {verify.coverage * 100:.0f}%")
        if quality is not None:
            toast.append("качество без ошибок" if not quality.errors
                         else f"ошибок в {len(quality.errors)} строках")
        text = "Готово: " + ", ".join(toast) if toast else "Готово!"
        if tl_result is not None and tl_result.tl_dir:
            text += f" · файлы: {tl_result.tl_dir}"
        self._toast(text)

    # --- экспорт / импорт -----------------------------------------------
    def do_export(self) -> None:
        default_dir = self.cfg.last_export_path or self.var_path.get().strip()
        folder = filedialog.askdirectory(
            title="Куда сохранить текст для агента?",
            initialdir=default_dir if Path(default_dir or ".").is_dir() else None)
        if not folder:
            return

        with_json = bool(self.var_export_json.get())
        with_docx = bool(self.var_export_docx.get())
        split = 5000 if self.var_export_split.get() else 0

        def task():
            pipeline = self._pipeline()
            pipeline.scan()
            if pipeline.project and pipeline.project.needs_preparation:
                self.log("Сначала подготовка (.rpa/.rpyc)…", "info")
                pipeline.prepare()
            pipeline.analyze()
            base = Path(folder) / "renpy_translator_export"
            self.log("Экспорт: txt" + (" + json" if with_json else "")
                     + (" + docx" if with_docx else "")
                     + (f", частями по {split}" if split else ""), "info")
            results = pipeline.export_all(base, with_json=with_json,
                                          with_docx=with_docx, split=split)
            self.cfg.last_export_path = folder
            self.save_settings()
            for res in results:
                self.log(res.summary(), "ok")
                for warning in res.warnings:
                    self.log(warning, "warn")
            return folder, results

        self._run("Экспорт текста", task, self._export_done)

    def _export_done(self, value) -> None:
        folder, results = value
        self._last_export_dir = Path(folder)
        text = ["Текст выгружен. Отдайте файл агенту и попросите заполнить "
                "строки «RU:».", ""]
        for res in results:
            text.append(str(getattr(res, "path", "")))
        text.append("")
        text.append("Важно для агента: сохранить пометки [#файл:строка], "
                    "строки EN:, теги {b} и подстановки [имя].")
        self._set_text(self.box_lit, "\n".join(text))
        self.log("Экспорт завершён.", "ok")
        self._open_folder(Path(folder))

    # --- части экспорта: слияние и сверка -------------------------------
    def _ask_parts(self, title: str) -> Optional[Path]:
        path = filedialog.askopenfilename(
            title=title,
            filetypes=[("Части экспорта", "*.txt"), ("Все файлы", "*.*")])
        return Path(path) if path else None

    def do_merge_parts(self) -> None:
        target = self._ask_parts("Выберите любую часть экспорта "
                                 "(например, ...-part01.txt)")
        if target is None:
            return

        def task():
            from core.parts import collect_parts, merge

            found = collect_parts(target)
            if not found.files:
                raise RuntimeError("рядом не найдено ни одной части "
                                   "(-part01.txt, -part02.txt …)")
            return found, merge(found.files)

        def done(value) -> None:
            found, result = value
            lines = [f"Частей: {len(found.files)} "
                     f"({found.files[0].name} … {found.files[-1].name})",
                     "Слияние: " + result.summary()]
            if result.duplicates:
                lines.append(f"! повторяющихся идентификаторов: "
                             f"{result.duplicates}")
            lines.append("Файл лежит рядом с частями: "
                         f"{result.path.name}")
            self._set_text(self.box_lit, "\n".join(lines))
            for line in lines:
                self.log(line, "ok" if not line.startswith("!") else "warn")

        self._run("Слияние частей", task, done)

    def do_verify_parts(self) -> None:
        target = self._ask_parts("Выберите любую часть экспорта для сверки")
        if target is None:
            return
        game = (self.var_path.get() or "").strip()

        def task():
            from core.parts import collect_parts, verify

            found = collect_parts(target)
            if not found.files:
                raise RuntimeError("рядом не найдено ни одной части")
            parsed = None
            if game:
                pipeline = self._pipeline()
                pipeline.scan()
                parsed = pipeline.analyze()
            return verify(found.files, parsed)

        def done(report) -> None:
            lines = report.lines()
            self._set_text(self.box_lit, "\n".join(lines))
            for line in lines:
                self.log(line, "warn" if line.strip().startswith("!") else "ok")

        self._run("Сверка частей", task, done)

    def do_import(self) -> None:
        paths = filedialog.askopenfilenames(
            title="Выберите файл(ы) с переводом — можно сразу все части "
                  "(txt / json / docx)",
            filetypes=[("Перевод", "*.txt *.json *.docx"), ("Текст", "*.txt"),
                       ("JSON", "*.json"), ("Word", "*.docx"),
                       ("Все файлы", "*.*")])
        if not paths:
            return
        imported = [Path(item) for item in paths]

        def task():
            pipeline = self._pipeline()
            pipeline.scan()
            if pipeline.project and pipeline.project.needs_preparation:
                pipeline.prepare()
            parsed = pipeline.analyze()
            total_applied = 0
            for file in imported:
                self.log(f"Читаю {file.name}…", "info")
                one = pipeline.import_file(file, merge=False)
                total_applied += one.applied
                self.log(one.summary(), "ok" if one.applied else "warn")
                for warning in one.warnings:
                    self.log("  ! " + warning, "warn")
            self.log(f"Файлов прочитано: {len(imported)}, "
                     f"строк перевода: {total_applied}", "ok")
            untranslated = sum(1 for item in parsed.items
                               if not item.is_translated())
            literary = len(pipeline.literary())
            tl_result, replace_result = pipeline.build(parsed)
            if tl_result:
                self.log(tl_result.summary(), "ok")
            if replace_result:
                self.log(replace_result.summary(), "ok")
            return result, untranslated, tl_result, literary

        self._run("Импорт правок", task, self._import_done)

    def _import_done(self, value) -> None:
        result, untranslated, tl_result, literary = value
        lines = [f"Применено строк: {getattr(result, 'applied', 0)}",
                 f"Без перевода осталось: {untranslated}",
                 f"Авторский перевод сохранён: {literary} строк — "
                 f"машинный перевод их больше не заменит"]
        if tl_result is not None:
            lines.append(f"Файлы перевода: {tl_result.tl_dir}")
        if untranslated:
            lines.append("\nСтроки без перевода остались на языке оригинала — "
                         "это нормально, если агент перевёл не всё.")
        self._set_text(self.box_lit, "\n".join(lines))
        self._ok("\n".join(lines[:6]))

    # --- проверка, отчёт, бэкап ------------------------------------------
    def do_check(self) -> None:
        def task():
            from core.verify import verify_project

            pipeline = self._pipeline()
            pipeline.scan()
            parsed = None
            try:
                parsed = pipeline.analyze()
            except Exception as exc:  # noqa: BLE001
                self.log(f"Разбор игры не удался: {exc}", "warn")
            report = verify_project(pipeline.project,
                                    self._settings().normalized_lang(), parsed)
            pipeline.verification = report
            self.log(report.summary(), "ok" if report.ok else "warn")
            return report

        self._run("Проверка перевода", task, self._check_done)

    def _check_done(self, report) -> None:
        lines = [report.summary(), ""]
        if not report.issues:
            lines.append("Проблем нет — файлы перевода выглядят корректно.")
        for issue in report.issues[:60]:
            mark = "✕" if issue.severity == "error" else "•"
            where = f" ({issue.where()})" if issue.where() else ""
            lines.append(f"{mark} {issue.label()}: {issue.message}{where}")
        if len(report.issues) > 60:
            lines.append(f"… и ещё {len(report.issues) - 60}")
        self._set_text(self.box_check, "\n".join(lines))
        self.tabs.set("Проверка и отчёт")

    def do_report(self) -> None:
        def task():
            from core.verify import verify_project

            pipeline = self._pipeline()
            if pipeline.project is None:
                pipeline.scan()
            if pipeline.parsed is None:
                try:
                    pipeline.analyze()
                except Exception:  # noqa: BLE001
                    pass
            if pipeline.verification is None:
                try:
                    pipeline.verification = verify_project(
                        pipeline.project,
                        self._settings().normalized_lang(), pipeline.parsed)
                except Exception as exc:  # noqa: BLE001
                    self.log(f"Проверка не удалась: {exc}", "warn")
            return pipeline.write_report()

        self._run("HTML-отчёт", task, self._report_done)

    def _report_done(self, path: Path) -> None:
        self.log(f"Отчёт сохранён: {path}", "ok")
        self._set_text(self.box_check, f"Отчёт: {path}")
        try:
            opened = webbrowser.open(Path(path).resolve().as_uri())
        except Exception:  # noqa: BLE001
            opened = False
        if not opened:
            self._open_folder(Path(path).parent)

    def do_restore(self) -> None:
        from core.backup import latest_backup, restore_backup

        try:
            project = scan_project(self.var_path.get().strip())
        except ProjectError as exc:
            self._warn(str(exc))
            return
        archive = latest_backup(project)
        if archive is None:
            self._info("Резервных копий для этой игры нет.")
            return
        if not self._ask(
                f"Вернуть файлы игры из копии?\n\n{archive.name}\n\n"
                          "Созданные программой каталоги перевода будут удалены."):
            return

        def task():
            result = restore_backup(project, archive)
            self.log(result.summary(), "ok" if result.ok else "warn")
            return result

        self._run("Откат бэкапа", task,
                  lambda res: self._ok("Готово.\n\n" + res.summary()))

    def do_breakdown(self) -> None:
        """Показать, из чего складывается число переводимых строк."""
        path = (self.var_path.get() or "").strip()
        if not path:
            self._warn("Сначала выберите папку игры.")
            return

        def task():
            from core.breakdown import build_breakdown

            pipeline = self._pipeline()
            pipeline.scan()
            if pipeline.project is not None and pipeline.project.needs_preparation:
                self.log("Сначала подготовка (.rpa/.rpyc)…", "info")
                pipeline.prepare()
            parsed = pipeline.analyze()
            return build_breakdown(parsed, pipeline)

        def done(report) -> None:
            lines = report.lines()
            self._set_text(self.box_check, "\n".join(lines))
            for line in lines:
                if line.strip():
                    self.log(line.strip(), "ok")
            self.tabs.set("Проверка и отчёт")

        self._run("Что переводим", task, done)

    # --- меню и интерфейс -------------------------------------------------
    def do_check_ui(self) -> None:
        """Почему надписи в меню игры остались английскими."""
        path = (self.var_path.get() or "").strip()
        if not path:
            self._warn("Сначала выберите папку игры.")
            return

        def task():
            from core.scanner import scan_project
            from core.uicheck import check_ui

            project = scan_project(path)
            return check_ui(project, self.current_language())

        def done(report) -> None:
            lines = report.lines()
            self._set_text(self.box_check, "\n".join(lines))
            for line in lines:
                self.log(line.strip(), "warn" if line.strip().startswith("!")
                         else "info")
            self.tabs.set("Проверка и отчёт")

        self._run("Проверка меню", task, done)

    def do_check_syntax(self) -> None:
        """Найти и починить строки без двоеточия (`menu` вместо `menu:`)."""
        path = (self.var_path.get() or "").strip()
        if not path:
            self._warn("Сначала выберите папку игры.")
            return

        def task():
            from core.scanner import scan_project
            from core.screen_refs import check_screen_refs
            from core.syntax_guard import check_dir

            report = check_dir(Path(path), repair=True)
            try:
                screens = check_screen_refs(scan_project(path))
            except Exception:  # noqa: BLE001
                screens = None
            return report, screens

        def done(result) -> None:
            report, screens = result
            lines = report.lines()
            if not report.issues:
                lines = ["Синтаксис файлов игры в порядке: строк без двоеточия "
                         "нет.",
                         f"Проверено файлов: {report.checked}.",
                         "",
                         "Именно такая строка (`menu` вместо `menu:`) даёт "
                         "при запуске:",
                         "  Парсинг не удался — expected ':' not found."]
            else:
                lines.append("")
                lines.append("Рядом лежащие .rpyc удалены — Ren'Py "
                             "перекомпилирует скрипт при запуске.")
                lines.append("Запускайте игру: ошибка `expected ':'` "
                             "больше не появится.")
            if screens is not None and screens.missing:
                lines.append("")
                lines += screens.lines()
                lines.append("  Такие экраны пропадают, когда скрипт "
                             "восстановлен из .rpyc не полностью.")
                lines.append("  Лечится только возвратом оригинала: "
                             "«Откатить бэкап» или .rpyc из backup_*.zip.")
            self._set_text(self.box_check, "\n".join(lines))
            for line in lines[:12]:
                if line.strip():
                    self.log(line, "warn" if line.strip().startswith("!") else "ok")

        self._run("Проверка синтаксиса", task, done)

    def do_fix_menus(self) -> None:
        """Починить меню и экраны: картинки-кнопки и строки без _()."""
        path = (self.var_path.get() or "").strip()
        if not path:
            self._warn("Сначала выберите папку игры.")
            return
        if not self._ask("Починить меню и экраны?\n\n"
                         "Программа заменит кнопки меню, нарисованные "
                         "картинками (Start/Load/…), текстовыми с русской "
                         "подписью и обернёт в _() строки экранов, которые "
                         "без этого никогда не переводятся.\n\n"
                         "Оригинал screens.rpy сохраняется, всё можно "
                         "откатить кнопкой «Откатить бэкап»."):
            return

        def task():
            from core.engine_strings import write_engine_rpy
            from core.syntax_guard import check_project as check_syntax
            from core.fonts import install as install_fonts
            from core.fonts import installed_font_rel
            from core.langs import label_for
            from core.menu_patch import patch_image_menus
            from core.scanner import scan_project
            from core.screen_patcher import (patch_language_menus,
                                             wrap_plain_strings)
            from core.uicheck import check_ui

            language = self.current_language()
            project = scan_project(path)

            # Сначала страховка: если в файлах уже есть `menu` без двоеточия
            # (след прошлых правок), игра не запустится — чиним до всего.
            check_syntax(project, repair=True)
            try:
                menus = patch_image_menus(project)
                menus.journal(project, language)
                texts = wrap_plain_strings(project)
                # Шрифты и пункт «Русский»: без шрифта с кириллицей надпись
                # в меню языка рисуется квадратиками.
                fonts = install_fonts(project, None, language,
                                      mode=self.cfg.font_mode, build_mode="tl")
                done_lang, lang_warnings = patch_language_menus(
                    project, language, label_for(language),
                    font_override=installed_font_rel(project, language))
            finally:
                # ...и ещё раз после правок: любые наши изменения не должны
                # оставить файл без двоеточия
                syntax = check_syntax(project, repair=True)
            engine_path, engine_count, engine_note = write_engine_rpy(
                project, language)
            report = check_ui(project, language)
            return (menus, texts, fonts, done_lang, lang_warnings, report,
                    engine_count, engine_note, syntax)

        def done(result) -> None:
            (menus, texts, fonts, done_lang, lang_warnings, report,
             engine_count, engine_note, syntax) = result
            lines = ["Меню и экраны игры", "",
                     "• " + menus.summary(),
                     "• " + texts.summary(),
                     "• " + fonts.summary(),
                     f"• строки движка Ren'Py (окна «выйти?», «в главное "
                     f"меню?»): {engine_count}",
                     f"• {syntax.summary()}"]
            lines += ["    " + message for message in done_lang]
            lines += ["    ! " + message for message in lang_warnings]
            if engine_note:
                lines.append("    " + engine_note)
            if texts.backups:
                lines.append("")
                lines.append("Оригинал экранов: " + texts.backups[0])
            lines += ["", "Повторная проверка:", ""] + report.lines()
            self._set_text(self.box_check, "\n".join(lines))
            for line in lines:
                if line.strip():
                    self.log(line.strip())
            self.tabs.set("Проверка и отчёт")

        self._run("Починка меню", task, done)

    # --- папки и файлы ----------------------------------------------------
    def do_open_game(self) -> None:
        path = (self.var_path.get() or "").strip()
        if path:
            self._open_folder(Path(path))

    def do_open_tl(self) -> None:
        path = (self.var_path.get() or "").strip()
        if not path:
            return
        try:
            project = scan_project(path)
        except ProjectError:
            return
        target = project.game_dir / "tl" / self.current_language()
        self._open_folder(target if target.is_dir()
                          else project.game_dir / "tl")

    def do_open_log(self) -> None:
        self._open_folder(Path(default_log_path()).parent)

    def do_clear_cache(self) -> None:
        if not self._ask("Очистить кэш переводов?\n\n"
                          "Все строки будут переводиться заново (полезно, если "
                          "нужно применить новый глоссарий)."):
            return
        from core.cache import TranslationCache

        cache = TranslationCache(default_cache_path(), enabled=False)
        cache.clear()
        self.log("Кэш очищен.", "ok")

    def _open_folder(self, path: Path) -> None:
        path = Path(path)
        try:
            if not path.exists():
                path = path.parent
            if os.name == "nt":
                os.startfile(str(path))  # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                import subprocess

                subprocess.Popen(["open", str(path)])
            else:
                import subprocess

                subprocess.Popen(["xdg-open", str(path)])
        except Exception as exc:  # noqa: BLE001
            logger.debug("Не удалось открыть папку %s: %s", path, exc)

    # --- справка и настройки ---------------------------------------------
    def show_help(self) -> None:
        window = ctk.CTkToplevel(self)
        window.title("Справка")
        window.geometry("660x600")
        window.after(30, lambda: self._center_over(window))
        window.transient(self)
        box = ctk.CTkTextbox(window, wrap="word")
        box.pack(fill="both", expand=True, padx=16, pady=16)
        box.insert("1.0", HELP_TEXT)
        box.configure(state="disabled")

    def open_settings(self) -> None:
        window = ctk.CTkToplevel(self)
        window.title("Настройки")
        window.geometry("660x700")
        window.minsize(520, 420)
        window.transient(self)
        window.after(30, lambda: self._center_over(window))

        head = ctk.CTkFrame(window, fg_color="transparent")
        head.pack(fill="x", padx=18, pady=(14, 0))
        ctk.CTkLabel(head, text="Настройки перевода", anchor="w",
                     font=ctk.CTkFont(size=15, weight="bold")
                     ).pack(side="left")
        ctk.CTkLabel(head, text="меняются сразу, применяются к следующему "
                                "запуску", anchor="e",
                     font=ctk.CTkFont(size=10),
                     text_color=TEXT_MUTED).pack(side="right")

        values: dict[str, tk.StringVar] = {}
        frame = ctk.CTkScrollableFrame(window)
        frame.pack(fill="both", expand=True, padx=12, pady=(8, 4))

        def field(label: str, initial: str, hint: str = "") -> None:
            ctk.CTkLabel(frame, text=label, anchor="w",
                         font=ctk.CTkFont(size=12, weight="bold")
                         ).pack(fill="x", padx=6, pady=(10, 0))
            var = tk.StringVar(value=initial)
            values[label] = var
            ctk.CTkEntry(frame, textvariable=var).pack(fill="x", padx=6)
            if hint:
                ctk.CTkLabel(frame, text=hint, anchor="w",
                             font=ctk.CTkFont(size=10),
                             text_color=("#6b7280", "#8b949e")
                             ).pack(fill="x", padx=6)

        def field_row(specs: list[tuple[str, str, str]]) -> None:
            """Три поля в ряд — чтобы настройки влезали без прокрутки."""
            row = ctk.CTkFrame(frame, fg_color="transparent")
            row.pack(fill="x", padx=6, pady=(10, 0))
            for index, (label, initial, hint) in enumerate(specs):
                row.grid_columnconfigure(index, weight=1)
                cell = ctk.CTkFrame(row, fg_color="transparent")
                cell.grid(row=0, column=index, sticky="ew",
                          padx=(0, 8) if index < len(specs) - 1 else 0)
                ctk.CTkLabel(cell, text=label, anchor="w",
                             font=ctk.CTkFont(size=12, weight="bold")
                             ).pack(fill="x")
                var = tk.StringVar(value=initial)
                values[label] = var
                ctk.CTkEntry(cell, textvariable=var).pack(fill="x")
                if hint:
                    ctk.CTkLabel(cell, text=hint, anchor="w",
                                 font=ctk.CTkFont(size=10),
                                 text_color=TEXT_MUTED).pack(fill="x")

        field("Движок перевода", self.cfg.engine,
              "google (по умолчанию), google-web, mymemory, deepl, libre")
        field_row([("Потоков", str(self.cfg.workers), "1–2 при лимитах Google"),
                   ("Строк в запросе", str(self.cfg.batch_size), "меньше — мягче"),
                   ("Пауза, с", str(self.cfg.delay), "0 — без паузы")])
        values["Пауза между запросами, с"] = values["Пауза, с"]
        field("API-ключ (DeepL)", self.cfg.api_key or "")
        field("Адрес LibreTranslate", self.cfg.libre_url or "")
        field("Глоссарий (json)", self.cfg.glossary_path or "",
              "пары «Термин»: «Перевод» — соблюдаются автоматически")
        field("Повторных попыток для неудачных строк",
              str(getattr(self.cfg, "retry_rounds", 3)),
              "если сервис ограничил запросы — программа добирает строки сама")

        switches: dict[str, tk.BooleanVar] = {}
        for text, attr in (("Резервная копия перед работой", "backup"),
                           ("Использовать кэш переводов", "use_cache"),
                           ("Шрифт PT Serif с кириллицей", "font_mode"),
                           ("Пропускать инструменты разработчика "
                            "(ActionEditor и подобное)", "skip_dev_tools"),
                           ("Проверять качество перевода", "quality_check"),
                           ("Добавить «Русский» в меню «Язык» игры "
                            "(правка screens.rpy с бэкапом)",
                            "patch_language_menu"),
                           ("Переводить меню-картинки: заменить кнопки "
                            "«Start/Load/…» текстом (правка screens.rpy)",
                            "patch_image_menus"),
                           ("Оборачивать строки экранов в _(), иначе они "
                            "не переводятся (правка .rpy игры)",
                            "wrap_screen_text"),
                           ("Переводить строки движка Ren'Py "
                            "(окна подтверждения, быстрые сохранения)",
                            "engine_strings")):
            if attr == "font_mode":
                var = tk.BooleanVar(value=self.cfg.font_mode == "pretty")
            else:
                var = tk.BooleanVar(value=bool(getattr(self.cfg, attr)))
            switches[attr] = var
            ctk.CTkSwitch(frame, text=text, variable=var,
                          font=ctk.CTkFont(size=12)).pack(anchor="w", padx=6,
                                                          pady=(10, 0))

        def apply_and_close() -> None:
            def as_int(name: str, fallback: int) -> int:
                try:
                    return max(1, int(values[name].get()))
                except (KeyError, ValueError):
                    return fallback

            self.cfg.engine = values["Движок перевода"].get().strip() or "google"
            self.cfg.workers = as_int("Потоков", 4)
            self.cfg.batch_size = as_int("Строк в запросе", 25)
            try:
                self.cfg.delay = max(0.0, float(values["Пауза, с"].get()))
            except (KeyError, ValueError):
                self.cfg.delay = 0.0
            self.cfg.api_key = values["API-ключ (DeepL)"].get().strip()
            self.cfg.libre_url = values["Адрес LibreTranslate"].get().strip()
            self.cfg.glossary_path = values["Глоссарий (json)"].get().strip()
            try:
                self.cfg.retry_rounds = max(
                    0, int(values["Повторных попыток для неудачных строк"].get()))
            except (KeyError, ValueError):
                self.cfg.retry_rounds = 3
            self.cfg.backup = bool(switches["backup"].get())
            self.cfg.use_cache = bool(switches["use_cache"].get())
            self.cfg.font_mode = "pretty" if switches["font_mode"].get() else "fix"
            self.cfg.skip_dev_tools = bool(switches["skip_dev_tools"].get())
            self.cfg.patch_language_menu = bool(
                switches["patch_language_menu"].get())
            self.var_patch_menu.set(bool(self.cfg.patch_language_menu))
            self.cfg.patch_image_menus = bool(
                switches["patch_image_menus"].get())
            self.var_patch_img.set(bool(self.cfg.patch_image_menus))
            self.cfg.wrap_screen_text = bool(
                switches["wrap_screen_text"].get())
            self.var_wrap_text.set(bool(self.cfg.wrap_screen_text))
            self.cfg.engine_strings = bool(
                switches["engine_strings"].get())
            self.cfg.quality_check = bool(switches["quality_check"].get())
            self.var_quality.set(bool(self.cfg.quality_check))
            self.cfg.save()
            self.log("Настройки сохранены.", "ok")
            window.destroy()

        footer = ctk.CTkFrame(window, fg_color="transparent")
        footer.pack(fill="x", padx=18, pady=(0, 14))
        ctk.CTkButton(footer, text="Сохранить", height=40,
                      command=apply_and_close).pack(side="left", expand=True,
                                                    fill="x")
        outline(footer, "Закрыть", window.destroy, width=140,
                height=40).pack(side="left", padx=(10, 0))

    # ------------------------------------------------------------------
    def _bind_shortcuts(self) -> None:
        for sequence, handler in (
                ("<Control-o>", lambda _e: self.on_pick_folder()),
                ("<Control-r>", lambda _e: self.on_scan()),
                ("<F5>", lambda _e: self.do_auto() if not self._busy else None),
                ("<Control-e>", lambda _e: self.do_export() if not self._busy else None),
                ("<Control-i>", lambda _e: self.do_import() if not self._busy else None),
                ("<Control-p>", lambda _e: self.do_check() if not self._busy else None),
                ("<Control-h>", lambda _e: self.do_report() if not self._busy else None),
                ("<Escape>", lambda _e: self.on_cancel() if self._busy else None),
                ("<F1>", lambda _e: self.show_help())):
            try:
                self.bind(sequence, handler)
            except tk.TclError:
                pass

    def _on_close(self) -> None:
        try:
            self.save_settings()
        finally:
            self.destroy()


# ===========================================================================
HELP_TEXT = """\
RenPyTranslator — перевод визуальных новелл Ren'Py

БЫСТРЫЙ СТАРТ
1. Укажите папку игры (обычно ...\\MyNovel или ...\\MyNovel\\game).
2. Выберите язык перевода (по умолчанию русский).
3. Нажмите «Сделать всё автоматически».

Язык оригинала определяется автоматически: английский, китайский, японский
и любой другой — Google понимает исходный язык сам, указывать его не нужно.

ЛИТЕРАТУРНЫЙ ПЕРЕВОД
«Литературный текст» → «Экспорт текста для агента» → отдайте файл вашему
AI-агенту → «Импорт правок в игру». Агенту нужно сохранить служебные пометки
[#файл:строка], строки EN:, теги {b} и подстановки [имя].

ПРОВЕРКА
«Проверка и отчёт» → «Проверить перевод» (без запуска игры) и «HTML-отчёт»
со всеми подробностями, включая потерянные подстановки.

ЕСЛИ ЧТО-ТО ПОШЛО НЕ ТАК
• «Откатить бэкап» вернёт файлы игры.
• Кэш сохраняет переведённое: повторный запуск продолжит с места остановки.
• Журнал: вкладка «Журнал», кнопка «Открыть лог-файл» в «Проверка и отчёт».

ГОРЯЧИЕ КЛАВИШИ
Ctrl+O — папка, Ctrl+R — проверить папку, F5 — сделать всё,
Ctrl+E — экспорт, Ctrl+I — импорт, Ctrl+P — проверка перевода,
Ctrl+H — отчёт, Esc — стоп, F1 — справка.
"""


def run_modern_gui(settings=None) -> None:
    """Запуск современного интерфейса."""
    app = App(settings)

    def report_callback_exception(exc_type, exc, tb) -> None:  # noqa: ANN001
        logger.error("Ошибка интерфейса: %s",
                     "".join(traceback.format_exception(exc_type, exc, tb)))
        try:
            self._error(f"Внутренняя ошибка:\n\n{exc}")

        except Exception:  # noqa: BLE001
            pass

    app.report_callback_exception = report_callback_exception  # type: ignore[assignment]
    app.mainloop()


if __name__ == "__main__":
    run_modern_gui()
