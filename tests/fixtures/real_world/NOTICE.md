# Реальные скрипты Ren'Py (для регрессионных тестов)

Файлы `the_question_*.rpy` взяты из демо-игры «The Question», входящей в
рендеринг-движок [Ren'Py](https://www.renpy.org/) (автор Tom Rothamel).
Ren'Py распространяется по лицензии MIT, поэтому эти файлы можно использовать
в тестах.

`the_question_tl_russian_script.rpy` — **сгенерированный самим движком** файл
перевода (`game/tl/russian/script.rpy`). Тест `test_real_world_ids` сверяет
идентификаторы `translate russian <id>`, которые вычисляет наш парсер, с теми,
что создал Ren'Py. Совпадение — 100 %.
