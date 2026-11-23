#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Сборка RenPyTranslator в один исполняемый файл (PyInstaller).

Использование::

    python build.py            # собрать RenPyTranslator.exe
    python build.py --onedir   # папка вместо одного файла (стартует быстрее)
    python build.py --console  # оставить консоль (полезно для CLI-режима)
    python build.py --clean    # удалить артефакты сборки

Результат: ``dist/RenPyTranslator.exe`` (Windows) или
``dist/RenPyTranslator`` (Linux/macOS).
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
APP_NAME = "RenPyTranslator"
ENTRY = ROOT / "main.py"

HIDDEN_IMPORTS = [
    "core", "core.scanner", "core.rpa_extractor", "core.rpyc_decompiler",
    "core.rpy_parser", "core.translator", "core.placeholder", "core.tl_writer",
    "core.direct_replacer", "core.exporter", "core.importer", "core.cache",
    "core.backup", "core.pipeline", "core.models", "core.utils",
    "core.appconfig", "core.fonts", "core.image_text", "core.engine_strings",
    "core.uicheck", "core.literary", "gui", "gui_modern", "gui_images",
    "customtkinter", "PIL", "PIL.Image", "PIL.ImageDraw", "PIL.ImageFont",
    "deep_translator", "rpycdec", "unrpa", "tqdm", "docx",
    "tkinter", "tkinter.filedialog", "tkinter.messagebox", "tkinter.ttk",
]

EXCLUDES = [
    "numpy", "pandas", "matplotlib", "scipy", "IPython", "notebook",
    "PyQt5", "PySide2", "PySide6", "torch", "tensorflow",
]


def check_pyinstaller() -> bool:
    try:
        import PyInstaller  # noqa: F401

        return True
    except ImportError:
        print("[!] PyInstaller не установлен.")
        print("    Установите: pip install pyinstaller")
        return False


def build_icon_arg() -> list[str]:
    icon = ROOT / "assets" / "icon.ico"
    if icon.exists():
        return ["--icon", str(icon)]
    return []


VERSION_INFO_TEMPLATE = """\
VSVersionInfo(
  ffi=FixedFileInfo(
    filevers=(2, 0, 0, 0),
    prodvers=(2, 0, 0, 0),
    mask=0x3f,
    flags=0x0,
    OS=0x40004,
    fileType=0x1,
    subtype=0x0,
    date=(0, 0)
  ),
  kids=[
    StringFileInfo([
      StringTable(
        '041904b0',
        [StringStruct('CompanyName', 'RenPyTranslator'),
         StringStruct("FileDescription", "Русификатор визуальных новелл Ren'Py"),
         StringStruct('FileVersion', '3.0.0'),
         StringStruct('InternalName', 'RenPyTranslator'),
         StringStruct('LegalCopyright', 'Для личного использования'),
         StringStruct('OriginalFilename', 'RenPyTranslator.exe'),
         StringStruct('ProductName', 'RenPyTranslator'),
         StringStruct('ProductVersion', '3.0.0')])
    ]),
    VarFileInfo([VarStruct('Translation', [1049, 1200])])
  ]
)
"""


def build_version_file() -> list[str]:
    """Файл с версией для проводника Windows (только при сборке под Windows)."""
    if os.name != "nt":
        return []
    path = ROOT / "build" / "version_info.txt"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(VERSION_INFO_TEMPLATE, encoding="utf-8")
    return ["--version-file", str(path)]


def build_command(onefile: bool, console: bool) -> list[str]:
    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm",
        "--clean",
        "--name", APP_NAME,
        "--onefile" if onefile else "--onedir",
        "--console" if console else "--windowed",
        "--add-data", f"{ROOT / 'config.py'}{os.pathsep}.",
        "--add-data", f"{ROOT / 'assets'}{os.pathsep}assets",
        # customtkinter держит шрифты и json-темы в собственном каталоге:
        # без них .exe теряет оформление и выглядит как голый tkinter
        "--collect-all", "customtkinter",
        *build_icon_arg(),
        *build_version_file(),
    ]

    for module in HIDDEN_IMPORTS:
        cmd.extend(["--hidden-import", module])
    for module in EXCLUDES:
        cmd.extend(["--exclude-module", module])

    cmd.append(str(ENTRY))
    return cmd


def clean() -> None:
    for name in ("build", "dist", f"{APP_NAME}.spec"):
        target = ROOT / name
        if target.is_dir():
            shutil.rmtree(target, ignore_errors=True)
            print(f"удалено: {target}")
        elif target.exists():
            target.unlink()
            print(f"удалено: {target}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Сборка RenPyTranslator в .exe")
    parser.add_argument("--onedir", action="store_true",
                        help="собрать папку вместо одного файла")
    parser.add_argument("--console", action="store_true",
                        help="оставить консольное окно (для CLI)")
    parser.add_argument("--clean", action="store_true", help="удалить артефакты сборки")
    args = parser.parse_args()

    if args.clean:
        clean()
        return 0

    if not check_pyinstaller():
        return 1

    if not ENTRY.exists():
        print(f"[!] Не найден {ENTRY}")
        return 1

    cmd = build_command(onefile=not args.onedir, console=args.console)
    print("Запуск сборки:\n  " + " ".join(cmd) + "\n")
    result = subprocess.run(cmd, cwd=str(ROOT))
    if result.returncode != 0:
        print("\n[!] Сборка завершилась с ошибкой.")
        return result.returncode

    exe_name = APP_NAME + (".exe" if os.name == "nt" else "")
    target = ROOT / "dist" / exe_name
    print("\nГотово!")
    if target.exists():
        size_mb = target.stat().st_size / (1024 * 1024)
        print(f"  {target}  ({size_mb:.1f} МБ)")
    else:
        print(f"  смотрите результат в {ROOT / 'dist'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
