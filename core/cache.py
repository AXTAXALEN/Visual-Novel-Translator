"""
Кэш переводов: ``translation_cache.json``.

Формат (как в ТЗ)::

    {
      "Hello, how are you?": "Привет, как дела?",
      "The sun was setting.": "Солнце садилось."
    }

Дополнительно рядом сохраняется служебный файл с метаданными, но основной
кэш остаётся «плоским» словарём оригинал -> перевод, чтобы его можно было
править руками.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Iterator, Optional

from core.utils import atomic_write_json, get_logger, read_text_file

logger = get_logger(__name__)


class TranslationCache:
    """Потокобезопасный кэш переводов."""

    def __init__(self, path: Path, enabled: bool = True) -> None:
        self.path = Path(path)
        self.enabled = enabled
        self._lock = threading.RLock()
        self._data: dict[str, str] = {}
        self._dirty = False
        self._dirty_count = 0
        self.hits = 0
        self.misses = 0
        self.load()

    # ------------------------------------------------------------------
    def load(self) -> None:
        if not self.path.exists():
            self._data = {}
            return

        try:
            text = read_text_file(self.path)
            data = json.loads(text)
        except Exception as exc:
            logger.warning("Не удалось прочитать кэш %s: %s", self.path, exc)
            self._data = {}
            return

        if not isinstance(data, dict):
            logger.warning("Кэш %s имеет неожиданный формат — игнорирую", self.path)
            self._data = {}
            return

        # Совместимость: значения могут быть словарями {"translation": ...}
        clean: dict[str, str] = {}
        for key, value in data.items():
            if isinstance(value, str):
                clean[key] = value
            elif isinstance(value, dict) and isinstance(value.get("translation"), str):
                clean[key] = value["translation"]
        self._data = clean
        logger.info("Кэш загружен: %d строк (%s)", len(clean), self.path)

    # ------------------------------------------------------------------
    def get(self, source: str) -> Optional[str]:
        if not self.enabled:
            return None
        with self._lock:
            value = self._data.get(source)
            if value:
                self.hits += 1
                return value
            self.misses += 1
            return None

    def set(self, source: str, translation: str) -> None:
        if not self.enabled or not translation:
            return
        with self._lock:
            if self._data.get(source) != translation:
                self._data[source] = translation
                self._dirty = True
                self._dirty_count += 1

    def update(self, mapping: dict[str, str]) -> None:
        for k, v in mapping.items():
            self.set(k, v)

    # ------------------------------------------------------------------
    def save(self, force: bool = False) -> None:
        if not self.enabled:
            return
        with self._lock:
            if not self._dirty and not force:
                return
            atomic_write_json(self.path, dict(sorted(self._data.items())))
            self._dirty = False
            self._dirty_count = 0

    def maybe_save(self, every: int = 200) -> None:
        with self._lock:
            if self._dirty_count >= every:
                self.save(force=True)

    # ------------------------------------------------------------------
    def clear(self) -> None:
        with self._lock:
            self._data = {}
            self._dirty = True
            if self.path.exists():
                try:
                    self.path.unlink()
                except OSError as exc:
                    logger.warning("Не удалось удалить %s: %s", self.path, exc)

    def __len__(self) -> int:
        with self._lock:
            return len(self._data)

    def __contains__(self, item: str) -> bool:
        with self._lock:
            return item in self._data

    def items(self) -> Iterator[tuple[str, str]]:
        with self._lock:
            yield from list(self._data.items())

    def as_dict(self) -> dict[str, str]:
        with self._lock:
            return dict(self._data)

    # ------------------------------------------------------------------
    @staticmethod
    def default_path() -> Path:
        from config import default_cache_path

        return default_cache_path()
