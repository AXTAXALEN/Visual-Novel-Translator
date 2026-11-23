#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Тесты RenPyTranslator (без pytest — обычный unittest).

Запуск::

    python tests/run_tests.py            # все тесты
    python tests/run_tests.py -v         # подробно

Тесты используют фикстуру ``tests/fixtures/sample_game`` — маленькую, но
«злую» новеллу: меню с условиями, NVL, extend, тройные кавычки, вложенные
метки, интерполяции, теги, экранированные фигурные скобки, screen language,
``_()``-строки, Character-имена, python-вставки.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
import unittest
from typing import Optional
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from config import Settings  # noqa: E402
from core.cache import TranslationCache  # noqa: E402
from core.exporter import export_json, export_txt  # noqa: E402
from core.importer import import_translations, parse_txt  # noqa: E402
from core.models import BLOCK_KINDS, KIND_MENU, KIND_UI  # noqa: E402
from core.placeholder import (  # noqa: E402
    TOKEN_ALT,
    protect,
    restore,
    verify,
)
from core.rpa_extractor import extract_native, read_index  # noqa: E402
from core.rpy_parser import (  # noqa: E402
    IdRegistry,
    build_logical_lines,
    decode_string_body,
    encode_say_string,
    parse_project,
    parse_say_statement,
)
from core.scanner import scan_project  # noqa: E402
from core.tl_writer import build_say_code, build_tl  # noqa: E402

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "sample_game"


def copy_fixture() -> Path:
    tmp = Path(tempfile.mkdtemp(prefix="rpt_test_"))
    target = tmp / "game_project"
    shutil.copytree(FIXTURE, target)
    return target


class TempDirMixin:
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="rpt_test_"))
        self.project_dir = self.tmp / "sample_game"
        shutil.copytree(FIXTURE, self.project_dir)
        self.settings = Settings(
            game_path=self.project_dir / "game",
            cache_path=self.tmp / "cache.json",
            log_path=self.tmp / "test.log",
            engine="mock",
            backup=False,
        )

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)


# ===========================================================================
# 1. Строковые правила Ren'Py
# ===========================================================================
class TestStringRules(unittest.TestCase):
    def test_decode_escapes(self):
        self.assertEqual(decode_string_body(r"a\nb"), "a\nb")
        self.assertEqual(decode_string_body(r"say \"hi\""), 'say "hi"')
        self.assertEqual(decode_string_body(r"back\\slash"), "back\\slash")

    def test_decode_brace_escapes(self):
        # Ren'Py: \{ -> {{  (экранированная фигурная скобка)
        self.assertEqual(decode_string_body(r"use {braces\}"), "use {braces}")

    def test_decode_whitespace_collapse(self):
        self.assertEqual(decode_string_body("a  b\n   c"), "a b c")

    def test_encode_say_string(self):
        self.assertEqual(encode_say_string("hello"), '"hello"')
        self.assertEqual(encode_say_string('say "hi"'), '"say \\"hi\\""')
        self.assertEqual(encode_say_string("a\nb"), '"a\\nb"')

    def test_encode_double_space(self):
        self.assertEqual(encode_say_string("a  b"), '"a \\ b"')


# ===========================================================================
# 2. Разбор инструкций
# ===========================================================================
class TestSayParsing(unittest.TestCase):
    def test_simple_dialogue(self):
        parsed = parse_say_statement('e "Hello"')
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed["who"], "e")
        self.assertEqual(parsed["what_tok"].value, "Hello")

    def test_narration(self):
        parsed = parse_say_statement('"Just narration."')
        self.assertIsNotNone(parsed)
        self.assertIsNone(parsed["who"])

    def test_attributes_and_with(self):
        parsed = parse_say_statement('e happy "Hi" with dissolve')
        self.assertEqual(parsed["attributes"], ["happy"])
        self.assertEqual(parsed["with"], "dissolve")

    def test_nointeract(self):
        parsed = parse_say_statement('e "Hi" nointeract')
        self.assertFalse(parsed["interact"])

    def test_explicit_id(self):
        parsed = parse_say_statement('e "Hi" id my_id')
        self.assertEqual(parsed["explicit_id"], "my_id")

    def test_engine_statements_not_says(self):
        for line in (
            'play music "theme.ogg"',
            'show eileen happy',
            'scene bg room with fade',
            '$ x = "text"',
            'jump start',
            'voice "a.ogg"',
            'image bg park = "park.png"',
            'if flag == "yes":',
        ):
            with self.subTest(line=line):
                self.assertIsNone(parse_say_statement(line))


# ===========================================================================
# 3. Идентификаторы translate-блоков
# ===========================================================================
class TestBlockIds(TempDirMixin, unittest.TestCase):
    def _parsed(self):
        project = scan_project(self.settings.game_path)
        return project, parse_project(project, self.settings)

    def test_formula_matches_renpy(self):
        project, parsed = self._parsed()
        items = [i for i in parsed.items if i.original == "Hello, how are you?"]
        self.assertEqual(len(items), 1)
        code = 'e "Hello, how are you?"'
        expected = "start_" + hashlib.md5((code + "\r\n").encode("utf-8")).hexdigest()[:8]
        self.assertEqual(items[0].say.block_id, expected)

    def test_duplicate_says_get_suffix(self):
        project, parsed = self._parsed()
        ids = [i.say.block_id for i in parsed.items
               if i.file == "script.rpy" and i.original == "..."]
        self.assertEqual(len(ids), 2)
        self.assertNotEqual(ids[0], ids[1])
        self.assertTrue(ids[1].endswith("_1"))

    def test_ids_deterministic(self):
        _, first = self._parsed()
        _, second = self._parsed()
        a = [(i.file, i.line, i.say.block_id if i.say else "") for i in first.items]
        b = [(i.file, i.line, i.say.block_id if i.say else "") for i in second.items]
        self.assertEqual(a, b)

    def test_explicit_id_wins(self):
        _, parsed = self._parsed()
        item = [i for i in parsed.items if i.original.startswith("Inner local")][0]
        self.assertEqual(item.say.block_id, "inner_explicit_id")

    def test_label_prefix(self):
        _, parsed = self._parsed()
        for item in parsed.items:
            if item.kind in BLOCK_KINDS and item.label and not item.say.explicit_id:
                self.assertTrue(
                    item.say.block_id.startswith(item.label.replace(".", "_")),
                    f"{item.say.block_id} не начинается с {item.label}",
                )


