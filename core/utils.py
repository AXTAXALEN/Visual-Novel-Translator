"""Вспомогательные утилиты: логирование, файловые операции, прогресс."""

from __future__ import annotations

import json
import logging
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Iterable, Optional

LOGGER_NAME = "renpy_translator"

_log_configured = False


def get_logger(name: str = LOGGER_NAME) -> logging.Logger:
    return logging.getLogger(name)


def setup_logging(log_path: Optional[Path] = None, verbose: bool = False) -> logging.Logger:
    """Настраивает логирование в консоль и в файл (translator.log)."""
    global _log_configured
    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(logging.DEBUG if verbose else logging.INFO)
    logger.propagate = False

    if _log_configured:
        return logger

    fmt = logging.Formatter(
        "%(asctime)s | %(levelname)-7s | %(message)s", datefmt="%H:%M:%S"
    )

    console = logging.StreamHandler(stream=sys.stdout)
    console.setLevel(logging.DEBUG if verbose else logging.INFO)
    console.setFormatter(fmt)
    logger.addHandler(console)

    if log_path is not None:
        try:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            file_handler = logging.FileHandler(log_path, encoding="utf-8")
            file_handler.setLevel(logging.DEBUG)
            file_handler.setFormatter(
                logging.Formatter(
                    "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s"
                )
            )
            logger.addHandler(file_handler)
        except Exception as exc:  # pragma: no cover
            print(f"[!] не удалось открыть лог-файл {log_path}: {exc}")

    _log_configured = True
    return logger


def read_text_file(path: Path) -> str:
    """
    Читает текстовый файл Ren'Py.

    Ren'Py-скрипты практически всегда в UTF-8 (иногда с BOM), но встречаются
    и cp1251 (самодельные сборки) — поэтому есть запасной вариант.
    """
    raw = path.read_bytes()
    for encoding in ("utf-8-sig", "utf-8", "cp1251"):
        try:
            return normalize_newlines(raw.decode(encoding))
        except UnicodeDecodeError:
            continue
    return normalize_newlines(raw.decode("utf-8", errors="replace"))


def normalize_newlines(text: str) -> str:
    """
    Приводит переводы строк к ``\n``.

    Скрипты Ren'Py, написанные под Windows, сплошь и рядом сохранены в CRLF.
    Если оставить ``\r`` в конце строки, он приклеивается к последнему
    строковому литералу (``"Текст"\r``) и разбор реплики ломается — именно
    из-за этого в иных сборках «находилось» ноль реплик при 179 КБ текста.
    """
    if "\r" not in text:
        return text
    return text.replace("\r\n", "\n").replace("\r", "\n")


def read_raw_bytes(path: Path) -> bytes:
    return path.read_bytes()


