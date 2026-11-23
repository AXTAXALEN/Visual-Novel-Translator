"""
Страховка от «expected ':' not found» в файлах игры.

Зачем
-----
Ren'Py падает на первой же строке, где у блочной конструкции пропало
двоеточие::

    menu        ← было `menu:`
        "Да" :
            jump yes

Наши правки (обёртка строк в ``_()``, замена меню-картинок, подстановка
картинок ``rt_img()``) работают построчно по тексту, поэтому такая опечатка
теоретически возможна — и лучше поймать её у себя, чем увидеть её в игре.

Модуль делает две вещи:

1. ``check_project`` — находит строки-блоки без двоеточия (только там, где
   следующая непустая строка имеет больший отступ, то есть блок реально
   начинается);
2. ``repair_project`` — возвращает двоеточие на место (перед комментарием),
   предварительно сохранив копию файла.

Отдельный случай — **файлы, полученные декомпиляцией** (``.rpyc`` -> ``.rpy``).
Декомпилятор не умеет разбирать аргументы меню и печатает вместо них::

    menu # <unrecognized: ArgumentInfo>
    #   arguments: [('nvl', 'True')]

То есть ``menu (nvl=True):`` превращается в ``menu`` без двоеточия — игра
падает ровно с «expected ':' not found». Такие строки восстанавливаются
вместе с аргументами (``restore_argument_info``).

Проверка запускается автоматически после всех правок файлов игры.
"""

from __future__ import annotations

import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional

from config import TL_DIRECTORY
from core.scanner import RenpyProject
from core.utils import get_logger

logger = get_logger(__name__)

#: Ключевые слова, которые открывают блок **всегда**: `menu` без двоеточия —
#: это ошибка в любом месте файла, даже если следующая строка не с отступом
#: (именно так выглядит «line 24: expected ':' not found»).
#:
#: Здесь намеренно только ``menu``: ``label`` в экранах — обычный элемент
#: (``label title``), ``style foo is default`` — тоже корректная строка без
#: двоеточия. Дописывать им двоеточие «на всякий случай» нельзя — так можно
#: сломать рабочий файл.
ALWAYS_COLON = ("menu",)

#: Ключевые слова, которые открывают блок только при отступе у следующей
#: строки: внутри `init python:` слово `for` — обычный Python-код, в экранах
#: `label title` — элемент экрана, а `style foo is default` — строка без блока.
GATED_COLON = ("label", "screen", "style", "translate",
               "init", "python", "while", "for")

BLOCK_KEYWORDS = ALWAYS_COLON + GATED_COLON

_KEYWORD_RE = re.compile(r"^(?P<indent>[ \t]*)(?P<keyword>" +
                         "|".join(BLOCK_KEYWORDS) + r")\b(?P<rest>[^\n]*)")
_TRAILING_COMMENT = re.compile(r"\s+#.*$")

#: Заглушка декомпилятора вместо аргументов меню.
ARGINFO_RE = re.compile(
    r"^(?P<indent>[ \t]*)(?P<stmt>menu\b[^#\n]*?)[ \t]*"
    r"#[ \t]*<unrecognized:[ \t]*ArgumentInfo>")
#: Строка ``#   arguments: [('nvl', 'True')]`` под такой заглушкой.
ARGS_LINE_RE = re.compile(r"^\s*#[ \t]*arguments:[ \t]*\[(?P<body>.*)\]\s*$")
#: Любая заглушка декомпилятора: ``# <unrecognized: SLScreen>`` и т.п.
UNRECOGNIZED_RE = re.compile(r"#[ \t]*<unrecognized:[ \t]*(?P<kind>[A-Za-z_]+)>")
BACKUP_DIR = "rt_syntax_backup"


@dataclass
class SyntaxIssue:
    """Строка без двоеточия."""

    path: Path
    line: int
    text: str
    fixed: bool = False
    context: str = ""
    """Соседние строки — чтобы понять, откуда взялась поломка."""

    def where(self, project: Optional[RenpyProject] = None) -> str:
        name = self.path.name
        if project is not None:
            try:
                name = self.path.relative_to(project.game_dir).as_posix()
            except ValueError:
                pass
        return f"{name}:{self.line}"

    def describe(self) -> str:
        state = "исправлено" if self.fixed else "требует правки"
        text = f"{self.where()}: {self.text.strip()}  ({state})"
        if self.context:
            text += f"  [{self.context}]"
        return text


