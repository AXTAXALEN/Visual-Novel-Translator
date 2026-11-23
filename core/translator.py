"""
Движки перевода.

Реализованы:

* ``google``      — Google Translate через ``deep-translator``;
* ``google-web``  — Google Translate напрямую по HTTP (без внешних зависимостей);
* ``deepl``       — DeepL API (нужен ключ; поддерживается Free и Pro);
* ``libre``       — LibreTranslate (свой/публичный сервер);
* ``mock``        — оффлайн-заглушка для тестов и «сухого прогона»;
* ``none``        — ничего не переводим (используем кэш/импорт).

Все движки умеют: повторные попытки с экспоненциальной задержкой, ограничение
частоты запросов и «мягкую» деградацию (если строка не перевелась — вернётся
оригинал и это будет отражено в отчёте).
"""

from __future__ import annotations

import json
import random
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Callable, Optional, Sequence

from config import Settings
from core.utils import get_logger

logger = get_logger(__name__)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


class TranslationError(Exception):
    """Ошибка перевода отдельной строки."""


class ThrottleError(TranslationError):
    """Сервис перевода просит сбавить темп (429 / too many requests / квота)."""


class HostBlocked(TranslationError):
    """Конкретный адрес сервиса не пустил запрос (HTTP-код сохранён)."""

    def __init__(self, message: str, code: int = 0, host: str = "") -> None:
        super().__init__(message)
        self.code = int(code or 0)
        self.host = host


#: Признаки того, что нас ограничивают по частоте запросов.
THROTTLE_MARKERS = (
    "429", "too many requests", "too_many_requests", "toomanyrequests",
    "rate limit", "ratelimit", "quota", "server error: you made too many",
)


def is_throttle_error(exc: BaseException) -> bool:
    """Похоже ли исключение на «сбавь темп»."""
    if isinstance(exc, ThrottleError):
        return True
    text = f"{type(exc).__name__} {exc}".lower()
    return any(marker in text for marker in THROTTLE_MARKERS)


class RateLimiter:
    """Простое ограничение частоты: не чаще одного запроса в `delay` секунд."""

    def __init__(self, delay: float) -> None:
        self.delay = max(0.0, delay)
        self._lock = threading.Lock()
        self._last = 0.0

    def wait(self) -> None:
        if self.delay <= 0:
            return
        with self._lock:
            now = time.monotonic()
            delta = now - self._last
            if delta < self.delay:
                time.sleep(self.delay - delta)
            self._last = time.monotonic()

    def adapt(self, max_delay: float = 2.0) -> float:
        """Замедляемся: сервис просит реже (вызывается при 429)."""
        with self._lock:
            self.delay = min(max_delay, max(0.15, (self.delay or 0.05) * 2))
            return self.delay


def _http_json(url: str, payload: Optional[dict] = None, timeout: float = 30.0,
               headers: Optional[dict] = None) -> dict | list:
    """POST/GET JSON без внешних зависимостей."""
    data = None
    hdrs = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    if headers:
        hdrs.update(headers)

    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        hdrs["Content-Type"] = "application/json"

    req = urllib.request.Request(url, data=data, headers=hdrs)
    with urllib.request.urlopen(req, timeout=timeout) as response:
        raw = response.read().decode("utf-8", errors="replace")
    return json.loads(raw)


