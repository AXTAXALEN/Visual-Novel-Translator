screen main_menu():
    tag menu

    frame:
        vbox:
            textbutton _("Start") action Start()
            textbutton "Load" action ShowMenu("load")
            textbutton _("Quit") action Quit(confirm=False)
            text "Version [config.version]"
            text what id "what"
            label _("Language")
            textbutton "Settings" style "button" action ShowMenu("preferences")


screen say(who, what):
    window:
        id "window"
        text what id "what"
