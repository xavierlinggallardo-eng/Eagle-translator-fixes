
init 999:



    style say_dialogue:
        outlines [ (absolute(2), "#000", absolute(1), absolute(1)) ]
        color "#FFFFFF"



    style say_label:
        outlines [ (absolute(2), "#000", absolute(1), absolute(1)) ]



    style input_prompt:
        color "#FFFFFF"
        outlines [ (absolute(2), "#000", absolute(1), absolute(1)) ]



    style input:
        color "#FFFFFF"
        outlines [ (absolute(2), "#000", absolute(1), absolute(1)) ]



    style choice_button_text:
        idle_color "#FFFFFF"
        hover_color "#00FF00"
        insensitive_color "#808080"
        outlines [ (absolute(2), "#000", absolute(1), absolute(1)) ]



    style quick_button_text:
        font "DejaVuSans.ttf"
        size 18
        idle_color "#8885"
        hover_color "#0F05"
        insensitive_color "#4445"
        outlines [ (absolute(1), "#000A", absolute(0), absolute(0)) ]
        hover_outlines [ (absolute(1), "#050F", absolute(0), absolute(0)) ]



    style window:
        background None



    style namebox:
        background None



    style choice_button:
        background None



    $ quick_menu = True
    $ suppress_overlay = False






    if config.name == "View of family":
        define y = DynamicCharacter("povname", color="#000080")



    if config.name == "Dual Family":
        define ps = DynamicCharacter("ps_name", color="#faff70")
        define m = DynamicCharacter("m_name", color="#c579fc")
        define x = DynamicCharacter("x_name", color="#ffbcbc")
        define f = DynamicCharacter("f_name", color="#5998ff")
        define pf = DynamicCharacter("pf_name", color="#5998ff")
        define d = DynamicCharacter("d_name", color="#ffaddb")
        define w = DynamicCharacter("w_name", color="#c579fc")
        define s = DynamicCharacter("s_name", color="#faff70")
        define a = DynamicCharacter ("a_name", color="#4fe57a")
        define l = DynamicCharacter ("l_name", color="#4fe57a")
        define n = DynamicCharacter ("n_name", color="#d13cb8")
        define q = DynamicCharacter ("q_name", color="#d13cb8")
        define mu = Character("", color="#c579fc")
        define fu = Character("", color="#5998ff")
        define w2 = DynamicCharacter("w_name", color="#c579fc")
        define m1 = Character("", color="#c579fc")
        define du = Character("", color="#ffaddb")
        define sk = Character("Skeleton", color="#fff")
        define k = Character ("Karen", color="#d13cb8")
        define k2 = Character ("", color="#d13cb8")
        define g1 = Character ("Ghoulish Girl", color="#bcddff")
        define g2 = Character ("Jawsome Girl", color="#d1a22e")
        define r = Character ("Redhead Girl", color="#ff6d6d")
        define o = Character ("Sultry Girl", color="#5e37cc")
        define h = Character ("Hoodie Girl", color="#5cd6b3")
        define i = Character ("Isaac", color="#a00000")
        define c = Character ("Cashier", color="#a00000")
        define b = Character ("Ben", color="#ffb459")
        define b2 = Character ("", color="#ffb459")
        define l2 = Character ("", color="#4fe57a")
        define h2 = Character ("", color="#5cd6b3")
        define r2 = Character ("", color="#ff6d6d")
        define ha = Character ("Mara", color="#5cd6b3")
        define ra = Character ("Maddy", color="#ff6d6d")
        define ho = Character ("Hostess", color="#700024")





    if config.name == "Au-pair innocence SE":
        define t = Character('Tasha', color="#ff5353", image = 'tasha')
        define a = Character('Alex', color="#f0ff00", image = 'alex')
        define f = Character('David', color="#fcc863", image ='david')
        define ala = Character("Ala", color="#c354f7", image ='ala')
        define k = Character("Kevin", color="#9ccd3d", image ='kevin')
        define ta = Character("Tanya", color="#48d877", image ='tanya')
        define o = Character("Oliver", color="#28d9ff", image ='oliver')
        define c = Character('Charlotte', color="#ebf239", image ='charlotte')
        define co = Character('Conor', color="#99c7ff", image ='conor')
        define ma = Character('Mark', color="#59c7ff", image ='mark')
        define j = Character('Jake', color="#75c700", image ='jake')




    if config.name == "Life":
        style say_dialogue:
            font "DejaVuSans.ttf"




    if build.name == "LifeWithPlesuare" or build.name == "LifeWithPleasure":
        style say_dialogue:
            font "DejaVuSans.ttf"
        define gui.text_size = 30





    if config.name == "Where The Heart Is":
        define I = Character("[player_name]", color="#ffffff")
        define M = Character('Monica', color="#e12421")
        define K = Character('Katie', color="#9f8fcc")
        define J = Character('Jenna', color="#fda948")
        define Z = Character('Zarah', color="#939941")
        define E = Character('Elaine', color="#c8ffc8")
        define C = Character('Jolina', color="#927e74")
        define L = Character('Lily', color="#588ebd")
        define D = Character('Debbie', color="#b5287f")
        define A = Character('Angel', color="#588ebd")
        define W = Character('Wanda', color="#b5287f")
        define F = Character('Francois', color="#939941")
        define X = Character ('????', color="#c8ffc8")



    if config.name == "Family Matters":
        define perv = Character('Perv2k16', color="#ffffff")



    if config.name == "Deeper":
        style choice_button_text:
            outlines [ (absolute(1.5), "#000", absolute(1), absolute(1)) ]





    if config.name == "Seraphim Academy":
        style choice_button_text:
            outlines [ (absolute(2), "#000", absolute(1), absolute(1)) ]

        style window:
            background None

        screen choice(items):
            style_prefix "choice"

            fixed:
                viewport:
                    yinitial 0
                    if choice_screen_show_scrollbar:
                        scrollbars "vertical"
                    mousewheel True
                    draggable True


                    side_yfill True

                    vbox:
                        for i in items:
                            textbutton i.caption action [SetVariable("choice_screen_show_scrollbar", False), i.action]



    if config.name == "A New Home" or config.name == "A New Home v0.7":
        define gui.text_font = "DejaVuSans.ttf"
        define gui.choice_button_text_font = gui.text_font









    if config.name == "Babysitter":
        screen control():
            hbox:
                style_group "quick"
                textbutton ("stats") xpos 0 ypos 0 action If(renpy.get_screen("stat_box"), Hide("stat_box"), Show("stat_box"))


        screen quick_menu():

            hbox:
                style_group "quick"

                xalign 0.005
                yalign .99
                textbutton _("◀-o") focus_mask True action Rollback() xpos 1100 yalign 1.0 yoffset 0




                textbutton _("▶▶") focus_mask True action Skip() xpos 1135 yalign 1.0 yoffset 0



            hbox:
                style_group "quick"

                xalign 0.0025
                yalign .995
                textbutton _("Q.Save") action QuickSave()
                textbutton _("Q.Load") action QuickLoad()

                textbutton _("Prefs") action ShowMenu('preferences')
                textbutton _("Load") action ShowMenu("load")
                textbutton _("Save") action ShowMenu('save')

        screen say(who, what, side_image=None, two_window=True):


            if not two_window:


                window:
                    id "window"

                    has vbox:
                        style "say_vbox"

                    if who:
                        text who id "who"

                    text what id "what"

            else:


                vbox xpos 240:
                    style "say_two_window_vbox"

                    if who:
                        window:
                            style "say_who_window"

                            ypos 155

                            text who:
                                size 20
                                outlines [ (absolute(2), "#000", absolute(1), absolute(1)) ]
                                xalign 0.5
                                italic True
                                id "who"

                    window:
                        id "window"

                        has vbox:
                            style "say_vbox"

                        text what id "what":
                            outlines [ (absolute(2), "#000", absolute(1), absolute(1)) ]
                            color "#FFFFFF"
                            size 20


            if side_image:
                add side_image
            else:
                add SideImage() xalign 0.0 yalign 1.0


            hbox:
                xalign 1.0
                yalign 0.99
                style_group "quick"
                textbutton ("▼") focus_mask None action If(renpy.get_screen("say"), HideInterface(), None) xoffset -10 yoffset 0

            use quick_menu

        style quick_button_text is default:

            size 14
            idle_color "#8883"
            selected_idle_color "#cc03"
            insensitive_color "#4443"
            outlines [ (absolute(1), "#000", absolute(0), absolute(0)) ]

        style say_label:
            color "#FFFFFF"

        style menu_choice:
            color "#FFFFFF"
            outlines [ (absolute(2), "#000", absolute(1), absolute(1)) ]
            idle_color "#FFFFFF"
            hover_color "#00FF00"
            insensitive_color "#808080"





    if build.name == "A_Wife_And_Mother":
        style choice_button_text:
            idle_outlines [ (absolute(2), "#FFF", absolute(1), absolute(1)) ]
            hover_outlines [ (absolute(2), "#0F0", absolute(1), absolute(1)) ]
            insensitive_outlines [ (absolute(2), "#F00", absolute(1), absolute(1)) ]



    if config.name == "Freeloading Family":
        define s = DynamicCharacter("sis_name", image="sister", window_left_margin=0, who_color="#e8004d")
        define mel = Character('Melody', image="melody", window_left_margin=0, who_color="#bd01bd")
        define l = Character('Leah', image="leah", window_left_margin=0, who_color="#00abd9")
        define t = Character('Susan', image="susan", window_left_margin=0, who_color="#4e4e4e")
init offset = 999

init python:



    if config.name == "Parental Love":
        renpy.register_style_preference("text", "decorated", style.say_dialogue, "outlines", [ (2, "#000", 1, 1) ])
        renpy.register_style_preference("text", "decorated", style.say_label, "outlines", [ (2, "#000", 1, 1) ])





    if config.name == "Welcome to Temptation":
        config.mouse = { 'default' : [ ('gui/welcome_to_temptation_red5.png', 0, 0)] }




    if config.name == "Babysitter":
        style.say_who_window.background = None
        style.menu_choice_button.background = None
        style.menu_choice_button.hover_background = None
        
        def my_tag(tag, argument, contents):
            return [
                (renpy.TEXT_TAG, u"i"),
                (renpy.TEXT_TAG, u"color=#849eff"), 
            ] + contents + [
                (renpy.TEXT_TAG, u"/color"),
                (renpy.TEXT_TAG, u"/i"),
            ]
        config.custom_text_tags["t"] = my_tag
# Decompiled by unrpyc: https://github.com/CensoredUsername/unrpyc
