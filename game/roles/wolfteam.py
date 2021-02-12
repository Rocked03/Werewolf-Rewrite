from .role import Role


class WolfTeam(Role):
    team = 'wolf'
    actual_wolf = True
    wolfchat = True

    def __init__(self, player):
        super().__init__(player)
    
