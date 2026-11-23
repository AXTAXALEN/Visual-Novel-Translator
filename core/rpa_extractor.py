"""
Распаковка архивов Ren'Py (.rpa).

Реализован собственный читатель форматов **RPA-2.0 / RPA-3.0 / RPA-3.2**
(структура: текстовый заголовок -> zlib-сжатый pickle-индекс -> данные),
плюс запасные пути через ``rpycdec`` и консольную утилиту ``unrpa``.

Формат (RPA-3.0)::

    RPA-3.0 <offset:016x> <key:08x>\\n
    ...данные...
    <offset>: zlib(pickle({ "game/script.rpyc": [(off^key, len^key, b""), ...] }))
"""

from __future__ import annotations

import os
import pickle
import subprocess
import sys
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional

from core.utils import ensure_within, get_logger, human_size

logger = get_logger(__name__)

SCRIPT_SUFFIXES = (".rpy", ".rpyc", ".rpym", ".rpymc")


class RpaError(Exception):
    """Ошибка чтения/распаковки архива."""


@dataclass
class RpaExtractResult:
    archive: Path
    extracted: list[str] = field(default_factory=list)
    skipped: int = 0
    errors: list[str] = field(default_factory=list)
    method: str = "native"

    @property
    def ok(self) -> bool:
        return bool(self.extracted) or not self.errors

    def summary(self) -> str:
        return (f"{self.archive.name}: извлечено {len(self.extracted)} файлов "
                f"(метод: {self.method})")


# ---------------------------------------------------------------------------
# Нативный читатель RPA
# ---------------------------------------------------------------------------
#: Первые байты zlib-потока (RPA-1.0 / .rpi): индекса-заголовка просто нет.
ZLIB_MAGIC = (b"\x78\x9c", b"\x78\x01", b"\x78\xda")


def _read_header(fh) -> tuple[str, int, Optional[int]]:
    """Возвращает (версия, offset, key)."""
    fh.seek(0)
    head = fh.read(8)

    # RPA-1.0 (.rpi): файл начинается сразу со сжатого индекса.
    if head[:2] in ZLIB_MAGIC:
        return "RPA-1.0", 0, None

    fh.seek(0)
    line = fh.readline()
    try:
        header = line.decode("ascii", errors="replace").strip()
    except Exception as exc:  # pragma: no cover
        raise RpaError(f"не удалось прочитать заголовок: {exc}") from exc

    parts = header.split()
    if not parts:
        raise RpaError("пустой заголовок архива")

    version = parts[0]
    if not version.startswith(("RPA-1.0", "RPA-2.0", "RPA-3.0", "RPA-3.2")):
        raise RpaError(
            f"неподдерживаемый формат архива: {version!r} "
            "(поддерживаются RPA-1.0/2.0/3.0/3.2)"
        )

    if len(parts) < 2:
        raise RpaError("в заголовке нет смещения индекса")

    offset = int(parts[1], 16)
    key: Optional[int] = None
    if len(parts) >= 3:
        try:
            key = int(parts[2], 16)
        except ValueError:
            key = None
    return version, offset, key


def read_index(path: Path) -> tuple[dict[str, list[tuple[int, int, bytes]]], Optional[int]]:
    """
    Читает индекс архива.

    Возвращает ({имя: [(offset, length, prefix_bytes), ...]}, key).
    """
    with open(path, "rb") as fh:
        version, offset, key = _read_header(fh)

        fh.seek(offset)
        payload = fh.read()
        if not payload:
            raise RpaError("пустой индекс архива")

    try:
        raw_index = pickle.loads(zlib.decompress(payload), encoding="bytes")
    except Exception:
        try:
            raw_index = pickle.loads(payload, encoding="bytes")
        except Exception as exc:
            raise RpaError(f"не удалось разобрать индекс: {exc}") from exc

    index: dict[str, list[tuple[int, int, bytes]]] = {}
    for raw_name, entries in raw_index.items():
        name = raw_name.decode("utf-8", "surrogateescape") if isinstance(raw_name, bytes) else str(raw_name)
        name = name.replace("\\", "/")
        parts: list[tuple[int, int, bytes]] = []
        for entry in entries:
            if len(entry) == 2:
                off, length = entry[0], entry[1]
                prefix = b""
            else:
                off, length, prefix = entry[0], entry[1], entry[2] or b""
            if key is not None:
                off = off ^ key
                length = length ^ key
            parts.append((off, length, prefix))
        index[name] = parts

    return index, key


