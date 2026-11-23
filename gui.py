#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Графическое приложение RenPyTranslator.

Одно окно, три больших блока:

  «Быстрый старт»        — одна кнопка: найти текст, перевести, собрать файлы;
  «Литературный перевод» — экспорт текста для AI-агента и импорт правок обратно;
  «Инструменты»          — подготовка .rpa/.rpyc, отдельные шаги, папки, кэш.

Настройки (движок, ключ API, потоки, режим) прячутся за кнопкой «⚙» и
сохраняются в settings.json рядом с программой.
"""

from __future__ import annotations

import base64
import logging
import os
import queue
import subprocess
import sys
import threading
import time
import traceback
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Callable, Optional

from config import (
    APP_NAME,
    SUPPORTED_ENGINES,
    VERSION,
    Settings,
    default_cache_path,
    default_log_path,
)
from core.appconfig import UserConfig, reset as reset_config
from core.pipeline import Pipeline
from core.scanner import ProjectError, scan_project
from core.utils import get_logger, human_duration, setup_logging

logger = get_logger(__name__)

# ===========================================================================
# Палитра и шрифты
# ===========================================================================
THEMES = {
    "dark": {
        "bg": "#0d1117",
        "panel": "#151c28",
        "panel_alt": "#1b2434",
        "border": "#25314a",
        "text": "#e9eff8",
        "muted": "#8e9cb3",
        "accent": "#4c7bef",
        "accent_hi": "#6390ff",
        "accent_lo": "#3a62c9",
        "ok": "#37d07d",
        "warn": "#ffb454",
        "err": "#ff6b6b",
        "violet": "#8b5cf6",
        "log_bg": "#0b0f16",
        "log_text": "#cfd8e6",
        "chip": "#1b2434",
        "hover": "#26324a",
    },
    "light": {
        "bg": "#f4f6fb",
        "panel": "#ffffff",
        "panel_alt": "#eef1f8",
        "border": "#d7dce8",
        "text": "#1b2130",
        "muted": "#5f6b80",
        "accent": "#3b6fe0",
        "accent_hi": "#2f61d0",
        "accent_lo": "#2a55b8",
        "ok": "#1a9e5f",
        "warn": "#b26a00",
        "err": "#c8393a",
        "violet": "#7c4dff",
        "log_bg": "#fbfcfe",
        "log_text": "#2b3444",
        "chip": "#eef1f8",
        "hover": "#e3e8f4",
    },
}

#: Текущая палитра (заполняется в apply_theme до создания виджетов).
P: dict[str, str] = dict(THEMES["dark"])


def apply_theme(name: str) -> str:
    """Переключить палитру до построения интерфейса."""
    key = name if name in THEMES else "dark"
    P.clear()
    P.update(THEMES[key])
    return key


def enable_high_dpi() -> None:
    """Чёткий интерфейс на мониторах с масштабом 125–200 % (Windows)."""
    if os.name != "nt":
        return
    try:
        import ctypes

        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:  # noqa: BLE001
            ctypes.windll.user32.SetProcessDPIAware()
    except Exception:  # noqa: BLE001
        pass

IS_WINDOWS = os.name == "nt"

#: Как часто писать «пульс» в журнал во время долгой работы, сек.
HEARTBEAT_SECONDS = 4.0
FONT_UI = "Segoe UI" if IS_WINDOWS else "DejaVu Sans"
FONT_MONO = "Consolas" if IS_WINDOWS else "DejaVu Sans Mono"

#: Иконка приложения, встроенная в код (чтобы не зависеть от внешних файлов).
ICON_B64 = None  # заполняется ниже, если assets/icon_256.png доступен

#: Языки, которые можно выбрать в интерфейсе (движок Ren'Py ждёт tl/<lang>/).
from core.langs import LANGUAGE_LABELS, label_for as _label_for  # noqa: E402

UI_LANGUAGES = tuple(sorted(LANGUAGE_LABELS, key=lambda code: _label_for(code)))


def _load_icon_b64() -> Optional[str]:
    """Иконка: сначала из упакованных ресурсов, затем из assets/."""
    candidates = []
    base = getattr(sys, "_MEIPASS", None)
    if base:
        candidates.append(Path(base) / "assets" / "icon_256.png")
    candidates.append(Path(__file__).resolve().parent / "assets" / "icon_256.png")
    for path in candidates:
        try:
            if path.exists():
                return base64.b64encode(path.read_bytes()).decode("ascii")
        except OSError:
            continue
    return None


# ===========================================================================
# Вспомогательные виджеты
# ===========================================================================
class Card(tk.Frame):
    """Панель с заголовком и подписью."""

    def __init__(self, parent: tk.Misc, title: str, subtitle: str = "",
                 accent: str = P["accent"]) -> None:
        super().__init__(parent, bg=P["panel"], highlightthickness=1,
                         highlightbackground=P["border"])
        self.accent = accent

        head = tk.Frame(self, bg=P["panel"])
        head.pack(fill="x", padx=16, pady=(14, 0))

        bar = tk.Frame(head, bg=accent, width=4, height=18)
        bar.pack(side="left", padx=(0, 10))
        bar.pack_propagate(False)

        tk.Label(head, text=title, bg=P["panel"], fg=P["text"],
                 font=(FONT_UI, 11, "bold"), anchor="w").pack(side="left")

        if subtitle:
            tk.Label(self, text=subtitle, bg=P["panel"], fg=P["muted"],
                     font=(FONT_UI, 9), anchor="w", justify="left",
                     wraplength=280).pack(fill="x", padx=16, pady=(6, 0))

        self.body = tk.Frame(self, bg=P["panel"])
        self.body.pack(fill="both", expand=True, padx=16, pady=(10, 14))


class CircleBadge(tk.Canvas):
    """Кружок с номером шага или галочкой."""

    def __init__(self, parent: tk.Misc, number: int, size: int = 26) -> None:
        super().__init__(parent, width=size, height=size, bg=P["panel"],
                         highlightthickness=0)
        self.size = size
        self.number = number
        self.state = "idle"
        self._draw()

    def set_state(self, state: str) -> None:
        if state != self.state:
            self.state = state
            self._draw()

    def _draw(self) -> None:
        self.delete("all")
        s = self.size
        color = {"idle": P["border"], "active": P["accent"],
                 "done": P["ok"]}.get(self.state, P["border"])
        self.create_oval(1, 1, s - 1, s - 1, fill=color, outline=color)
        text = "✓" if self.state == "done" else str(self.number)
        self.create_text(s / 2, s / 2 + 1, text=text, fill="#0d1117",
                         font=(FONT_UI, int(s * 0.45), "bold"))



# ===========================================================================
# Окно настроек
# ===========================================================================
#: Движки, которые показываем пользователю (служебные mock/none — только в CLI).
GUI_ENGINES = ("google", "google-web", "mymemory", "deepl", "libre")

ENGINE_HINTS = {
    "google": "Бесплатно, без ключа. Google Translate, строки уходят пачками "
              "по 40 — почти не упирается в лимит запросов. Рекомендуется.",
    "google-web": "Запасной путь к тому же Google (по одной строке) — "
                  "включите, если основной движок отвечает ошибкой 429.",
    "mymemory": "Бесплатный резерв без ключа (дневной лимит знаков). "
                "Качество ниже, чем у Google; выручает, когда Google молчит.",
    "deepl": "Нужен ключ DeepL API (бесплатный тариф до 500 000 знаков в месяц). "
             "Качество выше, чем у Google.",
    "libre": "Свой сервер LibreTranslate (self-hosted). Укажите адрес и, если "
             "нужно, ключ.",
}


class SettingsDialog(tk.Toplevel):
    """Модальное окно настроек перевода."""

    def __init__(self, app: "App") -> None:
        super().__init__(app)
        self.app = app
        self.cfg = app.cfg

        self.title("Настройки — " + APP_NAME)
        self.configure(bg=P["panel"])
        self.resizable(False, False)
        self.transient(app)
        self.grab_set()

        self._build()
        self._load()

        self.update_idletasks()
        try:
            x = app.winfo_rootx() + (app.winfo_width() - self.winfo_width()) // 2
            y = app.winfo_rooty() + 90
            self.geometry(f"+{max(0, x)}+{max(0, y)}")
        except tk.TclError:
            pass

        self.bind("<Escape>", lambda _e: self.destroy())

    # ------------------------------------------------------------------
    def _build(self) -> None:
        root = tk.Frame(self, bg=P["panel"])
        root.pack(fill="both", expand=True, padx=22, pady=18)

        tk.Label(root, text="Настройки перевода", bg=P["panel"], fg=P["text"],
                 font=(FONT_UI, 13, "bold")).grid(row=0, column=0, columnspan=2,
                                                  sticky="w")
        tk.Label(root, text="Менять их нужно редко — значения по умолчанию подходят "
                            "почти всем.",
                 bg=P["panel"], fg=P["muted"], font=(FONT_UI, 9)).grid(
            row=1, column=0, columnspan=2, sticky="w", pady=(2, 14))

        row = 2

        def field_label(text: str) -> None:
            nonlocal row
            tk.Label(root, text=text, bg=P["panel"], fg=P["text"], font=(FONT_UI, 10),
                     anchor="w").grid(row=row, column=0, sticky="w", pady=6,
                                      padx=(0, 16))

        # движок
        field_label("Движок перевода")
        self.var_engine = tk.StringVar()
        combo = ttk.Combobox(root, textvariable=self.var_engine, state="readonly",
                             width=44, values=list(GUI_ENGINES))
        combo.grid(row=row, column=1, sticky="w", pady=6)
        combo.bind("<<ComboboxSelected>>", lambda _e: self._update_hint())
        row += 1

        self.lbl_hint = tk.Label(root, text="", bg=P["panel"], fg=P["muted"],
                                 font=(FONT_UI, 9), wraplength=460, justify="left")
        self.lbl_hint.grid(row=row, column=1, sticky="w", pady=(0, 8))
        row += 1

        # ключ
        field_label("Ключ API")
        key_wrap = tk.Frame(root, bg=P["panel"])
        key_wrap.grid(row=row, column=1, sticky="w", pady=6)
        self.var_key = tk.StringVar()
        self.entry_key = ttk.Entry(key_wrap, textvariable=self.var_key, width=36,
                                   show="•")
        self.entry_key.pack(side="left")
        self.btn_key = ttk.Button(key_wrap, text="показать", style="Ghost.TButton",
                                  command=self._toggle_key)
        self.btn_key.pack(side="left", padx=(6, 0))
        row += 1

        # адрес LibreTranslate
        field_label("Адрес LibreTranslate")
        self.var_libre = tk.StringVar()
        ttk.Entry(root, textvariable=self.var_libre, width=46).grid(
            row=row, column=1, sticky="w", pady=6)
        row += 1

        # производительность
        field_label("Скорость")
        speed = tk.Frame(root, bg=P["panel"])
        speed.grid(row=row, column=1, sticky="w", pady=6)
        self.var_workers = tk.IntVar()
        self.var_batch = tk.IntVar()
        self.var_delay = tk.DoubleVar()

        tk.Label(speed, text="потоков", bg=P["panel"], fg=P["muted"],
                 font=(FONT_UI, 9)).pack(side="left", padx=(0, 4))
        ttk.Spinbox(speed, from_=1, to=8, width=4, textvariable=self.var_workers).pack(
            side="left", padx=(0, 14))
        tk.Label(speed, text="строк в запросе", bg=P["panel"], fg=P["muted"],
                 font=(FONT_UI, 9)).pack(side="left", padx=(0, 4))
        ttk.Spinbox(speed, from_=1, to=100, width=4, textvariable=self.var_batch).pack(
            side="left", padx=(0, 14))
        tk.Label(speed, text="пауза, с", bg=P["panel"], fg=P["muted"],
                 font=(FONT_UI, 9)).pack(side="left", padx=(0, 4))
        ttk.Spinbox(speed, from_=0, to=5, increment=0.5, width=4,
                    textvariable=self.var_delay).pack(side="left")
        row += 1

        # режим сборки
        field_label("Режим сборки")
        mode = tk.Frame(root, bg=P["panel"])
        mode.grid(row=row, column=1, sticky="w", pady=6)
        self.var_mode = tk.StringVar()
        ttk.Radiobutton(mode, text="tl/ — отдельные файлы перевода (безопасно)",
                        variable=self.var_mode, value="tl").pack(anchor="w")
        ttk.Radiobutton(mode, text="replace — правка самих .rpy (для сборок без tl)",
                        variable=self.var_mode, value="replace").pack(anchor="w")
        row += 1

        # переключатели
        field_label("Что переводить")
        checks = tk.Frame(root, bg=P["panel"])
        checks.grid(row=row, column=1, sticky="w", pady=6)
        self.var_names = tk.BooleanVar()
        self.var_ui = tk.BooleanVar()
        self.var_backup = tk.BooleanVar()
        self.var_cache = tk.BooleanVar()
        self.var_default_lang = tk.BooleanVar()
        self.var_hook = tk.BooleanVar()
        for text, var in (("имена персонажей", self.var_names),
                          ("интерфейс и меню", self.var_ui),
                          ("бэкап скриптов", self.var_backup),
                          ("использовать кэш", self.var_cache)):
            ttk.Checkbutton(checks, text=text, variable=var).pack(anchor="w")
        row += 1

        field_label("Поведение в игре")
        behaviour = tk.Frame(root, bg=P["panel"])
        behaviour.grid(row=row, column=1, sticky="w", pady=6)
        ttk.Checkbutton(behaviour, text="включать русский язык автоматически",
                        variable=self.var_default_lang).pack(anchor="w")
        ttk.Checkbutton(behaviour,
                        text="страховка: подмена текста на лету (runtime-хук)",
                        variable=self.var_hook).pack(anchor="w")
        row += 1

        # шрифт
        field_label("Шрифт в игре")
        fonts = tk.Frame(root, bg=P["panel"])
        fonts.grid(row=row, column=1, sticky="w", pady=6)
        self.var_fonts = tk.StringVar()
        ttk.Radiobutton(
            fonts, text="красивый PT Serif для всего текста (полная кириллица)",
            variable=self.var_fonts, value="pretty").pack(anchor="w")
        ttk.Radiobutton(
            fonts, text="только починить шрифты без русских букв",
            variable=self.var_fonts, value="fix").pack(anchor="w")
        ttk.Radiobutton(fonts, text="не трогать шрифты игры",
                        variable=self.var_fonts, value="off").pack(anchor="w")
        row += 1

        # глоссарий
        field_label("Глоссарий (JSON)")
        gloss = tk.Frame(root, bg=P["panel"])
        gloss.grid(row=row, column=1, sticky="w", pady=6)
        self.var_glossary = tk.StringVar()
        ttk.Entry(gloss, textvariable=self.var_glossary, width=34).pack(side="left")
        ttk.Button(gloss, text="Выбрать…", style="Ghost.TButton",
                   command=self._pick_glossary).pack(side="left", padx=(6, 0))
        row += 1

        # кнопки
        foot = tk.Frame(root, bg=P["panel"])
        foot.grid(row=row, column=0, columnspan=2, sticky="ew", pady=(18, 0))
        ttk.Button(foot, text="Сбросить всё", style="Ghost.TButton",
                   command=self._reset).pack(side="left")
        ttk.Button(foot, text="Отмена", style="Ghost.TButton",
                   command=self.destroy).pack(side="right")
        ttk.Button(foot, text="Сохранить", style="Accent.TButton",
                   command=self._save).pack(side="right", padx=(0, 8))

        root.columnconfigure(1, weight=1)
        self._update_hint()

    # ------------------------------------------------------------------
    def _update_hint(self) -> None:
        self.lbl_hint.configure(text=ENGINE_HINTS.get(self.var_engine.get(), ""))

    def _toggle_key(self) -> None:
        hidden = bool(self.entry_key.cget("show"))
        self.entry_key.configure(show="" if hidden else "•")
        self.btn_key.configure(text="скрыть" if hidden else "показать")

    def _pick_glossary(self) -> None:
        path = filedialog.askopenfilename(
            parent=self, title="Файл глоссария (JSON)",
            filetypes=[("JSON", "*.json"), ("Все файлы", "*.*")])
        if path:
            self.var_glossary.set(path)

    def _load(self) -> None:
        cfg = self.cfg
        self.var_engine.set(cfg.engine)
        self.var_key.set(cfg.api_key)
        self.var_libre.set(cfg.libre_url)
        self.var_workers.set(cfg.workers)
        self.var_batch.set(cfg.batch_size)
        self.var_delay.set(cfg.delay)
        self.var_mode.set(cfg.mode if cfg.mode in ("tl", "replace") else "tl")
        self.var_glossary.set(cfg.glossary_path)
        self.var_fonts.set(cfg.font_mode if cfg.font_mode in ("pretty", "fix", "off")
                           else "pretty")
        self.var_names.set(cfg.extract_names)
        self.var_ui.set(cfg.extract_ui)
        self.var_backup.set(cfg.backup)
        self.var_cache.set(cfg.use_cache)
        self.var_default_lang.set(cfg.set_default_language)
        self.var_hook.set(cfg.hook_fallback)
        if cfg.engine not in GUI_ENGINES:
            self.var_engine.set(GUI_ENGINES[0])
        self._update_hint()

    def _save(self) -> None:
        cfg = self.cfg
        cfg.engine = self.var_engine.get()
        cfg.api_key = self.var_key.get().strip()
        cfg.libre_url = self.var_libre.get().strip()
        try:
            cfg.workers = max(1, int(self.var_workers.get()))
        except (tk.TclError, ValueError):
            cfg.workers = 4
        try:
            cfg.batch_size = max(1, int(self.var_batch.get()))
        except (tk.TclError, ValueError):
            cfg.batch_size = 25
        try:
            cfg.delay = max(0.0, float(self.var_delay.get()))
        except (tk.TclError, ValueError):
            cfg.delay = 0.0
        cfg.mode = self.var_mode.get()
        cfg.font_mode = self.var_fonts.get() or "pretty"
        cfg.glossary_path = self.var_glossary.get().strip()
        cfg.extract_names = bool(self.var_names.get())
        cfg.extract_ui = bool(self.var_ui.get())
        cfg.backup = bool(self.var_backup.get())
        cfg.use_cache = bool(self.var_cache.get())
        cfg.set_default_language = bool(self.var_default_lang.get())
        cfg.hook_fallback = bool(self.var_hook.get())
        cfg.save()
        self.app.refresh_flags()
        self.app.log("Настройки сохранены. Движок перевода: " + cfg.engine, "ok")
        self.destroy()

    def _reset(self) -> None:
        if not messagebox.askyesno(APP_NAME, "Сбросить все настройки?"):
            return
        reset_config()
        self.cfg.__dict__.update(UserConfig().__dict__)
        self._load()
        self._update_hint()


# ===========================================================================
# Приложение
# ===========================================================================
ICON_B64_CACHE: Optional[str] = None


class App(tk.Tk):
    def __init__(self, initial: Optional[Settings] = None) -> None:
        super().__init__()

        self.cfg = UserConfig.load()
        if initial is not None and initial.game_path:
            self.cfg.last_game_path = str(initial.game_path)

        self.title(f"{APP_NAME} {VERSION} — русификатор визуальных новелл Ren'Py")
        self.geometry("1140x820")
        self.minsize(1010, 720)
        self.configure(bg=P["bg"])
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        self.log_queue: "queue.Queue[tuple[str, str]]" = queue.Queue()
        self.ui_queue: "queue.Queue[Callable[[], None]]" = queue.Queue()
        self.project = None
        self.pipeline: Optional[Pipeline] = None
        self.worker: Optional[threading.Thread] = None
        self._cancel = threading.Event()
        self._busy = False
        self._busy_started = 0.0
        self._busy_label = ""
        self._last_progress_text = ""
        self._last_export_dir: Optional[Path] = None
        self.var_font_mode = tk.StringVar(value=self.cfg.font_mode)

        self._setup_style()
        self._set_window_icon()
        self._build_ui()
        self._install_log_handler()
        self._poll_log()

        self._bind_shortcuts()

        if self.cfg.last_game_path:
            self.var_game.set(self.cfg.last_game_path)
            self.after(200, self.on_scan)

        self.log("Добро пожаловать! Выберите папку игры — дальше всё сделает программа.",
                 "info")

    def _bind_shortcuts(self) -> None:
        """Горячие клавиши: всё делается одной рукой."""
        bound = (
            ("<Control-o>", lambda _e: self.on_pick_folder()),
            ("<Control-r>", lambda _e: self.on_scan()),
            ("<F5>", lambda _e: (not self._busy) and self.do_auto()),
            ("<Control-e>", lambda _e: (not self._busy) and self.do_export()),
            ("<Control-i>", lambda _e: (not self._busy) and self.do_import()),
            ("<Control-p>", lambda _e: (not self._busy) and self.do_check()),
            ("<Control-h>", lambda _e: (not self._busy) and self.do_report()),
            ("<Escape>", lambda _e: self.on_cancel() if self._busy else None),
            ("<F1>", lambda _e: self.show_help()),
        )
        for sequence, handler in bound:
            try:
                self.bind(sequence, handler)
            except tk.TclError:  # pragma: no cover
                pass

    # ------------------------------------------------------------------
    # Оформление
    # ------------------------------------------------------------------
    def _setup_style(self) -> None:
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass

        style.configure(".", background=P["bg"], foreground=P["text"],
                        font=(FONT_UI, 10), borderwidth=0, focuscolor=P["accent"])

        style.configure("TFrame", background=P["bg"])
        style.configure("Panel.TFrame", background=P["panel"])

        style.configure("TLabel", background=P["bg"], foreground=P["text"])
        style.configure("Panel.TLabel", background=P["panel"], foreground=P["text"])
        style.configure("Muted.TLabel", background=P["panel"], foreground=P["muted"],
                        font=(FONT_UI, 9))

        # --- кнопки ---
        style.configure("Accent.TButton", background=P["accent"], foreground="#ffffff",
                        font=(FONT_UI, 11, "bold"), padding=(16, 10), borderwidth=0)
        style.map("Accent.TButton",
                  background=[("active", P["accent_hi"]), ("pressed", P["accent_lo"]),
                              ("disabled", "#2b3550")],
                  foreground=[("disabled", "#7c8699")])

        style.configure("TButton", background=P["panel_alt"], foreground=P["text"],
                        font=(FONT_UI, 10), padding=(12, 7), borderwidth=0)
        style.map("TButton",
                  background=[("active", "#26324a"), ("pressed", "#1a2233"),
                              ("disabled", "#1a2130")],
                  foreground=[("disabled", "#6b7688")])

        style.configure("Ghost.TButton", background=P["panel"], foreground=P["muted"],
                        padding=(10, 6))
        style.map("Ghost.TButton", background=[("active", P["panel_alt"])],
                  foreground=[("active", P["text"])])

        style.configure("Danger.TButton", background=P["panel_alt"], foreground=P["warn"])

        # --- прочее ---
        style.configure("TEntry", fieldbackground=P["panel_alt"], foreground=P["text"],
                        insertcolor=P["text"], bordercolor=P["border"],
                        lightcolor=P["border"], darkcolor=P["border"], padding=6)
        style.map("TEntry", bordercolor=[("focus", P["accent"])])

        style.configure("TCombobox", fieldbackground=P["panel_alt"], background=P["panel_alt"],
                        foreground=P["text"], arrowcolor=P["text"], bordercolor=P["border"],
                        padding=5)
        style.map("TCombobox",
                  fieldbackground=[("readonly", P["panel_alt"])],
                  foreground=[("readonly", P["text"])])

        style.configure("TSpinbox", fieldbackground=P["panel_alt"], foreground=P["text"],
                        arrowcolor=P["text"], bordercolor=P["border"], padding=4)

        style.configure("TCheckbutton", background=P["panel"], foreground=P["text"],
                        font=(FONT_UI, 9))
        style.map("TCheckbutton", background=[("active", P["panel"])],
                  foreground=[("active", P["text"])])

        style.configure("TRadiobutton", background=P["panel"], foreground=P["text"],
                        font=(FONT_UI, 10))
        style.map("TRadiobutton", background=[("active", P["panel"])])

        style.configure("Accent.Horizontal.TProgressbar", troughcolor=P["panel_alt"],
                        background=P["accent"], bordercolor=P["panel_alt"],
                        lightcolor=P["panel_alt"], darkcolor=P["panel_alt"],
                        thickness=10)

        style.configure("TSeparator", background=P["border"])

    def _set_window_icon(self) -> None:
        global ICON_B64_CACHE
        try:
            if ICON_B64_CACHE is None:
                ICON_B64_CACHE = _load_icon_b64()
            if ICON_B64_CACHE:
                self._icon_img = tk.PhotoImage(data=ICON_B64_CACHE)
                self.iconphoto(True, self._icon_img)
        except Exception:  # noqa: BLE001
            pass
        if IS_WINDOWS:
            try:
                base = getattr(sys, "_MEIPASS", Path(__file__).resolve().parent)
                ico = Path(base) / "assets" / "icon.ico"
                if ico.exists():
                    self.iconbitmap(str(ico))
            except Exception:  # noqa: BLE001
                pass

    # ------------------------------------------------------------------
    # Разметка
    # ------------------------------------------------------------------
    def _build_ui(self) -> None:
        self._build_header()

        body = tk.Frame(self, bg=P["bg"])
        body.pack(fill="both", expand=True, padx=18, pady=(0, 8))
        body.columnconfigure(0, weight=1)

        self._build_folder_card(body)
        self._build_actions(body)
        self._build_progress(body)
        self._build_log(body)

        self._build_footer()

    # --- шапка ---------------------------------------------------------
    def _build_header(self) -> None:
        header = tk.Frame(self, bg=P["panel"])
        header.pack(fill="x")

        inner = tk.Frame(header, bg=P["panel"])
        inner.pack(fill="x", padx=18, pady=12)

        try:
            global ICON_B64_CACHE
            if ICON_B64_CACHE is None:
                ICON_B64_CACHE = _load_icon_b64()
            if ICON_B64_CACHE:
                img = tk.PhotoImage(data=ICON_B64_CACHE)
                small = img.subsample(4, 4)
                lbl = tk.Label(inner, image=small, bg=P["panel"])
                lbl.image = small
                lbl.pack(side="left", padx=(0, 12))
        except Exception:  # noqa: BLE001
            pass

        titles = tk.Frame(inner, bg=P["panel"])
        titles.pack(side="left")
        tk.Label(titles, text=APP_NAME, bg=P["panel"], fg=P["text"],
                 font=(FONT_UI, 15, "bold"), anchor="w").pack(anchor="w")
        tk.Label(titles, text="Русификатор визуальных новелл на движке Ren'Py",
                 bg=P["panel"], fg=P["muted"], font=(FONT_UI, 9)).pack(anchor="w")

        right = tk.Frame(inner, bg=P["panel"])
        right.pack(side="right")

        self.btn_settings = ttk.Button(right, text="⚙  Настройки",
                                       style="Ghost.TButton",
                                       command=self.open_settings)
        self.btn_settings.pack(side="right", padx=(10, 0))
        self.btn_help = ttk.Button(right, text="?  Справка", style="Ghost.TButton",
                                   command=self.show_help)
        self.btn_help.pack(side="right")
        self.btn_theme = ttk.Button(right, text=self._theme_button_text(),
                                    style="Ghost.TButton", command=self.toggle_theme)
        self.btn_theme.pack(side="right", padx=(0, 6))

        tk.Label(right, text=f"v{VERSION}", bg=P["panel"], fg=P["muted"],
                 font=(FONT_UI, 9)).pack(side="right", padx=(0, 14))

        tk.Frame(self, bg=P["border"], height=1).pack(fill="x")

    # --- папка игры ----------------------------------------------------
    def _build_folder_card(self, parent: tk.Misc) -> None:
        card = tk.Frame(parent, bg=P["panel"], highlightthickness=1,
                        highlightbackground=P["border"])
        card.grid(row=0, column=0, sticky="ew", pady=(14, 10))

        row = tk.Frame(card, bg=P["panel"])
        row.pack(fill="x", padx=16, pady=(14, 0))

        tk.Label(row, text="Папка игры", bg=P["panel"], fg=P["text"],
                 font=(FONT_UI, 11, "bold")).pack(side="left", padx=(0, 14))

        self.var_game = tk.StringVar()
        self.entry_game = ttk.Entry(row, textvariable=self.var_game,
                                    font=(FONT_UI, 10))
        self.entry_game.pack(side="left", fill="x", expand=True, padx=(0, 10))
        self.entry_game.bind("<Return>", lambda _e: self.on_scan())

        ttk.Button(row, text="Выбрать папку…", command=self.on_pick_folder).pack(side="left")
        ttk.Button(row, text="Проверить", command=self.on_scan).pack(side="left", padx=(8, 0))

        tk.Label(row, text="Язык:", bg=P["panel"], fg=P["muted"],
                 font=(FONT_UI, 9)).pack(side="left", padx=(14, 4))
        self.var_lang_label = tk.StringVar(
            value=self._lang_label(self.cfg.target_lang or "russian"))
        self.cmb_lang = ttk.Combobox(row, textvariable=self.var_lang_label,
                                     width=18, state="readonly",
                                     values=[self._lang_label(code)
                                             for code in UI_LANGUAGES])
        self.cmb_lang.pack(side="left")
        self.cmb_lang.bind("<<ComboboxSelected>>",
                           lambda _e: self.save_settings())

        self.lbl_scan = tk.Label(card, text="Папка не выбрана", bg=P["panel"],
                                 fg=P["muted"], font=(FONT_UI, 9), anchor="w",
                                 justify="left")
        self.lbl_scan.pack(fill="x", padx=16, pady=(8, 14))

    # --- три блока действий -------------------------------------------
    def _build_actions(self, parent: tk.Misc) -> None:
        holder = tk.Frame(parent, bg=P["bg"])
        holder.grid(row=1, column=0, sticky="nsew")
        for col in range(3):
            holder.columnconfigure(col, weight=1, uniform="cards")

        # 1. Быстрый старт
        quick = Card(holder, "Быстрый старт",
                     "Одна кнопка: найти текст, перевести и собрать файлы перевода.",
                     accent=P["accent"])
        quick.grid(row=0, column=0, sticky="nsew", padx=(0, 8))

        self.btn_auto = ttk.Button(quick.body, text="▶   Сделать всё автоматически",
                                   style="Accent.TButton", command=self.do_auto)
        self.btn_auto.pack(fill="x")
        tk.Label(quick.body, text="Всё включено: перевод, шрифт с кириллицей, "
                                  "включение языка",
                 bg=P["panel"], fg=P["muted"], font=(FONT_UI, 9),
                 wraplength=280, justify="left").pack(fill="x", pady=(10, 0))

        self.chk_default_lang = ttk.Checkbutton(
            quick.body,
            text="Включать русский язык автоматически",
            variable=self._var_flag("set_default_language"),
            command=self.save_settings)
        self.chk_default_lang.pack(anchor="w", pady=(8, 0))

        self.chk_button = ttk.Checkbutton(
            quick.body, text="Кнопка языка в меню игры",
            variable=self._var_flag("language_button"),
            command=self.save_settings)
        self.chk_button.pack(anchor="w")

        self.chk_patch_menu = ttk.Checkbutton(
            quick.body, text="Вписать «Русский» в screens.rpy",
            variable=self._var_flag("patch_language_menu"),
            command=self.save_settings)
        self.chk_patch_menu.pack(anchor="w")

        self.chk_hook = ttk.Checkbutton(
            quick.body, text="Страховка: подмена на лету",
            variable=self._var_flag("hook_fallback"),
            command=self.save_settings)
        self.chk_hook.pack(anchor="w")

        self.var_pretty_font = tk.BooleanVar(value=self.cfg.font_mode == "pretty")
        self.chk_font = ttk.Checkbutton(
            quick.body, text="Красивый шрифт PT Serif",
            variable=self.var_pretty_font, command=self._on_pretty_font_toggle)
        self.chk_font.pack(anchor="w")

        # 2. Литературный перевод
        lit = Card(holder, "Литературный перевод",
                   "Экспорт → правки у AI-агента → импорт. Лучшее качество текста.",
                   accent=P["violet"])
        lit.grid(row=0, column=1, sticky="nsew", padx=8)

        ttk.Button(lit.body, text="1.  Экспорт текста для агента",
                   command=self.do_export).pack(fill="x")
        self.btn_import = ttk.Button(lit.body, text="2.  Импорт правок в игру",
                                     command=self.do_import)
        self.btn_import.pack(fill="x", pady=(8, 0))
        tk.Label(lit.body, text="Подойдёт файл .txt, .json или .docx — тот, что "
                                "вернул агент.",
                 bg=P["panel"], fg=P["muted"], font=(FONT_UI, 9),
                 wraplength=280, justify="left").pack(fill="x", pady=(10, 0))

        # 3. Инструменты
        tools = Card(holder, "Инструменты",
                     "Отдельные шаги и папки — если что-то нужно сделать вручную.",
                     accent=P["muted"])
        tools.grid(row=0, column=2, sticky="nsew", padx=(8, 0))

        for text, command in (("Подготовить .rpa / .rpyc…", self.do_prepare),
                              ("Перевести (автоматически)", self.do_translate),
                              ("Только собрать файлы", self.do_build)):
            ttk.Button(tools.body, text=text, command=command).pack(fill="x", pady=(0, 6))

        bottom = tk.Frame(tools.body, bg=P["panel"])
        bottom.pack(fill="x", pady=(4, 0))
        ttk.Button(bottom, text="Папка игры", style="Ghost.TButton",
                   command=self.do_open_game).pack(side="left")
        ttk.Button(bottom, text="Очистить кэш", style="Ghost.TButton",
                   command=self.do_clear_cache).pack(side="right")

        check_row = tk.Frame(tools.body, bg=P["panel"])
        check_row.pack(fill="x", pady=(6, 0))
        ttk.Button(check_row, text="Проверить перевод", style="Ghost.TButton",
                   command=self.do_check).pack(side="left")
        ttk.Button(check_row, text="Отчёт", style="Ghost.TButton",
                   command=self.do_report).pack(side="right")

        log_row = tk.Frame(tools.body, bg=P["panel"])
        log_row.pack(fill="x", pady=(6, 0))
        ttk.Button(log_row, text="Открыть лог-файл", style="Ghost.TButton",
                   command=self.do_open_log).pack(side="left")
        ttk.Button(log_row, text="Откатить бэкап", style="Ghost.TButton",
                   command=self.do_restore).pack(side="right")

    # --- прогресс ------------------------------------------------------
    def _build_progress(self, parent: tk.Misc) -> None:
        frame = tk.Frame(parent, bg=P["bg"])
        frame.grid(row=2, column=0, sticky="ew", pady=(10, 6))

        self.progress = ttk.Progressbar(frame, style="Accent.Horizontal.TProgressbar",
                                        maximum=100, value=0)
        self.progress.pack(fill="x")

        info = tk.Frame(frame, bg=P["bg"])
        info.pack(fill="x", pady=(6, 0))

        self.var_status = tk.StringVar(value="Готов к работе")
        tk.Label(info, textvariable=self.var_status, bg=P["bg"], fg=P["muted"],
                 font=(FONT_UI, 9), anchor="w").pack(side="left")

        self.btn_cancel = ttk.Button(info, text="Отмена", style="Ghost.TButton",
                                     command=self.on_cancel)
        # показываем только во время работы

        # --- сводка последнего прогона ---
        chips = tk.Frame(frame, bg=P["bg"])
        chips.pack(fill="x", pady=(8, 0))
        self.chips: dict[str, tk.Label] = {}
        for key, title in (("strings", "Строк"), ("translated", "Переведено"),
                           ("quality", "Качество"), ("coverage", "Покрытие"),
                           ("blocks", "Блоков в tl")):
            box = tk.Frame(chips, bg=P["chip"], highlightthickness=1,
                           highlightbackground=P["border"])
            box.pack(side="left", padx=(0, 8))
            value = tk.Label(box, text="—", bg=P["chip"], fg=P["text"],
                             font=(FONT_UI, 13, "bold"))
            value.pack(anchor="w", padx=12, pady=(6, 0))
            tk.Label(box, text=title, bg=P["chip"], fg=P["muted"],
                     font=(FONT_UI, 8)).pack(anchor="w", padx=12, pady=(0, 6))
            self.chips[key] = value

    # --- журнал --------------------------------------------------------
    def _build_log(self, parent: tk.Misc) -> None:
        frame = tk.Frame(parent, bg=P["panel"], highlightthickness=1,
                         highlightbackground=P["border"])
        frame.grid(row=3, column=0, sticky="nsew", pady=(0, 10))
        parent.rowconfigure(3, weight=1)

        head = tk.Frame(frame, bg=P["panel"])
        head.pack(fill="x", padx=14, pady=(10, 4))
        tk.Label(head, text="ЖУРНАЛ", bg=P["panel"], fg=P["muted"],
                 font=(FONT_UI, 8, "bold")).pack(side="left")
        ttk.Button(head, text="Очистить", style="Ghost.TButton",
                   command=self._clear_log).pack(side="right")
        ttk.Button(head, text="Скопировать", style="Ghost.TButton",
                   command=self._copy_log).pack(side="right", padx=(0, 6))

        wrap = tk.Frame(frame, bg=P["panel"])
        wrap.pack(fill="both", expand=True, padx=10, pady=(0, 10))

        self.log_text = tk.Text(wrap, height=8, wrap="word", bd=0,
                                bg=P["log_bg"], fg=P["log_text"],
                                insertbackground=P["text"], relief="flat",
                                font=(FONT_MONO, 9), padx=10, pady=8)
        scroll = ttk.Scrollbar(wrap, command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=scroll.set, state="disabled")
        self.log_text.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")

        self.log_text.tag_configure("info", foreground=P["log_text"])
        self.log_text.tag_configure("ok", foreground=P["ok"])
        self.log_text.tag_configure("warn", foreground=P["warn"])
        self.log_text.tag_configure("err", foreground=P["err"])
        self.log_text.tag_configure("step", foreground=P["accent_hi"],
                                    font=(FONT_MONO, 9, "bold"))

    def _build_footer(self) -> None:
        bar = tk.Frame(self, bg=P["panel"])
        bar.pack(fill="x")
        inner = tk.Frame(bar, bg=P["panel"])
        inner.pack(fill="x", padx=18, pady=6)
        tk.Label(inner, text="Перевод для личного использования · оригинальные .rpy "
                             "не меняются в режиме tl/",
                 bg=P["panel"], fg=P["muted"], font=(FONT_UI, 8)).pack(side="left")
        tk.Label(inner, text=f"кэш: {default_cache_path()}", bg=P["panel"],
                 fg="#5f6b80", font=(FONT_UI, 8)).pack(side="right")

    # ------------------------------------------------------------------
    # Язык перевода
    # ------------------------------------------------------------------
    @staticmethod
    def _lang_label(code: str) -> str:
        """«Русский · russian» — понятно и человеку, и программе."""
        code = (code or "russian").strip()
        return f"{_label_for(code)} · {code}"

    def current_language(self) -> str:
        """Код выбранного языка (то, что попадёт в tl/<lang>/)."""
        try:
            text = self.var_lang_label.get()
        except (AttributeError, tk.TclError):
            return self.cfg.target_lang or "russian"
        if "·" in text:
            return text.split("·")[-1].strip() or "russian"
        return text.strip() or "russian"

    # ------------------------------------------------------------------
    # Тема оформления
    # ------------------------------------------------------------------
    def _theme_button_text(self) -> str:
        return "◐  Светлая тема" if getattr(self.cfg, "theme", "dark") == "dark" \
            else "◑  Тёмная тема"

    def toggle_theme(self) -> None:
        """Переключение тёмной/светлой темы (применяется при перезапуске)."""
        self.cfg.theme = "light" if getattr(self.cfg, "theme", "dark") == "dark" \
            else "dark"
        self.cfg.save()
        new_name = "светлую" if self.cfg.theme == "light" else "тёмную"
        if messagebox.askyesno(
                APP_NAME,
                f"Включить {new_name} тему?\n\n"
                "Оформление применится сразу после перезапуска программы."):
            self._restart()

    def _restart(self) -> None:
        """Перезапустить приложение (для смены темы)."""
        try:
            self.save_settings()
        except Exception:  # noqa: BLE001
            pass
        args = [sys.executable]
        if not getattr(sys, "frozen", False):
            args.append(str(Path(sys.argv[0]).resolve()))
        args.extend(sys.argv[1:])
        try:
            os.execv(sys.executable, args)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Перезапуск не удался: %s", exc)
            messagebox.showinfo(APP_NAME,
                                "Закройте и откройте программу заново — "
                                "тема применится.")

    # ------------------------------------------------------------------
    # Проверка перевода, отчёт, откат бэкапа (v3.0)
    # ------------------------------------------------------------------
    def do_check(self) -> None:
        """Статическая проверка готового каталога tl (без запуска игры)."""
        from core.verify import verify_project

        if self._busy:
            return

        def task() -> None:
            pipeline = self._pipeline()
            pipeline.scan()
            parsed = None
            try:
                parsed = pipeline.analyze()
            except Exception as exc:  # noqa: BLE001
                logger.warning("Разбор игры не удался: %s", exc)
            report = verify_project(pipeline.project,
                                    self._settings().normalized_lang(), parsed)
            pipeline.verification = report
            self.log(report.summary(), "ok" if report.ok else "warn")
            for issue in report.issues[:15]:
                tag = "err" if issue.severity == "error" else "warn"
                where = f" ({issue.where()})" if issue.where() else ""
                self.log(f"  {issue.label()}: {issue.message}{where}", tag)
            self.post(lambda: self._check_done(report))

        self._run("Проверка перевода", task)

    def _check_done(self, report) -> None:
        self._update_summary()
        if report.ok:
            messagebox.showinfo(
                APP_NAME,
                "Проверка пройдена — файлы перевода выглядят корректно.\n\n"
                f"Файлов: {report.files}, блоков: {report.blocks}\n"
                f"Покрытие: {report.coverage * 100:.1f}%")
        else:
            messagebox.showwarning(
                APP_NAME,
                "Найдены проблемы в файлах перевода:\n\n"
                + "\n".join(f"• {i.label()}: {i.message}"
                            for i in report.errors[:8])
                + "\n\nПодробности — в журнале и в HTML-отчёте.")

    def do_report(self) -> None:
        """Сохранить HTML-отчёт и открыть его в браузере."""
        if self._busy:
            return

        def task() -> None:
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
                        pipeline.project, self._settings().normalized_lang(),
                        pipeline.parsed)
                except Exception:  # noqa: BLE001
                    pass
            path = pipeline.write_report()
            self.post(lambda: self._open_report(path))

        self._run("Отчёт", task)

    def _open_report(self, path: Path) -> None:
        self.log(f"Отчёт сохранён: {path}", "ok")
        try:
            import webbrowser

            webbrowser.open(path.resolve().as_uri())
        except Exception as exc:  # noqa: BLE001
            logger.debug("Не удалось открыть отчёт: %s", exc)
            self._open_folder(path.parent)

    def do_restore(self) -> None:
        """Откатить игру из самой свежей резервной копии."""
        from core.backup import latest_backup, restore_backup

        if self._busy:
            return
        try:
            project = scan_project(self.var_game.get().strip())
        except ProjectError as exc:
            messagebox.showwarning(APP_NAME, str(exc))
            return
        archive = latest_backup(project)
        if archive is None:
            messagebox.showinfo(APP_NAME, "Резервных копий для этой игры нет.")
            return
        if not messagebox.askyesno(
                APP_NAME,
                f"Вернуть файлы игры из копии?\n\n{archive.name}\n\n"
                "Созданные программой каталоги перевода будут удалены."):
            return

        def task() -> None:
            result = restore_backup(project, archive)
            self.log(result.summary(), "ok" if result.ok else "warn")
            self.post(lambda: messagebox.showinfo(
                APP_NAME, "Готово.\n\n" + result.summary()))

        self._run("Откат бэкапа", task)

    # ------------------------------------------------------------------
    def _update_summary(self) -> None:
        """Обновить чипы-сводку из состояния последнего прогона."""
        pipeline = self.pipeline
        if pipeline is None:
            return
        report = getattr(pipeline, "last_translate_report", None)
        quality = getattr(pipeline, "quality", None)
        verify = getattr(pipeline, "verification", None)
        build = getattr(pipeline, "last_build_result", None)

        def put(key: str, value: str, color: str = "") -> None:
            label = self.chips.get(key)
            if label is None:
                return
            label.configure(text=value)
            if color:
                label.configure(fg=color)

        if report is not None:
            put("strings", str(report.unique_texts))
            put("translated", str(report.translated + report.from_cache))
        if quality is not None:
            errors = len(quality.errors)
            put("quality", "чисто" if not errors else f"{errors} ошиб.",
                P["ok"] if not errors else P["err"])
        if verify is not None:
            put("coverage", f"{verify.coverage * 100:.0f}%",
                P["ok"] if verify.coverage >= 0.99 else P["warn"])
        if build is not None:
            put("blocks", str(getattr(build, "blocks", 0)))

    # ------------------------------------------------------------------
    # Переменные-флаги (для чекбоксов)
    # ------------------------------------------------------------------
    def _var_flag(self, field: str) -> tk.BooleanVar:
        store = getattr(self, "_flags", None)
        if store is None:
            store = {}
            self._flags = store
        if field not in store:
            var = tk.BooleanVar(value=bool(getattr(self.cfg, field)))
            var.trace_add("write", lambda *_a, f=field, v=var: setattr(self.cfg, f, bool(v.get())))
            store[field] = var
        return store[field]

    # ------------------------------------------------------------------
    # Логирование в окно
    # ------------------------------------------------------------------
    def _install_log_handler(self) -> None:
        setup_logging(default_log_path(), verbose=False)

        class _Handler(logging.Handler):
            def __init__(handler_self, sink) -> None:  # noqa: N805
                super().__init__()
                handler_self.sink = sink
                handler_self.setFormatter(logging.Formatter("%(message)s"))

            def emit(handler_self, record: logging.LogRecord) -> None:  # noqa: N805
                try:
                    level = "err" if record.levelno >= logging.ERROR else (
                        "warn" if record.levelno >= logging.WARNING else "info")
                    handler_self.sink((level, handler_self.format(record)))
                except Exception:  # noqa: BLE001
                    pass

        handler = _Handler(lambda item: self.log_queue.put(item))
        handler.setLevel(logging.INFO)
        logging.getLogger("renpy_translator").addHandler(handler)

    def post(self, func: Callable[[], None]) -> None:
        """Выполнить действие в главном потоке Tk (безопасно из фонового)."""
        self.ui_queue.put(func)

    def _poll_log(self) -> None:
        try:
            while True:
                level, message = self.log_queue.get_nowait()
                self._append_log(message, level)
        except queue.Empty:
            pass

        try:
            while True:
                action = self.ui_queue.get_nowait()
                try:
                    action()
                except Exception:  # noqa: BLE001
                    logger.debug("Ошибка обновления интерфейса: %s", traceback.format_exc())
        except queue.Empty:
            pass

        self.after(100, self._poll_log)

    def _append_log(self, message: str, tag: str = "info") -> None:
        try:
            self.log_text.configure(state="normal")
            self.log_text.insert("end", message.rstrip() + "\n", tag)
            self.log_text.see("end")
            self.log_text.configure(state="disabled")
        except tk.TclError:
            pass

    def log(self, message: str, tag: str = "info") -> None:
        self._append_log(message, tag)

    def _clear_log(self) -> None:
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")

    def _copy_log(self) -> None:
        try:
            text = self.log_text.get("1.0", "end").strip()
            self.clipboard_clear()
            self.clipboard_append(text)
            self.var_status.set("Журнал скопирован в буфер обмена")
        except tk.TclError:
            pass

    # ------------------------------------------------------------------
    # Настройки
    # ------------------------------------------------------------------
    def open_settings(self) -> None:
        """Модальное окно настроек."""
        SettingsDialog(self)

    def refresh_flags(self) -> None:
        """Перечитывает чекбоксы главного окна из self.cfg."""
        self._var_flag("set_default_language").set(bool(self.cfg.set_default_language))
        self._var_flag("language_button").set(bool(self.cfg.language_button))
        self._var_flag("patch_language_menu").set(bool(self.cfg.patch_language_menu))
        self._var_flag("hook_fallback").set(bool(self.cfg.hook_fallback))
        self.var_font_mode.set(self.cfg.font_mode)
        try:
            self.var_pretty_font.set(self.cfg.font_mode == "pretty")
        except (tk.TclError, AttributeError):
            pass

    def _on_pretty_font_toggle(self) -> None:
        """Галочка «красивый шрифт»: включена — pretty, выключена — fix."""
        self.var_font_mode.set("pretty" if self.var_pretty_font.get() else "fix")
        self.save_settings()

    def save_settings(self) -> None:
        """Сохраняет всё, что задаётся в главном окне."""
        try:
            self.cfg.set_default_language = bool(self._var_flag("set_default_language").get())
            self.cfg.language_button = bool(self._var_flag("language_button").get())
            self.cfg.patch_language_menu = bool(self._var_flag("patch_language_menu").get())
            self.cfg.hook_fallback = bool(self._var_flag("hook_fallback").get())
            self.cfg.font_mode = self.var_font_mode.get() or "pretty"
            self.cfg.target_lang = self.current_language()
            self.cfg.last_game_path = self.var_game.get().strip()
            self.cfg.save()
        except Exception as exc:  # noqa: BLE001
            logger.debug("Настройки не сохранены: %s", exc)

    def _settings(self) -> Settings:
        self.save_settings()
        settings = self.cfg.to_settings()
        settings.game_path = Path(self.var_game.get().strip()).expanduser()
        settings.target_lang = self.current_language()
        settings.set_default_language = bool(self._var_flag("set_default_language").get())
        settings.language_button = bool(self._var_flag("language_button").get())
        settings.patch_language_menu = bool(self._var_flag("patch_language_menu").get())
        settings.hook_fallback = bool(self._var_flag("hook_fallback").get())
        return settings

    # ------------------------------------------------------------------
    # Работа с папкой
    # ------------------------------------------------------------------
    def on_pick_folder(self) -> None:
        initial = self.var_game.get().strip() or None
        folder = filedialog.askdirectory(
            title="Выберите папку game (или корень игры)", initialdir=initial)
        if folder:
            self.var_game.set(folder)
            self.on_scan()

    def on_scan(self) -> None:
        path = self.var_game.get().strip()
        if not path:
            self.lbl_scan.configure(text="Папка не выбрана", fg=P["muted"])
            return
        try:
            self.project = scan_project(Path(path).expanduser())
        except ProjectError as exc:
            self.project = None
            self.lbl_scan.configure(text=f"⚠ {exc}", fg=P["err"])
            return

        self.cfg.last_game_path = str(Path(path).expanduser())
        self.save_settings()

        project = self.project
        parts = [f"✓ {project.name}: {len(project.rpy_files)} .rpy"]
        if project.rpyc_files:
            parts.append(f"{len(project.rpyc_files)} .rpyc")
        if project.rpa_files:
            parts.append(f"{len(project.rpa_files)} .rpa")
        if project.language_dirs:
            parts.append("переводы: " + ", ".join(project.language_dirs[:4]))
        else:
            parts.append("переводов нет")
        self.lbl_scan.configure(text="  ·  ".join(parts), fg=P["ok"])
        self.log(
            f"Найдена игра: {project.name} · .rpy: {len(project.rpy_files)} · "
            f".rpyc: {len(project.rpyc_files)} · .rpa: {len(project.rpa_files)} · "
            f"переводы: {', '.join(project.language_dirs) if project.language_dirs else 'нет'}",
            "ok")

        if not project.has_scripts:
            self.lbl_scan.configure(
                text="⚠ В папке нет скриптов Ren'Py — проверьте путь", fg=P["warn"])

    # ------------------------------------------------------------------
    # Инфраструктура фоновых задач
    # ------------------------------------------------------------------
    def _set_busy(self, busy: bool, title: str = "") -> None:
        self._busy = busy
        state = "disabled" if busy else "normal"
        for button in (self.btn_auto, self.btn_import):
            try:
                button.configure(state=state)
            except tk.TclError:
                pass
        if busy:
            self._busy_started = time.monotonic()
            self._busy_label = title or "Работаю…"
            self._last_progress_text = ""
            self.var_status.set(f"{self._busy_label}  ·  прошло 0 с")
            self.btn_cancel.configure(state="normal", text="Отмена")
            self.btn_cancel.pack(side="right")
            self._cancel.clear()
            self.after(1000, self._tick_status)
        else:
            self._busy_label = ""
            self._last_progress_text = ""
            self.btn_cancel.pack_forget()
            self.progress.configure(value=0)

    def on_cancel(self) -> None:
        self._cancel.set()
        self.btn_cancel.configure(state="disabled", text="Останавливаюсь…")
        self.log("Останавливаюсь после текущего пакета… "
                 "Уже переведённые строки сохранятся в кэше.", "warn")
        self.var_status.set("Остановка…")

    def _make_progress(self, total: int, desc: str, enabled: bool):
        from core.utils import Progress

        progress = Progress(total=total, desc=desc, enabled=False)
        state = {"last_beat": 0.0, "start": time.monotonic()}

        def callback(count: int, total_: int) -> None:
            percent = (count / total_ * 100.0) if total_ else 0.0
            eta = human_duration(progress.elapsed_eta())
            elapsed = max(0.001, time.monotonic() - state["start"])
            speed = count / elapsed
            text = (f"{desc}: {count} из {total_} ({percent:4.1f}%) · "
                    f"{speed:.1f} строк/с · осталось ≈ {eta}")

            def update() -> None:
                self.progress.configure(maximum=100, value=percent)
                self._last_progress_text = text
                self._refresh_status()
                # «пульс» в журнал — чтобы было видно, что работа идёт
                now = time.monotonic()
                if now - state["last_beat"] > HEARTBEAT_SECONDS:
                    state["last_beat"] = now
                    self.log(f"… {text}", "info")

            self.post(update)

        progress.callback = callback
        return progress

    def _refresh_status(self) -> None:
        """Собирает строку состояния: прогресс + сколько уже идёт работа."""
        if not self._busy:
            return
        elapsed = human_duration(time.monotonic() - self._busy_started)
        base = self._last_progress_text or self._busy_label or "Работаю…"
        self.var_status.set(f"{base}  ·  прошло {elapsed}")

    def _tick_status(self) -> None:
        """Раз в секунду обновляем счётчик времени: окно «живое»."""
        if not self._busy:
            return
        self._refresh_status()
        self.after(1000, self._tick_status)

    def _log_fonts(self, pipeline: Pipeline) -> None:
        """Пишет в журнал, что стало со шрифтами."""
        report = getattr(pipeline, "font_report", None)
        if report is None:
            return
        if report.mode == "off":
            return
        if report.warnings:
            self.log("Шрифты: " + "; ".join(report.warnings), "warn")
            return
        self.log("Шрифты: " + report.summary(), "ok")
        for name in report.replaced:
            self.log(f"  без кириллицы → PT Serif: {name}", "info")
        for name in report.missing:
            self.log(f"  файл шрифта не найден → PT Serif: {name}", "info")

    def _pipeline(self) -> Pipeline:
        pipeline = Pipeline(self._settings(),
                            progress_factory=self._make_progress,
                            should_cancel=self._cancel.is_set)
        self.pipeline = pipeline
        return pipeline

    def _run(self, title: str, func: Callable[[], None]) -> None:
        """Запускает задачу в фоне, блокируя интерфейс от повторных нажатий."""
        if self._busy:
            messagebox.showinfo(APP_NAME, "Дождитесь завершения текущей операции.")
            return
        if not self.var_game.get().strip():
            messagebox.showwarning(APP_NAME, "Сначала выберите папку игры.")
            return

        self._set_busy(True, title)
        self.log("")
        self.log(f"▶ {title}", "step")

        def runner() -> None:
            error: Optional[BaseException] = None
            try:
                func()
            except ProjectError as exc:
                error = exc
                logger.error("%s", exc)
            except Exception as exc:  # noqa: BLE001
                error = exc
                logger.error("Ошибка: %s", exc)
                logger.debug(traceback.format_exc())
            finally:
                def finish() -> None:
                    self._set_busy(False)
                    self.var_status.set("Готов к работе")
                    if error is not None:
                        messagebox.showerror(
                            APP_NAME,
                            f"Не получилось выполнить действие:\n\n{error}\n\n"
                            "Подробности — в журнале ниже и в translator.log.")
                self.post(finish)

        self.worker = threading.Thread(target=runner, daemon=True)
        self.worker.start()

    # ------------------------------------------------------------------
    # Действия
    # ------------------------------------------------------------------
    def do_auto(self) -> None:
        """Полный автоматический цикл."""
        def task() -> None:
            pipeline = self._pipeline()
            project = pipeline.scan()
            self.post(lambda: self.lbl_scan.configure(
                text=f"✓ {project.name}: {len(project.rpy_files)} .rpy", fg=P["ok"]))

            if project.needs_preparation:
                self.log("Распаковываю .rpa и декомпилирую .rpyc…", "info")
                rpa_results, decompile_report = pipeline.prepare()
                for res in rpa_results:
                    self.log(f"  {res.summary()}", "info")
                if decompile_report.results:
                    self.log(f"  {decompile_report.summary()}", "info")
                if decompile_report.unavailable:
                    self.log(f"  ! {decompile_report.unavailable}", "warn")
                project = pipeline.project

            if pipeline.settings.backup:
                from core.backup import create_backup

                backup = create_backup(project, extra_paths=[pipeline.settings.cache_path])
                self.log("  " + backup.summary(), "ok")

            parsed = pipeline.analyze()
            stats = parsed.stats()
            self.log(f"Найдено строк: {stats.get('total', 0)} "
                     f"(реплик {stats.get('dialogue', 0)}, нарратив "
                     f"{stats.get('narration', 0)}, меню {stats.get('menu', 0)}, "
                     f"интерфейс {stats.get('ui', 0)})", "ok")

            if stats.get("total", 0) == 0:
                raise RuntimeError(
                    "В скриптах не найдено переводимого текста. "
                    "Возможно, скрипты скомпилированы — нажмите «Подготовить».")

            story = (stats.get("dialogue", 0) + stats.get("narration", 0)
                     + stats.get("menu", 0))
            if story == 0 and stats.get("ui", 0):
                self.log(
                    "⚠ Текста истории не найдено: только строки интерфейса "
                    f"({stats.get('ui', 0)}). Похоже, это техническая сборка, "
                    "где сюжет ещё не написан.", "warn")
            elif story < 5:
                self.log(f"⚠ Реплик всего {story} — похоже, сборка ранняя, "
                         f"основной объём текста здесь интерфейсный "
                         f"({stats.get('ui', 0)} строк).", "warn")

            self.log("Перевожу. Прогресс — в строке состояния и здесь; "
                     "кнопка «Отмена» остановит работу, уже переведённое "
                     "останется в кэше.", "info")
            report = pipeline.translate(parsed)
            self.log(report.summary(), "ok" if not report.failed else "warn")
            for warning in report.warnings:
                self.log("  ! " + warning, "warn")

            tl_result, replace_result = pipeline.build(parsed)
            if tl_result:
                self.log(tl_result.summary(), "ok")
                self.log(f"  файлы: {tl_result.tl_dir}", "info")
            if replace_result:
                self.log(replace_result.summary(), "ok")
            self._log_fonts(pipeline)

            self.post(lambda: self._auto_done(report, tl_result, replace_result))

        self._run("Полный цикл русификации", task)

    def _auto_done(self, report, tl_result, replace_result) -> None:
        if report is not None and getattr(report, "aborted", False):
            self._aborted_dialog(report)
            return

        lines = ["Готово!"]
        if report:
            lines.append(f"Переведено строк: {report.translated} "
                         f"(из кэша: {report.from_cache})")
            if report.failed:
                lines.append(f"Не переведено: {report.failed} "
                             "(см. журнал и translation_cache.json.failed.txt)")
        if tl_result:
            lines.append(f"Файлов перевода: {len(tl_result.files_written)} "
                         f"в {tl_result.tl_dir}")
        if replace_result:
            lines.append(f"Изменено файлов игры: {len(replace_result.changed_files)}")

        quality = getattr(self.pipeline, "quality", None) if self.pipeline else None
        verify = getattr(self.pipeline, "verification", None) if self.pipeline else None
        if quality is not None:
            if quality.errors:
                lines.append(f"Проверка качества: {len(quality.errors)} строк "
                             "с ошибками (подробности — в отчёте)")
            else:
                lines.append("Проверка качества: без ошибок")
        if verify is not None:
            lines.append(f"Проверка файлов перевода: "
                         f"{'чисто' if verify.ok else 'есть замечания'}, "
                         f"покрытие {verify.coverage * 100:.1f}%")
        if self.pipeline is not None and getattr(self.pipeline, "font_report", None):
            font_report = self.pipeline.font_report
            if font_report.mode != "off":
                lines.append("Шрифт: PT Serif с полной кириллицей" if
                             font_report.mode == "pretty" else
                             "Шрифты: починены только те, где не было русских букв")

        self._update_summary()

        if tl_result:
            lines.append("\nЧто дальше:\n"
                         "1) Запустите игру.\n"
                         "2) Настройки → Язык → Русский.\n"
                         "   (если язык не включается сам — поставьте галочку "
                         "«Включать русский автоматически»)")
            lines.append("\nДля литературного качества: «Экспорт текста для агента» → "
                         "правки у AI → «Импорт правок в игру».")

        lines.append("\nHTML-отчёт со всеми подробностями — кнопка «Отчёт».")
        messagebox.showinfo(APP_NAME, "\n".join(lines))
        if tl_result and tl_result.tl_dir:
            self._open_folder(tl_result.tl_dir)

    def _aborted_dialog(self, report) -> None:
        """Объясняем, почему перевод встал и что делать дальше."""
        messagebox.showwarning(
            APP_NAME,
            "Перевод остановлен досрочно.\n\n"
            f"Причина: {report.abort_reason}.\n"
            f"Переведено {report.translated} строк, не хватило "
            f"{max(0, report.failed)}.\n\n"
            "Скорее всего Google просит сбавить темп (лимит запросов). "
            "Что делать:\n\n"
            "1) Откройте «⚙ Настройки» → «Скорость»: потоков 1–2, "
            "строк в запросе 10, пауза 1,5 с.\n"
            "2) Нажмите «Перевести (автоматически)» — уже переведённое "
            "возьмётся из кэша, программа продолжит с места остановки.\n"
            "3) Если снова упирается — подождите 10–15 минут и повторите, "
            "либо переключите движок на «google-web».\n\n"
            "Всё, что уже переведено, сохранено — работа не пропала.")

    def do_prepare(self) -> None:
        def task() -> None:
            pipeline = self._pipeline()
            project = pipeline.scan()
            if not (project.rpa_files or project.rpyc_files):
                self.log("Архивов .rpa и файлов .rpyc нет — подготовка не нужна.", "info")
                return
            rpa_results, decompile_report = pipeline.prepare()
            for res in rpa_results:
                self.log(res.summary(), "ok" if res.ok else "warn")
            if decompile_report.results:
                self.log(decompile_report.summary(), "ok")
            if decompile_report.unavailable:
                self.log("! " + decompile_report.unavailable, "warn")
            self.log("Подготовка завершена. Теперь можно переводить.", "ok")

        self._run("Подготовка (.rpa / .rpyc)", task)

    def do_translate(self) -> None:
        def task() -> None:
            pipeline = self._pipeline()
            pipeline.scan()
            if pipeline.project and pipeline.project.needs_preparation:
                self.log("Сначала подготовка (.rpa/.rpyc)…", "info")
                pipeline.prepare()
            parsed = pipeline.analyze()
            report = pipeline.translate(parsed)
            self.log(report.summary(), "ok" if not report.failed else "warn")
            self.log("Перевод сохранён в кэш. Можно нажимать «Только собрать».",
                     "info")

        self._run("Автоматический перевод", task)

    def do_build(self) -> None:
        def task() -> None:
            pipeline = self._pipeline()
            pipeline.scan()
            parsed = pipeline.analyze()
            tl_result, replace_result = pipeline.build(parsed)
            if tl_result:
                self.log(tl_result.summary(), "ok")
                self.log(f"  {tl_result.tl_dir}", "info")
            if replace_result:
                self.log(replace_result.summary(), "ok")
            self._log_fonts(pipeline)
            self.log("Сборка завершена.", "ok")

        self._run("Сборка файлов перевода", task)

    def do_export(self) -> None:
        """Экспорт текста для агента: JSON + TXT в выбранную папку."""
        default_dir = self.cfg.last_export_path or self.var_game.get().strip()
        folder = filedialog.askdirectory(
            title="Куда сохранить файлы для переводчика?",
            initialdir=default_dir if Path(default_dir or ".").is_dir() else None)
        if not folder:
            return

        def task() -> None:
            pipeline = self._pipeline()
            pipeline.scan()
            if pipeline.project and pipeline.project.needs_preparation:
                self.log("Сначала подготовка (.rpa/.rpyc)…", "info")
                pipeline.prepare()
            parsed = pipeline.analyze()

            base = Path(folder) / "renpy_translator_export"
            results = pipeline.export_all(base)
            try:
                results.append(pipeline.export("docx", base.with_suffix(".docx")))
            except Exception as exc:  # noqa: BLE001
                self.log(f"DOCX не создан: {exc}", "warn")
            self.cfg.last_export_path = folder
            self.save_settings()

            for res in results:
                self.log(f"{res.summary()}", "ok")
            self._last_export_dir = Path(folder)
            self.post(lambda: self._export_done(folder, results))

        self._run("Экспорт текста для агента", task)

    def _export_done(self, folder: str, results) -> None:
        names = "\n".join(f"   • {r.path.name}" for r in results)
        messagebox.showinfo(
            APP_NAME,
            "Файлы для перевода готовы:\n\n"
            f"{names}\n\n"
            f"Папка: {folder}\n\n"
            "ОТПРАВЬТЕ AI-АГЕНТУ файл .txt и попросите:\n"
            "«Заполни все строки RU: литературным русским переводом. "
            "Не меняй строки EN:, номера [#...] и вставки [переменная], {тег}, %s».\n\n"
            "• .json — резервная копия для программы,\n"
            "• .docx — если удобнее править в Word.\n\n"
            "Когда агент вернёт файл, нажмите «2. Импорт правок в игру».")
        self._open_folder(Path(folder))

    def do_import(self) -> None:
        path = filedialog.askopenfilename(
            title="Выберите файл с переводом (txt / json / docx)",
            filetypes=[("Перевод", "*.txt *.json *.docx"),
                       ("Текст", "*.txt"), ("JSON", "*.json"),
                       ("Word", "*.docx"), ("Все файлы", "*.*")])
        if not path:
            return
        imported = Path(path)

        def task() -> None:
            pipeline = self._pipeline()
            pipeline.scan()
            if pipeline.project and pipeline.project.needs_preparation:
                self.log("Сначала подготовка (.rpa/.rpyc)…", "info")
                pipeline.prepare()
            parsed = pipeline.analyze()

            self.log(f"Читаю {imported.name}…", "info")
            result = pipeline.import_file(imported)
            self.log(result.summary(), "ok" if result.applied else "warn")
            for warning in result.warnings:
                self.log("  ! " + warning, "warn")

            untranslated = sum(1 for item in parsed.items if not item.is_translated())

            tl_result, replace_result = pipeline.build(parsed)
            if tl_result:
                self.log(tl_result.summary(), "ok")
            if replace_result:
                self.log(replace_result.summary(), "ok")
            self._log_fonts(pipeline)

            self.post(lambda: self._import_done(result, untranslated,
                                                    tl_result, replace_result))

        self._run("Импорт правок в игру", task)

    def _import_done(self, result, untranslated: int, tl_result, replace_result) -> None:
        text = (f"Применено строк: {result.applied}\n"
                f"Не найдено соответствий: {result.missing}\n")
        if tl_result:
            text += f"\nСобрано блоков перевода: {tl_result.blocks}, " \
                    f"UI-строк: {tl_result.strings}\n"
        if replace_result:
            text += f"\nИзменено файлов игры: {len(replace_result.changed_files)}\n"

        if untranslated:
            text += (f"\nБез перевода осталось строк: {untranslated}.\n"
                     "Хотите перевести их автоматически?")
            if messagebox.askyesno(APP_NAME, text):
                self.do_translate_after_import()
                return
        else:
            text += "\nВсе строки переведены. Запускайте игру и выбирайте русский язык!"

        messagebox.showinfo(APP_NAME, text)

    def do_translate_after_import(self) -> None:
        def task() -> None:
            pipeline = self._pipeline()
            pipeline.scan()
            parsed = pipeline.analyze()
            report = pipeline.translate(parsed)
            self.log(report.summary(), "ok" if not report.failed else "warn")
            tl_result, replace_result = pipeline.build(parsed)
            if tl_result:
                self.log(tl_result.summary(), "ok")
            if replace_result:
                self.log(replace_result.summary(), "ok")
            self._log_fonts(pipeline)
            self.post(lambda: messagebox.showinfo(
                APP_NAME, "Оставшиеся строки переведены. Запускайте игру!"))

        self._run("Перевод оставшихся строк", task)

    def do_open_game(self) -> None:
        path = self.var_game.get().strip()
        if not path:
            messagebox.showwarning(APP_NAME, "Сначала выберите папку игры.")
            return
        self._open_folder(Path(path).expanduser())

    def do_open_log(self) -> None:
        path = default_log_path()
        if not path.exists():
            messagebox.showinfo(APP_NAME, "Лог-файл пока пуст.")
            return
        try:
            if IS_WINDOWS:
                os.startfile(str(path))  # type: ignore[attr-defined]
            else:
                subprocess.Popen(["xdg-open", str(path)])
        except Exception as exc:  # noqa: BLE001
            messagebox.showinfo(APP_NAME, f"Лог-файл: {path}\n({exc})")

    def do_clear_cache(self) -> None:
        path = default_cache_path()
        if not messagebox.askyesno(
                APP_NAME, f"Удалить кэш переводов?\n\n{path}\n\n"
                          "Строки придётся переводить заново."):
            return
        try:
            if path.exists():
                path.unlink()
            self.log("Кэш удалён.", "ok")
        except OSError as exc:
            messagebox.showerror(APP_NAME, f"Не удалось удалить кэш: {exc}")

    def _open_folder(self, path: Path) -> None:
        try:
            path = Path(path)
            if not path.exists():
                return
            if IS_WINDOWS:
                os.startfile(str(path))  # type: ignore[attr-defined]
            else:
                subprocess.Popen(["xdg-open", str(path)])
        except Exception:  # noqa: BLE001
            pass

    # ------------------------------------------------------------------
    def show_help(self) -> None:
        messagebox.showinfo(
            f"{APP_NAME} — как пользоваться",
            "1. Выберите папку игры (обычно …/Игра/game).\n\n"
            "2. Нажмите «Сделать всё автоматически» — программа распакует архивы, "
            "найдёт текст, переведёт его и создаст файлы перевода.\n\n"
            "3. Запустите игру: Настройки → Язык → Русский.\n"
            "   Если такого пункта нет — включите галочку «Включать русский "
            "автоматически» и нажмите кнопку снова.\n\n"
            "Литературный перевод:\n"
            "   • «Экспорт текста для агента» → получите .txt и .json;\n"
            "   • отдайте .txt AI-агенту, попросите заполнить строки RU:;\n"
            "   • «Импорт правок в игру» → выберите возвращённый файл (.txt/.json/.docx).\n\n"
            "Шрифты: в игру ставится PT Serif с полной кириллицей (лицензия OFL), "
            "поэтому русские буквы не превращаются в квадратики. В настройках можно "
            "выбрать «чинить только шрифты без русских букв» или не трогать шрифты.\n\n"
            "Оригинальные скрипты игры не меняются (режим tl/), кэш переводов "
            "сохраняется — повторный запуск не тратит лимиты API.")

    def _on_close(self) -> None:
        try:
            self.save_settings()
        finally:
            self.destroy()


# ===========================================================================
def _try_modern(settings: Optional[Settings]) -> bool:
    """Современный интерфейс на customtkinter. False — если недоступен."""
    try:
        import customtkinter  # noqa: F401
    except Exception as exc:  # noqa: BLE001
        logger.info("customtkinter недоступен (%s) — использую классический "
                    "интерфейс", exc)
        return False
    try:
        from gui_modern import run_modern_gui

        logger.info("Запускаю современный интерфейс")
        run_modern_gui(settings)
        return True
    except Exception:  # noqa: BLE001
        logger.exception("Современный интерфейс не запустился — "
                         "переключаюсь на классический")
        try:  # чтобы «огрызок» нового окна не висел за спиной
            root = getattr(tk, "_default_root", None)
            if root is not None:
                for child in list(root.winfo_children()):
                    child.destroy()
                root.destroy()
        except Exception:  # noqa: BLE001
            pass
        return False


def run_gui(settings: Optional[Settings] = None) -> None:
    """Точка входа графического режима (её вызывает и main.py, и .exe)."""
    enable_high_dpi()
    if _try_modern(settings):
        return
    try:
        apply_theme(UserConfig.load().theme)
    except Exception:  # noqa: BLE001
        apply_theme("dark")
    try:
        app = App(settings)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Не удалось создать окно: %s", exc)
        try:
            root = tk.Tk()
            root.withdraw()
            messagebox.showerror(
                APP_NAME,
                f"Не удалось запустить программу:\n\n{exc}\n\n"
                f"Подробности: {default_log_path()}")
            root.destroy()
        except Exception:  # noqa: BLE001
            pass
        raise

    def report_callback_exception(exc_type, exc, tb) -> None:  # noqa: ANN001
        text = "".join(traceback.format_exception(exc_type, exc, tb))
        logger.error("Ошибка интерфейса: %s", text)
        try:
            messagebox.showerror(
                APP_NAME,
                f"Внутренняя ошибка:\n\n{exc}\n\n"
                f"Подробности записаны в {default_log_path()}")
        except Exception:  # noqa: BLE001
            pass

    app.report_callback_exception = report_callback_exception  # type: ignore[method-assign]
    app.mainloop()


if __name__ == "__main__":
    run_gui()