# ===========================================================================
# 4. Извлечение текста
# ===========================================================================
class TestExtraction(TempDirMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.project = scan_project(self.settings.game_path)
        self.parsed = parse_project(self.project, self.settings)

    def test_kind_counts(self):
        stats = self.parsed.stats()
        self.assertEqual(stats["dialogue"], 12)
        self.assertEqual(stats["narration"], 4)
        self.assertEqual(stats["extend"], 1)
        self.assertEqual(stats["menu"], 3)
        self.assertEqual(stats["ui"], 9)
        self.assertEqual(stats["name"], 2)

    def test_ui_strings(self):
        originals = {i.original for i in self.parsed.items if i.kind == KIND_UI}
        self.assertIn("Start", originals)
        self.assertIn("Load", originals)
        self.assertIn("Narrator", originals)      # из _("Narrator")
        self.assertIn("A short visual novel about fluffy adventures.", originals)
        # свойства экранов не переводим
        self.assertNotIn("what", originals)
        self.assertNotIn("button", originals)

    def test_menu_captions(self):
        captions = {i.original for i in self.parsed.items if i.kind == KIND_MENU}
        self.assertEqual(captions, {"Ask about the weather", "Say nothing", "Leave"})

    def test_python_code_ignored(self):
        for item in self.parsed.items:
            self.assertNotIn("Python-коде", item.original)

    def test_character_names(self):
        names = {i.original for i in self.parsed.items if i.kind == "name"}
        self.assertIn("Eileen", names)

    def test_context_filled(self):
        item = [i for i in self.parsed.items if i.original == "Hello, how are you?"][0]
        self.assertTrue(item.context_before)
        self.assertTrue(item.context_after)

    def test_interpolations_kept_verbatim(self):
        item = [i for i in self.parsed.items if "[player_name]" in i.original][0]
        self.assertIn("{b}", item.original)
        self.assertIn("{/b}", item.original)


# ===========================================================================
# 5. Плейсхолдеры
# ===========================================================================
class TestPlaceholders(unittest.TestCase):
    def test_interpolation_and_tags(self):
        text = "Hi, [player_name]! {b}Bold{/b} and {color=#f00}red{/color}."
        p = protect(text)
        self.assertNotIn("[player_name]", p.masked)
        self.assertNotIn("{color", p.masked)
        self.assertTrue(verify(p.masked, p))
        result = restore(p.masked, p)
        self.assertEqual(result.text, text)

    def test_printf_and_escapes(self):
        text = r"%s took %d items\nand %% done"
        p = protect(text)
        self.assertTrue(verify(p.masked, p))
        self.assertEqual(restore(p.masked, p).text, text)

    def test_escaped_braces(self):
        text = "literal {{ and [[ here"
        p = protect(text)
        self.assertEqual(restore(p.masked, p).text, text)

    def test_lost_token_repaired(self):
        text = "Hello {b}world{/b}"
        p = protect(text)
        broken = p.masked.replace(p.masked[-7:], "")  # «переводчик съел» хвост
        self.assertFalse(verify(broken, p))
        result = restore(broken, p)
        self.assertTrue(result.repaired)
        self.assertIn("Hello", result.text)

    def test_alt_template(self):
        text = "Take [item] now"
        p = protect(text, TOKEN_ALT)
        self.assertIn("[[PH0]]", p.masked)
        self.assertEqual(restore(p.masked, p).text, text)


# ===========================================================================
# 6. Кэш
# ===========================================================================
class TestCache(TempDirMixin, unittest.TestCase):
    def test_roundtrip(self):
        cache = TranslationCache(self.tmp / "c.json")
        cache.set("Hello", "Привет")
        cache.save()
        self.assertTrue((self.tmp / "c.json").exists())

        cache2 = TranslationCache(self.tmp / "c.json")
        self.assertEqual(cache2.get("Hello"), "Привет")
        self.assertEqual(len(cache2), 1)

    def test_second_run_uses_cache(self):
        from core.pipeline import Pipeline

        pipeline = Pipeline(self.settings)
        pipeline.scan()
        parsed = pipeline.analyze()
        first = pipeline.translate(parsed, show_progress=False)
        self.assertGreater(first.translated, 0)
        self.assertEqual(first.failed, 0)

        pipeline2 = Pipeline(self.settings)
        pipeline2.scan()
        parsed2 = pipeline2.analyze()
        second = pipeline2.translate(parsed2, show_progress=False)
        self.assertEqual(second.translated, 0)
        self.assertEqual(second.from_cache, first.translated + first.from_cache)


# ===========================================================================
# 7. Генерация tl/
# ===========================================================================
class TestTLWriter(TempDirMixin, unittest.TestCase):
    def _build(self):
        from core.pipeline import Pipeline

        pipeline = Pipeline(self.settings)
        project = pipeline.scan()
        parsed = pipeline.analyze()
        pipeline.translate(parsed, show_progress=False)
        result = build_tl(project, parsed.items, "russian")
        return project, parsed, result

    def test_files_created(self):
        project, parsed, result = self._build()
        tl_dir = project.game_dir / "tl" / "russian"
        self.assertTrue(tl_dir.is_dir())
        self.assertTrue((tl_dir / "script.rpy").exists())
        self.assertTrue((tl_dir / "zz_renpy_translator_strings.rpy").exists())
        self.assertGreater(result.blocks, 10)
        self.assertGreater(result.strings, 5)

    def test_blocks_are_valid_and_match_ids(self):
        project, parsed, result = self._build()
        tl_dir = project.game_dir / "tl" / "russian"
        # только блоки перевода (файл строк движка сюда не относится)
        text = "\n".join(p.read_text("utf-8")
                         for p in sorted(tl_dir.glob("*.rpy"))
                         if "engine" not in p.name)
        expected = {i.say.block_id for i in parsed.items if i.say and i.is_translated()}
        for block_id in expected:
            self.assertIn(f"translate russian {block_id}:", text)

        # все инструкции внутри блоков имеют отступ 4 пробела
        for line in text.split("\n"):
            if line and not line.startswith("#") and not line.startswith("translate"):
                self.assertTrue(line.startswith("    "), repr(line))

    def test_strings_are_deduplicated(self):
        project, parsed, result = self._build()
        text = (project.game_dir / "tl" / "russian" /
                "zz_renpy_translator_strings.rpy").read_text("utf-8")
        olds = [l for l in text.split("\n") if l.strip().startswith("old ")]
        self.assertEqual(len(olds), len(set(olds)))

    def test_generated_file_is_parseable(self):
        project, parsed, result = self._build()
        tl_file = project.game_dir / "tl" / "russian" / "script.rpy"
        lines = build_logical_lines(tl_file.read_text("utf-8"))
        self.assertTrue(lines)
        registry = IdRegistry()
        parsed_tl = parse_project(project, self.settings, [tl_file])
        # внутри translate-блоков ничего не извлекаем повторно
        self.assertEqual(parsed_tl.items, [])

    def test_preserves_tags_and_vars(self):
        project, parsed, result = self._build()
        text = (project.game_dir / "tl" / "russian" / "script.rpy").read_text("utf-8")
        self.assertIn("[player_name]", text)
        self.assertIn("{b}Really{/b}", text)
        self.assertIn("with dissolve", text)
        self.assertIn("e happy ", text)

    def test_say_code_rebuilt(self):
        project, parsed, result = self._build()
        item = [i for i in parsed.items if i.original == "Hello, how are you?"][0]
        code = build_say_code(item, "Привет, как дела?")
        self.assertEqual(code, 'e "Привет, как дела?"')

    def test_hook_and_language_files(self):
        from core.pipeline import Pipeline

        pipeline = Pipeline(self.settings)
        project = pipeline.scan()
        parsed = pipeline.analyze()
        pipeline.translate(parsed, show_progress=False)
        build_tl(project, parsed.items, "russian",
                 set_default_language=True, force_language=False,
                 hook_fallback=True)
        tl_dir = project.game_dir / "tl" / "russian"
        lang_file = tl_dir / "zz_renpy_translator_language.rpy"
        hook_file = tl_dir / "zz_renpy_translator_hook.rpy"
        self.assertTrue(lang_file.exists())
        self.assertTrue(hook_file.exists())
        self.assertIn('config.default_language = "russian"',
                      lang_file.read_text("utf-8"))
        self.assertIn("say_menu_text_filter", hook_file.read_text("utf-8"))


# ===========================================================================
# 8. Прямая замена
# ===========================================================================
class TestReplaceMode(TempDirMixin, unittest.TestCase):
    def _run(self):
        from core.pipeline import Pipeline

        self.settings.mode = "replace"
        pipeline = Pipeline(self.settings)
        pipeline.scan()
        parsed = pipeline.analyze()
        pipeline.translate(parsed, show_progress=False)
        tl_result, replace_result = pipeline.build(parsed, mode="replace")
        return parsed, replace_result

    def test_replacement_applied(self):
        parsed, result = self._run()
        self.assertGreater(result.replacements, 20)
        text = (self.project_dir / "game" / "script.rpy").read_text("utf-8")
        self.assertIn("[ru] ", text)
        self.assertNotIn('e "Hello, how are you?"', text)

    def test_structure_preserved(self):
        parsed, result = self._run()
        project = scan_project(self.project_dir)
        reparsed = parse_project(project, self.settings)
        # количество и типы строк не изменились
        self.assertEqual(reparsed.stats()["total"], parsed.stats()["total"])
        self.assertEqual(reparsed.stats()["menu"], parsed.stats()["menu"])

    def test_tags_and_vars_survive(self):
        parsed, result = self._run()
        text = (self.project_dir / "game" / "script.rpy").read_text("utf-8")
        self.assertIn("[player_name]", text)
        self.assertIn("{b}Really{/b}", text)
        self.assertIn("with dissolve", text)

    def test_menu_conditions_survive(self):
        parsed, result = self._run()
        text = (self.project_dir / "game" / "script.rpy").read_text("utf-8")
        self.assertIn('if points > 0:', text)

    def test_screens_strings_replaced(self):
        parsed, result = self._run()
        text = (self.project_dir / "game" / "screens.rpy").read_text("utf-8")
        # [ru] — «чужая» скобка из перевода: экранируется, чтобы игра не упала
        self.assertIn('textbutton _("[[ru] Начать")', text)
        # а настоящая интерполяция из оригинала остаётся рабочей
        self.assertIn("[config.version]", text)
        self.assertIn('text what id "what"', text)


# ===========================================================================
# 9. Экспорт / импорт
# ===========================================================================
class TestExportImport(TempDirMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.project = scan_project(self.settings.game_path)
        self.parsed = parse_project(self.project, self.settings)

    def test_json_export_schema(self):
        out = self.tmp / "export.json"
        result = export_json(self.project, self.parsed, out)
        data = json.loads(out.read_text("utf-8"))

        self.assertEqual(result.count, len(self.parsed.items))
        self.assertIn("meta", data)
        self.assertIn("strings", data)
        self.assertEqual(data["meta"]["total_strings"], len(data["strings"]))
        for key in ("id", "file", "line", "label", "character", "original",
                    "translation", "context_before", "context_after"):
            self.assertIn(key, data["strings"][0])

    def test_json_roundtrip(self):
        out = self.tmp / "export.json"
        export_json(self.project, self.parsed, out)
        data = json.loads(out.read_text("utf-8"))
        for entry in data["strings"]:
            entry["translation"] = "RU:" + entry["original"]
        filled = self.tmp / "filled.json"
        filled.write_text(json.dumps(data, ensure_ascii=False), "utf-8")

        project = scan_project(self.settings.game_path)
        parsed = parse_project(project, self.settings)
        result = import_translations(filled, parsed)
        self.assertEqual(result.by_id, len(parsed.items))
        self.assertEqual(result.missing, 0)
        self.assertTrue(all(i.is_translated() for i in parsed.items))

    def test_txt_export_and_import(self):
        out = self.tmp / "export.txt"
        export_txt(self.project, self.parsed, out)
        text = out.read_text("utf-8")
        self.assertIn("EN: Hello, how are you?", text)
        self.assertIn("RU: ", text)

        # «переводчик» заполняет строки RU:
        filled_lines = []
        for line in text.split("\n"):
            if line.startswith("RU: "):
                filled_lines.append("RU: ПРИВЕТ")
            else:
                filled_lines.append(line)
        filled = self.tmp / "filled.txt"
        filled.write_text("\n".join(filled_lines), "utf-8")

        mapping = parse_txt(filled)
        self.assertTrue(any(v == "ПРИВЕТ" for v in mapping.values()))

        project = scan_project(self.settings.game_path)
        parsed = parse_project(project, self.settings)
        result = import_translations(filled, parsed)
        self.assertGreater(result.applied, 10)

    def test_txt_multiline_translation(self):
        txt = (
            "=== a.rpy | label: start ===\n\n"
            "[#a.rpy:1] (e)\n"
            "EN: Hello\n"
            "RU: Привет,\n"
            "    как дела?\n\n"
        )
        path = self.tmp / "multi.txt"
        path.write_text(txt, "utf-8")
        mapping = parse_txt(path)
        self.assertEqual(mapping["a.rpy:1"], "Привет,\nкак дела?")

    def test_import_and_build_end_to_end(self):
        from core.pipeline import Pipeline

        out = self.tmp / "export.json"
        export_json(self.project, self.parsed, out)
        data = json.loads(out.read_text("utf-8"))
        for entry in data["strings"]:
            entry["translation"] = "ГОТОВО " + entry["original"]
        filled = self.tmp / "filled.json"
        filled.write_text(json.dumps(data, ensure_ascii=False), "utf-8")

        pipeline = Pipeline(self.settings)
        project = pipeline.scan()
        pipeline.analyze()
        pipeline.import_file(filled, fill_from_cache=False)
        tl_result, _ = pipeline.build(pipeline.parsed)

        self.assertIsNotNone(tl_result)
        text = (project.game_dir / "tl" / "russian" / "script.rpy").read_text("utf-8")
        self.assertIn("ГОТОВО Hello, how are you?", text)

    def test_docx_roundtrip(self):
        """DOCX: экспорт -> правка в Word -> импорт (то, что делает GUI)."""
        try:
            from docx import Document  # noqa: F401
        except ImportError:
            self.skipTest("python-docx не установлен")
        from docx import Document
        from core.exporter import export_docx
        from core.importer import parse_docx

        out = self.tmp / "export.docx"
        export_docx(self.project, self.parsed, out)
        self.assertTrue(out.exists())

        # «редактор» заполняет строки RU:
        doc = Document(str(out))
        filled_count = 0
        for paragraph in doc.paragraphs:
            if paragraph.text.startswith("RU:"):
                paragraph.text = "RU: DOCX И ПЕРЕВОД"
                filled_count += 1
        doc.save(str(out))
        self.assertEqual(filled_count, len(self.parsed.items))

        by_id, by_text = parse_docx(out)
        self.assertEqual(len(by_id), len(self.parsed.items))
        self.assertEqual(set(by_id.values()), {"DOCX И ПЕРЕВОД"})

        project = scan_project(self.settings.game_path)
        parsed = parse_project(project, self.settings)
        result = import_translations(out, parsed)
        self.assertEqual(result.applied, len(parsed.items))
        self.assertEqual(result.missing, 0)

    def test_docx_table_import(self):
        """Запасной вариант: DOCX-таблица «оригинал | перевод»."""
        try:
            from docx import Document
        except ImportError:
            self.skipTest("python-docx не установлен")
        from core.importer import import_translations

        doc = Document()
        table = doc.add_table(rows=2, cols=2)
        table.cell(0, 0).text = "Оригинал"
        table.cell(0, 1).text = "Перевод"
        table.cell(1, 0).text = "Hello, how are you?"
        table.cell(1, 1).text = "Привет, как дела?"
        path = self.tmp / "table.docx"
        doc.save(str(path))

        project = scan_project(self.settings.game_path)
        parsed = parse_project(project, self.settings)
        result = import_translations(path, parsed)
        self.assertGreaterEqual(result.applied, 1)
        translated = [i for i in parsed.items if i.translation == "Привет, как дела?"]
        self.assertEqual(len(translated), 1)


# ===========================================================================
# 10. Архивы .rpa
# ===========================================================================
class TestRpa(TempDirMixin, unittest.TestCase):
    def _make_archive(self) -> Path:
        src_dir = self.tmp / "payload"
        src_dir.mkdir(parents=True)
        (src_dir / "script.rpy").write_text(
            'label start:\n    "Hello from archive."\n', "utf-8")
        (src_dir / "image.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 32)

        archive = self.tmp / "archive.rpa"
        try:
            from rpycdec import create_rpa  # type: ignore

            create_rpa(str(archive), [str(src_dir)], base_dir=str(src_dir))
        except Exception as exc:  # pragma: no cover
            self.skipTest(f"rpycdec.create_rpa недоступен: {exc}")
        return archive

    def test_read_index(self):
        archive = self._make_archive()
        index, key = read_index(archive)
        self.assertIsNotNone(key)
        self.assertIn("script.rpy", index)

    def test_native_extract_scripts_only(self):
        archive = self._make_archive()
        out = self.tmp / "extracted"
        result = extract_native(archive, out, suffixes=(".rpy", ".rpyc"))
        self.assertIn("script.rpy", result.extracted)
        self.assertFalse((out / "image.png").exists())
        self.assertIn("Hello from archive",
                      (out / "script.rpy").read_text("utf-8"))

    def test_prepare_via_pipeline(self):
        archive = self._make_archive()
        game_dir = self.project_dir / "game"
        # «игра без скриптов»: только архив
        for rpy in game_dir.glob("*.rpy"):
            rpy.unlink()
        shutil.copy2(archive, game_dir / "archive.rpa")

        from core.pipeline import Pipeline

        settings = Settings(game_path=game_dir, cache_path=self.tmp / "c.json",
                            log_path=self.tmp / "l.log", backup=False)
        pipeline = Pipeline(settings)
        project = pipeline.scan()
        self.assertTrue(project.rpa_files)
        rpa_results, _ = pipeline.prepare()
        self.assertTrue(any(r.extracted for r in rpa_results))
        self.assertTrue((game_dir / "script.rpy").exists())


# ===========================================================================
# 11. Бэкап
# ===========================================================================
class TestBackup(TempDirMixin, unittest.TestCase):
    def test_backup_created(self):
        from core.backup import create_backup

        project = scan_project(self.project_dir)
        result = create_backup(project, extra_paths=[self.settings.cache_path])
        self.assertTrue(result.ok)
        self.assertTrue(result.path.exists())
        with zipfile.ZipFile(result.path) as zf:
            names = zf.namelist()
        self.assertTrue(any(n.endswith("script.rpy") for n in names))
        self.assertIn("manifest.json", names)


# ===========================================================================
# 12. Сканер проекта
# ===========================================================================
class TestScanner(TempDirMixin, unittest.TestCase):
    def test_accepts_game_dir(self):
        project = scan_project(self.project_dir / "game")
        self.assertEqual(project.game_dir, (self.project_dir / "game").resolve())
        self.assertEqual(len(project.rpy_files), 5)

    def test_accepts_project_root(self):
        project = scan_project(self.project_dir)
        self.assertTrue(str(project.game_dir).endswith("game"))

    def test_tl_dir_excluded(self):
        tl = self.project_dir / "game" / "tl" / "russian"
        tl.mkdir(parents=True)
        (tl / "script.rpy").write_text("translate russian x:\n    pass\n", "utf-8")
        project = scan_project(self.project_dir)
        self.assertFalse(any("tl" in p.parts for p in project.rpy_files))
        self.assertEqual(project.language_dirs, ["russian"])


# ===========================================================================
# 13. Полный цикл
# ===========================================================================
class TestFullPipeline(TempDirMixin, unittest.TestCase):
    def test_full_run_tl(self):
        from core.pipeline import Pipeline

        self.settings.backup = True
        pipeline = Pipeline(self.settings)
        report = pipeline.run_full()

        self.assertTrue(report.ok)
        self.assertIsNotNone(report.tl_result)
        self.assertGreater(report.tl_result.blocks, 10)
        self.assertTrue((self.project_dir / "game" / "tl" / "russian").is_dir())
        self.assertTrue((self.project_dir / "_renpy_translator_backup").is_dir())

    def test_full_run_replace_with_dry_run(self):
        from core.pipeline import Pipeline

        self.settings.mode = "replace"
        self.settings.dry_run = True
        pipeline = Pipeline(self.settings)
        pipeline.scan()
        parsed = pipeline.analyze()
        pipeline.translate(parsed, show_progress=False)
        before = (self.project_dir / "game" / "script.rpy").read_text("utf-8")
        pipeline.build(parsed, mode="replace")
        after = (self.project_dir / "game" / "script.rpy").read_text("utf-8")
        self.assertEqual(before, after, "dry-run не должен менять файлы")


# ===========================================================================
# 13б. Экранирование скобок для Ren'Py
# ===========================================================================
class TestRenpyEscaping(unittest.TestCase):
    def test_foreign_square_brackets_are_escaped(self):
        from core.placeholder import escape_for_renpy

        self.assertEqual(escape_for_renpy("[ru] Eileen", "Eileen"),
                         "[[ru] Eileen")
        self.assertEqual(escape_for_renpy("100% [ok]", "fine"), "100% [[ok]")

    def test_original_interpolation_is_kept(self):
        from core.placeholder import escape_for_renpy

        original = "Hello, [player_name]!"
        self.assertEqual(escape_for_renpy("Привет, [player_name]!", original),
                         "Привет, [player_name]!")

    def test_original_tags_are_kept(self):
        from core.placeholder import escape_for_renpy

        original = "{b}Yes{/b}, {i}sir{/i}"
        self.assertEqual(escape_for_renpy("{b}Да{/b}, {i}сэр{/i}", original),
                         "{b}Да{/b}, {i}сэр{/i}")

    def test_safe_tags_added_by_translator(self):
        from core.placeholder import escape_for_renpy

        self.assertEqual(escape_for_renpy("{i}Курсив{/i}", "Italic"),
                         "{i}Курсив{/i}")

    def test_unknown_tag_is_escaped(self):
        from core.placeholder import escape_for_renpy

        self.assertEqual(escape_for_renpy("{перевод} текст", "text"),
                         "{{перевод} текст")

    def test_unbalanced_brackets_are_escaped(self):
        from core.placeholder import escape_for_renpy

        self.assertEqual(escape_for_renpy("Скобка [ без пары", "text"),
                         "Скобка [[ без пары")
        self.assertEqual(escape_for_renpy("Фигурная { без пары", "text"),
                         "Фигурная {{ без пары")

    def test_generated_tl_does_not_break_interpolation(self):
        """Сквозной тест: перевод с [ru] не должен ломать tl-файл."""
        import tempfile

        from core.pipeline import Pipeline

        tmp = Path(tempfile.mkdtemp(prefix="rpt_esc_"))
        try:
            game = tmp / "game"
            game.mkdir()
            (game / "script.rpy").write_text(
                'define e = Character("Eileen")\n\n'
                "label start:\n"
                '    e "Hello, [player_name]!"\n'
                "    return\n", "utf-8")

            settings = Settings(game_path=game, engine="mock",
                                cache_path=tmp / "cache.json", backup=False)
            settings.workers = 1
            pipeline = Pipeline(settings)
            pipeline.scan()
            parsed = pipeline.analyze()
            pipeline.translate(parsed, show_progress=False)

            # «агент» добавляет пометку [ru] к каждой строке
            for item in parsed.items:
                item.translation = "[ru] " + item.original
            pipeline.build(parsed)

            text = (game / "tl" / "russian" / "script.rpy").read_text("utf-8")
            self.assertIn("[[ru]", text)
            self.assertIn("[player_name]", text)
            self.assertNotIn('" [ru]', text)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


# ===========================================================================
# 13в. Лимиты сервиса, «пульс» и отмена
# ===========================================================================
class GoodEngine:
    """Запасной движок: переводит всё и сразу."""

    name = "good"

    def __init__(self, settings=None):
        from core.translator import RateLimiter

        self.settings = settings
        self.abort_check = None
        self.limiter = RateLimiter(0.0)
        self.stats = {"requests": 0, "retries": 0, "errors": 0, "chars": 0}

    def clone(self):
        return GoodEngine(self.settings)

    def translate_one(self, text):
        return self.translate_batch([text])[0]

    def translate_batch(self, texts):
        self.stats["requests"] += 1
        return [f"RU:{text}" for text in texts]


class FlakyEngine:
    """Движок, который первые N пакетов «падает» (как лимит Google), потом ок."""

    name = "flaky"

    def __init__(self, fail_batches: int = 999, settings=None, state=None):
        from core.translator import RateLimiter

        self.state = state if state is not None else {"left": fail_batches}
        self.settings = settings
        self.abort_check = None
        self.limiter = RateLimiter(0.0)
        self.stats = {"requests": 0, "retries": 0, "errors": 0, "chars": 0}
        self.calls: list[str] = []

    def clone(self):
        twin = FlakyEngine(self.settings, state=self.state)
        twin.calls = self.calls
        return twin

    def translate_one(self, text):
        return self.translate_batch([text])[0]

    def translate_batch(self, texts):
        self.stats["requests"] += 1
        if self.state["left"] > 0:
            self.state["left"] -= 1
            self.stats["errors"] += 1
            return [None] * len(texts)
        out = []
        for text in texts:
            self.calls.append(text)
            out.append("RU " + text)
        return out


class ThrottlingEngine:
    """Движок, который всегда отвечает «слишком много запросов»."""

    name = "throttle"

    def __init__(self, settings=None, calls=None):
        from core.translator import RateLimiter

        self.settings = settings
        self.abort_check = None
        self.limiter = RateLimiter(0.0)
        self.stats = {"requests": 0, "retries": 0, "errors": 0, "chars": 0}
        self.calls = calls if calls is not None else []

    def clone(self):
        return ThrottlingEngine(self.settings, self.calls)

    def _aborted(self):
        check = self.abort_check
        try:
            return bool(check and check())
        except Exception:  # noqa: BLE001
            return False

    def translate_one(self, text):
        from core.translator import ThrottleError

        self.calls.append(text)
        raise ThrottleError("HTTP 429 Too Many Requests")

    def translate_batch(self, texts):
        from core.translator import BaseEngine

        return BaseEngine.translate_batch(self, texts)


class TestCrlfAndCoverage(TempDirMixin, unittest.TestCase):
    """Windows-сборки (CRLF) не должны терять реплики."""

    SCRIPT = (
        'define e = Character("Eileen")\n'
        '\n'
        'label start:\n'
        '    scene bg room\n'
        '    e "Hello there, friend."\n'
        '    "Narration line one."\n'
        '    "Narration line two."\n'
        '    menu:\n'
        '        "Go left":\n'
        '            jump left\n'
        '\n'
        'label left:\n'
        '    e "We went left."\n'
    )

    def _counts(self, payload: bytes) -> dict:
        from collections import Counter

        from core.pipeline import Pipeline

        game = self.tmp / "crlf_game" / "game"
        game.mkdir(parents=True, exist_ok=True)
        (game / "script.rpy").write_bytes(payload)
        settings = Settings(game_path=game, cache_path=self.tmp / "crlf_cache.json",
                            backup=False, engine="mock")
        pipeline = Pipeline(settings)
        pipeline.scan()
        parsed = pipeline.analyze()
        return Counter(item.kind for item in parsed.items)

    def test_crlf_keeps_dialogue(self):
        lf = self._counts(self.SCRIPT.encode("utf-8"))
        crlf = self._counts(self.SCRIPT.replace("\n", "\r\n").encode("utf-8"))
        bom = self._counts(b"\xef\xbb\xbf" + self.SCRIPT.replace("\n", "\r\n").encode("utf-8"))
        tabs = self._counts(self.SCRIPT.replace("\n", "\r\n").replace("    ", "\t").encode("utf-8"))

        for name, counts in (("LF", lf), ("CRLF", crlf), ("BOM", bom), ("TABS", tabs)):
            self.assertEqual(counts.get("dialogue"), 2, f"{name}: реплики потеряны")
            self.assertEqual(counts.get("narration"), 2, f"{name}: нарратив потерян")
            self.assertEqual(counts.get("menu"), 1, f"{name}: меню потеряно")

    def test_read_text_file_normalizes_newlines(self):
        from core.utils import read_text_file

        path = self.tmp / "win.rpy"
        path.write_bytes(b'label a:\r\n    "Hi."\r\n')
        self.assertNotIn("\r", read_text_file(path))

    def test_coverage_warning_on_unparsed_text(self):
        from core.models import ParsedFile
        from core.rpy_parser import check_coverage

        body = "\n".join(f'    ignus "Line number {i} of the story."' for i in range(60))
        parsed = ParsedFile(path="script.rpy", items=[], labels=[], warnings=[])
        check_coverage(body, parsed)
        self.assertTrue(parsed.warnings)
        self.assertIn("не полностью", parsed.warnings[0])

    def test_ui_only_file_no_false_alarm(self):
        """screens.rpy: строки вида `text "…"` не должны давать ложную тревогу."""
        from core.pipeline import Pipeline

        game = self.tmp / "ui_game" / "game"
        game.mkdir(parents=True, exist_ok=True)
        body = 'screen menu():\n' + "".join(
            f'    textbutton _("Button number {i}") action NullAction()\n'
            for i in range(40))
        (game / "screens.rpy").write_text(body, encoding="utf-8")
        (game / "script.rpy").write_text(self.SCRIPT, encoding="utf-8")
        settings = Settings(game_path=game, cache_path=self.tmp / "ui_cache.json",
                            backup=False, engine="mock")
        pipeline = Pipeline(settings)
        pipeline.scan()
        parsed = pipeline.analyze()
        self.assertFalse([w for w in parsed.warnings if "не полностью" in w])

    def test_no_coverage_warning_when_all_parsed(self):
        from core.pipeline import Pipeline

        game = self.tmp / "ok_game" / "game"
        game.mkdir(parents=True, exist_ok=True)
        (game / "script.rpy").write_text(self.SCRIPT, encoding="utf-8")
        settings = Settings(game_path=game, cache_path=self.tmp / "ok_cache.json",
                            backup=False, engine="mock")
        pipeline = Pipeline(settings)
        pipeline.scan()
        parsed = pipeline.analyze()
        self.assertFalse([w for w in parsed.warnings if "не полностью" in w])


def _write_rpa(path: Path, files: dict, version: str = "RPA-3.0",
               key: int = 0x42424242, prefix: bytes = b"") -> Path:
    """Пишет архив нужного формата — как это делает сам Ren'Py."""
    import pickle
    import zlib

    header_len = {"RPA-3.0": 34, "RPA-3.2": 34, "RPA-2.0": 25}.get(version, 0)
    blobs = []
    index = {}
    offset = header_len
    for name, data in files.items():
        blobs.append(data)
        if version in ("RPA-3.0", "RPA-3.2"):
            index[name] = [(offset ^ key, len(data) ^ key, prefix)]
        else:
            index[name] = [(offset, len(data))]
        offset += len(data)
    packed = zlib.compress(pickle.dumps(index, 2))
    if version in ("RPA-3.0", "RPA-3.2"):
        header = (version.encode() + b" " + f"{offset:016x}".encode() + b" "
                  + f"{key:08x}".encode() + b"\n")
    elif version == "RPA-2.0":
        header = version.encode() + b" " + f"{offset:016x}".encode() + b"\n"
    else:  # RPA-1.0: данные в .rpa, индекс в парном .rpi
        path.write_bytes(b"".join(blobs))
        path.with_suffix(".rpi").write_bytes(packed)
        return path
    path.write_bytes(header + b"".join(blobs) + packed)
    return path


class TestLanguageEnabling(TempDirMixin, unittest.TestCase):
    """Русский включается, даже если в меню игры такого языка нет."""

    SCREENS = (
        'screen preferences():\n'
        '    textbutton "English" action Language(None)\n'
        '    textbutton "简体中文" action Language("simplified_chinese")\n'
    )

    def _game(self) -> Path:
        game = self.tmp / "lang_game" / "game"
        game.mkdir(parents=True, exist_ok=True)
        (game / "script.rpy").write_text(
            'label start:\n    "Hello there, friend."\n', encoding="utf-8")
        (game / "screens.rpy").write_text(self.SCREENS, encoding="utf-8")
        (game / "options.rpy").write_text(
            "define config.default_language = None\n", encoding="utf-8")
        return game

    def _build(self, game: Path, **flags):
        from core.pipeline import Pipeline

        settings = Settings(game_path=game, cache_path=self.tmp / "lang_cache.json",
                            backup=False, engine="mock", **flags)
        pipeline = Pipeline(settings)
        pipeline.scan()
        parsed = pipeline.analyze()
        pipeline.translate(parsed, show_progress=False)
        return pipeline.build(parsed)[0]

    def test_warns_when_game_menu_has_no_russian(self):
        game = self._game()
        result = self._build(game)
        self.assertTrue([w for w in result.warnings if "нет пункта" in w])

    def test_language_file_uses_late_init_and_soft_force(self):
        game = self._game()
        self._build(game, set_default_language=True)
        text = (game / "tl" / "russian" /
                "zz_renpy_translator_language.rpy").read_text(encoding="utf-8")
        self.assertIn("init 999 python", text)          # позже define в options.rpy
        self.assertNotIn("init -999 python", text)
        self.assertIn("if not _preferences.language", text)
        self.assertIn('config.default_language = "russian"', text)

    def test_language_menu_detection(self):
        from core.tl_writer import language_menu_languages

        game = self._game()
        from core.scanner import scan_project

        project = scan_project(game)
        has_menu, langs = language_menu_languages(project)
        self.assertTrue(has_menu)
        self.assertEqual(langs, {"simplified_chinese"})


class TestExistingTranslation(TempDirMixin, unittest.TestCase):
    """Официальный перевод игры не дублируется (иначе Ren'Py упадёт)."""

    def test_blocks_already_translated_are_skipped(self):
        from core.pipeline import Pipeline

        game = self.tmp / "own" / "game"
        game.mkdir(parents=True, exist_ok=True)
        (game / "script.rpy").write_text(
            'label start:\n    "Hello there, friend."\n    "Good evening."\n',
            encoding="utf-8")
        settings = Settings(game_path=game, engine="mock",
                            cache_path=self.tmp / "own_cache.json", backup=False)
        pipeline = Pipeline(settings)
        pipeline.scan()
        parsed = pipeline.analyze()
        pipeline.translate(parsed, show_progress=False)
        first, _ = pipeline.build(parsed)
        self.assertTrue(first.blocks >= 2)

        # теперь «игра» уже содержит свой перевод одного из блоков
        ids = [i.say.block_id for i in parsed.items if i.say and i.say.block_id]
        tl_dir = game / "tl" / "russian"
        official = tl_dir / "official.rpy"
        official.write_text(
            f'translate russian {ids[0]}:\n\n    # "Hello there, friend."\n'
            '    "Привет, друг."\n', encoding="utf-8")

        second, _ = pipeline.build(parsed)
        self.assertGreaterEqual(second.skipped_existing, 1)
        text = (tl_dir / "script.rpy").read_text(encoding="utf-8")
        self.assertNotIn(f"translate russian {ids[0]}:", text)

        from core.verify import verify_project

        report = verify_project(pipeline.project, "russian", parsed)
        self.assertTrue(report.ok, [i.message for i in report.errors])


class TestQuality(unittest.TestCase):
    """Контроль качества перевода (v3.0)."""

    def codes(self, src, dst, lang="russian"):
        from core.quality import check_pair

        return {i.code for i in check_pair(src, dst, lang)}

    def test_good_translation_has_no_issues(self):
        self.assertEqual(self.codes(
            "Hello [name], you have {b}5{/b} gold.",
            "Привет, [name], у тебя {b}5{/b} золота."), set())

    def test_placeholder_loss_is_error(self):
        from core.quality import check_pair

        issues = check_pair("Hello [name].", "Привет.")
        self.assertTrue(any(i.code == "placeholder_missing"
                            and i.severity == "error" for i in issues))

    def test_percent_count_mismatch(self):
        self.assertIn("percent_mismatch",
                      self.codes("You have %d coins.", "У тебя монет."))
        # смена буквы спецификатора безопасна — только заметка
        self.assertEqual(self.codes("You have %d coins.", "У тебя %s монет."),
                         {"percent_letter"})

    def test_untranslated_text_detected(self):
        self.assertIn("not_translated",
                      self.codes("Hello there, friend.", "Hello there, friend."))

    def test_foreign_script_detected(self):
        self.assertIn("foreign_script", self.codes("See you.", "こんにちは"))

    def test_latin_names_are_not_punished(self):
        codes = self.codes("Eileen smiled at John.", "Эйлин улыбнулась Джону.")
        self.assertNotIn("foreign_script", codes)

    def test_glossary_term_required(self):
        from core.quality import check_pair

        issues = check_pair("The Academy is closed.", "Школа закрыта.",
                            "russian", {"Academy": "Академия"})
        self.assertTrue(any(i.code == "glossary_missing" for i in issues))

    def test_safest_translation_drops_broken(self):
        from core.quality import safest_translation

        self.assertEqual(safest_translation("Hi [name]", "Привет"), "")
        self.assertEqual(safest_translation("Hi [name]", "Привет [name]"),
                         "Привет [name]")

    def test_report_retry_and_fatal_sets(self):
        from core.quality import check_translations

        report = check_translations([
            ("Hello [name].", "Привет."),
            ("Fine.", "Хорошо."),
        ])
        self.assertEqual(report.checked, 2)
        self.assertIn("Hello [name].", report.retry_texts())
        self.assertIn("Hello [name].", report.fatal_texts())
        self.assertFalse(report.ok)


class TestVerify(unittest.TestCase):
    """Статическая проверка tl-каталога (v3.0)."""

    def build(self, files: dict[str, str]):
        import tempfile
        from pathlib import Path as _Path

        tmp = _Path(tempfile.mkdtemp())
        tl = tmp / "tl" / "russian"
        tl.mkdir(parents=True)
        for name, text in files.items():
            (tl / name).write_text(text, encoding="utf-8")
        return tmp, tl

    def test_clean_translation_passes(self):
        from core.verify import verify_tl_dir

        _, tl = self.build({"script.rpy": (
            'translate russian start_a:\n\n    # e "Hi [name]!"\n'
            '    e "Привет [name]!"\n')})
        report = verify_tl_dir(tl, "russian", expected_ids={"start_a"})
        self.assertTrue(report.ok, [i.message for i in report.issues])
        self.assertEqual(report.coverage, 1.0)
        self.assertEqual(report.blocks, 1)

    def test_stray_statement_is_error(self):
        from core.verify import verify_tl_dir

        _, tl = self.build({"bad.rpy": "$ x = 1\ntranslate russian a:\n\n"
                                       '    # "Hi"\n    "Привет"\n'})
        report = verify_tl_dir(tl, "russian")
        self.assertTrue(any(i.code == "stray_statement"
                            for i in report.errors))

    def test_wrong_language_is_error(self):
        from core.verify import verify_tl_dir

        _, tl = self.build({"bad.rpy": 'translate english a:\n\n'
                                       '    # "Hi"\n    "Привет"\n'})
        report = verify_tl_dir(tl, "russian")
        self.assertTrue(any(i.code == "wrong_language" for i in report.errors))

    def test_duplicate_block_across_files(self):
        from core.verify import verify_tl_dir

        _, tl = self.build({
            "a.rpy": 'translate russian same:\n\n    # "A"\n    "А"\n',
            "b.rpy": 'translate russian same:\n\n    # "B"\n    "Б"\n',
        })
        report = verify_tl_dir(tl, "russian")
        self.assertTrue(any(i.code == "duplicate_id" for i in report.errors))

    def test_empty_new_is_error(self):
        from core.verify import verify_tl_dir

        _, tl = self.build({"zz.rpy": 'translate russian strings:\n\n'
                                      '    old "Start"\n    new ""\n'})
        report = verify_tl_dir(tl, "russian")
        self.assertTrue(any(i.code == "empty_new" for i in report.errors))
        self.assertEqual(report.strings, 1)

    def test_placeholder_lost_between_comment_and_text(self):
        from core.verify import verify_tl_dir

        _, tl = self.build({"x.rpy": 'translate russian a:\n\n'
                                     '    # e "Hi [name]"\n    e "Привет"\n'})
        report = verify_tl_dir(tl, "russian")
        self.assertTrue(any(i.code == "placeholder_lost"
                            for i in report.errors))

    def test_coverage_reports_missing(self):
        from core.verify import verify_tl_dir

        _, tl = self.build({"x.rpy": 'translate russian a:\n\n'
                                     '    # "Hi"\n    "Привет"\n'})
        report = verify_tl_dir(tl, "russian", expected_ids={"a", "b"})
        self.assertAlmostEqual(report.coverage, 0.5)
        self.assertTrue(any(i.code == "missing_translation"
                            for i in report.issues))

    def test_unbalanced_quotes(self):
        from core.verify import verify_tl_dir

        _, tl = self.build({"x.rpy": 'translate russian a:\n\n'
                                     '    # "Hi"\n    "Привет\n'})
        report = verify_tl_dir(tl, "russian")
        self.assertTrue(any(i.code == "unbalanced_quotes"
                            for i in report.errors))

    def test_say_with_many_attributes(self):
        """`att hoodiecrossed closed "…"` — не пустой блок."""
        from core.verify import verify_tl_dir

        _, tl = self.build({"x.rpy":
            'translate russian a:\n\n'
            '    # att hoodiecrossed closed "Tried to kill {i}you{/i}."\n'
            '    att hoodiecrossed closed "Пытался убить {i}тебя{/i}."\n'
            '\n'
            'translate russian b:\n\n'
            '    # luc "Hmph."\n    luc "Хм."\n'})
        report = verify_tl_dir(tl, "russian")
        self.assertEqual(report.empty_blocks, 0)
        self.assertTrue(report.ok, [i.message for i in report.errors])

    def test_stale_rpyc_warning(self):
        import os
        import time as _time

        from core.verify import verify_tl_dir

        _, tl = self.build({"x.rpy": 'translate russian a:\n\n    # "Hi"\n'
                                     '    "Привет"\n'})
        compiled = tl / "x.rpyc"
        compiled.write_bytes(b"old")
        past = _time.time() - 1000
        os.utime(compiled, (past, past))
        report = verify_tl_dir(tl, "russian")
        self.assertTrue(any(i.code == "stale_rpyc" for i in report.warnings))


class TestReport(unittest.TestCase):
    """HTML-отчёт (v3.0)."""

    def test_html_contains_key_parts(self):
        from core.quality import check_translations
        from core.report import build_html_report
        from core.verify import VerifyReport

        quality = check_translations([("Hi [name].", "Привет.")])
        verify = VerifyReport(language="russian", files=2, blocks=10,
                              coverage_total=10, coverage_done=9)
        html = build_html_report(game_name="Test <Game>", game_path="/tmp/g",
                                 language="russian", translate_report=None,
                                 quality=quality, verify=verify)
        self.assertIn("Отчёт о переводе", html)
        self.assertIn("Test &lt;Game&gt;", html)          # html-экранирование
        self.assertIn("потерян плейсхолдер", html)
        self.assertIn("90", html)                          # покрытие 90 %
        self.assertIn("<style>", html)                     # автономность

    def test_write_report_creates_file(self):
        import tempfile
        from pathlib import Path as _Path

        from core.report import write_report

        target = _Path(tempfile.mkdtemp()) / "r.html"
        result = write_report(target, game_name="G", game_path="/g",
                              language="russian")
        self.assertTrue(result.exists())
        self.assertIn("RenPyTranslator", result.read_text(encoding="utf-8"))


class TestRestore(TempDirMixin, unittest.TestCase):
    """Откат из резервной копии (v3.0)."""

    def test_backup_and_restore_roundtrip(self):
        from core.backup import create_backup, restore_backup
        from core.scanner import scan_project

        game = self.tmp / "g2" / "game"
        game.mkdir(parents=True, exist_ok=True)
        script = game / "script.rpy"
        script.write_text('label start:\n    "Hello."\n', encoding="utf-8")

        project = scan_project(game)
        backup = create_backup(project)
        self.assertTrue(backup.ok)

        script.write_text('label start:\n    "Changed."\n', encoding="utf-8")
        tl_dir = game / "tl" / "russian"
        tl_dir.mkdir(parents=True)
        (tl_dir / "script.rpy").write_text("translate russian x:\n",
                                           encoding="utf-8")

        result = restore_backup(project)
        self.assertTrue(result.ok, result.problems)
        self.assertIn("Hello", script.read_text(encoding="utf-8"))
        self.assertFalse(tl_dir.exists())
        self.assertEqual(result.removed, 1)

    def test_restore_without_backup_reports_problem(self):
        from core.backup import restore_backup
        from core.scanner import scan_project

        game = self.tmp / "g3" / "game"
        game.mkdir(parents=True, exist_ok=True)
        (game / "script.rpy").write_text("label start:\n    return\n",
                                         encoding="utf-8")
        result = restore_backup(scan_project(game))
        self.assertFalse(result.ok)
        self.assertTrue(result.problems)


class TestLanguages(unittest.TestCase):
    """Мультиязычность (v3.0)."""

    def test_labels(self):
        from core.langs import is_cyrillic, label_for, needs_bundled_font

        self.assertEqual(label_for("russian"), "Русский")
        self.assertEqual(label_for("japanese"), "日本語")
        self.assertTrue(is_cyrillic("ukrainian"))
        self.assertFalse(is_cyrillic("german"))
        self.assertFalse(needs_bundled_font("chinese"))
        self.assertTrue(needs_bundled_font("russian"))

    def test_fonts_skipped_for_cjk(self):
        """Для китайского PT Serif не ставим — шрифт бесполезен."""
        import tempfile
        from pathlib import Path as _Path

        from core.fonts import install
        from core.scanner import scan_project

        game = _Path(tempfile.mkdtemp()) / "g" / "game"
        game.mkdir(parents=True)
        (game / "script.rpy").write_text('label start:\n    "Hi."\n',
                                         encoding="utf-8")
        project = scan_project(game)
        report = install(project, None, "chinese", mode="pretty")
        self.assertTrue(any("PT Serif" in w for w in report.warnings))
        self.assertFalse((game / "tl" / "chinese" / "fonts").exists())


class TestQualityPipeline(TempDirMixin, unittest.TestCase):
    """Сквозная проверка: битые строки откатываются в оригинал."""

    class BrokenEngine:
        """Ломает плейсхолдеры, как это иногда делает Google."""

        name = "broken"

        def clone(self):
            return self

        abort_check = None

        def translate(self, text, source="auto", target="russian"):
            return "Перевод без подстановок"

        def translate_batch(self, texts):
            return ["Перевод без подстановок" for _ in texts]

        def translate_one(self, text):
            return "Перевод без подстановок"

    def _pipeline(self):
        from core.pipeline import Pipeline

        game = self.tmp / "qp" / "game"
        game.mkdir(parents=True, exist_ok=True)
        (game / "script.rpy").write_text(
            'label start:\n    "Hello [name], how are you?"\n\n'
            '    "Good evening."\n', encoding="utf-8")
        settings = Settings(game_path=game, engine="mock",
                            cache_path=self.tmp / "q_cache.json",
                            backup=False)
        pipeline = Pipeline(settings)
        pipeline.scan()
        return pipeline

    def test_broken_line_keeps_placeholders_and_is_flagged(self):
        """Сломанный перевод чинится (плейсхолдер на месте) и попадает в отчёт."""
        pipeline = self._pipeline()
        pipeline.get_engine = lambda: self.BrokenEngine()  # type: ignore[assignment]
        parsed = pipeline.analyze()
        pipeline.translate(parsed, show_progress=False)
        quality = pipeline.quality
        self.assertIsNotNone(quality)
        broken = [i for i in parsed.items if "[name]" in i.original]
        self.assertTrue(broken)
        # плейсхолдер сохранён — игра не сломается
        self.assertIn("[name]", broken[0].translation)
        # но строка помечена как требующая переделки
        self.assertTrue(any(i.code == "placeholder_repaired"
                            for i in quality.issues))
        good = [i for i in parsed.items if i.original == "Good evening."]
        self.assertTrue(good[0].translation)

    def test_verify_and_report_after_build(self):
        pipeline = self._pipeline()
        pipeline.get_engine = lambda: self.BrokenEngine()  # type: ignore[assignment]
        parsed = pipeline.analyze()
        pipeline.translate(parsed, show_progress=False)
        tl_result, _ = pipeline.build(parsed)
        self.assertIsNotNone(tl_result)
        self.assertIsNotNone(pipeline.verification)
        self.assertTrue(pipeline.verification.ok,
                        [i.message for i in pipeline.verification.errors])
        self.assertIn("Отчёт о переводе", pipeline.report_html())


class TestScreenPatcher(TempDirMixin, unittest.TestCase):
    """Поиск меню «Язык» в экранах игры и вписывание в него кнопки."""

    SCREENS = (
        'screen preferences():\n'
        '    vbox:\n'
        '        textbutton "Display" action ShowMenu("preferences")\n'
        '\n'
        '        vbox:\n'
        '            text "Language"\n'
        '            textbutton "English" text_font "DejaVuSans.ttf" action Language(None)\n'
        '            textbutton "简体中文" text_font "DejaVuSans.ttf" '
        'action Language("simplified_chinese")\n'
    )

    def _project(self, screens: Optional[str] = None):
        from core.scanner import scan_project

        game = self.tmp / "patch_game" / "game"
        game.mkdir(parents=True, exist_ok=True)
        (game / "script.rpy").write_text('label start:\n    "Hi."\n', encoding="utf-8")
        (game / "screens.rpy").write_text(screens or self.SCREENS, encoding="utf-8")
        return scan_project(game)

    def test_finds_language_buttons(self):
        from core.screen_patcher import find_language_buttons, pick_button

        project = self._project()
        found = find_language_buttons(project)
        self.assertEqual(len(found), 2)
        best = pick_button(found)
        self.assertEqual(best.screen, "preferences")
        self.assertIn("Language(None)", best.text)      # встанем после «English»
        self.assertEqual(best.font, "DejaVuSans.ttf")
        self.assertEqual(best.indent, "            ")

    def test_patch_inserts_line_with_same_indent_and_font(self):
        from core.screen_patcher import patch_language_menus

        project = self._project()
        screens = project.game_dir / "screens.rpy"
        done, warnings = patch_language_menus(project, "russian", "Русский")
        self.assertEqual(len(done), 1, warnings)
        text = screens.read_text(encoding="utf-8")
        self.assertEqual(text.count('action Language("russian")'), 1)
        self.assertIn(
            '            textbutton "Русский" text_font "DejaVuSans.ttf" '
            'action Language("russian")', text)
        # оригинал сохранён рядом с переводом, но не как .rpy
        backup = project.game_dir / "tl" / "russian" / "rt_backup" / "screens.rpy.bak"
        self.assertTrue(backup.exists())
        self.assertNotIn('Language("russian")', backup.read_text(encoding="utf-8"))

    def test_patch_is_idempotent(self):
        from core.screen_patcher import patch_language_menus

        project = self._project()
        patch_language_menus(project, "russian", "Русский")
        done, warnings = patch_language_menus(project, "russian", "Русский")
        self.assertEqual(done, [])
        text = (project.game_dir / "screens.rpy").read_text(encoding="utf-8")
        self.assertEqual(text.count('action Language("russian")'), 1)

    def test_grid_is_not_patched(self):
        from core.screen_patcher import patch_language_menus

        screens = (
            'screen preferences():\n'
            '    grid 2 2:\n'
            '        textbutton "English" action Language(None)\n'
            '        textbutton "简体中文" action Language("simplified_chinese")\n'
            '        null\n'
            '        null\n'
        )
        project = self._project(screens)
        done, warnings = patch_language_menus(project, "russian", "Русский")
        self.assertEqual(done, [])
        self.assertTrue(any("grid" in w for w in warnings))
        text = (project.game_dir / "screens.rpy").read_text(encoding="utf-8")
        self.assertNotIn('Language("russian")', text)

    def test_crlf_is_preserved(self):
        from core.screen_patcher import patch_language_menus

        screens = self.SCREENS.replace("\n", "\r\n")
        project = self._project(screens)
        screens_path = project.game_dir / "screens.rpy"
        screens_path.write_bytes(screens.encode("utf-8"))
        patch_language_menus(project, "russian", "Русский")
        raw = screens_path.read_bytes()
        self.assertIn(b'action Language("russian")\r\n', raw)

    def test_button_screen_file(self):
        from core.screen_patcher import button_screen_code

        code = button_screen_code("russian")
        self.assertIn("screen rt_language_toggle():", code)
        self.assertIn("config.always_shown_screens", code)
        self.assertIn('action Language("russian")', code)
        self.assertIn("action Language(None)", code)
        self.assertTrue(code.startswith("# [renpy-translator]"))

    def test_pipeline_writes_button_and_patches_on_request(self):
        from core.pipeline import Pipeline

        project = self._project()
        settings = Settings(game_path=project.game_dir, engine="mock",
                            cache_path=self.tmp / "patch_cache.json",
                            backup=False, language_button=True,
                            patch_language_menu=False)
        pipeline = Pipeline(settings)
        pipeline.scan()
        parsed = pipeline.analyze()
        pipeline.translate(parsed, show_progress=False)
        pipeline.build(parsed)
        tl_dir = project.game_dir / "tl" / "russian"
        self.assertTrue((tl_dir / "zz_renpy_translator_language_button.rpy").exists())
        # без галочки файл игры не тронут
        self.assertNotIn('Language("russian")',
                         (project.game_dir / "screens.rpy").read_text(encoding="utf-8"))


class TestVendorLibraries(TempDirMixin, unittest.TestCase):
    """Сторонние библиотеки в game/ не должны превращаться в файлы перевода."""

    LIB = (
        'init python:\n'
        '    CC_COLOR_BURN_NAME = "crosscouloir.color_burn"\n'
        '\n'
        '    _mode = BlendMode(CC_COLOR_BURN_NAME)\n'
        '\n'
        '    _mode.vars = """\n'
        'uniform float u_lod_bias;\n'
        'uniform sampler2D tex0;\n'
        '"""\n'
        '    _mode.fragment_functions = """\n'
        'float blendColorBurn(float base, float blend) {\n'
        '    return (blend==0.0)?blend:0.0;\n'
        '}\n'
        '"""\n'
        '    _mode.vertex_shader = """\n'
        'v_tex_coord = a_tex_coord;\n'
        '"""\n'
    )

    def test_init_python_block_is_skipped(self):
        from core.rpy_parser import PYTHON_BLOCK_RE

        for line in ("init python:", "init 5 python:", "python early:",
                     "init -1 python in ns:", "python in store:"):
            self.assertTrue(PYTHON_BLOCK_RE.match(line), line)

    def test_assignments_are_not_say_statements(self):
        from core.rpy_parser import parse_say_statement

        for line in ('CC_COLOR_BURN_NAME = "crosscouloir.color_burn"',
                     '_mode.vars = "uniform float u_lod_bias;"',
                     'd["key"] = "value"'):
            self.assertIsNone(parse_say_statement(line), line)
        self.assertIsNotNone(parse_say_statement('e happy "Привет"'))
        self.assertIsNotNone(parse_say_statement('"Просто строка"'))
        self.assertIsNotNone(parse_say_statement('e "Текст" (what_color="#fff")'))

    def test_library_file_produces_no_tl_file(self):
        from core.pipeline import Pipeline

        game = self.tmp / "vendor_game" / "game"
        (game / "renpy_blend_modes").mkdir(parents=True, exist_ok=True)
        (game / "renpy_blend_modes" / "color_burn.rpy").write_text(
            self.LIB, encoding="utf-8")
        (game / "script.rpy").write_text(
            'label start:\n    "Real dialogue."\n', encoding="utf-8")

        settings = Settings(game_path=game, cache_path=self.tmp / "vendor_cache.json",
                            backup=False, engine="mock")
        pipeline = Pipeline(settings)
        pipeline.scan()
        parsed = pipeline.analyze()
        originals = [i.original for i in parsed.items]
        self.assertIn("Real dialogue.", originals)
        self.assertFalse([t for t in originals if "crosscouloir" in t])
        self.assertFalse([t for t in originals if "blendColorBurn" in t])

        pipeline.translate(parsed, show_progress=False)
        pipeline.build(parsed, mode="tl")

        tl_dir = game / "tl" / "russian"
        self.assertFalse((tl_dir / "renpy_blend_modes" / "color_burn.rpy").exists(),
                         "файл библиотеки попал в перевод — игра не запустится")
        self.assertTrue((tl_dir / "script.rpy").exists())


class TestHookSafety(TempDirMixin, unittest.TestCase):
    """Runtime-хук не должен ронять игру на чужих версиях Ren'Py."""

    BODY = (
        'label start:\n'
        '    "Hello there."\n'
        '    "Second line."\n'
    )

    def _hook_text(self) -> str:
        from core.pipeline import Pipeline

        game = self.tmp / "hook_game" / "game"
        game.mkdir(parents=True, exist_ok=True)
        (game / "script.rpy").write_text(self.BODY, encoding="utf-8")
        settings = Settings(game_path=game, cache_path=self.tmp / "hook_cache.json",
                            backup=False, engine="mock")
        pipeline = Pipeline(settings)
        pipeline.scan()
        parsed = pipeline.analyze()
        report = pipeline.translate(parsed, show_progress=False)
        pipeline.build(parsed, mode="tl")
        from core.tl_writer import _build_hook_file
        return _build_hook_file(parsed.items, "russian")

    def test_hook_guards_unknown_config_variable(self):
        text = self._hook_text()
        # присваивание есть, но только под hasattr и внутри try
        self.assertIn('hasattr(config, "use_menu_text_filter")', text)
        self.assertIn("except Exception", text)
        # присваивание ровно одно и оно под защитой hasattr
        self.assertEqual(text.count("config.use_menu_text_filter ="), 1)

    def test_hook_keeps_previous_filter(self):
        text = self._hook_text()
        self.assertIn('getattr(config, "say_menu_text_filter", None)', text)
        self.assertIn("_renpy_translator_install()", text)


class TestGoogleFreeEngine(unittest.TestCase):
    """Бесплатный Google: пачки, разбор ответов, реакция на лимиты."""

    def _engine(self, **kw):
        from core.translator import GoogleFreeEngine

        settings = Settings(engine="google", **kw)
        return GoogleFreeEngine(settings)

    def test_batches_many_lines_into_few_requests(self):
        engine = self._engine()
        calls = []

        def fake_post(texts):
            calls.append(list(texts))
            return json.dumps([[f"RU:{t}", "en"] for t in texts], ensure_ascii=False)

        engine._post = fake_post
        texts = [f"Line number {i}" for i in range(90)]
        result = engine.translate_batch(texts)

        self.assertEqual(result, [f"RU:{t}" for t in texts])
        self.assertEqual(len(calls), 3, "90 строк должны уйти тремя запросами по 40")
        self.assertEqual(len(calls[0]), 40)
        self.assertEqual(len(calls[-1]), 10)

    def test_empty_and_letterless_lines_are_not_sent(self):
        engine = self._engine()
        calls = []

        def fake_post(texts):
            calls.append(list(texts))
            return json.dumps([[f"RU:{t}", "en"] for t in texts], ensure_ascii=False)

        engine._post = fake_post
        result = engine.translate_batch(["", "Hello", "   "])
        self.assertEqual(result[0], "")
        self.assertEqual(result[1], "RU:Hello")
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(calls[0]), 1)

    def test_parse_variants(self):
        engine = self._engine()
        self.assertEqual(engine._parse('[["Привет","en"]]', 1), ["Привет"])
        self.assertEqual(engine._parse('["Привет","en"]', 1), ["Привет"])
        self.assertEqual(engine._parse('[["А","en"],["Б","en"]]', 2), ["А", "Б"])

    def test_429_becomes_throttle_error(self):
        import urllib.error

        from core.translator import ThrottleError

        engine = self._engine()

        def fake_post(texts):
            raise urllib.error.HTTPError("url", 429, "Too Many Requests", {}, None)

        engine._post = fake_post
        with self.assertRaises(ThrottleError):
            engine.translate_batch(["Hello"])

    def test_sorry_html_becomes_throttle_error(self):
        from core.translator import ThrottleError

        engine = self._engine()
        engine._post = lambda texts: "<html><title>Sorry...</title></html>"
        with self.assertRaises(ThrottleError):
            engine.translate_batch(["Hello"])

    def test_hosts_rotate_when_blocked(self):
        """403 на одном адресе — следующий запрос идёт на другое зеркало."""
        import urllib.error
        import urllib.request as urlreq

        from core.translator import ThrottleError

        engine = self._engine()
        seen: list[str] = []

        def fake_urlopen(req, timeout=None):
            seen.append(req.full_url.split("/")[2])
            raise urllib.error.HTTPError(req.full_url, 403, "Forbidden", {}, None)

        real = urlreq.urlopen
        urlreq.urlopen = fake_urlopen
        self.addCleanup(setattr, urlreq, "urlopen", real)

        for _ in range(4):
            with self.assertRaises(ThrottleError):
                engine._translate_chunk(["Hello there"])

        self.assertEqual(seen, ["clients5.google.com", "clients6.google.com",
                                "translate.google.com", "clients5.google.com"])

    def test_network_error_is_plain_translation_error(self):
        import urllib.error
        import urllib.request as urlreq

        from core.translator import TranslationError

        engine = self._engine()

        def fake_urlopen(req, timeout=None):
            raise urllib.error.URLError("timed out")

        real = urlreq.urlopen
        urlreq.urlopen = fake_urlopen
        self.addCleanup(setattr, urlreq, "urlopen", real)

        with self.assertRaises(TranslationError) as ctx:
            engine._translate_chunk(["Hello there"])
        self.assertNotIn("ограничивает", str(ctx.exception))
        self.assertEqual(engine._host_index, 1, "адрес должен смениться")

    def test_count_mismatch_falls_back_to_single_lines(self):
        engine = self._engine()
        responses = [json.dumps([["А", "en"]], ensure_ascii=False),
                     json.dumps([["Б", "en"]], ensure_ascii=False),
                     json.dumps([["В", "en"]], ensure_ascii=False)]

        def fake_post(texts):
            if len(texts) > 1:
                return json.dumps([["спутано", "en"]], ensure_ascii=False)
            return responses.pop(0)

        engine._post = fake_post
        result = engine.translate_batch(["a", "b", "c"])
        self.assertEqual(result, ["А", "Б", "В"])

    def test_mymemory_engine_parses_response(self):
        from core.translator import MyMemoryEngine

        engine = MyMemoryEngine(Settings(engine="mymemory"))
        import core.translator as tr

        original = tr._http_json
        tr._http_json = lambda url, timeout=30: {
            "responseData": {"translatedText": "Привет, мир"},
            "responseStatus": 200,
        }
        try:
            self.assertEqual(engine.translate_one("Hello world"), "Привет, мир")
        finally:
            tr._http_json = original


class TestRpaFormats(TempDirMixin, unittest.TestCase):
    """Архивы .rpa всех версий и вложенные пути внутри них."""

    BODY = 'label start:\n    "Hello from the archive."\n'

    def test_all_rpa_versions(self):
        from core.rpa_extractor import extract_native

        for version, suffix in (("RPA-3.0", ".rpa"), ("RPA-3.2", ".rpa"),
                                ("RPA-2.0", ".rpa"), ("RPA-1.0", ".rpa")):
            out = self.tmp / f"out_{version}"
            archive = _write_rpa(self.tmp / f"a_{version}{suffix}",
                                 {"game/script.rpy": self.BODY.encode()},
                                 version=version)
            if version == "RPA-1.0":
                archive = archive.with_suffix(".rpi")
            result = extract_native(archive, out)
            self.assertEqual(result.errors, [], f"{version}: {result.errors}")
            target = out / "script.rpy"
            self.assertTrue(target.exists(), f"{version}: файл не извлечён")
            self.assertIn("Hello from the archive", target.read_text(encoding="utf-8"))

    def test_nested_paths_and_prefix_bytes(self):
        from core.rpa_extractor import extract_native

        out = self.tmp / "out_nested"
        archive = _write_rpa(
            self.tmp / "nested.rpa",
            {"game/story/chapter1.rpy": self.BODY.encode(),
             "game/story/deep/chapter2.rpy": "label two:\n    \"Second.\"\n".encode()},
            prefix=b"",
        )
        result = extract_native(archive, out)
        self.assertEqual(result.errors, [])
        self.assertTrue((out / "story" / "chapter1.rpy").exists())
        self.assertTrue((out / "story" / "deep" / "chapter2.rpy").exists())

    def test_pipeline_reads_text_from_archive_only(self):
        """Папка game/ — только archive.rpa (как в FireOnTheMountain)."""
        from core.pipeline import Pipeline

        game = self.tmp / "arch_game" / "game"
        game.mkdir(parents=True, exist_ok=True)
        _write_rpa(game / "archive.rpa",
                   {"game/script.rpy": ('label start:\n'
                                        '    "Before Ignus can finish, he disappears."\n'
                                        '    e "Hello."\n').encode(),
                    "game/screens.rpy": ('screen x():\n'
                                         '    textbutton _("Start") action Start()\n').encode()})

        settings = Settings(game_path=game, cache_path=self.tmp / "arch_cache.json",
                            backup=False, engine="mock")
        pipeline = Pipeline(settings)
        project = pipeline.scan()
        self.assertTrue(project.rpa_files, "архив не найден")
        self.assertTrue(project.needs_preparation)
        pipeline.prepare()
        parsed = pipeline.analyze()
        originals = [i.original for i in parsed.items]
        self.assertIn("Before Ignus can finish, he disappears.", originals)
        self.assertIn("Start", originals)


class TestAtlAndIdentity(TempDirMixin, unittest.TestCase):
    """Свойства ATL не переводятся, а строки без букв получают блок."""

    def _build(self, body: str) -> tuple:
        from core.pipeline import Pipeline

        game = self.tmp / "atl_game" / "game"
        game.mkdir(parents=True, exist_ok=True)
        (game / "script.rpy").write_text(body, encoding="utf-8")
        settings = Settings(game_path=game, cache_path=self.tmp / "atl_cache.json",
                            backup=False, engine="mock")
        pipeline = Pipeline(settings)
        pipeline.scan()
        parsed = pipeline.analyze()
        report = pipeline.translate(parsed, show_progress=False)
        build = pipeline.build(parsed)
        tl = (game / "tl" / "russian" / "script.rpy").read_text(encoding="utf-8")
        return parsed, tl

    def test_atl_properties_not_translated(self):
        body = (
            'label start:\n'
            '    show blood07:\n'
            '        alpha 0.5\n'
            '        blend "multiply"\n'
            '    "Real dialogue here."\n'
        )
        parsed, tl = self._build(body)
        originals = [i.original for i in parsed.items]
        self.assertIn("Real dialogue here.", originals)
        self.assertNotIn("multiply", originals)
        self.assertNotIn("multiply", tl)

    def test_identity_block_for_letterless_line(self):
        body = (
            'label start:\n'
            '    "{cps=1}...{/cps}{w=1.0}{nw}"\n'
            '    "Real dialogue here."\n'
        )
        _, tl = self._build(body)
        self.assertEqual(tl.count("translate russian"), 2,
                         "строка без букв тоже должна получить блок")


class TestThrottling(TempDirMixin, unittest.TestCase):
    def test_throttle_detection(self):
        from core.translator import ThrottleError, is_throttle_error

        self.assertTrue(is_throttle_error(ThrottleError("x")))
        self.assertTrue(is_throttle_error(RuntimeError("HTTP 429: too many requests")))
        self.assertTrue(is_throttle_error(RuntimeError("You made too many requests")))
        self.assertFalse(is_throttle_error(RuntimeError("connection reset")))

    def test_rate_limiter_slows_down(self):
        from core.translator import RateLimiter

        limiter = RateLimiter(0.0)
        first = limiter.adapt()
        second = limiter.adapt()
        self.assertGreater(first, 0.0)
        self.assertGreater(second, first)
        self.assertLessEqual(second, 2.0)

    def test_batch_fails_fast_on_throttle(self):
        """После 429 не мучаем остаток пачки — сразу отдаём None."""
        engine = ThrottlingEngine()
        results = engine.translate_batch(["a", "b", "c", "d"])
        self.assertEqual(results, [None, None, None, None])
        self.assertEqual(len(engine.calls), 1, "движок должен вызваться один раз")

    def test_pipeline_aborts_instead_of_hanging(self):
        """Долгий «шторм повторов» превращается в понятную остановку."""
        import time as _time

        from core.pipeline import Pipeline

        settings = Settings(game_path=self.project_dir / "game", engine="google",
                            cache_path=self.tmp / "throttle_cache.json", backup=False)
        settings.workers = 2
        settings.batch_size = 5

        pipeline = Pipeline(settings)
        pipeline.scan()
        parsed = pipeline.analyze()
        pipeline._engine = ThrottlingEngine(settings)

        started = _time.monotonic()
        report = pipeline.translate(parsed, show_progress=False)
        elapsed = _time.monotonic() - started

        self.assertTrue(report.aborted)
        self.assertIn("ограничивает", report.abort_reason)
        self.assertEqual(report.translated, 0)
        self.assertGreater(report.failed, 0)
        self.assertLess(elapsed, 10.0, "остановка должна быть быстрой")

        # Неудачи считаются по строкам, а не по попыткам: дублей быть не должно.
        total = sum(len(c) for c in pipeline._make_chunks(parsed)) \
            if hasattr(pipeline, "_make_chunks") else None
        self.assertEqual(len(report.failed_texts), report.failed)
        self.assertEqual(len(set(report.failed_texts)), len(report.failed_texts))
        if total is not None:
            self.assertLessEqual(report.failed, total)

    def test_cancel_stops_translation(self):
        from core.pipeline import Pipeline

        settings = Settings(game_path=self.project_dir / "game", engine="google",
                            cache_path=self.tmp / "cancel_cache.json", backup=False)
        settings.workers = 1
        settings.batch_size = 2

        pipeline = Pipeline(settings, should_cancel=lambda: True)
        pipeline.scan()
        parsed = pipeline.analyze()
        pipeline._engine = ThrottlingEngine(settings)

        report = pipeline.translate(parsed, show_progress=False)
        self.assertTrue(report.aborted)
        self.assertIn("пользовател", report.abort_reason)


class TestEngineFallback(unittest.TestCase):
    """Когда бесплатный сервис блокирует IP — переключаемся, а не сдаёмся."""

    def _settings(self, **kw):
        return Settings(**kw)

    def _chain(self, **kw):
        from core.pipeline import Pipeline

        return Pipeline(self._settings(**kw)).engine_chain()

    def test_chain_order(self):
        self.assertEqual(self._chain(engine="google"),
                         ["google", "google-web", "mymemory"])
        self.assertEqual(self._chain(engine="google-web"),
                         ["google-web", "google", "mymemory"])
        self.assertEqual(self._chain(engine="deepl"),
                         ["deepl", "google", "google-web", "mymemory"])
        self.assertEqual(self._chain(engine="libre",
                                     libretranslate_url="http://localhost:5000"),
                         ["libre", "google", "google-web", "mymemory"])

    def test_mock_and_none_have_no_backups(self):
        self.assertEqual(self._chain(engine="mock"), ["mock"])
        self.assertEqual(self._chain(engine="none"), ["none"])

    def test_libre_without_url_is_not_in_chain(self):
        self.assertEqual(self._chain(engine="google", libretranslate_url=None),
                         ["google", "google-web", "mymemory"])

    def test_pinned_engine_has_no_backups(self):
        from core.pipeline import Pipeline

        pipeline = Pipeline(self._settings(engine="google"))
        pipeline._engine = ThrottlingEngine()
        self.assertEqual(pipeline.engine_chain(), ["throttle"])


class TestEngineFailover(TempDirMixin, unittest.TestCase):
    """Все движки ограничивают — перевод честно останавливается, но не молча."""

    class NamedThrottle(ThrottlingEngine):
        def __init__(self, name="throttle", settings=None):
            super().__init__(settings)
            self.name = name

        def clone(self):
            return TestEngineFailover.NamedThrottle(self.name, self.settings)

    def _pipeline(self):
        tmp = Path(tempfile.mkdtemp(prefix="rpt_failover_"))
        self.addCleanup(lambda: __import__("shutil").rmtree(tmp, ignore_errors=True))
        game = tmp / "game"
        game.mkdir(parents=True)
        lines = ["label start:", ""]
        for index in range(8):
            lines.append(f'    narrator "failover line {index} needs a backup engine"')
        lines += ["", "    return", ""]
        (game / "script.rpy").write_text("\n".join(lines), encoding="utf-8")

        settings = Settings(game_path=game, engine="google",
                            cache_path=tmp / "cache.json", log_path=tmp / "t.log",
                            backup=False, quality_check=False,
                            verify_after_build=False)
        settings.workers = 2
        settings.batch_size = 4
        settings.slow_mode = False
        from core.pipeline import Pipeline

        pipeline = Pipeline(settings)
        pipeline.scan()
        return pipeline

    def _install_factory(self):
        import core.pipeline as pipeline_module

        tried: list[str] = []

        def factory(name, settings, fallback=True):
            tried.append(name)
            return self.NamedThrottle(name, settings)

        original = pipeline_module.create_engine
        pipeline_module.create_engine = factory
        self.addCleanup(setattr, pipeline_module, "create_engine", original)
        return tried

    def test_all_engines_throttled_switches_then_stops(self):
        pipeline = self._pipeline()
        parsed = pipeline.analyze()
        tried = self._install_factory()

        report = pipeline.translate(parsed, show_progress=False)

        self.assertEqual(tried, ["google", "google-web", "mymemory"],
                         "при лимитах нужно пройти всю цепочку движков")
        switched = [w for w in report.warnings if "переключаюсь" in w]
        self.assertEqual(len(switched), 2)
        self.assertIn("google-web", switched[0])
        self.assertIn("mymemory", switched[1])
        self.assertTrue(report.aborted)
        self.assertIn("ограничивает", report.abort_reason)
        self.assertEqual(report.translated, 0)
        self.assertGreater(report.failed, 0)
        self.assertEqual(report.failed, len(set(report.failed_texts)))

    def test_backup_engine_finishes_the_work(self):
        """Первый движок заблокирован — второй доводит перевод до конца."""
        pipeline = self._pipeline()
        parsed = pipeline.analyze()

        import core.pipeline as pipeline_module

        calls = {"google": 0}

        class Blocked(self.NamedThrottle):
            pass

        def factory(name, settings, fallback=True):
            if name == "google":
                calls["google"] += 1
                return Blocked(name, settings)          # всегда лимит
            return GoodEngine(settings)                 # запасной работает

        original = pipeline_module.create_engine
        pipeline_module.create_engine = factory
        self.addCleanup(setattr, pipeline_module, "create_engine", original)

        report = pipeline.translate(parsed, show_progress=False)

        self.assertEqual(report.failed, 0, "запасной движок должен всё перевести")
        self.assertEqual(report.recovered, 0)
        self.assertGreaterEqual(calls["google"], 1)
        translated = [i for i in parsed.items if i.translation]
        self.assertEqual(len(translated), len(parsed.items))
        self.assertFalse(report.aborted)


class TestSlowMode(TempDirMixin, unittest.TestCase):
    """Сервис отбил всё — не сдаёмся, а замедляемся и доводим перевод."""

    def _pipeline(self):
        tmp = Path(tempfile.mkdtemp(prefix="rpt_slow_"))
        self.addCleanup(lambda: __import__("shutil").rmtree(tmp, ignore_errors=True))
        game = tmp / "game"
        game.mkdir(parents=True)
        lines = ["label start:", ""]
        for index in range(6):
            lines.append(f'    narrator "slow mode line {index} should be translated"')
        lines += ["", "    return", ""]
        (game / "script.rpy").write_text("\n".join(lines), encoding="utf-8")

        settings = Settings(game_path=game, engine="google",
                            cache_path=tmp / "cache.json", log_path=tmp / "t.log",
                            backup=False, quality_check=False,
                            verify_after_build=False)
        settings.workers = 2
        settings.batch_size = 3
        settings.retry_rounds = 0          # дожим выключаем: проверяем именно его
        from core.pipeline import Pipeline

        pipeline = Pipeline(settings)
        pipeline.scan()
        return pipeline

    def test_slow_mode_recovers_after_throttling(self):
        pipeline = self._pipeline()
        parsed = pipeline.analyze()

        import core.pipeline as pipeline_module

        state = {"calls": 0}

        class Recovering(ThrottlingEngine):
            name = "recovering"

            def clone(self):
                return self

            def translate_batch(self, texts):
                state["calls"] += 1
                # Медленный режим поднимает паузу между запросами — как только
                # она стала заметной, «сервис сжалился» и отдаёт перевод.
                delay = float(getattr(self.settings, "request_delay", 0.0) or 0.0)
                if delay >= 3.0:
                    return [f"RU:{text}" for text in texts]
                from core.translator import ThrottleError

                raise ThrottleError("HTTP 429")

        def factory(name, settings, fallback=True):
            return Recovering(settings)

        slept: list[float] = []
        original_sleep = pipeline_module.time.sleep
        original_factory = pipeline_module.create_engine
        pipeline_module.create_engine = factory
        pipeline_module.time.sleep = lambda seconds: slept.append(seconds)
        self.addCleanup(setattr, pipeline_module, "create_engine", original_factory)
        self.addCleanup(setattr, pipeline_module.time, "sleep", original_sleep)

        report = pipeline.translate(parsed, show_progress=False)

        self.assertTrue(any("медленный режим" in w for w in report.warnings))
        self.assertTrue(slept, "медленный режим должен делать паузы")
        self.assertEqual(report.failed, 0,
                         "медленный режим обязан дотащить строки до конца")
        self.assertFalse(report.aborted)
        translated = [i for i in parsed.items if i.translation]
        self.assertEqual(len(translated), len(parsed.items))

    def test_slow_mode_gives_up_on_dead_service(self):
        pipeline = self._pipeline()
        parsed = pipeline.analyze()

        import core.pipeline as pipeline_module

        def factory(name, settings, fallback=True):
            return TestEngineFailover.NamedThrottle(name, settings)

        original_sleep = pipeline_module.time.sleep
        original_factory = pipeline_module.create_engine
        pipeline_module.create_engine = factory
        pipeline_module.time.sleep = lambda seconds: None
        self.addCleanup(setattr, pipeline_module, "create_engine", original_factory)
        self.addCleanup(setattr, pipeline_module.time, "sleep", original_sleep)

        started = __import__("time").monotonic()
        report = pipeline.translate(parsed, show_progress=False)
        elapsed = __import__("time").monotonic() - started

        self.assertTrue(report.aborted)
        self.assertGreater(report.failed, 0)
        self.assertLess(elapsed, 20.0, "мёртвый сервис не должен держать программу")

    def test_slow_mode_can_be_disabled(self):
        pipeline = self._pipeline()
        pipeline.settings.slow_mode = False
        parsed = pipeline.analyze()

        import core.pipeline as pipeline_module

        def factory(name, settings, fallback=True):
            return TestEngineFailover.NamedThrottle(name, settings)

        original_sleep = pipeline_module.time.sleep
        original_factory = pipeline_module.create_engine
        pipeline_module.create_engine = factory
        pipeline_module.time.sleep = lambda seconds: None
        self.addCleanup(setattr, pipeline_module, "create_engine", original_factory)
        self.addCleanup(setattr, pipeline_module.time, "sleep", original_sleep)

        report = pipeline.translate(parsed, show_progress=False)
        self.assertTrue(report.aborted)
        self.assertFalse(any("медленный режим" in w for w in report.warnings))


# ===========================================================================
# 14. Шрифты с кириллицей
# ===========================================================================
class TestFonts(TempDirMixin, unittest.TestCase):
    def _make_game(self, font_name: str = "fonts/Latin.ttf") -> Path:
        game = self.tmp / "fontgame" / "game"
        (game / "fonts").mkdir(parents=True, exist_ok=True)
        (game / "script.rpy").write_text(
            'define e = Character("Eileen")\n\n'
            "label start:\n"
            '    e "Hello, how are you?"\n'
            '    "The sun was setting."\n'
            "    return\n", "utf-8")
        (game / "gui.rpy").write_text(
            "init -2 python:\n"
            f'    gui.text_font = "{font_name}"\n'
            f'    gui.name_text_font = "{font_name}"\n\n'
            "style say_dialogue:\n"
            f'    font "{font_name}"\n'
            "    size 22\n", "utf-8")
        (game / "tl").mkdir(exist_ok=True)
        return game

    # --- сам шрифт -----------------------------------------------------
    def test_pt_serif_has_full_cyrillic(self):
        from core.fonts import FONT_FILES, FONTS_SOURCE_DIR, font_has_cyrillic

        for filename in FONT_FILES.values():
            path = FONTS_SOURCE_DIR / filename
            self.assertTrue(path.is_file(), f"нет файла {filename}")
            self.assertTrue(font_has_cyrillic(path), f"{filename}: нет кириллицы")

    def test_latin_only_font_is_detected(self):
        try:
            from fontTools import subset  # noqa: F401
        except ImportError:
            self.skipTest("fontTools не установлен")
        from fontTools import subset
        from core.fonts import FONT_FILES, FONTS_SOURCE_DIR, font_has_cyrillic

        source = FONTS_SOURCE_DIR / FONT_FILES[(False, False)]
        target = self.tmp / "Latin.ttf"
        options = subset.Options()
        font = subset.load_font(str(source), options)
        subsetter = subset.Subsetter(options=options)
        subsetter.populate(unicodes=list(range(0x20, 0x7F)))
        subsetter.subset(font)
        subset.save_font(font, str(target), options)

        self.assertFalse(font_has_cyrillic(target))

    # --- поиск ссылок --------------------------------------------------
    def test_font_references_collected(self):
        from core.fonts import collect_font_references

        game = self._make_game()
        project = scan_project(game)
        refs = collect_font_references(project)
        self.assertEqual(refs, ["fonts/Latin.ttf"])

    # --- установка -----------------------------------------------------
    def _pipeline_for(self, game: Path, mode: str):
        from core.pipeline import Pipeline

        settings = Settings(game_path=game, engine="mock", font_mode=mode,
                            cache_path=self.tmp / "cache.json")
        settings.workers = 1
        pipeline = Pipeline(settings)
        pipeline.scan()
        parsed = pipeline.analyze()
        pipeline.translate(parsed, show_progress=False)
        pipeline.build(parsed)
        return pipeline

    def test_pretty_mode_installs_fonts(self):
        from core.fonts import FONT_FILES, FONT_RPY_NAME

        game = self._make_game()
        self._write_latin_only_font(game / "fonts" / "Latin.ttf")
        pipeline = self._pipeline_for(game, "pretty")
        report = pipeline.font_report

        self.assertEqual(report.mode, "pretty")
        fonts_dir = game / "tl" / "russian" / "fonts"
        self.assertTrue(fonts_dir.is_dir())
        for filename in FONT_FILES.values():
            self.assertTrue((fonts_dir / filename).is_file(), filename)
        self.assertTrue((fonts_dir / "OFL.txt").is_file())

        rpy = (game / "tl" / "russian" / FONT_RPY_NAME).read_text("utf-8")
        self.assertIn("config.font_replacement_map", rpy)
        self.assertIn("fonts/Latin.ttf", rpy)
        self.assertIn("init 999 python:", rpy)
        self.assertIn("PT_Serif-Regular.ttf", rpy)

    def test_generated_blocks_are_valid_python(self):
        import re as _re

        from core.fonts import FONT_RPY_NAME

        game = self._make_game()
        self._pipeline_for(game, "pretty")
        text = (game / "tl" / "russian" / FONT_RPY_NAME).read_text("utf-8")

        lines = text.splitlines()
        blocks = 0
        index = 0
        while index < len(lines):
            if not _re.match(r"^init\s+(-?\d+)?\s*python\s*:", lines[index]):
                index += 1
                continue
            index += 1
            body = []
            while index < len(lines) and (lines[index].startswith("    ")
                                          or not lines[index].strip()):
                body.append(lines[index][4:] if lines[index].startswith("    ")
                            else lines[index])
                index += 1
            compile("\n".join(body), "<init>", "exec")
            blocks += 1
        self.assertGreaterEqual(blocks, 2)

    def test_fix_mode_keeps_good_fonts(self):
        from core.fonts import FONT_FILES, FONTS_SOURCE_DIR

        game = self._make_game(font_name="fonts/PT_Serif-Regular.ttf")
        (game / "fonts" / "PT_Serif-Regular.ttf").write_bytes(
            (FONTS_SOURCE_DIR / FONT_FILES[(False, False)]).read_bytes())

        pipeline = self._pipeline_for(game, "fix")
        self.assertEqual(pipeline.font_report.replaced, [])
        self.assertEqual(pipeline.font_report.kept, ["fonts/PT_Serif-Regular.ttf"])
        # в режиме «починить» нечего чинить — файлы не копируются
        self.assertFalse((game / "tl" / "russian" / "fonts").exists())

    def _write_latin_only_font(self, target: Path) -> bool:
        """Кладёт настоящий шрифт без кириллицы (нужен fontTools)."""
        try:
            from fontTools import subset
        except ImportError:
            return False
        from core.fonts import FONT_FILES, FONTS_SOURCE_DIR

        source = FONTS_SOURCE_DIR / FONT_FILES[(False, False)]
        options = subset.Options()
        font = subset.load_font(str(source), options)
        subsetter = subset.Subsetter(options=options)
        subsetter.populate(unicodes=list(range(0x20, 0x7F)))
        subsetter.subset(font)
        subset.save_font(font, str(target), options)
        return True

    def test_fix_mode_replaces_broken_font(self):
        from core.fonts import FONT_RPY_NAME

        game = self._make_game()
        if not self._write_latin_only_font(game / "fonts" / "Latin.ttf"):
            self.skipTest("нет fontTools — нечем создать шрифт без кириллицы")
        pipeline = self._pipeline_for(game, "fix")
        self.assertEqual(pipeline.font_report.replaced, ["fonts/Latin.ttf"])
        rpy = (game / "tl" / "russian" / FONT_RPY_NAME).read_text("utf-8")
        self.assertIn("config.font_replacement_map", rpy)
        self.assertNotIn("init 999 python:", rpy)

    def test_missing_font_is_replaced(self):
        game = self._make_game(font_name="fonts/NoSuchFont.ttf")
        pipeline = self._pipeline_for(game, "fix")
        self.assertEqual(pipeline.font_report.missing, ["fonts/NoSuchFont.ttf"])

    def test_off_mode_does_nothing(self):
        game = self._make_game()
        pipeline = self._pipeline_for(game, "off")
        self.assertEqual(pipeline.font_report.mode, "off")
        self.assertFalse((game / "tl" / "russian" / "fonts").exists())
        self.assertFalse((game / "tl" / "russian" /
                          "zz_renpy_translator_fonts.rpy").exists())

    def test_replace_mode_puts_fonts_in_game_root(self):
        from core.pipeline import Pipeline

        game = self._make_game()
        settings = Settings(game_path=game, engine="mock", font_mode="pretty",
                            mode="replace", cache_path=self.tmp / "cache2.json")
        settings.workers = 1
        pipeline = Pipeline(settings)
        pipeline.scan()
        parsed = pipeline.analyze()
        pipeline.translate(parsed, show_progress=False)
        pipeline.build(parsed, mode="replace")
        self.assertTrue((game / "renpy_translator_fonts" / "PT_Serif-Regular.ttf").is_file())
        self.assertTrue((game / "zz_renpy_translator_fonts.rpy").is_file())


# ===========================================================================
# 14. Реальные скрипты Ren'Py (демо-игра «The Question», MIT)
# ===========================================================================
class TestRealWorld(unittest.TestCase):
    """
    Сверяем идентификаторы translate-блоков с теми, что сгенерировал сам
    движок Ren'Py (файл game/tl/russian/script.rpy из демо-игры).
    """

    FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "real_world"

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="rpt_real_"))
        game = self.tmp / "the_question" / "game"
        game.mkdir(parents=True)
        shutil.copy2(self.FIXTURE_DIR / "the_question_script.rpy", game / "script.rpy")
        shutil.copy2(self.FIXTURE_DIR / "the_question_options.rpy", game / "options.rpy")
        self.game = game
        self.reference = (self.FIXTURE_DIR /
                          "the_question_tl_russian_script.rpy").read_text("utf-8")

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_real_world_ids(self):
        project = scan_project(self.game)
        parsed = parse_project(project, Settings())
        ours = {i.say.block_id for i in parsed.items
                if i.say and i.say.block_id and i.file == "script.rpy"}

        reference_ids = {
            m for m in re.findall(r"^translate russian (\S+):", self.reference, flags=re.M)
            if m != "strings"
        }
        self.assertGreater(len(reference_ids), 50)
        self.assertEqual(reference_ids - ours, set(),
                         "не совпали id, сгенерированные Ren'Py")

    def test_real_world_text_extracted(self):
        project = scan_project(self.game)
        parsed = parse_project(project, Settings())
        stats = parsed.stats()
        self.assertGreater(stats["dialogue"], 30)
        self.assertGreater(stats["narration"], 10)
        self.assertGreater(stats["menu"], 3)
        self.assertEqual(parsed.warnings, [])


