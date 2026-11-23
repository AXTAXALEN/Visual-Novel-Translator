# Основной сценарий тестовой игры
define e = Character("Eileen", color="#c8ffc8")
define n = Character(_("Narrator"), kind=nvl)
define narrator = Character(None)
define cen = Character("???", color="#fff")

default player_name = "Alex"
default points = 0

image bg room = "images/bg_room.png"

label start:

    scene bg room with fade

    "The sun was setting over the horizon."

    e "Hello, how are you?"

    e happy "I'm fine, [player_name]! {b}Really{/b}."

    show eileen happy

    e "This is a \"quoted\" word and a line\nbreak." with dissolve

    menu:
        "Ask about the weather":
            e "It's {i}raining{/i} again."

        "Say nothing" if points > 0:
            "You stay silent."

        "Leave":
            jump chapter_01

    if points > 0:
        e "You have [points] points."
    else:
        "You have no points yet."

    extend " And that's all."

    e "..."
    e "..."

    e """
    A long monologue line one.
    And line two.
    """

    e "Warning: 100% sure? Use \{braces\} carefully."

    $ renpy.say(None, "Этот текст в Python-коде не трогаем")

    return