# ===========================================================================
# Базовая реализация
# ===========================================================================
class BaseEngine(ABC):
    """Базовый класс движка перевода."""

    name: str = "base"

    #: Устанавливается Pipeline: позволяет прервать работу из GUI.
    abort_check: Optional[Callable[[], bool]] = None

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.abort_check = None
        self.limiter = RateLimiter(settings.request_delay)
        self.target = settings.target_lang
        self.source = settings.source_lang or "auto"
        self.stats = {"requests": 0, "retries": 0, "errors": 0, "chars": 0}
        self._stats_lock = threading.Lock()

    # ------------------------------------------------------------------
    def clone(self) -> "BaseEngine":
        """Новый экземпляр для потока (чтобы не делить состояние)."""
        return type(self)(self.settings)

    # ------------------------------------------------------------------
    @abstractmethod
    def _translate_one(self, text: str) -> str:
        """Переводит одну строку. Может бросить исключение."""

    # ------------------------------------------------------------------
    def translate_one(self, text: str) -> str:
        """Перевод с повторами и учётом статистики."""
        if not text.strip():
            return text

        last_exc: Optional[Exception] = None
        throttled = False
        for attempt in range(self.settings.max_retries + 1):
            if self._aborted():
                raise TranslationError("перевод прерван пользователем")
            try:
                self.limiter.wait()
                with self._stats_lock:
                    self.stats["requests"] += 1
                    self.stats["chars"] += len(text)
                result = self._translate_one(text)
                if result is None:
                    raise TranslationError("движок вернул пустой ответ")
                return result
            except Exception as exc:  # noqa: BLE001 - движки кидают разное
                last_exc = exc
                with self._stats_lock:
                    self.stats["errors"] += 1

                if is_throttle_error(exc):
                    # Не устраиваем «шторм повторов»: замедляемся и быстро сдаёмся,
                    # чтобы пользователь видел результат, а не бесконечное ожидание.
                    throttled = True
                    new_delay = self.limiter.adapt()
                    logger.warning("Сервис перевода просит сбавить темп (429). "
                                   "Пауза между запросами: %.1f с", new_delay)
                    if attempt >= 1:
                        raise ThrottleError(str(exc)) from exc
                    time.sleep(min(2.0, new_delay * 2))
                    continue

                if attempt >= self.settings.max_retries:
                    break
                backoff = min(30.0, (2 ** attempt) + random.uniform(0, 1.0))
                with self._stats_lock:
                    self.stats["retries"] += 1
                logger.debug("Повтор перевода (%s, попытка %d): %s",
                             type(exc).__name__, attempt + 1, exc)
                time.sleep(backoff)

        if throttled:
            raise ThrottleError(f"{type(last_exc).__name__}: {last_exc}")
        raise TranslationError(f"{type(last_exc).__name__}: {last_exc}")

    def _aborted(self) -> bool:
        """Просили ли прервать работу."""
        check = self.abort_check
        try:
            return bool(check and check())
        except Exception:  # noqa: BLE001
            return False

    # ------------------------------------------------------------------
    def translate_batch(self, texts: Sequence[str]) -> list[Optional[str]]:
        """
        Переводит пачку строк.

        Возвращает список: перевод либо None, если строку перевести не удалось.
        Движки с настоящей пакетной поддержкой переопределяют метод.
        """
        results: list[Optional[str]] = []
        for index, text in enumerate(texts):
            if self._aborted():
                results.extend([None] * (len(texts) - index))
                break
            try:
                results.append(self.translate_one(text))
            except ThrottleError as exc:
                # сервис просит подождать — не тратим время на остаток пачки
                logger.warning("Перевод приостановлен (лимит запросов): %s", exc)
                results.append(None)
                results.extend([None] * (len(texts) - index - 1))
                break
            except TranslationError as exc:
                logger.warning("Не удалось перевести строку: %s", exc)
                results.append(None)
        return results

    # ------------------------------------------------------------------
    def close(self) -> None:  # pragma: no cover - переопределяется при необходимости
        pass