@dataclass
class SyntaxReport:
    issues: list[SyntaxIssue] = field(default_factory=list)
    checked: int = 0
    repaired: int = 0
    restored_args: int = 0
    """Сколько меню восстановлено вместе с аргументами (``nvl=True`` и т.п.)."""
    lossy: list[str] = field(default_factory=list)
    """Заглушки декомпилятора: ``файл:строка — <unrecognized: …>``."""

    @property
    def ok(self) -> bool:
        return not [issue for issue in self.issues if not issue.fixed]

    def summary(self) -> str:
        if not self.issues:
            text = f"файлов проверено: {self.checked}, ошибок нет"
            if self.restored_args:
                text += (f", восстановлено меню с аргументами: "
                         f"{self.restored_args}")
            return text
        if self.ok:
            text = (f"файлов проверено: {self.checked}, восстановлено "
                    f"двоеточий: {self.repaired}")
            if self.restored_args:
                text += f" (из них меню с аргументами: {self.restored_args})"
            return text
        broken = [issue for issue in self.issues if not issue.fixed]
        return (f"файлов проверено: {self.checked}, ошибок: {len(broken)} — "
                f"игра не запустится, пока их не исправить")

    def lines(self, project: Optional[RenpyProject] = None) -> list[str]:
        out = [self.summary()]
        if self.lossy:
            unique = sorted(set(item.split(" — ")[0].split(":")[0]
                                for item in self.lossy))
            out.append(f"  ! файлов после декомпиляции с потерями: "
                       f"{len(unique)} — {', '.join(unique[:3])}"
                       + (" …" if len(unique) > 3 else ""))
            for item in self.lossy[:5]:
                out.append(f"  ! заглушка декомпилятора: {item}")
        for issue in self.issues[:20]:
            out.append("  " + (issue.describe() if project is None
                               else f"{issue.where(project)}: "
                                    f"{issue.text.strip()}  "
                                    f"({'исправлено' if issue.fixed else '! требует правки'})"))
        return out


def _iter_game_files(project: RenpyProject) -> Iterable[Path]:
    tl_root = (project.game_dir / TL_DIRECTORY).resolve()
    for path in project.rpy_files:
        try:
            if tl_root in path.resolve().parents:
                continue
        except OSError:
            pass
        yield path


def _arguments_from_repr(body: str) -> str:
    """
    Превратить repr-список аргументов в аргументы Ren'Py.

    ``[('nvl', 'True')]`` -> ``nvl=True``; значения-строки в кавычках
    остаются строками. Пустой список -> пустая строка.
    """
    parts: list[str] = []
    for key, value in re.findall(
            r"\(\s*'(?P<key>[^']*)'\s*,\s*(?P<value>.*?)\s*,?\s*\)", body):
        key = key.strip()
        value = value.strip()
        if key in ("", "None"):
            continue
        if value.startswith("'") and value.endswith("'") and len(value) >= 2:
            inner = value[1:-1]
            # repr(bool)/repr(number) -> как есть, repr(str) -> строка в кавычках
            if re.fullmatch(r"(True|False|None|-?\d+(\.\d+)?)", inner):
                parts.append(f"{key}={inner}")
            else:
                escaped = inner.replace('"', '\\"')
                parts.append(f'{key}="{escaped}"')
        else:
            parts.append(f"{key}={value}")
    return ", ".join(parts)


def restore_argument_info(lines: list[str]) -> int:
    """
    Починить меню, у которого декомпилятор съел аргументы.

    Возвращает число исправленных строк. Меняет ``lines`` на месте.
    """
    fixed = 0
    for index, line in enumerate(lines):
        match = ARGINFO_RE.match(line.rstrip("\r\n"))
        if not match:
            continue
        ending = "\r\n" if line.endswith("\r\n") else (
            "\n" if line.endswith("\n") else "")
        arguments = ""
        for probe in lines[index + 1:index + 12]:
            found = ARGS_LINE_RE.match(probe.rstrip("\r\n"))
            if found:
                arguments = _arguments_from_repr(found.group("body"))
                break
            if probe.strip() and not probe.lstrip().startswith("#"):
                break
        statement = match.group("stmt").strip().rstrip(":")
        spec = f" ({arguments})" if arguments else ""
        lines[index] = f"{match.group('indent')}{statement}{spec}:{ending}"
        fixed += 1
    return fixed


