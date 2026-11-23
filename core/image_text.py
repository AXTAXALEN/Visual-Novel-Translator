"""
Текст, нарисованный картинками: поиск, замена, откат.

Зачем
-----
Часть надписей в новеллах — не текст, а PNG: логотип на титуле, «Start»
поверх арта, подписи кнопок-картинок. Ни ``translate strings``, ни шрифты
их не меняют: буквы запечены в пиксели.

Что делает модуль
-----------------
1. **Ищет** такие картинки: проходит по ``screen``-блокам ``game/*.rpy``,
   собирает все литералы вида ``add "gui/title.png"`` /
   ``imagebutton ... "gui/x.png"`` и оценивает каждую картинку эвристикой
   «похоже на текст» (много мелких компонент одинаковой высоты, узкие
   штрихи, мало «заливки»).
2. **Рисует русскую версию**: заливает найденную область цветом фона и
   пишет поверх свой текст нужным шрифтом, размером и цветом. Размер
   картинки не меняется — вёрстка игры не поедет.
3. **Подключает** новую картинку, не ломая оригинал: литерал в экране
   превращается в ``rt_img("gui/title.png")``, а функция ``rt_img``
   (генерируется в ``game/tl/<язык>/zz_renpy_translator_images.rpy``)
   отдаёт русский файл только при включённом русском языке — при
   переключении на English остаётся оригинал.
4. **Откатывает**: копии изменённых ``.rpy`` лежат в
   ``game/tl/<язык>/rt_images_backup/``, кнопка «Вернуть исходники»
   возвращает всё как было и удаляет сгенерированные картинки.

Ограничения (честно)
--------------------
* Если текст нарисован с градиентом, тенью или поверх фотографии, заливка
  прямоугольника будет заметна — тогда лучше подобрать рамку поменьше или
  использовать цвет фона вручную.
* Рамку можно задать руками (x, y, ширина, высота) — по умолчанию берётся
  автоматически по «чернилам» картинки.
"""

from __future__ import annotations

import json
import re
import shutil
from collections import Counter, deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional

from config import GENERATED_PREFIX, TL_DIRECTORY
from core.scanner import RenpyProject
from core.utils import get_logger

logger = get_logger(__name__)

try:  # Pillow нужен только для этой функции; без него программа работает
    from PIL import Image, ImageDraw, ImageFont
except Exception:  # pragma: no cover  # noqa: BLE001
    Image = None  # type: ignore[assignment]
    ImageDraw = None  # type: ignore[assignment]
    ImageFont = None  # type: ignore[assignment]


#: Расширения картинок, которые умеет Ren'Py.
IMAGE_EXT = (".png", ".jpg", ".jpeg", ".webp", ".bmp")

#: Имя файла-реестра внутри game/tl/<язык>/.
REGISTRY_NAME = "rt_images.json"

#: Имя генерируемого rpy-файла с функцией rt_img.
HELPER_NAME = GENERATED_PREFIX + "_images.rpy"

#: Каталог резервных копий экранов. Копии лежат с суффиксом ``.bak``:
#: Ren'Py загружает любой ``*.rpy`` внутри ``game/``, и копия экранов
#: с расширением ``.rpy`` привела бы к двойному объявлению экранов.
BACKUP_DIR = "rt_images_backup"

#: Суффикс резервной копии.
BACKUP_SUFFIX = ".bak"

#: Порог «чернил» при определении текста.
INK_DIFF = 40

#: Картинки меньше этого не рассматриваем как кандидатов на надпись.
MIN_WIDTH = 24
MIN_HEIGHT = 12

_IMAGE_LITERAL = (r"[\"'](?P<path>[^\"'\n]+?\.(?:png|jpe?g|webp|bmp))"
                  r"(?P<percent>%s)?[\"']")

#: ``add "gui/title.png"`` (в том числе с аттрибутами).
ADD_RE = re.compile(r"\badd\s+" + _IMAGE_LITERAL, re.I)

