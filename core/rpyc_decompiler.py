"""
Декомпиляция скомпилированных скриптов Ren'Py (.rpyc -> .rpy).

Порядок попыток:

1. Python-пакет ``rpycdec`` (``pip install rpycdec``) — самый удобный путь.
2. Модуль/скрипт ``unrpyc.py`` (CensoredUsername/unrpyc), если указан путь
   через ``--unrpyc`` или он лежит рядом с программой.
3. «Хак с un.rpyc»: файл кладётся в ``game/``, и игра при запуске сама
   декомпилирует всё, что рядом. Мы можем установить его автоматически
   (``install_unrpyc_hook``).

Если .rpyc не декомпилируется — программа предупреждает и продолжает работу
(остальные файлы переводятся нормально).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional

from core.scanner import RenpyProject
from core.utils import get_logger

logger = get_logger(__name__)

RPYC_SUFFIXES = (".rpyc", ".rpymc")
UNRPYC_HOOK_NAME = "un.rpyc"


@dataclass
class DecompileResult:
    source: Path
    output: Optional[Path] = None
    ok: bool = False
    method: str = ""
    error: str = ""
    lossy: bool = False
    """Декомпилятор оставил заглушки (`# <unrecognized: …>`): файл неполный."""
    note: str = ""

    def summary(self) -> str:
        if self.ok:
            return f"{self.source.name} -> {self.output.name if self.output else '?'}"
        return f"{self.source.name}: {self.error}"


@dataclass
class DecompileReport:
    results: list[DecompileResult] = field(default_factory=list)
    unavailable: Optional[str] = None

    @property
    def ok_count(self) -> int:
        return sum(1 for r in self.results if r.ok)

    @property
    def fail_count(self) -> int:
        return sum(1 for r in self.results if not r.ok)

    def summary(self) -> str:
        if self.unavailable:
            return f"декомпиляция недоступна: {self.unavailable}"
        return f"декомпилировано {self.ok_count} из {len(self.results)} .rpyc"


# ---------------------------------------------------------------------------
# Определение доступных инструментов
# ---------------------------------------------------------------------------
def tool_status(unrpyc_path: Optional[Path] = None) -> dict[str, str]:
    """Что доступно для декомпиляции."""
    status: dict[str, str] = {}

    try:
        import rpycdec  # noqa: F401

        status["rpycdec"] = "доступен (python-пакет)"
    except ImportError:
        status["rpycdec"] = "не установлен (pip install rpycdec)"

    path = unrpyc_path or find_unrpyc()
    status["unrpyc"] = (
        f"найден: {path}" if path else "не найден (укажите --unrpyc путь/к/unrpyc.py)"
    )
    return status


def find_unrpyc() -> Optional[Path]:
    """Ищет unrpyc.py рядом с программой и в текущем каталоге."""
    candidates = [
        Path(__file__).resolve().parent.parent / "unrpyc.py",
        Path.cwd() / "unrpyc.py",
        Path.cwd() / "tools" / "unrpyc.py",
    ]
    for cand in candidates:
        if cand.is_file():
            return cand
    return None


# ---------------------------------------------------------------------------
# Декомпиляция
# ---------------------------------------------------------------------------
def decompile_with_rpycdec(source: Path, output: Path) -> DecompileResult:
    result = DecompileResult(source=source, method="rpycdec")
    try:
        from rpycdec import decompile_file  # type: ignore
    except ImportError:
        result.error = "пакет rpycdec не установлен"
        return result

    try:
        decompile_file(str(source), str(output))
    except Exception as exc:  # noqa: BLE001 - rpycdec кидает разные ошибки
        result.error = f"rpycdec: {type(exc).__name__}: {exc}"
        return result

    if output.exists() and output.stat().st_size > 0:
        result.ok = True
        result.output = output
    else:
        result.error = "rpycdec не создал файл"
    return result


def decompile_with_unrpyc(source: Path, output: Path,
                          unrpyc_path: Optional[Path] = None) -> DecompileResult:
    result = DecompileResult(source=source, method="unrpyc")
    script = unrpyc_path or find_unrpyc()
    if not script:
        result.error = "unrpyc.py не найден"
        return result

    cmd = [sys.executable, str(script), "-c", str(source), str(output)]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    except (OSError, subprocess.SubprocessError) as exc:
        result.error = f"не удалось запустить unrpyc: {exc}"
        return result

    if proc.returncode != 0:
        result.error = f"unrpyc: {(proc.stderr or proc.stdout)[-300:]}"
        return result

    if output.exists() and output.stat().st_size > 0:
        result.ok = True
        result.output = output
    else:
        result.error = "unrpyc не создал файл"
    return result


def decompile_file(source: Path, output: Optional[Path] = None,
                   unrpyc_path: Optional[Path] = None) -> DecompileResult:
    """Декомпилирует один .rpyc, перебирая доступные способы."""
    output = output or source.with_suffix(".rpy")
    errors: list[str] = []

    for func in (decompile_with_rpycdec,):
        res = func(source, output)
        if res.ok:
            logger.debug("Декомпилирован %s (%s)", source.name, res.method)
            return res
        errors.append(res.error)

    res = decompile_with_unrpyc(source, output, unrpyc_path)
    if res.ok:
        logger.debug("Декомпилирован %s (unrpyc)", source.name)
        return res
    errors.append(res.error)

    res = DecompileResult(source=source, method="failed",
                          error="; ".join(e for e in errors if e))
    logger.warning("Не удалось декомпилировать %s: %s", source, res.error)
    return res


def _repair_arguments(path: Path) -> int:
    """Восстановить `menu (nvl=True):`, съеденное декомпилятором."""
    from core.syntax_guard import restore_argument_info

    try:
        raw = path.read_text(encoding="utf-8", errors="surrogateescape")
    except OSError:
        return 0
    lines = raw.splitlines(keepends=True)
    fixed = restore_argument_info(lines)
    if fixed:
        path.write_text("".join(lines), encoding="utf-8",
                        errors="surrogateescape")
        logger.info("в %s восстановлены аргументы меню: %d", path.name, fixed)
    return fixed


def _lossy_markers(path: Path) -> list[str]:
    """Заглушки декомпилятора, которые остались в файле."""
    from core.syntax_guard import UNRECOGNIZED_RE

    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    return [m.group("kind") for m in UNRECOGNIZED_RE.finditer(text)]


def decompile_all(project: RenpyProject, unrpyc_path: Optional[Path] = None,
                  overwrite: bool = False,
                  delete_compiled: bool = True) -> DecompileReport:
    """
    Декомпилирует все .rpyc проекта, для которых нет .rpy.

    `delete_compiled` — удалить .rpyc после успешной декомпиляции, чтобы
    движок гарантированно перекомпилировал .rpy (важно для режима replace).
    """
    report = DecompileReport()

    targets: list[Path] = []
    for rpyc in project.rpyc_files:
        rpy = rpyc.with_suffix(".rpy")
        if rpy.exists() and not overwrite:
            continue
        targets.append(rpyc)

    if not targets:
        logger.info("Декомпиляция не требуется: все .rpyc уже имеют .rpy")
        return report

    status = tool_status(unrpyc_path)
    if status.get("rpycdec", "").startswith("не установлен") and \
            status.get("unrpyc", "").startswith("не найден"):
        report.unavailable = (
            "нет ни пакета rpycdec, ни скрипта unrpyc.py. "
            "Установите: pip install rpycdec  (или скачайте unrpyc.py и "
            "укажите --unrpyc)"
        )
        logger.warning(report.unavailable)
        return report

    for source in targets:
        result = decompile_file(source, source.with_suffix(".rpy"), unrpyc_path)
        report.results.append(result)
        if result.ok and result.output is not None:
            fixed = _repair_arguments(result.output)
            if fixed:
                result.note = f"восстановлены аргументы меню: {fixed}"
            markers = _lossy_markers(result.output)
            # `ArgumentInfo` мы уже починили; остальные заглушки означают, что
            # часть кода (например, экраны) декомпилятор не восстановил.
            if [kind for kind in markers if kind != "ArgumentInfo"]:
                sideways = result.output.with_suffix(".rpy.rt_lossy")
                try:
                    result.output.replace(sideways)
                except OSError:
                    pass
                result.lossy = True
                result.ok = False
                kinds = sorted(set(markers))
                result.error = ("декомпиляция неполная (потеряно: "
                                + ", ".join(kinds) + "); исходный .rpyc "
                                "оставлен, файл отложен как " + sideways.name)
                logger.warning("%s: %s", source.name, result.error)
                continue
        if result.ok and delete_compiled:
            try:
                source.unlink()
                logger.debug("Удалён скомпилированный %s", source.name)
            except OSError as exc:
                logger.debug("Не удалось удалить %s: %s", source, exc)

    logger.info("Декомпиляция: %s", report.summary())
    return report


# ---------------------------------------------------------------------------
# «Хак» с un.rpyc
# ---------------------------------------------------------------------------
def install_unrpyc_hook(game_dir: Path, unrpyc_binary: Optional[Path] = None) -> Optional[Path]:
    """
    Кладёт ``un.rpyc`` в game/.

    После этого достаточно запустить игру один раз: Ren'Py выполнит файл и
    декомпилирует все соседние .rpyc. Требует готовый ``un.rpyc`` из
    релизов unrpyc (мы не можем собрать его без Ren'Py SDK).
    """
    if unrpyc_binary is None:
        logger.info(
            "un.rpyc не указан. Скачайте un.rpyc из релизов "
            "https://github.com/CensoredUsername/unrpyc/releases и положите "
            "в папку game/ вручную, затем запустите игру один раз."
        )
        return None

    target = game_dir / UNRPYC_HOOK_NAME
    try:
        shutil.copy2(unrpyc_binary, target)
        logger.info("Установлен %s -> %s", unrpyc_binary, target)
        return target
    except OSError as exc:
        logger.error("Не удалось установить un.rpyc: %s", exc)
        return None


def remove_unrpyc_hook(game_dir: Path) -> bool:
    """Убирает un.rpyc (после использования он не нужен)."""
    target = game_dir / UNRPYC_HOOK_NAME
    if target.exists():
        try:
            target.unlink()
            return True
        except OSError:
            return False
    return False
