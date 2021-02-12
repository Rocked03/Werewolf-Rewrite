from .role import Role


class VillageTeam(Role):
    team = 'village'
    
    def __init__(self, player):
        super().__init__(player)