#: ``imagebutton auto "gui/start_%s.png"`` / ``idle "gui/x.png"``.
IMAGEBUTTON_RE = re.compile(r"\bimagebutton\b", re.I)
QUOTED_IMAGE_RE = re.compile(_IMAGE_LITERAL, re.I)

#: ``define gui.main_menu_background = "gui/main_menu.png"``
DEFINE_RE = re.compile(
    r"^\s*define\s+(?P<name>[\w\.]+)\s*=\s*" + _IMAGE_LITERAL, re.I)

#: ``add gui.main_menu_background`` — картинка подставлена переменной.
ADD_NAME_RE = re.compile(r"\badd\s+(?P<name>[A-Za-z_][\w\.]*)\s*(?=$|[,)\s])")

#: Признак того, что строка объявляет экран.
SCREEN_RE = re.compile(r"^\s*screen\s+[\w\.]+\s*(\(|:)")


# ---------------------------------------------------------------------------
# Эвристика «в картинке есть текст»
# ---------------------------------------------------------------------------
def _ink_mask(image, size_limit: int = 320):
    """Бинарная маска «чернил» и её геометрия (уменьшенная копия)."""
    im = image.convert("RGBA")
    width, height = im.size
    scale = 1.0
    if max(width, height) > size_limit:
        scale = size_limit / max(width, height)
        im = im.resize((max(1, int(width * scale)), max(1, int(height * scale))),
                       Image.NEAREST)
    w, h = im.size
    alpha = im.getchannel("A")
    has_alpha = alpha.getextrema()[0] < 250

    if has_alpha:
        mask = [[alpha.getpixel((x, y)) > 32 for x in range(w)] for y in range(h)]
    else:
        gray = im.convert("L")
        values = list(gray.getdata())
        background = Counter(values).most_common(1)[0][0]
        mask = [[abs(gray.getpixel((x, y)) - background) > INK_DIFF
                 for x in range(w)] for y in range(h)]
    return mask, (w, h), scale, has_alpha


def _components(mask, size) -> list[tuple[int, int, int, int, int]]:
    """Связные области: список (x0, y0, x1, y1, площадь)."""
    w, h = size
    seen = [[False] * w for _ in range(h)]
    result: list[tuple[int, int, int, int, int]] = []
    for y in range(h):
        for x in range(w):
            if not mask[y][x] or seen[y][x]:
                continue
            queue = deque([(x, y)])
            seen[y][x] = True
            x0 = x1 = x
            y0 = y1 = y
            area = 0
            while queue:
                cx, cy = queue.popleft()
                area += 1
                if cx < x0:
                    x0 = cx
                if cx > x1:
                    x1 = cx
                if cy < y0:
                    y0 = cy
                if cy > y1:
                    y1 = cy
                for nx, ny in ((cx + 1, cy), (cx - 1, cy),
                               (cx, cy + 1), (cx, cy - 1)):
                    if 0 <= nx < w and 0 <= ny < h and mask[ny][nx] and not seen[ny][nx]:
                        seen[ny][nx] = True
                        queue.append((nx, ny))
            result.append((x0, y0, x1, y1, area))
    return result