# ===========================================================================
# Google: бесплатный пакетный endpoint (client=dict-chrome-ex)
# ===========================================================================
class GoogleFreeEngine(BaseEngine):
    """
    Google Translate без ключа и без сторонних пакетов — **пачками**.

    Используется публичный endpoint, который применяет расширение Chrome
    для перевода страниц:

        POST https://clients5.google.com/translate_a/t?client=dict-chrome-ex
        q=<строка 1>&q=<строка 2>&...   sl=auto  tl=ru

    Ответ: ``[["Привет, мир", "en"], ["Доброе утро", "en"], ...]`` — по одному
    элементу на каждую переданную строку, в том же порядке.

    Почему это важно: ``translate.google.com/m`` и
    ``translate.googleapis.com/translate_a/single`` (client=gtx) отдают
    редирект на ``/sorry`` и HTTP 429 уже после нескольких десятков запросов.
    Здесь же в одном запросе уходит до ``MAX_PER_REQUEST`` строк, поэтому на
    2 000 строк получается ~80 запросов вместо 2 000 — упереться в лимит
    почти невозможно.
    """

    name = "google"
    ENDPOINT = "https://clients5.google.com/translate_a/t"
    #: Зеркала того же endpoint: если один адрес блокируют, пробуем следующий.
    HOSTS = (
        "https://clients5.google.com/translate_a/t",
        "https://clients6.google.com/translate_a/t",
        "https://translate.google.com/translate_a/t",
    )
    MAX_PER_REQUEST = 40        # строк в одном запросе
    MAX_CHARS = 3500            # символов в одном запросе
    USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/124.0 Safari/537.36")

    def __init__(self, settings: Settings) -> None:
        super().__init__(settings)
        self.target_code = GoogleEngine._normalize_lang(self.target)
        self.source_code = ("auto" if self.source in ("auto", "Auto", "") else
                            GoogleEngine._normalize_lang(self.source))
        self._host_index = 0

    def clone(self) -> "GoogleFreeEngine":
        return GoogleFreeEngine(self.settings)

    # ------------------------------------------------------------------
    def _post(self, texts: Sequence[str]) -> str:
        """Отправляет пачку строк, возвращает тело ответа."""
        data = urllib.parse.urlencode(
            [("q", text) for text in texts]).encode("utf-8")
        host = self.HOSTS[self._host_index % len(self.HOSTS)]
        url = (f"{host}?client=dict-chrome-ex"
               f"&sl={urllib.parse.quote(self.source_code)}"
               f"&tl={urllib.parse.quote(self.target_code)}")
        req = urllib.request.Request(
            url, data=data,
            headers={"User-Agent": self.USER_AGENT,
                     "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
                     "Accept": "*/*"},
        )
        try:
            with urllib.request.urlopen(req, timeout=self.settings.timeout) as response:
                return response.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            body = ""
            try:
                body = exc.read().decode("utf-8", errors="replace")[:200]
            except Exception:  # noqa: BLE001
                pass
            # 403/404/429 на одном адресе не значит, что нас заблокировали
            # везде: переставляем хост для следующего запроса
            self._host_index += 1
            raise HostBlocked(
                f"Google ответил HTTP {exc.code} ({host.split('/')[2]})"
                + (f": {body}" if body else ""),
                code=exc.code, host=host.split("/")[2]) from exc
        except urllib.error.URLError as exc:
            self._host_index += 1
            raise HostBlocked(
                f"нет связи с {host.split('/')[2]}: {exc.reason}",
                code=0, host=host.split("/")[2]) from exc

    # ------------------------------------------------------------------
    def _parse(self, raw: str, expected: int) -> list[str]:
        payload = json.loads(raw)
        # Вариант 1: [["Привет", "en"], ...]
        if isinstance(payload, list) and payload and isinstance(payload[0], list):
            rv = []
            for item in payload:
                value = item[0] if item else ""
                rv.append(value if isinstance(value, str) else "")
            return rv
        # Вариант 2 (одна строка): ["Привет", "en"]
        if isinstance(payload, list) and payload and isinstance(payload[0], str):
            return [payload[0]]
        # Вариант 3: просто список строк
        if isinstance(payload, list):
            return [x for x in payload if isinstance(x, str)]
        raise TranslationError("неожиданный ответ Google")

    # ------------------------------------------------------------------
    def _translate_one(self, text: str) -> str:
        return self.translate_batch([text])[0] or ""

    # ------------------------------------------------------------------
    def translate_batch(self, texts: Sequence[str]) -> list[Optional[str]]:
        """
        Переводит пачку: строки пакуются в несколько запросов.

        Если сервис отвечает лимитом — бросаем :class:`ThrottleError`, чтобы
        конвейер замедлился и честно остановился вместо «шторма повторов».
        """
        results: list[Optional[str]] = [None] * len(texts)

        # пустые строки и строки без букв переводить незачем
        pending: list[int] = []
        for index, text in enumerate(texts):
            if not text.strip():
                results[index] = text
                continue
            pending.append(index)
        if not pending:
            return results

        for chunk in self._pack(texts, pending):
            if self._aborted():
                break
            self.limiter.wait()
            with self._stats_lock:
                self.stats["requests"] += 1
                self.stats["chars"] += sum(len(texts[i]) for i in chunk)
            values = self._translate_chunk([texts[i] for i in chunk])
            for index, value in zip(chunk, values):
                results[index] = value
            if values is None or all(v is None for v in values):
                # сервис упёрся в лимит — остаток пачки не мучаем
                break
        return results

    # ------------------------------------------------------------------
    def _pack(self, texts: Sequence[str], indexes: Sequence[int]) -> list[list[int]]:
        """Делит индексы на порции по количеству строк и по длине."""
        chunks: list[list[int]] = []
        current: list[int] = []
        length = 0
        for index in indexes:
            size = len(texts[index]) + 1
            if current and (len(current) >= self.MAX_PER_REQUEST
                            or length + size > self.MAX_CHARS):
                chunks.append(current)
                current, length = [], 0
            current.append(index)
            length += size
        if current:
            chunks.append(current)
        return chunks

    # ------------------------------------------------------------------
    def _translate_chunk(self, chunk: Sequence[str]) -> list[Optional[str]]:
        """Один запрос. При сетевых/лимитных ошибках — понятные исключения."""
        try:
            raw = self._post(chunk)
        except HostBlocked as exc:
            with self._stats_lock:
                self.stats["errors"] += 1
            if exc.code in (429, 503, 502, 403, 400):
                new_delay = self.limiter.adapt()
                raise ThrottleError(
                    f"{exc}; пауза увеличена до {new_delay:.1f} с") from exc
            if exc.code:
                raise TranslationError(str(exc)) from exc
            raise TranslationError(str(exc)) from exc
        except urllib.error.HTTPError as exc:
            with self._stats_lock:
                self.stats["errors"] += 1
            if exc.code in (429, 503, 502):
                new_delay = self.limiter.adapt()
                raise ThrottleError(
                    f"Google ограничивает частоту запросов (HTTP {exc.code}); "
                    f"пауза увеличена до {new_delay:.1f} с") from exc
            if exc.code in (403, 400):
                raise ThrottleError(
                    f"Google отклонил запрос (HTTP {exc.code}) — "
                    "похоже, на этот IP сейчас действует ограничение") from exc
            raise TranslationError(f"HTTP {exc.code} от Google") from exc
        except (urllib.error.URLError, OSError) as exc:
            with self._stats_lock:
                self.stats["errors"] += 1
            raise TranslationError(f"сеть недоступна: {exc}") from exc

        if not raw.strip():
            raise TranslationError("Google вернул пустой ответ")
        if raw.lstrip().startswith("<"):
            # HTML вместо JSON — это страница «/sorry»
            with self._stats_lock:
                self.stats["errors"] += 1
            new_delay = self.limiter.adapt()
            raise ThrottleError(
                "Google вернул страницу ограничения доступа вместо перевода; "
                f"пауза увеличена до {new_delay:.1f} с")

        try:
            values = self._parse(raw, len(chunk))
        except (json.JSONDecodeError, TranslationError) as exc:
            raise TranslationError(f"не удалось разобрать ответ Google: {exc}") from exc

        if len(values) != len(chunk):
            # Ответ «съехал» — переводим по одной, чтобы не путать строки.
            logger.debug("Google вернул %d переводов на %d строк — по одной",
                         len(values), len(chunk))
            single: list[Optional[str]] = []
            for text in chunk:
                try:
                    raw_one = self._post([text])
                    parsed = self._parse(raw_one, 1)
                    single.append(parsed[0] if parsed else None)
                except ThrottleError:
                    single.append(None)
                except (TranslationError, json.JSONDecodeError):
                    single.append(None)
            return single

        return [value or None for value in values]


