# for translations - keep the keys in English, and fill in the values with the translations

lang = {
    "en": {
        "phrases": {
            "now_daytime": [
                "It is now **daytime**. Use `{0}lynch <player>` to vote to lynch <player>."
            ], # prefix

            "now_nighttime": [
                "It is now **nighttime**."
            ],

            "almost_day": [
                "**A few <villager|pl> awake early and notice it is still dark outside. The night is almost over and there are still whispers heard in the village**"
            ],

            "almost_night": [
                "**As the sun sinks inexorably toward the horizon, turning the lanky pine trees into fire-edged silhouettes, the <villager|pl> are reminded that very little time remains for them to reach a decision; if darkness falls before they have done so, the majority will win the vote. No one will be lynched if there are no votes or an even split.**"
            ],

            "night_summary": [
                "Night lasted **{0}**. The {1} wake up and search the village."
            ],  # time elapsed, villagers



            "no_kills": [
                "The <villager|pl> discover the dead body of a beloved pet penguin, but luckily no one was harmed.",
                "The <villager|pl> discover the dead body of a beloved pet dog, but luckily no one was harmed.",
                "The <villager|pl> discover the dead body of a beloved pet cat, but luckily no one was harmed.",
                "Paw prints and tufts of fur are found circling the village, but everyone seems unharmed.",
                "Some house doors have been opened.",
                "A scent much like that of a wolf permeates the air.",
                "Half-buried wolf droppings are found.",
                "Traces of wolf fur are found."
            ],

            "dead_body": [
                "The dead <body|{0}> of {1}{2} <was|{0}> found. Those remaining mourn the tragedy."
            ],  # 'bodies'/'was' pl, player listing, oxford comma

            'not_enough_votes': [
                "Not enough votes were cast to lynch a player."
            ],
        },

        "teams": {
            "village": "village"
            "wolf": "wolf"
        },

        "roles": {
            "villager": {
                "sg": "villager",
                "pl": "villagers"
            },
            "wolf": {
                "sg": "wolf",
                "pl": "wolves"
            },
        },

        "totems": {

        },


        "plurals" = {
            'body': 'bodies',
            'was': 'were'
        }
    }
}