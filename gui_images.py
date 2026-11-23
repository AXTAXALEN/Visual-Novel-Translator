"""
Вкладка «Текст-картинки»: локализация надписей, нарисованных картинками.

Работает поверх :mod:`core.image_text`: находит в экранах игры картинки
с текстом, показывает превью, даёт мышью обвести область, вписать русский
текст и подставить результат в игру (с резервной копией и откатом).
"""

from __future__ import annotations

import tkinter as tk
from pathlib import Path
from tkinter import colorchooser, filedialog
from typing import Optional

import customtkinter as ctk

from core.utils import get_logger

logger = get_logger(__name__)

try:
    from PIL import Image, ImageTk
except Exception:  # pragma: no cover  # noqa: BLE001
    Image = None  # type: ignore[assignment]
    ImageTk = None  # type: ignore[assignment]

TEXT_MUTED = ("#5a6478", "#98a2b3")

CANVAS_W = 360
CANVAS_H = 230

FONT_CHOICES = ("PT Serif Bold (по умолчанию)", "PT Serif Regular",
                "PT Serif Italic", "Другой файл…")

FONT_FILES = {
    "PT Serif Bold (по умолчанию)": "PT_Serif-Bold.ttf",
    "PT Serif Regular": "PT_Serif-Regular.ttf",
    "PT Serif Italic": "PT_Serif-Italic.ttf",
}