class TestLongTextSplit(unittest.TestCase):
    """Деление сверхдлинных строк: лимит API и языки без пробелов."""

    Pipeline = None

    @classmethod
    def setUpClass(cls):
        from core.pipeline import MAX_CHARS_PER_REQUEST, Pipeline

        cls.Pipeline = Pipeline
        cls.limit = MAX_CHARS_PER_REQUEST

    def test_short_text_untouched(self):
        self.assertEqual(self.Pipeline._split_long_text("Привет, мир!"),
                         ["Привет, мир!"])

    def test_latin_split_is_lossless(self):
        text = ("A sentence with [name] and a {b}tag{/b}. " * 200)
        parts = self.Pipeline._split_long_text(text)
        self.assertGreater(len(parts), 1)
        self.assertEqual("".join(parts), text)
        self.assertTrue(all(len(part) <= self.limit for part in parts))
        # подстановки не разрезаны
        for part in parts:
            self.assertEqual(part.count("[name]"), part.count("["))

    def test_chinese_split_by_cjk_punctuation(self):
        text = "你好，[player_name]！今天天气很好，我们一起去公园吧。" * 300
        parts = self.Pipeline._split_long_text(text)
        self.assertGreater(len(parts), 1)
        self.assertEqual("".join(parts), text)
        self.assertTrue(all(len(part) <= self.limit for part in parts))
        for part in parts[:-1]:
            self.assertIn(part[-1], "。！？；…」』〕】",
                          "кусок должен кончаться знаком конца предложения")
        self.assertEqual(sum("[player_name]" in part for part in parts),
                         len(parts), "подстановка разорвана")

    def test_monolith_without_punctuation(self):
        text = "字" * (self.limit * 2 + 37)
        parts = self.Pipeline._split_long_text(text)
        self.assertEqual([len(part) for part in parts],
                         [self.limit, self.limit, 37])
        self.assertEqual("".join(parts), text)

    def test_placeholder_not_cut_in_hard_split(self):
        text = "字" * 4000 + "[player_name]" + "字" * 4000
        parts = self.Pipeline._split_long_text(text)
        self.assertEqual("".join(parts), text)
        self.assertTrue(any("[player_name]" in part for part in parts))


