"""
Экспорт извлечённого текста для ручного / AI-перевода.

Поддерживаемые форматы:

* ``json`` — полный дамп с контекстом (как в ТЗ);
* ``txt``  — человекочитаемый файл ``[#id] (Персонаж) / EN: / RU:``;
* ``docx`` — то же самое, но документом Word (если установлен python-docx).

Формат TXT специально сделан удобным для передачи AI-агенту: достаточно
заполнить строки ``RU:`` и вернуть файл обратно через ``--import``.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional

from config import APP_NAME, VERSION
from core.models import (
    KIND_DIALOGUE,
    KIND_EXTEND,
    KIND_MENU,
    KIND_NAME,
    KIND_NARRATION,
    KIND_UI,
    ParsedProject,
    TextItem,
)
from core.scanner import RenpyProject
from core.utils import atomic_write_json, atomic_write_text, get_logger

logger = get_logger(__name__)

#: Сколько строк кладём в один файл при разбиении на части.
SPLIT_DEFAULT = 5000


#: Предел DOCX (можно поднять через --docx-limit).
DOCX_LIMIT = 15000

#: Как часто сообщать о прогрессе (в строках).
PROGRESS_EVERY = 5000

KIND_TITLES = {
    KIND_DIALOGUE: "реплика",
    KIND_NARRATION: "нарратив",
    KIND_EXTEND: "продолжение",
    KIND_MENU: "выбор",
    KIND_UI: "интерфейс",
    KIND_NAME: "имя",
}

TXT_HEADER = """\
# {app} {version} — текст визуальной новеллы для литературного перевода
# Игра: {game}
# Файлов: {files} | Строк: {count} | Уникальных: {unique}
# Экспортировано: {date}
#
# ЧТО ДЕЛАТЬ АГЕНТУ (переводчику):
#   Заполните поле "RU:" литературным переводом на русский язык.
#   В поле "MT:" лежит черновой машинный перевод — это основа, а не образец:
#   его можно и нужно править. Если "RU:" оставить пустым, программа возьмёт "MT:".
#
# ЧТО НЕЛЬЗЯ МЕНЯТЬ:
#   * строки "EN:", номера [#файл:строка] и заголовки "=== ... ===";
#   * служебные вставки [переменная], {{тег}}, %s, \\n — переносите как есть;
#   * комментарии "#" — в них контекст (кто говорит, какая сцена, что было до
#     и после), программа их читает и по ним тоже сопоставляет строки.
#
# ЧТО ЕСТЬ В КОНТЕКСТЕ КАЖДОЙ СТРОКИ:
#   [#файл:строка] (тип · персонаж · сцена: метка)
#     тип: реплика / нарратив / выбор / интерфейс / имя / продолжение
#     персонаж: имя того, кто говорит (для нарратива — рассказчик)
#     сцена: метка Ren'Py, в которой находится строка (начало главы)
#   # до:    предыдущая реплика — чтобы понимать, что уже сказано
#   # после: следующая реплика — чтобы понимать, к чему ведёт
#   # до: / # после: — до двух реплик сцены с каждой стороны (кто и что сказал)
#   # экран: / # исходник: — для интерфейса: экран и строка, откуда взята надпись
#
# ВЕРНУТЬ: этот же файл (.txt) обратно в программу —
#   «2. Импорт правок в игру» (или --import файл.txt --mode tl).
#