def atomic_write_text(path: Path, text: str, encoding: str = "utf-8") -> None:
    """Атомарная запись текста (через временный файл + rename)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp_", suffix=path.suffix)
    try:
        with os.fdopen(fd, "w", encoding=encoding, newline="\n") as f:
            f.write(text)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except OSError:
                pass


def atomic_write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp_")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except OSError:
                pass


def atomic_write_json(path: Path, data: Any, indent: int = 2) -> None:
    text = json.dumps(data, ensure_ascii=False, indent=indent)
    atomic_write_text(path, text + "\n")


def human_duration(seconds: float) -> str:
    """'1 ч 05 мин' / '3 мин 07 с' / '12 с'."""
    seconds = max(0, int(seconds))
    if seconds >= 3600:
        return f"{seconds // 3600} ч {(seconds % 3600) // 60:02d} мин"
    if seconds >= 60:
        return f"{seconds // 60} мин {seconds % 60:02d} с"
    return f"{seconds} с"


def human_size(num: float) -> str:
    for unit in ("Б", "КБ", "МБ", "ГБ"):
        if num < 1024 or unit == "ГБ":
            return f"{num:.1f} {unit}" if unit != "Б" else f"{int(num)} {unit}"
        num /= 1024.0
    return f"{num:.1f} ГБ"


def safe_relpath(path: Path, base: Path) -> str:
    """Относительный POSIX-путь (для JSON/логов)."""
    try:
        return path.relative_to(base).as_posix()
    except ValueError:
        return path.as_posix()


def ensure_within(path: Path, base: Path) -> bool:
    """Защита от path traversal при распаковке архивов."""
    try:
        path.resolve().relative_to(base.resolve())
        return True
    except ValueError:
        return False


def remove_file(path: Path, logger: Optional[logging.Logger] = None) -> bool:
    try:
        if path.exists():
            path.unlink()
            return True
    except OSError as exc:
        if logger:
            logger.warning("не удалось удалить %s: %s", path, exc)
    return False


def copy_tree(src: Path, dst: Path, ignore_patterns: Iterable[str] = ()) -> None:
    shutil.copytree(
        src,
        dst,
        dirs_exist_ok=True,
        ignore=shutil.ignore_patterns(*ignore_patterns) if ignore_patterns else None,
    )


class Progress:
    """
    Прогресс-бар с ETA.

    Использует tqdm, если он установлен; иначе печатает простую строку.
    """

    def __init__(self, total: int, desc: str = "", enabled: bool = True,
                 callback=None) -> None:
        self.total = max(0, total)
        self.desc = desc
        self.enabled = enabled
        self.callback = callback
        self.count = 0
        self._start = time.monotonic()
        self._last_print = 0.0
        self._tqdm = None

        if enabled and self.total:
            try:
                from tqdm import tqdm  # type: ignore

                self._tqdm = tqdm(total=self.total, desc=desc, unit="стр",
                                  dynamic_ncols=True)
            except Exception:
                self._tqdm = None

    # ------------------------------------------------------------------
    def update(self, n: int = 1) -> None:
        self.count += n
        if self._tqdm is not None:
            self._tqdm.update(n)
        elif self.enabled:
            now = time.monotonic()
            if now - self._last_print > 0.4 or self.count >= self.total:
                self._last_print = now
                sys.stdout.write("\r" + self.status_line().ljust(78))
                sys.stdout.flush()
                if self.count >= self.total:
                    sys.stdout.write("\n")
                    sys.stdout.flush()
        if self.callback is not None:
            try:
                self.callback(self.count, self.total)
            except Exception:  # pragma: no cover
                pass

    def elapsed_eta(self) -> float:
        """Оценка оставшегося времени, сек."""
        elapsed = time.monotonic() - self._start
        rate = self.count / elapsed if elapsed > 0 and self.count else 0.0
        return (self.total - self.count) / rate if rate else 0.0

    def status_line(self) -> str:
        elapsed = time.monotonic() - self._start
        rate = self.count / elapsed if elapsed > 0 and self.count else 0.0
        eta = (self.total - self.count) / rate if rate else 0.0
        pct = (self.count / self.total * 100.0) if self.total else 0.0
        return (f"{self.desc}: {self.count}/{self.total} ({pct:5.1f}%) "
                f"ETA {human_duration(eta)}")

    def close(self) -> None:
        if self._tqdm is not None:
            self._tqdm.close()
        elif self.enabled and self.total and self.count >= self.total:
            pass


class OperationStats:
    """Простая статистика выполнения (для отчёта в конце)."""

    def __init__(self) -> None:
        self.started_at = time.time()
        self.counter: dict[str, int] = {}
        self.notes: list[str] = []
        self.warnings: list[str] = []
        self.errors: list[str] = []

    def inc(self, key: str, n: int = 1) -> None:
        self.counter[key] = self.counter.get(key, 0) + n

    def note(self, text: str) -> None:
        self.notes.append(text)

    def warn(self, text: str, logger: Optional[logging.Logger] = None) -> None:
        self.warnings.append(text)
        if logger:
            logger.warning(text)

    def error(self, text: str, logger: Optional[logging.Logger] = None) -> None:
        self.errors.append(text)
        if logger:
            logger.error(text)

    @property
    def elapsed(self) -> float:
        return time.time() - self.started_at

    def summary(self) -> str:
        parts = [f"{k}: {v}" for k, v in sorted(self.counter.items())]
        parts.append(f"время: {human_duration(self.elapsed)}")
        return "; ".join(parts)
