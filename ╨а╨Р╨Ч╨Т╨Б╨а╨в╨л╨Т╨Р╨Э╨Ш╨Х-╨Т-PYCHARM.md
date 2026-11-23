# Развёртывание в PyCharm и коммит в удалённый репозиторий

Актуальная версия: **3.7.6**. В архиве лежат только исходники — без `.exe`,
кэшей и журналов (их и не надо коммитить, они в `.gitignore`).

## 1. Распаковать и открыть

1. Распакуйте архив в удобное место, например `D:\Projects\renpy_translator`.
2. PyCharm → **File → Open** → выберите папку `renpy_translator` (ту, где лежит
   `main.py`), → **OK** → *Trust Project*.
3. Проверьте, что сверху в дереве видны `main.py`, `config.py`, папки `core/`,
   `tests/`, `assets/`, `docs/`.

## 2. Настроить интерпретатор

1. **File → Settings → Project: renpy_translator → Python Interpreter**.
2. **Add Interpreter → Add Local Interpreter → Virtualenv Environment → New**:
   * Base interpreter: Python **3.10–3.13** (любой из этого диапазона);
   * Location: `<папка проекта>\.venv` (попадает в `.gitignore`).
3. Откройте терминал PyCharm (внизу: **Terminal**) — он уже активирует `.venv` —
   и поставьте зависимости:

```bash
pip install -r requirements.txt
```

Минимум для запуска — только стандартная библиотека + `tkinter`.
Всё остальное (`customtkinter`, `deep-translator`, `rpycdec`, `pillow`,
`python-docx`, `tqdm`) улучшает вид и охват, но программа умеет работать и без них.

> **Linux:** если `import tkinter` падает — `sudo apt install python3-tk`.
> **Windows:** tkinter идёт в комплекте с Python, ничего ставить не нужно.

## 3. Запуск

Создайте конфигурацию запуска: **Run → Edit Configurations → + → Python**

| Поле | Значение |
|---|---|
| Name | `RenPyTranslator (GUI)` |
| Script path | `<папка проекта>\main.py` |
| Parameters | (пусто) |
| Working directory | `<папка проекта>` |

Запуск без параметров открывает окно программы. CLI-пример — в конфигурации
с параметрами `Parameters`:

```
--game "C:/Visual Novels/AfterClass-Itch-v0.13.30i-a01-pc/game" --engine google --workers 2 --delay 1.5 --set-default-language
```

Полезные флаги: `--breakdown` (что переводим), `--doctor` (проверка окружения),
`--prepare` (распаковать `.rpa` / декомпилировать `.rpyc`), `--version`.

## 4. Тесты

231 тест, зависимости не нужны (все сетевые движки подменяются заглушками):

```bash
python tests/run_tests.py        # или: python tests/run_tests.py -v
```

В PyCharm: правый клик по `tests/run_tests.py` → **Run 'run_tests'**.
Ожидаемый результат: `OK (skipped=3)`, около 15 секунд.

## 5. Git: репозиторий и первый коммит

Вариант А — всё из терминала PyCharm:

```bash
git init -b main
git add .
git commit -m "RenPyTranslator 3.7.6: устойчивый перевод при лимитах сервиса"
git remote add origin https://github.com/<ваш-аккаунт>/<репозиторий>.git
git push -u origin main
```

Вариант Б — через интерфейс: **VCS → Enable Version Control Integration → Git**,
затем **Git → Add**, **Git → Commit** (галочка *Commit and Push…*),
на первом пуше PyCharm попросит URL удалённого репозитория.

Что **не** попадёт в коммит (см. `.gitignore`): `.venv/`, `.idea/`,
`__pycache__/`, `build/`, `dist/`, `*.exe`, `*.log`, `translation_cache.json`,
`*.failed.txt`, `backup_*/`.

## 6. Сборка `.exe` (по желанию, только Windows)

```bash
pip install pyinstaller
python build.py           # либо двойной клик по build.bat
```

Результат — `dist\RenPyTranslator.exe` (onefile, ~27 МБ). Папка `dist/` в
`.gitignore`, поэтому в репозиторий бинарник не уедет — это нормальная практика.

## 7. Что внутри архива

```
renpy_translator/
├── main.py                  точка входа: CLI + запуск окна
├── config.py                константы, Settings, VERSION
├── gui.py                   классическое окно (запасное)
├── gui_modern.py            основное окно (CustomTkinter)
├── gui_images.py            вкладка «Текст-картинки»
├── core/                    движок: разбор .rpy, rpa/rpyc, перевод, сборка tl,
│                            шрифты, кэш, качество, отчёты, breakdown
├── tests/run_tests.py       все 231 тест одним файлом
├── assets/                  иконки и шрифты (PT Serif и др.)
├── docs/                    скриншоты для README
├── examples/                пример глоссария и сгенерированного tl/russian
├── build.py, build.bat      сборка exe через PyInstaller
├── requirements.txt         зависимости
├── README.md                полное руководство (15 разделов)
├── CHANGELOG.md             история версий
├── ЧТО-НОВОГО-3.x.x.md      заметки к выпускам
├── КАК-УСТАНОВИТЬ.txt       инструкция для пользователя exe
├── УНИВЕРСАЛЬНОСТЬ.md       как это работает на любой игре
└── .gitignore               что не коммитить
```

## 8. Если что-то не запускается

| Симптом | Причина и решение |
|---|---|
| `ModuleNotFoundError: customtkinter` | `pip install -r requirements.txt` (или программа сама откроет старый GUI — она умеет) |
| Окно не появляется, в консоли пусто | запущен `main.pyw`/`pythonw` без консоли и упал — запустите через `python main.py` и смотрите traceback |
| `No module named 'tkinter'` | Linux: `sudo apt install python3-tk`; Windows: переустановить Python с галочкой *tcl/tk* |
| `--version` показывает не 3.7.6 | в PyCharm выбран другой интерпретатор или рабочая папка; сверьте `config.py` → `VERSION` |
| Тесты падают на `PermissionsError` | запускайте из папки проекта, не из `tests/` (там относительные пути) |