class TestDrainFailures(unittest.TestCase):
    """Строки, упавшие из-за лимитов сервиса, добираются повторными попытками."""

    def _pipeline(self):
        import tempfile
        from pathlib import Path as _P

        from core.pipeline import Pipeline

        tmp = _P(tempfile.mkdtemp(prefix="rpt_drain_"))
        self.addCleanup(lambda: __import__("shutil").rmtree(tmp, ignore_errors=True))
        game = tmp / "game"
        game.mkdir(parents=True)
        lines = ["label start:", ""]
        for index in range(6):
            lines.append(f'    narrator "line number {index} for the drain test"')
        lines += ["", "    return", ""]
        (game / "script.rpy").write_text("\n".join(lines), encoding="utf-8")

        settings = Settings(game_path=game, engine="google",
                            cache_path=tmp / "cache.json", log_path=tmp / "t.log",
                            backup=False, quality_check=False,
                            verify_after_build=False)
        settings.workers = 2
        settings.batch_size = 3
        pipeline = Pipeline(settings)
        pipeline.scan()
        return pipeline, settings

    def test_failed_strings_are_retried_and_recovered(self):
        pipeline, settings = self._pipeline()
        parsed = pipeline.analyze()
        engine = FlakyEngine(fail_batches=1, settings=settings)
        pipeline._engine = engine

        report = pipeline.translate(parsed, show_progress=False)

        self.assertEqual(report.failed, 0, "все строки должны быть переведены "
                                          "повторными попытками")
        self.assertGreater(report.recovered, 0)
        self.assertEqual(report.failed_texts, [])
        translated = [i for i in parsed.items if i.translation]
        self.assertEqual(len(translated), len(parsed.items))

    def test_drain_can_be_disabled(self):
        pipeline, settings = self._pipeline()
        settings.retry_rounds = 0
        parsed = pipeline.analyze()
        pipeline._engine = FlakyEngine(fail_batches=999, settings=settings)

        report = pipeline.translate(parsed, show_progress=False)
        self.assertGreater(report.failed, 0)
        self.assertEqual(report.recovered, 0)