class ImagesTab(ctk.CTkFrame):
    """Интерфейс локализации картинок с текстом."""

    def __init__(self, master, app) -> None:
        super().__init__(master, fg_color="transparent")
        self.app = app
        self.candidates: list = []
        self.current = None
        self.scale = 1.0
        self.box: Optional[tuple[int, int, int, int]] = None
        self._drag_start: Optional[tuple[int, int]] = None
        self._showing_ru = False
        self._custom_font: Optional[Path] = None

        self.grid_columnconfigure(0, weight=0)
        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(1, weight=1)

        header = ctk.CTkFrame(self, fg_color="transparent")
        header.grid(row=0, column=0, columnspan=2, sticky="ew", padx=14,
                    pady=(10, 2))
        header.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(
            header, justify="left", anchor="w", wraplength=690,
            font=ctk.CTkFont(size=11), text_color=TEXT_MUTED,
            text=("Логотипы, титулы и надписи-картинки не переводятся "
                  "текстом: буквы запечены в пиксели. Здесь можно найти "
                  "такие картинки, обвести область мышью, вписать русский "
                  "текст — программа нарисует картинку того же размера и "
                  "подставит её вместо оригинала.")).grid(row=0, column=0,
                                                          sticky="w",
                                                          columnspan=3)
        ctk.CTkButton(header, text="Найти картинки с текстом", width=200,
                      height=30, command=self.do_scan).grid(row=1, column=0,
                                                            sticky="w",
                                                            pady=(6, 0))
        ctk.CTkButton(header, text="Вернуть исходники", width=160, height=30,
                      fg_color="#7a3b3b", hover_color="#632f2f",
                      command=self.do_revert).grid(row=1, column=1,
                                                   sticky="w", padx=(8, 0),
                                                   pady=(6, 0))

        # --- список картинок ---
        left = ctk.CTkFrame(self, corner_radius=10,
                            fg_color=("#eef1f6", "#22262f"))
        left.grid(row=1, column=0, sticky="nsew", padx=(14, 6), pady=(4, 12))
        self.list_box = ctk.CTkScrollableFrame(left, width=255,
                                               fg_color="transparent")
        self.list_box.pack(fill="both", expand=True, padx=6, pady=6)
        self._placeholder("Список пуст — нажмите «Найти картинки с текстом».")

        # --- превью и настройки ---
        right = ctk.CTkFrame(self, corner_radius=10,
                             fg_color=("#eef1f6", "#22262f"))
        right.grid(row=1, column=1, sticky="nsew", padx=(6, 14), pady=(4, 12))
        right.grid_columnconfigure(0, weight=1)

        self.canvas = tk.Canvas(right, width=CANVAS_W, height=CANVAS_H,
                                highlightthickness=0, bg="#181b21")
        self.canvas.grid(row=0, column=0, sticky="n", padx=8, pady=(8, 2))
        self.canvas.bind("<ButtonPress-1>", self._on_press)
        self.canvas.bind("<B1-Motion>", self._on_drag)
        self.canvas.bind("<ButtonRelease-1>", self._on_release)
        self._canvas_image = None
        self._draw_placeholder()

        self.lbl_file = ctk.CTkLabel(right, text="файл не выбран",
                                     font=ctk.CTkFont(size=11),
                                     text_color=TEXT_MUTED, anchor="w")
        self.lbl_file.grid(row=1, column=0, sticky="w", padx=12)

        form = ctk.CTkFrame(right, fg_color="transparent")
        form.grid(row=2, column=0, sticky="ew", padx=8, pady=(2, 4))
        form.grid_columnconfigure(1, weight=1)

        self.var_text = tk.StringVar()
        self.var_font = tk.StringVar(value=FONT_CHOICES[0])
        self.var_size = tk.StringVar()
        self.var_colour = tk.StringVar(value="#ffffff")
        self.var_box = [tk.StringVar() for _ in range(4)]

        ctk.CTkLabel(form, text="Русский текст", anchor="w").grid(
            row=0, column=0, sticky="w", pady=2)
        entry = ctk.CTkEntry(form, textvariable=self.var_text)
        entry.grid(row=0, column=1, sticky="ew", padx=(8, 8), pady=2)
        entry.bind("<Return>", lambda _event: self.do_make())

        ctk.CTkLabel(form, text="Шрифт", anchor="w").grid(
            row=1, column=0, sticky="w", pady=2)
        ctk.CTkOptionMenu(form, values=list(FONT_CHOICES),
                          variable=self.var_font,
                          command=self._on_font_change).grid(
            row=1, column=1, sticky="ew", padx=(8, 8), pady=2)

        size_row = ctk.CTkFrame(form, fg_color="transparent")
        size_row.grid(row=2, column=1, sticky="ew", padx=(8, 8), pady=2)
        ctk.CTkLabel(form, text="Размер", anchor="w").grid(
            row=2, column=0, sticky="w", pady=2)
        ctk.CTkEntry(size_row, textvariable=self.var_size, width=60,
                     placeholder_text="авто").pack(side="left")
        ctk.CTkLabel(size_row, text="  Цвет", width=44, anchor="e").pack(
            side="left", padx=(12, 0))
        ctk.CTkEntry(size_row, textvariable=self.var_colour, width=90).pack(
            side="left", padx=(4, 0))
        ctk.CTkButton(size_row, text="…", width=32, height=26,
                      command=self._pick_colour).pack(side="left", padx=(4, 0))

        box_row = ctk.CTkFrame(form, fg_color="transparent")
        box_row.grid(row=3, column=1, sticky="ew", padx=(8, 8), pady=2)
        ctk.CTkLabel(form, text="Рамка", anchor="w").grid(
            row=3, column=0, sticky="w", pady=2)
        for index, caption in enumerate(("x", "y", "ш", "в")):
            ctk.CTkLabel(box_row, text=caption, width=16).pack(side="left")
            ctk.CTkEntry(box_row, textvariable=self.var_box[index],
                         width=52).pack(side="left", padx=(0, 6))
        ctk.CTkButton(box_row, text="Авто", width=56, height=26,
                      command=self._auto_box).pack(side="left")

        actions = ctk.CTkFrame(right, fg_color="transparent")
        actions.grid(row=3, column=0, sticky="ew", padx=8, pady=(0, 6))
        actions.grid_columnconfigure((0, 1), weight=1)
        self.btn_make = ctk.CTkButton(actions, text="1. Сделать картинку",
                                      height=30,
                                      command=self.do_make, state="disabled")
        self.btn_make.grid(row=0, column=0, sticky="ew", padx=(0, 4))
        self.btn_apply = ctk.CTkButton(actions, text="2. Применить к игре",
                                       height=30,
                                       fg_color="#1f6f43",
                                       hover_color="#195636",
                                       command=self.do_apply,
                                       state="disabled")
        self.btn_apply.grid(row=0, column=1, sticky="ew", padx=(4, 0))
        ctk.CTkButton(actions, text="Показать оригинал / русскую",
                      height=26, fg_color="#3a3f4b",
                      hover_color="#2f333d",
                      command=self._toggle_ru).grid(row=1, column=0,
                                                    columnspan=2, sticky="ew",
                                                    pady=(6, 0))

        self.box_log = ctk.CTkTextbox(self, height=92, corner_radius=10,
                                      font=ctk.CTkFont(size=11))
        self.box_log.grid(row=2, column=0, columnspan=2, sticky="nsew",
                          padx=14, pady=(0, 12))
        self._set_log("Здесь появятся сообщения: что нашлось, что "
                      "нарисовано, какие файлы игры изменены.")

    # ------------------------------------------------------------------
    # вспомогательное
    # ------------------------------------------------------------------
    def _set_log(self, text: str, append: bool = False) -> None:
        self.box_log.configure(state="normal")
        if not append:
            self.box_log.delete("1.0", "end")
        self.box_log.insert("end", text + "\n")
        self.box_log.see("end")
        self.box_log.configure(state="disabled")

    def _placeholder(self, text: str) -> None:
        for child in self.list_box.winfo_children():
            child.destroy()
        ctk.CTkLabel(self.list_box, text=text, wraplength=205,
                     justify="left", font=ctk.CTkFont(size=11),
                     text_color=TEXT_MUTED).pack(anchor="w", padx=6, pady=6)

    def _draw_placeholder(self) -> None:
        self.canvas.delete("all")
        self.canvas.create_text(CANVAS_W // 2, CANVAS_H // 2,
                                text="Выберите картинку слева",
                                fill="#7c8698", font=("Segoe UI", 11))

    def _project(self):
        from core.scanner import scan_project

        path = (self.app.var_path.get() or "").strip()
        if not path:
            raise RuntimeError("Сначала выберите папку игры.")
        return scan_project(path)

    def _font_path(self) -> Optional[Path]:
        from core.image_text import default_font

        choice = self.var_font.get()
        if choice == "Другой файл…":
            return self._custom_font
        name = FONT_FILES.get(choice)
        base = Path(__file__).resolve().parent / "assets" / "fonts"
        if name and (base / name).is_file():
            return base / name
        return default_font()

    def _on_font_change(self, choice: str) -> None:
        if choice != "Другой файл…":
            self._custom_font = None
            return
        path = filedialog.askopenfilename(
            title="Файл шрифта",
            filetypes=[("Шрифты", "*.ttf *.otf"), ("Все файлы", "*.*")])
        if not path:
            self.var_font.set(FONT_CHOICES[0])
            return
        self._custom_font = Path(path)
        self._set_log(f"шрифт: {path}", append=True)

    def _pick_colour(self) -> None:
        rgb, hex_value = colorchooser.askcolor(color=self.var_colour.get()
                                               or "#ffffff",
                                               title="Цвет текста")
        if hex_value:
            self.var_colour.set(hex_value)

    # ------------------------------------------------------------------
    # поиск
    # ------------------------------------------------------------------
    def do_scan(self) -> None:
        def task():
            from core.image_text import find_candidates

            project = self._project()
            return find_candidates(project, min_score=0.25)

        def done(candidates) -> None:
            self.candidates = candidates
            for child in self.list_box.winfo_children():
                child.destroy()
            if not candidates:
                self._placeholder("Надписей-картинок в экранах не найдено. "
                                  "Если знаете файл руками — откройте его в "
                                  "графическом редакторе и положите рядом "
                                  "версию с '_ru'.")
            for cand in candidates:
                text = (f"{cand.rel}\n{cand.width}×{cand.height} · "
                        f"{cand.verdict()}")
                ctk.CTkButton(self.list_box, text=text, anchor="w",
                              height=46, width=235,
                              font=ctk.CTkFont(size=11),
                              fg_color=("#dde3ec", "#2b3140"),
                              hover_color=("#cdd6e3", "#353c4c"),
                              text_color=("#1d2430", "#e7ecf3"),
                              command=lambda c=cand: self._select(c)
                              ).pack(fill="x", pady=2)
            self._set_log(f"Найдено картинок: {len(candidates)}. "
                          f"Выберите ту, где нарисован текст.")

        self.app._run("Поиск картинок с текстом", task, done)

    # ------------------------------------------------------------------
    # выбор и превью
    # ------------------------------------------------------------------
    def _select(self, cand) -> None:
        self.current = cand
        self._showing_ru = False
        self.lbl_file.configure(text=f"{cand.rel}  ({cand.width}×"
                                     f"{cand.height}) · ссылки: "
                                     f"{', '.join(cand.places[:3])}")
        self._auto_box()
        self._render()

    def _auto_box(self) -> None:
        cand = self.current
        if cand is None:
            return
        box = cand.bbox or (0, 0, cand.width, cand.height)
        for var, value in zip(self.var_box, box):
            var.set(str(int(value)))
        self.box = box
        self._update_canvas_box()

    def _manual_box(self) -> Optional[tuple[int, int, int, int]]:
        try:
            x0, y0, x1, y1 = (int(var.get()) for var in self.var_box)
        except (TypeError, ValueError):
            return self.box
        if x1 <= x0 or y1 <= y0:
            return self.box
        return (x0, y0, x1, y1)

    def _render(self) -> None:
        if Image is None or self.current is None:
            return
        path = self.current.path
        if self._showing_ru:
            ru = path.with_name(f"{path.stem}_ru{path.suffix}")
            if ru.is_file():
                path = ru
        try:
            image = Image.open(path).convert("RGBA")
        except Exception as exc:  # noqa: BLE001
            self._set_log(f"не открывается {path}: {exc}", append=True)
            return
        width, height = image.size
        self.scale = min(CANVAS_W / max(1, width), CANVAS_H / max(1, height), 1.0)
        shown = image.resize((max(1, int(width * self.scale)),
                              max(1, int(height * self.scale))))
        # На Canvas нужен именно Tk-образ (CTkImage работает только в
        # CTkLabel/CTkButton), поэтому конвертируем через ImageTk.
        photo = ImageTk.PhotoImage(shown)
        self._canvas_image = photo
        self.canvas.delete("all")
        # шахматка, чтобы была видна прозрачность
        for row in range(0, CANVAS_H, 16):
            for column in range(0, CANVAS_W, 16):
                if (row // 16 + column // 16) % 2:
                    self.canvas.create_rectangle(column, row, column + 16,
                                                 row + 16, fill="#20242c",
                                                 outline="")
        self.canvas.create_image(CANVAS_W // 2, CANVAS_H // 2, image=photo)
        self.btn_make.configure(state="normal")
        self.btn_apply.configure(state="normal")
        self._update_canvas_box()

    def _update_canvas_box(self) -> None:
        self.canvas.delete("box")
        box = self.box
        if not box or self.current is None:
            return
        x0, y0 = self._to_canvas(box[0], box[1])
        x1, y1 = self._to_canvas(box[2], box[3])
        self.canvas.create_rectangle(x0, y0, x1, y1, outline="#e5534b",
                                     width=2, tags="box")

    def _to_canvas(self, x: float, y: float) -> tuple[float, float]:
        width = self.current.width if self.current else 1
        height = self.current.height if self.current else 1
        return (CANVAS_W / 2 - width * self.scale / 2 + x * self.scale,
                CANVAS_H / 2 - height * self.scale / 2 + y * self.scale)

    def _to_image(self, x: float, y: float) -> tuple[float, float]:
        width = self.current.width if self.current else 1
        height = self.current.height if self.current else 1
        return ((x - (CANVAS_W / 2 - width * self.scale / 2)) / self.scale,
                (y - (CANVAS_H / 2 - height * self.scale / 2)) / self.scale)

    # --- мышь ---------------------------------------------------------
    def _on_press(self, event) -> None:
        if self.current is None:
            return
        self._drag_start = (event.x, event.y)

    def _on_drag(self, event) -> None:
        if self._drag_start is None or self.current is None:
            return
        self.canvas.delete("drag")
        self.canvas.create_rectangle(self._drag_start[0], self._drag_start[1],
                                     event.x, event.y, outline="#4c9aff",
                                     dash=(4, 3), width=2, tags="drag")

    def _on_release(self, event) -> None:
        if self._drag_start is None:
            return
        x0, y0 = self._to_image(*self._drag_start)
        x1, y1 = self._to_image(event.x, event.y)
        self._drag_start = None
        self.canvas.delete("drag")
        if abs(x1 - x0) < 3 or abs(y1 - y0) < 3:
            return
        box = (int(min(x0, x1)), int(min(y0, y1)),
               int(max(x0, x1)), int(max(y0, y1)))
        for var, value in zip(self.var_box, box):
            var.set(str(value))
        self.box = box
        self._update_canvas_box()

    def _toggle_ru(self) -> None:
        if self.current is None:
            return
        self._showing_ru = not self._showing_ru
        self._render()

    # ------------------------------------------------------------------
    # рисование и подключение
    # ------------------------------------------------------------------
    def _params(self) -> dict:
        params: dict = {}
        box = self._manual_box()
        if box:
            params["box"] = box
        size = (self.var_size.get() or "").strip()
        if size.isdigit():
            params["size"] = int(size)
        colour = (self.var_colour.get() or "").strip()
        if colour:
            params["colour"] = colour
        font = self._font_path()
        if font:
            params["font"] = font
        return params

    def do_make(self) -> None:
        if self.current is None:
            self.app._warn("Сначала выберите картинку.")
            return
        rel = self.current.rel
        text = self.var_text.get()
        params = self._params()
        language = self.app.current_language()

        def task():
            from core.image_text import render_replacement

            project = self._project()
            return render_replacement(project, rel, text, **params)

        def done(target: Path) -> None:
            self._showing_ru = True
            self._render()
            self._set_log(f"нарисовано: {target.name}"
                          f" ({target.stat().st_size} байт).\n"
                          f"Дальше — «2. Применить к игре»: программа "
                          f"подставит файл в экран {rel}.", append=True)
            self._last_rel = rel
            self._last_language = language

        self._last_rel = rel
        self._last_language = language
        self.app._run("Рисуем русскую картинку", task, done)

    def do_apply(self) -> None:
        rel = getattr(self, "_last_rel", None) or (self.current.rel
                                                   if self.current else None)
        if not rel:
            self.app._warn("Сначала нажмите «Сделать картинку».")
            return
        language = getattr(self, "_last_language", None) or \
            self.app.current_language()
        project_cache = self.current

        def task():
            from core import image_text

            project = self._project()
            source = project.game_dir / rel
            target = source.with_name(f"{source.stem}_ru{source.suffix}")
            if not target.is_file():
                raise RuntimeError("нет русской картинки — сначала "
                                   "«1. Сделать картинку»")
            params = {"text": self.var_text.get()}
            box = self._manual_box()
            if box:
                params["box"] = list(box)
            image_text.register(project, language, rel,
                                project.relative(target), **params)
            changed = image_text.patch_references(project, language, [rel])
            helper = image_text.write_helper(project, language)
            return changed, helper

        def done(result) -> None:
            changed, helper = result
            self._set_log("подключено: " + (", ".join(changed) or "—") +
                          (f"\nслужебный файл: {helper.name}" if helper else ""),
                          append=True)

        self.app._run("Подключаем картинку", task, done)

    def do_revert(self) -> None:
        if not self.app._ask("Вернуть оригинальные картинки и экраны?\n\n"
                             "Изменённые экраны будут восстановлены, "
                             "нарисованные файлы '_ru' удалены."):
            return
        language = self.app.current_language()

        def task():
            from core import image_text

            project = self._project()
            return image_text.revert_references(project, language)

        def done(restored) -> None:
            self._set_log("возвращено файлов: "
                          f"{len(restored)}" + (
                              "\n" + "\n".join(restored) if restored else ""),
                          append=True)
            self._showing_ru = False
            if self.current is not None:
                self._render()

        self.app._run("Возврат исходников", task, done)