# ===========================================================================
# MyMemory (резервный бесплатный сервис)
# ===========================================================================
class MyMemoryEngine(BaseEngine):
    """MyMemory — бесплатный сервис без ключа (лимит ~5 000 знаков в день)."""

    name = "mymemory"
    ENDPOINT = "https://api.mymemory.translated.net/get"

    def __init__(self, settings: Settings) -> None:
        super().__init__(settings)
        self.target_code = GoogleEngine._normalize_lang(self.target)
        source = self.source if self.source not in ("auto", "Auto", "") else "en"
        self.source_code = GoogleEngine._normalize_lang(source)

    def clone(self) -> "MyMemoryEngine":
        return MyMemoryEngine(self.settings)

    def _translate_one(self, text: str) -> str:
        params = {
            "q": text,
            "langpair": f"{self.source_code}|{self.target_code}",
        }
        url = self.ENDPOINT + "?" + urllib.parse.urlencode(params)
        try:
            payload = _http_json(url, timeout=self.settings.timeout)
        except urllib.error.HTTPError as exc:
            if exc.code in (429, 403):
                raise ThrottleError(
                    f"MyMemory ограничил частоту запросов (HTTP {exc.code})") from exc
            raise TranslationError(f"MyMemory: HTTP {exc.code}") from exc

        data = (payload or {}).get("responseData") or {}
        value = data.get("translatedText") or ""
        status = (payload or {}).get("responseStatus")
        if str(status) != "200" and not value:
            message = (payload or {}).get("responseDetails") or "пустой ответ"
            if "quota" in str(message).lower() or "limit" in str(message).lower():
                raise ThrottleError(f"MyMemory: {message}")
            raise TranslationError(f"MyMemory: {message}")
        if "MYMEMORY WARNING" in value.upper():
            raise ThrottleError("MyMemory: исчерпан дневной лимит")
        return value


