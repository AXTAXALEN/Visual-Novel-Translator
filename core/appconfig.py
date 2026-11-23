"""
Пользовательские настройки приложения (``settings.json`` рядом с .exe).

Хранит последнюю папку игры, выбранный движок перевода, ключи и прочие
переключатели — чтобы при следующем запуске всё было как в прошлый раз.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Any, Optional

from config import Settings, app_dir, default_cache_path, default_log_path
from core.utils import atomic_write_json, get_logger, read_text_file

logger = get_logger(__name__)

CONFIG_FILENAME = "settings.json"


@dataclass
class UserConfig:
    """То, что переживает перезапуск программы."""

    last_game_path: str = ""
    last_export_path: str = ""
    engine: str = "google"
    mode: str = "tl"
    target_lang: str = "russian"
    api_key: str = ""
    libre_url: str = ""
    workers: int = 4
    batch_size: int = 25
    delay: float = 0.0
    set_default_language: bool = True
    language_button: bool = True
    patch_language_menu: bool = False
    patch_image_menus: bool = True
    engine_strings: bool = True
    wrap_screen_text: bool = True
    hook_fallback: bool = False
    font_mode: str = "pretty"
    theme: str = "dark"
    appearance: str = "dark"
    """Оформление современного интерфейса: dark / light / system."""
    quality_check: bool = True
    skip_dev_tools: bool = True
    retry_rounds: int = 3
    extract_names: bool = True
    extract_ui: bool = True
    glossary_path: str = ""
    backup: bool = True
    use_cache: bool = True

    # ------------------------------------------------------------------
    @classmethod
    def path(cls) -> Path:
        return app_dir() / CONFIG_FILENAME

    @classmethod
    def load(cls) -> "UserConfig":
        config = cls()
        path = cls.path()
        if not path.exists():
            return config
        try:
            data = json.loads(read_text_file(path))
        except Exception as exc:  # noqa: BLE001
            logger.warning("Не удалось прочитать настройки: %s", exc)
            return config

        known = {f.name for f in fields(cls)}
        for key, value in data.items():
            if key in known and value is not None:
                try:
                    setattr(config, key, type(getattr(config, key))(value)
                            if not isinstance(getattr(config, key), bool)
                            else bool(value))
                except Exception:  # noqa: BLE001
                    setattr(config, key, value)
        return config

    def save(self) -> None:
        try:
            atomic_write_json(self.path(), asdict(self))
        except OSError as exc:
            logger.debug("Не удалось сохранить настройки: %s", exc)

    # ------------------------------------------------------------------
    def to_settings(self) -> Settings:
        """Превращает пользовательские настройки в рабочие ``Settings``."""
        settings = Settings()
        if self.last_game_path:
            settings.game_path = Path(self.last_game_path)
        settings.engine = self.engine or "google"
        settings.mode = self.mode or "tl"
        settings.target_lang = self.target_lang or "russian"
        settings.api_key = self.api_key or None
        settings.libretranslate_url = self.libre_url or None
        settings.workers = max(1, int(self.workers or 4))
        settings.batch_size = max(1, int(self.batch_size or 25))
        settings.request_delay = max(0.0, float(self.delay or 0.0))
        settings.set_default_language = bool(self.set_default_language)
        settings.language_button = bool(self.language_button)
        settings.patch_language_menu = bool(self.patch_language_menu)
        settings.patch_image_menus = bool(self.patch_image_menus)
        settings.wrap_screen_text = bool(self.wrap_screen_text)
        settings.engine_strings = bool(self.engine_strings)
        settings.hook_fallback = bool(self.hook_fallback)
        settings.font_mode = self.font_mode if self.font_mode in ("pretty", "fix", "off") \
            else "pretty"
        settings.quality_check = bool(self.quality_check)
        settings.quality_fallback = bool(self.quality_check)
        settings.skip_dev_tools = bool(self.skip_dev_tools)
        settings.retry_rounds = max(0, int(self.retry_rounds or 0))
        settings.extract_names = bool(self.extract_names)
        settings.extract_ui = bool(self.extract_ui)
        settings.backup = bool(self.backup)
        settings.use_cache = bool(self.use_cache)
        settings.cache_path = default_cache_path()
        settings.log_path = default_log_path()
        if self.glossary_path:
            settings.glossary_path = Path(self.glossary_path)
        return settings

    def update_from(self, **kwargs: Any) -> None:
        for key, value in kwargs.items():
            if hasattr(self, key):
                setattr(self, key, value)


def reset() -> None:
    """Удаляет файл настроек (кнопка «сбросить настройки»)."""
    path = UserConfig.path()
    if path.exists():
        try:
            path.unlink()
        except OSError:
            pass
