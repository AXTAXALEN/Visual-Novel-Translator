"""
Проверка: все ли экраны, которые игра показывает, вообще существуют (3.7.4).

Зачем
-----
Файлы, полученные декомпиляцией (``.rpyc`` -> ``.rpy``), бывают неполными:
декомпилятор оставляет вместо непонятного куска кода заглушку

    # <unrecognized: SLScreen>

и **весь экран пропадает** из файла. Синтаксис при этом цел — игра
запускается, — но в момент, когда экран понадобится, Ren'Py падает с
«screen … is not defined».

Модуль ищет такие «висящие» ссылки: экран используется, но нигде не объявлен.
Проверка статическая, Ren'Py не нужен.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from config import TL_DIRECTORY
from core.scanner import RenpyProject
from core.utils import get_logger

logger = get_logger(__name__)

#: Объявление экрана: ``screen preferences():`` / ``screen edghlwstart:``
DECL_RE = re.compile(r"^[ \t]*screen[ \t]+(?P<name>[A-Za-z_]\w*)", re.M)

#: Использование экрана в скрипте и в экранном языке.
USE_RES = (
    re.compile(r"\b(?:show|call)[ \t]+screen[ \t]+(?P<name>[A-Za-z_]\w*)"),
    re.compile(r"^[ \t]*use[ \t]+(?P<name>[A-Za-z_]\w*)[ \t]*$", re.M),
    re.compile(r"\brenpy\.(?:show|call|hide)_screen\([ \t]*[\"']"
               r"(?P<name>[A-Za-z_]\w*)[\"']"),
    re.compile(r"\bShowMenu\([ \t]*[\"'](?P<name>[A-Za-z_]\w*)[\"']"),
    re.compile(r"\bShow\([ \t]*[\"']screen[ \t]+(?P<name>[A-Za-z_]\w*)[\"']"),
)

#: Экраны, которые Ren'Py объявляет сам или которые обычно есть в игре —
#: ругаться на них нельзя.
BUILTIN_SCREENS = frozenset({
    "say", "choice", "input", "nvl", "history", "help", "about", "save",
    "load", "preferences", "main_menu", "game_menu", "navigation", "quick_menu",
    "confirm", "skip_indicator", "notify", "nvl_dialogue", "auto", "menu",
    "file_slots", "file_picker", "page", "gui", "bubble", "quick_button",
    "keyboard_help", "mouse_help", "gamepad_help", "if", "while",
})


@dataclass
class ScreenRefsReport:
    declared: int = 0
    missing: list[str] = field(default_factory=list)
    """``экран — где используется``"""

    @property
    def ok(self) -> bool:
        return not self.missing

    def summary(self) -> str:
        if self.ok:
            return f"экраны: объявлено {self.declared}, висящих ссылок нет"
        return (f"экраны: объявлено {self.declared}, не объявлено в .rpy: "
                f"{len(self.missing)} (если экрана нет и в .rpyc — игра "
                f"упадёт, когда его покажет)")

    def lines(self, limit: int = 10) -> list[str]:
        out = [self.summary()]
        for item in self.missing[:limit]:
            out.append(f"  ! экран не объявлен в .rpy: {item}")
        if len(self.missing) > limit:
            out.append(f"  … и ещё {len(self.missing) - limit}")
        return out


def _iter_script_files(project: RenpyProject):
    tl_root = (project.game_dir / TL_DIRECTORY).resolve()
    for path in project.rpy_files:
        try:
            if tl_root in path.resolve().parents:
                continue
        except OSError:
            pass
        yield path


def check_screen_refs(project: RenpyProject) -> ScreenRefsReport:
    """Найти экраны, которые показываются, но нигде не объявлены."""
    report = ScreenRefsReport()
    if project is None:
        return report

    texts: dict[Path, str] = {}
    declared: set[str] = set()
    for path in _iter_script_files(project):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        texts[path] = text
        declared.update(DECL_RE.findall(text))
    report.declared = len(declared)
    known = declared | BUILTIN_SCREENS

    seen: dict[str, str] = {}
    for path, text in texts.items():
        for pattern in USE_RES:
            for match in pattern.finditer(text):
                name = match.group("name")
                if name in known:
                    continue
                if name in seen:
                    continue
                line = text.count("\n", 0, match.start()) + 1
                seen[name] = f"{path.name}:{line}"
                report.missing.append(f"{name} ({path.name}:{line})")

    if report.missing:
        logger.warning("экраны не найдены: %s",
                       ", ".join(item.split(" ")[0] for item in report.missing[:5]))
    return report
