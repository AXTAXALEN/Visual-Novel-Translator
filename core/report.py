"""
HTML-отчёт о прогоне (v3.0): один файл, открывается в браузере, без интернета.

Зачем: после перевода полезно видеть не только «готово», но и **что именно**
получилось — сколько строк переведено, что осталось непереведённым, какие
строки потеряли плейсхолдеры, сколько блоков не попало в tl. Отчёт
самодостаточен (стили внутри), его можно отправить второму человеку.
"""

from __future__ import annotations

import html
import time
from pathlib import Path
from typing import Any, Optional

from config import APP_NAME, VERSION
from core.utils import get_logger

logger = get_logger(__name__)

_MAX_ROWS = 250


def _badge(severity: str) -> str:
    colors = {"error": "#e5484d", "warning": "#f5a623", "info": "#5b8def"}
    label = {"error": "ошибка", "warning": "внимание", "info": "заметка"}
    color = colors.get(severity, "#8b8b8b")
    return (f'<span class="badge" style="background:{color}">'
            f'{label.get(severity, severity)}</span>')


def _clip(text: str, limit: int = 160) -> str:
    text = (text or "").replace("\n", "⏎")
    if len(text) > limit:
        text = text[:limit] + "…"
    return html.escape(text)


def _card(title: str, value: Any, hint: str = "") -> str:
    return (f'<div class="card"><div class="card-value">{html.escape(str(value))}</div>'
            f'<div class="card-title">{html.escape(title)}</div>'
            f'<div class="card-hint">{html.escape(hint)}</div></div>')


def _quality_rows(quality) -> str:
    if quality is None or not getattr(quality, "issues", None):
        return '<p class="empty">Проблем качества не найдено.</p>'
    rows = []
    for issue in quality.sorted_issues()[:_MAX_ROWS]:
        rows.append(
            "<tr>"
            f"<td>{_badge(issue.severity)}</td>"
            f"<td>{html.escape(issue.label())}</td>"
            f"<td class='mono'>{_clip(issue.original, 120)}</td>"
            f"<td class='mono'>{_clip(issue.translation, 120)}</td>"
            f"<td class='where'>{html.escape(issue.where or '—')}</td>"
            "</tr>")
    extra = len(quality.issues) - _MAX_ROWS
    note = (f'<p class="empty">Показаны первые {_MAX_ROWS} из {len(quality.issues)}.'
            '</p>') if extra > 0 else ""
    return (note + "<table><thead><tr><th></th><th>Проблема</th><th>Оригинал</th>"
            "<th>Перевод</th><th>Где</th></tr></thead><tbody>"
            + "".join(rows) + "</tbody></table>")


def _failed_rows(translate_report, items=None) -> str:
    """Список строк, которые остались на языке оригинала (файл:строка)."""
    failed = list(getattr(translate_report, "failed_texts", []) or [])
    if not failed:
        return ""
    places: dict[str, str] = {}
    for item in items or []:
        text = getattr(item, "original", "")
        if text in failed and text not in places:
            file = getattr(item, "file", "") or ""
            line = getattr(item, "line", 0) or 0
            places[text] = f"{file}:{line}" if file else ""
    rows = []
    for text in failed[:_MAX_ROWS]:
        rows.append(
            "<tr>"
            f"<td class='mono'>{_clip(text, 140)}</td>"
            f"<td class='where'>{html.escape(places.get(text, '') or '—')}</td>"
            "</tr>")
    extra = len(failed) - _MAX_ROWS
    note = (f'<p class="empty">Показаны первые {_MAX_ROWS} из {len(failed)}.</p>'
            if extra > 0 else "")
    return (note + f"<p class=\"empty\">Эти {len(failed)} строк остались на языке "
            "оригинала — обычно это сбой сети или лимит бесплатного сервиса. "
            "Запустите перевод ещё раз: готовое возьмётся из кэша, а эти строки "
            "программа попробует перевести снова.</p>"
            "<table><thead><tr><th>Строка</th><th>Где</th></tr></thead>"
            "<tbody>" + "".join(rows) + "</tbody></table>")


def _verify_rows(verify) -> str:
    if verify is None or not getattr(verify, "issues", None):
        return '<p class="empty">Каталог перевода в порядке.</p>'
    rows = []
    for issue in verify.issues[:_MAX_ROWS]:
        rows.append(
            "<tr>"
            f"<td>{_badge(issue.severity)}</td>"
            f"<td>{html.escape(issue.label())}</td>"
            f"<td>{_clip(issue.message, 160)}</td>"
            f"<td class='where'>{html.escape(issue.where() or '—')}</td>"
            "</tr>")
    return ("<table><thead><tr><th></th><th>Тип</th><th>Что не так</th>"
            "<th>Где</th></tr></thead><tbody>" + "".join(rows)
            + "</tbody></table>")


def build_html_report(*, game_name: str, game_path: str, language: str,
                      translate_report=None, quality=None, verify=None,
                      build_result=None, font_report=None,
                      items=None) -> str:
    """Собрать HTML-отчёт как строку."""
    strings = getattr(translate_report, "unique_texts", 0) or 0
    translated = getattr(translate_report, "translated", 0) or 0
    cached = getattr(translate_report, "from_cache", 0) or 0
    failed = getattr(translate_report, "failed", 0) or 0
    blocks = getattr(build_result, "blocks", 0) or 0
    ui_strings = getattr(build_result, "strings", 0) or 0
    coverage = getattr(verify, "coverage", 0.0) if verify is not None else 0.0
    quality_errors = len(quality.errors) if quality is not None else 0
    quality_warnings = len(quality.warnings) if quality is not None else 0
    verify_errors = len(verify.errors) if verify is not None else 0

    status_color = "#2ea043" if (quality_errors == 0 and verify_errors == 0) else "#f5a623"
    status_text = ("Всё чисто" if (quality_errors == 0 and verify_errors == 0)
                   else "Есть замечания")

    fonts_line = ""
    if font_report is not None:
        replaced = len(getattr(font_report, "replaced", []) or [])
        fonts_line = (f"<li>Шрифты: заменено на PT Serif — {replaced}, "
                      f"скопировано файлов — "
                      f"{len(getattr(font_report, 'copied', []) or [])}</li>")

    return f"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(APP_NAME)} — отчёт по переводу {html.escape(game_name)}</title>