"""
@dataclass
class ExportResult:
    path: Path
    format: str
    count: int = 0
    warnings: list[str] = field(default_factory=list)
    #: дополнительные файлы (когда экспорт разбит на части)
    parts: list[Path] = field(default_factory=list)

    def summary(self) -> str:
        size = ""
        try:
            size = f", {self.path.stat().st_size / 1048576:.1f} МБ"
        except OSError:
            pass
        extra = f" (частей: {len(self.parts)})" if self.parts else ""
        return (f"экспортировано {self.count} строк -> {self.path.name} "
                f"({self.format}{size}){extra}")


# ---------------------------------------------------------------------------
def _character_label(item: TextItem) -> str:
    if item.kind == KIND_UI:
        return "UI"
    if item.kind == KIND_MENU:
        return "Выбор"
    if item.kind == KIND_NAME:
        return f"имя:{item.character}" if item.character else "Имя"
    if item.kind == KIND_NARRATION:
        return "Narrator"
    if item.kind == KIND_EXTEND:
        return "extend"
    return item.character or "?"


def _sorted_items(items: Iterable[TextItem]) -> list[TextItem]:
    return sorted(items, key=lambda i: (i.file.lower(), i.line, i.uid))


#: Сколько соседних реплик показывать в блоке «строки рядом».
NEIGHBOURS_SIDE = 2
#: Максимальная длина строки контекста в TXT.
CTX_CLIP = 150


def _clip(text: str, limit: int = CTX_CLIP) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _speaker(item: TextItem, names: dict[str, str]) -> str:
    """Человеческое имя говорящего вместо тега."""
    if item.kind in (KIND_NARRATION, KIND_EXTEND):
        return "рассказчик"
    if item.kind == KIND_UI:
        return "интерфейс"
    if item.kind == KIND_MENU:
        return "выбор игрока"
    tag = (item.character or "").strip()
    if not tag:
        return "?"
    return names.get(tag, tag)


def _dialogue_items(items: list[TextItem]) -> list[TextItem]:
    """Только произносимые строки — из них собираем контекст беседы."""
    return [i for i in items
            if i.kind in (KIND_DIALOGUE, KIND_NARRATION, KIND_MENU, KIND_EXTEND)]


def _neighbours(items: list[TextItem]) -> dict[str, dict[str, list]]:
    """Что было до и после: в пределах файла и сцены (label), по 2 реплики."""
    result: dict[str, dict[str, list]] = {}
    groups: dict[tuple[str, str], list[TextItem]] = {}
    for item in items:
        if item.kind in (KIND_DIALOGUE, KIND_NARRATION, KIND_MENU, KIND_EXTEND):
            groups.setdefault((item.file, item.label or ""), []).append(item)

    for group in groups.values():
        for index, item in enumerate(group):
            before = group[max(0, index - NEIGHBOURS_SIDE):index]
            after = group[index + 1:index + 1 + NEIGHBOURS_SIDE]
            result[item.uid] = {
                "before": [(_speaker(i, {}), _clip(i.original)) for i in before],
                "after": [(_speaker(i, {}), _clip(i.original)) for i in after],
            }
    return result


def _file_lines(project: RenpyProject) -> dict[str, list[str]]:
    """Кэш содержимого .rpy файлов (нужен для имён экранов)."""
    cache: dict[str, list[str]] = getattr(project, "_rpt_lines_cache", {})
    project._rpt_lines_cache = cache  # type: ignore[attr-defined]
    return cache


SCREEN_RE = re.compile(r"^\s*screen\s+([A-Za-z_][\w]*)")
LABEL_RE = re.compile(r"^\s*label\s+([A-Za-z_][\w.]*)")


def _enclosing(project: Optional[RenpyProject], relative: str, line: int,
               pattern: "re.Pattern[str]") -> str:
    """Имя ближайшего screen/label выше указанной строки файла."""
    if project is None or not relative or line <= 0:
        return ""
    cache = _file_lines(project)
    if relative not in cache:
        path = project.game_dir / relative
        try:
            cache[relative] = path.read_text(encoding="utf-8",
                                             errors="replace").splitlines()
        except OSError:
            cache[relative] = []
    lines = cache[relative]
    for index in range(min(line, len(lines)) - 1, -1, -1):
        match = pattern.match(lines[index])
        if match:
            return match.group(1)
    return ""


def _character_names(items: list[TextItem]) -> dict[str, str]:
    """Тег персонажа -> отображаемое имя (из define Character("..."))."""
    names: dict[str, str] = {}
    for item in items:
        if item.kind == KIND_NAME and item.character:
            names.setdefault(item.character, item.original)
    return names


def _usage_counts(items: list[TextItem]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in items:
        if item.kind in (KIND_DIALOGUE, KIND_MENU) and item.character:
            counts[item.character] = counts.get(item.character, 0) + 1
    return counts


def _reference_block(items: list[TextItem]) -> str:
    """Справка в начале файла: персонажи и сцены."""
    names = _character_names(items)
    usage = _usage_counts(items)
    lines: list[str] = []

    if names:
        lines.append("# ПЕРСОНАЖИ (тег = имя в игре; сколько реплик):")
        for tag, name in sorted(names.items(), key=lambda kv: -usage.get(kv[0], 0)):
            lines.append(f"#   {tag:>10} = {name}  (реплик: {usage.get(tag, 0)})")
        lines.append("#")

    scenes: dict[str, int] = {}
    for item in items:
        if item.label:
            scenes[item.label] = scenes.get(item.label, 0) + 1
    if scenes:
        lines.append("# СЦЕНЫ (метка Ren'Py; строк):")
        for label, count in list(scenes.items())[:400]:
            lines.append(f"#   {label}  — {count}")
        lines.append("#")

    return "\n".join(lines) + ("\n" if lines else "")


# ---------------------------------------------------------------------------
# JSON
# ---------------------------------------------------------------------------
def export_json(project: RenpyProject, parsed: ParsedProject, output: Path,
                include_context: bool = True,
                translations: Optional[dict[str, object]] = None,
                progress=None) -> ExportResult:
    """
    JSON для машинной обработки и импорта.

    Пишется **потоком**: строки дописываются по одной, поэтому даже проект
    на 100 000 реплик не требует держать в памяти готовый JSON (раньше
    собирался один огромный словарь и строка — на больших новеллах это
    и приводило к зависанию).
    """
    items = _sorted_items(parsed.items)
    names = _character_names(items)
    near = _neighbours(items)

    output.parent.mkdir(parents=True, exist_ok=True)
    tmp = output.with_suffix(output.suffix + ".part")
    count = 0
    with tmp.open("w", encoding="utf-8", newline="\n") as handle:
        meta = {
            "game_name": project.name,
            "game_path": str(project.game_dir),
            "app": APP_NAME,
            "version": VERSION,
            "target_language": "russian",
            "total_strings": len(items),
            "unique_strings": len({i.original for i in items}),
            "exported_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "source_files": [str(name) for name in parsed.source_files],
            "kinds": {
                k: sum(1 for i in items if i.kind == k)
                for k in (KIND_DIALOGUE, KIND_NARRATION, KIND_EXTEND,
                          KIND_MENU, KIND_UI, KIND_NAME)
            },
        }
        handle.write('{\n  "meta": ')
        json.dump(meta, handle, ensure_ascii=False, indent=2)
        handle.write(',\n  "strings": [\n')
        first = True
        for item in items:
            entry = {
                "id": item.uid,
                "file": item.file,
                "line": item.line,
                "kind": item.kind,
                "label": item.label,
                "character": item.character,
                "speaker": _speaker(item, names),
                "original": item.original,
                "translation": item.translation,
                "block_id": item.say.block_id if item.say else None,
            }
            info = near.get(item.uid)
            if info:
                entry["before"] = [{"speaker": w, "text": t}
                                   for w, t in info.get("before", [])]
                entry["after"] = [{"speaker": w, "text": t}
                                  for w, t in info.get("after", [])]
            if include_context:
                entry["context_before"] = item.context_before
                entry["context_after"] = item.context_after
            if not first:
                handle.write(",\n")
            first = False
            handle.write("    " + json.dumps(entry, ensure_ascii=False))
            count += 1
            if progress is not None and count % PROGRESS_EVERY == 0:
                progress(count, len(items))
        handle.write("\n  ]\n}\n")
    tmp.replace(output)
    logger.info("JSON-экспорт: %s (%d строк)", output, count)
    return ExportResult(output, "json", count)


# ---------------------------------------------------------------------------
# TXT
# ---------------------------------------------------------------------------
def set_docx_limit(value: int) -> None:
    """Поднять/опустить предел DOCX (CLI: ``--docx-limit``)."""
    global DOCX_LIMIT
    DOCX_LIMIT = max(100, int(value))


def _txt_body_lines(items, project, names, near, machine, include_context):
    """Генератор строк для TXT: пишем по одной, ничего не копим в памяти."""
    current_file: Optional[str] = None
    for item in items:
        if item.file != current_file:
            current_file = item.file
            yield ""
            yield f"=== {item.file} | label: {item.label or '-'} ==="
            yield ""

        if item.kind == KIND_UI:
            screen = _enclosing(project, item.file, item.line, SCREEN_RE)
            role = "интерфейс"
            yield (f"[#{item.uid}] ({role}"
                   + (f" · экран: {screen}" if screen else "") + ")")
            if include_context and item.context_before:
                yield f"# исходник: {_clip(item.context_before)}"
        else:
            role = KIND_TITLES.get(item.kind, item.kind)
            head = f"[#{item.uid}] ({role} · {_speaker(item, names)}"
            if item.label:
                head += f" · сцена: {item.label}"
            head += ")"
            yield head

            info = near.get(item.uid) or {}
            if item.kind == KIND_NAME and item.character:
                yield (f"# имя персонажа (тег {item.character}); "
                       f"в репликах стоит как «{item.character}»")
            if include_context:
                for who, text in info.get("before", [])[-NEIGHBOURS_SIDE:]:
                    yield f"# до: {names.get(who, who)}: {text}"
                for who, text in info.get("after", [])[:NEIGHBOURS_SIDE]:
                    yield f"# после: {names.get(who, who)}: {text}"

        yield f"EN: {item.original}"
        if item.translation and item.translation.strip():
            mt = item.translation.strip("\n")
        else:
            mt = (machine.get(item.original) or "").strip("\n")
        if mt and mt.strip() != item.original.strip():
            for index, line in enumerate(mt.split("\n")):
                yield f"MT: {line}" if index == 0 else f"    {line}"
        yield "RU: "
        yield ""


def export_txt(project: RenpyProject, parsed: ParsedProject, output: Path,
               include_context: bool = True,
               translations: Optional[dict[str, str]] = None,
               split: int = 0,
               progress=None) -> ExportResult:
    """
    TXT для человека и AI-агента: контекст, машинный черновик, поле RU:.

    Пишется потоком (по строке), поэтому 100 000+ реплик не съедают память.
    При ``split`` файл режется на части по N строк — агенту проще работать
    с куском, чем с текстом на 40 МБ.
    """
    items = _sorted_items(parsed.items)
    unique = len({i.original for i in items})
    machine = dict(translations or {})
    names = _character_names(items)
    near = _neighbours(items)

    header = TXT_HEADER.format(
        app=APP_NAME,
        version=VERSION,
        game=project.name,
        files=len(parsed.source_files),
        count=len(items),
        unique=unique,
        date=time.strftime("%Y-%m-%d %H:%M"),
    )
    reference = _reference_block(items)

    output.parent.mkdir(parents=True, exist_ok=True)
    parts: list[Path] = []
    handle = tmp = None
    written = 0
    in_part = 0
    part_index = 1

    def open_part(path: Path) -> "object":
        path.parent.mkdir(parents=True, exist_ok=True)
        stream = path.with_suffix(path.suffix + ".part").open(
            "w", encoding="utf-8", newline="\n")
        stream.write(header)
        if reference:
            stream.write(reference)
        return stream

    def close_part(stream, path: Path) -> None:
        stream.flush()
        stream.close()
        path.with_suffix(path.suffix + ".part").replace(path)

    if split and len(items) > split:
        first_path = output.with_name(f"{output.stem}-part01{output.suffix}")
        parts.append(first_path)
        handle = open_part(first_path)
        current_path = first_path
    else:
        current_path = output
        handle = open_part(current_path)

    try:
        for line in _txt_body_lines(items, project, names, near, machine,
                                    include_context):
            handle.write(line + "\n")
            if line.startswith("RU: "):
                written += 1
                in_part += 1
                if progress is not None and written % PROGRESS_EVERY == 0:
                    progress(written, len(items))
                # новую часть не открываем, если это последняя строка —
                # иначе в конце остаётся пустая часть из одной шапки
                if split and in_part >= split and written < len(items):
                    close_part(handle, current_path)
                    part_index += 1
                    current_path = output.with_name(
                        f"{output.stem}-part{part_index:02d}{output.suffix}")
                    parts.append(current_path)
                    handle = open_part(current_path)
                    in_part = 0
    finally:
        if handle is not None:
            close_part(handle, current_path)

    result_path = parts[0] if parts else output
    if parts:
        logger.info("TXT-экспорт: %s (частей: %d, строк: %d)",
                    result_path.name, len(parts), written)
    else:
        logger.info("TXT-экспорт: %s (%d строк)", result_path, written)
    return ExportResult(result_path, "txt", written, parts=parts)


def export_docx(project: RenpyProject, parsed: ParsedProject, output: Path,
                include_context: bool = True,
                translations: Optional[dict[str, str]] = None) -> ExportResult:
    """Word-версия того же файла: контекст, черновик MT:, поле RU:."""
    try:
        from docx import Document  # type: ignore
        from docx.shared import Pt  # type: ignore
    except ImportError:
        result = ExportResult(output, "docx", 0,
                              ["python-docx не установлен — используйте --format json/txt"])
        logger.warning(result.warnings[0])
        return result

    items = _sorted_items(parsed.items)
    if len(items) > DOCX_LIMIT:
        message = (f"DOCX не собран: строк {len(items)}, а предел {DOCX_LIMIT}. "
                   f"Word-документ такого объёма держит всё в памяти — "
                   f"программа на нём и падала. Используйте TXT (он умеет "
                   f"разбивать на части). Можно поднять предел вручную: "
                   f"--format docx --docx-limit N")
        logger.warning(message)
        return ExportResult(output, "docx", 0, [message])

    machine = dict(translations or {})
    names = _character_names(items)
    near = _neighbours(items)

    def draft(item: TextItem) -> str:
        if item.translation and item.translation.strip():
            return item.translation.strip("\n")
        return (machine.get(item.original) or "").strip("\n")

    doc = Document()
    doc.core_properties.title = f"{project.name} — текст для перевода"
    doc.core_properties.author = f"{APP_NAME} {VERSION}"

    doc.add_heading(f"{project.name} — текст для литературного перевода", level=0)
    for line in ("Строк: {}. Заполните поле RU: переводом (MT: — черновик машины, "
                 "его можно править).".format(len(items)),
                 "Не меняйте номера [#...], строки EN: и служебные вставки: "
                 "[переменная], {тег}, %s, \\n.",
                 "Верните этот файл в программу: «2. Импорт правок в игру»."):
        doc.add_paragraph(line)
    reference = _reference_block(items)
    if reference:
        doc.add_paragraph(reference.replace("#", "").replace("\n", "\n"))

    current_file: Optional[str] = None
    for item in items:
        if item.file != current_file:
            current_file = item.file
            doc.add_heading(f"{item.file} (label: {item.label or '-'})", level=1)

        role = KIND_TITLES.get(item.kind, item.kind)
        header = doc.add_paragraph()
        run = header.add_run(f"[#{item.uid}] ({role} · {_speaker(item, names)})")
        run.bold = True
        run.font.size = Pt(9)

        info = near.get(item.uid) or {}
        if include_context and item.kind != KIND_UI:
            for who, text in info.get("before", []):
                ctx = doc.add_paragraph()
                ctx_run = ctx.add_run(f"до: {who}: {text}")
                ctx_run.italic = True
                ctx_run.font.size = Pt(8)
            for who, text in info.get("after", []):
                ctx = doc.add_paragraph()
                ctx_run = ctx.add_run(f"после: {who}: {text}")
                ctx_run.italic = True
                ctx_run.font.size = Pt(8)

        doc.add_paragraph(f"EN: {item.original}")
        mt = draft(item)
        if mt and mt.strip() != item.original.strip():
            doc.add_paragraph(f"MT: {mt}")
        doc.add_paragraph("RU: ")

    doc.save(str(output))
    logger.info("DOCX-экспорт: %s", output)
    return ExportResult(output, "docx", len(items))


# ---------------------------------------------------------------------------
def export(project: RenpyProject, parsed: ParsedProject, fmt: str,
           output: Optional[Path] = None, include_context: bool = True,
           translations: Optional[dict[str, str]] = None,
           split: int = 0) -> ExportResult:
    """Универсальная точка входа."""
    fmt = fmt.lower()
    if output is None:
        suffix = {"json": ".json", "txt": ".txt", "docx": ".docx"}.get(fmt, ".json")
        stamp = time.strftime("%Y%m%d_%H%M%S")
        output = project.project_root / f"renpy_translator_export_{stamp}{suffix}"

    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)

    if fmt == "json":
        return export_json(project, parsed, output, include_context)
    if fmt == "txt":
        return export_txt(project, parsed, output, include_context,
                          translations, split=split)
    if fmt == "docx":
        return export_docx(project, parsed, output, include_context, translations)

    raise ValueError(f"Неизвестный формат экспорта: {fmt} (json/txt/docx)")


def export_both(project: RenpyProject, parsed: ParsedProject, base_path: Path,
                include_context: bool = True,
                translations: Optional[dict[str, str]] = None,
                split: int = 0,
                with_json: bool = False,
                with_docx: bool = False,
                progress=None) -> list[ExportResult]:
    """
    Основной экспорт для агента: **TXT** (по желанию — разбитый на части).

    JSON и DOCX больше не делаются «за компанию»: на новелле в 100 000+
    реплик именно они превращали экспорт в зависание. Их можно включить
    явными флагами ``with_json`` / ``with_docx``.
    """
    results: list[ExportResult] = [
        export_txt(project, parsed, base_path.with_suffix(".txt"),
                   include_context, translations, split=split,
                   progress=progress),
    ]
    if with_json:
        results.append(export_json(project, parsed,
                                   base_path.with_suffix(".json"),
                                   include_context, progress=progress))
    if with_docx:
        results.append(export_docx(project, parsed,
                                   base_path.with_suffix(".docx"),
                                   include_context, translations))
    return results