def extract_native(path: Path, out_dir: Path,
                   suffixes: Optional[Iterable[str]] = None) -> RpaExtractResult:
    """Распаковка встроенным читателем."""
    result = RpaExtractResult(archive=path, method="native")
    allowed = tuple(s.lower() for s in suffixes) if suffixes else None

    index, _ = read_index(path)

    # RPA-1.0 (.rpi): индекс лежит в .rpi, а данные — в парном .rpa
    data_path = path
    if path.suffix.lower() == ".rpi":
        paired = path.with_suffix(".rpa")
        if not paired.exists():
            raise RpaError(
                f"для индекса {path.name} нужен парный файл {paired.name}")
        data_path = paired

    with open(data_path, "rb") as fh:
        for name, parts in index.items():
            if allowed and not name.lower().endswith(allowed):
                result.skipped += 1
                continue

            # Некоторые сборщики кладут файлы с префиксом "game/" — снимаем его.
            local_name = name
            for prefix in ("game/", "Game/", "./"):
                if local_name.startswith(prefix):
                    local_name = local_name[len(prefix):]
                    break

            target = out_dir / local_name
            if not ensure_within(target, out_dir):
                result.errors.append(f"небезопасный путь в архиве: {name}")
                continue

            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                with open(target, "wb") as out:
                    for off, length, prefix in parts:
                        if prefix:
                            out.write(prefix)
                        fh.seek(off)
                        remaining = length
                        while remaining > 0:
                            chunk = fh.read(min(remaining, 1 << 20))
                            if not chunk:
                                break
                            out.write(chunk)
                            remaining -= len(chunk)
                result.extracted.append(name)
            except OSError as exc:
                result.errors.append(f"{name}: {exc}")

    return result


# ---------------------------------------------------------------------------
# Запасные варианты
# ---------------------------------------------------------------------------
def extract_with_rpycdec(path: Path, out_dir: Path,
                         suffixes: Optional[Iterable[str]] = None) -> RpaExtractResult:
    result = RpaExtractResult(archive=path, method="rpycdec")
    try:
        from rpycdec import extract_rpa  # type: ignore
    except ImportError as exc:
        raise RpaError("пакет rpycdec не установлен") from exc

    with open(path, "rb") as fh:
        extract_rpa(fh, output_dir=str(out_dir), suffixes=list(suffixes) if suffixes else None)

    for item in out_dir.rglob("*"):
        if item.is_file():
            result.extracted.append(item.relative_to(out_dir).as_posix())
    return result


def extract_with_unrpa(path: Path, out_dir: Path,
                       suffixes: Optional[Iterable[str]] = None) -> RpaExtractResult:
    result = RpaExtractResult(archive=path, method="unrpa")
    out_dir.mkdir(parents=True, exist_ok=True)
    cmd = [sys.executable, "-m", "unrpa", "-p", str(out_dir), "-mp", str(path)]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    except (OSError, subprocess.SubprocessError) as exc:
        raise RpaError(f"не удалось запустить unrpa: {exc}") from exc

    if proc.returncode != 0:
        raise RpaError(f"unrpa завершился с ошибкой: {proc.stderr[-500:]}")

    for item in out_dir.rglob("*"):
        if item.is_file():
            result.extracted.append(item.relative_to(out_dir).as_posix())
    return result


# ---------------------------------------------------------------------------
# Публичный API
# ---------------------------------------------------------------------------
def extract_archive(path: Path, out_dir: Path, scripts_only: bool = True) -> RpaExtractResult:
    """
    Распаковывает один архив.

    `scripts_only` — извлекать только скрипты (.rpy/.rpyc/.rpym).
    """
    suffixes = SCRIPT_SUFFIXES if scripts_only else None
    errors: list[str] = []

    for func in (extract_native, extract_with_rpycdec, extract_with_unrpa):
        try:
            result = func(path, out_dir, suffixes)
            if result.extracted or not result.errors:
                logger.info("Распакован %s -> %s", path.name, out_dir)
                return result
            errors.append(f"{func.__name__}: {result.errors[:3]}")
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{func.__name__}: {exc}")
            logger.debug("Метод %s не сработал для %s: %s", func.__name__, path.name, exc)

    result = RpaExtractResult(archive=path, method="failed", errors=errors)
    logger.error("Не удалось распаковать %s: %s", path, "; ".join(errors))
    return result


def extract_all(rpa_files: Iterable[Path], out_dir: Path,
                scripts_only: bool = True) -> list[RpaExtractResult]:
    """Распаковывает все найденные архивы в `out_dir` (обычно — game/)."""
    results = []
    for rpa in rpa_files:
        results.append(extract_archive(rpa, out_dir, scripts_only=scripts_only))
    return results


def list_archive(path: Path) -> list[tuple[str, int]]:
    """Список файлов в архиве: [(имя, размер)]."""
    index, _ = read_index(path)
    out = []
    for name, parts in index.items():
        size = sum(length + len(prefix) for _, length, prefix in parts)
        out.append((name, size))
    return sorted(out)


def archive_summary(path: Path) -> str:
    try:
        entries = list_archive(path)
    except RpaError as exc:
        return f"{path.name}: ошибка ({exc})"
    total = sum(size for _, size in entries)
    scripts = [n for n, _ in entries if n.lower().endswith(SCRIPT_SUFFIXES)]
    return (f"{path.name}: {len(entries)} файлов ({human_size(total)}), "
            f"из них скриптов: {len(scripts)}")