<style>
  :root {{ color-scheme: light dark; }}
  * {{ box-sizing: border-box; }}
  body {{ margin: 0; padding: 32px; font: 15px/1.5 -apple-system, "Segoe UI",
         Roboto, "Helvetica Neue", Arial, sans-serif; background: #0f1116;
         color: #e6e8ee; }}
  h1 {{ font-size: 24px; margin: 0 0 4px; }}
  h2 {{ font-size: 18px; margin: 32px 0 12px; font-weight: 600; }}
  .sub {{ color: #9aa3b2; font-size: 14px; }}
  .status {{ display: inline-block; padding: 4px 12px; border-radius: 999px;
             background: {status_color}22; color: {status_color};
             border: 1px solid {status_color}55; font-size: 13px; }}
  .cards {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
            gap: 12px; margin-top: 20px; }}
  .card {{ background: #171a21; border: 1px solid #232733; border-radius: 12px;
           padding: 14px 16px; }}
  .card-value {{ font-size: 26px; font-weight: 700; }}
  .card-title {{ font-size: 13px; color: #b9c0cd; margin-top: 2px; }}
  .card-hint {{ font-size: 12px; color: #6f7887; margin-top: 4px; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 13px;
           background: #14171d; border-radius: 12px; overflow: hidden; }}
  th {{ text-align: left; padding: 10px 12px; background: #1b1f27; color: #9aa3b2;
        font-weight: 600; position: sticky; top: 0; }}
  td {{ padding: 9px 12px; border-top: 1px solid #20242e; vertical-align: top; }}
  tr:hover td {{ background: #181c24; }}
  .mono {{ font-family: ui-monospace, "Cascadia Mono", Consolas, monospace;
           font-size: 12.5px; color: #cfd6e4; }}
  .where {{ font-family: ui-monospace, monospace; font-size: 12px; color: #8b93a3;
            white-space: nowrap; }}
  .badge {{ display: inline-block; padding: 2px 8px; border-radius: 999px;
            color: #fff; font-size: 11px; font-weight: 600; }}
  .empty {{ color: #7c8493; font-size: 14px; }}
  ul {{ margin: 8px 0 0; padding-left: 20px; color: #b9c0cd; }}
  footer {{ margin-top: 36px; color: #6f7887; font-size: 12px; }}
  code {{ background: #1b1f27; padding: 1px 5px; border-radius: 5px; }}
</style>
</head>
<body>
  <h1>Отчёт о переводе</h1>
  <div class="sub">{html.escape(game_name)} · язык <b>{html.escape(language)}</b> ·
      {html.escape(game_path)}</div>
  <p><span class="status">{status_text}</span></p>

  <div class="cards">
    {_card("Строк в игре", strings, "уникальных фрагментов")}
    {_card("Переведено", translated, "в этом прогоне")}
    {_card("Из кэша", cached, "без обращения к сервису")}
    {_card("Ошибок перевода", failed, "строк не удалось перевести")}
    {_card("Блоков в tl", blocks, f"строк интерфейса: {ui_strings}")}
    {_card("Покрытие", f"{coverage * 100:.1f}%", "доля переведённых блоков")}
    {_card("Качество", f"{quality_errors} / {quality_warnings}", "ошибок / предупреждений")}
    {_card("Проверка tl", verify_errors, "критичных проблем в файлах")}
  </div>

  <h2>Строки без перевода</h2>
  {_failed_rows(translate_report, items) or '<p class="empty">Все найденные строки переведены.</p>'}

  <h2>Качество перевода</h2>
  {_quality_rows(quality)}

  <h2>Проверка файлов перевода</h2>
  {_verify_rows(verify)}

  <h2>Что дальше</h2>
  <ul>
    <li>Строки с ошибкой «потерян плейсхолдер» программа отдаёт оригиналом —
        лучше английская реплика, чем сломанная разметка.</li>
    <li>Раздел «Текст для агента» → выгрузите .txt/.docx, попросите агента
        заполнить <code>RU:</code>, затем загрузите файл обратно.</li>
    <li>Проверить игру: запустите её — язык включится автоматически
        (<code>zz_renpy_translator_language.rpy</code>).</li>
    {fonts_line}
  </ul>

  <footer>{html.escape(APP_NAME)} {html.escape(VERSION)} ·
      отчёт создан {time.strftime("%d.%m.%Y %H:%M")}</footer>
</body>
</html>
"""


def write_report(path: Path, **kwargs) -> Path:
    """Записать отчёт в файл."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(build_html_report(**kwargs), encoding="utf-8")
    logger.info("Отчёт сохранён: %s", path)
    return path


def default_report_path(project_root: Optional[Path] = None) -> Path:
    """Куда писать отчёт по умолчанию."""
    stamp = time.strftime("%Y%m%d-%H%M%S")
    base = Path(project_root) if project_root else Path.cwd()
    return base / f"renpy_translator_report_{stamp}.html"
