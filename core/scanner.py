"""
Обнаружение и валидация проекта Ren'Py.

Пользователь может указать:
  * корень игры (папку, в которой лежит `game/`), например
    ``C:/Games/MyVN``;
  * саму папку ``game``;
  * вообще любую папку внутри игры — мы поднимемся наверх и найдём `game/`.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from config import EXCLUDED_DIRS, IGNORED_FILES, RPA_EXT, RPI_EXT, RPY_EXT, RPYC_EXT, \
    RPYM_EXT, RPYMC_EXT, TL_DIRECTORY


class ProjectError(Exception):
    """Ошибка валидации проекта."""


@dataclass
class RenpyProject:
    """Обнаруженный проект Ren'Py."""

    game_dir: Path
    project_root: Path
    rpy_files: list[Path] = field(default_factory=list)
    rpyc_files: list[Path] = field(default_factory=list)
    rpa_files: list[Path] = field(default_factory=list)
    tl_dir: Optional[Path] = None
    language_dirs: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    has_engine_dir: bool = False

    # ------------------------------------------------------------------
    @property
    def name(self) -> str:
        return self.project_root.name

    @property
    def has_scripts(self) -> bool:
        return bool(self.rpy_files or self.rpyc_files or self.rpa_files)

    @property
    def needs_preparation(self) -> bool:
        """Нужна ли распаковка .rpa / декомпиляция .rpyc."""
        if self.rpy_files:
            # Даже при наличии .rpy могут остаться неразобранные главы в .rpyc
            # или целые главы в архиве .rpa.
            empty_rpyc = [
                p for p in self.rpyc_files
                if not p.with_suffix(".rpy").exists()
            ]
            return bool(self.rpa_files or empty_rpyc)
        return bool(self.rpyc_files or self.rpa_files)

    def relative(self, path: Path) -> str:
        try:
            return path.relative_to(self.game_dir).as_posix()
        except ValueError:
            return path.as_posix()

    def summary(self) -> str:
        return (
            f"Игра: {self.name}\n"
            f"  корень:        {self.project_root}\n"
            f"  game/:         {self.game_dir}\n"
            f"  .rpy файлов:   {len(self.rpy_files)}\n"
            f"  .rpyc файлов:  {len(self.rpyc_files)}\n"
            f"  .rpa архивов:  {len(self.rpa_files)}\n"
            f"  переводы (tl): "
            + (", ".join(self.language_dirs) if self.language_dirs else "нет")
        )


# ---------------------------------------------------------------------------
# Поиск папки game/
# ---------------------------------------------------------------------------
def _looks_like_game_dir(path: Path) -> bool:
    """Папка похожа на `game/`: есть скрипты, архивы или стандартные файлы."""
    if not path.is_dir():
        return False
    if (path / "script.rpy").exists() or (path / "script.rpyc").exists():
        return True
    if (path / "options.rpy").exists() or (path / "screens.rpy").exists():
        return True
    for entry in path.iterdir():
        if entry.suffix.lower() in (RPY_EXT, RPYC_EXT, RPYM_EXT, RPYMC_EXT, RPA_EXT):
            return True
    return False


def find_game_dir(raw_path: str | Path) -> tuple[Path, Path]:
    """
    Возвращает (game_dir, project_root).

    Порядок проверок:
      1. Если в указанной папке есть `game/` — это корень игры.
      2. Если сама папка называется `game` — это game_dir.
      3. Если папка содержит .rpy/.rpyc/.rpa — это game_dir.
      4. Иначе поднимаемся по родителям вверх (до 3 уровней).
    """
    path = Path(raw_path).expanduser()
    try:
        path = path.resolve()
    except OSError:
        pass

    if not path.exists():
        raise ProjectError(f"Путь не существует: {path}")
    if not path.is_dir():
        raise ProjectError(f"Ожидалась папка, а не файл: {path}")

    candidates: list[Path] = [path] + list(path.parents)[:3]
    for cand in candidates:
        game = cand / "game"
        if game.is_dir() and _looks_like_game_dir(game):
            return game, cand

    if path.name.lower() == "game" and _looks_like_game_dir(path):
        return path, path.parent

    if _looks_like_game_dir(path):
        return path, path.parent

    raise ProjectError(
        "Не удалось найти папку `game` с файлами Ren'Py.\n"
        f"Проверено: {path} и родительские каталоги.\n"
        "Укажите путь к папке game (например C:/Games/MyVN/game) "
        "или к корню игры (C:/Games/MyVN)."
    )


# ---------------------------------------------------------------------------
# Обход файлов
# ---------------------------------------------------------------------------
def _walk_scripts(root: Path, suffix: str) -> list[Path]:
    """
    Рекурсивно собирает файлы с расширением `suffix`, пропуская служебные
    каталоги (renpy/, tl/, cache/, saves/ ...).
    """
    result: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [
            d for d in sorted(dirnames)
            if d.lower() not in EXCLUDED_DIRS and not d.startswith(".")
        ]
        for fn in sorted(filenames):
            if fn.lower() in IGNORED_FILES:
                continue
            if fn.lower().endswith(suffix):
                result.append(Path(dirpath) / fn)
    return result


def scan_project(raw_path: str | Path) -> RenpyProject:
    """Полное сканирование проекта."""
    game_dir, project_root = find_game_dir(raw_path)

    project = RenpyProject(game_dir=game_dir, project_root=project_root)

    project.rpy_files = _walk_scripts(game_dir, RPY_EXT)
    project.rpyc_files = _walk_scripts(game_dir, RPYC_EXT)
    project.rpy_files += _walk_scripts(game_dir, RPYM_EXT)
    project.rpyc_files += _walk_scripts(game_dir, RPYMC_EXT)

    # Архивы .rpa лежат прямо в game/ (иногда в подпапках)
    for dirpath, dirnames, filenames in os.walk(game_dir):
        dirnames[:] = [d for d in dirnames if d.lower() not in {"renpy", "cache", "saves"}]
        for fn in filenames:
            name = fn.lower()
            if name.endswith((RPA_EXT, RPI_EXT)):
                project.rpa_files.append(Path(dirpath) / fn)

    tl_dir = game_dir / TL_DIRECTORY
    if tl_dir.is_dir():
        project.tl_dir = tl_dir
        project.language_dirs = sorted(
            p.name for p in tl_dir.iterdir() if p.is_dir()
        )

    project.has_engine_dir = (project_root / "renpy").is_dir()

    if not project.has_scripts:
        project.warnings.append(
            "В папке game/ не найдено ни .rpy, ни .rpyc, ни .rpa файлов. "
            "Возможно, указан не тот каталог или игра использует другую "
            "структуру файлов."
        )

    return project


def find_translation_files(game_dir: Path, language: str) -> list[Path]:
    """Все .rpy файлы существующего перевода для языка."""
    tl_dir = game_dir / TL_DIRECTORY / language
    if not tl_dir.is_dir():
        return []
    return sorted(tl_dir.rglob("*.rpy"))


def list_all_scripts(game_dir: Path) -> list[Path]:
    """Все .rpy файлы игры, включая tl/ (для служебных задач)."""
    return _walk_scripts(game_dir, RPY_EXT)


def is_generated_file(path: Path) -> bool:
    """Проверяет, что файл создан нашей программой (по шапке)."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            head = f.read(512)
    except OSError:
        return False
    return "renpy-translator" in head