def analyze_image(path: Path) -> tuple[float, Optional[tuple[int, int, int, int]], bool]:
    """
    Насколько картинка похожа на надпись.

    Возвращает (оценка 0..1, рамка «чернил» в исходных пикселях, есть ли
    прозрачность).
    """
    if Image is None:
        return 0.0, None, False
    try:
        image = Image.open(path)
        image.load()
    except Exception as exc:  # noqa: BLE001
        logger.debug("не читается картинка %s: %s", path, exc)
        return 0.0, None, False

    mask, (w, h), scale, has_alpha = _ink_mask(image)
    total = max(1, w * h)
    ink = sum(1 for row in mask for value in row if value)
    if not ink:
        return 0.0, None, has_alpha

    comps = [c for c in _components(mask, (w, h)) if c[4] >= 3]
    if len(comps) < 2:
        return 0.05, _bbox(comps, scale), has_alpha

    heights = sorted(c[3] - c[1] + 1 for c in comps)
    median = heights[len(heights) // 2]
    ink_ratio = ink / total
    uniform = 1.0 - min(1.0, _relative_spread(heights))
    tiny_share = sum(1 for value in heights if value <= max(2, median * 0.35)) / len(heights)
    many = min(1.0, len(comps) / 12.0)

    box = _bbox(comps, scale)
    concentration = 0.0
    if box:
        ink_area = max(1, (box[2] - box[0]) * (box[3] - box[1]))
        concentration = ink_area / float(max(1, image.width * image.height))
    if concentration > 0.85:
        # «чернила» размазаны по всей картинке — это фон, а не надпись
        many *= 0.4

    score = (0.30 * many
             + 0.25 * uniform
             + 0.20 * (1.0 if 3 <= median <= max(6, h * 0.75) else 0.0)
             + 0.15 * (1.0 if ink_ratio < 0.45 else 0.0)
             + 0.10 * (1.0 - tiny_share))
    return round(min(1.0, score), 3), _bbox(comps, scale), has_alpha


def _relative_spread(values: Iterable[int]) -> float:
    values = list(values)
    if not values:
        return 1.0
    mean = sum(values) / len(values)
    if not mean:
        return 1.0
    variance = sum((value - mean) ** 2 for value in values) / len(values)
    return (variance ** 0.5) / mean


def _bbox(comps, scale: float) -> Optional[tuple[int, int, int, int]]:
    if not comps:
        return None
    x0 = min(c[0] for c in comps)
    y0 = min(c[1] for c in comps)
    x1 = max(c[2] for c in comps)
    y1 = max(c[3] for c in comps)
    return (int(x0 / scale), int(y0 / scale),
            int(x1 / scale) + 1, int(y1 / scale) + 1)


# ---------------------------------------------------------------------------
# Поиск кандидатов
# ---------------------------------------------------------------------------
@dataclass
class ImageCandidate:
    """Картинка, на которую ссылаются экраны игры."""

    rel: str
    path: Path
    places: list[str] = field(default_factory=list)
    as_imagebutton: bool = False
    width: int = 0
    height: int = 0
    score: float = 0.0
    bbox: Optional[tuple[int, int, int, int]] = None
    has_alpha: bool = False
    missing: bool = False

    @property
    def name(self) -> str:
        return self.path.name

    def verdict(self) -> str:
        if self.missing:
            return "файла нет"
        if self.score >= 0.72:
            return "похоже на надпись"
        if self.score >= 0.45:
            return "возможно надпись"
        return "скорее картинка"

    def describe(self) -> str:
        size = f"{self.width}×{self.height}" if not self.missing else "—"
        where = ", ".join(self.places[:2]) or "—"
        return f"{self.rel}  [{size}]  {self.verdict()} · {where}"


def _iter_project_rpy(project: RenpyProject) -> Iterable[Path]:
    tl_root = (project.game_dir / TL_DIRECTORY).resolve()
    for path in project.rpy_files:
        try:
            if tl_root in path.resolve().parents:
                continue
        except OSError:
            pass
        if path.name.startswith(GENERATED_PREFIX):
            continue
        yield path


def collect_defines(project: RenpyProject) -> dict[str, str]:
    """Карты ``gui.x = "gui/x.png"``: имя переменной → путь к картинке."""
    defines: dict[str, str] = {}
    for path in _iter_project_rpy(project):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for line in text.splitlines():
            match = DEFINE_RE.match(line)
            if not match:
                continue
            literal = match.group("path")
            if "%s" in literal or match.group("percent"):
                continue
            defines[match.group("name")] = literal.replace("\\", "/").lstrip("/")
    return defines


def find_candidates(project: RenpyProject,
                    min_score: float = 0.0) -> list[ImageCandidate]:
    """Все картинки, на которые ссылаются экраны, с оценкой «это текст»."""
    found: dict[str, ImageCandidate] = {}
    defines = collect_defines(project)

    for path in _iter_project_rpy(project):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        in_screen = False
        for number, line in enumerate(text.splitlines(), 1):
            stripped = line.strip()
            if SCREEN_RE.match(line):
                in_screen = True
                continue
            if in_screen and stripped and not line[:1].isspace():
                in_screen = False
            if not in_screen:
                continue

            is_button = bool(IMAGEBUTTON_RE.search(line))
            matches = [m for m in ADD_RE.finditer(line)]
            if not matches:
                matches = QUOTED_IMAGE_RE.finditer(line) if is_button else []
            for match in matches:
                literal = match.group("path")
                if "%s" in literal or (match.groupdict().get("percent")):
                    continue
                if literal.startswith("http"):
                    continue
                rel = literal.replace("\\", "/").lstrip("/")
                entry = found.get(rel)
                if entry is None:
                    target = project.game_dir / rel
                    entry = ImageCandidate(rel=rel, path=target)
                    entry.missing = not target.is_file()
                    if not entry.missing and Image is not None:
                        try:
                            with Image.open(target) as img:
                                entry.width, entry.height = img.size
                        except Exception:  # noqa: BLE001
                            entry.missing = True
                    found[rel] = entry
                entry.as_imagebutton = entry.as_imagebutton or is_button
                place = f"{path.name}:{number}"
                if place not in entry.places:
                    entry.places.append(place)

            if not matches:
                for named in ADD_NAME_RE.finditer(line):
                    rel = defines.get(named.group("name"))
                    if not rel:
                        continue
                    entry = found.get(rel)
                    if entry is None:
                        target = project.game_dir / rel
                        entry = ImageCandidate(rel=rel, path=target)
                        entry.missing = not target.is_file()
                        if not entry.missing and Image is not None:
                            try:
                                with Image.open(target) as img:
                                    entry.width, entry.height = img.size
                            except Exception:  # noqa: BLE001
                                entry.missing = True
                        found[rel] = entry
                    place = f"{path.name}:{number} ({named.group('name')})"
                    if place not in entry.places:
                        entry.places.append(place)

    candidates: list[ImageCandidate] = []
    for entry in found.values():
        if entry.missing:
            continue
        if entry.width < MIN_WIDTH or entry.height < MIN_HEIGHT:
            continue
        entry.score, entry.bbox, entry.has_alpha = analyze_image(entry.path)
        if entry.score >= min_score:
            candidates.append(entry)

    candidates.sort(key=lambda c: (-c.score, c.rel))
    return candidates


# ---------------------------------------------------------------------------
# Рисование русской картинки
# ---------------------------------------------------------------------------
def default_font() -> Optional[Path]:
    base = Path(__file__).resolve().parent.parent / "assets" / "fonts"
    for name in ("PT_Serif-Bold.ttf", "PT_Serif-Regular.ttf"):
        if (base / name).is_file():
            return base / name
    return None


def _background_colour(image, box: tuple[int, int, int, int]) -> tuple[int, int, int, int]:
    """Самый частый цвет по периметру рамки — им и заливаем."""
    x0, y0, x1, y1 = box
    pixel = image.convert("RGBA").load()
    counter: Counter = Counter()
    step = max(1, (x1 - x0) // 32)
    for x in range(x0, max(x0 + 1, x1), step):
        counter[pixel[x, max(0, y0)]] += 1
        counter[pixel[x, min(image.height - 1, y1 - 1)]] += 1
    for y in range(y0, max(y0 + 1, y1), step):
        counter[pixel[max(0, x0), y]] += 1
        counter[pixel[min(image.width - 1, x1 - 1), y]] += 1
    return counter.most_common(1)[0][0]


def _wrap(draw, text: str, font, max_width: int) -> list[str]:
    lines: list[str] = []
    for paragraph in text.split("\n"):
        words = paragraph.split()
        if not words:
            lines.append("")
            continue
        current = words[0]
        for word in words[1:]:
            probe = current + " " + word
            if draw.textlength(probe, font=font) <= max_width:
                current = probe
            else:
                lines.append(current)
                current = word
        lines.append(current)
    return lines


def render_replacement(project: RenpyProject,
                       rel: str,
                       text: str,
                       box: Optional[tuple[int, int, int, int]] = None,
                       font: Optional[Path] = None,
                       size: Optional[int] = None,
                       colour: str = "#ffffff",
                       background: Optional[str] = None,
                       align: str = "center",
                       padding: int = 6) -> Path:
    """
    Нарисовать русскую версию картинки рядом с оригиналом.

    Размер холста не меняется, поэтому вёрстка игры остаётся прежней.
    """
    if Image is None:
        raise RuntimeError("нужен Pillow (pip install pillow)")

    source = project.game_dir / rel
    if not source.is_file():
        raise FileNotFoundError(source)

    score, detected, _ = analyze_image(source)
    if box is None:
        box = detected
    image = Image.open(source).convert("RGBA")
    if box is None:
        box = (0, 0, image.width, image.height)
    x0, y0, x1, y1 = (max(0, int(box[0])), max(0, int(box[1])),
                      min(image.width, int(box[2])), min(image.height, int(box[3])))
    if x1 <= x0 or y1 <= y0:
        raise ValueError("пустая рамка")

    fill = background or _rgb(_background_colour(image, (x0, y0, x1, y1)))
    draw = ImageDraw.Draw(image)
    draw.rectangle([x0, y0, x1 - 1, y1 - 1], fill=fill)

    if text.strip():
        font_path = Path(font) if font else default_font()
        if font_path is None or not Path(font_path).is_file():
            raise RuntimeError("не найден файл шрифта")
        max_width = max(8, (x1 - x0) - 2 * padding)
        max_height = max(8, (y1 - y0) - 2 * padding)
        start = int(size) if size else max(8, int(max_height * 0.7))
        chosen, lines = None, []
        for candidate_size in range(start, 7, -1):
            probe_font = ImageFont.truetype(str(font_path), candidate_size)
            probe_draw = ImageDraw.Draw(image)
            lines = _wrap(probe_draw, text, probe_font, max_width)
            line_height = int(candidate_size * 1.18) + 1
            total_height = line_height * len(lines)
            widest = max((probe_draw.textlength(line, font=probe_font)
                          for line in lines), default=0)
            if total_height <= max_height and widest <= max_width:
                chosen = probe_font
                break
        if chosen is None:
            chosen = ImageFont.truetype(str(font_path), 8)
            lines = _wrap(ImageDraw.Draw(image), text, chosen, max_width)
        line_height = int(chosen.size * 1.18) + 1
        total_height = line_height * len(lines)
        y = y0 + ((y1 - y0) - total_height) // 2
        for line in lines:
            width = ImageDraw.Draw(image).textlength(line, font=chosen)
            if align == "left":
                x = x0 + padding
            elif align == "right":
                x = x1 - padding - width
            else:
                x = x0 + ((x1 - x0) - width) / 2
            draw.text((x, y), line, font=chosen, fill=colour)
            y += line_height

    target = source.with_name(f"{source.stem}_ru{source.suffix}")
    image.save(target)
    logger.info("нарисована русская картинка: %s", target.name)
    return target


def _rgb(colour) -> tuple[int, int, int, int]:
    if isinstance(colour, tuple):
        return colour if len(colour) == 4 else (colour[0], colour[1], colour[2], 255)
    if isinstance(colour, str) and colour.startswith("#") and len(colour) == 7:
        return (int(colour[1:3], 16), int(colour[3:5], 16),
                int(colour[5:7], 16), 255)
    return (0, 0, 0, 255)


# ---------------------------------------------------------------------------
# Реестр замен
# ---------------------------------------------------------------------------
def registry_path(project: RenpyProject, language: str) -> Path:
    return project.game_dir / TL_DIRECTORY / language / REGISTRY_NAME


def load_registry(project: RenpyProject, language: str) -> dict:
    path = registry_path(project, language)
    if not path.is_file():
        return {"items": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {"items": {}}
    if not isinstance(data, dict):
        return {"items": {}}
    data.setdefault("items", {})
    return data


def save_registry(project: RenpyProject, language: str, data: dict) -> Path:
    path = registry_path(project, language)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8")
    return path


def register(project: RenpyProject, language: str, rel: str, rel_new: str,
             **params) -> dict:
    data = load_registry(project, language)
    data["items"][rel] = {"ru": rel_new, **params}
    save_registry(project, language, data)
    return data


# ---------------------------------------------------------------------------
# Подключение к экранам: литерал → rt_img("литерал")
# ---------------------------------------------------------------------------
def _backup_dir(project: RenpyProject, language: str) -> Path:
    return project.game_dir / TL_DIRECTORY / language / BACKUP_DIR


def patch_references(project: RenpyProject, language: str,
                     rels: Iterable[str]) -> list[str]:
    """
    Заменить литералы картинок на ``rt_img("...")`` в экранах игры.

    Возвращает список изменённых файлов (относительно ``game/``).
    """
    wanted = set(rels)
    if not wanted:
        return []
    changed: list[str] = []
    defines = collect_defines(project)
    by_name = {name: rel for name, rel in defines.items() if rel in wanted}
    backup = _backup_dir(project, language)
    backup.mkdir(parents=True, exist_ok=True)

    for path in _iter_project_rpy(project):
        try:
            original = path.read_text(encoding="utf-8", errors="surrogateescape")
        except OSError:
            continue
        lines = original.splitlines(keepends=True)
        in_screen = False
        touched = False
        for index, line in enumerate(lines):
            if SCREEN_RE.match(line):
                in_screen = True
                continue
            if in_screen and line.strip() and not line[:1].isspace():
                in_screen = False
            if not in_screen:
                continue
            new_line = line
            for match in list(QUOTED_IMAGE_RE.finditer(line)):
                literal = match.group("path")
                rel = literal.replace("\\", "/").lstrip("/")
                if rel not in wanted:
                    continue
                if 'rt_img(' in line:
                    continue
                quote = match.group(0)[0]
                new_line = new_line.replace(f"{quote}{literal}{quote}",
                                            f'rt_img("{literal}")', 1)
                touched = True
            if by_name and "rt_img(" not in new_line:
                named = ADD_NAME_RE.search(new_line)
                if named and named.group("name") in by_name:
                    new_line = new_line.replace(
                        named.group("name"),
                        f"rt_img({named.group('name')})", 1)
                    touched = True
            if touched and new_line != line:
                lines[index] = new_line
        if touched:
            target = backup / (path.name + BACKUP_SUFFIX)
            if not target.exists():
                shutil.copy2(path, target)
            path.write_text("".join(lines), encoding="utf-8",
                            errors="surrogateescape")
            changed.append(project.relative(path))
            logger.info("экран обновлён: %s", path.name)
    return changed


def revert_references(project: RenpyProject, language: str) -> list[str]:
    """Вернуть изменённые экраны из rt_images_backup и удалить русские PNG."""
    restored: list[str] = []
    backup = _backup_dir(project, language)
    if backup.is_dir():
        for saved in sorted(backup.glob("*" + BACKUP_SUFFIX)):
            name = saved.name[:-len(BACKUP_SUFFIX)]
            target = project.game_dir / name
            shutil.copy2(saved, target)
            restored.append(project.relative(target))
            saved.unlink()
    data = load_registry(project, language)
    for rel, info in list(data.get("items", {}).items()):
        ru = project.game_dir / str(info.get("ru", ""))
        if ru.is_file():
            try:
                ru.unlink()
            except OSError:
                pass
    data["items"] = {}
    save_registry(project, language, data)
    helper = project.game_dir / TL_DIRECTORY / language / HELPER_NAME
    if helper.is_file():
        helper.unlink()
    return restored


# ---------------------------------------------------------------------------
# Служебный rpy с функцией rt_img
# ---------------------------------------------------------------------------
def build_helper_rpy(language: str, items: dict) -> str:
    mapping = {rel: str(info.get("ru") or "") for rel, info in items.items()}
    mapping = {rel: ru for rel, ru in mapping.items() if ru}
    raw = json.dumps(mapping, ensure_ascii=False, indent=4)
    payload = "\n".join("    " + row if row else row
                         for row in raw.splitlines())
    payload = payload.lstrip()
    return (
        '"""Русские картинки вместо нарисованного текста.\n\n'
        "Создано RenPyTranslator. Файл можно удалить — игра вернётся\n"
        'к оригинальным картинкам."""\n\n'
        "init 150 python:\n\n"
        f"    _rt_image_language = {language!r}\n\n"
        f"    _rt_images = {payload}\n\n"
        "    def rt_img(path):\n"
        '        """Русский файл картинки вместо оригинала.\n\n'
        "        Возвращает оригинал, если включён другой язык или замены нет.\n"
        '        """\n'
        "        language = None\n"
        "        try:\n"
        "            language = _preferences.language or config.language\n"
        "        except Exception:\n"
        "            try:\n"
        "                language = config.language\n"
        "            except Exception:\n"
        "                language = None\n"
        "        if language != _rt_image_language:\n"
        "            return path\n"
        "        repl = _rt_images.get(path)\n"
        "        if repl is None:\n"
        "            repl = _rt_images.get(path.replace('\\\\', '/'))\n"
        "        return repl or path\n"
    )


def write_helper(project: RenpyProject, language: str) -> Optional[Path]:
    data = load_registry(project, language)
    items = data.get("items") or {}
    path = project.game_dir / TL_DIRECTORY / language / HELPER_NAME
    if not items:
        if path.is_file():
            path.unlink()
        return None
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(build_helper_rpy(language, items), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Полный проход: нарисовать + подключить
# ---------------------------------------------------------------------------
@dataclass
class ReplaceResult:
    rel: str
    rel_new: str = ""
    changed_files: list[str] = field(default_factory=list)
    helper: Optional[Path] = None

    def summary(self) -> str:
        text = f"{self.rel} → {self.rel_new}"
        if self.changed_files:
            text += f" (подключено в {', '.join(self.changed_files)})"
        return text


def replace(project: RenpyProject, language: str, rel: str, text: str,
            **kwargs) -> ReplaceResult:
    """Нарисовать русскую картинку и подключить её вместо оригинала."""
    target = render_replacement(project, rel, text, **kwargs)
    rel_new = project.relative(target)
    params = {key: value for key, value in kwargs.items()
              if key in ("box", "size", "colour", "background", "align")}
    params["text"] = text
    register(project, language, rel, rel_new, **params)
    changed = patch_references(project, language, [rel])
    helper = write_helper(project, language)
    try:
        from core.changelog import record

        record(project, language, "image_replace", project.game_dir / rel,
               f"{rel} → {rel_new}")
    except Exception:  # noqa: BLE001
        pass
    return ReplaceResult(rel=rel, rel_new=rel_new, changed_files=changed,
                         helper=helper)


def report(project: RenpyProject, language: str,
           min_score: float = 0.35) -> list[str]:
    """Текстовый отчёт «что нарисовано картинками»."""
    candidates = find_candidates(project, min_score=min_score)
    lines = ["Картинки с текстом (по экранам игры)", ""]
    if not candidates:
        return lines + ["не найдено — в экранах нет надписей-картинок"]
    for cand in candidates:
        lines.append(f"• {cand.rel}  ({cand.width}×{cand.height}) — "
                     f"{cand.verdict()}, ссылки: {', '.join(cand.places)}")
    lines += ["", "Чтобы заменить: вкладка «Текст-картинки» → выбрать файл → "
                  "вписать русский текст → «Сделать русскую картинку» → "
                  "«Применить к игре»."]
    return lines