# ===========================================================================
# Google (deep-translator)
# ===========================================================================
class GoogleEngine(BaseEngine):
    """Google Translate через пакет ``deep-translator``."""

    name = "google"

    def __init__(self, settings: Settings) -> None:
        super().__init__(settings)
        try:
            from deep_translator import GoogleTranslator  # type: ignore
        except ImportError as exc:  # pragma: no cover
            raise TranslationError(
                "Не установлен пакет deep-translator.\n"
                "Установите: pip install deep-translator\n"
                "или используйте --engine google-web (без зависимостей)."
            ) from exc
        self._cls = GoogleTranslator
        self._translator = GoogleTranslator(
            source=self.source if self.source != "auto" else "auto",
            target=self._normalize_lang(self.target),
        )

    @staticmethod
    def _normalize_lang(lang: str) -> str:
        mapping = {"russian": "ru", "english": "en", "ukrainian": "uk",
                   "japanese": "ja", "chinese": "zh-CN", "korean": "ko"}
        return mapping.get(lang.lower(), lang)

    def clone(self) -> "GoogleEngine":
        return GoogleEngine(self.settings)

    def _translate_one(self, text: str) -> str:
        return self._translator.translate(text)

    def translate_batch(self, texts: Sequence[str]) -> list[Optional[str]]:
        # deep-translator умеет translate_batch, но он склеивает строки
        # разделителем — для диалогов это рискованно, поэтому по одной.
        return super().translate_batch(texts)


# ===========================================================================
# Google (напрямую по HTTP, без зависимостей)
# ===========================================================================
class GoogleWebEngine(BaseEngine):
    """Google Translate через публичный endpoint ``translate_a/single``."""

    name = "google-web"
    ENDPOINT = "https://translate.googleapis.com/translate_a/single"

    def __init__(self, settings: Settings) -> None:
        super().__init__(settings)
        self.target_code = GoogleEngine._normalize_lang(self.target)
        self.source_code = "auto" if self.source in ("auto", "Auto", "") else self.source

    def clone(self) -> "GoogleWebEngine":
        return GoogleWebEngine(self.settings)

    def _translate_one(self, text: str) -> str:
        params = {
            "client": "gtx",
            "sl": self.source_code,
            "tl": self.target_code,
            "dt": "t",
            "q": text,
        }
        url = self.ENDPOINT + "?" + urllib.parse.urlencode(params)
        try:
            payload = _http_json(url, timeout=self.settings.timeout)
        except urllib.error.HTTPError as exc:
            if exc.code == 429:
                raise TranslationError(
                    "Google ограничил частоту запросов (HTTP 429). "
                    "Уменьшите --workers (например, --workers 1), добавьте "
                    "--delay 1.5 или используйте --engine deepl/libre."
                ) from exc
            if exc.code in (403, 400):
                raise TranslationError(
                    f"Google отклонил запрос (HTTP {exc.code}). "
                    "Проверьте язык (--lang) или попробуйте --engine deepl."
                ) from exc
            raise

        if not isinstance(payload, list) or not payload or not payload[0]:
            raise TranslationError("неожиданный ответ Google")

        chunks = []
        for part in payload[0]:
            if isinstance(part, list) and part and isinstance(part[0], str):
                chunks.append(part[0])
        result = "".join(chunks)
        if not result:
            raise TranslationError("Google вернул пустой перевод")
        return result

    def translate_batch(self, texts: Sequence[str]) -> list[Optional[str]]:
        return super().translate_batch(texts)


