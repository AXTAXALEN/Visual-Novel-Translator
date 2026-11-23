# Глава 1
label chapter_01:

    "Chapter one begins."

    nvl_narrator "NVL text here."

    call .inner

    return

    label .inner:
        e "Inner local label text." id inner_explicit_id
        return

label chapter_02:
    e "Chapter two starts here."
    return