class TestDevToolSkipping(unittest.TestCase):
    """Файлы инструментов разработчика не переводятся (и не портят отчёт)."""

    def test_markers(self):
        from core.rpy_parser import is_dev_tool_file

        self.assertTrue(is_dev_tool_file("ActionEditor3/ActionEditor_screens.rpy"))
        self.assertTrue(is_dev_tool_file("tools/sprite_editor.rpy"))
        self.assertTrue(is_dev_tool_file("testcases.rpy"))
        self.assertFalse(is_dev_tool_file("script.rpy"))
        self.assertFalse(is_dev_tool_file("screens.rpy"))
        self.assertFalse(is_dev_tool_file("mods/MyMod/script.rpy"))

    def test_dev_file_is_skipped(self):
        import tempfile
        from pathlib import Path as _P

        from config import Settings as _S
        from core.rpy_parser import parse_project
        from core.scanner import scan_project

        tmp = _P(tempfile.mkdtemp(prefix="rpt_dev_"))
        self.addCleanup(lambda: __import__("shutil").rmtree(tmp, ignore_errors=True))
        game = tmp / "game"
        (game / "ActionEditor3").mkdir(parents=True)
        (game / "script.rpy").write_text(
            'label start:\n    narrator "Real story line."\n    return\n',
            encoding="utf-8")
        (game / "ActionEditor3" / "ActionEditor_screens.rpy").write_text(
            'screen dev_tool():\n    text "Select a warper function"\n',
            encoding="utf-8")

        project = scan_project(game)
        parsed = parse_project(project, _S())
        texts = [item.original for item in parsed.items]
        self.assertIn("Real story line.", texts)
        self.assertNotIn("Select a warper function", texts)
        self.assertTrue(any("инструментов разработчика" in w
                            for w in parsed.warnings))

        parsed_all = parse_project(project, _S(skip_dev_tools=False))
        texts_all = [item.original for item in parsed_all.items]
        self.assertIn("Select a warper function", texts_all)


