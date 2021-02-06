from role import Role
from villageteam import VillageTeam


class Villager(VillageTeam):
    role = 'villager'
    description = 'TBA'

    def __init__(self, player):
        super().__init__(player)

    def night_check(self):
        return True