# ===========================================================================
# DeepL
# ===========================================================================
class DeepLEngine(BaseEngine):
    """DeepL API (Free или Pro)."""

    name = "deepl"

    def __init__(self, settings: Settings) -> None:
        super().__init__(settings)
        if not settings.api_key:
            raise TranslationError(
                "Для DeepL нужен API-ключ: --api-key ВАШ_КЛЮЧ\n"
                "Ключ бесплатного тарифа можно получить на deepl.com/pro-api"
            )
        self.api_key = settings.api_key.strip()
        self.endpoint = (
            "https://api-free.deepl.com/v2/translate"
            if self.api_key.endswith(":fx")
            else "https://api.deepl.com/v2/translate"
        )
        self.target_code = self._normalize_lang(self.target)
        self.source_code = None if self.source in ("auto", "") else self._normalize_lang(self.source)

    @staticmethod
    def _normalize_lang(lang: str) -> str:
        mapping = {"russian": "RU", "english": "EN", "ukrainian": "UK",
                   "japanese": "JA", "chinese": "ZH", "korean": "KO"}
        return mapping.get(lang.lower(), lang.upper())

    def clone(self) -> "DeepLEngine":
        return DeepLEngine(self.settings)

    def _request(self, texts: Sequence[str]) -> list[str]:
        data: dict = {"text": list(texts), "target_lang": self.target_code}
        if self.source_code:
            data["source_lang"] = self.source_code
        body = urllib.parse.urlencode(data, doseq=True).encode("utf-8")
        req = urllib.request.Request(
            self.endpoint,
            data=body,
            headers={
                "Authorization": f"DeepL-Auth-Key {self.api_key}",
                "Content-Type": "application/x-www-form-urlencoded",
                "User-Agent": USER_AGENT,
            },
        )
        with urllib.request.urlopen(req, timeout=self.settings.timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
        translations = payload.get("translations") or []
        if len(translations) != len(texts):
            raise TranslationError("DeepL вернул неверное число переводов")
        return [t.get("text", "") for t in translations]

    def _translate_one(self, text: str) -> str:
        return self._request([text])[0]

    def translate_batch(self, texts: Sequence[str]) -> list[Optional[str]]:
        results: list[Optional[str]] = [None] * len(texts)
        batch_size = max(1, min(self.settings.batch_size, 50))
        for start in range(0, len(texts), batch_size):
            chunk = list(texts[start:start + batch_size])
            try:
                self.limiter.wait()
                translated = self._request(chunk)
                for i, value in enumerate(translated):
                    results[start + i] = value
                with self._stats_lock:
                    self.stats["requests"] += 1
                    self.stats["chars"] += sum(len(t) for t in chunk)
            except Exception as exc:  # noqa: BLE001
                logger.warning("DeepL: ошибка пакетного перевода (%s), "
                               "перехожу на поштучный режим", exc)
                for i, text in enumerate(chunk):
                    try:
                        results[start + i] = self.translate_one(text)
                    except TranslationError:
                        results[start + i] = None
        return results


# ===========================================================================
# LibreTranslate
# ===========================================================================
class LibreEngine(BaseEngine):
    """LibreTranslate: локальный или публичный сервер."""

    name = "libre"

    def __init__(self, settings: Settings) -> None:
        super().__init__(settings)
        self.url = (settings.libretranslate_url or "https://libretranslate.com").rstrip("/")
        if not self.url.endswith("/translate"):
            self.url += "/translate"
        self.api_key = settings.api_key
        self.source_code = "auto" if self.source in ("auto", "") else self.source
        self.target_code = GoogleEngine._normalize_lang(self.target)

    def clone(self) -> "LibreEngine":
        return LibreEngine(self.settings)

    def _request(self, texts: Sequence[str]) -> list[str]:
        payload: dict = {
            "q": list(texts),
            "source": self.source_code,
            "target": self.target_code,
            "format": "text",
        }
        if self.api_key:
            payload["api_key"] = self.api_key
        data = _http_json(self.url, payload, timeout=self.settings.timeout * 2)
        if isinstance(data, dict):
            translated = data.get("translatedText")
            if isinstance(translated, str):
                return [translated]
            if isinstance(translated, list):
                return translated
        raise TranslationError(f"неожиданный ответ LibreTranslate: {str(data)[:200]}")

    def _translate_one(self, text: str) -> str:
        return self._request([text])[0]

    def translate_batch(self, texts: Sequence[str]) -> list[Optional[str]]:
        results: list[Optional[str]] = [None] * len(texts)
        batch_size = max(1, min(self.settings.batch_size, 50))
        for start in range(0, len(texts), batch_size):
            chunk = list(texts[start:start + batch_size])
            try:
                self.limiter.wait()
                translated = self._request(chunk)
                if len(translated) != len(chunk):
                    raise TranslationError("LibreTranslate вернул неверное число строк")
                for i, value in enumerate(translated):
                    results[start + i] = value
                with self._stats_lock:
                    self.stats["requests"] += 1
            except Exception as exc:  # noqa: BLE001
                logger.warning("LibreTranslate: %s — перехожу на поштучный режим", exc)
                for i, text in enumerate(chunk):
                    try:
                        results[start + i] = self.translate_one(text)
                    except TranslationError:
                        results[start + i] = None
        return results


# ===========================================================================
# Заглушки
# ===========================================================================
class MockEngine(BaseEngine):
    """
    Оффлайн-движок для тестов.

    Помечает строки префиксом ``[ru]`` и подставляет несколько слов из
    мини-словаря — этого достаточно, чтобы проверить весь конвейер без сети.
    """

    name = "mock"

    MINI_DICT = {
        "hello": "привет",
        "hi": "привет",
        "yes": "да",
        "no": "нет",
        "start": "начать",
        "load": "загрузить",
        "settings": "настройки",
        "quit": "выход",
        "the": "",
        "a": "",
    }

    def __init__(self, settings: Settings) -> None:
        super().__init__(settings)
        self.prefix = "[ru] "

    def clone(self) -> "MockEngine":
        return MockEngine(self.settings)

    def _translate_one(self, text: str) -> str:
        words = []
        for word in text.split(" "):
            key = word.strip(".,!?:;\"'").lower()
            if key in self.MINI_DICT and self.MINI_DICT[key]:
                words.append(self.MINI_DICT[key].capitalize()
                             if word[:1].isupper() else self.MINI_DICT[key])
            else:
                words.append(word)
        return self.prefix + " ".join(words)


class NullEngine(BaseEngine):
    """Ничего не переводит (полезно вместе с кэшем или импортом)."""

    name = "none"

    def clone(self) -> "NullEngine":
        return NullEngine(self.settings)

    def _translate_one(self, text: str) -> str:
        return text


# ===========================================================================
# Фабрика
# ===========================================================================
ENGINES = {
    # "google" — бесплатный Google пачками (по умолчанию, без ключа)
    "google": GoogleFreeEngine,
    "google-free": GoogleFreeEngine,
    # старый путь через пакет deep-translator
    "google-dt": GoogleEngine,
    # старый публичный endpoint translate_a/single (часто ловит 429)
    "google-web": GoogleWebEngine,
    "mymemory": MyMemoryEngine,
    "deepl": DeepLEngine,
    "libre": LibreEngine,
    "mock": MockEngine,
    "none": NullEngine,
}


def create_engine(name: str, settings: Settings, fallback: bool = True) -> BaseEngine:
    """
    Создаёт движок по имени.

    Если запрошенный движок недоступен (нет пакета/ключа) и `fallback`,
    пробуем разумную замену:
      deepl (нет ключа) -> google
      google (нет deep-translator) -> google-web
    """
    key = (name or "google").strip().lower()

    if key not in ENGINES:
        raise TranslationError(
            f"Неизвестный движок перевода: {name}. "
            f"Доступны: {', '.join(sorted(ENGINES))}"
        )

    try:
        return ENGINES[key](settings)
    except TranslationError as exc:
        if not fallback:
            raise
        logger.warning("%s — пробую запасной движок", exc)
        if key in ("google", "google-free"):
            return GoogleWebEngine(settings)
        if key == "deepl":
            logger.warning("DeepL недоступен, переключаюсь на Google")
            try:
                return GoogleFreeEngine(settings)
            except TranslationError:
                return GoogleWebEngine(settings)
        raise


@dataclass
class TranslationOutcome:
    """Итог перевода набора строк."""

    translated: int = 0
    from_cache: int = 0
    failed: int = 0
    requests: int = 0
    elapsed: float = 0.0
    failed_texts: list[str] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.failed_texts is None:
            self.failed_texts = []