class TestReportFailedSection(unittest.TestCase):
    """В отчёте есть список строк, оставшихся без перевода, с файлом и строкой."""

    def test_failed_rows(self):
        from core.pipeline import TranslateReport
        from core.report import build_html_report

        class Item:
            original = "Hello there"
            file = "script.rpy"
            line = 42

        report = TranslateReport(failed=1, failed_texts=["Hello there"])
        html = build_html_report(game_name="G", game_path="/g", language="russian",
                                 translate_report=report, items=[Item()])
        self.assertIn("Строки без перевода", html)
        self.assertIn("script.rpy:42", html)
        self.assertIn("Hello there", html)


class TestExportForAgent(TempDirMixin, unittest.TestCase):
    """Экспорт для агента: контекст, машинный черновик, поля RU:/MT:."""

    def _export(self, translations=None):
        from core.exporter import export_txt

        project = scan_project(self.project_dir / "game")
        parsed = parse_project(project, self.settings)
        out = self.tmp / "export.txt"
        export_txt(project, parsed, out, translations=translations or {})
        return out.read_text(encoding="utf-8"), parsed

    def test_has_context_and_draft(self):
        from core.exporter import export_txt
        from core.rpy_parser import parse_project as _parse

        project = scan_project(self.project_dir / "game")
        parsed = _parse(project, self.settings)
        drafts = {i.original: "ЧЕРНОВИК " + i.original[:20]
                  for i in parsed.items}
        out = self.tmp / "export.txt"
        export_txt(project, parsed, out, translations=drafts)
        text = out.read_text(encoding="utf-8")
        self.assertIn("# ПЕРСОНАЖИ", text)
        self.assertIn("# до:", text)
        self.assertIn("MT: ЧЕРНОВИК", text)
        self.assertIn("RU: ", text)
        # у каждой строки есть номер вида [#файл:строка]
        self.assertEqual(text.count("RU: "), len(parsed.items))

    def test_ui_rows_have_screen_name(self):
        text, _ = self._export()
        self.assertIn("(интерфейс · экран:", text)

    def test_without_draft_mt_missing_but_ru_present(self):
        text, _ = self._export()
        self.assertNotIn("MT: ЧЕРНОВИК", text)
        self.assertIn("RU: ", text)


class TestImportWithDraft(TempDirMixin, unittest.TestCase):
    """Пустой RU: означает «взять машинный черновик MT:»."""

    def test_falls_back_to_mt(self):
        from core.importer import parse_txt

        path = self.tmp / "agent.txt"
        path.write_text(
            "[#script.rpy:10] (реплика · Atticus · сцена: start)\n"
            "# до: Atticus: Hello.\n"
            "EN: You came back.\n"
            "MT: Вы вернулись.\n"
            "RU: \n"
            "\n"
            "[#script.rpy:11] (реплика · Lucius · сцена: start)\n"
            "EN: I always do.\n"
            "MT: Я всегда так делаю.\n"
            "RU: Я всегда возвращаюсь.\n",
            encoding="utf-8")
        parsed = parse_txt(path)
        self.assertEqual(parsed["script.rpy:10"], "Вы вернулись.")
        self.assertEqual(parsed["script.rpy:11"], "Я всегда возвращаюсь.")


class TestUICheck(unittest.TestCase):
    """Диагностика меню: видно, что переводится, а что нет."""

    def test_counts(self):
        import tempfile
        from pathlib import Path as _P

        from core.scanner import scan_project
        from core.uicheck import check_ui

        tmp = _P(tempfile.mkdtemp(prefix="rpt_ui_"))
        self.addCleanup(lambda: __import__("shutil").rmtree(tmp, ignore_errors=True))
        game = tmp / "game"
        game.mkdir(parents=True)
        (game / "script.rpy").write_text("label start:\n    return\n",
                                         encoding="utf-8")
        (game / "screens.rpy").write_text(
            'screen main_menu():\n'
            '    textbutton _("Start") action Start()\n'
            '    text "Made with Ren\'Py"\n'
            '    imagebutton idle "gui/start.png"\n',
            encoding="utf-8")

        report = check_ui(scan_project(game))
        self.assertEqual(report.wrapped, 1)
        self.assertEqual(report.plain, 1)
        self.assertEqual(report.images_total, 1)
        self.assertTrue(any("меню" in line for line in report.lines()))


class TestImageMenuPatch(TempDirMixin, unittest.TestCase):
    """Меню, нарисованное картинками, заменяется текстовыми кнопками."""

    SCREENS = (
        "screen navigation():\r\n"
        "\r\n"
        "    vbox:\r\n"
        "        style_prefix \"navigation\"\r\n"
        "\r\n"
        "        if main_menu:\r\n"
        "\r\n"
        "            imagebutton auto \"gui/menubuttons/start_%s.png\" action Start()\r\n"
        "\r\n"
        "        else:\r\n"
        "\r\n"
        "            imagebutton auto \"gui/menubuttons/save_%s.png\" action ShowMenu(\"save\")\r\n"
        "            imagebutton auto \"gui/menubuttons/quit_%s.png\" action Quit(confirm=not main_menu)\r\n"
        "            imagebutton idle \"gui/menubuttons/logo.png\"\r\n"
    )

    def _project(self):
        from core.scanner import scan_project

        game = self.tmp / "menugame" / "game"
        game.mkdir(parents=True, exist_ok=True)
        (game / "script.rpy").write_text("label start:\n    return\n", encoding="utf-8")
        (game / "screens.rpy").write_bytes(
            b"\xef\xbb\xbf" + self.SCREENS.encode("utf-8"))
        return scan_project(game)

    def test_replaces_with_russian_text(self):
        from core.menu_patch import patch_image_menus

        project = self._project()
        result = patch_image_menus(project)
        self.assertEqual(result.replaced, 3)
        self.assertEqual(len(result.skipped), 1)          # без action — не трогаем
        text = (project.game_dir / "screens.rpy").read_text(encoding="utf-8")
        self.assertIn('textbutton _("Начать") action Start()', text)
        self.assertIn('textbutton _("Сохранить") action ShowMenu("save")', text)
        self.assertIn('textbutton _("Выход") action Quit(confirm=not main_menu)', text)
        self.assertIn('imagebutton idle "gui/menubuttons/logo.png"', text)
        self.assertNotIn('menubuttons/save_%s.png" action', text)

    def test_keeps_crlf_and_bom(self):
        from core.menu_patch import patch_image_menus

        project = self._project()
        patch_image_menus(project)
        raw = (project.game_dir / "screens.rpy").read_bytes()
        self.assertTrue(raw.startswith(b"\xef\xbb\xbf"))
        self.assertIn(b'action Start()  ##', raw)
        self.assertIn(b"\r\n", raw)

    def test_dry_run_writes_nothing(self):
        from core.menu_patch import patch_image_menus

        project = self._project()
        before = (project.game_dir / "screens.rpy").read_bytes()
        result = patch_image_menus(project, dry_run=True)
        self.assertEqual(result.replaced, 3)
        self.assertTrue(result.dry_run)
        self.assertEqual((project.game_dir / "screens.rpy").read_bytes(), before)

    def test_idempotent(self):
        from core.menu_patch import patch_image_menus

        project = self._project()
        patch_image_menus(project)
        again = patch_image_menus(project)
        self.assertEqual(again.replaced, 0)
        self.assertIn("нечего", again.summary())


class TestRepairBroken34(unittest.TestCase):
    """Строки, испорченные 3.4.0 (без слова action), починяются."""

    BROKEN = (
        "screen navigation():\n"
        "    vbox:\n"
        '        textbutton _("Начать") Start()  ## было: imagebutton '
        "gui/menubuttons/start_%s.png\n"
        '        textbutton _("Загрузить") ShowMenu("load")  ## было: '
        "imagebutton gui/menubuttons/load_%s.png\n"
        '        textbutton _("Справка") action ShowMenu("help")  ## было: '
        "imagebutton gui/menubuttons/help_%s.png\n"
        '        textbutton _("Back") action Rollback()\n'
    )

    def _project(self):
        import tempfile

        from core.scanner import scan_project
        from pathlib import Path as _P

        tmp = _P(tempfile.mkdtemp(prefix="rpt_rep_"))
        self.addCleanup(lambda: __import__("shutil").rmtree(tmp, ignore_errors=True))
        game = tmp / "game"
        game.mkdir(parents=True)
        (game / "script.rpy").write_text("label start:\n    return\n", encoding="utf-8")
        (game / "screens.rpy").write_text(self.BROKEN, encoding="utf-8")
        return scan_project(game)

    def test_repairs_only_marked_lines(self):
        from core.menu_patch import repair_broken_textbuttons

        project = self._project()
        fixed, details = repair_broken_textbuttons(project)
        self.assertEqual(fixed, 2)
        text = (project.game_dir / "screens.rpy").read_text(encoding="utf-8")
        self.assertIn('textbutton _("Начать") action Start()  ## было:', text)
        self.assertIn('textbutton _("Загрузить") action ShowMenu("load")', text)
        # уже правильную строку не трогаем, двойного action не появляется
        self.assertEqual(text.count('action action'), 0)
        self.assertEqual(text.count('action ShowMenu("help")'), 1)
        self.assertEqual(text.count('acction'), 0)

    def test_repair_is_idempotent_and_runs_with_patch(self):
        from core.menu_patch import patch_image_menus

        project = self._project()
        result = patch_image_menus(project)
        self.assertEqual(result.repaired, 2)
        again = patch_image_menus(project)
        self.assertEqual(again.repaired, 0)
        text = (project.game_dir / "screens.rpy").read_text(encoding="utf-8")
        self.assertEqual(text.count("action Start()"), 1)
        self.assertNotIn('") Start()', text)


class TestWrapScreenStrings(TempDirMixin, unittest.TestCase):
    """Строки экранов без _() оборачиваются, служебные — нет."""

    SCREENS = (
        "screen about():\r\n"
        "    vbox:\r\n"
        "        text \"Created and Written by\"\r\n"
        "        text \"Music\"\r\n"
        "        text \"\"\r\n"
        "        text \"[page]\"\r\n"
        "        text \"[gui.about!t]\\n\"\r\n"
        "        text \"{u}Team{/u}\"\r\n"
        "        textbutton \"Русский\" action Language(\"russian\")\r\n"
        "        textbutton _(\"Back\") action Return()\r\n"
        "        label \"Credits\"\r\n"
        "        ## text \"комментарий\"\r\n"
    )

    def _project(self):
        from core.scanner import scan_project

        game = self.tmp / "wrapgame" / "game"
        game.mkdir(parents=True, exist_ok=True)
        (game / "script.rpy").write_text("label start:\n    return\n", encoding="utf-8")
        (game / "screens.rpy").write_bytes(b"\xef\xbb\xbf" + self.SCREENS.encode("utf-8"))
        return scan_project(game)

    def test_wraps_only_translatable(self):
        from core.screen_patcher import wrap_plain_strings

        project = self._project()
        report = wrap_plain_strings(project)
        text = (project.game_dir / "screens.rpy").read_text(encoding="utf-8")
        self.assertEqual(report.wrapped, 4)                     # 2 текста + {u}Team{/u} + label
        self.assertIn('text _("Created and Written by")', text)
        self.assertIn('text _("{u}Team{/u}")', text)
        self.assertIn('label _("Credits")', text)
        # служебное не тронуто
        self.assertIn('text ""', text)
        self.assertIn('text "[page]"', text)
        self.assertIn('text "[gui.about!t]\\n"', text)
        self.assertIn('textbutton "Русский" action Language("russian")', text)
        self.assertIn('textbutton _("Back") action Return()', text)
        self.assertIn('## text "комментарий"', text)

    def test_backup_and_crlf(self):
        from core.screen_patcher import wrap_plain_strings

        project = self._project()
        report = wrap_plain_strings(project)
        self.assertTrue(report.backups)
        backup = project.game_dir / "tl" / "russian" / "rt_backup" / "screens.rpy.bak"
        self.assertTrue(backup.exists())
        self.assertIn('text "Created and Written by"',
                      backup.read_text(encoding="utf-8"))
        raw = (project.game_dir / "screens.rpy").read_bytes()
        self.assertTrue(raw.startswith(b"\xef\xbb\xbf"))
        self.assertEqual(raw.count(b"\r\n"), self.SCREENS.count("\r\n"))

    def test_idempotent(self):
        from core.screen_patcher import wrap_plain_strings

        project = self._project()
        wrap_plain_strings(project)
        again = wrap_plain_strings(project)
        self.assertEqual(again.wrapped, 0)

    def test_dry_run_writes_nothing(self):
        from core.screen_patcher import wrap_plain_strings

        project = self._project()
        before = (project.game_dir / "screens.rpy").read_bytes()
        report = wrap_plain_strings(project, dry_run=True)
        self.assertEqual(report.wrapped, 4)
        self.assertEqual((project.game_dir / "screens.rpy").read_bytes(), before)


