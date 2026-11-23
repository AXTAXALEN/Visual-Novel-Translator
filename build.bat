@echo off
rem Сборка RenPyTranslator.exe (Windows). Двойной клик или: build.bat
chcp 65001 >nul
where python >nul 2>nul
if errorlevel 1 (
    echo [!] Python не найден в PATH. Установите Python 3.10+ и повторите.
    pause
    exit /b 1
)
python -m pip install --upgrade pyinstaller
python build.py %*
if errorlevel 1 (
    echo.
    echo [!] Сборка завершилась с ошибкой.
    pause
    exit /b 1
)
echo.
echo Готово: dist\RenPyTranslator.exe
pause