def _needs_colon(line: str, following: Optional[str],
                 keyword: str = "") -> bool:
    """
    Похоже ли, что у строки-блока пропало двоеточие.

    ``menu``, ``label``, ``screen``, ``style`` и ``translate`` требуют
    двоеточия всегда — проверять, есть ли под ними блок с отступом, не нужно
    (именно на этой проверке раньше и проскакивала поломка: если тело блока
    съехало, строка `menu` оставалась без двоеточия навсегда).
    """
    stripped_line = line.rstrip()
    if not stripped_line or stripped_line.lstrip().startswith("#"):
        return False
    body = _TRAILING_COMMENT.sub("", stripped_line)
    if ":" in body:
        return False
    if "=" in body or '"' in body or "'" in body:
        # define/присваивания и строки — не блочные объявления
        return False

    if keyword in ALWAYS_COLON:
        return True
    if following is None:
        return False
    indent = len(line) - len(line.lstrip())
    next_indent = len(following) - len(following.lstrip())
    return next_indent > indent


def _repair_line(line: str) -> str:
    stripped = line.rstrip("\n")
    match = _TRAILING_COMMENT.search(stripped)
    if match:
        head = stripped[:match.start()].rstrip()
        return head + ":" + stripped[match.start():] + (
            "\n" if line.endswith("\n") else "")
    return stripped.rstrip() + ":" + ("\n" if line.endswith("\n") else "")


def check_project(project: Optional[RenpyProject],
                  repair: bool = True) -> SyntaxReport:
    """Найти (и при ``repair`` — исправить) строки-блоки без двоеточия."""
    report = SyntaxReport()
    if project is None:
        return report

    for path in _iter_game_files(project):
        try:
            raw = path.read_text(encoding="utf-8", errors="surrogateescape")
        except OSError:
            continue
        report.checked += 1
        lines = raw.splitlines(keepends=True)
        changed = False

        # 0. заглушки декомпилятора: `menu` без аргументов и без двоеточия
        if repair:
            restored = restore_argument_info(lines)
            if restored:
                report.restored_args += restored
                changed = True
                logger.warning("восстановлены аргументы меню: %s (%d)",
                               path.name, restored)
        # в отчёт идут только те заглушки, которые починить не удалось
        for number, line in enumerate(lines, 1):
            for match in UNRECOGNIZED_RE.finditer(line):
                report.lossy.append(
                    f"{path.name}:{number} — <unrecognized: "
                    f"{match.group('kind')}>")
        for index, line in enumerate(lines):
            stripped = line.strip()
            if not stripped:
                continue
            match = _KEYWORD_RE.match(line)
            if not match:
                continue
            following = None
            for probe in lines[index + 1:]:
                if probe.strip():
                    following = probe
                    break
            if not _needs_colon(line, following, match.group("keyword")):
                continue
            before = [l.strip() for l in lines[max(0, index - 2):index] if l.strip()]
            after = [l.strip() for l in lines[index + 1:index + 4] if l.strip()]
            context = "выше: " + " / ".join(before[-2:]) if before else ""
            if after:
                context += ("; " if context else "") + "ниже: " + " / ".join(after[:2])
            issue = SyntaxIssue(path=path, line=index + 1, text=line.strip(),
                                context=context)
            if repair:
                lines[index] = _repair_line(line)
                issue.fixed = True
                report.repaired += 1
                changed = True
                logger.warning("починена строка без двоеточия: %s:%d (%s)",
                               path.name, index + 1, stripped)
            report.issues.append(issue)
        if changed:
            try:
                target_dir = project.game_dir / TL_DIRECTORY / BACKUP_DIR
                target_dir.mkdir(parents=True, exist_ok=True)
                saved = target_dir / (path.name + ".bak")
                if not saved.exists():
                    shutil.copy2(path, saved)
            except OSError:
                pass
            path.write_text("".join(lines), encoding="utf-8",
                            errors="surrogateescape")
            # рядом лежащий .rpyc собран из сломанного файла — убираем, чтобы
            # Ren'Py перекомпилировал скрипт, а не взял старый байткод
            compiled = path.with_suffix(".rpyc")
            if compiled.is_file():
                try:
                    compiled.unlink()
                    logger.info("удалён устаревший %s (перекомпилируется)", compiled.name)
                except OSError:
                    pass

    return report


def check_dir(path: Path, repair: bool = True) -> SyntaxReport:
    """Проверить (и починить) файлы игры по пути к папке ``game``."""
    from core.scanner import scan_project

    try:
        project = scan_project(path)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Не удалось просканировать %s: %s", path, exc)
        return SyntaxReport()
    return check_project(project, repair=repair)


def summarize(report: SyntaxReport) -> str:
    """Одна строка для отчётов программы."""
    if report.repaired:
        return f"синтаксис файлов игры: восстановлено двоеточий: {report.repaired}"
    if report.issues:
        return "синтаксис файлов игры: есть строки без двоеточия"
    return ""