class TestCjkFallback(unittest.TestCase):
    """Иероглифы и кириллица в одном шрифте: FontGroup."""

    def test_pt_serif_has_no_cjk(self):
        from core.fonts import FONT_FILES, FONTS_SOURCE_DIR, font_has_cjk

        for filename in FONT_FILES.values():
            self.assertFalse(font_has_cjk(FONTS_SOURCE_DIR / filename))

    def test_font_rpy_uses_fontgroup(self):
        import tempfile

        from core.fonts import build_font_rpy
        from core.scanner import scan_project
        from pathlib import Path as _P

        tmp = _P(tempfile.mkdtemp(prefix="rpt_cjk_"))
        self.addCleanup(lambda: __import__("shutil").rmtree(tmp, ignore_errors=True))
        game = tmp / "game"
        game.mkdir(parents=True)
        (game / "script.rpy").write_text("label start:\n    return\n", encoding="utf-8")
        project = scan_project(game)

        code = build_font_rpy(project, "tl/russian/fonts", "pretty", [],
                              ["fonts/SourceHanSans.ttf"])
        self.assertIn("FontGroup", code)
        self.assertIn("0x2E80, 0x9FFF", code)
        self.assertIn("fonts/SourceHanSans.ttf", code)
        self.assertIn("_rt_font_for(False, False)", code)

        plain = build_font_rpy(project, "tl/russian/fonts", "pretty", [])
        self.assertNotIn("FontGroup", plain)

    def test_existing_language_button_gets_cyrillic_font(self):
        import tempfile

        from core.scanner import scan_project
        from core.screen_patcher import patch_language_menus
        from pathlib import Path as _P

        tmp = _P(tempfile.mkdtemp(prefix="rpt_font_"))
        self.addCleanup(lambda: __import__("shutil").rmtree(tmp, ignore_errors=True))
        game = tmp / "game"
        game.mkdir(parents=True)
        (game / "script.rpy").write_text("label start:\n    return\n", encoding="utf-8")
        (game / "screens.rpy").write_text(
            "screen preferences():\n"
            "    vbox:\n"
            '        textbutton "English" action Language(None)\n'
            '        textbutton "Русский" action Language("russian")\n',
            encoding="utf-8")
        project = scan_project(game)

        done, warnings = patch_language_menus(
            project, "russian", "Русский",
            font_override="tl/russian/fonts/PT_Serif-Web-Regular.ttf")
        self.assertEqual(len(done), 1, warnings)
        text = (game / "screens.rpy").read_text(encoding="utf-8")
        self.assertIn('textbutton "Русский" text_font '
                      '"tl/russian/fonts/PT_Serif-Web-Regular.ttf" '
                      'action Language("russian")', text)
        self.assertEqual(text.count('Language("russian")'), 1)


class TestLiteraryTranslation(TempDirMixin, unittest.TestCase):
    """Авторский перевод не затирается машинным."""

    def _pipeline(self):
        from config import Settings
        from core.pipeline import Pipeline
        from pathlib import Path as _P

        game = self.tmp / "litgame" / "game"
        game.mkdir(parents=True, exist_ok=True)
        (game / "script.rpy").write_text(
            'define e = Character("Ellis")\n'
            "label start:\n"
            '    e "Hello there, friend."\n'
            '    e "How are you today?"\n'
            "    return\n", encoding="utf-8")
        settings = Settings(game_path=game, engine="mock",
                            cache_path=self.tmp / "lit_cache.json",
                            log_path=self.tmp / "lit.log", backup=False,
                            language_button=False, patch_language_menu=False,
                            patch_image_menus=False, wrap_screen_text=False,
                            engine_strings=False, font_mode="off")
        pipeline = Pipeline(settings)
        pipeline.scan()
        return pipeline, game

    def test_store_roundtrip(self):
        from core.literary import LiteraryStore

        path = self.tmp / "lit.json"
        store = LiteraryStore(path=path)
        self.assertTrue(store.add("Hello", "Привет"))
        self.assertFalse(store.add("Hello", "Привет"))     # без изменений
        self.assertTrue(store.add("Bye", "Пока"))
        store.save()
        again = LiteraryStore.load_file(path)
        self.assertEqual(len(again), 2)
        self.assertEqual(again.get("Hello"), "Привет")
        self.assertIsNone(again.get("Nope"))

    def test_imported_lines_survive_cache_reset(self):
        pipeline, game = self._pipeline()
        parsed = pipeline.analyze()
        pipeline.translate(parsed, show_progress=False)
        originals = [item.original for item in parsed.items
                     if "friend" in item.original or "today" in item.original]
        pipeline.save_literary({originals[0]: "Лит. строка первая.",
                                originals[1]: "Лит. строка вторая."})
        # кэш выкидываем — как будто программа установлена заново
        pipeline.cache.clear() if hasattr(pipeline.cache, "clear") else None
        try:
            (self.tmp / "lit_cache.json").unlink()
        except OSError:
            pass

        from config import Settings
        from core.pipeline import Pipeline

        fresh = Pipeline(pipeline.settings)
        fresh.scan()
        parsed2 = fresh.analyze()
        report = fresh.translate(parsed2, show_progress=False)
        translations = {item.original: item.translation for item in parsed2.items}
        self.assertEqual(translations[originals[0]], "Лит. строка первая.")
        self.assertEqual(translations[originals[1]], "Лит. строка вторая.")
        self.assertEqual(report.from_literary, 2)
        self.assertTrue((game / "tl" / "russian" / "rt_literary.json").exists())

    def test_only_file_strings_become_literary(self):
        """В авторское хранилище не должен попадать машинный перевод."""
        pipeline, game = self._pipeline()
        parsed = pipeline.analyze()
        pipeline.translate(parsed, show_progress=False)   # в кэше теперь MT
        imported = self.tmp / "lit_in.json"
        imported.write_text(
            '{"strings": [{"original": "Hello there, friend.",'
            ' "translation": "Привет, друг."}]}', encoding="utf-8")
        result = pipeline.import_file(imported)
        store = pipeline.literary()
        self.assertEqual(len(store), 1)
        self.assertEqual(store.get("Hello there, friend."), "Привет, друг.")
        self.assertIn("Привет, друг.", result.pairs["Hello there, friend."])

    def test_already_russian_strings_are_not_sent_to_engine(self):
        pipeline, _game = self._pipeline()
        parsed = pipeline.analyze()
        report = pipeline.translate(parsed, show_progress=False)
        self.assertGreaterEqual(report.already_russian, 0)
        self.assertFalse(any(item.translation.startswith("[ru]")
                             for item in parsed.items
                             if item.original.startswith("Начать")))


class TestEngineStrings(TempDirMixin, unittest.TestCase):
    """Строки движка: окна подтверждения и служебные сообщения."""

    def _project(self, tl_files=None):
        from core.scanner import scan_project

        game = self.tmp / "enggame" / "game"
        game.mkdir(parents=True, exist_ok=True)
        (game / "script.rpy").write_text("label start:\n    return\n", encoding="utf-8")
        if tl_files:
            tl_dir = game / "tl" / "russian"
            tl_dir.mkdir(parents=True, exist_ok=True)
            for name, text in tl_files.items():
                (tl_dir / name).write_text(text, encoding="utf-8")
        return scan_project(game)

    def test_file_contains_prompts_and_language_guard(self):
        from core.engine_strings import build_engine_rpy

        code = build_engine_rpy("russian")
        self.assertIn("translate russian strings:", code)
        self.assertIn('old "Are you sure you want to quit?"', code)
        self.assertIn('new "Выйти из игры?"', code)
        self.assertIn('"MAIN_MENU":', code)
        self.assertIn("setattr(layout, _rt_name", code)
        self.assertIn("_preferences.language", code)
        self.assertIn("interact_callbacks", code)

    def test_duplicates_from_game_translation_are_skipped(self):
        from core.engine_strings import write_engine_rpy

        project = self._project(tl_files={"common.rpy":
            'translate russian strings:\n\n'
            '    old "Are you sure you want to quit?"\n'
            '    new "Уже переведено игрой."\n'})
        path, count, note = write_engine_rpy(project, "russian")
        self.assertTrue(path.exists())
        text = path.read_text(encoding="utf-8")
        self.assertNotIn('old "Are you sure you want to quit?"', text)
        self.assertIn('old "Are you sure?"', text)
        self.assertIn("уже переведено", note)

    def test_strings_block_skipped_when_only_rpyc(self):
        from core.engine_strings import write_engine_rpy

        project = self._project()
        (project.game_dir / "tl" / "russian").mkdir(parents=True, exist_ok=True)
        (project.game_dir / "tl" / "russian" / "common.rpyc").write_bytes(b"x")
        path, count, _note = write_engine_rpy(project, "russian")
        text = path.read_text(encoding="utf-8")
        self.assertNotIn("translate russian strings:", text)
        self.assertIn("init 999 python:", text)   # подмена окон остаётся

    def test_own_generated_file_is_not_a_conflict(self):
        from core.engine_strings import write_engine_rpy

        project = self._project()
        write_engine_rpy(project, "russian")
        _path, count, note = write_engine_rpy(project, "russian")
        self.assertGreater(count, 10)          # строки не выкинули сами себя
        self.assertEqual(note, "")


# ---------------------------------------------------------------------------
# Текст, нарисованный картинками (3.6.0)
# ---------------------------------------------------------------------------
class TestImageText(unittest.TestCase):
    """Поиск, отрисовка и подключение картинок с текстом."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.game = Path(self._tmp.name) / "game"
        (self.game / "gui").mkdir(parents=True)
        from PIL import Image, ImageDraw, ImageFont

        from core.image_text import default_font

        font = default_font()
        title = Image.new("RGBA", (600, 110), (0, 0, 0, 0))
        ImageDraw.Draw(title).text(
            (20, 24), "MAIN MENU",
            font=ImageFont.truetype(str(font), 56), fill=(255, 255, 255, 255))
        title.save(self.game / "gui" / "title.png")
        Image.new("RGBA", (300, 200), (30, 10, 20, 255)).save(
            self.game / "gui" / "bg.png")
        (self.game / "gui.rpy").write_text(
            'define gui.main_menu_background = "gui/bg.png"\n',
            encoding="utf-8")
        (self.game / "screens.rpy").write_text(
            'screen main_menu():\n'
            '    add "gui/title.png" xalign 0.5\n'
            '    add gui.main_menu_background\n'
            '    textbutton _("Start") action Start()\n\n'
            'style something:\n'
            '    background "gui/title.png"\n',
            encoding="utf-8")
        (self.game / "script.rpy").write_text("label start:\n    return\n",
                                             encoding="utf-8")
        self.project = scan_project(self.game)

    def tearDown(self):
        self._tmp.cleanup()

    def test_finds_text_image(self):
        from core import image_text

        candidates = image_text.find_candidates(self.project)
        rels = [cand.rel for cand in candidates]
        self.assertIn("gui/title.png", rels)
        title = next(cand for cand in candidates if cand.rel == "gui/title.png")
        self.assertGreater(title.score, 0.6)
        self.assertEqual(title.verdict(), "похоже на надпись")

    def test_define_reference_is_found(self):
        from core import image_text

        defines = image_text.collect_defines(self.project)
        self.assertEqual(defines.get("gui.main_menu_background"), "gui/bg.png")
        rels = [cand.rel for cand in image_text.find_candidates(self.project)]
        self.assertIn("gui/bg.png", rels)

    def test_render_keeps_size_and_adds_ru_file(self):
        from PIL import Image

        from core import image_text

        target = image_text.render_replacement(
            self.project, "gui/title.png", "ГЛАВНОЕ МЕНЮ")
        self.assertTrue(target.is_file())
        self.assertEqual(target.name, "title_ru.png")
        with Image.open(self.game / "gui" / "title.png") as before, \
                Image.open(target) as after:
            self.assertEqual(before.size, after.size)
        self.assertGreater(target.stat().st_size, 200)

    def test_patch_and_revert(self):
        from core import image_text

        result = image_text.replace(self.project, "russian", "gui/title.png",
                                    "ГЛАВНОЕ МЕНЮ")
        self.assertEqual(result.rel_new, "gui/title_ru.png")

        screens = (self.game / "screens.rpy").read_text(encoding="utf-8")
        self.assertIn('add rt_img("gui/title.png")', screens)
        # style-блок не трогаем: там значение вычисляется при init
        self.assertIn('background "gui/title.png"', screens)

        helper = result.helper
        self.assertIsNotNone(helper)
        code = helper.read_text(encoding="utf-8")
        self.assertIn("def rt_img(path)", code)
        self.assertIn("_rt_image_language = 'russian'", code)

        # резервная копия не должна быть .rpy — иначе Ren'Py её загрузит
        backups = list((self.game / "tl" / "russian" /
                        image_text.BACKUP_DIR).iterdir())
        self.assertEqual([path.suffix for path in backups], [".bak"])

        image_text.revert_references(self.project, "russian")
        restored = (self.game / "screens.rpy").read_text(encoding="utf-8")
        self.assertIn('add "gui/title.png"', restored)
        self.assertNotIn("rt_img(", restored)
        self.assertFalse((self.game / "gui" / "title_ru.png").exists())
        self.assertFalse(helper.exists())

    def test_helper_switches_only_for_russian(self):
        from core import image_text

        code = image_text.build_helper_rpy(
            "russian", {"gui/title.png": {"ru": "gui/title_ru.png"}})
        self.assertIn('_rt_images = {', code)
        self.assertIn('"gui/title.png": "gui/title_ru.png"', code)
        self.assertIn("if language != _rt_image_language", code)
        compile(code.replace("init 150 python:", "if True:"), "<helper>",
                "exec")


# ---------------------------------------------------------------------------
# Страховка от «expected ':' not found» (3.6.1)
# ---------------------------------------------------------------------------
class TestSyntaxGuard(unittest.TestCase):
    """Строки-блоки без двоеточия: находим и чиним."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.game = Path(self._tmp.name) / "game"
        self.game.mkdir(parents=True)
        (self.game / "c-x_edg01.rpy").write_text(
            'label start:\n'
            '    menu\n'
            '        "Поговорить":\n'
            '            jump talk\n'
            '\n'
            '    screen broken\n'
            '        text "x"\n',
            encoding="utf-8")
        (self.game / "ok.rpy").write_text(
            'screen main_menu():\n'
            '    textbutton _("Start") action Start()\n\n'
            'define x = 1\n\n'
            'init python:\n'
            '    pass\n\n'
            'label fine:\n'
            '    return\n',
            encoding="utf-8")
        self.project = scan_project(self.game)

    def tearDown(self):
        self._tmp.cleanup()

    def test_finds_missing_colon(self):
        from core import syntax_guard

        report = syntax_guard.check_project(self.project, repair=False)
        self.assertEqual(len(report.issues), 2)
        self.assertFalse(report.ok)
        self.assertIn("ошибок: 2", report.summary())
        self.assertIn("не запустится", report.summary())

    def test_no_false_positives(self):
        from core import syntax_guard

        report = syntax_guard.check_project(self.project, repair=False)
        names = {issue.text for issue in report.issues}
        self.assertNotIn("define x = 1", names)
        self.assertNotIn("init python:", names)
        self.assertNotIn("label fine:", names)

    def test_repair_restores_colon_and_backup(self):
        from core import syntax_guard

        report = syntax_guard.check_project(self.project, repair=True)
        self.assertEqual(report.repaired, 2)
        text = (self.game / "c-x_edg01.rpy").read_text(encoding="utf-8")
        self.assertIn("    menu:", text)
        self.assertIn("    screen broken:", text)
        again = syntax_guard.check_project(self.project, repair=False)
        self.assertTrue(again.ok)
        backups = list((self.game / "tl" / syntax_guard.BACKUP_DIR).glob("*.bak"))
        self.assertEqual(len(backups), 1)

    def test_menu_without_body_is_still_fixed(self):
        """`menu` без отступа у следующей строки тоже чинится."""
        from core import syntax_guard

        broken = self.game / "edge.rpy"
        broken.write_text(
            'label start:\n'
            '    menu\n'
            '    "Да":\n'
            '        jump start\n',
            encoding="utf-8")
        self.project = scan_project(self.game)   # файл появился после setUp
        report = syntax_guard.check_project(self.project, repair=True)
        self.assertTrue(report.repaired >= 1)
        self.assertIn("    menu:\n", broken.read_text(encoding="utf-8"))

    def test_stale_rpyc_removed(self):
        """После правки рядом лежащий .rpyc убираем: его собрали из битого файла."""
        from core import syntax_guard

        broken = self.game / "edge2.rpy"
        broken.write_text('label start:\n    menu\n        "Да":\n'
                          '            jump start\n', encoding="utf-8")
        compiled = broken.with_suffix(".rpyc")
        compiled.write_bytes(b"stale")
        self.project = scan_project(self.game)
        syntax_guard.check_project(self.project, repair=True)
        self.assertFalse(compiled.exists())

    def test_no_false_positive_on_style_and_screen_label(self):
        """`style a is b` и `label title` в экране — не ошибки."""
        from core import syntax_guard

        screen = self.game / "extra_screen.rpy"
        screen.write_text(
            'style window is default\n'
            'style say_label is default\n'
            '\n'
            'screen say(who, what):\n'
            '    label title\n'
            '    if who:\n'
            '        text who\n',
            encoding="utf-8")
        self.project = scan_project(self.game)
        report = syntax_guard.check_project(self.project, repair=False)
        texts = {issue.text for issue in report.issues}
        self.assertNotIn("style window is default", texts)
        self.assertNotIn("label title", texts)

    def test_menu_arguments_restored(self):
        """`menu # <unrecognized: ArgumentInfo>` -> `menu (nvl=True):`."""
        from core import syntax_guard

        broken = self.game / "decompiled.rpy"
        broken.write_text(
            'label edgar_hlwnti:\n'
            '    "Text."\n'
            '    menu # <unrecognized: ArgumentInfo>\n'
            '    #   __version__: 1\n'
            "    #   arguments: [('nvl', 'True')]\n"
            '        "Yes.":\n'
            '            return\n'
            '        "No.":\n'
            '            return\n',
            encoding="utf-8")
        self.project = scan_project(self.game)
        report = syntax_guard.check_project(self.project, repair=True)
        self.assertEqual(report.restored_args, 1)
        text = broken.read_text(encoding="utf-8")
        self.assertIn("    menu (nvl=True):\n", text)
        self.assertEqual(syntax_guard.check_project(self.project,
                                                    repair=False).issues, [])

    def test_lossy_decompile_is_reported(self):
        """Заглушки декомпилятора видны в отчёте, игра при этом чинится."""
        from core import syntax_guard

        broken = self.game / "lossy.rpy"
        broken.write_text(
            '# <unrecognized: SLScreen>\n'
            'label start:\n'
            '    menu # <unrecognized: ArgumentInfo>\n'
            "    #   arguments: [('nvl', 'True')]\n"
            '        "Yes.":\n'
            '            return\n',
            encoding="utf-8")
        self.project = scan_project(self.game)
        report = syntax_guard.check_project(self.project, repair=True)
        self.assertTrue(report.lossy)
        self.assertTrue(any("SLScreen" in item for item in report.lossy))
        self.assertIn("декомпиляции с потерями", "\n".join(report.lines()))

    def test_reported_in_ui_check(self):
        from core.uicheck import check_ui

        report = check_ui(self.project, "russian")
        text = "\n".join(report.lines())
        self.assertIn("синтаксис файлов игры", text)


