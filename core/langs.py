"""
Справочник языков (v3.0): как называть язык в интерфейсе и что с ним можно.

Программа изначально делалась под русский, но движок Ren'Py универсален:
достаточно ``game/tl/<язык>/``. Здесь собрано то, что нужно знать о языке,
чтобы не наломать дров:

* ``LANGUAGE_LABELS`` — как язык называется для игрока (для кнопки в меню);
* ``CYRILLIC_LANGS`` — нужен ли шрифт с кириллицей (PT Serif);
* ``SCRIPT_OF`` — какая письменность ожидается (проверка качества);
* ``is_cyrillic`` / ``label_for`` — удобные обёртки.
"""

from __future__ import annotations

#: Код языка → подпись для игрока.
LANGUAGE_LABELS = {
    "russian": "Русский",
    "ukrainian": "Українська",
    "belarusian": "Беларуская",
    "bulgarian": "Български",
    "serbian": "Српски",
    "kazakh": "Қазақша",
    "english": "English",
    "german": "Deutsch",
    "french": "Français",
    "spanish": "Español",
    "italian": "Italiano",
    "portuguese": "Português",
    "polish": "Polski",
    "czech": "Čeština",
    "turkish": "Türkçe",
    "japanese": "日本語",
    "korean": "한국어",
    "chinese": "中文",
    "simplified_chinese": "简体中文",
    "traditional_chinese": "繁體中文",
    "arabic": "العربية",
    "hebrew": "עברית",
    "greek": "Ελληνικά",
    "thai": "ไทย",
    "hindi": "हिन्दी",
}

#: Языки, которым нужен шрифт с кириллицей (наш PT Serif).
CYRILLIC_LANGS = frozenset({
    "russian", "ukrainian", "belarusian", "bulgarian", "serbian", "kazakh",
    "macedonian", "montenegrin", "kyrgyz", "tajik", "mongolian",
})

#: Языки, для которых бандл-шрифт PT Serif бесполезен (другая письменность).
NON_LATIN_CYRILLIC_FONTS = frozenset({
    "japanese", "korean", "chinese", "simplified_chinese",
    "traditional_chinese", "arabic", "hebrew", "thai", "hindi", "greek",
})


def is_cyrillic(language: str) -> bool:
    """Нужен ли этому языку шрифт с кириллицей."""
    return (language or "").lower() in CYRILLIC_LANGS


def label_for(language: str) -> str:
    """Подпись языка для игрока (для кнопки в меню игры)."""
    code = (language or "").lower()
    return LANGUAGE_LABELS.get(code, code.capitalize() or "Русский")


def needs_bundled_font(language: str) -> bool:
    """Имеет смысл ставить наш PT Serif (только для кириллицы и латиницы)."""
    code = (language or "").lower()
    return code not in NON_LATIN_CYRILLIC_FONTS


def ui_choices() -> tuple[tuple[str, str], ...]:
    """Список «код — подпись» для выпадающего списка в интерфейсе."""
    return tuple(sorted(LANGUAGE_LABELS.items(), key=lambda kv: kv[1]))
