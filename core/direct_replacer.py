"""
Режим прямой замены: подставляем перевод прямо в .rpy файлы игры.

Работаем не «построчно регулярками», а по точным смещениям символов,
которые парсер запомнил для каждого литерала (``TextItem.edit``). Это
сохраняет отступы, комментарии и всё остальное форматирование файла.

После правки рядом лежащий .rpyc удаляется, чтобы Ren'Py перекомпилировал
скрипт (иначе движок может взять устаревший байткод).
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from core.models import TextItem
from core.placeholder import escape_for_renpy
from core.rpy_parser import decode_string_body, make_literal
from core.scanner import RenpyProject
from core.utils import atomic_write_text, get_logger, read_text_file, remove_file

logger = get_logger(__name__)


@dataclass
class ReplaceResult:
    changed_files: list[Path] = field(default_factory=list)
    replacements: int = 0
    skipped: int = 0
    warnings: list[str] = field(default_factory=list)
    removed_rpyc: list[Path] = field(default_factory=list)

    def summary(self) -> str:
        return (f"прямая замена: {self.replacements} строк в "
                f"{len(self.changed_files)} файлах")


def _span_looks_right(content: str, start: int, end: int, edit,
                      items: list[TextItem]) -> bool:
    """
    Похоже ли, что по этим смещениям всё ещё лежит тот самый литерал.

    Смещения запоминаются при разборе файла. Если файл с тех пор правили
    (например, «Починить меню» переписал screens.rpy), смещения устаревают —
    и запись «по старым координатам» вырезала бы кусок соседнего кода. Один
    неверный байт превращается в `expected ':' not found` при запуске игры,
    поэтому проверяем кавычки и сам текст.
    """
    fragment = content[start:end]
    quote = edit.quote or '"'
    want = quote * 3 if edit.triple else quote
    if not fragment.startswith(want) or not fragment.endswith(want):
        return False
    if len(items) != 1 or edit.triple:
        return True
    body = fragment[len(want):len(fragment) - len(want)]
    try:
        decoded = decode_string_body(body, raw=edit.raw)
    except Exception:  # noqa: BLE001 - сомнительный разбор не повод падать
        return True
    return decoded == items[0].original


def _group_items(items: Iterable[TextItem]) -> dict[str, list[TextItem]]:
    by_file: dict[str, list[TextItem]] = defaultdict(list)
    for item in items:
        if item.edit is None:
            continue
        if not item.is_translated():
            continue
        by_file[item.file].append(item)
    return dict(by_file)


def replace_in_files(project: RenpyProject, items: Iterable[TextItem],
                     delete_rpyc: bool = True,
                     dry_run: bool = False) -> ReplaceResult:
    """Применяет переводы к оригинальным .rpy файлам."""
    result = ReplaceResult()
    by_file = _group_items(list(items))

    for rel_file, file_items in sorted(by_file.items()):
        path = project.game_dir / rel_file
        if not path.exists():
            result.warnings.append(f"файл не найден: {rel_file}")
            continue

        content = read_text_file(path)
        original_length = len(content)

        # группируем элементы с одинаковым span (monologue-абзацы)
        spans: dict[tuple[int, int], list[TextItem]] = defaultdict(list)
        for item in file_items:
            spans[(item.edit.start, item.edit.end)].append(item)  # type: ignore[union-attr]

        edits: list[tuple[int, int, str]] = []
        for (start, end), group in spans.items():
            group.sort(key=lambda i: i.extra.get("paragraph_index", 0))
            edit = group[0].edit
            assert edit is not None

            if len(group) > 1:
                text = "\n\n".join(
                    escape_for_renpy(i.result_text(), i.original) for i in group)
            else:
                text = escape_for_renpy(group[0].result_text(), group[0].original)

            literal = make_literal(text, quote=edit.quote, raw=edit.raw,
                                   triple=edit.triple)
            if "\n" in text and not edit.triple:
                # в обычных кавычках переводы строк экранируются внутри make_literal
                pass
            if not _span_looks_right(content, start, end, edit, group):
                result.skipped += 1
                result.warnings.append(
                    f"{rel_file}: файл изменился после разбора "
                    f"(строка {group[0].line}) — пропущено, чтобы не испортить код"
                )
                continue
            edits.append((start, end, literal))

        # применяем справа налево, чтобы смещения не «уезжали»
        new_content = content
        for start, end, literal in sorted(edits, key=lambda e: e[0], reverse=True):
            if start < 0 or end > len(new_content) or end <= start:
                result.skipped += 1
                result.warnings.append(
                    f"{rel_file}: некорректные смещения ({start}:{end}) — пропущено"
                )
                continue
            new_content = new_content[:start] + literal + new_content[end:]
            result.replacements += 1

        if new_content == content:
            continue

        if dry_run:
            result.changed_files.append(path)
            logger.info("[dry-run] Изменился бы %s", rel_file)
            continue

        atomic_write_text(path, new_content)
        result.changed_files.append(path)
        logger.info("Обновлён %s (%d строк)", rel_file, len(edits))

        if delete_rpyc:
            compiled = path.with_suffix(".rpyc")
            if remove_file(compiled, logger):
                result.removed_rpyc.append(compiled)

        if abs(len(new_content) - original_length) > max(10000, original_length):
            result.warnings.append(
                f"{rel_file}: размер файла изменился очень сильно — проверьте игру"
            )

    return result


def make_backup_copy(project: RenpyProject, rel_file: str,
                     backup_dir: Path) -> Path:
    """Кладёт копию исходного файла в каталог бэкапа (дополнительная страховка)."""
    source = project.game_dir / rel_file
    target = backup_dir / rel_file
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(source.read_bytes())
    return target