# ---------------------------------------------------------------------------
# Журнал изменений файлов игры (3.6.2)
# ---------------------------------------------------------------------------
class TestChangeJournal(unittest.TestCase):
    """Кто и когда правил файлы игры."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.game = Path(self._tmp.name) / "game"
        self.game.mkdir(parents=True)
        (self.game / "script.rpy").write_text("label start:\n    return\n",
                                             encoding="utf-8")
        self.project = scan_project(self.game)

    def tearDown(self):
        self._tmp.cleanup()

    def test_record_and_read(self):
        from core import changelog

        changelog.record(self.project, "russian", "image_menu_patch",
                        Path("script.rpy"), "заменено: 3")
        entries = changelog.read(self.project, "russian")
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["action"], "image_menu_patch")
        self.assertEqual(entries[0]["path"], "script.rpy")
        self.assertEqual(entries[0]["detail"], "заменено: 3")
        self.assertIn("time", entries[0])
        self.assertEqual(len(changelog.report_lines(self.project, "russian")), 2)

    def test_journal_survives_missing_project(self):
        from core import changelog

        changelog.record(None, "russian", "x")  # не падает
        self.assertEqual(changelog.read(self.project, "russian"), [])

    def test_ui_check_shows_journal(self):
        from core import changelog
        from core.uicheck import check_ui

        changelog.record(self.project, "russian", "image_replace",
                        Path("script.rpy"), "gui/title.png")
        text = "\n".join(check_ui(self.project, "russian").lines())
        self.assertIn("журнал изменений", text)


# ---------------------------------------------------------------------------
# Экспорт больших проектов (3.7.0)
# ---------------------------------------------------------------------------
class TestLargeExport(unittest.TestCase):
    """TXT потоком, разбиение на части, отказ от DOCX на объёме."""

    COUNT = 12000

    def _parsed(self, count=None):
        from core.models import ParsedFile, ParsedProject, TextItem

        count = count or self.COUNT
        self._tmp = tempfile.TemporaryDirectory()
        game = Path(self._tmp.name) / "game"
        game.mkdir(parents=True)
        (game / "script.rpy").write_text("label start:\n    return\n",
                                        encoding="utf-8")
        self.project = scan_project(game)
        items = [TextItem(uid=f"f.rpy:{index}", file="f.rpy", line=index,
                          kind="dialogue",
                          original=f"Original line number {index}, ok.",
                          character="e", label="start")
                 for index in range(count)]
        return ParsedProject(files=[ParsedFile(path=game / "script.rpy",
                                              items=items)])

    def tearDown(self):
        if getattr(self, "_tmp", None) is not None:
            self._tmp.cleanup()

    def _out(self) -> Path:
        return Path(self._tmp.name)

    def test_txt_split_into_parts(self):
        from core.exporter import export_txt

        parsed = self._parsed()
        result = export_txt(self.project, parsed,
                            self._out() / "export.txt", split=5000)
        self.assertEqual(len(result.parts), 3)      # 5000 + 5000 + 2000
        self.assertEqual(result.count, self.COUNT)
        first = result.path.read_text(encoding="utf-8")
        self.assertIn("ЧТО ДЕЛАТЬ АГЕНТУ", first)     # шапка с правилами
        self.assertGreaterEqual(first.count("EN: "), 4000)
        self.assertEqual(first.count("RU: "), 5000)

    def test_txt_without_split_is_single_file(self):
        from core.exporter import export_txt

        parsed = self._parsed(count=100)
        result = export_txt(self.project, parsed, self._out() / "one.txt")
        self.assertEqual(result.parts, [])
        self.assertEqual(result.path.name, "one.txt")
        self.assertEqual(result.path.read_text(encoding="utf-8").count("RU: "), 100)

    def test_json_stream_is_valid(self):
        import json as jsonlib

        from core.exporter import export_json

        parsed = self._parsed(count=2000)
        result = export_json(self.project, parsed, self._out() / "export.json")
        data = jsonlib.loads(result.path.read_text(encoding="utf-8"))
        self.assertEqual(len(data["strings"]), 2000)
        self.assertEqual(data["meta"]["total_strings"], 2000)

    def test_docx_refused_above_limit(self):
        from core.exporter import DOCX_LIMIT, export_docx

        parsed = self._parsed(count=DOCX_LIMIT + 10)
        result = export_docx(self.project, parsed, self._out() / "big.docx")
        self.assertEqual(result.count, 0)
        self.assertTrue(result.warnings)
        self.assertIn("TXT", result.warnings[0])
        self.assertFalse(result.path.exists())

    def test_export_both_defaults_to_txt_only(self):
        from core.exporter import export_both

        parsed = self._parsed(count=50)
        results = export_both(self.project, parsed, self._out() / "agent")
        self.assertEqual([res.format for res in results], ["txt"])
        self.assertFalse((self._out() / "agent.json").exists())
        self.assertFalse((self._out() / "agent.docx").exists())


# ---------------------------------------------------------------------------
# Слияние и сверка частей экспорта (3.7.1)
# ---------------------------------------------------------------------------
class TestExportParts(unittest.TestCase):
    """Части собираются в один файл без потерь и подмен."""

    COUNT = 1000
    SPLIT = 250

    def setUp(self):
        from core.exporter import export_txt
        from core.models import ParsedFile, ParsedProject, TextItem

        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        game = self.dir / "game"
        game.mkdir(parents=True)
        (game / "script.rpy").write_text("label start:\n    return\n",
                                        encoding="utf-8")
        self.project = scan_project(game)
        items = [TextItem(uid=f"f.rpy:{index}", file="f.rpy", line=index,
                          kind="dialogue",
                          original=f"Original line {index}.",
                          character="e", label="start")
                 for index in range(self.COUNT)]
        self.parsed = ParsedProject(
            files=[ParsedFile(path=game / "script.rpy", items=items)])
        self.single = export_txt(self.project, self.parsed,
                                 self.dir / "one.txt")
        self.parts = export_txt(self.project, self.parsed,
                                self.dir / "e.txt", split=self.SPLIT)

    def tearDown(self):
        self._tmp.cleanup()

    def test_parts_found_and_sorted(self):
        from core.parts import collect_parts

        found = collect_parts(self.dir / "e-part01.txt")
        self.assertEqual(len(found.files), len(self.parts.parts))
        names = [path.name for path in found.files]
        self.assertEqual(names, sorted(names))
        self.assertEqual(found.stem, "e")

    def test_merge_matches_single_export(self):
        from core.parts import collect_parts, merge

        found = collect_parts(self.dir)
        result = merge(found.files, self.dir / "merged.txt")
        self.assertEqual(result.ids, self.COUNT)
        self.assertEqual(result.duplicates, 0)

        def body(path):
            """Тело дословно, вместе с пустыми строками."""
            lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
            index = 0
            while index < len(lines) and (lines[index].startswith("#")
                                          or not lines[index].strip()):
                index += 1
            while index > 0 and not lines[index - 1].strip():
                index -= 1
            return lines[index:]

        merged = body(self.dir / "merged.txt")
        single = body(self.single.path)
        self.assertEqual(len(merged), len(single))
        self.assertEqual(merged, single)

    def test_merge_keeps_russian_text(self):
        from core.parts import collect_parts, merge

        part = self.parts.parts[1]
        text = part.read_text(encoding="utf-8").replace(
            "RU: \n", "RU: Переведённая строка\n")
        part.write_text(text, encoding="utf-8")
        merged = merge(collect_parts(self.dir).files,
                       self.dir / "merged.txt")
        body = merged.path.read_text(encoding="utf-8")
        self.assertIn("RU: Переведённая строка", body)

    def test_no_empty_trailing_part(self):
        """Ровное деление не оставляет часть из одной шапки."""
        from core.exporter import export_txt

        result = export_txt(self.project, self.parsed, self.dir / "t.txt",
                            split=500)  # 1000 строк / 500 = ровно
        self.assertEqual(len(result.parts), 2)
        for part in result.parts:
            text = part.read_text(encoding="utf-8")
            self.assertEqual(text.count("RU: "), 500, part.name)

    def test_verify_reports_all_good(self):
        from core.parts import collect_parts, verify

        report = verify(collect_parts(self.dir).files, self.parsed)
        self.assertTrue(report.ok)
        self.assertEqual(report.ids, self.COUNT)
        self.assertEqual(report.empty_ru, self.COUNT)
        self.assertIn("Всё сходится", "\n".join(report.lines()))

    def test_verify_finds_lost_line(self):
        from core.parts import collect_parts, verify

        part = self.parts.parts[1]
        lines = [line for line in part.read_text(encoding="utf-8").split("\n")
                 if not line.startswith("[#f.rpy:400]")]
        part.write_text("\n".join(lines), encoding="utf-8")
        report = verify(collect_parts(self.dir).files, self.parsed)
        self.assertFalse(report.ok)
        self.assertEqual(report.missing_in_game, ["f.rpy:400"])

    def test_verify_finds_changed_en(self):
        from core.parts import collect_parts, verify

        part = self.parts.parts[0]
        text = part.read_text(encoding="utf-8").replace(
            "EN: Original line 5.", "EN: Подменённая строка.")
        part.write_text(text, encoding="utf-8")
        report = verify(collect_parts(self.dir).files, self.parsed)
        self.assertIn("f.rpy:5", report.changed_en)

    def test_verify_finds_duplicate_id(self):
        from core.parts import collect_parts, verify

        part = self.parts.parts[1]
        text = part.read_text(encoding="utf-8")
        # дублируем первую запись части
        index = text.find("[#")
        end = text.find("RU: ", index) + len("RU: \n")
        text = text + "\n" + text[index:end]
        part.write_text(text, encoding="utf-8")
        report = verify(collect_parts(self.dir).files, self.parsed)
        self.assertTrue(report.duplicate_ids)


# ---------------------------------------------------------------------------
# Отчёт «что переводим» (3.7.5)
# ---------------------------------------------------------------------------
class TestBreakdown(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.game = Path(self._tmp.name) / "novel" / "game"
        self.game.mkdir(parents=True)

    def tearDown(self):
        self._tmp.cleanup()

    def _report(self, files: dict, ready: dict | None = None):
        from core.breakdown import build_breakdown
        from core.rpy_parser import parse_project

        for name, body in files.items():
            (self.game / name).write_text(body, encoding="utf-8")
        project = scan_project(self.game)
        parsed = parse_project(project, Settings(game_path=self.game))
        return build_breakdown(parsed, None, game_translations=ready or {})

    def test_counts_and_types(self):
        report = self._report({
            "a.rpy": (
                'define e = Character("Ellis")\n'
                'label a:\n'
                '    e "Hello there."\n'
                '    e "Hello there."\n'
                '    "Narration line."\n'
                '    menu:\n'
                '        "Go on":\n'
                '            return\n'),
        })
        self.assertEqual(report.unique, 4)       # 3 строки + имя персонажа
        self.assertGreaterEqual(report.per_kind.get("dialogue", 0), 2)
        self.assertEqual(report.items, 5)
        self.assertIn("к переводу", "\n".join(report.lines()))

    def test_already_translated_is_subtracted(self):
        report = self._report(
            {"a.rpy": 'label a:\n    "Hello there."\n    "Another line."\n'},
            ready={"Hello there.": "Привет."})
        self.assertEqual(report.already, 1)
        self.assertEqual(report.to_translate, 1)

    def test_duplicate_files_reported(self):
        body = 'label a:\n    "Same text."\n    "Second text."\n'
        report = self._report({"a.rpy": body, "copy_a.rpy": body})
        self.assertTrue(report.duplicate_files)
        self.assertEqual(report.unique, 2)       # текст не удвоился

    def test_top_files_sorted(self):
        report = self._report({
            "big.rpy": 'label a:\n' + "".join(
                f'    "Line number {i}."\n' for i in range(10)),
            "small.rpy": 'label b:\n    "Only one."\n',
        })
        self.assertEqual(report.top_files[0][0], "big.rpy")


# ---------------------------------------------------------------------------
# Висящие ссылки на экраны после неполной декомпиляции (3.7.4)
# ---------------------------------------------------------------------------
class TestScreenRefs(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.game = Path(self._tmp.name) / "novel" / "game"
        self.game.mkdir(parents=True)
        (self.game / "screens.rpy").write_text(
            'screen say(who, what):\n'
            '    text what\n'
            '\n'
            'screen quick_menu():\n'
            '    textbutton "Save" action ShowMenu("save")\n',
            encoding="utf-8")

    def tearDown(self):
        self._tmp.cleanup()

    def test_missing_screen_found_once(self):
        from core.screen_refs import check_screen_refs

        (self.game / "c-x_edg01.rpy").write_text(
            'label start:\n'
            '    show screen edghlwstart\n'
            '    call screen edghlwstart\n'
            '    show screen canv_overlay\n'
            '    return\n', encoding="utf-8")
        report = check_screen_refs(scan_project(self.game))
        names = [item.split(" ")[0] for item in report.missing]
        self.assertEqual(names.count("edghlwstart"), 1)
        self.assertIn("canv_overlay", names)
        self.assertFalse(report.ok)

    def test_declared_and_builtin_are_not_reported(self):
        from core.screen_refs import check_screen_refs

        (self.game / "c-x_edg01.rpy").write_text(
            'screen canv_overlay():\n'
            '    text "x"\n'
            '\n'
            'label start:\n'
            '    show screen canv_overlay\n'
            '    show screen say\n'
            '    return\n', encoding="utf-8")
        report = check_screen_refs(scan_project(self.game))
        self.assertTrue(report.ok, report.missing)
        self.assertIn("объявлено", report.summary())


# ---------------------------------------------------------------------------
# Прямая замена не портит код, если файл изменился после разбора (3.7.3)
# ---------------------------------------------------------------------------
class TestReplaceGuard(unittest.TestCase):
    SCRIPT = (
        'define e = Character("Ellis")\n'
        'label start:\n'
        '    menu:\n'
        '        "Go on":\n'
        '            jump next\n'
        '    e "Hello there."\n'
        '    return\n'
        '\n'
        'label next:\n'
        '    return\n'
    )

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.game = Path(self._tmp.name) / "novel" / "game"
        self.game.mkdir(parents=True)
        (self.game / "c-x_edg01.rpy").write_text(self.SCRIPT, encoding="utf-8")

    def tearDown(self):
        self._tmp.cleanup()

    def _parsed(self):
        from config import Settings
        from core.pipeline import Pipeline

        settings = Settings(game_path=self.game,
                            cache_path=Path(self._tmp.name) / "c.json",
                            backup=False, engine="mock", mode="replace")
        pipeline = Pipeline(settings)
        pipeline.scan()
        return pipeline.analyze()

    def test_stale_offsets_do_not_corrupt_code(self):
        from core.direct_replacer import replace_in_files
        from core.scanner import scan_project

        parsed = self._parsed()
        for item in parsed.items:
            item.translation = "Перевод"

        # файл изменили после разбора: все смещения «уехали»
        path = self.game / "c-x_edg01.rpy"
        path.write_text("# вставка сверху\n\n" + self.SCRIPT, encoding="utf-8")

        result = replace_in_files(scan_project(self.game), parsed.items)
        self.assertGreaterEqual(result.skipped, 1)
        self.assertTrue(any("изменился после разбора" in w
                            for w in result.warnings))
        text = path.read_text(encoding="utf-8")
        self.assertIn("    menu:\n", text)      # структура не пострадала
        self.assertIn('define e = Character("Ellis")', text)


# ---------------------------------------------------------------------------
# Повторный запуск не переводит заново (3.7.2)
# ---------------------------------------------------------------------------
class TestRecovery(unittest.TestCase):
    """Готовый перевод берётся из игры и из кэша в профиле пользователя."""

    SCRIPT = (
        'define e = Character("Ellis")\n'
        'label start:\n'
        '    e "Good morning, traveller."\n'
        '    "The road was empty."\n'
        '    e "We should go."\n'
        '    return\n'
    )

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.game = self.tmp / "novel" / "game"
        self.game.mkdir(parents=True)
        (self.game / "script.rpy").write_text(self.SCRIPT, encoding="utf-8")

    def tearDown(self):
        self._tmp.cleanup()

    def _settings(self, name: str, **kwargs) -> "Settings":
        from config import Settings

        return Settings(game_path=self.game,
                        cache_path=self.tmp / f"cache_{name}.json",
                        backup=False, engine="mock", **kwargs)

    def test_tl_file_pairs_are_read(self):
        from core.recovery import _pairs_from_tl_file

        tl = self.tmp / "tl_file.rpy"
        tl.write_text(
            'translate russian start_a1b2:\n'
            '\n'
            '    # e "Good morning, traveller."\n'
            '    e "Доброе утро, странник."\n'
            '\n'
            '    # "The road was empty."\n'
            '    "Дорога была пуста."\n'
            '\n'
            'translate russian strings:\n'
            '\n'
            '    old "Start"\n'
            '    new "Начать"\n',
            encoding="utf-8")
        pairs = _pairs_from_tl_file(tl)
        self.assertEqual(pairs.get("Good morning, traveller."),
                         "Доброе утро, странник.")
        self.assertEqual(pairs.get("The road was empty."), "Дорога была пуста.")
        self.assertEqual(pairs.get("Start"), "Начать")

    def test_second_run_takes_translation_from_game(self):
        """Второй прогон с пустым кэшем ничего не переводит заново."""
        from core.pipeline import Pipeline

        # прошлый прогон уже оставил перевод в игре (mode=tl)
        tl_dir = self.game / "tl" / "russian"
        tl_dir.mkdir(parents=True)
        (tl_dir / "zz_renpy_translator_dialogue.rpy").write_text(
            'translate russian start_a1:\n'
            '\n'
            '    # e "Good morning, traveller."\n'
            '    e "Доброе утро, странник."\n'
            '\n'
            'translate russian start_b2:\n'
            '\n'
            '    # "The road was empty."\n'
            '    "Дорога была пуста."\n'
            '\n'
            'translate russian start_c3:\n'
            '\n'
            '    # e "We should go."\n'
            '    e "Нам пора идти."\n'
            '\n'
            'translate russian strings:\n'
            '\n'
            '    old "Ellis"\n'
            '    new "Эллис"\n',
            encoding="utf-8")

        # новая версия программы, новый пустой кэш — как после удаления старой
        second = Pipeline(self._settings("second"))
        second.scan()
        parsed = second.analyze()
        report = second.translate(parsed)
        self.assertEqual(report.translated, 0, "перевод пошёл заново")
        self.assertEqual(report.from_game, 4, "перевод из игры не найден")
        self.assertIn("взято из игры", report.summary())

        # и в кэш он тоже попал — следующий запуск будет таким же быстрым
        self.assertEqual(second.cache.get("Good morning, traveller."),
                         "Доброе утро, странник.")

    def test_literary_translation_survives_cache_loss(self):
        from core.literary import LiteraryStore
        from core.pipeline import Pipeline

        settings = self._settings("lit")
        pipeline = Pipeline(settings)
        pipeline.scan()
        parsed = pipeline.analyze()
        store = LiteraryStore.load(pipeline.project, "russian")
        store.add("Good morning, traveller.", "Доброе утро, странник!")
        store.save()

        fresh = Pipeline(self._settings("lit2"))
        fresh.scan()
        parsed = fresh.analyze()
        report = fresh.translate(parsed)
        self.assertGreaterEqual(report.from_literary, 1)

    def test_cache_lives_in_user_profile(self):
        import config

        config.migrate_legacy_cache  # функция существует
        path = config.default_cache_path()
        self.assertEqual(Path(path).parent, config.user_data_dir())
        self.assertNotEqual(Path(path).parent, config.app_dir())

    def test_legacy_cache_is_migrated(self):
        import json

        import config

        legacy = self.tmp / "translation_cache.json"
        legacy.write_text(json.dumps({"Hello.": "Привет."}), encoding="utf-8")
        target = self.tmp / "profile" / "translation_cache.json"
        target.parent.mkdir(parents=True)
        target.write_text(json.dumps({"Bye.": "Пока."}), encoding="utf-8")

        real_app_dir = config.app_dir
        config.app_dir = lambda: self.tmp  # старый кэш «рядом с exe»
        try:
            config.migrate_legacy_cache(target)
        finally:
            config.app_dir = real_app_dir

        data = json.loads(target.read_text(encoding="utf-8"))
        self.assertEqual(data.get("Hello."), "Привет.")
        self.assertEqual(data.get("Bye."), "Пока.")


if __name__ == "__main__":
    unittest.main(verbosity=2)
