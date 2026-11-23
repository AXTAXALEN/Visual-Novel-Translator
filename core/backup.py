"""
Резервное копирование скриптов игры.

Перед любым изменением файлов создаётся архив
``<корень игры>/_renpy_translator_backup/backup_YYYYmmdd_HHMMSS.zip``,
в который попадают все .rpy/.rpyc/.rpym (текстовые скрипты).

Ассеты (.rpa с картинками/музыкой) не копируются: они большие и мы их
не изменяем.
"""

from __future__ import annotations

import json
import shutil
import time
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional

from config import APP_NAME, VERSION
from core.scanner import RenpyProject
from core.utils import get_logger, human_size

logger = get_logger(__name__)

BACKUP_DIR_NAME = "_renpy_translator_backup"
BACKUP_SUFFIXES = {".rpy", ".rpyc", ".rpym", ".rpymc"}


@dataclass
class BackupResult:
    path: Optional[Path] = None
    files: int = 0
    size: int = 0
    skipped: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.path is not None

    def summary(self) -> str:
        if not self.ok:
            return "бэкап не создан"
        return f"бэкап: {self.path.name} ({self.files} файлов, {human_size(self.size)})"


def create_backup(project: RenpyProject, extra_paths: Iterable[Path] = (),
                  include_tl: bool = True) -> BackupResult:
    """
    Создаёт zip-архив со скриптами игры.

    `extra_paths` — дополнительные файлы (например, кэш переводов).
    """
    result = BackupResult()
    backup_dir = project.project_root / BACKUP_DIR_NAME

    try:
        backup_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        logger.error("Не удалось создать каталог бэкапов %s: %s", backup_dir, exc)
        return result

    timestamp = time.strftime("%Y%m%d_%H%M%S")
    archive_path = backup_dir / f"backup_{timestamp}.zip"

    counter = 0
    try:
        with zipfile.ZipFile(archive_path, "w", zipfile.ZIP_DEFLATED) as zf:
            manifest = {
                "app": APP_NAME,
                "version": VERSION,
                "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "game": str(project.project_root),
                "files": [],
            }

            for path in project.rpy_files + project.rpyc_files:
                if not include_tl and "tl" in path.parts:
                    continue
                try:
                    rel = path.relative_to(project.project_root).as_posix()
                    zf.write(path, rel)
                    manifest["files"].append(rel)
                    counter += 1
                except (OSError, ValueError) as exc:
                    result.skipped.append(f"{path}: {exc}")

            for path in extra_paths:
                try:
                    path = Path(path)
                    if path.exists():
                        zf.write(path, "extras/" + path.name)
                        counter += 1
                except OSError as exc:
                    result.skipped.append(f"{path}: {exc}")

            zf.writestr("manifest.json",
                        json.dumps(manifest, ensure_ascii=False, indent=2))
    except OSError as exc:
        logger.error("Не удалось создать бэкап: %s", exc)
        return result

    result.path = archive_path
    result.files = counter
    result.size = archive_path.stat().st_size
    logger.info("Создан бэкап: %s (%d файлов, %s)",
                archive_path, counter, human_size(result.size))

    _cleanup_old_backups(backup_dir)
    return result


def _cleanup_old_backups(backup_dir: Path, keep: int = 10) -> None:
    """Оставляет `keep` самых свежих архивов."""
    try:
        archives = sorted(backup_dir.glob("backup_*.zip"))
    except OSError:
        return
    for old in archives[:-keep]:
        try:
            old.unlink()
            logger.debug("Удалён старый бэкап %s", old)
        except OSError:
            pass


def list_backups(project: RenpyProject) -> list[Path]:
    backup_dir = project.project_root / BACKUP_DIR_NAME
    if not backup_dir.is_dir():
        return []
    return sorted(backup_dir.glob("backup_*.zip"), reverse=True)


# ---------------------------------------------------------------------------
# Восстановление (v3.0)
# ---------------------------------------------------------------------------
@dataclass
class RestoreResult:
    """Итог отката из резервной копии."""

    archive: Optional[Path] = None
    restored: int = 0
    removed: int = 0
    problems: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.archive is not None and not self.problems

    def summary(self) -> str:
        if self.archive is None:
            return "резервная копия не найдена"
        text = f"восстановлено файлов: {self.restored}"
        if self.removed:
            text += f", удалено лишних: {self.removed}"
        if self.problems:
            text += f", проблем: {len(self.problems)}"
        return text


def latest_backup(project: RenpyProject) -> Optional[Path]:
    """Самая свежая резервная копия проекта."""
    backups = list_backups(project)   # свежие первыми
    return backups[0] if backups else None


def restore_backup(project: RenpyProject,
                   archive: Optional[Path] = None,
                   clean_tl: bool = True) -> RestoreResult:
    """Вернуть скрипты игры из zip-бэкапа.

    ``clean_tl=True`` удаляет каталоги ``game/tl/<язык>``, которые создала
    программа, — иначе после отката останется «висящий» перевод. Каталог
    ``tl`` целиком не трогается, только наши подкаталоги, которых не было
    в архиве.
    """
    result = RestoreResult()
    archive = Path(archive) if archive else latest_backup(project)
    if archive is None or not archive.is_file():
        result.problems.append("резервных копий нет")
        return result
    result.archive = archive

    from config import TL_DIRECTORY

    in_archive: set[str] = set()
    try:
        with zipfile.ZipFile(archive) as zf:
            names = zf.namelist()
            for name in names:
                if name == "manifest.json" or name.startswith("extras/"):
                    continue
                if not name.lower().endswith(tuple(BACKUP_SUFFIXES)):
                    continue
                target = project.project_root / name
                try:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with zf.open(name) as src, open(target, "wb") as dst:
                        dst.write(src.read())
                    result.restored += 1
                    in_archive.add(name)
                except (OSError, zipfile.BadZipFile) as exc:
                    result.problems.append(f"{name}: {exc}")
    except (OSError, zipfile.BadZipFile) as exc:
        result.problems.append(f"архив не читается: {exc}")
        return result

    if clean_tl:
        game_dir = project.game_dir
        tl_root = game_dir / TL_DIRECTORY
        if tl_root.is_dir():
            for lang_dir in sorted(tl_root.iterdir()):
                if not lang_dir.is_dir():
                    continue
                rel_prefix = lang_dir.relative_to(project.project_root).as_posix()
                if any(name.startswith(rel_prefix + "/") for name in in_archive):
                    continue
                try:
                    shutil.rmtree(lang_dir)
                    result.removed += 1
                    logger.info("Удалён каталог перевода: %s", lang_dir)
                except OSError as exc:
                    result.problems.append(f"{lang_dir}: {exc}")

    logger.info("Восстановление из %s: %s", archive.name, result.summary())
    return result
